"""Tiled art: pieces that butt against each other along a shared edge (round 4).

A flat icon often colours ONE shape in sections - Google Home's house in red / blue / yellow /
green, the Maps pin, the Drive and Play triangles - or paints a shading wedge over a piece
(Gmail). Extruded and bevelled piece by piece, every shared edge becomes a V groove in which the
plate shows through (white seams), and pieces on different layers step up and down against each
other. Icon Composer's answer is a group with *Combined* lighting: one glass body for the union
silhouette, the colours painted on from the layer art (the worker and viewport build
``mode='combined'`` layers from the silhouette + layer texture).

Two decisions are made here:

* :func:`tile_pairs` - elements that tile a shape: opaque pieces that butt against each other along
  a substantial straight CUT (the colour sections of one object; a short rounded tail where the
  cut runs into a cap is allowed - Play's arms). The smart split keeps such pieces on one layer.
  Curved or kinked shared edges are left alone: they are the outline of one object lying on
  another (Earth's waves, the plates of Stack, a ring around a planet) and read best as stacked
  layers; so are inlays (a piece set into a notch of another: Wallet's window).
  :func:`lining_pairs` (an edge line drawn under a piece) and :func:`print_pairs` (round 5: a print
  on a card that another object lies across - Translate's 文 under the G card) join the same way.
* :func:`auto_mode` - a layer is 'combined' when its occlusion-cut regions share a substantial part
  of their outlines with each other (their own bevels would otherwise cut grooves between them), or
  when a translucent region overlays other regions of the layer (a shading overlay is paint, not a
  separate glass slab). Layers of separate pieces (Calculator's symbols) stay 'individual', and so
  do see-through layers (a translucent region over nothing, a raster): the combined body is
  painted from the layer texture, which carries no alpha.

All lengths are art units (the longer viewBox side = 2.0); geometries are compared in SVG units
with the thresholds scaled by the art factor."""
from __future__ import annotations

from typing import List, Sequence, Tuple

import numpy as np
import shapely
import shapely.errors

from .elements import ElementStore, Elem
from .paths import shapely_from_path

# ----------------------------------------------------------------------------------------------
# tunables (art units; chosen on the 68-icon corpus, see tests/test_svg_tiling.py)
# ----------------------------------------------------------------------------------------------
SEAM_EPS = 0.005            # two outlines closer than this coincide (~1.25 px on a 500 px icon)
STRAIGHT_TOL = 0.012        # straight runs of a seam: Douglas-Peucker tolerance
TILE_MIN_LEN = 0.08         # cross-layer tiles: straight shared edge >= this (4 % of the icon) ...
TILE_MIN_FRAC = 0.10        # ... and >= this fraction of the smaller piece's outline ...
TILE_STRAIGHT_FRAC = 0.6    # ... and most of the shared edge is one straight cut ...
CUT_MAX_TAIL = 0.12         # ... with at most this much of it off the straight run (a rounded cap)
INLAY_HULL_FRAC = 0.9       # a piece >= this much inside the other's convex hull is set INTO it
COMBINED_MIN_RATIO = 0.10   # layer: shared outline / total region outline >= this -> 'combined'
COMBINED_MIN_LEN = 0.05     # ... and the shared outline is at least this long
OVERLAY_MIN_COVER = 0.95    # a translucent region with >= this share of its area on other regions
LINING_MAX_WIDTH = 0.03     # lining: the visible rest of a piece mostly covered by another is a band
LINING_MIN_COVER = 0.3      # ... this thin, the coverer hides >= this share of it ...
LINING_MIN_SHARED = 0.45    # ... and the band shares >= this share of its outline with the coverer
PRINT_MIN_VISIBLE = 0.5     # print on a card under an object lying across it: >= this share stays visible
OPAQUE = 0.99


def _polys(g):
    """Polygonal part of a (make_valid) geometry."""
    if g is None or g.is_empty:
        return shapely.Polygon()
    g = shapely.make_valid(g)
    parts = [p for p in shapely.get_parts(g) if p.geom_type in ("Polygon", "MultiPolygon") and p.area > 0]
    if not parts:
        return shapely.Polygon()
    return parts[0] if len(parts) == 1 else shapely.union_all(parts)


def _opaque(e: Elem) -> bool:
    return e.total_opacity >= OPAQUE and e.paint.get("opaque", True) and not e.image


def seam(ga, gb, eps: float):
    """Linework of `ga`'s outline that butts against `gb`: within `eps` of `gb`'s outline, with
    `gb` on the OTHER side. Coincident outlines on the same side (two shapes sharing an outer rim)
    lie along the overlap of the two and are removed with it; a hairline overlap (<= 2 eps wide,
    anti-alias bleed) still counts as butting."""
    try:
        near = ga.boundary.intersection(gb.boundary.buffer(eps, quad_segs=2))
        if near.is_empty:
            return near
        core = ga.intersection(gb).buffer(-eps, quad_segs=2)
        if not core.is_empty:
            near = near.difference(core.buffer(2 * eps, quad_segs=2))
        return near
    except shapely.errors.GEOSException:
        return shapely.LineString()


def cut_length(lines, tol: float, max_tail: float, min_len: float) -> float:
    """Total length of the connected components of `lines` that look like a CUT: the longest
    straight run of the component (Douglas-Peucker with `tol`) covers >= TILE_STRAIGHT_FRAC of it
    and the rest - e.g. where the cut runs into a piece's rounded cap - is at most `max_tail` long.
    A seam along two sides of an object (the corner of a plate lying on another, Stack) or a curve
    (Earth's waves) is not a cut. Components shorter than `min_len` are ignored."""
    if lines is None or lines.is_empty:
        return 0.0
    try:
        merged = shapely.line_merge(shapely.union_all([g for g in shapely.get_parts(lines)
                                                       if g.geom_type in ("LineString", "LinearRing")]))
    except (shapely.errors.GEOSException, ValueError):
        return 0.0
    total = 0.0
    for comp in shapely.get_parts(merged):
        if comp.geom_type not in ("LineString", "LinearRing") or comp.length < min_len:
            continue
        pts = np.asarray(comp.simplify(tol, preserve_topology=False).coords)
        if len(pts) < 2:
            continue
        run = float(np.hypot(*np.diff(pts, axis=0).T).max())
        if run >= TILE_STRAIGHT_FRAC * comp.length and comp.length - run <= max_tail:
            total += comp.length
    return total


# ----------------------------------------------------------------------------------------------
# cross-element tiles (split)
# ----------------------------------------------------------------------------------------------
def tile_pairs(elems: Sequence[Elem], idxs: Sequence[int], gaps: np.ndarray, tol: float,
               k: float) -> List[Tuple[int, int, float]]:
    """(i, j, cut length in art units) for pairs of opaque elements among `idxs` (store indices,
    i < j) that tile a shape: they touch and butt against each other along a straight cut
    (:func:`cut_length`) of >= TILE_MIN_LEN that is >= TILE_MIN_FRAC of the smaller outline, and
    neither is set INTO the other (an inlay: a window in a card, a lens in a body - those keep
    their own planes). Longest first."""
    k = k or 1.0
    eps = SEAM_EPS / k
    cand = [i for i in idxs if _opaque(elems[i])]
    geoms = {i: _polys(elems[i].geom(tol)) for i in cand}
    hulls: dict = {}
    out = []
    for x, i in enumerate(cand):
        gi = geoms[i]
        if gi.is_empty:
            continue
        for j in cand[x + 1:]:
            gj = geoms[j]
            if gj.is_empty or not (gaps[i, j] <= eps):
                continue
            pmin = min(gi.length, gj.length)
            if pmin <= 0:
                continue
            lines = seam(gi, gj, eps)
            total = lines.length if not lines.is_empty else 0.0
            if total * k < TILE_MIN_LEN or total < TILE_MIN_FRAC * pmin:
                continue
            s = cut_length(lines, STRAIGHT_TOL / k, CUT_MAX_TAIL / k, 4 * eps)
            if s * k < TILE_MIN_LEN or s < TILE_STRAIGHT_FRAC * total:
                continue
            small, big = (i, j) if gi.area <= gj.area else (j, i)
            if big not in hulls:
                hulls[big] = geoms[big].convex_hull
            try:
                inlay = geoms[small].intersection(hulls[big]).area >= INLAY_HULL_FRAC * geoms[small].area
            except shapely.errors.GEOSException:
                inlay = False
            if not inlay:
                out.append((min(i, j), max(i, j), round(s * k, 6)))
    out.sort(key=lambda t: -t[2])
    return out


def lining_pairs(elems: Sequence[Elem], idxs: Sequence[int], edges: Sequence[Tuple[int, int]],
                 tol: float, k: float) -> List[Tuple[int, int, float]]:
    """(i, j, shared length) for an opaque element i whose visible rest (minus every opaque element
    painted above it) is a thin band (<= LINING_MAX_WIDTH) running along the edge of ONE element j
    above it that covers much of it: an edge line of j drawn underneath (the yellow stroke inside
    Classroom's frame, the dark inner line of CRD's frame). On its own plane such a band becomes a
    hairline at another height with a groove against j; it belongs to j's layer."""
    k = k or 1.0
    eps = SEAM_EPS / k
    cand = set(i for i in idxs if _opaque(elems[i]))
    above: dict = {}
    for i, j in edges:
        if i in cand and j in cand:
            above.setdefault(i, []).append(j)
    out = []
    for i, ups in above.items():
        gi = _polys(elems[i].geom(tol))
        if gi.is_empty:
            continue
        gups = {j: _polys(elems[j].geom(tol)) for j in ups}
        try:
            vis = _polys(gi.difference(shapely.union_all(list(gups.values()))))
            if vis.is_empty or vis.length <= 0 or 2 * vis.area / vis.length * k > LINING_MAX_WIDTH:
                continue
            for j, gj in gups.items():
                if gj.is_empty or gi.intersection(gj).area < LINING_MIN_COVER * gi.area:
                    continue
                shared = vis.boundary.intersection(gj.buffer(eps, quad_segs=2)).length
                if shared >= LINING_MIN_SHARED * vis.length:
                    out.append((min(i, j), max(i, j), round(shared * k, 6)))
        except shapely.errors.GEOSException:
            continue
    out.sort(key=lambda t: -t[2])
    return out


def print_pairs(elems: Sequence[Elem], idxs: Sequence[int], edges: Sequence[Tuple[int, int]],
                inside: Sequence[Tuple[int, int]], tol: float) -> List[Tuple[int, int, float]]:
    """(i, j, 0.0) for an opaque element j PRINTED on an opaque card i (j lies inside i - the
    analysis' 'inside' relation, i = the top-most such card) that another opaque object lying ACROSS
    the card partly covers: painted above j, overlapping j and i, but not itself on i (Translate's 文
    on the back card, under the front G card). On a plane of its own such a print floats between the
    two cards as a glass slab with bright rims (QA round 4 #5: "the 文 card reads as pale glass"); on
    its card's plane - one 'combined' body painted from the layer art - it stays the card's print. A
    print that nothing from outside its card covers keeps its own plane (the white G on the front
    card, a keypad's keys under keys of the same body, Settings' gear); so does a piece the covering
    objects mostly hide (< PRINT_MIN_VISIBLE of it shows: Translate's pink under-layer of the G card
    belongs with the G card)."""
    cand = set(i for i in idxs if _opaque(elems[i]))
    ins = set(inside)
    on: dict = {}
    for i, j in inside:
        if i in cand and j in cand:
            on.setdefault(j, []).append(i)
    over: dict = {}
    for a, b in edges:
        if a in cand and b in cand:
            over.setdefault(a, []).append(b)
    edge_set = set(edges)
    out = []
    for j, cards in on.items():
        i = max(cards)                                  # the card it is printed on (painted last)
        across = [k for k in over.get(j, ()) if k > j and (i, k) not in ins and (j, k) not in ins
                  and (i, k) in edge_set]
        if not across:
            continue
        gj = _polys(elems[j].geom(tol))
        if gj.is_empty:
            continue
        try:
            hidden = gj.intersection(shapely.union_all([_polys(elems[k].geom(tol)) for k in over[j] if k > j]))
            visible = 1.0 - hidden.area / gj.area
        except shapely.errors.GEOSException:
            continue
        if visible >= PRINT_MIN_VISIBLE:
            out.append((i, j, 0.0))
    out.sort()
    return out


# ----------------------------------------------------------------------------------------------
# per-layer mode
# ----------------------------------------------------------------------------------------------
def _translucent(e: Elem) -> bool:
    return not e.image and (e.total_opacity < OPAQUE or not e.paint.get("opaque", True))


def _neighbours(geoms: Sequence, eps: float) -> List[List[int]]:
    """For every geometry the (ascending) indices of the OTHER geometries within `eps` of it. An
    STRtree query: the pairwise distance loop was O(n^2) - 1.4 s of a 440-dot icon's import, and
    structural edits re-derive the mode several times."""
    n = len(geoms)
    try:
        tree = shapely.STRtree(geoms)
        src, dst = tree.query(geoms, predicate="dwithin", distance=eps)
    except (shapely.errors.GEOSException, ValueError, TypeError):   # (GEOS without dwithin)
        return [[b for b in range(n) if b != a and geoms[b].distance(geoms[a]) <= eps] for a in range(n)]
    out: List[List[int]] = [[] for _ in range(n)]
    for a, b in zip(src.tolist(), dst.tolist()):
        if a != b:
            out[a].append(b)
    return [sorted(o) for o in out]


def region_tiling(regions: Sequence[Tuple[Elem, object]], tol: float, k: float) -> dict:
    """Metrics of a layer's occlusion-cut, plate-clipped regions [(element, pathops path)]:

    * ``shared`` - outline the regions share with each other (art units, each edge counted once);
    * ``ratio`` - shared outline / total region outline;
    * ``overlay`` - a translucent region lies (>= OVERLAY_MIN_COVER) on other regions: shading;
    * ``seeThrough`` - a translucent region (or a raster) shows what lies BELOW the layer. A
      combined body is painted from the layer texture without its alpha, so it would turn such a
      region opaque (Secure Folder's 66 % white folder tab)."""
    k = k or 1.0
    eps = SEAM_EPS / k
    items = []
    for m, p in regions:
        g = _polys(shapely_from_path(p, tol))
        if not g.is_empty:
            items.append((m, g))
    res = {"regions": len(items), "shared": 0.0, "ratio": 0.0, "overlay": False,
           "seeThrough": any(m.image for m, _g in items)}
    if len(items) < 2:
        return res
    geoms = [g for _m, g in items]
    perim = sum(g.length for g in geoms)
    shared = 0.0
    overlay = see = False
    near = _neighbours(geoms, eps)
    for a, (m, g) in enumerate(items):
        others = [geoms[b] for b in near[a]]
        if _translucent(m) and g.area > 0:
            try:
                cover = g.intersection(shapely.union_all(others)).area / g.area if others else 0.0
            except shapely.errors.GEOSException:
                cover = 0.0
            if cover >= OVERLAY_MIN_COVER:
                overlay = True
            else:
                see = True
        if not others:
            continue
        try:
            shared += g.boundary.intersection(shapely.union_all(others).buffer(eps, quad_segs=2)).length
        except shapely.errors.GEOSException:
            continue
    res["shared"] = round(shared * k / 2.0, 6)
    res["ratio"] = round(shared / perim, 4) if perim > 0 else 0.0
    res["overlay"] = bool(overlay)
    res["seeThrough"] = bool(see or res["seeThrough"])
    return res


def mode_for(metrics: dict) -> str:
    """'combined' for tiled regions or shading overlays, unless the layer is see-through."""
    if metrics.get("seeThrough"):
        return "individual"
    if metrics.get("overlay"):
        return "combined"
    if metrics.get("ratio", 0.0) >= COMBINED_MIN_RATIO and metrics.get("shared", 0.0) >= COMBINED_MIN_LEN:
        return "combined"
    return "individual"


def auto_mode(store: ElementStore, element_ids: Sequence[str], regions=None) -> str:
    """Default ``Layer.mode`` for a layer holding `element_ids` ('combined' for tiled art)."""
    from .geometry import layer_regions, members_of   # (geometry imports this module's users)

    if regions is None:
        members = members_of(store, element_ids)
        if len(members) < 2:
            return "individual"
        regions = layer_regions(store, members)
    if len(regions) < 2:
        return "individual"
    return mode_for(region_tiling(regions, store.tolerance, store.art.k))


def layer_defaults(store: ElementStore, element_ids: Sequence[str]) -> Tuple[str, float]:
    """(auto mode, max inscribed radius of the layer's bodies in that mode - ``LayerGeometry.maxRadius``) of a
    layer holding `element_ids` - one occlusion cut."""
    from .geometry import layer_max_radius, layer_regions, members_of

    members = members_of(store, element_ids)
    if not members:
        return "individual", 0.0
    regions = layer_regions(store, members)
    mode = auto_mode(store, element_ids, regions) if len(members) > 1 else "individual"
    return mode, layer_max_radius(store, element_ids, mode, regions)


def default_mode_kept(store: ElementStore, mode: str, element_ids: Sequence[str]) -> bool:
    """True when `mode` is what :func:`auto_mode` picks for `element_ids` - i.e. the user has not
    overridden it, so a structural edit may re-derive it for the edited layer."""
    try:
        return mode == auto_mode(store, element_ids)
    except Exception:  # noqa: BLE001 - a heuristic must never break an edit
        return False
