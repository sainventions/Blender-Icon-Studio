"""Spline hygiene for exported geometry (art space).

The 3D builders sweep every spline with a round bevel whose inset outline is ~0.045 art units away
from the contour. Debris that is invisible in a flat fill breaks that sweep:

* micro segments (rounding debris of boolean ops: a corner split into 0.0005-long pieces, zero-length
  segments with dangling handles, micro loops that come back to their start) hide sharp corners
  from the builder's corner handling and make the inset outline loop -> failed / hollow caps;
* spikes (out-and-back slivers of zero area) and self-intersections (figure-8 twists);
* degenerate splines (slivers with ~zero area, < 3 points).

:func:`clean_splines` removes them while moving the outline by at most ~``MICRO`` and guarantees
the corpus invariant checked by the tests: every spline is closed, has >= 3 points, a
non-degenerate area and does not self-intersect."""
from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

import numpy as np
import shapely
import shapely.errors
from shapely.geometry import LineString, Polygon
from shapely.strtree import STRtree

MICRO = 0.004            # art units (~1 px of a 500 px icon): shorter segments are merged
DEGENERATE = 1e-5        # segments shorter than this are always merged (zero-length debris)
KINK_DEG = 5.0           # a short segment next to a tangent discontinuity above this is debris
MIN_AREA = 1e-6          # art units²: smaller splines are dropped (slivers)
SPIKE_TURN_DEG = 170.0   # out-and-back turns sharper than this ...
SPIKE_MAX_LEN = 0.02     # ... on short arms ...
SPIKE_AREA_RATIO = 0.02  # ... enclosing (almost) no area are spikes
SAMPLES = 8              # samples per cubic for area / intersection tests
LOOP_KEEP_RATIO = 0.02   # a self-intersection lobe smaller than this share of the other is dropped

Pt = Tuple[float, float]


# ----------------------------------------------------------------------------------------------
# cubic helpers
# ----------------------------------------------------------------------------------------------
def _seg(points: Sequence[dict], i: int) -> Tuple[Pt, Pt, Pt, Pt]:
    a, b = points[i], points[(i + 1) % len(points)]
    return tuple(a["co"]), tuple(a["hr"]), tuple(b["hl"]), tuple(b["co"])  # type: ignore[return-value]


def _ctrl_len(seg) -> float:
    p0, c1, c2, p1 = seg
    return math.dist(p0, c1) + math.dist(c1, c2) + math.dist(c2, p1)


def _sub(seg, t0: float, t1: float):
    """Exact sub-curve [t0, t1] of a cubic (de Casteljau)."""
    p = np.asarray(seg, dtype=float)

    def split(q, t):
        a = q[:3] + (q[1:] - q[:3]) * t
        b = a[:2] + (a[1:] - a[:2]) * t
        c = b[0] + (b[1] - b[0]) * t
        return np.array([q[0], a[0], b[0], c]), np.array([c, b[1], a[2], q[3]])

    if t1 < 1.0:
        p, _ = split(p, t1)
        t0 = t0 / t1 if t1 > 1e-12 else 0.0
    if t0 > 0.0:
        _, p = split(p, t0)
    return tuple(tuple(float(v) for v in row) for row in p)


def _from_pieces(pieces: Sequence) -> List[dict]:
    """Closed chain of cubic pieces -> spline points."""
    n = len(pieces)
    return [{"co": pieces[k][0], "hl": pieces[k - 1][2], "hr": pieces[k][1]} for k in range(n)]


def sample(points: Sequence[dict], ns: int = SAMPLES) -> Tuple[np.ndarray, np.ndarray]:
    """-> (samples (n*ns, 2), parameter (segment index + t)) of a closed spline."""
    n = len(points)
    t = np.arange(ns) / ns
    mt = 1 - t
    w = np.stack([mt ** 3, 3 * mt * mt * t, 3 * mt * t * t, t ** 3], axis=1)  # (ns, 4)
    ctrl = np.array([_seg(points, i) for i in range(n)], dtype=float)        # (n, 4, 2)
    pts = np.einsum("tk,nkd->ntd", w, ctrl).reshape(-1, 2)
    par = (np.arange(n)[:, None] + t[None, :]).reshape(-1)
    return pts, par


def signed_area(points: Sequence[dict], ns: int = SAMPLES) -> float:
    if len(points) < 1:
        return 0.0
    p, _ = sample(points, ns)
    x, y = p[:, 0], p[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def is_simple(points: Sequence[dict], ns: int = SAMPLES) -> bool:
    if len(points) < 2:
        return False
    p, _ = sample(points, ns)
    keep = np.r_[True, np.any(np.abs(np.diff(p, axis=0)) > 1e-12, axis=1)]
    p = p[keep]
    if len(p) < 3:
        return False
    try:
        return bool(shapely.LinearRing(p).is_simple)
    except (shapely.errors.GEOSException, ValueError):
        return False


# ----------------------------------------------------------------------------------------------
# repairs
# ----------------------------------------------------------------------------------------------
def merge_micro(points: List[dict], eps: float = MICRO, extent: Optional[float] = None) -> List[dict]:
    """Collapse runs of points joined by segments shorter than `eps` (control polygon length) into
    one point at their mean; the run's outer handles are kept (translated), so the neighbouring
    tangents - and a corner hidden in the run - survive as a proper corner point. A run never
    spans more than `extent` (default 1.5 eps): finely subdivided real detail (the round cap of a
    thin stroke) is thinned out, not collapsed."""
    n = len(points)
    if n < 2:
        return points
    extent = 1.5 * eps if extent is None else extent
    lens = [_ctrl_len(_seg(points, i)) for i in range(n)]
    if not any(L < eps for L in lens):
        return points
    kink = [_kink_deg(points, i) > KINK_DEG for i in range(n)]
    # debris = degenerate, or short AND next to a kink; a finely subdivided smooth curve (the
    # tangent-continuous quads of a stroker's round cap) is real geometry and stays
    micro = [L < DEGENERATE or (L < eps and (kink[i] or kink[(i + 1) % n])) for i, L in enumerate(lens)]
    if not any(micro) or all(micro):
        return points
    s = next(i for i in range(n) if not micro[i - 1])
    groups: List[List[int]] = []
    for k in range(n):
        i = (s + k) % n
        if k and micro[(i - 1) % n] and math.dist(points[groups[-1][0]]["co"], points[i]["co"]) < extent:
            groups[-1].append(i)
        else:
            groups.append([i])
    out = []
    for g in groups:
        if len(g) == 1:
            out.append(points[g[0]])
            continue
        c = np.mean([points[i]["co"] for i in g], axis=0)
        first, last = points[g[0]], points[g[-1]]
        dx0, dy0 = c[0] - first["co"][0], c[1] - first["co"][1]
        dx1, dy1 = c[0] - last["co"][0], c[1] - last["co"][1]
        out.append({"co": (float(c[0]), float(c[1])),
                    "hl": (first["hl"][0] + dx0, first["hl"][1] + dy0),
                    "hr": (last["hr"][0] + dx1, last["hr"][1] + dy1)})
    return out


def _unit(v) -> Optional[np.ndarray]:
    v = np.asarray(v, dtype=float)
    n = float(np.hypot(v[0], v[1]))
    return v / n if n > 1e-12 else None


def _kink_deg(points: Sequence[dict], i: int) -> float:
    """Tangent discontinuity at point i (degrees, 0 = smooth); 180 when a tangent is undefined."""
    tin, tout = _tangents(points, i)
    if tin is None or tout is None:
        return 180.0
    return math.degrees(math.acos(max(-1.0, min(1.0, float(np.dot(tin, tout))))))


def _tangents(points: Sequence[dict], i: int):
    n = len(points)
    a, b, c = points[i - 1], points[i], points[(i + 1) % n]
    tin = None
    for q in (b["hl"], a["hr"], a["co"]):
        tin = _unit(np.subtract(b["co"], q))
        if tin is not None:
            break
    tout = None
    for q in (b["hr"], c["hl"], c["co"]):
        tout = _unit(np.subtract(q, b["co"]))
        if tout is not None:
            break
    return tin, tout


def remove_spikes(points: List[dict], eps: float = MICRO) -> List[dict]:
    """Remove out-and-back spikes: a point where the outline reverses (> SPIKE_TURN_DEG) on short
    arms that enclose (almost) no area. A real sharp tip (crescent, star point) encloses area."""
    pts = list(points)
    guard = 0
    while len(pts) >= 3 and guard < 64:
        guard += 1
        n = len(pts)
        hit = None
        for i in range(n):
            tin, tout = _tangents(pts, i)
            if tin is None or tout is None:
                continue
            ang = math.degrees(math.acos(max(-1.0, min(1.0, float(np.dot(tin, tout))))))
            if ang < SPIKE_TURN_DEG:
                continue
            s1, s2 = _seg(pts, (i - 1) % n), _seg(pts, i)
            arm = max(_ctrl_len(s1), _ctrl_len(s2))
            if arm > SPIKE_MAX_LEN:
                continue
            lobe = sample([pts[i - 1], pts[i], pts[(i + 1) % n]][:3], SAMPLES)[0][: 2 * SAMPLES + 1]
            x, y = lobe[:, 0], lobe[:, 1]
            area = abs(0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))
            if area > SPIKE_AREA_RATIO * arm * arm:
                continue
            hit = i
            break
        if hit is None:
            break
        i = hit
        n = len(pts)
        a, c = pts[i - 1], pts[(i + 1) % n]
        if math.dist(a["co"], c["co"]) < eps:
            m = ((a["co"][0] + c["co"][0]) / 2, (a["co"][1] + c["co"][1]) / 2)
            merged = {"co": m, "hl": (a["hl"][0] + m[0] - a["co"][0], a["hl"][1] + m[1] - a["co"][1]),
                      "hr": (c["hr"][0] + m[0] - c["co"][0], c["hr"][1] + m[1] - c["co"][1])}
            drop = {i, (i + 1) % n}
            pts = [merged if k == (i - 1) % n else p for k, p in enumerate(pts) if k not in drop]
        else:
            a2 = dict(a, hr=(a["co"][0] + (c["co"][0] - a["co"][0]) / 3, a["co"][1] + (c["co"][1] - a["co"][1]) / 3))
            c2 = dict(c, hl=(a["co"][0] + 2 * (c["co"][0] - a["co"][0]) / 3,
                             a["co"][1] + 2 * (c["co"][1] - a["co"][1]) / 3))
            pts = [a2 if k == (i - 1) % n else c2 if k == (i + 1) % n else p
                   for k, p in enumerate(pts) if k != i]
    return pts


def _first_crossing(points: Sequence[dict]) -> Optional[Tuple[float, float, Pt]]:
    """First pair of crossing (non-adjacent) sample edges -> (param a, param b, point), a < b."""
    p, par = sample(points)
    m = len(p)
    if m < 4:
        return None
    nxt = np.roll(p, -1, axis=0)
    segs = [LineString([p[k], nxt[k]]) for k in range(m)]
    tree = STRtree(segs)
    ia, ib = tree.query(segs, predicate="intersects")
    best = None
    for a, b in zip(ia.tolist(), ib.tolist()):
        if a >= b or b - a == 1 or (a == 0 and b == m - 1):
            continue
        if best is None or (a, b) < best:
            best = (a, b)
    if best is None:
        return None
    a, b = best
    x = segs[a].intersection(segs[b])
    if x.is_empty:
        return None
    q = np.asarray(x.representative_point().coords[0])

    def param(k):
        d = nxt[k] - p[k]
        L2 = float(np.dot(d, d))
        u = float(np.clip(np.dot(q - p[k], d) / L2, 0.0, 1.0)) if L2 > 0 else 0.0
        step = (par[(k + 1) % m] - par[k]) % len(points) if m > 1 else 0.0
        return par[k] + u * step

    return param(a), param(b), (float(q[0]), float(q[1]))


def _pieces_between(points: Sequence[dict], pa: float, pb: float, x: Pt) -> List[tuple]:
    """Cubic pieces from parameter pa to pb (pa < pb, same orientation), endpoints snapped to x."""
    n = len(points)
    sa, ta = int(pa), pa - int(pa)
    sb, tb = int(pb), pb - int(pb)
    out = []
    if sa == sb:
        out.append(_sub(_seg(points, sa), ta, tb))
    else:
        out.append(_sub(_seg(points, sa), ta, 1.0))
        for s in range(sa + 1, sb):
            out.append(_seg(points, s % n))
        if tb > 1e-9:
            out.append(_sub(_seg(points, sb % n), 0.0, tb))
    out = [pc for pc in out if _ctrl_len(pc) > 1e-12]
    if not out:
        return out
    f, l = out[0], out[-1]
    out[0] = (x, f[1], f[2], f[3])
    out[-1] = (l[0], l[1], l[2], x) if len(out) > 1 else (x, f[1], f[2], x)
    return out


def repair_loops(points: List[dict], max_iter: int = 12) -> List[List[dict]]:
    """Cut self-intersections: at the first crossing the contour splits into two lobes; a lobe
    smaller than LOOP_KEEP_RATIO of the other (rounding debris) is dropped, otherwise both are
    kept as separate contours. Returns the resulting simple contours (possibly empty)."""
    todo = [points]
    done: List[List[dict]] = []
    it = 0
    while todo and it < max_iter:
        it += 1
        pts = todo.pop()
        if len(pts) < 1:
            continue
        cr = _first_crossing(pts) if len(pts) >= 2 else None
        if cr is None:
            done.append(pts)
            continue
        pa, pb, x = cr
        n = len(pts)
        inner = _pieces_between(pts, pa, pb, x)
        outer = _pieces_between(pts, pb, pa + n, x)
        lobes = [_from_pieces(pc) for pc in (outer, inner) if pc]
        areas = [abs(signed_area(lb)) for lb in lobes]
        if not lobes:
            continue
        big = max(areas)
        for lb, ar in zip(lobes, areas):
            if ar >= max(MIN_AREA, LOOP_KEEP_RATIO * big):
                todo.append(lb)
    done += todo
    return [d for d in done if is_simple(d)] if done else []


def _polygon_fallback(points: Sequence[dict]) -> List[List[dict]]:
    """Last resort: the sampled outline, made valid, as straight-segment contours."""
    p, _ = sample(points, 16)
    try:
        g = shapely.make_valid(Polygon(p))
    except (shapely.errors.GEOSException, ValueError):
        return []
    out = []
    for poly in getattr(g, "geoms", [g]):
        if poly.geom_type != "Polygon" or poly.is_empty or poly.area < MIN_AREA:
            continue
        ring = np.asarray(poly.exterior.coords)[:-1]
        if len(ring) < 3:
            continue
        pts = []
        for k in range(len(ring)):
            a, b, c = ring[k - 1], ring[k], ring[(k + 1) % len(ring)]
            pts.append({"co": (float(b[0]), float(b[1])),
                        "hl": (float(b[0] + (a[0] - b[0]) / 3), float(b[1] + (a[1] - b[1]) / 3)),
                        "hr": (float(b[0] + (c[0] - b[0]) / 3), float(b[1] + (c[1] - b[1]) / 3))})
        out.append(pts)
    return out


def ensure_three(points: List[dict]) -> List[dict]:
    """Split the longest segment(s) until the spline has >= 3 points (exact, de Casteljau)."""
    pts = list(points)
    while 0 < len(pts) < 3:
        n = len(pts)
        k = max(range(n), key=lambda i: _ctrl_len(_seg(pts, i)))
        left, right = _sub(_seg(pts, k), 0.0, 0.5), _sub(_seg(pts, k), 0.5, 1.0)
        a = dict(pts[k], hr=left[1])
        mid = {"co": left[3], "hl": left[2], "hr": right[1]}
        j = (k + 1) % n
        if n == 1:
            pts = [{"co": a["co"], "hl": right[2], "hr": left[1]}, mid]
            continue
        b = dict(pts[j], hl=right[2])
        new = list(pts)
        new[k] = a
        new[j] = b
        new.insert(k + 1, mid)
        pts = new
    return pts


def _rounded(points: Sequence[dict], nd: int = 6) -> List[dict]:
    def R(p):
        return (round(float(p[0]), nd) + 0.0, round(float(p[1]), nd) + 0.0)
    return [{"co": R(p["co"]), "hl": R(p["hl"]), "hr": R(p["hr"])} for p in points]


def clean_spline(points: List[dict], eps: float = MICRO) -> List[List[dict]]:
    """One closed spline -> zero or more clean, simple, closed splines with >= 3 points."""
    pts = merge_micro(list(points), eps)
    pts = remove_spikes(pts, eps)
    pts = merge_micro(pts, eps)
    if len(pts) < 1 or abs(signed_area(pts)) < MIN_AREA:
        return []
    pts = ensure_three(pts)
    parts = [pts] if is_simple(pts) else repair_loops(pts)
    if not parts and abs(signed_area(pts)) >= MIN_AREA:
        parts = _polygon_fallback(pts)
    out = []
    for q in parts:
        q = _rounded(ensure_three(q))
        if len(q) >= 3 and abs(signed_area(q)) >= MIN_AREA and is_simple(q):
            out.append(q)
    return out


def clean_splines(splines: List[dict], eps: float = MICRO) -> List[dict]:
    """Clean every spline dict ({closed, points, ...}); all results are closed."""
    out = []
    for s in splines:
        for q in clean_spline(s["points"], eps):
            out.append({**{k: v for k, v in s.items() if k != "points"}, "closed": True, "points": q})
    return out


def spline_issues(points: Sequence[dict], closed: bool = True) -> List[str]:
    """Invariant check used by the tests: [] when the spline is closed, has >= 3 points, a
    non-degenerate area and no self-intersection."""
    out = []
    if not closed:
        out.append("open")
    if len(points) < 3:
        out.append(f"{len(points)} points")
    if len(points) >= 1 and abs(signed_area(points)) < MIN_AREA:
        out.append("degenerate area")
    if len(points) >= 2 and not is_simple(points):
        out.append("self-intersection")
    return out
