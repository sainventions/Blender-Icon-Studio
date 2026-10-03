"""Geometry export: pathops paths -> cubic Bezier splines in art space, occlusion-cut regions,
silhouettes, safe bevel radius, corner-preserving polyline smoothing, layer SVGs + textures, and
the hash-cached :class:`GeometryBundle`.

Exported splines are fills: always closed, clipped to the plate outline when there is a plate
(art flush with the plate edge snapped onto it), and cleaned by :mod:`.hygiene` (no micro debris,
spikes, self-intersections or slivers; >= 3 points each)."""
from __future__ import annotations

import json
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pathops
import shapely
import shapely.errors
from picosvg.svg_transform import Affine2D
from shapely import affinity

from bis.models import GeometryBundle, Layer, LayerGeometry, Project, Region, Spline, SplinePoint
from .common import PIPELINE_VERSION, atomic_write_bytes, atomic_write_text, sha1
from .elements import ElementStore, Elem, model_paint
from .paths import bounds, is_empty, op, shapely_from_path, split_contours, union_all
from .plate import PLATE_CLIP_INSET, PLATE_SNAP, plate_clip_path, plate_snap_band
from . import hygiene, raster, textures

# ----------------------------------------------------------------------------------------------
# tunables
# ----------------------------------------------------------------------------------------------
SMOOTH_POLYLINES = True       # corner-preserving smoothing of dense all-straight contours
SMOOTH_MIN_SEGMENTS = 24      # ... with at least this many segments
SMOOTH_STRAIGHT_FRAC = 0.95   # ... of which at least this fraction are straight lines
SMOOTH_CORNER_DEG = 25.0      # turning angles above this stay sharp corners
SMOOTH_MAX_BULGE = 0.006      # art units (~1.5 px on a 500 px icon): straighten if a curve bulges more
SMOOTH_TRACED_MIN_SEGMENTS = 6     # raster traces: smooth every contour ...
SMOOTH_TRACED_CORNER_DEG = 50.0    # ... keeping only clear corners (the trace is already sub-pixel smooth)
SAFE_RADIUS_AREA_TOL = 0.02   # morphological opening may remove <= 2 % of the area
SAFE_RADIUS_CAP = 0.5         # art units
REGION_Z_STEP = 0.001         # zSub per translucent overlap level
TEXTURE_SIZE = 2048
TEXTURE_BUDGET = ((8, 2048), (24, 1024))  # (max layers, max texture px); more layers -> 512 px
CACHE_DIR = "cache"
CACHE_KEEP_SECONDS = 3600     # unreferenced cache files older than this are pruned


# ----------------------------------------------------------------------------------------------
# splines
# ----------------------------------------------------------------------------------------------
def _lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _close(segs: List[tuple], start) -> List[tuple]:
    """A filled contour is implicitly closed: add the closing line when the end is not the start."""
    if segs and start is not None and math.dist(segs[-1][3], start) > 1e-9:
        cur = segs[-1][3]
        segs = segs + [(cur, _lerp(cur, start, 1 / 3), _lerp(cur, start, 2 / 3), start, True)]
    return segs


def _contours(sk: pathops.Path) -> List[Tuple[bool, List[tuple]]]:
    """-> [(closed, [(p0, c1, c2, p1, is_line), ...]), ...] (cubic segments, SVG space). Every
    contour is returned CLOSED: the geometry is filled, and fills close their subpaths."""
    out: List[Tuple[bool, List[tuple]]] = []
    segs: List[tuple] = []
    start = cur = None
    for verb, pts in sk:
        if verb == pathops.PathVerb.MOVE:
            if segs:
                out.append((True, _close(segs, start)))
            segs = []
            start = cur = pts[0]
        elif verb == pathops.PathVerb.LINE:
            p = pts[0]
            segs.append((cur, _lerp(cur, p, 1 / 3), _lerp(cur, p, 2 / 3), p, True))
            cur = p
        elif verb == pathops.PathVerb.QUAD:
            c, p = pts
            segs.append((cur, _lerp(cur, c, 2 / 3), _lerp(p, c, 2 / 3), p, False))
            cur = p
        elif verb == pathops.PathVerb.CUBIC:
            segs.append((cur, pts[0], pts[1], pts[2], False))
            cur = pts[2]
        elif verb == pathops.PathVerb.CONIC:
            raise ValueError("convertConicsToQuads() first")
        elif verb == pathops.PathVerb.CLOSE:
            if cur is not None and start is not None and math.dist(cur, start) > 1e-9:
                segs.append((cur, _lerp(cur, start, 1 / 3), _lerp(cur, start, 2 / 3), start, True))
            if segs:
                out.append((True, segs))
            segs = []
            cur = start
    if segs:
        out.append((True, _close(segs, start)))
    return out


def _drop_degenerate(segs: List[tuple], eps: float) -> List[tuple]:
    return [s for s in segs if not (math.dist(s[0], s[3]) < eps and math.dist(s[0], s[1]) < eps
                                    and math.dist(s[2], s[3]) < eps)]


def smooth_polyline(pts: List[Tuple[float, float]], corner_deg: float = SMOOTH_CORNER_DEG,
                    max_bulge: float = SMOOTH_MAX_BULGE) -> List[dict]:
    """Closed polyline -> G1 cubic spline through every vertex.

    * turning angles above `corner_deg` stay sharp corners;
    * long segments (straight edges between dense curve samples) stay exactly straight and the
      neighbouring curve leaves them tangentially;
    * elsewhere the tangent is the bisector of the adjacent segment directions (Catmull-Rom-like,
      robust to uneven spacing), handle length = |segment| / 3;
    * a segment whose curve would bulge more than `max_bulge` (target units) from its chord is
      straightened (guards against overshoot)."""
    n = len(pts)
    P = np.asarray(pts, dtype=float)
    d_next = np.roll(P, -1, axis=0) - P          # segment i: P[i] -> P[i+1]
    l_seg = np.linalg.norm(d_next, axis=1)
    u_seg = d_next / np.maximum(l_seg, 1e-12)[:, None]
    l_prev, u_prev = np.roll(l_seg, 1), np.roll(u_seg, 1, axis=0)   # segment i-1 ends at P[i]
    med = float(np.median(l_seg)) or 1e-12
    shorter_nb = np.minimum(np.roll(l_seg, 1), np.roll(l_seg, -1))
    straight = (l_seg > 4.0 * med) | ((l_seg > 2.0 * med) & (l_seg > 3.0 * shorter_nb))
    straight_prev = np.roll(straight, 1)
    cosang = np.clip((u_seg * u_prev).sum(axis=1), -1.0, 1.0)
    corner = np.degrees(np.arccos(cosang)) > corner_deg
    t_in = np.empty_like(P)
    t_out = np.empty_like(P)
    for i in range(n):
        if corner[i] or (straight[i] and straight_prev[i]):
            t_in[i], t_out[i] = u_prev[i], u_seg[i]
        elif straight_prev[i]:
            t_in[i] = t_out[i] = u_prev[i]
        elif straight[i]:
            t_in[i] = t_out[i] = u_seg[i]
        else:
            t = u_prev[i] + u_seg[i]
            nt = np.linalg.norm(t)
            t_in[i] = t_out[i] = (t / nt) if nt > 1e-12 else u_seg[i]
    hr = P + t_out * (l_seg / 3.0)[:, None]                    # handle leaving P[i] on segment i
    hl = P - t_in * (l_prev / 3.0)[:, None]                    # handle entering P[i] on segment i-1
    # overshoot guard: bulge of segment i = distance of B(0.5) from its chord
    nxt_hl = np.roll(hl, -1, axis=0)
    mid = (P + 3 * hr + 3 * nxt_hl + np.roll(P, -1, axis=0)) / 8.0
    chord_mid = P + d_next / 2.0
    dm = mid - chord_mid
    bulge = np.abs(u_seg[:, 0] * dm[:, 1] - u_seg[:, 1] * dm[:, 0])
    bad = bulge > np.maximum(max_bulge, 0.0)
    if bad.any():
        idx = np.nonzero(bad)[0]
        hr[idx] = P[idx] + d_next[idx] / 3.0
        nxt = (idx + 1) % n
        hl[nxt] = P[nxt] - d_next[idx] / 3.0
    return [{"co": (P[i, 0], P[i, 1]), "hl": (hl[i, 0], hl[i, 1]), "hr": (hr[i, 0], hr[i, 1])} for i in range(n)]


def path_to_splines(sk: pathops.Path, m: Affine2D, smooth: bool = SMOOTH_POLYLINES,
                    eps: float = 1e-9, nd: int = 6, clean: bool = True, traced: bool = False) -> List[dict]:
    """pathops.Path (SVG space) -> [{closed, points:[{co, hl, hr}]}] in the target space of `m`.

    Lines become cubics with handles at 1/3 and 2/3 (exact); quads are degree-elevated exactly.
    Every contour is closed (filled geometry). Dense all-straight contours (polyline exports,
    traced rasters) are optionally smoothed - unless smoothing makes the contour (or a sibling
    contour) self-intersect, then the exact polygon is kept. With `clean` (the default; target
    space = art units) micro segments, spikes, loops and slivers are removed
    (:func:`hygiene.clean_spline`): every returned spline is closed, simple, has >= 3 points and
    a non-degenerate area. `traced` = the path is a raster trace (sub-pixel polygon of a smooth
    outline): every all-straight contour is smoothed, however few segments (a traced dot is a
    ~12-gon), and only turns above SMOOTH_TRACED_CORNER_DEG stay corners."""
    min_segs = SMOOTH_TRACED_MIN_SEGMENTS if traced else SMOOTH_MIN_SEGMENTS
    corner_deg = SMOOTH_TRACED_CORNER_DEG if traced else SMOOTH_CORNER_DEG
    a, b, c, d, e, f = m.a, m.b, m.c, m.d, m.e, m.f

    def X(p):
        return (a * p[0] + c * p[1] + e, b * p[0] + d * p[1] + f)

    def R(p):
        return (round(p[0], nd) + 0.0, round(p[1], nd) + 0.0)

    contours = []   # (exact points, smoothed points | None)
    for _closed, segs in _contours(sk):
        segs = _drop_degenerate(segs, eps)
        if not segs:
            continue
        exact = [{"co": X(p0), "hl": X(segs[k - 1][2]), "hr": X(c1)} for k, (p0, c1, _c2, _p1, _l) in enumerate(segs)]
        sm = None
        n_lines = sum(1 for s in segs if s[4])
        if smooth and len(segs) >= min_segs and n_lines >= SMOOTH_STRAIGHT_FRAC * len(segs):
            sm = smooth_polyline([X(s[0]) for s in segs], corner_deg)
            if n_lines < len(segs):
                _keep_curves(sm, segs, X)
            if not hygiene.is_simple(sm):
                sm = None   # smoothing overshot into a loop: keep the exact polygon
        contours.append((exact, sm))
    if any(sm is not None for _e, sm in contours) and len(contours) > 1:
        _unsmooth_crossing(contours)
    splines = []
    for exact, sm in contours:
        pts = sm if sm is not None else exact
        if not clean:
            splines.append({"closed": True, "points": [{k: R(v) for k, v in p.items()} for p in pts]})
            continue
        # micro segments of exact contours are boolean-op / rounding debris; a smoothed contour's
        # short segments are real samples of a dense G1 curve (merging them would kink it)
        parts = hygiene.clean_spline(pts, hygiene.MICRO if sm is None else hygiene.DEGENERATE)
        if sm is not None and not parts:
            parts = hygiene.clean_spline(exact)
            sm = None
        for q in parts:
            splines.append({"closed": True, **({"smoothed": True} if sm is not None else {}), "points": q})
    return splines


def _keep_curves(sm: List[dict], segs: List[tuple], X) -> None:
    """smooth_polyline only sees the vertices: put the exact control points of the (<= 5 %) curve
    segments of a mostly-straight contour back - e.g. the plate clip's corner arc on a traced or
    polyline outline - instead of flattening them to their chords. In place."""
    n = len(segs)
    for k, (_p0, c1, c2, _p1, is_line) in enumerate(segs):
        if not is_line:
            sm[k]["hr"] = X(c1)
            sm[(k + 1) % n]["hl"] = X(c2)


def _unsmooth_crossing(contours: List[list]) -> None:
    """A smoothed contour must not cross a sibling contour (a hole bulging through its outer
    contour): revert both to their exact polygons. In place."""
    rings = []
    for exact, sm in contours:
        p, _ = hygiene.sample(sm if sm is not None else exact, 4)
        try:
            rings.append(shapely.LinearRing(p) if len(p) >= 3 else None)
        except (shapely.errors.GEOSException, ValueError):
            rings.append(None)
    valid = [r for r in rings if r is not None]
    if len(valid) < 2:
        return
    tree = shapely.STRtree([r if r is not None else shapely.Point() for r in rings])
    ia, ib = tree.query([r if r is not None else shapely.Point() for r in rings], predicate="intersects")
    for i, j in zip(ia.tolist(), ib.tolist()):
        if i < j and (contours[i][1] is not None or contours[j][1] is not None):
            contours[i] = (contours[i][0], None)
            contours[j] = (contours[j][0], None)


def _bez(a, b, t):
    mt = 1 - t
    return tuple(mt ** 3 * a["co"][k] + 3 * mt * mt * t * a["hr"][k] + 3 * mt * t * t * b["hl"][k] + t ** 3 * b["co"][k]
                 for k in range(2))


def _spline_path(s: dict) -> pathops.Path:
    """One spline as an exact cubic pathops contour (closed)."""
    p = pathops.Path()
    pts = s["points"]
    n = len(pts)
    p.moveTo(*pts[0]["co"])
    for i in range(1, n + 1):
        a, b = pts[i - 1], pts[i % n]
        p.cubicTo(*a["hr"], *b["hl"], *b["co"])
    p.close()
    return p


def annotate_holes(splines: List[dict]) -> None:
    """depth / hole / parent for each spline (three.js Shape + holes). Contours never cross after
    simplify, so a point on the curve itself (segment midpoint) is an unambiguous probe. The
    containment test runs on the exact cubic contours (skia winding), not on a sampled polygon -
    sampling misclassifies the hole of a thin ring (outline circles a few px wide)."""
    paths = [_spline_path(s) if len(s["points"]) >= 2 else pathops.Path() for s in splines]
    areas = [abs(p.area) if len(s["points"]) >= 2 else 0.0 for p, s in zip(paths, splines)]
    bbs = [p.bounds if a > 0 else None for p, a in zip(paths, areas)]
    for i, s in enumerate(splines):
        pts = s["points"]
        probe = _bez(pts[0], pts[1 % len(pts)], 0.5) if len(pts) > 1 else tuple(pts[0]["co"])
        containers = []
        for j, pj in enumerate(paths):
            b = bbs[j]
            if (j == i or b is None or areas[j] <= areas[i] or not (b[0] <= probe[0] <= b[2])
                    or not (b[1] <= probe[1] <= b[3])):
                continue
            if pj.contains(probe):
                containers.append(j)
        s["depth"] = len(containers)
        s["hole"] = len(containers) % 2 == 1
        s["parent"] = min(containers, key=lambda j: areas[j]) if (containers and s["hole"]) else -1


def to_model_splines(splines: List[dict]) -> List[Spline]:
    return [Spline(closed=s["closed"], hole=s.get("hole", False), parent=s.get("parent", -1),
                   depth=s.get("depth", 0),
                   points=[SplinePoint(co=p["co"], hl=p["hl"], hr=p["hr"]) for p in s["points"]])
            for s in splines]


def splines_to_d(splines: Sequence) -> str:
    """Inverse of path_to_splines (validation, three.js SVGLoader). Accepts dicts or models."""
    from .common import fmt
    out = []
    for s in splines:
        s = s if isinstance(s, dict) else s.model_dump()
        pts = s["points"]
        if not pts:
            continue
        out.append(f"M{fmt(pts[0]['co'][0], 6)} {fmt(pts[0]['co'][1], 6)}")
        seq = list(range(1, len(pts))) + ([0] if s["closed"] else [])
        prev = pts[0]
        for k in seq:
            p = pts[k]
            out.append("C" + " ".join(fmt(v, 6) for v in (*prev["hr"], *p["hl"], *p["co"])))
            prev = p
        if s["closed"]:
            out.append("Z")
    return " ".join(out)


# ----------------------------------------------------------------------------------------------
# safe radius
# ----------------------------------------------------------------------------------------------
def safe_radius(geoms: Sequence, area_tol: float = SAFE_RADIUS_AREA_TOL, cap: float = SAFE_RADIUS_CAP) -> float:
    """Largest r such that a morphological opening with radius r removes at most `area_tol` of the
    total area as *thin features* - lost pieces longer than 2.5 r (bars, hairlines, spikes).
    Plain convex-corner rounding (pieces ~r in size) is not a thin feature: a round bevel simply
    rounds such corners. Geometries in art units; binary search on vectorised shapely buffers."""
    arr = np.array([g for g in geoms if g is not None and not g.is_empty and g.area > 0], dtype=object)
    if len(arr) == 0:
        return 0.0
    arr = shapely.make_valid(shapely.simplify(arr, 5e-4))  # 0.025 % of the icon: plenty, much faster
    arr = arr[~shapely.is_empty(arr)]
    if len(arr) == 0:
        return 0.0
    areas = shapely.area(arr)
    total = float(areas.sum())
    hi = float(min(cap, max(math.sqrt(a / math.pi) for a in areas)))

    def loss(r: float) -> float:
        opened = shapely.buffer(shapely.buffer(arr, -r, quad_segs=6), r, quad_segs=6)
        try:
            lost = shapely.difference(arr, opened)
        except shapely.errors.GEOSException:
            try:
                lost = shapely.difference(arr, opened, grid_size=max(r * 1e-3, 1e-9))
            except shapely.errors.GEOSException:  # last resort: plain area difference
                return float(np.clip(areas - shapely.area(opened), 0.0, None).sum()) / total
        parts = shapely.get_parts(lost)
        if len(parts) == 0:
            return 0.0
        b = shapely.bounds(parts)
        extent = np.maximum(b[:, 2] - b[:, 0], b[:, 3] - b[:, 1])
        return float(shapely.area(parts[extent > 2.5 * r]).sum()) / total

    if loss(hi) <= area_tol:
        return round(hi, 5)
    lo = 0.0
    for _ in range(14):
        mid = (lo + hi) / 2
        if loss(mid) <= area_tol:
            lo = mid
        else:
            hi = mid
    return round(lo, 5)


# ----------------------------------------------------------------------------------------------
# per-layer geometry
# ----------------------------------------------------------------------------------------------
def members_of(store: ElementStore, element_ids: Sequence[str]) -> List[Elem]:
    idx = store.index
    return sorted((store.elems[idx[i]] for i in element_ids if i in idx), key=lambda e: idx[e.id])


def _bbox_overlap(a, b) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def occlusion_regions(members: Sequence[Elem]) -> List[Tuple[Elem, pathops.Path]]:
    """Each member minus every opaque member above it (same opacity group): disjoint pieces."""
    out = []
    for k, m in enumerate(members):
        region = m.path
        for up in members[k + 1:]:
            if up.opaque_in_group and up.opacity_group == m.opacity_group and _bbox_overlap(m.bbox, up.bbox):
                region = op(region, up.path, pathops.PathOp.DIFFERENCE)
                if is_empty(region):
                    break
        if not is_empty(region):
            out.append((m, region))  # may be m.path itself: treat as read-only
    return out


def _region_levels(regions: List[Tuple[Elem, pathops.Path]], eps_area: float) -> List[int]:
    levels: List[int] = []
    bbs = [bounds(p) for _, p in regions]
    for k, (_m, p) in enumerate(regions):
        lvl = 0
        for j in range(k):
            if levels[j] + 1 <= lvl or not _bbox_overlap(bbs[k], bbs[j]):
                continue
            inter = op(p, regions[j][1], pathops.PathOp.INTERSECTION)
            if not is_empty(inter) and abs(inter.area) > eps_area:
                lvl = levels[j] + 1
        levels.append(lvl)
    return levels


def snap_to_rim(paths: List[pathops.Path], band: Optional[pathops.Path], snap: float) -> List[pathops.Path]:
    """Grow every path that reaches into the plate's rim `band` by up to `snap` (SVG units), only
    inside the band and only into the empty gap, so an art edge lying just inside the plate outline
    (a frame drawn on the source plate's own outline, which the fitted parametric shape matches to
    ~0.01) ends exactly ON the outline after clipping instead of leaving a hairline of plate visible
    around it. Gap that lies within `snap` of two paths of the list (where they meet at the rim)
    is left to neither: claiming it would leave one of them a thin flag along the other's edge -
    finer than the spline hygiene's resolution (a pinched contour)."""
    if band is None or is_empty(band) or snap <= 0:
        return paths
    near = [k for k, p in enumerate(paths)
            if not is_empty(p) and not is_empty(op(p, band, pathops.PathOp.INTERSECTION))]
    if not near:
        return paths
    art = union_all(paths) if len(paths) > 1 else pathops.Path(paths[0])
    reach = {}                                      # band area within `snap` of each near path
    for k in near:
        grown = pathops.Path(paths[k])
        try:
            grown.stroke(2.0 * snap, pathops.LineCap.ROUND_CAP, pathops.LineJoin.ROUND_JOIN, 4.0)
            grown.convertConicsToQuads(snap * 1e-3)
        except pathops.PathOpsError:
            continue
        r = op(op(grown, band, pathops.PathOp.INTERSECTION), art, pathops.PathOp.DIFFERENCE)
        if not is_empty(r):
            reach[k] = r
    out = list(paths)
    for k, r in reach.items():
        bb = bounds(r)
        rivals = [q for j, q in reach.items() if j != k and _bbox_overlap(bb, bounds(q))]
        grow = op(r, union_all(rivals), pathops.PathOp.DIFFERENCE) if rivals else r
        grow = _drop_hairlines(grow, SNAP_MIN_WIDTH * snap)
        if not is_empty(grow):
            out[k] = op(paths[k], grow, pathops.PathOp.UNION)
    return out


SNAP_MIN_WIDTH = 0.05   # growth pieces thinner than this share of the snap distance are dropped


def _drop_hairlines(p: pathops.Path, min_width: float) -> pathops.Path:
    """`p` without the contours whose mean width (2 area / perimeter) is below `min_width`: where
    art lies (numerically) ON the plate outline the growth is a zero-width sliver that would pinch
    the region's contour."""
    if is_empty(p):
        return p
    out = pathops.Path()
    for c in split_contours(p):
        g = shapely_from_path(c, min_width)
        if not g.is_empty and g.length > 0 and 2.0 * g.area / g.length >= min_width:
            out.addPath(c)
    if is_empty(out):
        return out
    try:
        out.simplify(fix_winding=True)
    except pathops.PathOpsError:
        pass
    return out


def clip_to_plate(paths: List[pathops.Path], clip: Optional[pathops.Path],
                  band: Optional[pathops.Path] = None, snap: float = 0.0) -> List[pathops.Path]:
    """Intersect every path with the plate outline: art reaching over the plate edge would hang off
    the plate - and past its bevel - once extruded. With a rim `band`, art flush with the edge is
    first snapped onto the outline (:func:`snap_to_rim`). Paths entirely outside become empty.
    No clip (no plate) or clipping that would empty EVERY path (art not on the plate at all, e.g.
    after a user rescale): the paths are returned unchanged."""
    if clip is None or is_empty(clip):
        return paths
    orig = paths
    paths = snap_to_rim(paths, band, snap)
    out = []
    for p in paths:
        cb = bounds(clip)
        pb = bounds(p)
        inside = pb[0] >= cb[0] and pb[1] >= cb[1] and pb[2] <= cb[2] and pb[3] <= cb[3]
        if is_empty(p):
            out.append(p)
            continue
        q = op(p, clip, pathops.PathOp.INTERSECTION)
        if inside and abs(abs(q.area) - abs(p.area)) <= 1e-9 * max(1.0, abs(p.area)):
            q = p   # untouched: keep the original contour structure
        out.append(q)
    if all(is_empty(q) for q in out):
        return orig
    return out


def plate_clip(store: ElementStore) -> Tuple[Optional[pathops.Path], Optional[pathops.Path], float]:
    """(clip outline, snap band, snap distance in SVG units) for :func:`clip_to_plate`."""
    return plate_clip_path(store), plate_snap_band(store), PLATE_SNAP / (store.art.k or 1.0)


def layer_regions(store: ElementStore, members: Sequence[Elem]) -> List[Tuple[Elem, pathops.Path]]:
    """Occlusion-cut regions clipped to the plate (empty pieces dropped)."""
    regions = occlusion_regions(members)
    clipped = clip_to_plate([p for _m, p in regions], *plate_clip(store))
    return [(m, p) for (m, _p), p in zip(regions, clipped) if not is_empty(p)]


FUSE_GAP = 1e-4   # art units: pieces closer than 2x this are fused in the silhouette (rounding gaps)


def silhouette_path(store: ElementStore, members: Sequence[Elem]) -> pathops.Path:
    """Union of the members, with the hairline gaps between pieces that only numerically butt
    against each other (Illustrator's 3-decimal coordinates leave 0.001-unit slits between Drive's
    sections) bridged: a 'combined' body built from this silhouette must be ONE body, not two
    bodies with a seam between them."""
    u = union_all([m.path for m in members])
    if len(members) < 2:
        return u
    r = FUSE_GAP / (store.art.k or 1.0)
    idx = store.index
    pos = [idx[m.id] for m in members]
    bridges = []
    dil = {}

    def dilated(m):
        if m.id not in dil:
            d = pathops.Path(m.path)
            try:
                d.stroke(2.0 * r, pathops.LineCap.ROUND_CAP, pathops.LineJoin.ROUND_JOIN, 4.0)
                d.convertConicsToQuads(r * 1e-2)
                dil[m.id] = op(d, m.path, pathops.PathOp.UNION)
            except pathops.PathOpsError:
                dil[m.id] = None
        return dil[m.id]

    for a in range(len(members)):
        for b in range(a + 1, len(members)):
            g = store.gaps[pos[a], pos[b]] if store.gaps.shape[0] > max(pos[a], pos[b]) else np.inf
            if 0.0 < g <= 2.0 * r:
                da, db = dilated(members[a]), dilated(members[b])
                if da is not None and db is not None:
                    bridges.append(op(da, db, pathops.PathOp.INTERSECTION))
    if not bridges:
        return u
    return union_all([u] + bridges)


def layer_safe_radius(store: ElementStore, element_ids: Sequence[str], mode: str = "individual") -> float:
    members = members_of(store, element_ids)
    if not members:
        return 0.0
    art = store.art
    tol = store.tolerance
    if mode == "combined":
        paths = clip_to_plate([silhouette_path(store, members)], *plate_clip(store))
    else:
        paths = [p for _m, p in layer_regions(store, members)]
    return safe_radius([_art_scale_geom(shapely_from_path(p, tol), art.k) for p in paths])


def _art_scale_geom(g, k: float):
    """Scale an SVG-space geometry to art units (only lengths/areas matter for the safe radius)."""
    return affinity.scale(g, k, k, origin=(0, 0))


def layer_hash(store: ElementStore, layer: Layer, texture_size: int) -> str:
    """Content hash of one layer's geometry: member element contents (not the whole store, so an
    edit elsewhere never invalidates this layer's texture), mode and pipeline tunables."""
    members = members_of(store, layer.elementIds)
    grads = sorted({m.paint.get("id") for m in members if m.paint.get("id")})
    plate = store.plate or {}
    clip = [plate.get(k) for k in ("bbox", "shape", "cornerRadius")] + [PLATE_CLIP_INSET, PLATE_SNAP] if plate else None
    return sha1(PIPELINE_VERSION, list(store.view_box), [m.content_hash for m in members],
                [store.gradients.get(g, "") for g in grads], layer.mode, SMOOTH_POLYLINES,
                SMOOTH_MIN_SEGMENTS, SMOOTH_STRAIGHT_FRAC, SMOOTH_CORNER_DEG, SMOOTH_MAX_BULGE, SAFE_RADIUS_AREA_TOL,
                SMOOTH_TRACED_MIN_SEGMENTS, SMOOTH_TRACED_CORNER_DEG,
                hygiene.MICRO, hygiene.MIN_AREA, clip, FUSE_GAP, SNAP_MIN_WIDTH, texture_size)[:20]


def _build_layer_entry(store: ElementStore, layer: Layer, h: str, cache: Path, project_dir: Path,
                       texture_size: int) -> dict:
    """Compute + write one layer's geometry, SVG and texture. Returns the cache entry dict."""
    art = store.art
    M = art.matrix
    members = members_of(store, layer.elementIds)
    clip = plate_clip(store)
    regions = layer_regions(store, members)
    eps_area = art.area * 1e-6
    levels = _region_levels(regions, eps_area)
    region_out = []
    for (m, p), lvl in zip(regions, levels):
        spl = path_to_splines(p, M, traced=bool(m.image))
        if not spl:
            continue   # nothing but slivers / debris left
        annotate_holes(spl)
        region_out.append({"elementId": m.id, "paint": model_paint(m.paint, art).model_dump(),
                           "opacity": round(m.total_opacity, 6), "zSub": round(lvl * REGION_Z_STEP, 6),
                           "splines": spl})
    sil = clip_to_plate([silhouette_path(store, members)], *clip)[0]
    sil.convertConicsToQuads(0.001)
    sil_spl = path_to_splines(sil, M, traced=bool(members) and all(m.image for m in members))
    annotate_holes(sil_spl)
    tol = store.tolerance
    if layer.mode == "combined":
        sr_paths = [sil]
    else:
        sr_paths = [p for _m, p in regions]
    sr = safe_radius([_art_scale_geom(shapely_from_path(p, tol), art.k) for p in sr_paths])
    bb = art.bbox(bounds(sil)) if not is_empty(sil) else (0.0, 0.0, 0.0, 0.0)

    # standalone layer SVG (source viewBox) + texture (art square)
    svg_name = f"layer-{h}.svg"
    tex_name = f"tex-{h}.png"
    svg_text = textures.layer_svg(members, store.gradients, store.view_box, project_dir)
    atomic_write_text(cache / svg_name, svg_text)
    sq = art.square_view_box()
    tex_svg = textures.layer_svg(members, store.gradients, sq, project_dir, size=(texture_size, texture_size))
    tex = textures.render_texture(tex_svg, texture_size)
    atomic_write_bytes(cache / tex_name, raster.encode_png(tex, 3))

    images = []
    for m in members:
        if m.image and m.image.get("file"):
            mat = Affine2D(*m.image["matrix"])
            to_art = Affine2D.compose_ltr((mat, M))
            images.append({"elementId": m.id, "file": m.image["file"], "bbox": [round(v, 6) for v in art.bbox(m.bbox)],
                           "opacity": round(m.total_opacity, 6), "width": m.image["width"],
                           "height": m.image["height"],
                           "matrix": [round(v, 9) for v in (to_art.a, to_art.b, to_art.c, to_art.d, to_art.e, to_art.f)],
                           "opaque": bool(m.image.get("opaque"))})
    entry = {"hash": h, "silhouette": sil_spl, "regions": region_out, "safeRadius": sr,
             "bbox": [round(v, 6) for v in bb], "svg": svg_name, "texture": tex_name, "images": images}
    atomic_write_text(cache / f"lg-{h}.json", json.dumps(entry, separators=(",", ":")))
    return entry


def _layer_geometry(entry: dict, layer_id: str, cache: Path, project_dir: Path, url_prefix: str) -> LayerGeometry:
    prefix = url_prefix.rstrip("/")
    images = []
    for im in entry["images"]:
        images.append({**im, "path": str((Path(project_dir) / im["file"]).resolve()),
                       "url": f"{prefix}/{im['file']}"})
    return LayerGeometry(
        layerId=layer_id, hash=entry["hash"],
        silhouette=to_model_splines(entry["silhouette"]),
        regions=[Region(elementId=r["elementId"], paint=r["paint"], opacity=r["opacity"], zSub=r["zSub"],
                        splines=to_model_splines(r["splines"])) for r in entry["regions"]],
        safeRadius=entry["safeRadius"], bbox=tuple(entry["bbox"]),
        texture=f"{prefix}/{CACHE_DIR}/{entry['texture']}",
        texturePath=str((cache / entry["texture"]).resolve()),
        svg=f"{prefix}/{CACHE_DIR}/{entry['svg']}",
        images=images,
    )


# ----------------------------------------------------------------------------------------------
# bundle (cached)
# ----------------------------------------------------------------------------------------------
_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_BUNDLES: Dict[Tuple[str, str], GeometryBundle] = {}
_BUNDLE_CAP = 32


def _project_lock(project_dir: Path) -> threading.Lock:
    key = str(Path(project_dir).resolve())
    with _LOCKS_GUARD:
        if key not in _LOCKS:
            _LOCKS[key] = threading.Lock()
        return _LOCKS[key]


def plate_element_ids(store: ElementStore, project: Project) -> List[str]:
    in_layers = {eid for L in project.layers for eid in L.elementIds}
    return [i for i in store.plate_ids if i not in in_layers and i in store.index]


def bundle_hash(store: ElementStore, project: Project, url_prefix: str, texture_size: int) -> Tuple[str, Dict[str, str]]:
    lh = {L.id: layer_hash(store, L, texture_size) for L in project.layers}
    h = sha1(PIPELINE_VERSION, project.id, url_prefix, store.hash, [(L.id, lh[L.id]) for L in project.layers],
             plate_element_ids(store, project))[:20]
    return h, lh


def geometry_file(project_dir: Path, h: str) -> Path:
    return Path(project_dir) / CACHE_DIR / f"geometry-{h}.json"


def texture_size_for(n_layers: int, requested: int = TEXTURE_SIZE) -> int:
    """Per-layer texture size under a total budget: every layer texture covers the whole art
    square and is loaded by Blender and three.js, so an 'element' split with dozens of layers
    must not mean dozens of 2048 px RGBA textures (~21 MB of VRAM each incl. mipmaps)."""
    for max_layers, px in TEXTURE_BUDGET:
        if n_layers <= max_layers:
            return min(requested, px)
    return min(requested, 512)


def build_bundle(project_dir: Path, project: Project, url_prefix: str,
                 texture_size: int = TEXTURE_SIZE) -> GeometryBundle:
    project_dir = Path(project_dir)
    store = ElementStore.load(project_dir)
    texture_size = texture_size_for(len(project.layers), texture_size)
    h, lh = bundle_hash(store, project, url_prefix, texture_size)
    key = (str(project_dir.resolve()), h)
    gfile = geometry_file(project_dir, h)
    hit = _BUNDLES.get(key)
    if hit is not None and _keep_alive(gfile, hit):
        return hit.model_copy()
    if gfile.exists():
        try:
            bundle = GeometryBundle.model_validate_json(gfile.read_bytes())
            _remember(key, bundle)
            _keep_alive(gfile, bundle)
            return bundle.model_copy()
        except Exception:  # noqa: BLE001 - corrupt cache file: rebuild
            pass
    with _project_lock(project_dir):
        cache = project_dir / CACHE_DIR
        cache.mkdir(parents=True, exist_ok=True)
        entries: Dict[str, dict] = {}
        todo = []
        for L in project.layers:
            f = cache / f"lg-{lh[L.id]}.json"
            if f.exists() and (cache / f"tex-{lh[L.id]}.png").exists():
                try:
                    entries[L.id] = json.loads(f.read_text(encoding="utf-8"))
                    continue
                except Exception:  # noqa: BLE001
                    pass
            todo.append(L)
        if todo:
            workers = min(len(todo), max(1, (os.cpu_count() or 4) // 2), 6)
            if workers > 1:
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futs = {L.id: ex.submit(_build_layer_entry, store, L, lh[L.id], cache, project_dir, texture_size)
                            for L in todo}
                    for lid, fut in futs.items():
                        entries[lid] = fut.result()
            else:
                for L in todo:
                    entries[L.id] = _build_layer_entry(store, L, lh[L.id], cache, project_dir, texture_size)
        layers = {L.id: _layer_geometry(entries[L.id], L.id, cache, project_dir, url_prefix) for L in project.layers}
        plate = _plate_dict(store, project, cache, project_dir, url_prefix)
        bundle = GeometryBundle(projectId=project.id, hash=h, viewBox=tuple(store.view_box), plate=plate,
                                layers=layers)
        atomic_write_text(gfile, bundle.model_dump_json())
        _remember(key, bundle)
        _prune(cache, bundle, plate)
    return bundle.model_copy()


def _keep_alive(gfile: Path, bundle: GeometryBundle) -> bool:
    """True if the bundle file exists. Bundles in use get their files' mtimes refreshed (at most
    every 10 min) so cache pruning never deletes textures a recent render may still read."""
    try:
        age = time.time() - gfile.stat().st_mtime
    except OSError:
        return False
    if age > 600:
        cache = gfile.parent
        names = [gfile.name] + [f"{p}-{lg.hash}.{ext}" for lg in bundle.layers.values()
                                for p, ext in (("lg", "json"), ("tex", "png"), ("layer", "svg"))]
        for n in names:
            try:
                os.utime(cache / n)
            except OSError:
                pass
    return True


def _remember(key, bundle: GeometryBundle) -> None:
    with _LOCKS_GUARD:  # request threads share this LRU
        _BUNDLES.pop(key, None)
        _BUNDLES[key] = bundle
        while len(_BUNDLES) > _BUNDLE_CAP:
            _BUNDLES.pop(next(iter(_BUNDLES)))


def _plate_dict(store: ElementStore, project: Project, cache: Path, project_dir: Path,
                url_prefix: str) -> Optional[dict]:
    if not store.plate:
        return None
    ids = plate_element_ids(store, project)
    if not ids:
        return None
    members = members_of(store, ids)
    name = f"plate-{sha1(store.hash, ids)[:16]}.svg"
    if not (cache / name).exists():
        atomic_write_text(cache / name, textures.layer_svg(members, store.gradients, store.view_box, project_dir))
    p = store.plate
    return {"bbox": p["bbox"], "shape": p["shape"], "cornerRadius": p.get("cornerRadius"),
            "iou": p.get("iou"), "elementIds": ids, "svg": f"{url_prefix.rstrip('/')}/{CACHE_DIR}/{name}",
            "svgPath": str((cache / name).resolve())}


def _prune(cache: Path, bundle: GeometryBundle, plate: Optional[dict]) -> None:
    keep = {f"geometry-{bundle.hash}.json"}
    for lg in bundle.layers.values():
        keep |= {f"lg-{lg.hash}.json", f"tex-{lg.hash}.png", f"layer-{lg.hash}.svg"}
    if plate:
        keep.add(Path(plate["svgPath"]).name)
    now = time.time()
    try:
        for f in cache.iterdir():
            if f.name in keep or not f.name.split("-")[0] in ("geometry", "lg", "tex", "layer", "plate"):
                continue
            try:
                if now - f.stat().st_mtime > CACHE_KEEP_SECONDS:
                    f.unlink()
            except OSError:
                pass
    except OSError:
        pass
