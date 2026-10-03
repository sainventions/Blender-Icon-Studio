"""Layer / plate geometry (PLAN D3).

Primary route: a 2D bezier curve per region (or per layer silhouette) with
``extrude = max(thickness/2 − bevel, 0)``, ``bevel_mode='ROUND'``, ``bevel_depth = bevel``,
``offset = −bevel`` (silhouette preserved), smooth shading. The bevel is clamped to
``0.9 × safeRadius`` (thin features invert otherwise) and to half the thickness.

Fallback route (``'gn'``) when the clamp cuts the bevel below 30 % of the request, or the bevel is 0
(touching contours break the legacy scanfill): the curve object keeps ``fill_mode='NONE'`` and gets
Geometry Nodes *Fill Curve (N-gons)* + *Set Material* + *Set Shade Smooth* → Solidify → Bevel
(``use_clamp_overlap``). Weighted Normal is rejected on curve objects (critic verification), so it is
not used.

Curve datablocks are cached by (layer hash, piece, depth params, route) so re-renders after material /
lighting edits never rebuild geometry.

Baked meshes (:func:`solid_mesh`): the scene renders MESH objects whose data is the curve (or the GN
route's Fill Curve → Solidify → Bevel stack) evaluated ONCE and cached. A render with persistent data off
evaluates every object from scratch, so live curve bevels / modifier stacks were re-evaluated on every
render (a traced raster contour through the GN route: ~6 s per render for two pieces).
Splines get an adaptive ``resolution_u`` (dense traced contours need no 12× subdivision per segment).
"""
from __future__ import annotations

import math
from collections import OrderedDict
from typing import Iterable, Optional

import bpy

from .util import stable_hash

FALLBACK_RATIO = 0.30
MIN_BEVEL = 1e-4
_CURVE_CACHE: "OrderedDict[str, tuple[str, str]]" = OrderedDict()   # key -> (curve name, route)
CACHE_LIMIT = 160


# ------------------------------------------------------------------------------------------------
# bevel policy
# ------------------------------------------------------------------------------------------------
def effective_bevel(requested: float, thickness: float, safe_radius: float) -> tuple[float, str]:
    """-> (bevel to use on the curve route, route 'curve'|'gn')."""
    requested = max(0.0, float(requested))
    thickness = max(0.0, float(thickness))
    cap = min(0.9 * max(0.0, float(safe_radius)), thickness / 2.0)
    bevel = min(requested, cap)
    if thickness <= 1e-6:
        return 0.0, "gn"
    if requested <= MIN_BEVEL:
        return 0.0, "gn"
    if bevel < FALLBACK_RATIO * requested:
        return bevel, "gn"
    return bevel, "curve"


# ------------------------------------------------------------------------------------------------
# spline hygiene
# ------------------------------------------------------------------------------------------------
def _d2(a, b) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def _flat_area(pts: list, closed: bool, n: int = 6) -> float:
    ring = []
    m = len(pts)
    for i in range(m if closed else m - 1):
        a, b = pts[i], pts[(i + 1) % m]
        p0, c1, c2, p1 = a["co"], a["hr"], b["hl"], b["co"]
        for j in range(n):
            t = j / n
            mt = 1 - t
            ring.append((mt ** 3 * p0[0] + 3 * mt * mt * t * c1[0] + 3 * mt * t * t * c2[0] + t ** 3 * p1[0],
                         mt ** 3 * p0[1] + 3 * mt * mt * t * c1[1] + 3 * mt * t * t * c2[1] + t ** 3 * p1[1]))
    return 0.5 * sum(ring[i - 1][0] * ring[i][1] - ring[i][0] * ring[i - 1][1] for i in range(len(ring)))


def sanitize(splines: list, eps: float = 2.5e-4, min_area: float = 2e-7) -> list:
    """Merge coincident consecutive points (zero-length segments make the curve bevel shoot spikes
    along an undefined normal) and drop degenerate slivers."""
    out = []
    e2 = eps * eps
    for s in splines:
        pts = [{"co": list(p["co"]), "hl": list(p.get("hl") or p["co"]), "hr": list(p.get("hr") or p["co"])}
               for p in s.get("points") or []]
        closed = bool(s.get("closed", True))
        merged: list = []
        for p in pts:
            if merged and _d2(p["co"], merged[-1]["co"]) < e2:
                merged[-1]["hr"] = p["hr"]
            else:
                merged.append(p)
        if closed and len(merged) > 1 and _d2(merged[0]["co"], merged[-1]["co"]) < e2:
            last = merged.pop()
            merged[0]["hl"] = last["hl"]
        if len(merged) < 2 or (closed and abs(_flat_area(merged, closed)) < min_area):
            continue
        out.append({**s, "points": merged})
    return out


def max_turn_deg(splines: list) -> float:
    """Largest tangent turn at any spline point (180 = cusp). Uses the true end tangents."""
    best = 0.0
    for s in splines:
        pts = s["points"]
        m = len(pts)
        for i, p in enumerate(pts):
            co = p["co"]
            ti = (co[0] - p["hl"][0], co[1] - p["hl"][1])
            if ti[0] * ti[0] + ti[1] * ti[1] < 1e-14:
                q = pts[i - 1]
                ti = (co[0] - q["hr"][0], co[1] - q["hr"][1])
                if ti[0] * ti[0] + ti[1] * ti[1] < 1e-14:
                    ti = (co[0] - q["co"][0], co[1] - q["co"][1])
            to = (p["hr"][0] - co[0], p["hr"][1] - co[1])
            if to[0] * to[0] + to[1] * to[1] < 1e-14:
                q = pts[(i + 1) % m]
                to = (q["hl"][0] - co[0], q["hl"][1] - co[1])
                if to[0] * to[0] + to[1] * to[1] < 1e-14:
                    to = (q["co"][0] - co[0], q["co"][1] - co[1])
            a = abs(math.degrees(math.atan2(ti[0] * to[1] - ti[1] * to[0], ti[0] * to[0] + ti[1] * to[1])))
            if not s.get("closed", True) and i in (0, m - 1):
                continue
            best = max(best, a)
    return best


# ------------------------------------------------------------------------------------------------
# corner fillets: the round curve bevel pinches at sharp corners (miter-scaled profile -> dark specks in
# glass). Rounding corners with a radius ~ the bevel keeps the sweep smooth (and reads as Apple-soft).
# ------------------------------------------------------------------------------------------------
FILLET_MIN_TURN = 22.0     # degrees; smoother joins are left alone


def _bez(p0, c1, c2, p1, t):
    mt = 1 - t
    return (mt ** 3 * p0[0] + 3 * mt * mt * t * c1[0] + 3 * mt * t * t * c2[0] + t ** 3 * p1[0],
            mt ** 3 * p0[1] + 3 * mt * mt * t * c1[1] + 3 * mt * t * t * c2[1] + t ** 3 * p1[1])


def _sub(seg, t0, t1):
    """Control points of the cubic ``seg`` restricted to [t0, t1] (two de Casteljau splits)."""
    def split(p0, c1, c2, p1, t):        # -> right half [t, 1]
        lerp = lambda a, b: (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)  # noqa: E731
        a, b, c = lerp(p0, c1), lerp(c1, c2), lerp(c2, p1)
        d, e = lerp(a, b), lerp(b, c)
        f = lerp(d, e)
        return (f, e, c, p1), (p0, a, d, f)
    p0, c1, c2, p1 = seg
    if t0 > 1e-9:
        (p0, c1, c2, p1), _ = split(p0, c1, c2, p1, t0)
        t1 = (t1 - t0) / (1 - t0) if t0 < 1 else 1.0
    if t1 < 1 - 1e-9:
        _, (p0, c1, c2, p1) = split(p0, c1, c2, p1, t1)
    return p0, c1, c2, p1


def _arclen_table(seg, n: int = 16):
    pts = [_bez(*seg, i / n) for i in range(n + 1)]
    acc = [0.0]
    for i in range(n):
        acc.append(acc[-1] + math.dist(pts[i], pts[i + 1]))
    return acc


def _t_at(acc, length, from_end=False):
    total = acc[-1]
    if total <= 1e-12:
        return 1.0 if from_end else 0.0
    target = total - length if from_end else length
    target = max(0.0, min(total, target))
    n = len(acc) - 1
    for i in range(n):
        if acc[i + 1] >= target:
            seg = acc[i + 1] - acc[i]
            f = 0.0 if seg <= 1e-15 else (target - acc[i]) / seg
            return (i + f) / n
    return 1.0


def _unit(v):
    n = math.hypot(v[0], v[1])
    return (v[0] / n, v[1] / n) if n > 1e-12 else (0.0, 0.0)


def fillet_corners(splines: list, radius: float, min_turn: float = FILLET_MIN_TURN, min_radius: float = 0.0) -> list:
    """Round every corner sharper than ``min_turn`` with an (approximately circular) fillet of
    ``radius``. A fillet that does not fit in 45 % of the adjacent segments is shrunk, or — when
    ``min_radius`` > 0 — skipped (the corner stays sharp). Closed splines only."""
    if radius <= 1e-6:
        return splines
    out = []
    for s in splines:
        pts = s["points"]
        m = len(pts)
        if not s.get("closed", True) or m < 2:
            out.append(s)
            continue
        segs = [(tuple(pts[i]["co"]), tuple(pts[i]["hr"]), tuple(pts[(i + 1) % m]["hl"]), tuple(pts[(i + 1) % m]["co"]))
                for i in range(m)]
        acc = [_arclen_table(sg) for sg in segs]
        # corner i sits between segs[i-1] (incoming) and segs[i] (outgoing)
        cut = [0.0] * m
        turn = [0.0] * m
        for i in range(m):
            sin_, sout = segs[i - 1], segs[i]
            tin = _unit((sin_[3][0] - sin_[2][0], sin_[3][1] - sin_[2][1]))
            if tin == (0.0, 0.0):
                tin = _unit((sin_[3][0] - sin_[1][0], sin_[3][1] - sin_[1][1]))
            if tin == (0.0, 0.0):
                tin = _unit((sin_[3][0] - sin_[0][0], sin_[3][1] - sin_[0][1]))
            tout = _unit((sout[1][0] - sout[0][0], sout[1][1] - sout[0][1]))
            if tout == (0.0, 0.0):
                tout = _unit((sout[2][0] - sout[0][0], sout[2][1] - sout[0][1]))
            if tout == (0.0, 0.0):
                tout = _unit((sout[3][0] - sout[0][0], sout[3][1] - sout[0][1]))
            ang = math.degrees(math.acos(max(-1.0, min(1.0, tin[0] * tout[0] + tin[1] * tout[1]))))
            if min_turn < ang < 175.0:
                turn[i] = ang
                cut[i] = radius * math.tan(math.radians(ang) / 2)
        if not any(cut):
            out.append(s)
            continue
        for i in range(m):        # never let two fillets overlap on one segment
            if cut[i] == 0:
                continue
            lim = 0.45 * min(acc[i - 1][-1], acc[i][-1])
            if cut[i] > lim:
                # a fillet tighter than the requested radius would make the bevel's inset outline loop
                # (offset of an arc with radius < offset distance) -> leave this corner sharp
                cut[i] = 0.0 if min_radius > 0 else lim
        new_pts = []
        for i in range(m):
            sg = segs[i]
            t0 = _t_at(acc[i], cut[i]) if cut[i] else 0.0
            j = (i + 1) % m
            t1 = _t_at(acc[i], cut[j], from_end=True) if cut[j] else 1.0
            if t1 <= t0 + 1e-6:
                t0 = t1 = (t0 + t1) / 2
            p0, c1, c2, p1 = _sub(sg, t0, t1)
            new_pts.append({"co": list(p0), "hl": None, "hr": list(c1)})
            new_pts.append({"co": list(p1), "hl": list(c2), "hr": None, "_corner_out": j})
        # stitch: end of segment i (corner j) -> start of segment j: fillet arc or plain join
        res = new_pts
        n2 = len(new_pts)
        stitched = []
        for k in range(0, n2, 2):
            start, end = res[k], res[k + 1]
            nxt = res[(k + 2) % n2]
            j = end["_corner_out"]
            stitched.append(start)
            if cut[j] > 0:
                corner = segs[j][0]
                phi = math.radians(turn[j])
                r_eff = cut[j] / max(1e-9, math.tan(phi / 2))
                h = 4.0 / 3.0 * math.tan(phi / 4) * r_eff
                d_end = _unit((corner[0] - end["co"][0], corner[1] - end["co"][1]))
                d_nxt = _unit((corner[0] - nxt["co"][0], corner[1] - nxt["co"][1]))
                end["hr"] = [end["co"][0] + d_end[0] * h, end["co"][1] + d_end[1] * h]
                nxt["hl"] = [nxt["co"][0] + d_nxt[0] * h, nxt["co"][1] + d_nxt[1] * h]
                stitched.append(end)
            else:
                # no corner: segment end and the next start coincide -> merge into one point
                nxt["hl"] = end["hl"]
        final = []
        for p in stitched:
            final.append({"co": p["co"], "hl": p["hl"] if p["hl"] is not None else p["co"],
                          "hr": p["hr"] if p["hr"] is not None else p["co"]})
        out.append({**s, "points": final})
    return out


# ------------------------------------------------------------------------------------------------
# curve datablocks
# ------------------------------------------------------------------------------------------------
RES_MAX = 12          # bezier subdivisions per segment (smooth few-point outlines: petals, circles)
RES_TOL = 0.0008      # max chord error (local units ≈ 0.2 px at 512 px for a ±1 icon)
RES_BUDGET = 1024     # max evaluated points per spline (segments × resolution) for pathological dense contours


def spline_resolution(points: list, closed: bool = True) -> int:
    """Subdivisions per segment from Wang's formula (cubic: n = sqrt(0.75·L / tol), L = max second
    difference of the control points) for the most curved segment (Blender's resolution is per spline).
    Few-point smooth outlines keep 12; dense traced contours (raster alpha masks: 100-400 nearly straight
    segments) get 1-3 instead of 12 — 4-10× fewer vertices in every downstream step (bevel, fill, BVH,
    EEVEE). Not a percentile: a spline with one big arc among many straight segments (Ti84, Play Store,
    Home) would collapse that arc into 1-2 chords (up to 7 px at 512 px). Corpus cost of the max over
    the 90th percentile: +15 % vertices."""
    m = len(points)
    segs = m if closed else m - 1
    if segs <= 0:
        return RES_MAX
    worst = 0.0
    for i in range(segs):
        a, b = points[i], points[(i + 1) % m]
        p0, c1, c2, p1 = a["co"], a.get("hr") or a["co"], b.get("hl") or b["co"], b["co"]
        worst = max(worst, math.hypot(p0[0] - 2 * c1[0] + c2[0], p0[1] - 2 * c1[1] + c2[1]),
                    math.hypot(c1[0] - 2 * c2[0] + p1[0], c1[1] - 2 * c2[1] + p1[1]))
    n = int(math.ceil(math.sqrt(0.75 * worst / RES_TOL)))
    return max(1, min(RES_MAX, max(1, RES_BUDGET // segs), n))


def _fill_splines(cu: bpy.types.Curve, splines: Iterable[dict]) -> int:
    n = 0
    for s in splines:
        pts = s.get("points") or []
        if len(pts) < 2:
            continue
        sp = cu.splines.new("BEZIER")
        sp.bezier_points.add(len(pts) - 1)
        for bp, p in zip(sp.bezier_points, pts):
            bp.handle_left_type = "FREE"
            bp.handle_right_type = "FREE"
            co, hl, hr = p["co"], p.get("hl", p["co"]), p.get("hr", p["co"])
            bp.co = (co[0], co[1], 0.0)
            bp.handle_left = (hl[0], hl[1], 0.0)
            bp.handle_right = (hr[0], hr[1], 0.0)
        sp.use_cyclic_u = bool(s.get("closed", True))
        sp.use_smooth = True
        sp.resolution_u = spline_resolution(pts, bool(s.get("closed", True)))
        n += 1
    return n


CUSP_DEG = 150.0     # sharper tips than this make the round curve bevel overshoot -> GN route
FILLET_RATIO = 1.2   # corner fillet radius relative to the bevel (≥ 1: the inset outline must not loop)


def curve_data(key_parts: dict, splines: list, thickness: float, bevel: float, route: str,
               segments: int = 6) -> tuple[bpy.types.Curve, str]:
    """Cached 2D curve datablock in LOCAL units (z centred on 0, spans ±thickness/2).

    Returns ``(curve, route)``; the route may switch from 'curve' to 'gn' when the (sanitised) splines
    contain cusps. Splines are only processed on a cache miss."""
    key = stable_hash({**key_parts, "t": round(thickness, 6), "b": round(bevel, 6), "r": route, "s": segments})
    hit = _CURVE_CACHE.get(key)
    cu = bpy.data.curves.get(hit[0]) if hit else None
    if cu is not None:
        _CURVE_CACHE.move_to_end(key)
        return cu, hit[1]
    clean = sanitize(splines)
    final_route = route
    if route == "curve" and bevel > MIN_BEVEL and max_turn_deg(clean) > CUSP_DEG:
        final_route = "gn"
    if final_route == "curve" and bevel > MIN_BEVEL:
        clean = sanitize(fillet_corners(clean, FILLET_RATIO * bevel, min_radius=bevel))
    cu = bpy.data.curves.new(f"BIS~{key[:10]}", "CURVE")
    cu.dimensions = "2D"
    cu.resolution_u = 12
    cu.twist_mode = "MINIMUM"
    _fill_splines(cu, clean)
    if final_route == "curve":
        cu.fill_mode = "BOTH"
        cu.extrude = max(thickness / 2.0 - bevel, 0.0)
        cu.bevel_mode = "ROUND"
        cu.bevel_depth = bevel
        cu.bevel_resolution = max(1, int(segments))
        cu.offset = -bevel
        cu.use_fill_caps = False
    else:
        cu.fill_mode = "NONE"            # Fill Curve (GN) replaces the legacy scanfill
        cu.extrude = 0.0
        cu.bevel_depth = 0.0
        cu.offset = 0.0
    cu.materials.append(None)
    if final_route == "curve" and bevel > MIN_BEVEL and not _caps_ok(cu, clean, bevel, thickness):
        # the inset (offset = −bevel) outline self-intersected and scanfill dropped the caps
        # (blocky traced contours, small notches): rebuild through Fill Curve + Solidify + Bevel
        final_route = "gn"
        cu.fill_mode = "NONE"
        cu.extrude = 0.0
        cu.bevel_depth = 0.0
        cu.offset = 0.0
    cu["bis_key"] = key
    cu["bis_route"] = final_route
    _CURVE_CACHE[key] = (cu.name, final_route)
    _evict()
    return cu, final_route


def _ring_metrics(splines: list) -> tuple[float, float]:
    """(even-odd filled area, total perimeter) of flattened splines."""
    area = 0.0
    perim = 0.0
    for s in splines:
        ring = _flatten_ring(s["points"], bool(s.get("closed", True)), 6)
        if len(ring) < 3:
            continue
        a = abs(0.5 * sum(ring[i - 1][0] * ring[i][1] - ring[i][0] * ring[i - 1][1] for i in range(len(ring))))
        perim += sum(math.dist(ring[i - 1], ring[i]) for i in range(len(ring)))
        area += -a if s.get("hole") else a
    return abs(area), perim


def _caps_ok(cu: bpy.types.Curve, splines: list, bevel: float, thickness: float) -> bool:
    """Evaluate the bevelled curve once and check that its front cap exists (≥ 45 % of the expected
    inset area). Runs only on a curve-cache miss."""
    area, perim = _ring_metrics(splines)
    expected = area - perim * bevel
    if expected <= 0.08 * area or area < 1e-6:
        return True            # too thin to judge; the clamp already protects thin features
    # an UNLINKED original object: new_from_object evaluates just this curve (extrude/bevel/offset are curve
    # data, not modifiers). Linking it into the scene + depsgraph update re-evaluated the scene's relations
    # on every cache miss (2.5x slower on 40-piece icons, identical cap areas).
    ob = bpy.data.objects.new("BIS~capcheck", cu)
    try:
        me = bpy.data.meshes.new_from_object(ob)
        try:
            import numpy as np
            n = len(me.polygons)
            nrm = np.empty(n * 3, dtype=np.float32)
            ctr = np.empty(n * 3, dtype=np.float32)
            ar = np.empty(n, dtype=np.float32)
            me.polygons.foreach_get("normal", nrm)
            me.polygons.foreach_get("center", ctr)
            me.polygons.foreach_get("area", ar)
            front = (nrm[2::3] > 0.999) & (ctr[2::3] >= thickness / 2.0 - 1e-5)
            cap = float(ar[front].sum())
        finally:
            bpy.data.meshes.remove(me)
    finally:
        bpy.data.objects.remove(ob, do_unlink=True)
    return cap >= 0.45 * expected


def _evict() -> None:
    while len(_CURVE_CACHE) > CACHE_LIMIT:
        key, (name, _route) = _CURVE_CACHE.popitem(last=False)
        cu = bpy.data.curves.get(name)
        if cu is not None and cu.users == 0:
            bpy.data.curves.remove(cu)
    while len(_MESH_CACHE) > CACHE_LIMIT:
        key, (name, _route) = _MESH_CACHE.popitem(last=False)
        me = bpy.data.meshes.get(name)
        if me is not None and me.users == 0:
            bpy.data.meshes.remove(me)


# ------------------------------------------------------------------------------------------------
# baked meshes: evaluate the curve / GN stack once, render plain meshes
# ------------------------------------------------------------------------------------------------
_MESH_CACHE: "OrderedDict[str, tuple[str, str]]" = OrderedDict()   # key -> (mesh name, route)
BAKE_SCENE = "BIS Bake"


def _bake_scene() -> bpy.types.Scene:
    """A private scene whose depsgraph evaluates only the object being baked (the icon scene's relations
    are never touched)."""
    sc = bpy.data.scenes.get(BAKE_SCENE)
    if sc is None:
        sc = bpy.data.scenes.new(BAKE_SCENE)
    return sc


def solid_mesh(key_parts: dict, splines: list, thickness: float, bevel: float, route: str, segments: int = 6,
               gn_bevel: Optional[float] = None) -> tuple[bpy.types.Mesh, str]:
    """Cached MESH datablock of a piece: the bevelled curve (curve route) or the evaluated GN fallback stack
    (Fill Curve → Solidify → Bevel with ``gn_bevel``, default ``bevel``). Local units, z centred on 0.
    Returns ``(mesh, route)`` (the route may switch to 'gn', see :func:`curve_data`)."""
    gb = bevel if gn_bevel is None else gn_bevel
    cu, final_route = curve_data(key_parts, splines, thickness, bevel, route, segments)
    key = stable_hash({"cu": cu.get("bis_key", cu.name), "route": final_route, "gb": round(gb, 6), "s": segments,
                       "t": round(thickness, 6), "v": "m1"})
    hit = _MESH_CACHE.get(key)
    me = bpy.data.meshes.get(hit[0]) if hit else None
    if me is not None:
        _MESH_CACHE.move_to_end(key)
        return me, hit[1]
    ob = bpy.data.objects.new("BIS~bake", cu)
    try:
        if final_route != "gn":
            # extrude / bevel / offset are curve data: an unlinked original object evaluates just this curve
            me = bpy.data.meshes.new_from_object(ob)
        else:
            sc = _bake_scene()
            sc.collection.objects.link(ob)
            apply_route(ob, "gn", thickness, gb, segments, None)
            with bpy.context.temp_override(scene=sc, view_layer=sc.view_layers[0]):
                dg = bpy.context.evaluated_depsgraph_get()     # the bake scene's own depsgraph
                me = bpy.data.meshes.new_from_object(ob.evaluated_get(dg))
    finally:
        bpy.data.objects.remove(ob, do_unlink=True)
        sc = bpy.data.scenes.get(BAKE_SCENE)
        if sc is not None and sc != bpy.context.scene:     # keep saved .blend files free of it
            bpy.data.scenes.remove(sc)
    me.name = f"BIS~{key[:10]}"
    if len(me.materials) == 0:
        me.materials.append(None)
    else:
        for i in range(len(me.materials)):
            me.materials[i] = None          # the object-linked slot carries the material
    me["bis_key"] = key
    me["bis_route"] = final_route
    _MESH_CACHE[key] = (me.name, final_route)
    _evict()
    return me, final_route


def is_cached_mesh(me) -> bool:
    return me is not None and bool(me.get("bis_key"))


def purge_unused_meshes() -> int:
    cached = {v[0] for v in _MESH_CACHE.values()}
    n = 0
    for me in list(bpy.data.meshes):
        if me.get("bis_key") and me.users == 0 and me.name not in cached:
            bpy.data.meshes.remove(me)
            n += 1
    return n


def reset_caches() -> None:
    """Forget cached datablock names (after read_homefile the datablocks are gone)."""
    _CURVE_CACHE.clear()
    _MESH_CACHE.clear()
    _OCC_CACHE.clear()


def purge_unused_curves(all_unused: bool = False) -> int:
    """Remove unused BIS curves that fell out of the LRU cache (or every unused one)."""
    cached = {v[0] for v in _CURVE_CACHE.values()}
    n = 0
    for cu in list(bpy.data.curves):
        if cu.get("bis_key") and cu.users == 0 and (all_unused or cu.name not in cached):
            _CURVE_CACHE.pop(cu["bis_key"], None)
            bpy.data.curves.remove(cu)
            n += 1
    return n


# ------------------------------------------------------------------------------------------------
# GN fallback (Fill Curve N-gons -> Set Material -> smooth) + Solidify + Bevel
# ------------------------------------------------------------------------------------------------
def fill_group() -> bpy.types.NodeTree:
    ng = bpy.data.node_groups.get("BIS FillCurve")
    if ng is not None:
        return ng
    ng = bpy.data.node_groups.new("BIS FillCurve", "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    ng.interface.new_socket("Material", in_out="INPUT", socket_type="NodeSocketMaterial")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    gi = ng.nodes.new("NodeGroupInput")
    go = ng.nodes.new("NodeGroupOutput")
    fill = ng.nodes.new("GeometryNodeFillCurve")
    if "Mode" in fill.inputs:
        fill.inputs["Mode"].default_value = "N-gons"
    elif hasattr(fill, "mode"):
        fill.mode = "NGONS"
    setmat = ng.nodes.new("GeometryNodeSetMaterial")
    smooth = ng.nodes.new("GeometryNodeSetShadeSmooth")
    ng.links.new(gi.outputs["Geometry"], fill.inputs["Curve"])
    ng.links.new(fill.outputs["Mesh"], setmat.inputs["Geometry"])
    ng.links.new(gi.outputs["Material"], setmat.inputs["Material"])
    ng.links.new(setmat.outputs["Geometry"], smooth.inputs["Geometry"])
    ng.links.new(smooth.outputs["Geometry"], go.inputs[0])
    for i, n in enumerate((gi, fill, setmat, smooth, go)):
        n.location = (i * 220, 0)
    return ng


def apply_route(ob: bpy.types.Object, route: str, thickness: float, bevel_req: float, segments: int,
                mat: Optional[bpy.types.Material]) -> None:
    """Ensure the modifier stack matches the route (curve route: none)."""
    mods = ob.modifiers
    if route != "gn":
        for m in list(mods):
            if m.name.startswith("BIS"):
                mods.remove(m)
        return
    gn = mods.get("BIS Fill") or mods.new("BIS Fill", "NODES")
    ng = fill_group()
    if gn.node_group != ng:
        gn.node_group = ng
    ident = ng.interface.items_tree["Material"].identifier
    if gn.get(ident) != mat:
        gn[ident] = mat
    if thickness > 1e-6:
        so = mods.get("BIS Solidify") or mods.new("BIS Solidify", "SOLIDIFY")
        so.thickness = thickness
        so.offset = 0.0
        so.use_even_offset = True
        so.use_quality_normals = True
    elif mods.get("BIS Solidify"):
        mods.remove(mods["BIS Solidify"])
    if bevel_req > MIN_BEVEL and thickness > 1e-6:
        bv = mods.get("BIS Bevel") or mods.new("BIS Bevel", "BEVEL")
        bv.width = min(bevel_req, thickness / 2.0)
        bv.segments = max(1, int(segments))
        bv.limit_method = "ANGLE"
        bv.angle_limit = math.radians(40)
        bv.use_clamp_overlap = True
        bv.harden_normals = False
        bv.profile = 0.5
    elif mods.get("BIS Bevel"):
        mods.remove(mods["BIS Bevel"])
    # keep order: Fill -> Solidify -> Bevel
    order = [n for n in ("BIS Fill", "BIS Solidify", "BIS Bevel") if mods.get(n)]
    for i, n in enumerate(order):
        idx = mods.find(n)
        if idx != i:
            with bpy.context.temp_override(object=ob):
                bpy.ops.object.modifier_move_to_index(modifier=n, index=i)


# ------------------------------------------------------------------------------------------------
# plate outlines (same formulas as web/src/lib/shapes.ts)
# ------------------------------------------------------------------------------------------------
K = 0.5522847498  # cubic bezier circle constant


def _poly_to_bezier_smooth(pts: list[tuple[float, float]]) -> list[dict]:
    """Closed Catmull-Rom through ``pts`` -> FREE-handle bezier points (C1 smooth)."""
    n = len(pts)
    out = []
    for i in range(n):
        p0, p1, p2 = pts[i - 1], pts[i], pts[(i + 1) % n]
        tx, ty = (p2[0] - p0[0]) / 6.0, (p2[1] - p0[1]) / 6.0
        out.append({"co": [p1[0], p1[1]], "hl": [p1[0] - tx, p1[1] - ty], "hr": [p1[0] + tx, p1[1] + ty]})
    return out


def squircle_points(n: int = 64, exp: float = 5.0, r: float = 1.0) -> list[tuple[float, float]]:
    pts = []
    for i in range(n):
        t = 2 * math.pi * i / n
        c, s = math.cos(t), math.sin(t)
        x = r * math.copysign(abs(c) ** (2.0 / exp), c)
        y = r * math.copysign(abs(s) ** (2.0 / exp), s)
        pts.append((x, y))
    return pts


def plate_splines(shape: str, corner_radius: float = 0.225) -> list[dict]:
    """Plate outline in canvas space (−1..1) as contract-style splines (circle / squircle / square)."""
    if shape == "circle":
        pts = []
        for cx, cy in ((1, 0), (0, 1), (-1, 0), (0, -1)):
            tx, ty = -cy * K, cx * K
            pts.append({"co": [cx, cy], "hl": [cx - tx, cy - ty], "hr": [cx + tx, cy + ty]})
        return [{"closed": True, "points": pts}]
    if shape == "squircle":
        return [{"closed": True, "points": _poly_to_bezier_smooth(squircle_points(72))}]
    # square
    sq = [(1, 1), (-1, 1), (-1, -1), (1, -1)]
    pts = []
    for i, (x, y) in enumerate(sq):
        px, py = sq[i - 1]
        nx, ny = sq[(i + 1) % 4]
        pts.append({"co": [x, y], "hl": [x + (px - x) / 3, y + (py - y) / 3], "hr": [x + (nx - x) / 3, y + (ny - y) / 3]})
    return [{"closed": True, "points": pts}]


def rounded_rect_splines(corner_radius: float) -> list[dict]:
    """Rounded square −1..1 with corner radius r = cornerRadius × 2, CCW, exact quarter arcs."""
    r = max(1e-4, min(1.0, corner_radius * 2.0))
    k = r * K
    pts = []

    def arc(cx, cy, a0):
        # quarter arc around (cx, cy) from angle a0 to a0+90° (CCW)
        a1 = a0 + math.pi / 2
        p0 = (cx + r * math.cos(a0), cy + r * math.sin(a0))
        p1 = (cx + r * math.cos(a1), cy + r * math.sin(a1))
        t0 = (-math.sin(a0), math.cos(a0))
        t1 = (-math.sin(a1), math.cos(a1))
        return p0, t0, p1, t1

    corners = [(1 - r, 1 - r, 0.0), (-1 + r, 1 - r, math.pi / 2), (-1 + r, -1 + r, math.pi), (1 - r, -1 + r, 1.5 * math.pi)]
    segs = [arc(*c) for c in corners]
    for p0, t0, p1, t1 in segs:
        pts.append({"co": list(p0), "hl": None, "hr": [p0[0] + t0[0] * k, p0[1] + t0[1] * k]})
        pts.append({"co": list(p1), "hl": [p1[0] - t1[0] * k, p1[1] - t1[1] * k], "hr": None})
    n = len(pts)
    for i, p in enumerate(pts):
        if p["hl"] is None:          # straight edge from previous point
            q = pts[i - 1]["co"]
            p["hl"] = [p["co"][0] + (q[0] - p["co"][0]) / 3, p["co"][1] + (q[1] - p["co"][1]) / 3]
        if p["hr"] is None:
            q = pts[(i + 1) % n]["co"]
            p["hr"] = [p["co"][0] + (q[0] - p["co"][0]) / 3, p["co"][1] + (q[1] - p["co"][1]) / 3]
    return [{"closed": True, "points": pts}]


def plate_outline(shape: str, corner_radius: float = 0.225) -> list[dict]:
    if shape == "rounded" and corner_radius * 2 > 1e-4:
        return rounded_rect_splines(corner_radius)
    if shape == "rounded":
        shape = "square"
    return plate_splines(shape, corner_radius)


# ------------------------------------------------------------------------------------------------
# simple shapes for swatches / tests
# ------------------------------------------------------------------------------------------------
def circle_spline(cx: float, cy: float, r: float, hole: bool = False) -> dict:
    pts = []
    order = ((1, 0), (0, 1), (-1, 0), (0, -1))
    if hole:
        order = tuple(reversed(order))
    for ux, uy in order:
        tx, ty = (-uy * K * r, ux * K * r) if not hole else (uy * K * r, -ux * K * r)
        x, y = cx + ux * r, cy + uy * r
        pts.append({"co": [x, y], "hl": [x - tx, y - ty], "hr": [x + tx, y + ty]})
    return {"closed": True, "hole": hole, "parent": -1, "depth": 1 if hole else 0, "points": pts}


def rect_spline(x0: float, y0: float, x1: float, y1: float) -> dict:
    c = [(x1, y1), (x0, y1), (x0, y0), (x1, y0)]
    pts = []
    for i, (x, y) in enumerate(c):
        px, py = c[i - 1]
        nx, ny = c[(i + 1) % 4]
        pts.append({"co": [x, y], "hl": [x + (px - x) / 3, y + (py - y) / 3], "hr": [x + (nx - x) / 3, y + (ny - y) / 3]})
    return {"closed": True, "hole": False, "parent": -1, "depth": 0, "points": pts}


def poly_spline(corners: list) -> dict:
    """Closed straight-edged spline through ``corners`` (vector-like handles at 1/3)."""
    pts = []
    n = len(corners)
    for i, (x, y) in enumerate(corners):
        px, py = corners[i - 1]
        nx, ny = corners[(i + 1) % n]
        pts.append({"co": [x, y], "hl": [x + (px - x) / 3, y + (py - y) / 3], "hr": [x + (nx - x) / 3, y + (ny - y) / 3]})
    return {"closed": True, "hole": False, "parent": -1, "depth": 0, "points": pts}


def card_splines(bbox: list) -> list[dict]:
    return [rect_spline(*bbox)]


# ------------------------------------------------------------------------------------------------
# coarse occupancy masks (EEVEE refraction roles: does a glass layer above really overlap this one?)
# ------------------------------------------------------------------------------------------------
_OCC_CACHE: "OrderedDict[str, object]" = OrderedDict()
OCC_N = 64
OCC_EXTENT = 1.25


def _flatten_ring(points: list, closed: bool, n: int = 5) -> list:
    ring = []
    m = len(points)
    for i in range(m if closed else m - 1):
        a, b = points[i], points[(i + 1) % m]
        p0, c1, c2, p1 = a["co"], a.get("hr", a["co"]), b.get("hl", b["co"]), b["co"]
        for j in range(n):
            t = j / n
            mt = 1 - t
            ring.append((mt ** 3 * p0[0] + 3 * mt * mt * t * c1[0] + 3 * mt * t * t * c2[0] + t ** 3 * p1[0],
                         mt ** 3 * p0[1] + 3 * mt * mt * t * c1[1] + 3 * mt * t * t * c2[1] + t ** 3 * p1[1]))
    return ring


def occupancy(key: str, splines: list, scale: float, dx: float, dy: float):
    """Even-odd raster (OCC_N², canvas −1.25..1.25) of ``splines`` mapped by p*scale + (dx, dy),
    dilated by one cell. Cached by ``key``."""
    import numpy as np
    ck = f"{key}|{scale:.5f}|{dx:.5f}|{dy:.5f}"
    hit = _OCC_CACHE.get(ck)
    if hit is not None:
        return hit
    n = OCC_N
    c = (np.arange(n) + 0.5) / n * (2 * OCC_EXTENT) - OCC_EXTENT
    px, py = np.meshgrid(c, c)
    px = px.ravel()
    py = py.ravel()
    inside = np.zeros(px.shape, dtype=bool)
    for s in splines:
        ring = _flatten_ring(s.get("points") or [], bool(s.get("closed", True)))
        if len(ring) < 3:
            continue
        r = np.asarray(ring, dtype=np.float64) * scale + np.array([dx, dy])
        x0, y0 = r[:, 0][:, None], r[:, 1][:, None]
        r1 = np.roll(r, -1, axis=0)
        x1, y1 = r1[:, 0][:, None], r1[:, 1][:, None]
        cond = (y0 > py) != (y1 > py)
        with np.errstate(divide="ignore", invalid="ignore"):
            xint = x0 + (py - y0) * (x1 - x0) / np.where(np.abs(y1 - y0) < 1e-12, 1e-12, (y1 - y0))
        crossings = np.count_nonzero(cond & (px < xint), axis=0)
        inside ^= (crossings % 2).astype(bool)
    m = inside.reshape(n, n)
    d = m.copy()
    d[1:, :] |= m[:-1, :]
    d[:-1, :] |= m[1:, :]
    d[:, 1:] |= m[:, :-1]
    d[:, :-1] |= m[:, 1:]
    _OCC_CACHE[ck] = d
    while len(_OCC_CACHE) > 64:
        _OCC_CACHE.popitem(last=False)
    return d
