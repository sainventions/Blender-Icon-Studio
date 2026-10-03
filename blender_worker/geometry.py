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

Robustness (QA round 2): region / silhouette splines are fills, so they are always closed (an open subpath
was swept as a hollow tube); collinear runs are joined before only CONVEX corners are filleted (a concave
fillet grew the silhouette); and every build is validated by casting camera rays at the evaluated mesh
(:func:`check_piece`: no missing / inverted / folded cap, nothing outside the outline). A failing build
falls back to the GN route, then to a bevel clamped to the piece's own safe radius, then to a plain
extrusion. The saved .blend (``save_blend``) uses the curves themselves (live bevel / modifier stack).

Baked meshes (:func:`solid_mesh`): the scene renders MESH objects whose data is the curve (or the GN
route's Fill Curve → Solidify → Bevel stack) evaluated ONCE and cached. A render with persistent data off
evaluates every object from scratch, so live curve bevels / modifier stacks were re-evaluated on every
render (a traced raster contour through the GN route: ~6 s per render for two pieces).
Splines get an adaptive ``resolution_u`` (dense traced contours need no 12× subdivision per segment).
"""
from __future__ import annotations

import json
import math
from collections import OrderedDict
from typing import Iterable, Optional

import bpy

from .util import log, stable_hash

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


def count_open(splines: list) -> int:
    """Splines flagged ``closed: false`` (SVG subpaths without 'Z', e.g. ending in 'h0')."""
    return sum(1 for s in splines if not s.get("closed", True) and len(s.get("points") or []) > 1)


def sanitize(splines: list, eps: float = 2.5e-4, min_area: float = 2e-7) -> list:
    """Region / silhouette splines are FILLS: every spline comes back closed (an open subpath is filled by
    SVG as if closed — a flag left open made the curve bevel sweep a hollow tube along the outline). A
    first point that coincides with the last is merged into it; otherwise an open spline is closed with a
    straight segment. Coincident consecutive points are merged (zero-length segments make the curve bevel
    shoot spikes along an undefined normal) and degenerate slivers are dropped."""
    out = []
    e2 = eps * eps
    for s in splines:
        pts = [{"co": list(p["co"]), "hl": list(p.get("hl") or p["co"]), "hr": list(p.get("hr") or p["co"])}
               for p in s.get("points") or []]
        was_open = not bool(s.get("closed", True))
        merged: list = []
        for p in pts:
            if merged and _d2(p["co"], merged[-1]["co"]) < e2:
                merged[-1]["hr"] = p["hr"]
            else:
                merged.append(p)
        if len(merged) > 1 and _d2(merged[0]["co"], merged[-1]["co"]) < e2:
            last = merged.pop()
            merged[0]["hl"] = last["hl"]
        elif was_open and len(merged) > 1:
            # implicit straight closing segment (the open path's dangling end handles are meaningless)
            a, b = merged[-1], merged[0]
            a["hr"] = [a["co"][0] + (b["co"][0] - a["co"][0]) / 3, a["co"][1] + (b["co"][1] - a["co"][1]) / 3]
            b["hl"] = [b["co"][0] + (a["co"][0] - b["co"][0]) / 3, b["co"][1] + (a["co"][1] - b["co"][1]) / 3]
        if len(merged) < 2 or abs(_flat_area(merged, True)) < min_area:
            continue
        out.append({**s, "closed": True, "points": merged})
    return out


# ------------------------------------------------------------------------------------------------
# collinear runs: boolean-op outlines split straight edges into many short collinear segments. Fillets
# are limited by the adjacent segment lengths, so a sharp corner between two such runs stayed unfilleted
# (miter-pinched round bevel: dark specks / glints at acute corners — Home, Drive). Joining the runs
# first lets the corner get its full fillet.
# ------------------------------------------------------------------------------------------------
STRAIGHT_TURN = 3.0        # degrees: a joint between straight segments that turns less is removable
STRAIGHT_TOL = 4e-4        # max distance of a removed point / handle from the merged chord (local units)


def _seg_straight(p0, c1, c2, p1, tol: float) -> bool:
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    ln = math.hypot(dx, dy)
    if ln < 1e-9:
        return True
    for c in (c1, c2):
        rx, ry = c[0] - p0[0], c[1] - p0[1]
        if abs(rx * dy - ry * dx) / ln > tol:
            return False
        t = (rx * dx + ry * dy) / ln
        if t < -tol or t > ln + tol:
            return False
    return True


def _pt_line_dist(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    ln = math.hypot(dx, dy)
    if ln < 1e-12:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (ln * ln)))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def merge_collinear(splines: list, max_turn: float = STRAIGHT_TURN, tol: float = STRAIGHT_TOL) -> list:
    """Remove points joining two straight segments almost in line (turn < ``max_turn``) as long as every
    removed point stays within ``tol`` of the merged chord. Closed splines only; curved segments are kept."""
    cos_t = math.cos(math.radians(max_turn))
    out = []
    for s in splines:
        pts = s["points"]
        m = len(pts)
        if m < 4:
            out.append(s)
            continue
        straight = [_seg_straight(pts[i]["co"], pts[i]["hr"], pts[(i + 1) % m]["hl"], pts[(i + 1) % m]["co"], tol)
                    for i in range(m)]

        def removable(i):
            if not (straight[i - 1] and straight[i]):
                return False
            a, p, b = pts[i - 1]["co"], pts[i]["co"], pts[(i + 1) % m]["co"]
            u = _unit((p[0] - a[0], p[1] - a[1]))
            v = _unit((b[0] - p[0], b[1] - p[1]))
            return u[0] * v[0] + u[1] * v[1] >= cos_t

        rem = [removable(i) for i in range(m)]
        if not any(rem):
            out.append(s)
            continue
        start = next((i for i in range(m) if not rem[i]), 0)
        keep = [start]
        run: list = []
        for k in range(1, m + 1):
            i = (start + k) % m
            if k < m and rem[i]:
                a = pts[keep[-1]]["co"]
                b = pts[(i + 1) % m]["co"]
                if all(_pt_line_dist(pts[j]["co"], a, b) <= tol for j in run + [i]):
                    run.append(i)
                    continue
                keep.append(i)          # deviation too large: keep this point as a new anchor
                run = []
                continue
            if k < m:
                keep.append(i)
            run = []
        if len(keep) < 3 or len(keep) == m:
            out.append(s)
            continue
        new = [{"co": list(pts[i]["co"]), "hl": list(pts[i]["hl"]), "hr": list(pts[i]["hr"])} for i in keep]
        n = len(new)
        for k in range(n):
            i, j = keep[k], keep[(k + 1) % n]
            if (j - i) % m != 1:        # merged run: straight chord with handles at thirds
                a, b = new[k], new[(k + 1) % n]
                a["hr"] = [a["co"][0] + (b["co"][0] - a["co"][0]) / 3, a["co"][1] + (b["co"][1] - a["co"][1]) / 3]
                b["hl"] = [b["co"][0] + (a["co"][0] - b["co"][0]) / 3, b["co"][1] + (a["co"][1] - b["co"][1]) / 3]
        out.append({**s, "points": new})
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


def _material_left(s: dict) -> bool:
    """True when the filled side of a closed spline is on the left of its direction of travel (CCW outer
    or CW hole; a hole is an odd nesting depth)."""
    ring = _flatten_ring(s["points"], True, 4)
    if len(ring) < 3:
        return True
    area = 0.5 * sum(ring[i - 1][0] * ring[i][1] - ring[i][0] * ring[i - 1][1] for i in range(len(ring)))
    hole = bool(s.get("hole")) or int(s.get("depth", 0) or 0) % 2 == 1
    return (area > 0) != hole


def fillet_corners(splines: list, radius: float, min_turn: float = FILLET_MIN_TURN, min_radius: float = 0.0,
                   convex_only: bool = True, skipped: Optional[list] = None) -> list:
    """Round every corner sharper than ``min_turn`` with an (approximately circular) fillet of
    ``radius``. A fillet that does not fit in 45 % of the adjacent segments is shrunk, or — when
    ``min_radius`` > 0 — skipped (the corner stays sharp; its turn angle is appended to ``skipped``).
    ``convex_only``: concave corners are left sharp — a fillet there adds material outside the outline
    (the silhouette grew into the inner corners of a '+'), and the inset outline of a concave corner
    does not loop anyway. Closed splines only."""
    if radius <= 1e-6:
        return splines
    out = []
    for s in splines:
        pts = s["points"]
        m = len(pts)
        if not s.get("closed", True) or m < 2:
            out.append(s)
            continue
        mat_left = _material_left(s) if convex_only else True
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
            if convex_only:
                left_turn = tin[0] * tout[1] - tin[1] * tout[0] > 0
                if left_turn != mat_left:
                    continue            # concave corner
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
                if min_radius > 0 and skipped is not None:
                    skipped.append(turn[i])
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


GUARD_RATIO = 1.0     # guard point distance from a sharp corner, relative to the bevel


def _seg_tangents(pts: list, i: int) -> tuple:
    """(incoming, outgoing) unit tangents at point i of a closed spline."""
    m = len(pts)
    a, p, b = pts[i - 1], pts[i], pts[(i + 1) % m]
    tin = (0.0, 0.0)
    for q in (p["hl"], a["hr"], a["co"]):
        tin = _unit((p["co"][0] - q[0], p["co"][1] - q[1]))
        if tin != (0.0, 0.0):
            break
    tout = (0.0, 0.0)
    for q in (p["hr"], b["hl"], b["co"]):
        tout = _unit((q[0] - p["co"][0], q[1] - p["co"][1]))
        if tout != (0.0, 0.0):
            break
    return tin, tout


def guard_corners(splines: list, dist: float, min_turn: float = FILLET_MIN_TURN) -> list:
    """Split the STRAIGHT segments next to every corner left sharp (concave corners — fillet_corners keeps
    them sharp so the silhouette never grows — and unfilletable convex ones) at ``dist`` from the corner.

    The round curve bevel mitres a sharp corner and its smooth vertex normal points along the bisector. A
    straight edge is a single quad strip (resolution 1 on all-straight splines), so that diagonal normal was
    interpolated along the WHOLE edge: skewed, wedge-shaped shading on every edge of a rectangular hole
    (Sheets' cells, Slides' frame). A guard point confines the mitre normal to ``dist`` of the corner; the
    outline is unchanged. Closed splines only."""
    if dist <= 1e-6:
        return splines
    out = []
    for s in splines:
        pts = s["points"]
        m = len(pts)
        if not s.get("closed", True) or m < 3:
            out.append(s)
            continue
        sharp = []
        for i in range(m):
            tin, tout = _seg_tangents(pts, i)
            ang = math.degrees(math.acos(max(-1.0, min(1.0, tin[0] * tout[0] + tin[1] * tout[1]))))
            sharp.append(min_turn < ang < 175.0)
        if not any(sharp):
            out.append(s)
            continue
        cp = [{"co": list(p["co"]), "hl": list(p["hl"]), "hr": list(p["hr"])} for p in pts]
        new = []
        for i in range(m):
            p, q = cp[i], cp[(i + 1) % m]
            new.append(p)
            if not (sharp[i] or sharp[(i + 1) % m]):
                continue
            if not _seg_straight(p["co"], p["hr"], q["hl"], q["co"], STRAIGHT_TOL):
                continue
            ln = math.dist(p["co"], q["co"])
            d = min(dist, 0.3 * ln)
            if d < 1e-3 * max(ln, 1e-9) or d < 1e-4:
                continue
            ts = ([d / ln] if sharp[i] else []) + ([1.0 - d / ln] if sharp[(i + 1) % m] else [])
            mids = [{"co": [p["co"][0] + (q["co"][0] - p["co"][0]) * t, p["co"][1] + (q["co"][1] - p["co"][1]) * t]}
                    for t in ts]
            chain = [p] + mids + [q]
            for u, v in zip(chain, chain[1:]):
                u["hr"] = [u["co"][0] + (v["co"][0] - u["co"][0]) / 3, u["co"][1] + (v["co"][1] - u["co"][1]) / 3]
                v["hl"] = [v["co"][0] + (u["co"][0] - v["co"][0]) / 3, v["co"][1] + (u["co"][1] - v["co"][1]) / 3]
            new.extend(mids)
        out.append({**s, "points": new})
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
ACUTE_GN_DEG = 100.0  # an unfilletable convex corner sharper than this pinches the round bevel -> GN route
_PENDING_MESH: dict = {}   # curve key -> name of the mesh evaluated (and validated) on the cache miss


def _set_curve_route(cu: bpy.types.Curve, route: str, thickness: float, bevel: float, segments: int) -> None:
    if route == "curve":
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


def _evaluate(cu: bpy.types.Curve, route: str, thickness: float, gb: float, segments: int) -> bpy.types.Mesh:
    """Evaluate a piece once into a new mesh: the bevelled curve (curve route) or Fill Curve → Solidify →
    Bevel (GN route, ``gb`` = Bevel modifier width)."""
    ob = bpy.data.objects.new("BIS~bake", cu)
    try:
        if route != "gn":
            # extrude / bevel / offset are curve data: an UNLINKED original object evaluates just this curve
            # (linking it into the icon scene re-evaluated the scene's relations on every cache miss)
            return bpy.data.meshes.new_from_object(ob)
        sc = _bake_scene()
        sc.collection.objects.link(ob)
        apply_route(ob, "gn", thickness, gb, segments, None)
        with bpy.context.temp_override(scene=sc, view_layer=sc.view_layers[0]):
            dg = bpy.context.evaluated_depsgraph_get()     # the bake scene's own depsgraph
            return bpy.data.meshes.new_from_object(ob.evaluated_get(dg))
    finally:
        bpy.data.objects.remove(ob, do_unlink=True)
        sc = bpy.data.scenes.get(BAKE_SCENE)
        if sc is not None and sc != bpy.context.scene:     # keep saved .blend files free of it
            bpy.data.scenes.remove(sc)


def piece_safe_radius(splines: list, res: int = 128, keep: float = 0.985) -> float:
    """Largest radius whose morphological opening keeps ``keep`` of the piece's area (raster, numpy only):
    the bevel a single piece can take without inverting its thin features. The layer ``safeRadius`` is
    measured on the layer union, where a thin ring merged with its neighbours can look thick (Ti73's pie)."""
    import numpy as np
    rings = [np.asarray(_flatten_ring(s["points"], True, 6), dtype=np.float64) for s in splines]
    rings = [r for r in rings if len(r) >= 3]
    if not rings:
        return 0.0
    allr = np.vstack(rings)
    x0, y0 = allr.min(axis=0)
    x1, y1 = allr.max(axis=0)
    h = max(x1 - x0, y1 - y0) / res
    if h <= 1e-9:
        return 0.0
    xs = np.arange(x0 - 2 * h, x1 + 2 * h, h)
    ys = np.arange(y0 - 2 * h, y1 + 2 * h, h)
    inside = _scan_inside(_ring_segments(rings), xs, ys)
    area = int(inside.sum())
    if area < 8:
        return 0.0

    def step(m, grow, square):
        o = m.copy()
        op = np.logical_or if grow else np.logical_and
        o[1:, :] = op(o[1:, :], m[:-1, :])
        o[:-1, :] = op(o[:-1, :], m[1:, :])
        o[:, 1:] = op(o[:, 1:], m[:, :-1])
        o[:, :-1] = op(o[:, :-1], m[:, 1:])
        if square:
            o[1:, 1:] = op(o[1:, 1:], m[:-1, :-1])
            o[:-1, :-1] = op(o[:-1, :-1], m[1:, 1:])
            o[1:, :-1] = op(o[1:, :-1], m[:-1, 1:])
            o[:-1, 1:] = op(o[:-1, 1:], m[1:, :-1])
        return o

    def opened(rp: int) -> int:
        m = inside
        for i in range(rp):            # alternating cross / square: an octagon ~ a disc of radius rp
            m = step(m, False, i % 2 == 1)
        for i in range(rp):
            m = step(m, True, i % 2 == 1)
        return int((m & inside).sum())

    lo, hi = 0, max(1, res // 2)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if opened(mid) >= keep * area:
            lo = mid
        else:
            hi = mid - 1
    return lo * h


def _badness(rep: dict) -> float:
    return rep["holes"] + rep["grow"]


def curve_data(key_parts: dict, splines: list, thickness: float, bevel: float, route: str,
               segments: int = 6, gn_bevel: Optional[float] = None) -> tuple[bpy.types.Curve, str]:
    """Cached 2D curve datablock in LOCAL units (z centred on 0, spans ±thickness/2).

    Returns ``(curve, route)``. Splines are only processed on a cache miss: sanitised (always closed
    fills), collinear runs joined, convex corners filleted for the round bevel. Every candidate build is
    evaluated once and validated with :func:`check_piece` (no missing / inverted cap, nothing outside the
    outline); the first one that passes wins and its mesh is handed to :func:`solid_mesh`:

    1. 'curve' — round curve bevel, ``offset = −bevel`` (skipped for cusps / unfilletable acute corners);
    2. 'gn' — Fill Curve → Solidify → Bevel modifier (``gn_bevel``; clamps itself, never grows);
    3. 'curve' with the bevel clamped to the piece's own safe radius (thin rings whose layer safeRadius was
       measured on a thick union; Fill Curve can also fail on holes that nearly touch the outline);
    4. 'curve' without bevel (plain extrusion).
    If all fail, the least bad one is kept. ``cu['bis_bevel']`` / ``cu['bis_gb']`` record the bevel used."""
    gb = bevel if gn_bevel is None else gn_bevel
    key = stable_hash({**key_parts, "t": round(thickness, 6), "b": round(bevel, 6), "r": route, "s": segments,
                       "gb": round(gb, 6), "v": 5})
    hit = _CURVE_CACHE.get(key)
    cu = bpy.data.curves.get(hit[0]) if hit else None
    if cu is not None:
        _CURVE_CACHE.move_to_end(key)
        return cu, hit[1]
    clean = merge_collinear(sanitize(splines))
    reasons = []
    cands = []
    if route == "curve" and bevel > MIN_BEVEL:
        if max_turn_deg(clean) > CUSP_DEG:
            reasons.append("cusp")
        else:
            skipped: list = []
            shaped = guard_corners(sanitize(fillet_corners(clean, FILLET_RATIO * bevel, min_radius=bevel,
                                                           skipped=skipped)), GUARD_RATIO * bevel)
            if any(t > ACUTE_GN_DEG for t in skipped):
                reasons.append("acute corner")
            else:
                cands.append(("curve", bevel, shaped))
    cands.append(("gn", gb, clean))
    cu = bpy.data.curves.new(f"BIS~{key[:10]}", "CURVE")
    cu.dimensions = "2D"
    cu.resolution_u = 12
    cu.twist_mode = "MINIMUM"
    cu.materials.append(None)
    best = None            # (badness, route, bevel, splines, mesh)
    k = 0
    tried_safe = False
    while k < len(cands):
        r_route, r_bev, r_spl = cands[k]
        k += 1
        cu.splines.clear()
        _fill_splines(cu, r_spl)
        _set_curve_route(cu, r_route, thickness, r_bev, segments)
        me = _evaluate(cu, r_route, thickness, r_bev, segments)
        rep = check_piece(me, r_spl, r_bev if r_route == "curve" else 0.0, clean)
        if piece_ok(rep):
            if best is not None:
                bpy.data.meshes.remove(best[4])
            best = (0.0, r_route, r_bev, r_spl, me)
            break
        reasons.append(f"{r_route} {r_bev:.3f} check (holes {rep['holes']:.3f}, outside {rep['grow']:.3f})")
        if best is None or _badness(rep) < best[0]:
            if best is not None:
                bpy.data.meshes.remove(best[4])
            best = (_badness(rep), r_route, r_bev, r_spl, me)
        else:
            bpy.data.meshes.remove(me)
        if k == len(cands) and not tried_safe:
            tried_safe = True
            sr = 0.9 * piece_safe_radius(clean)
            b2 = min(bevel if bevel > MIN_BEVEL else gb, sr, thickness / 2.0)
            if b2 > MIN_BEVEL:
                cands.append(("curve", b2, guard_corners(sanitize(fillet_corners(clean, FILLET_RATIO * b2,
                                                                                  min_radius=b2)), GUARD_RATIO * b2)))
            cands.append(("curve", 0.0, clean))
    _, f_route, f_bev, f_spl, me = best
    cu.splines.clear()
    _fill_splines(cu, f_spl)
    _set_curve_route(cu, f_route, thickness, f_bev, segments)
    if reasons:
        cu["bis_reason"] = "; ".join(reasons)
        if best[0] > 0:
            log("piece kept with defects:", key_parts.get("lid", ""), key_parts.get("p", ""), cu["bis_reason"])
    cu["bis_key"] = key
    cu["bis_route"] = f_route
    cu["bis_outline"] = json.dumps([{"points": sp["points"], "hole": bool(sp.get("hole"))} for sp in clean],
                                   separators=(",", ":"))        # true fill outline (scene_info checks)
    cu["bis_bevel"] = f_bev if f_route == "curve" else 0.0
    cu["bis_gb"] = f_bev if f_route == "gn" else 0.0
    _PENDING_MESH[key] = me.name
    _CURVE_CACHE[key] = (cu.name, f_route)
    _evict()
    return cu, f_route


CHECK_GRID = 44          # samples along the longer side of a piece for check_piece
HOLE_MAX = 0.006         # tolerated fraction of inside samples without a front-facing surface
GROW_MAX = 0.004         # tolerated outside hits (fraction of the inside sample count)


def _ring_segments(rings: list):
    import numpy as np
    return np.vstack([np.hstack([r, np.roll(r, -1, axis=0)]) for r in rings])


def _scan_inside(segs, xs, ys):
    """Even-odd raster (len(ys), len(xs)) of closed polylines given as segments (E, 4): one scanline per
    row (crossings sorted + searchsorted) — O(rows · E) instead of O(points · E)."""
    import numpy as np
    xa, ya, xb, yb = segs[:, 0], segs[:, 1], segs[:, 2], segs[:, 3]
    out = np.zeros((len(ys), len(xs)), dtype=bool)
    for j, y in enumerate(ys):
        c = (ya > y) != (yb > y)
        if not c.any():
            continue
        xc = xa[c] + (y - ya[c]) * (xb[c] - xa[c]) / (yb[c] - ya[c])
        xc.sort()
        out[j] = (np.searchsorted(xc, xs) % 2) == 1
    return out


def _near_mask(segs, gx0: float, gy0: float, h: float, shape: tuple, tol: float):
    """Cells (centres gx0 + i·h, gy0 + j·h) that may lie within ``tol`` of the polylines (conservative:
    the outline is sampled every h/4; a cell centre within ``tol`` of a sample lies at most
    floor(tol/h + 0.625) cells (Chebyshev) from that sample's cell)."""
    import numpy as np
    x0, y0 = segs[:, 0], segs[:, 1]
    dx, dy = segs[:, 2] - x0, segs[:, 3] - y0
    n = np.maximum(1, np.ceil(np.hypot(dx, dy) / (0.25 * h)).astype(int))
    idx = np.repeat(np.arange(len(segs)), n)
    t = (np.arange(int(n.sum())) - np.repeat(np.cumsum(n) - n, n)) / np.repeat(n, n)
    qx = x0[idx] + t * dx[idx]
    qy = y0[idx] + t * dy[idx]
    ny, nx = shape
    ix = np.clip(np.round((qx - gx0) / h).astype(int), 0, nx - 1)
    iy = np.clip(np.round((qy - gy0) / h).astype(int), 0, ny - 1)
    m = np.zeros(shape, dtype=bool)
    m[iy, ix] = True
    for _ in range(int(np.floor(tol / h + 0.625))):
        o = m.copy()
        o[1:, :] |= m[:-1, :]
        o[:-1, :] |= m[1:, :]
        o[:, 1:] |= m[:, :-1]
        o[:, :-1] |= m[:, 1:]
        o[1:, 1:] |= m[:-1, :-1]
        o[:-1, :-1] |= m[1:, 1:]
        o[1:, :-1] |= m[:-1, 1:]
        o[:-1, 1:] |= m[1:, :-1]
        m = o
    return m


def check_piece(me: bpy.types.Mesh, splines: list, bevel: float, outline: Optional[list] = None) -> dict:
    """Validate a baked piece against its outline by casting camera rays (+Z → −Z) on a grid:

    * ``holes`` — fraction of the samples inside the outline whose first hit is missing or back-facing:
      the front cap is missing / inverted, or the bevel folded over itself (the piece renders hollow —
      Scandit's bracket and 'D' — or with dark specks / glints at pinched corners);
    * ``grow`` — hits on samples outside the outline (relative to the inside count): the bevel inverted a
      thin feature or overshot a corner, so the piece pokes out past its silhouette (DJI's blades).
    ``splines`` is the shape actually built (filleted corners), ``outline`` the true silhouette (default:
    the same) — fillets only remove material. Samples near either outline are ignored."""
    import bmesh
    import numpy as np
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree

    def _rings(spl):
        rr = [np.asarray(_flatten_ring(s["points"], True, 6), dtype=np.float64) for s in spl]
        return [r for r in rr if len(r) >= 3]

    rings = _rings(splines)
    orings = _rings(outline) if outline is not None else rings
    nv = len(me.vertices)
    if not rings or nv < 3:
        return {"holes": 1.0 if rings else 0.0, "grow": 0.0, "n": 0, "holesN": 0, "growN": 0}
    co = np.empty(nv * 3, dtype=np.float64)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    allr = np.vstack(rings + orings)
    x0 = min(allr[:, 0].min(), co[:, 0].min())
    x1 = max(allr[:, 0].max(), co[:, 0].max())
    y0 = min(allr[:, 1].min(), co[:, 1].min())
    y1 = max(allr[:, 1].max(), co[:, 1].max())
    h = max(x1 - x0, y1 - y0) / CHECK_GRID
    if h <= 1e-9:
        return {"holes": 0.0, "grow": 0.0, "n": 0, "holesN": 0, "growN": 0}
    xs = np.arange(x0 + h / 2, x1, h)
    ys = np.arange(y0 + h / 2, y1, h)
    tol = max(0.0025, 0.12 * bevel, 0.35 * h)
    segs = _ring_segments(rings)
    test_in = _scan_inside(segs, xs, ys) & ~_near_mask(segs, xs[0], ys[0], h, (len(ys), len(xs)), tol)
    osegs = segs if orings is rings else _ring_segments(orings)
    o_in = _scan_inside(osegs, xs, ys)
    test_out = ~o_in & ~_near_mask(osegs, xs[0], ys[0], h, (len(ys), len(xs)), tol)
    X, Y = np.meshgrid(xs, ys)
    bm = bmesh.new()
    bm.from_mesh(me)
    tree = BVHTree.FromBMesh(bm)
    bm.free()
    ztop = float(co[:, 2].max()) + 1.0
    down = Vector((0.0, 0.0, -1.0))
    holes = 0
    n_in = int(test_in.sum())
    jit = (0.0, 0.0), (0.013 * h, 0.021 * h), (-0.017 * h, -0.011 * h)
    for x, y in zip(X[test_in], Y[test_in]):
        for jx, jy in jit:        # a ray exactly on a triangle edge of a grid-aligned cap can slip through
            loc, nor, _i, _d = tree.ray_cast(Vector((x + jx, y + jy, ztop)), down)
            if loc is not None and nor.z > 0.02:
                break
        else:
            holes += 1
    grow = 0
    for x, y in zip(X[test_out], Y[test_out]):
        loc, _n, _i, _d = tree.ray_cast(Vector((x, y, ztop)), down)
        if loc is not None:
            grow += 1
    return {"holes": holes / max(1, n_in), "grow": grow / max(1, n_in), "n": n_in,
            "holesN": holes, "growN": grow}


def curve_splines(cu: bpy.types.Curve) -> list:
    """Contract-style splines of a curve datablock (the shape actually built)."""
    return [{"closed": True, "points": [{"co": list(bp.co[:2]), "hl": list(bp.handle_left[:2]),
                                         "hr": list(bp.handle_right[:2])} for bp in sp.bezier_points]}
            for sp in cu.splines]


def piece_ok(r: dict) -> bool:
    return not ((r["holes"] > HOLE_MAX and r.get("holesN", 0) >= 2) or (r["grow"] > GROW_MAX and r.get("growN", 0) >= 2))


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
    Returns ``(mesh, route)`` (the route may switch, see :func:`curve_data`)."""
    cu, final_route = curve_data(key_parts, splines, thickness, bevel, route, segments, gn_bevel)
    key = stable_hash({"cu": cu.get("bis_key", cu.name), "route": final_route, "s": segments,
                       "t": round(thickness, 6), "v": "m2"})
    hit = _MESH_CACHE.get(key)
    me = bpy.data.meshes.get(hit[0]) if hit else None
    pending = _PENDING_MESH.pop(cu.get("bis_key", ""), None)
    pme = bpy.data.meshes.get(pending) if pending else None
    if me is not None:
        _MESH_CACHE.move_to_end(key)
        if pme is not None and pme.users == 0 and pme != me:
            bpy.data.meshes.remove(pme)
        return me, hit[1]
    if pme is not None:
        me = pme                 # evaluated + validated by curve_data on this cache miss
    else:
        gb = float(cu.get("bis_gb", 0.0) or 0.0) if final_route == "gn" else 0.0
        me = _evaluate(cu, final_route, thickness, gb, segments)
    me.name = f"BIS~{key[:10]}"
    if len(me.materials) == 0:
        me.materials.append(None)
    else:
        for i in range(len(me.materials)):
            me.materials[i] = None          # the object-linked slot carries the material
    me["bis_key"] = key
    me["bis_route"] = final_route
    me["bis_curve"] = cu.name
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
    _PENDING_MESH.clear()


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
