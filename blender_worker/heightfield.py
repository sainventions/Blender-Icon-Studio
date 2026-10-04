"""Height-field bodies: the default geometry of EVERY piece, silhouette, raster contour, card and the plate
(PLAN §11 Geometry). Replaces the curve-bevel / GN-fallback / cap-verification machinery.

A body is a watertight, smooth solid over a 2D outline whose height only depends on the inward distance to
that outline. Thin parts simply taper, so tips and corners can never fold over or self-intersect (the curve
bevel offset every contour inward by the bevel radius: thin features inverted, star tips crossed over).

PROFILE (all lengths in the piece's LOCAL units: the art units of its splines)
------------------------------------------------------------------------------------------------------------
    d(p)     inward distance from a point p inside the piece to its outline (every contour: outers + holes)
    island   one outer contour together with its holes (even-odd nesting); D = max d over the island
             (its inradius). Every interior point belongs to the island of its nearest outline segment.
    t, b, k  thickness, bevel (round-edge radius; the scene passes b = min(bevel, t/2)), inflate (0..1)
    e        = max(t/2 − b, 0)                                   half height of the vertical side wall
    hb(d)    = sqrt(b² − (b − min(d, b))²)                        round edge: quarter circle of radius b
    dome(d)  = k · D · sqrt(1 − (1 − min(d/D, 1))²)               inflate: quarter ellipse D × k·D
    top      z(d) = e + hb(d) + dome(d)          bottom = −z(d) (mirrored)
    wall     vertical, from −e to +e along the outline (only when e > 0)
    The body is centred on its mid-plane z = 0; its half height is max z (= t/2 for an island with
    D ≥ b and k = 0; less for thin islands, which taper; t/2 + k·D with inflate — reached at the island's
    centre, which is a vertex whenever the profile still rises there, see SAMPLING 2).
    Normals: dz/dd = (b − d)/hb(d) [d < b] + k·(1 − d/D)/sqrt(1 − (1 − d/D)²) [d < D]: infinite at d = 0
    whenever b > 0 or k > 0, i.e. the tangent is VERTICAL at the rim (smooth with the wall / the mirrored
    bottom). Vertex normal n = normalize(−dz/dd · g, 1) with g = the softmin-weighted mean of the unit
    directions from the FOOT POINTS of p (outline segments whose distance is a local minimum along their
    ring; weights exp(−(dist − d)/κ), κ = max(0.0015 world units, 0.3·d); directions within 60° of the
    nearest one are averaged and normalised as one cluster, the rest as another, the two blended by weight).
    g is the exact unit gradient of d (round blobs: true sphere / dome normals) except near the medial axis
    where a second foot point is nearly as close (ridges of thin parts, mitres of sharp corners): there it
    shrinks toward 0, so ridge vertices get symmetric, more vertical normals (a softened crease). Outline
    vertices use the bisector of their two outline edges (horizontal normals when the tangent is vertical).
    A vertex normal is finally blended toward +Z just enough to face all its top faces (dot ≥ 0.05).
    b = 0 and k = 0: flat top and bottom, vertical wall, sharp (split-normal) rim edges.

SAMPLING (tolerances are WORLD units; local = world / scale, scale = art scale × layer scale; the constants
are the module tunables below: CHORD_TOL 0.0006, MAX_EDGE 0.04, MERGE_EPS 1e-5, CORNER_DEG 30, GUARD 0.002..0.008,
FAN_DEG 15, RING_TOL 0.04, TAN_K 0.006, TAN 0.004..0.04, MERGE_Q 2e-6, NEAR 0.01)
------------------------------------------------------------------------------------------------------------
 1. Outline: every bezier segment is flattened with n = max(ceil(sqrt(0.75·L/CHORD_TOL)), ceil(len/MAX_EDGE))
    uniform-t samples (Wang's bound: L = max |P0 − 2C1 + C2|, |C1 − 2C2 + P1|; len = control-polygon length),
    n ≤ 256. Open splines are closed with a straight segment (fills). Points closer than MERGE_EPS are merged,
    zero-width spikes / slits (turn > ~155° folding back within 20·MERGE_EPS of itself) removed, rings with
    < 3 points or |area| < MIN_AREA dropped. Corners turning more than CORNER_DEG get GUARD points on both
    adjacent edges at g = clamp(0.5·b, GUARD_MIN, GUARD_MAX) from the corner (edges longer than 3g), which
    confines the corner's bisector normal to g. Rings are oriented outer CCW / hole CW (material on the left)
    by even-odd nesting; a hole belongs to the island of its smallest enclosing outer ring. Distance queries
    use the SHAPE rings (the same polylines without collinear points) and re-measure points closer than
    NEAR (or 16·CHORD_TOL) on the sample rings themselves.
 2. Island inradius D: max d over a 40 × 40 scanline raster of the piece's bbox, refined by 12 steps of
    pattern search from each island's best cell (islands the raster misses: 0.25 × the ring's smaller bbox
    side); later raised to the largest d measured at a mesh vertex. Ring distances per island (graded where
    the profile is steep): bevel rings d = b·(1 − cos(jπ/2K)), j = 1..K, K = clamp(bevelSegments, 2, 16)
    (uniform in angle along the quarter circle); with inflate also dome rings d = D·(1 − cos(jπ/2J)),
    j = 1..J−1, J = clamp(bevelSegments, 3, 12). Distances ≥ 0.999·D are dropped, and so is a ring closer to
    its inner neighbour than 0.25 × the smaller neighbouring gap. Beyond the last ring the profile is flat
    (k = 0) or nearly so: no interior grid is needed — except the island's APEX (its best inradius probe),
    added whenever the profile still rises there (inflate, or D < b: a bevelled sphere / lens) and no Steiner
    point lies within APEX_CLEAR × (D − last ring distance) of it: otherwise a dome / sphere head ended in a flat
    cap at the last ring (radius 0.26·D, 3.4 % low at 6 segments; 0.5·D, 13 % low at 3).
 3. Ring candidates: from every outline vertex v with turn angle α (left/convex > 0) along its inward
    bisector: convex v at t = d_j / max(cos(α/2), 0.1) (the mitre point at true distance d_j), reflex v at
    t = d_j, plus a FAN at reflex vertices sharper than FAN_DEG: directions rotated from the incoming to the
    outgoing edge normal in ≤ FAN_DEG steps, at t = d_j. A candidate is kept when its true distance is
    ≥ (1 − RING_TOL)·d_j; the first one that fails ends its ray and adds a MEDIAL point halfway between it and
    the last valid one (or v) — the ray crossed the medial axis (ridges of thin parts / tip spines). Ring j's
    points are thinned on a grid of cell max(0.45·min(gap_j, MAX_EDGE), 0.8·s_j) with the along-ring spacing
    s_j = clamp(TAN_K / sqrt(dz/dd(d_j)), TAN_MIN, TAN_MAX) (z is constant along a ring; a chord's sag only
    matters in proportion to the slope). Medial points are thinned on a grid of 2 × their median radius
    0.35·min(local gap, MAX_EDGE), dropped within that radius of a kept ring point and when their true
    distance < 0.25·d_1 (the ray left the piece). All Steiner points within ~MERGE_Q of an earlier point or an
    outline vertex are dropped (nearly coincident CDT inputs make slivers).
 4. Triangulation: mathutils.geometry.delaunay_2d_cdt(outline vertices + Steiner points, faces = every ring
    (CCW), output_type 1 'inside', epsilon 1e-7); a triangle is kept when it lies inside an ODD number of
    rings (even-odd holes). Any interior edge joining two outline vertices (a chord across a thin part, which
    would pinch the body to the wall height) gets its midpoint inserted and the CDT is redone (≤ 4 passes);
    a triangle with three outline vertices and no such chord gets its centroid.
 5. Mesh: top = every vertex at z(d) with the D of its nearest segment's island; bottom = mirrored copy
    (outline vertices shared when e = 0); wall quads along every outline edge when e > 0; consistent CCW
    winding (outward normals). Per-corner normals: the vertex normals above; a rim vertex whose normal cannot
    face all its faces (cusp / very sharp corner: a cone apex) gets split normals (each face's corner = the
    mean of its other two corners) and its edges marked sharp. Stored as Blender 5.0 "free" custom normals
    (FLOAT_VECTOR corner attribute 'custom_normal'), all faces smooth; flat rims (b = k = 0) are sharp edges,
    and so are their vertical wall edges at corners turning more than CORNER_DEG (each wall quad's own normal).
    Watertight and 2-manifold by construction: the top is a graph over a planar triangulation, z ≥ 0 ≥ −z,
    equality only on the shared outline.

Cached by (piece key, thickness, bevel, inflate, segments, scale) → mesh datablock (LRU). Pure numpy except
:func:`triangulate` (mathutils) and :func:`to_mesh` / :func:`check_mesh` (bpy), imported lazily.
"""
from __future__ import annotations

import math
import time
from collections import OrderedDict
from typing import Callable, Optional, Sequence

import numpy as np

# ------------------------------------------------------------------------------------------------
# tunables (WORLD units unless noted)
# ------------------------------------------------------------------------------------------------
CHORD_TOL = 0.0006        # max outline chord error (≈ 0.15 px at 512 px for a ±1 icon)
MAX_EDGE = 0.04           # max outline sample spacing (z only varies across rings: straight runs need few)
MERGE_EPS = 1e-5          # outline points closer than this are merged
MERGE_Q = 2e-6            # Steiner points closer than about this to another point are dropped
MIN_AREA = 1e-8           # rings with a smaller |area| are dropped
CORNER_DEG = 30.0         # corners turning more than this get guard points
GUARD_MIN, GUARD_MAX = 0.002, 0.008
FAN_DEG = 15.0            # reflex corner fan step (degrees)
RING_TOL = 0.04           # a ring candidate is valid when its true distance ≥ (1 − RING_TOL) × its ring distance
KAPPA = 0.0015            # softmin width of the normal's inward direction ...
KAPPA_REL = 0.3           # ... at least this fraction of the distance (ridges soften in proportion to their depth)
SOFT_REACH = 4.0          # segments farther than d + SOFT_REACH·κ are ignored by the softmin (weight < 2 %)
NEAR = 0.01               # points closer to the outline than this are measured on the sample outline
TAN_K = 0.006             # ring point spacing along a ring = TAN_K / sqrt(profile slope there) ...
TAN_MIN, TAN_MAX = 0.004, 0.04   # ... clamped (z is constant along a ring: flat rings need few points)
NORMAL_MIN_DOT = 0.05     # a vertex normal must face every adjacent top face at least this much
APEX_CLEAR = 0.3          # the island apex is skipped when a Steiner point is closer than this × (D − last ring)
MIN_THICKNESS = 1e-4
CHORD_PASSES = 4
CACHE_LIMIT = 200
VERSION = 2               # bump when the geometry changes (cache key)

_MESH_CACHE: "OrderedDict[str, str]" = OrderedDict()     # key -> mesh name
_INRADIUS_CACHE: "OrderedDict[str, list]" = OrderedDict()


# ================================================================================================
# profile
# ================================================================================================
def wall_half(thickness: float, bevel: float) -> float:
    """e = max(t/2 − b, 0)."""
    return max(float(thickness) / 2.0 - float(bevel), 0.0)


def profile(d, thickness: float, bevel: float, inflate: float, D) -> np.ndarray:
    """Top height z(d) = e + hb(d) + inflate·D·sqrt(1 − (1 − min(d/D, 1))²) (mirrored for the bottom)."""
    d = np.maximum(np.asarray(d, dtype=np.float64), 0.0)
    b = max(0.0, float(bevel))
    z = np.full(d.shape, wall_half(thickness, b))
    if b > 0:
        db = np.minimum(d, b)
        z = z + np.sqrt(np.maximum(b * b - (b - db) ** 2, 0.0))
    k = max(0.0, float(inflate))
    if k > 0:
        Dv = np.maximum(np.asarray(D, dtype=np.float64), 1e-12)
        u = np.minimum(d / Dv, 1.0)
        z = z + k * Dv * np.sqrt(np.maximum(1.0 - (1.0 - u) ** 2, 0.0))
    return z


def slope(d, bevel: float, inflate: float, D) -> np.ndarray:
    """dz/dd of :func:`profile` (inf at d = 0 when bevel > 0 or inflate > 0)."""
    d = np.maximum(np.asarray(d, dtype=np.float64), 0.0)
    b = max(0.0, float(bevel))
    s = np.zeros(d.shape)
    with np.errstate(divide="ignore", invalid="ignore"):
        if b > 0:
            hb = np.sqrt(np.maximum(b * b - (b - np.minimum(d, b)) ** 2, 0.0))
            sb = np.where(d < b, np.where(hb > 0, (b - d) / hb, np.inf), 0.0)
            s = s + sb
        k = max(0.0, float(inflate))
        if k > 0:
            Dv = np.maximum(np.asarray(D, dtype=np.float64), 1e-12)
            u = np.minimum(d / Dv, 1.0)
            r = np.sqrt(np.maximum(1.0 - (1.0 - u) ** 2, 0.0))
            sd = np.where(u < 1.0, np.where(r > 0, k * (1.0 - u) / r, np.inf), 0.0)
            s = s + sd
    return s


def half_height(thickness: float, bevel: float, inflate: float, D: float) -> float:
    """Max top height of an island with inradius D (= its half height)."""
    return float(profile(np.array([max(D, 0.0)]), thickness, bevel, inflate, max(D, 1e-12))[0])


def ring_distances(bevel: float, inflate: float, D: float, segments: int) -> np.ndarray:
    """Inward distances of the graded offset rings of one island (sorted, 0 < d < D)."""
    out = []
    b = max(0.0, float(bevel))
    if b > 0:
        K = int(max(2, min(16, segments)))
        out += [b * (1.0 - math.cos(j * math.pi / (2 * K))) for j in range(1, K + 1)]
    if inflate > 0 and D > 0:
        J = int(max(3, min(12, segments)))
        out += [D * (1.0 - math.cos(j * math.pi / (2 * J))) for j in range(1, J)]
    ds = np.array(sorted(x for x in out if 0.0 < x < D * 0.999), dtype=np.float64)
    if len(ds) < 2:
        return ds
    keep = [ds[0]]
    for x in ds[1:]:
        if x - keep[-1] > 1e-12:
            keep.append(x)
    ds = np.array(keep)
    # merge rings much closer than their neighbours' gaps (bevel and dome rings interleave)
    while len(ds) > 2:
        gaps = np.diff(np.concatenate([[0.0], ds]))
        nb = np.minimum(np.concatenate([gaps[1:], [np.inf]]), np.concatenate([[np.inf], gaps[:-1]]))
        bad = np.nonzero(gaps[1:] < 0.25 * nb[1:])[0]
        if not len(bad):
            break
        ds = np.delete(ds, bad[0] + 1)
    return ds


# ================================================================================================
# outline
# ================================================================================================
def _spline_arrays(s: dict):
    pts = s.get("points") or []
    co = np.array([p["co"][:2] for p in pts], dtype=np.float64).reshape(-1, 2)
    hl = np.array([(p.get("hl") or p["co"])[:2] for p in pts], dtype=np.float64).reshape(-1, 2)
    hr = np.array([(p.get("hr") or p["co"])[:2] for p in pts], dtype=np.float64).reshape(-1, 2)
    return co, hl, hr


def flatten_spline(s: dict, tol: float, max_edge: Optional[float]) -> np.ndarray:
    """Adaptive polyline of one bezier spline (closed ring, no repeated end point). ``max_edge`` None: chord
    tolerance only (the SHAPE polyline used for distances)."""
    co, hl, hr = _spline_arrays(s)
    m = len(co)
    if m < 2:
        return co
    closed = bool(s.get("closed", True))
    nseg = m if closed else m - 1
    i = np.arange(nseg)
    j = (i + 1) % m
    P0, C1, C2, P1 = co[i], hr[i], hl[j], co[j]
    L = np.maximum(np.hypot(*(P0 - 2 * C1 + C2).T), np.hypot(*(C1 - 2 * C2 + P1).T))
    n_curv = np.ceil(np.sqrt(0.75 * L / max(tol, 1e-12)))
    plen = (np.hypot(*(C1 - P0).T) + np.hypot(*(C2 - C1).T) + np.hypot(*(P1 - C2).T))
    n_len = np.ceil(plen / max(max_edge, 1e-12)) if max_edge else 1
    n = np.clip(np.maximum(n_curv, n_len), 1, 256).astype(np.int64)
    seg = np.repeat(np.arange(nseg), n)
    t = (np.arange(int(n.sum())) - np.repeat(np.cumsum(n) - n, n)) / np.repeat(n, n)
    t = t[:, None]
    mt = 1.0 - t
    pts = (mt ** 3 * P0[seg] + 3 * mt * mt * t * C1[seg] + 3 * mt * t * t * C2[seg] + t ** 3 * P1[seg])
    if not closed:
        pts = np.vstack([pts, co[-1:]])
    return pts


def _area(r: np.ndarray) -> float:
    x, y = r[:, 0], r[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y))


def _is_spike(a, v, c, w_eps: float) -> bool:
    """v is the tip of a zero-width spike / slit: the outline folds back (turn > ~155°) and both neighbours lie
    within ``w_eps`` of the other edge's line."""
    ux, uy = v[0] - a[0], v[1] - a[1]
    wx, wy = c[0] - v[0], c[1] - v[1]
    lu, lw = math.hypot(ux, uy), math.hypot(wx, wy)
    if lu < 1e-300 or lw < 1e-300:
        return True
    if (ux * wx + uy * wy) / (lu * lw) > -0.9:
        return False
    cr = abs(ux * wy - uy * wx)
    return cr / lu < w_eps or cr / lw < w_eps


def clean_ring(r: np.ndarray, eps: float, w_eps: Optional[float] = None) -> np.ndarray:
    """Merge near-duplicate consecutive points (incl. the wrap) and remove zero-width spikes and slits (an outline
    that runs out and back along itself, narrower than ``w_eps`` = 20·eps: boolean-union seams). A slit is
    peeled from its tip inward (stack), so it goes entirely however many points it has."""
    w_eps = 20.0 * eps if w_eps is None else w_eps
    if len(r) < 3:
        return r
    d = np.hypot(*(r - np.roll(r, 1, axis=0)).T)
    keep = d > eps
    if not keep.any():
        return r[:1]
    r = r[keep]
    n = len(r)
    if n < 3:
        return r
    a = r - np.roll(r, 1, axis=0)
    b = np.roll(r, -1, axis=0) - r
    cosang = (a * b).sum(1) / np.maximum(np.hypot(*a.T) * np.hypot(*b.T), 1e-300)
    if not (cosang < -0.9).any():
        return r
    pts = [tuple(p) for p in r]
    for _ in range(2):                       # second pass: spikes across the wrap
        out: list = []
        for p in pts:
            if out and math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) <= eps:
                continue
            out.append(p)
            while len(out) >= 3 and _is_spike(out[-3], out[-2], out[-1], w_eps):
                out.pop(-2)
                if len(out) >= 2 and math.hypot(out[-1][0] - out[-2][0], out[-1][1] - out[-2][1]) <= eps:
                    out.pop()
        if len(out) < 3:
            return np.asarray(out).reshape(-1, 2)
        pts = out[len(out) // 2:] + out[:len(out) // 2]
    while len(pts) >= 3 and _is_spike(pts[-2], pts[-1], pts[0], w_eps):
        pts.pop()
    return np.asarray(pts)


def guard_ring(r: np.ndarray, dist: float, min_turn_deg: float = CORNER_DEG) -> np.ndarray:
    """Insert guard points at ``dist`` from every corner sharper than ``min_turn_deg`` on both adjacent edges
    (edges longer than 3·dist). The outline is unchanged."""
    n = len(r)
    if n < 3 or dist <= 0:
        return r
    a = r - np.roll(r, 1, axis=0)                 # incoming edge (prev -> i)
    b = np.roll(r, -1, axis=0) - r                # outgoing edge (i -> next)
    la, lb = np.hypot(*a.T), np.hypot(*b.T)
    cosang = (a * b).sum(1) / np.maximum(la * lb, 1e-300)
    sharp = cosang < math.cos(math.radians(min_turn_deg))
    if not sharp.any():
        return r
    out = []
    for i in range(n):
        p, q = r[i], r[(i + 1) % n]
        out.append(p)
        L = lb[i]
        if L <= 3 * dist:
            continue
        u = (q - p) / L
        if sharp[i]:
            out.append(p + u * dist)
        if sharp[(i + 1) % n]:
            out.append(q - u * dist)
    return np.asarray(out)


def _simplify(r: np.ndarray, tol: float) -> np.ndarray:
    """The SHAPE ring of a sample ring: the same polyline without its (nearly) collinear points (straight runs
    subdivided by MAX_EDGE), so it describes exactly the same outline with fewer segments."""
    out = r
    for it in range(16):
        n = len(out)
        if n <= 4:
            return out
        a, c = np.roll(out, 1, axis=0), np.roll(out, -1, axis=0)
        ac = c - a
        lac = np.maximum(np.hypot(ac[:, 0], ac[:, 1]), 1e-300)
        dev = np.abs((out[:, 0] - a[:, 0]) * ac[:, 1] - (out[:, 1] - a[:, 1]) * ac[:, 0]) / lac
        t = ((out - a) * ac).sum(1) / (lac * lac)
        rem = (dev < tol) & (t > 0.0) & (t < 1.0)
        if not rem.any():
            return out
        rem &= (np.arange(n) % 2) == it % 2   # never two neighbours at once (each removal re-checks next pass)
        if n % 2 == 1:
            rem[-1] = False                   # (index n-1 and 0 are neighbours)
        if not rem.any():
            continue
        out = out[~rem]
    return out


def _point_in_ring(P: np.ndarray, r: np.ndarray) -> np.ndarray:
    """Even-odd point-in-polygon of points P (N, 2) against one ring."""
    x0, y0 = r[:, 0], r[:, 1]
    x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
    px, py = P[:, 0][:, None], P[:, 1][:, None]
    cond = (y0 > py) != (y1 > py)
    with np.errstate(divide="ignore", invalid="ignore"):
        xi = x0 + (py - y0) * (x1 - x0) / np.where(np.abs(y1 - y0) < 1e-300, 1e-300, y1 - y0)
    return (np.count_nonzero(cond & (px < xi), axis=1) % 2) == 1


def organise(rings: list) -> tuple[list, np.ndarray, np.ndarray]:
    """Even-odd nesting of rings -> (rings oriented outer CCW / hole CW, hole flags, island id per ring)."""
    R = len(rings)
    bb = np.array([[r[:, 0].min(), r[:, 1].min(), r[:, 0].max(), r[:, 1].max()] for r in rings]).reshape(-1, 4)
    areas = np.array([_area(r) for r in rings])
    contains = np.zeros((R, R), dtype=bool)       # contains[j, i]: ring j contains ring i
    for i in range(R):
        ri = rings[i]
        probes = ri[[0, len(ri) // 3, (2 * len(ri)) // 3]]
        for j in range(R):
            if j == i or abs(areas[j]) < abs(areas[i]):
                continue
            if bb[j, 0] > bb[i, 0] or bb[j, 1] > bb[i, 1] or bb[j, 2] < bb[i, 2] or bb[j, 3] < bb[i, 3]:
                continue
            contains[j, i] = int(_point_in_ring(probes, rings[j]).sum()) >= 2
    depth = contains.sum(axis=0)
    hole = (depth % 2) == 1
    island = np.arange(R)
    for i in range(R):
        if hole[i]:
            par = [j for j in range(R) if contains[j, i] and depth[j] == depth[i] - 1]
            if par:
                island[i] = min(par, key=lambda j: abs(areas[j]))
    out = []
    for i, r in enumerate(rings):
        ccw = areas[i] > 0
        out.append(r if ccw != bool(hole[i]) else r[::-1].copy())
    return out, hole, island


def outline(splines: list, tol: float, max_edge: float, eps: float, guard: float = 0.0):
    """Splines -> (oriented sample rings, oriented SHAPE rings, hole flags, island per ring).

    Sample rings (chord tolerance + ``max_edge`` + corner guards) are the mesh outline and the sources of the
    ring points; shape rings (chord tolerance only: no collinear subdivisions) describe the same outline with
    fewer segments and serve the distance queries of deep points. Degenerate rings are dropped."""
    rings, shapes = [], []
    for s in splines or []:
        if len(s.get("points") or []) < 2:
            continue
        r = clean_ring(flatten_spline(s, tol, max_edge), eps)
        if len(r) < 3 or abs(_area(r)) < MIN_AREA:
            continue
        c = _simplify(r, 0.05 * tol)
        if guard > 0:
            r = guard_ring(r, guard)
        rings.append(r)
        shapes.append(c)
    if not rings:
        return [], [], np.zeros(0, bool), np.zeros(0, int)
    oriented, hole, island = organise(rings)
    shapes = [c if (_area(c) > 0) == (_area(o) > 0) else c[::-1].copy() for c, o in zip(shapes, oriented)]
    return oriented, shapes, hole, island


# ================================================================================================
# distances
# ================================================================================================
def _segments(rings: list):
    A = np.vstack(rings)
    B = np.vstack([np.roll(r, -1, axis=0) for r in rings])
    ring_of = np.concatenate([np.full(len(r), k) for k, r in enumerate(rings)])
    return A, B, ring_of


def _ring_neighbours(rings: list) -> tuple[np.ndarray, np.ndarray]:
    """(previous, next) segment index of every segment of :func:`_segments` along its own ring."""
    idx = [np.arange(len(r)) + s for r, s in zip(rings, np.cumsum([0] + [len(r) for r in rings[:-1]]))]
    return (np.concatenate([np.roll(i, 1) for i in idx]).astype(np.int64),
            np.concatenate([np.roll(i, -1) for i in idx]).astype(np.int64))


def nearest(P: np.ndarray, A: np.ndarray, B: np.ndarray, kappa: float = 0.0, chunk: int = 384, rel: float = 0.0,
            nbr: Optional[tuple] = None):
    """Distance of points P (N, 2) to segments A→B (M, 2) -> (d (N,), segment index (N,), g (N, 2)).

    ``g`` is the unit direction from the nearest outline point to p (inward for interior points); with
    ``kappa`` > 0 it is the softmin-weighted mean over the FOOT POINTS (weights exp(−(dist − d)/κ) with
    κ = max(kappa, rel·d)), which shrinks toward 0 on the medial axis. With ``nbr`` = (previous, next) segment
    index along each ring (:func:`_ring_neighbours`) only segments whose distance is a local minimum along their
    ring count as foot points: on a ridge (a second, nearly as close foot point across a thin part / a corner
    bisector) g blends both sides, but over a round blob (a disc's centre: one foot point, the rest of the
    outline merely a little farther) g stays the exact unit gradient of d — a sphere / dome keeps true normals.
    Chunks of spatially sorted points only test the segments inside their bounding box grown by an upper bound
    of their distances (exact)."""
    P = np.asarray(P, dtype=np.float64).reshape(-1, 2)
    N, M = len(P), len(A)
    d = np.zeros(N)
    seg = np.zeros(N, dtype=np.int64)
    g = np.zeros((N, 2))
    if N == 0 or M == 0:
        return d, seg, g
    # float32 in the hot loop (coordinates ~1, distances needed to ~1e-6): half the memory traffic
    f32 = np.float32
    ax_, ay_ = A[:, 0].astype(f32), A[:, 1].astype(f32)
    abx, aby = (B[:, 0] - A[:, 0]).astype(f32), (B[:, 1] - A[:, 1]).astype(f32)
    L2 = (B[:, 0] - A[:, 0]) ** 2 + (B[:, 1] - A[:, 1]) ** 2
    inv = np.where(L2 > 1e-30, 1.0 / np.where(L2 > 1e-30, L2, 1.0), 0.0).astype(f32)
    sminx, smaxx = np.minimum(A[:, 0], B[:, 0]), np.maximum(A[:, 0], B[:, 0])
    sminy, smaxy = np.minimum(A[:, 1], B[:, 1]), np.maximum(A[:, 1], B[:, 1])
    ext = float(max(np.ptp(A[:, 0]), np.ptp(A[:, 1]), 1e-9))
    cell = ext / 24.0
    kx = np.floor((P[:, 0] - A[:, 0].min()) / cell).astype(np.int64)
    ky = np.floor((P[:, 1] - A[:, 1].min()) / cell).astype(np.int64)
    order = np.lexsort((np.where(kx % 2 == 0, ky, -ky), kx))
    stride = max(1, M // 128)
    Sx, Sy = ax_[::stride], ay_[::stride]
    pos = np.full(M, -1, dtype=np.int64) if (kappa > 0 and nbr is not None) else None
    for c0 in range(0, N, chunk):
        idx = order[c0:c0 + chunk]
        px, py = P[idx, 0][:, None].astype(f32), P[idx, 1][:, None].astype(f32)
        ubp = float(np.sqrt(((px - Sx) ** 2 + (py - Sy) ** 2).min(axis=1)).max())
        ub = ubp + SOFT_REACH * max(kappa, rel * ubp) + 1e-6 if kappa > 0 else ubp + 1e-6
        x0, x1 = float(px.min()) - ub, float(px.max()) + ub
        y0, y1 = float(py.min()) - ub, float(py.max()) + ub
        sel = np.nonzero((smaxx >= x0) & (sminx <= x1) & (smaxy >= y0) & (sminy <= y1))[0]
        if len(sel) == 0:
            sel = np.arange(M)
        sx, sy, bx, by = ax_[sel], ay_[sel], abx[sel], aby[sel]
        apx, apy = px - sx, py - sy
        t = np.clip((apx * bx + apy * by) * inv[sel], 0.0, 1.0)
        dx = apx - t * bx
        dy = apy - t * by
        dist = np.sqrt(dx * dx + dy * dy)
        j = dist.argmin(axis=1)
        rows = np.arange(len(idx))
        dmin = dist[rows, j]
        d[idx] = dmin
        seg[idx] = sel[j]
        if kappa > 0:
            # two-cluster softmin: unit directions from segments on the nearest one's side (within 60°) and from
            # the other side(s) are averaged separately and normalised, then blended by their total weights —
            # full-length g on one side, the symmetric mean on a ridge / mitre (|g| = cos(half the angle))
            kap = np.maximum(kappa, rel * dmin)[:, None]
            w = np.exp(-(dist - dmin[:, None]) / kap)
            if pos is not None:
                # foot points only: a segment's distance is a local minimum along its ring (segments culled by
                # the box are farther than every selected one)
                pos[sel] = np.arange(len(sel))
                pp, pn = pos[nbr[0][sel]], pos[nbr[1][sel]]
                big = np.float32(np.inf)
                dprev = np.where(pp >= 0, dist[:, np.maximum(pp, 0)], big)
                dnext = np.where(pn >= 0, dist[:, np.maximum(pn, 0)], big)
                tie = np.float32(1e-6)
                w = np.where((dist <= dprev + tie) & (dist <= dnext + tie), w, 0.0)
                pos[sel] = -1
            inv_d = 1.0 / np.maximum(dist, 1e-30)
            ux, uy = dx * inv_d, dy * inv_d
            dm = np.maximum(dmin, 1e-30)
            same = (ux * (dx[rows, j] / dm)[:, None] + uy * (dy[rows, j] / dm)[:, None]) > 0.5
            w[dist <= 1e-30] = 0.0
            ws_, wo_ = np.where(same, w, 0.0), np.where(same, 0.0, w)
            Ws, Wo = ws_.sum(1), wo_.sum(1)
            sx, sy = (ws_ * ux).sum(1), (ws_ * uy).sum(1)
            ox, oy = (wo_ * ux).sum(1), (wo_ * uy).sum(1)
            sn = np.maximum(np.hypot(sx, sy), 1e-30)
            on = np.maximum(np.hypot(ox, oy), 1e-30)
            tot = np.maximum(Ws + Wo, 1e-30)
            g[idx, 0] = (Ws * sx / sn + Wo * ox / on) / tot
            g[idx, 1] = (Ws * sy / sn + Wo * oy / on) / tot
        else:
            dm = np.maximum(dmin, 1e-30)
            g[idx, 0], g[idx, 1] = dx[rows, j] / dm, dy[rows, j] / dm
    return d, seg, g


def _scan_inside(rings: list, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Even-odd raster (len(ys), len(xs)) of closed rings: one scanline per row."""
    A, B, _ = _segments(rings)
    xa, ya, xb, yb = A[:, 0], A[:, 1], B[:, 0], B[:, 1]
    out = np.zeros((len(ys), len(xs)), dtype=bool)
    for j, y in enumerate(ys):
        c = (ya > y) != (yb > y)
        if not c.any():
            continue
        xc = xa[c] + (y - ya[c]) * (xb[c] - xa[c]) / (yb[c] - ya[c])
        xc.sort()
        out[j] = (np.searchsorted(xc, xs) % 2) == 1
    return out


class Outline:
    """Distance queries against a piece outline: deep points use the SHAPE rings (few segments), points
    closer than ``near`` are re-measured on the SAMPLE rings (the mesh outline itself)."""

    def __init__(self, rings: list, shapes: list, island: np.ndarray, near: float):
        self.rings, self.shapes, self.island, self.near = rings, shapes, island, near
        self.A, self.B, self.ring_of = _segments(rings)
        self.Ac, self.Bc, self.ring_of_c = _segments(shapes)
        self.nbr, self.nbr_c = _ring_neighbours(rings), _ring_neighbours(shapes)

    def query(self, P: np.ndarray, kappa: float = 0.0, rel: float = 0.0):
        """-> (d, island id, g) of points P."""
        P = np.asarray(P, dtype=np.float64).reshape(-1, 2)
        d, sg, g = nearest(P, self.Ac, self.Bc, kappa, rel=rel, nbr=self.nbr_c)
        isl = self.island[self.ring_of_c[sg]] if len(P) else np.zeros(0, dtype=np.int64)
        m = d < self.near
        if m.any():
            d2, sg2, g2 = nearest(P[m], self.A, self.B, kappa, rel=rel, nbr=self.nbr)
            d[m], g[m], isl[m] = d2, g2, self.island[self.ring_of[sg2]]
        return d, isl, g


def island_inradius(ol: Outline, grid: int = 40, centres: Optional[dict] = None) -> np.ndarray:
    """Estimated inradius D per ring's island (indexed by ring; holes carry their island's value). ``centres``
    (a dict) receives {island: (x, y)} — the centre of its largest inscribed disc found (the apex)."""
    rings, island = ol.shapes, ol.island
    D = np.zeros(len(rings))
    if not rings:
        return D
    allr = np.vstack(rings)
    (x0, y0), (x1, y1) = allr.min(axis=0), allr.max(axis=0)
    h = max(x1 - x0, y1 - y0) / grid
    if h <= 0:
        return D
    xs = np.arange(x0 + h / 2, x1, h)
    ys = np.arange(y0 + h / 2, y1, h)
    ins = _scan_inside(rings, xs, ys)
    X, Y = np.meshgrid(xs, ys)
    P = np.column_stack([X[ins], Y[ins]])
    if len(P):
        d, sg, _ = nearest(P, ol.Ac, ol.Bc)
        isl = island[ol.ring_of_c[sg]]
        order = np.argsort(-d)
        for k in np.unique(isl):
            D[k] = max(D[k], float(d[isl == k].max()))
        # refine the best probe of each island: a few steps of gradient ascent on d (the grid is coarse)
        for k in np.unique(isl):
            p = P[order[isl[order] == k][0]].copy()
            step = 0.5 * h
            best = D[k]
            for _ in range(12):
                cand = p + step * np.array([(1, 0), (-1, 0), (0, 1), (0, -1), (0.7, 0.7), (-0.7, 0.7),
                                            (0.7, -0.7), (-0.7, -0.7)])
                dc, sc_, _ = nearest(cand, ol.Ac, ol.Bc)
                ok = island[ol.ring_of_c[sc_]] == k
                j = int(np.argmax(np.where(ok, dc, -1.0)))
                if ok[j] and dc[j] > best:
                    best, p = float(dc[j]), cand[j]
                else:
                    step *= 0.5
            D[k] = max(D[k], best)
            if centres is not None:
                centres[int(k)] = (float(p[0]), float(p[1]))
    # tiny islands the grid missed: a lower bound from the ring's own size
    for k in range(len(rings)):
        if island[k] == k and D[k] <= 0:
            r = rings[k]
            D[k] = 0.25 * min(np.ptp(r[:, 0]), np.ptp(r[:, 1]))
    return D[island]


def inradius(splines: list, scale: float = 1.0, key: Optional[str] = None) -> float:
    """Largest island inradius of a piece (local units; cached by ``key``) — framing / lift estimates."""
    if key is not None:
        hit = _INRADIUS_CACHE.get(key)
        if hit is not None:
            return hit[0]
    sc = max(float(scale), 1e-9)
    rings, shapes, _hole, island = outline(splines, CHORD_TOL / sc, MAX_EDGE / sc, MERGE_EPS / sc)
    D = float(island_inradius(Outline(rings, shapes, island, 0.0)).max()) if rings else 0.0
    if key is not None:
        _INRADIUS_CACHE[key] = [D]
        while len(_INRADIUS_CACHE) > 4 * CACHE_LIMIT:
            _INRADIUS_CACHE.popitem(last=False)
    return D


# ================================================================================================
# Steiner points
# ================================================================================================
def _unit(v: np.ndarray) -> np.ndarray:
    n = np.hypot(v[:, 0], v[:, 1])
    return v / np.maximum(n, 1e-300)[:, None]


def _rot(v: np.ndarray, ang: np.ndarray) -> np.ndarray:
    c, s = np.cos(ang), np.sin(ang)
    return np.column_stack([c * v[:, 0] - s * v[:, 1], s * v[:, 0] + c * v[:, 1]])


def _near_any(P: np.ndarray, Q: np.ndarray, rad: np.ndarray) -> np.ndarray:
    """For each point of P: is some point of Q closer than its ``rad``? (uniform grid hash of Q, cell = max rad)"""
    cell = max(float(rad.max()), 1e-12)
    qk = np.floor(Q / cell).astype(np.int64)
    order = np.lexsort((qk[:, 1], qk[:, 0]))
    qk, Qs = qk[order], Q[order]
    keys = qk[:, 0] * 4294967296 + qk[:, 1]
    pk = np.floor(P / cell).astype(np.int64)
    hit = np.zeros(len(P), dtype=bool)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            kk = (pk[:, 0] + dx) * 4294967296 + (pk[:, 1] + dy)
            lo = np.searchsorted(keys, kk, side="left")
            hi = np.searchsorted(keys, kk, side="right")
            for i in np.nonzero(hi > lo)[0]:
                if hit[i]:
                    continue
                dq = Qs[lo[i]:hi[i]] - P[i]
                if ((dq * dq).sum(1) < rad[i] * rad[i]).any():
                    hit[i] = True
    return hit


def steiner_points(ol: Outline, Dr: np.ndarray, bevel: float, inflate: float, segments: int,
                   max_edge: float, tan_min: float = 0.0, scale_w: float = 1.0) -> np.ndarray:
    """Graded ring points + medial points (SAMPLING 2–3)."""
    rings, island = ol.rings, ol.island
    rays_o, rays_dir, rays_scale, rays_isl = [], [], [], []
    for k, r in enumerate(rings):
        prev, nxt = np.roll(r, 1, axis=0), np.roll(r, -1, axis=0)
        tin, tout = _unit(r - prev), _unit(nxt - r)
        nin = np.column_stack([-tin[:, 1], tin[:, 0]])
        nout = np.column_stack([-tout[:, 1], tout[:, 0]])
        cross = tin[:, 0] * tout[:, 1] - tin[:, 1] * tout[:, 0]
        dot = (tin * tout).sum(1)
        alpha = np.arctan2(cross, dot)                     # > 0: convex (left turn, material on the left)
        bis = nin + nout
        bn = np.hypot(bis[:, 0], bis[:, 1])
        fallback = np.where((alpha > 0)[:, None], -tin, tin)
        bis = np.where((bn > 1e-6)[:, None], bis / np.maximum(bn, 1e-300)[:, None], fallback)
        sc = np.where(alpha > 0, 1.0 / np.maximum(np.cos(alpha / 2.0), 0.1), 1.0)
        rays_o.append(r)
        rays_dir.append(bis)
        rays_scale.append(sc)
        rays_isl.append(np.full(len(r), island[k]))
        fan = (alpha < -math.radians(FAN_DEG))
        if fan.any():
            fi = np.nonzero(fan)[0]
            J = np.ceil(np.abs(alpha[fi]) / math.radians(FAN_DEG)).astype(int)
            for i, nj in zip(fi, J):
                steps = np.arange(1, nj) / nj
                dirs = _rot(np.repeat(nin[i:i + 1], len(steps), axis=0), alpha[i] * steps)
                rays_o.append(np.repeat(r[i:i + 1], len(steps), axis=0))
                rays_dir.append(dirs)
                rays_scale.append(np.ones(len(steps)))
                rays_isl.append(np.full(len(steps), island[k]))
    O = np.vstack(rays_o)
    Dd = np.vstack(rays_dir)
    S = np.concatenate(rays_scale)
    I = np.concatenate(rays_isl)
    pts_out = []
    for isl in np.unique(I):
        sel = np.nonzero(I == isl)[0]
        ds = ring_distances(bevel, inflate, float(Dr[isl]), segments)
        if len(ds) == 0:
            continue
        o, dr, sc = O[sel], Dd[sel], S[sel]
        K = len(ds)
        t = sc[:, None] * ds[None, :]                                 # (R, K)
        P = o[:, None, :] + dr[:, None, :] * t[..., None]             # (R, K, 2)
        dtrue = ol.query(P.reshape(-1, 2))[0].reshape(len(sel), K)
        ok = dtrue >= (1.0 - RING_TOL) * ds[None, :]
        # a ray ends at its first failing ring
        alive = np.cumprod(ok, axis=1).astype(bool)
        first_bad = np.where(alive.all(axis=1), K, alive.sum(axis=1))
        gaps = np.diff(np.concatenate([[0.0], ds]))
        # tangential spacing per ring: z is constant along a ring, a chord across a ring's curve only sags by
        # spacing²/8R, which matters in proportion to the profile slope there -> spacing ∝ 1/sqrt(slope)
        sl = slope(ds, bevel, inflate, float(Dr[isl]))
        tan = np.clip(TAN_K * scale_w / np.sqrt(np.maximum(sl, 1e-6)), tan_min, TAN_MAX * scale_w)
        ring_pts = []
        for j in range(K):
            q = P[alive[:, j], j]
            if not len(q):
                continue
            cellr = max(0.45 * min(gaps[j], max_edge), 0.8 * tan[j])
            key = np.floor(q / max(cellr, 1e-12)).astype(np.int64)
            _, first = np.unique(key, axis=0, return_index=True)
            ring_pts.append(q[np.sort(first)])
        kept = np.vstack(ring_pts) if ring_pts else np.zeros((0, 2))
        # medial points: halfway between the last valid ring point (or the outline vertex) and the first failure
        rr = np.nonzero(first_bad < K)[0]
        if len(rr):
            fb = first_bad[rr]
            a = np.where((fb > 0)[:, None], P[rr, np.maximum(fb - 1, 0)], o[rr])
            bpt = P[rr, fb]
            med = 0.5 * (a + bpt)
            dmed = 0.5 * (np.where(fb > 0, ds[np.maximum(fb - 1, 0)], 0.0) + ds[fb])
            gap_at = gaps[np.minimum(np.searchsorted(ds, dmed), K - 1)]
            rad = 0.35 * np.minimum(gap_at, max_edge)
            # thin medial points against everything kept so far (and each other, greedily by grid)
            key = np.floor(med / max(2.0 * float(np.median(rad)), 1e-12)).astype(np.int64)
            _, first = np.unique(key, axis=0, return_index=True)
            first = np.sort(first)
            med, rad = med[first], rad[first]
            if len(kept):
                med = med[~_near_any(med, kept, rad)]
            if len(med):
                dm = ol.query(med)[0]
                med = med[dm > 0.25 * ds[0]]       # a ray that left the piece through a thin part
            kept = np.vstack([kept, med]) if len(med) else kept
        pts_out.append(kept)
    return np.vstack(pts_out) if pts_out else np.zeros((0, 2))


def _with_apices(steiner: np.ndarray, centres: dict, Dr: np.ndarray, bevel: float, inflate: float,
                 segments: int) -> np.ndarray:
    """Steiner points + the apex (inradius centre) of every island whose profile still rises at D (inflate > 0,
    or D < bevel), unless a Steiner point already lies within APEX_CLEAR × (D − its last ring distance)."""
    add = []
    for q, c in centres.items():
        D = float(Dr[q])
        if D <= 0 or not (inflate > 0 or D < bevel):
            continue
        ds = ring_distances(bevel, inflate, D, segments)
        clear = APEX_CLEAR * (D - (float(ds[-1]) if len(ds) else 0.0))
        if len(steiner) and float(np.hypot(*(steiner - np.asarray(c)).T).min()) < clear:
            continue
        add.append(c)
    return np.vstack([steiner, np.asarray(add, dtype=np.float64)]) if add else steiner


def merge_points(P: np.ndarray, q: float, fixed: Optional[np.ndarray] = None) -> np.ndarray:
    """Drop points of P within about ``q`` of an earlier point (or of a ``fixed`` point): rounding to a
    q-grid and to a half-cell shifted grid. The CDT only merges exactly coincident input points; nearly
    coincident ones (rays of a cusp meeting on its axis) made sliver triangles."""
    if not len(P):
        return P
    n_fixed = 0 if fixed is None else len(fixed)
    allp = P if fixed is None else np.vstack([fixed, P])
    keep = np.ones(len(allp), dtype=bool)
    for off in (0.0, 0.5):
        key = np.round(allp / q + off).astype(np.int64)
        key[~keep] = np.iinfo(np.int64).min + np.arange(int((~keep).sum()))[:, None]   # dropped: unique keys
        _, first = np.unique(key, axis=0, return_index=True)
        k2 = np.zeros(len(allp), dtype=bool)
        k2[first] = True
        keep &= k2
    keep[:n_fixed] = True
    return allp[n_fixed:][keep[n_fixed:]]


# ================================================================================================
# triangulation
# ================================================================================================
def triangulate(points: np.ndarray, ring_ranges: list, eps: float):
    """CDT of ``points`` (outline vertices first, ring k = points[s:e]) + Steiner points.

    -> (verts (V, 2), tris (T, 3) CCW, inside the even-odd region, constraint edge keys {(a, b) a < b},
    outline vertex flags (V,))."""
    from mathutils import Vector
    from mathutils.geometry import delaunay_2d_cdt
    vin = [Vector((float(x), float(y))) for x, y in points]
    faces = []
    for s, e in ring_ranges:          # the CDT wants CCW faces: holes (CW, material on the left) reversed
        f = list(range(s, e))
        faces.append(f if _area(points[s:e]) > 0 else f[::-1])
    vout, eout, fout, vorig, eorig, forig = delaunay_2d_cdt(vin, [], faces, 1, eps, True)
    V = np.array([(v.x, v.y) for v in vout], dtype=np.float64).reshape(-1, 2)
    keep = [i for i, fo in enumerate(forig) if len(fo) % 2 == 1]
    T = np.array([fout[i] for i in keep if len(fout[i]) == 3], dtype=np.int64).reshape(-1, 3)
    n_outline = ring_ranges[-1][1] if ring_ranges else 0
    on_outline = np.zeros(len(V), dtype=bool)
    for i, vo in enumerate(vorig):
        if any(o < n_outline for o in vo):
            on_outline[i] = True
    cons = set()
    for (a, b), eo in zip(eout, eorig):
        if eo:
            cons.add((min(a, b), max(a, b)))
            on_outline[a] = on_outline[b] = True       # intersection points of the outline are on it too
    if len(T):
        p = V[T]
        ar = (p[:, 1, 0] - p[:, 0, 0]) * (p[:, 2, 1] - p[:, 0, 1]) - (p[:, 2, 0] - p[:, 0, 0]) * (p[:, 1, 1] - p[:, 0, 1])
        T = np.where((ar < -1e-20)[:, None], T[:, [0, 2, 1]], T)
    return V, T, cons, on_outline


def _edges(T: np.ndarray):
    """Directed edges of CCW triangles -> (a, b, undirected key, count per key, inverse)."""
    a = T.reshape(-1)
    b = T[:, [1, 2, 0]].reshape(-1)
    lo, hi = np.minimum(a, b), np.maximum(a, b)
    keys = lo * (int(T.max()) + 1 if len(T) else 1) + hi
    uk, inv, cnt = np.unique(keys, return_inverse=True, return_counts=True)
    return a, b, uk, cnt, inv


# ================================================================================================
# build
# ================================================================================================
def _safe_normals(P: np.ndarray, T: np.ndarray, n: np.ndarray, free: np.ndarray) -> np.ndarray:
    """Blend the analytic vertex normals of the top surface toward +Z (which faces every top face: the top is a
    graph z(x, y)) just enough that each faces all of its adjacent top faces (dot ≥ NORMAL_MIN_DOT). Only
    ``free`` vertices are touched (rim vertices keep their horizontal normal). Matters on ridges of thin parts,
    where the one-sided analytic normal leans away from the faces across the ridge."""
    if not len(T):
        return n
    p0, p1, p2 = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
    fn = np.cross(p1 - p0, p2 - p0)
    fn = fn / np.maximum(np.linalg.norm(fn, axis=1), 1e-300)[:, None]
    corner_v = T.reshape(-1)
    corner_f = np.repeat(np.arange(len(T)), 3)
    out = n.copy()
    todo = free.copy()
    up = np.array([0.0, 0.0, 1.0])
    for lam in (0.0, 0.25, 0.5, 0.75, 0.9, 1.0):
        cand = (1.0 - lam) * n + lam * up
        cand = cand / np.maximum(np.linalg.norm(cand, axis=1), 1e-300)[:, None]
        mind = np.full(len(n), np.inf)
        np.minimum.at(mind, corner_v, (cand[corner_v] * fn[corner_f]).sum(1))
        ok = todo & (mind >= NORMAL_MIN_DOT)
        out[ok] = cand[ok]
        todo &= ~ok
        if not todo.any():
            break
    out[todo] = up
    return out


def build(splines: list, thickness: float, bevel: float, inflate: float = 0.0, segments: int = 6,
          scale: float = 1.0, triangulator: Optional[Callable] = None) -> Optional[dict]:
    """Height-field body of one piece (local units). -> dict of numpy arrays:

    ``verts`` (V, 3), ``loops`` (L,) vertex index per face corner, ``starts`` (F,) first loop of each face
    (faces are triangles then wall quads), ``normals`` (L, 3) custom split normals, ``sharp`` (S, 2)
    vertex pairs of sharp edges, plus ``info`` {half, D, islands, verts, faces, ms, ...}. None when the
    outline is empty/degenerate."""
    t0 = time.perf_counter()
    sc = max(float(scale), 1e-9)
    th = max(float(thickness), MIN_THICKNESS / sc)
    b = max(0.0, min(float(bevel), th / 2.0)) if bevel is not None else 0.0
    k = max(0.0, float(inflate or 0.0))
    if b < 1e-7 / sc:
        b = 0.0
    tol, max_edge, eps = CHORD_TOL / sc, MAX_EDGE / sc, MERGE_EPS / sc
    guard = min(max(0.5 * b, GUARD_MIN / sc), GUARD_MAX / sc)
    rings, shapes, hole, island = outline(splines, tol, max_edge, eps, guard)
    if not rings:
        return None
    ol = Outline(rings, shapes, island, max(NEAR / sc, 16 * tol))
    centres: dict = {}
    Dr = island_inradius(ol, centres=centres) if (b > 0 or k > 0) else np.zeros(len(rings))
    steiner = (steiner_points(ol, Dr, b, k, segments, max_edge, TAN_MIN / sc, 1.0 / sc) if (b > 0 or k > 0)
               else np.zeros((0, 2)))
    steiner = _with_apices(steiner, centres, Dr, b, k, segments)
    tri = triangulator or triangulate
    starts_r = np.cumsum([0] + [len(r) for r in rings])
    ring_ranges = [(int(starts_r[i]), int(starts_r[i + 1])) for i in range(len(rings))]
    outline_pts = np.vstack(rings)
    extra = merge_points(steiner, MERGE_Q / sc, outline_pts)
    V = T = None
    for _ in range(CHORD_PASSES + 1):
        V, T, cons, on_outline = tri(np.vstack([outline_pts, extra]), ring_ranges, 1e-7 / sc)
        if not len(T):
            return None
        ea, eb, uk, cnt, inv = _edges(T)
        # chords: interior edges joining two outline vertices that are not outline (constraint) edges
        both = on_outline[ea] & on_outline[eb] & (cnt[inv] == 2)
        lo, hi = np.minimum(ea, eb), np.maximum(ea, eb)
        add = []
        if both.any():
            for x, y in set(zip(lo[both].tolist(), hi[both].tolist())):
                if (x, y) not in cons:
                    add.append(0.5 * (V[x] + V[y]))
        # triangles with three outline vertices and no chord (a whole small piece): their centroid
        allb = on_outline[T].all(axis=1)
        if allb.any() and not add:
            cen = V[T[allb]].mean(axis=1)
            dd = ol.query(cen)[0]
            add += [c for c, x in zip(cen, dd) if x > 1e-6 / sc]
        if not add:
            break
        extra = np.vstack([extra, merge_points(np.asarray(add), MERGE_Q / sc, np.vstack([outline_pts, extra]))])
    used = np.unique(T)
    remap = np.full(len(V), -1, dtype=np.int64)
    remap[used] = np.arange(len(used))
    V2 = V[used]
    T = remap[T]
    on_b = on_outline[used]
    nv = len(V2)
    # ---- outline (boundary) edges of the kept triangulation ---------------------------------------------
    ea, eb, uk, cnt, inv = _edges(T)
    bmask = cnt[inv] == 1
    be_a, be_b = ea[bmask], eb[bmask]            # directed a -> b, interior on the left (CCW triangles)
    on_b[be_a] = True
    on_b[be_b] = True
    # inward normal at outline vertices: mean of the adjacent outline edges' left normals
    ev = V2[be_b] - V2[be_a]
    en = _unit(np.column_stack([-ev[:, 1], ev[:, 0]]))
    gb = np.zeros((nv, 2))
    np.add.at(gb, be_a, en)
    np.add.at(gb, be_b, en)
    # ---- distances, islands ---------------------------------------------------------------------------
    d = np.zeros(nv)
    g = np.zeros((nv, 2))
    Dv = np.ones(nv)
    inner = np.nonzero(~on_b)[0]
    if len(inner):
        di, isl, gi = ol.query(V2[inner], KAPPA / sc, KAPPA_REL)
        d[inner] = di
        g[inner] = gi
        Dmax = Dr.copy()
        for q in np.unique(isl):
            m = isl == q
            Dmax[island == q] = max(float(Dmax[q]), float(di[m].max()))
        Dv[inner] = np.maximum(Dmax[isl], 1e-12)
    else:
        Dmax = Dr.copy()
    gbn = np.hypot(gb[:, 0], gb[:, 1])
    g[on_b] = gb[on_b] / np.maximum(gbn[on_b], 1e-300)[:, None]
    d[on_b] = 0.0
    # outline vertices without an outline edge (internal constraints between two inside regions): no direction
    loose = on_b & (gbn < 1e-9)
    # ---- heights + normals ------------------------------------------------------------------------------
    e = wall_half(th, b)
    if e < 1e-7 / sc:
        e = 0.0
    z = profile(d, th, b, k, Dv)
    s = slope(d, b, k, Dv)
    vertical = np.isinf(s) & ~loose
    with np.errstate(invalid="ignore"):
        nt = np.column_stack([-np.where(np.isinf(s), 0.0, s)[:, None] * g, np.ones(nv)])
    nt[vertical] = np.column_stack([-g[vertical], np.zeros(int(vertical.sum()))])
    nt = nt / np.maximum(np.linalg.norm(nt, axis=1), 1e-300)[:, None]
    P3 = np.column_stack([V2, z])
    nt = _safe_normals(P3, T, nt, ~vertical)
    # ---- per-corner normals of the top; singular outline vertices -----------------------------------------
    # Rim vertices at cusps / very sharp corners are cone apices: no single normal faces all their faces.
    # Their fans are split (sharp edges) and each face's corner there continues the face's other two corners.
    fn = np.cross(P3[T[:, 1]] - P3[T[:, 0]], P3[T[:, 2]] - P3[T[:, 0]])
    fn = fn / np.maximum(np.linalg.norm(fn, axis=1), 1e-300)[:, None]
    CT = nt[T]                                              # (T, 3 corners, 3)
    cdot = (CT * fn[:, None, :]).sum(-1)
    mind = np.full(nv, np.inf)
    np.minimum.at(mind, T.reshape(-1), cdot.reshape(-1))
    e_wall = wall_half(th, b)
    if e_wall >= 1e-7 / sc and len(be_a):
        # wall corners: the rim vertex's horizontal normal against the wall quad's own outward normal
        ev_ = V2[be_b] - V2[be_a]
        wn_ = _unit(np.column_stack([ev_[:, 1], -ev_[:, 0]]))
        hz = -g / np.maximum(np.hypot(g[:, 0], g[:, 1]), 1e-300)[:, None]
        for vv in (be_a, be_b):
            np.minimum.at(mind, vv, (hz[vv] * wn_).sum(1))
    sing = on_b & (mind < NORMAL_MIN_DOT)
    if sing.any():
        for c in range(3):
            m = sing[T[:, c]]
            if m.any():
                o = nt[T[m, (c + 1) % 3]] + nt[T[m, (c + 2) % 3]]
                o = o / np.maximum(np.linalg.norm(o, axis=1), 1e-300)[:, None]
                bad = (o * fn[m]).sum(1) < NORMAL_MIN_DOT
                o[bad] = fn[m][bad]
                CT[m, c] = o
    # ---- assemble ---------------------------------------------------------------------------------------
    walls = e > 0
    n_in = int((~on_b).sum())
    if walls:
        nb_index = np.arange(nv) + nv               # every vertex mirrored (outline ring at −e)
        verts = np.vstack([P3, np.column_stack([V2, -z])])
    else:
        nb_index = np.where(on_b, np.arange(nv), 0)  # outline vertices shared by top and bottom
        nb_index[~on_b] = nv + np.arange(n_in)
        verts = np.vstack([P3, np.column_stack([V2[~on_b], -z[~on_b]])])
    mirror = np.array([1.0, 1.0, -1.0])
    loops = [T.reshape(-1), nb_index[T[:, [0, 2, 1]]].reshape(-1)]
    normals = [CT.reshape(-1, 3), (CT[:, [0, 2, 1]] * mirror).reshape(-1, 3)]
    nfaces = 2 * len(T)
    starts = [np.arange(0, 6 * len(T), 3)]
    sharp = [np.zeros((0, 2), dtype=np.int64)]
    if sing.any():
        tv = np.column_stack([T.reshape(-1), T[:, [1, 2, 0]].reshape(-1)])
        tv = tv[sing[tv[:, 0]] | sing[tv[:, 1]]]
        sharp += [tv, nb_index[tv]]
    if walls:
        quad = np.column_stack([be_b, be_a, nb_index[be_a], nb_index[be_b]])
        horiz = np.column_stack([-g[:, 0], -g[:, 1], np.zeros(nv)])
        horiz = horiz / np.maximum(np.linalg.norm(horiz, axis=1), 1e-300)[:, None]
        qn = horiz[np.column_stack([be_b, be_a, be_a, be_b])]           # (Q, 4, 3)
        ev = V2[be_b] - V2[be_a]
        wn = _unit(np.column_stack([ev[:, 1], -ev[:, 0]]))                # the wall's own outward normal
        wall_crease = sing.copy()
        if not vertical[on_b].any():
            # flat rim (no bevel, no inflate): a corner turning more than CORNER_DEG is a vertical crease too —
            # the bisector normal shaded a sharp slab's corners as if they were rounded
            mind_w = np.full(nv, np.inf)
            for vv in (be_a, be_b):
                np.minimum.at(mind_w, vv, (horiz[vv, :2] * wn).sum(1))
            wall_crease |= on_b & (mind_w < math.cos(math.radians(CORNER_DEG / 2.0)))
        if wall_crease.any():
            wn3 = np.column_stack([wn, np.zeros(len(wn))])
            for c, vv in enumerate((be_b, be_a, be_a, be_b)):
                m = wall_crease[vv]
                qn[m, c] = wn3[m]
            sv = np.nonzero(wall_crease)[0]
            sharp.append(np.column_stack([sv, nb_index[sv]]))
        loops.append(quad.reshape(-1))
        normals.append(qn.reshape(-1, 3))
        starts.append(6 * len(T) + 4 * np.arange(len(quad)))
        nfaces += len(quad)
        if not vertical[on_b].any():
            # flat rim (no bevel, no inflate): the rim edges are creases
            sharp += [np.column_stack([be_a, be_b]), np.column_stack([nb_index[be_a], nb_index[be_b]])]
    sharp = np.vstack(sharp)
    info = {"half": float(z.max()) if len(z) else 0.0, "wall": e, "bevel": b, "inflate": k,
            "D": float(Dmax.max()) if len(Dmax) else 0.0, "rings": len(rings), "holes": int(hole.sum()),
            "islands": int(len(np.unique(island))), "steiner": int(len(steiner)), "verts": int(len(verts)),
            "singular": int(sing.sum()),
            "faces": int(nfaces), "ms": round((time.perf_counter() - t0) * 1000.0, 2)}
    return {"verts": verts, "loops": np.concatenate(loops).astype(np.int64),
            "starts": np.concatenate(starts).astype(np.int64), "normals": np.vstack(normals),
            "sharp": sharp.astype(np.int64), "info": info}


# ================================================================================================
# checks (numpy level; Blender-level BVH check in check_mesh)
# ================================================================================================
def check_arrays(arr: dict) -> dict:
    """Topology report of :func:`build` output: non-manifold / inconsistently wound edges, signed volume,
    custom normals facing away from their faces."""
    V, L, S = arr["verts"], arr["loops"], arr["starts"]
    ends = np.append(S[1:], len(L))
    sizes = ends - S
    face_of = np.repeat(np.arange(len(S)), sizes)
    pos = np.arange(len(L)) - np.repeat(S, sizes)
    nxt = np.where(pos + 1 < np.repeat(sizes, sizes), np.arange(len(L)) + 1, np.repeat(S, sizes))
    a, b = L, L[nxt]
    key = np.minimum(a, b) * (len(V) + 1) + np.maximum(a, b)
    uk, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
    nonmanifold = int((cnt != 2).sum())
    # each undirected edge must appear once in each direction
    fwd = np.zeros(len(uk), dtype=np.int64)
    np.add.at(fwd, inv, (a < b).astype(np.int64))
    misoriented = int(((cnt == 2) & (fwd != 1)).sum())
    # signed volume (divergence theorem over fan triangles)
    vol = 0.0
    fn = np.zeros((len(S), 3))
    for nsz in np.unique(sizes):
        fs = np.nonzero(sizes == nsz)[0]
        idx = L[S[fs][:, None] + np.arange(nsz)[None, :]]
        p = V[idx]
        for j in range(1, nsz - 1):
            p0, p1, p2 = p[:, 0], p[:, j], p[:, j + 1]
            c = np.cross(p1 - p0, p2 - p0)
            vol += float((p0 * c).sum()) / 6.0
            fn[fs] += c
    fnu = fn / np.maximum(np.linalg.norm(fn, axis=1), 1e-300)[:, None]
    dots = (arr["normals"] * fnu[face_of]).sum(1)
    big = np.linalg.norm(fn, axis=1)[face_of] > 1e-14
    return {"nonManifold": nonmanifold, "misoriented": misoriented, "volume": vol,
            "invertedNormals": int(((dots < -1e-3) & big).sum()), "minNormalDot": float(dots[big].min()) if big.any() else 1.0}


# ================================================================================================
# Blender meshes (+ cache)
# ================================================================================================
def to_mesh(name: str, arr: dict):
    import bpy
    me = bpy.data.meshes.new(name)
    V, L, S = arr["verts"], arr["loops"], arr["starts"]
    me.vertices.add(len(V))
    me.vertices.foreach_set("co", V.astype(np.float32).ravel())
    me.loops.add(len(L))
    me.loops.foreach_set("vertex_index", L.astype(np.int32))
    me.polygons.add(len(S))
    me.polygons.foreach_set("loop_start", S.astype(np.int32))
    me.update(calc_edges=True)
    sf = me.attributes.get("sharp_face")
    if sf is not None:                       # no 'sharp_face' attribute = every face smooth
        me.attributes.remove(sf)
    if len(arr["sharp"]):
        ev = np.empty(len(me.edges) * 2, dtype=np.int32)
        me.edges.foreach_get("vertices", ev)
        ev = ev.reshape(-1, 2).astype(np.int64)
        n = len(V) + 1
        ekey = np.minimum(ev[:, 0], ev[:, 1]) * n + np.maximum(ev[:, 0], ev[:, 1])
        sk = np.minimum(arr["sharp"][:, 0], arr["sharp"][:, 1]) * n + np.maximum(arr["sharp"][:, 0], arr["sharp"][:, 1])
        flags = np.isin(ekey, sk)
        att = me.attributes.get("sharp_edge") or me.attributes.new("sharp_edge", "BOOLEAN", "EDGE")
        att.data.foreach_set("value", flags)
    nrm = arr["normals"].astype(np.float32)
    try:
        # Blender 5.0 "free" custom normals: a float corner attribute used as-is (10x faster than
        # normals_split_custom_set's lnor-space encoding, and exact). The sharp edges above still describe the
        # creases should a user clear the custom normals.
        att = me.attributes.new("custom_normal", "FLOAT_VECTOR", "CORNER")
        att.data.foreach_set("vector", nrm.ravel())
    except (RuntimeError, TypeError, AttributeError):
        cn = me.attributes.get("custom_normal")
        if cn is not None:
            me.attributes.remove(cn)
        me.normals_split_custom_set(nrm)
    return me


def _key(key_parts: dict, thickness: float, bevel: float, inflate: float, segments: int, scale: float) -> str:
    from .util import stable_hash
    return stable_hash({**key_parts, "t": round(float(thickness), 6), "b": round(float(bevel), 6),
                        "k": round(float(inflate), 4), "s": int(segments), "sc": round(float(scale), 4),
                        "v": VERSION})


def piece_mesh(key_parts: dict, splines: list, thickness: float, bevel: float, inflate: float = 0.0,
               segments: int = 6, scale: float = 1.0):
    """Cached MESH datablock of one piece (local units, centred on its mid-plane) -> (mesh | None, info).
    ``mesh['bis_half']`` = its half height, ``mesh['bis_route']`` = 'heightfield'."""
    import bpy
    key = _key(key_parts, thickness, bevel, inflate, segments, scale)
    name = _MESH_CACHE.get(key)
    me = bpy.data.meshes.get(name) if name else None
    if me is not None:
        _MESH_CACHE.move_to_end(key)
        return me, {"half": float(me.get("bis_half", 0.0)), "cached": True}
    arr = build(splines, thickness, bevel, inflate, segments, scale)
    if arr is None:
        return None, {"half": 0.0, "empty": True}
    me = to_mesh(f"BIS~{key[:10]}", arr)
    me.materials.append(None)               # the object-linked slot carries the material
    me["bis_key"] = key
    me["bis_route"] = "heightfield"
    me["bis_half"] = arr["info"]["half"]
    me["bis_ms"] = arr["info"]["ms"]
    _MESH_CACHE[key] = me.name
    _evict()
    return me, dict(arr["info"], cached=False)


def _evict() -> None:
    import bpy
    while len(_MESH_CACHE) > CACHE_LIMIT:
        _k, name = _MESH_CACHE.popitem(last=False)
        me = bpy.data.meshes.get(name)
        if me is not None and me.users == 0:
            bpy.data.meshes.remove(me)


def is_cached_mesh(me) -> bool:
    return me is not None and bool(me.get("bis_key"))


def purge_unused_meshes() -> int:
    import bpy
    cached = set(_MESH_CACHE.values())
    n = 0
    for me in list(bpy.data.meshes):
        if me.get("bis_key") and me.users == 0 and me.name not in cached:
            bpy.data.meshes.remove(me)
            n += 1
    return n


def reset_caches() -> None:
    """Forget cached datablock names (after read_homefile the datablocks are gone)."""
    _MESH_CACHE.clear()


def check_mesh(me) -> dict:
    """Blender-level check of a body mesh: non-manifold edges, self-intersections (BVH overlap of
    non-adjacent faces), faces whose custom normals point away from them, signed volume."""
    import bmesh
    from mathutils.bvhtree import BVHTree
    bm = bmesh.new()
    bm.from_mesh(me)
    nonman = sum(1 for e in bm.edges if not e.is_manifold)
    bm.faces.ensure_lookup_table()
    tree = BVHTree.FromBMesh(bm)
    pairs = tree.overlap(tree)
    verts_of = [set(v.index for v in f.verts) for f in bm.faces]
    inter = sum(1 for i, j in pairs if i < j and not (verts_of[i] & verts_of[j]))
    vol = bm.calc_volume(signed=True)
    bm.free()
    cn = np.empty(len(me.loops) * 3, dtype=np.float32)
    me.corner_normals.foreach_get("vector", cn)
    cn = cn.reshape(-1, 3)
    pn = np.empty(len(me.polygons) * 3, dtype=np.float32)
    me.polygons.foreach_get("normal", pn)
    pn = pn.reshape(-1, 3)
    ls = np.empty(len(me.polygons), dtype=np.int32)
    me.polygons.foreach_get("loop_start", ls)
    lt = np.empty(len(me.polygons), dtype=np.int32)
    me.polygons.foreach_get("loop_total", lt)
    face_of = np.repeat(np.arange(len(ls)), lt)
    dots = (cn * pn[face_of]).sum(1)
    return {"nonManifold": int(nonman), "selfIntersections": int(inter), "volume": float(vol),
            "invertedNormals": int((dots < -1e-3).sum()), "minNormalDot": float(dots.min()) if len(dots) else 1.0,
            "verts": len(me.vertices), "faces": len(me.polygons)}
