"""Structural layer operations (re-split, merge, split, move elements) with z-order validation.

All operations work on the element store (never re-parse the SVG) and return a complete new
layer list (bottom -> top). Illegal edits - ones that would need an element to sit both above and
below another layer ("weaving") - raise :class:`ZOrderError` (a ``ValueError``)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from bis.models import Layer, Project
from .elements import ElementStore, Elem, element_name, to_model
from .geometry import layer_max_radius, layer_shape
from .layers import (clamp_bevel, dedupe_names, default_depth, is_image_only, layer_name, make_layer, members_for,
                     next_layer_ids, restack, stack_gap)
from .paths import clean_d, islands, skia_from_d, bounds
from .prepass import auto_name
from .split import Analysis, SplitParams, components, forced_units, split, topo_order, _unit_preserving
from .tiling import auto_mode, default_mode_kept, layer_defaults, lining_pairs, print_pairs, tile_pairs
from bis import stacking


class ZOrderError(ValueError):
    """The requested edit cannot be represented as a stack of layers."""


# ----------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------
def foreground_indices(store: ElementStore, active: Optional[Iterable[str]] = None) -> List[int]:
    """Store indices of the non-plate elements to lay out. `active` = the project's element ids
    (an undone islands split references superseded elements); without it, every element that
    an islands split has not superseded."""
    plate = set(store.plate_ids)
    act = set(active or ()) & set(store.index)
    if not act:
        act = {e.id for e in store.elems} - set(store.replaced)
    return [i for i, e in enumerate(store.elems) if e.id in act and e.id not in plate]


def fresh_layers(store: ElementStore, strategy: str, params: Optional[SplitParams] = None,
                 active: Optional[Iterable[str]] = None, art_scale: float = 1.0) -> Tuple[List[Layer], dict]:
    """Default layers for all non-plate (active) elements with the given strategy. The smart split
    keeps the pieces that tile one shape together; every layer gets its default mode ('combined'
    for tiled art, :func:`bis.svg.tiling.auto_mode`) and the default depth (raster-only layers: flat cards), and
    the layers are stacked at their REAL heights, overlap-aware (:func:`bis.stacking.restack`, PLAN §11 round 8:
    a layer stacks only above the lower layers it overlaps; `art_scale` = ``canvas.art.scale``)."""
    fg = foreground_indices(store, active)
    tiles = []
    if strategy == "smart":   # pieces that tile one shape, edge lines drawn under a piece, covered prints
        tiles = (tile_pairs(store.elems, fg, store.gaps, store.tolerance, store.art.k)
                 + lining_pairs(store.elems, fg, store.edges, store.tolerance, store.art.k)
                 + print_pairs(store.elems, fg, store.edges, store.inside, store.tolerance))
    an = Analysis(store.elems, store.gaps, store.edges, fg, store.view_box, store.inside, tiles)
    groups, info = split(an, strategy, params)
    layers: List[Layer] = []
    shapes: Dict[str, stacking.LayerShape] = {}
    for i, g in enumerate(groups):
        members = [an.els[k] for k in g]
        mode, _mr = layer_defaults(store, [m.id for m in members])
        layers.append(make_layer(f"L{i + 1}", members, mode=mode))
        shapes[layers[-1].id] = layer_shape(store, layers[-1].elementIds, mode, S=art_scale)
    stacking.restack(layers, shapes, art_scale=art_scale)
    info["combined"] = [L.id for L in layers if L.mode == "combined"]
    info["maxRadius"] = {lid: sh.maxRadius for lid, sh in shapes.items()}
    dedupe_names(layers)
    return layers, info


def layer_radii(store: ElementStore, layers: Sequence[Layer]) -> Dict[str, float]:
    """{layer id: maxRadius of its bodies in its mode} (cached per layer content)."""
    return {L.id: layer_max_radius(store, L.elementIds, L.mode) for L in layers}


def layer_shapes(store: ElementStore, layers: Sequence[Layer], art_scale: float = 1.0) -> Dict[str, stacking.LayerShape]:
    """{layer id: :class:`bis.stacking.LayerShape` (maxRadius, footprint, pieces) of its bodies in its mode}
    (cached per layer content) - what the overlap-aware stack needs."""
    return {L.id: layer_shape(store, L.elementIds, L.mode, S=stacking.layer_scale(L, art_scale)) for L in layers}


def _art_scale(project: Project) -> float:
    try:
        return float(project.canvas.art.scale)
    except (AttributeError, TypeError, ValueError):
        return 1.0


def _art_offset(project: Project) -> Tuple[float, float]:
    try:
        return float(project.canvas.art.x), float(project.canvas.art.y)
    except (AttributeError, TypeError, ValueError):
        return 0.0, 0.0


def _assign(store: ElementStore, layers: Sequence[Layer]) -> List[int]:
    """Element index -> layer position (-1 = not in any layer)."""
    a = [-1] * len(store.elems)
    idx = store.index
    for k, L in enumerate(layers):
        for eid in L.elementIds:
            if eid in idx:
                a[idx[eid]] = k
    return a


def _layer_graph(store: ElementStore, assign: Sequence[int]) -> Dict[int, set]:
    succ: Dict[int, set] = {}
    for i, j in store.edges:
        a, b = assign[i], assign[j]
        if a >= 0 and b >= 0 and a != b:
            succ.setdefault(a, set()).add(b)
    return succ


def _find_cycle(succ: Dict[int, set], nodes: Sequence[int]) -> Optional[List[int]]:
    color: Dict[int, int] = {}
    stack_path: List[int] = []

    def dfs(u) -> Optional[List[int]]:
        color[u] = 1
        stack_path.append(u)
        for v in succ.get(u, ()):
            if color.get(v, 0) == 1:
                return stack_path[stack_path.index(v):] + [v]
            if color.get(v, 0) == 0:
                r = dfs(v)
                if r:
                    return r
        stack_path.pop()
        color[u] = 2
        return None

    for n in nodes:
        if color.get(n, 0) == 0:
            r = dfs(n)
            if r:
                return r
    return None


def ordered_legal(store: ElementStore, layers: List[Layer], priority: Sequence[float]) -> List[Layer]:
    """Validate the z-order constraints of `layers` and return them topologically sorted
    (ties broken by `priority`, so a legal order is kept as is)."""
    assign = _assign(store, layers)
    succ = _layer_graph(store, assign)
    cyc = _find_cycle(succ, list(range(len(layers))))
    if cyc:
        names = " → ".join(f"'{layers[k].name}'" for k in cyc)
        raise ZOrderError(f"this edit would break the stacking order (overlapping art would have to be "
                          f"both above and below: {names})")
    edges = [(i, j) for i, j in store.edges if assign[i] >= 0 and assign[j] >= 0]
    order = topo_order(edges, assign, list(range(len(layers))), {k: float(priority[k]) for k in range(len(layers))})
    return [layers[k] for k in order]


def _auto_named(L: Layer, members: Sequence[Elem]) -> bool:
    auto = layer_name(members)
    return L.name == auto or bool(re.fullmatch(re.escape(auto) + r" \d+", L.name))


def _refresh(store: ElementStore, layers: List[Layer], auto: Dict[str, bool], gap: Optional[float],
             art_scale: float = 1.0, art_offset: Tuple[float, float] = (0.0, 0.0)) -> List[Layer]:
    """Names, real-height overlap-aware re-stack (`gap` of the stack before the edit, None = hand-placed) and
    unique names."""
    for L in layers:
        if auto.get(L.id, True):
            L.name = layer_name(members_for(store, L.elementIds))
    restack(layers, layer_shapes(store, layers, art_scale), gap, art_scale, art_offset)
    dedupe_names(layers)
    return layers


def _stack_gap(store: ElementStore, layers: Sequence[Layer], art_scale: float,
               art_offset: Tuple[float, float] = (0.0, 0.0)) -> Optional[float]:
    """The rule-stack gap of the layers BEFORE an edit (None = hand-placed z)."""
    return stack_gap(layers, layer_shapes(store, layers, art_scale), art_scale, art_offset)


def _is_card(store: ElementStore, L: Optional[Layer]) -> bool:
    """The layer holds raster images only (a round-8 flat card)."""
    return L is not None and is_image_only(members_for(store, L.elementIds))


def _card_rule(store: ElementStore, L: Layer, was_card: bool, donor: Optional[Layer] = None) -> None:
    """Flat raster cards across structural edits (PLAN §11 round 8; in place, z kept): a layer that NOW holds raster
    images only becomes a flat card; a former card that now holds vector art too gets a full body back - the
    depth of `donor` (a non-card layer the art came from) or the import default - instead of flattening the
    vector art into a 0.02 card. A layer that stays a card (or stays vector) is left as it is."""
    now_card = _is_card(store, L)
    if now_card and not was_card:
        stacking.card_depth(L.depth)
    elif was_card and not now_card:
        z = L.depth.z
        L.depth = donor.depth.model_copy(deep=True) if donor is not None else default_depth()
        L.depth.z = z
    clamp_bevel(L)


def _auto_flags(store: ElementStore, layers: Sequence[Layer]) -> Dict[str, bool]:
    return {L.id: _auto_named(L, members_for(store, L.elementIds)) for L in layers}


def _sorted_ids(store: ElementStore, ids) -> List[str]:
    idx = store.index
    return sorted({i for i in ids if i in idx}, key=lambda e: idx[e])


# ----------------------------------------------------------------------------------------------
# operations
# ----------------------------------------------------------------------------------------------
def merge(store: ElementStore, project: Project, layer_ids: Sequence[str]) -> List[Layer]:
    ids = list(dict.fromkeys(layer_ids))
    if len(ids) < 2:
        raise ValueError("select at least two layers to merge")
    pos = {L.id: k for k, L in enumerate(project.layers)}
    missing = [i for i in ids if i not in pos]
    if missing:
        raise ValueError(f"unknown layer id(s): {', '.join(missing)}")
    layers = [L.model_copy(deep=True) for L in project.layers]
    auto = _auto_flags(store, layers)
    scale = _art_scale(project)
    offset = _art_offset(project)
    gap = _stack_gap(store, layers, scale, offset)
    sel = sorted(ids, key=lambda i: pos[i])
    primary = layers[pos[sel[0]]]
    # layers still on their default mode -> the merged layer gets the default of its new content
    # (merging the tiles of one shape makes it 'combined'); a mode the user picked is kept
    derive = all(default_mode_kept(store, layers[pos[i]].mode, layers[pos[i]].elementIds) for i in sel)
    was_card = _is_card(store, primary)
    donor = next((layers[pos[i]] for i in sel if not _is_card(store, layers[pos[i]])), None)
    merged_ids = _sorted_ids(store, [e for i in sel for e in layers[pos[i]].elementIds])
    primary.elementIds = merged_ids
    if derive:
        primary.mode = auto_mode(store, merged_ids)
    _card_rule(store, primary, was_card, donor)   # a card merged with vector art is a full body again
    auto[primary.id] = all(auto.get(i, True) for i in sel)
    keep = [L for L in layers if L.id not in sel[1:]]
    prio = [float(pos[L.id]) for L in keep]
    out = ordered_legal(store, keep, prio)
    return _refresh(store, out, auto, gap, scale, offset)


def move(store: ElementStore, project: Project, element_ids: Sequence[str], to_layer_id: Optional[str]) -> List[Layer]:
    eids = _sorted_ids(store, element_ids)
    if not eids:
        raise ValueError("no known elements to move")
    layers = [L.model_copy(deep=True) for L in project.layers]
    auto = _auto_flags(store, layers)
    scale = _art_scale(project)
    offset = _art_offset(project)
    gap = _stack_gap(store, layers, scale, offset)
    pos = {L.id: k for k, L in enumerate(layers)}
    if to_layer_id is not None and to_layer_id not in pos:
        raise ValueError(f"unknown layer id: {to_layer_id}")
    moving = set(eids)
    origin = next((L for L in layers if moving & set(L.elementIds)), None)
    touched = {L.id for L in layers if moving & set(L.elementIds)} | ({to_layer_id} if to_layer_id else set())
    derive = {L.id: default_mode_kept(store, L.mode, L.elementIds) for L in layers if L.id in touched}
    cards = {L.id: _is_card(store, L) for L in layers if L.id in touched}
    # the depth a former card takes when vector art joins it: the first non-card source layer's
    donor = next((L for L in layers if moving & set(L.elementIds) and not cards.get(L.id)), None)
    for L in layers:
        L.elementIds = [e for e in L.elementIds if e not in moving]
    prio = {L.id: float(k) for k, L in enumerate(layers)}
    if to_layer_id is not None:
        target = layers[pos[to_layer_id]]
        target.elementIds = _sorted_ids(store, list(target.elementIds) + eids)
    else:
        new_id = next_layer_ids([L.id for L in layers], 1)[0]
        members = members_for(store, eids)
        if origin is None or derive.get(origin.id, True):
            mode, _mr = layer_defaults(store, eids)
        else:
            mode = origin.mode
        new = make_layer(new_id, members, template=origin, mode=mode)
        if origin is not None:   # slots in right above its source; the re-stack sorts out the heights
            new.depth.z = origin.depth.z
            _card_rule(store, new, cards.get(origin.id, False), donor)   # a card template + vector art
        layers.append(new)
        prio[new_id] = (pos[origin.id] + 0.5) if origin is not None else -0.5
        auto[new_id] = True
    for L in layers:   # layers that gave or got elements re-derive their default mode
        if L.elementIds and derive.get(L.id):
            mode = auto_mode(store, L.elementIds)
            if mode != L.mode:
                L.mode = mode
                clamp_bevel(L)
        if L.elementIds and L.id in cards:   # gave / got elements: raster-only -> flat card, card + vector -> body
            _card_rule(store, L, cards[L.id], donor)
    keep = [L for L in layers if L.elementIds]
    out = ordered_legal(store, keep, [prio[L.id] for L in keep])
    return _refresh(store, out, auto, gap, scale, offset)


def split_layer(store: ElementStore, project: Project, layer_id: str, mode: str,
                project_dir: Optional[Path] = None) -> List[Layer]:
    pos = {L.id: k for k, L in enumerate(project.layers)}
    if layer_id not in pos:
        raise ValueError(f"unknown layer id: {layer_id}")
    if mode not in ("elements", "islands"):
        raise ValueError(f"unknown split mode '{mode}'")
    layers = [L.model_copy(deep=True) for L in project.layers]
    auto = _auto_flags(store, layers)
    scale = _art_scale(project)
    offset = _art_offset(project)
    gap = _stack_gap(store, layers, scale, offset)
    src = layers[pos[layer_id]]
    derive = default_mode_kept(store, src.mode, src.elementIds)
    idx = store.index
    gidx = sorted(idx[e] for e in src.elementIds if e in idx)
    groups: List[List[str]] = []
    if mode == "elements":
        an = Analysis(store.elems, store.gaps, store.edges, gidx, store.view_box, store.inside)
        units = forced_units(an.els)
        if len(set(units)) >= 2:
            lab = {u: [] for u in dict.fromkeys(units)}
            for k, u in enumerate(units):
                lab[u].append(k)
            assign = units
            order = an.order_clusters(assign)
            groups = [[an.els[k].id for k in lab[c]] for c in order]
        else:
            mode = "islands"  # a single (compound) element: fall through to islands
    if mode == "islands":
        if project_dir is None:
            raise ValueError("islands split needs the project directory")
        store = _explode_islands(store, project, [store.elems[i].id for i in gidx], project_dir)
        src.elementIds = [e.id for e in _expanded(store, src.elementIds)]
        idx = store.index
        gidx = sorted(idx[e] for e in src.elementIds if e in idx)
        an = Analysis(store.elems, store.gaps, store.edges, gidx, store.view_box, store.inside)
        adj = an.diag * SplitParams().adjacency_pct / 100.0
        comps = _unit_preserving(components(an, list(range(len(gidx))), adj), forced_units(an.els))
        if len(comps) >= 2:
            assign = [0] * len(gidx)
            for c, comp in enumerate(comps):
                for k in comp:
                    assign[k] = c
            order = an.order_clusters(assign)
            groups = [[an.els[k].id for k in comps[c]] for c in order]
    if len(groups) < 2:
        raise ValueError("this layer cannot be split any further")
    new_ids = [src.id] + next_layer_ids([L.id for L in layers], len(groups) - 1)
    pieces = []
    base_z = src.depth.z
    for k, (lid, ids) in enumerate(zip(new_ids, groups)):
        members = members_for(store, ids)
        if derive:   # the source layer had its default mode: so do the pieces
            mode, _mr = layer_defaults(store, ids)
        else:
            mode = None
        piece = make_layer(lid, members, template=src, mode=mode)
        piece.depth.z = base_z   # a hand-placed stack lifts each piece onto the one below (real heights)
        auto[lid] = auto.get(src.id, True) or k > 0
        pieces.append(piece)
    k0 = pos[layer_id]
    out = layers[:k0] + pieces + layers[k0 + 1:]
    prio = [float(i) for i in range(len(out))]
    out = ordered_legal(store, out, prio)
    return _refresh(store, out, auto, gap, scale, offset)


def _expanded(store: ElementStore, ids: Sequence[str]) -> List[Elem]:
    """Element ids -> store elements, following island replacements (e5 -> e5-1, e5-2, ...)."""
    idx = store.index
    return sorted((store.elems[idx[i]] for i in dict.fromkeys(store.resolve(ids)) if i in idx),
                  key=lambda e: idx[e.id])


def _explode_islands(store: ElementStore, project: Project, ids: Sequence[str],
                     project_dir: Path) -> ElementStore:
    """Replace every multi-island path element among `ids` by one element per island and update
    ``project.elements`` / ``project.layers`` in place.

    New islands are inserted right after their element, which stays in the store (superseded,
    ``store.replaced``) so an undone split keeps working; exploding it again reuses the same
    island ids. Returns a NEW saved store when islands were added (the cached store is never
    mutated - other threads may be reading it)."""
    from .split import compute_analysis

    wanted = set(ids)
    replaced: Dict[str, List[str]] = {}
    inserts: Dict[str, List[Elem]] = {}
    taken = set(store.index)
    for e in store.elems:
        if e.id not in wanted or e.image:
            continue
        if e.id in store.replaced:  # exploded before (that split was undone): reuse its islands
            replaced[e.id] = store.resolve([e.id])
            continue
        parts = islands(e.path, store.tolerance)
        if len(parts) < 2:
            continue
        reps = []
        for k, p in enumerate(parts):
            nid = f"{e.id}-{k + 1}"
            while nid in taken:
                nid += "x"
            taken.add(nid)
            d = clean_d(p, 4)
            ne = Elem(id=nid, uid=f"{e.uid}#i{k}", d=d, paint=e.paint, opacity=e.opacity,
                      group_opacity=e.group_opacity, opacity_group=e.opacity_group,
                      meta={**e.meta, "island": k, "islandOf": e.id, "base": f"{e.uid}#i{k}"},
                      shadow=e.shadow)
            ne._path = skia_from_d(d)
            ne.bbox = bounds(ne._path)
            ne.area = float(ne.geom(store.tolerance).area)
            ne.name = element_name(ne, auto_name)
            reps.append(ne)
        inserts[e.id] = reps
        replaced[e.id] = [r.id for r in reps]
    if not replaced:
        return store
    if inserts:
        new_elems: List[Elem] = []
        for e in store.elems:
            new_elems.append(e)
            new_elems += inserts.get(e.id, [])
        edges, gaps, inside = compute_analysis(new_elems, store.view_box, store.tolerance)
        store = ElementStore(filename=store.filename, view_box=store.view_box, elems=new_elems,
                             gradients=store.gradients, plate=store.plate, warnings=store.warnings, edges=edges,
                             gaps=gaps, inside=inside, tolerance=store.tolerance, source_sha1=store.source_sha1,
                             replaced={**store.replaced, **{k: [r.id for r in v] for k, v in inserts.items()}})
        store.save(project_dir)
    art = store.art
    known = {m.id: m for m in project.elements}
    out = []
    for m in project.elements:
        for i in (store.resolve([m.id]) if m.id in replaced else [m.id]):
            out.append(known[i] if i in known else to_model(store.get(i), art))
    project.elements = list({m.id: m for m in out}.values())
    for L in project.layers:
        if any(i in replaced for i in L.elementIds):
            L.elementIds = list(dict.fromkeys(x for i in L.elementIds
                                              for x in (store.resolve([i]) if i in replaced else [i])))
    return store
    edges, gaps, inside = compute_analysis(new_elems, store.view_box, store.tolerance)
    store = ElementStore(filename=store.filename, view_box=store.view_box, elems=new_elems,
                         gradients=store.gradients, plate=store.plate, warnings=store.warnings, edges=edges,
                         gaps=gaps, inside=inside, tolerance=store.tolerance, source_sha1=store.source_sha1)
    store.save(project_dir)
    art = store.art
    out = []
    for m in project.elements:
        if m.id in replaced:
            out += [to_model(e, art) for e in replaced[m.id]]
        else:
            out.append(m)
    project.elements = out
    for L in project.layers:
        if any(i in replaced for i in L.elementIds):
            L.elementIds = [x for i in L.elementIds for x in ([e.id for e in replaced[i]] if i in replaced else [i])]
    return store
