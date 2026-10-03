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
from .geometry import layer_safe_radius
from .layers import (dedupe_names, has_default_stack, layer_name, make_layer, members_for, next_layer_ids,
                     restack)
from .paths import clean_d, islands, skia_from_d, bounds
from .prepass import auto_name
from .split import Analysis, SplitParams, components, forced_units, split, topo_order, _unit_preserving
from .tiling import auto_mode, default_mode_kept, layer_defaults, lining_pairs, tile_pairs


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
                 active: Optional[Iterable[str]] = None) -> Tuple[List[Layer], dict]:
    """Default layers for all non-plate (active) elements with the given strategy. The smart split
    keeps the pieces that tile one shape together; every layer gets its default mode ('combined'
    for tiled art, :func:`bis.svg.tiling.auto_mode`) and a bevel within that mode's safe radius."""
    fg = foreground_indices(store, active)
    tiles = []
    if strategy == "smart":   # pieces that tile one shape, and edge lines drawn under a piece
        tiles = (tile_pairs(store.elems, fg, store.gaps, store.tolerance, store.art.k)
                 + lining_pairs(store.elems, fg, store.edges, store.tolerance, store.art.k))
    an = Analysis(store.elems, store.gaps, store.edges, fg, store.view_box, store.inside, tiles)
    groups, info = split(an, strategy, params)
    layers: List[Layer] = []
    for i, g in enumerate(groups):
        members = [an.els[k] for k in g]
        mode, sr = layer_defaults(store, [m.id for m in members])
        layers.append(make_layer(f"L{i + 1}", members, i, sr, mode=mode))
    info["combined"] = [L.id for L in layers if L.mode == "combined"]
    dedupe_names(layers)
    return layers, info


def _clamp_bevel(store: ElementStore, L: Layer) -> None:
    """Keep a layer's bevel within the safe radius of its (possibly new) mode. In place."""
    sr = layer_safe_radius(store, L.elementIds, L.mode)
    if sr > 0:
        L.depth.bevel = round(min(L.depth.bevel, 0.9 * sr), 5)


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


def _refresh(store: ElementStore, layers: List[Layer], auto: Dict[str, bool], was_default: bool) -> List[Layer]:
    for L in layers:
        if auto.get(L.id, True):
            L.name = layer_name(members_for(store, L.elementIds))
    restack(layers, was_default)
    dedupe_names(layers)
    return layers


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
    was_default = has_default_stack(layers)
    sel = sorted(ids, key=lambda i: pos[i])
    primary = layers[pos[sel[0]]]
    # layers still on their default mode -> the merged layer gets the default of its new content
    # (merging the tiles of one shape makes it 'combined'); a mode the user picked is kept
    derive = all(default_mode_kept(store, layers[pos[i]].mode, layers[pos[i]].elementIds) for i in sel)
    merged_ids = _sorted_ids(store, [e for i in sel for e in layers[pos[i]].elementIds])
    primary.elementIds = merged_ids
    if derive:
        primary.mode = auto_mode(store, merged_ids)
    sr = layer_safe_radius(store, merged_ids, primary.mode)
    if sr > 0:
        primary.depth.bevel = round(min(primary.depth.bevel, 0.9 * sr), 5)
    auto[primary.id] = all(auto.get(i, True) for i in sel)
    keep = [L for L in layers if L.id not in sel[1:]]
    prio = [float(pos[L.id]) for L in keep]
    out = ordered_legal(store, keep, prio)
    return _refresh(store, out, auto, was_default)


def move(store: ElementStore, project: Project, element_ids: Sequence[str], to_layer_id: Optional[str]) -> List[Layer]:
    eids = _sorted_ids(store, element_ids)
    if not eids:
        raise ValueError("no known elements to move")
    layers = [L.model_copy(deep=True) for L in project.layers]
    auto = _auto_flags(store, layers)
    was_default = has_default_stack(layers)
    pos = {L.id: k for k, L in enumerate(layers)}
    if to_layer_id is not None and to_layer_id not in pos:
        raise ValueError(f"unknown layer id: {to_layer_id}")
    moving = set(eids)
    origin = next((L for L in layers if moving & set(L.elementIds)), None)
    touched = {L.id for L in layers if moving & set(L.elementIds)} | ({to_layer_id} if to_layer_id else set())
    derive = {L.id: default_mode_kept(store, L.mode, L.elementIds) for L in layers if L.id in touched}
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
            mode, sr = layer_defaults(store, eids)
        else:
            mode, sr = origin.mode, layer_safe_radius(store, eids, origin.mode)
        new = make_layer(new_id, members, len(layers), sr, template=origin, mode=mode)
        if origin is not None:
            new.depth.bevel = min(new.depth.bevel, round(0.9 * sr, 5)) if sr > 0 else new.depth.bevel
        layers.append(new)
        prio[new_id] = (pos[origin.id] + 0.5) if origin is not None else -0.5
        auto[new_id] = True
    for L in layers:   # layers that gave or got elements re-derive their default mode
        if L.elementIds and derive.get(L.id):
            mode = auto_mode(store, L.elementIds)
            if mode != L.mode:
                L.mode = mode
                _clamp_bevel(store, L)
    keep = [L for L in layers if L.elementIds]
    out = ordered_legal(store, keep, [prio[L.id] for L in keep])
    return _refresh(store, out, auto, was_default)


def split_layer(store: ElementStore, project: Project, layer_id: str, mode: str,
                project_dir: Optional[Path] = None) -> List[Layer]:
    pos = {L.id: k for k, L in enumerate(project.layers)}
    if layer_id not in pos:
        raise ValueError(f"unknown layer id: {layer_id}")
    if mode not in ("elements", "islands"):
        raise ValueError(f"unknown split mode '{mode}'")
    layers = [L.model_copy(deep=True) for L in project.layers]
    auto = _auto_flags(store, layers)
    was_default = has_default_stack(layers)
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
            mode, sr = layer_defaults(store, ids)
        else:
            mode, sr = None, layer_safe_radius(store, ids, src.mode)
        piece = make_layer(lid, members, 0, sr, template=src, mode=mode)
        piece.depth.z = round(base_z + k * 0.13 / max(1, len(groups)), 6)
        auto[lid] = auto.get(src.id, True) or k > 0
        pieces.append(piece)
    k0 = pos[layer_id]
    out = layers[:k0] + pieces + layers[k0 + 1:]
    prio = [float(i) for i in range(len(out))]
    out = ordered_legal(store, out, prio)
    return _refresh(store, out, auto, was_default)


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
