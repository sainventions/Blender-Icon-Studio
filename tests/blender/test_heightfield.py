"""Height-field bodies (blender_worker/heightfield.py, PLAN §11 Geometry), the pure-numpy parts: the profile,
graded ring distances, outline flattening / cleaning / nesting, distance queries and Steiner sampling. No Blender
needed (the CDT and mesh checks run inside Blender: test_heightfield_corpus.py)."""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from blender_worker import geometry as G  # noqa: E402
from blender_worker import heightfield as H  # noqa: E402


def _ring_area(r):
    x, y = r[:, 0], r[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y))


def _poly(corners, closed=True):
    s = G.poly_spline(corners)
    s["closed"] = closed
    return s


# ------------------------------------------------------------------------------------------------ profile
def test_profile_round_edge_and_wall():
    t, b = 0.10, 0.03
    e = H.wall_half(t, b)
    assert e == pytest.approx(0.02)
    d = np.array([0.0, b / 2, b, 2 * b])
    z = H.profile(d, t, b, 0.0, 1.0)
    assert z[0] == pytest.approx(e)                                   # the rim sits on the wall top
    assert z[1] == pytest.approx(e + math.sqrt(b * b - (b / 2) ** 2))  # quarter circle of radius b
    assert z[2] == pytest.approx(t / 2) and z[3] == pytest.approx(t / 2)   # flat top beyond the round edge
    s = H.slope(d, b, 0.0, 1.0)
    assert np.isinf(s[0]) and s[2] == 0.0 and s[3] == 0.0             # vertical tangent at the rim


def test_thin_parts_taper_instead_of_inverting():
    """An island narrower than the bevel (D < b) is a lens of half height hb(D) < t/2, never an inverted bevel."""
    t, b = 0.10, 0.05
    halves = [H.half_height(t, b, 0.0, D) for D in (0.002, 0.01, 0.025, 0.049, 0.05, 0.2)]
    assert all(h2 >= h1 for h1, h2 in zip(halves, halves[1:]))         # monotone in the island's size
    assert halves[0] < 0.3 * t / 2 and halves[-1] == pytest.approx(t / 2)
    assert max(halves) <= t / 2 + 1e-12


def test_inflate_dome_and_sphere():
    t, b, k, D = 0.10, 0.03, 1.0, 0.3
    assert H.half_height(t, b, k, D) == pytest.approx(t / 2 + k * D)
    assert np.isinf(H.slope(np.array([0.0]), 0.0, k, D)[0])           # inflate alone also meets the rim vertically
    assert H.slope(np.array([D]), b, k, D)[0] == pytest.approx(0.0)
    # bevel = radius = half the thickness: a disc of radius R becomes a near-sphere, with a round edge of radius
    # (1 − WALL_MIN)·R on the minimum wall WALL_MIN·R (round 8: no knife-thin rims)
    R = 0.2
    r = np.linspace(0, R, 9)
    z = H.profile(R - r, 2 * R, R, 0.0, R)
    b = (1.0 - H.WALL_MIN) * R
    assert H.rim_bevel(2 * R, R) == pytest.approx(b) and H.wall_half(2 * R, R) == pytest.approx(H.WALL_MIN * R)
    assert np.allclose(z, H.WALL_MIN * R + np.sqrt(b * b - (b - np.minimum(R - r, b)) ** 2))
    assert z[0] == pytest.approx(R) and z[-1] == pytest.approx(H.WALL_MIN * R)


def test_round_edge_is_capped_at_the_local_half_width():
    """Round 8 (QA r9 N4): the round-edge radius is capped LOCALLY. The disc model: an island narrower than the bevel
    is a round body of radius D (slope 0 at its centre: no roof ridge), its half height e + min(b, D)."""
    t, b = 0.16, 0.06
    e = H.wall_half(t, b)
    for D in (0.01, 0.03, 0.059, 0.2):
        assert H.half_height(t, b, 0.0, D) == pytest.approx(e + min(b, D))
        assert H.slope(np.array([D]), b, 0.0, D)[0] == pytest.approx(0.0, abs=1e-9)    # flat on the spine / apex
    d = np.linspace(0.0, 0.02, 5)
    assert np.allclose(H.rim_height(d, 0.02), np.sqrt(0.02 ** 2 - (0.02 - d) ** 2))   # a tube of radius w
    sd, sb = H.rim_slopes(np.array([0.0, 0.01, 0.02, 0.03]), np.full(4, 0.02))
    assert np.isinf(sd[0]) and sd[2] == 0.0 and sd[3] == 0.0 and sb[3] == 1.0


def test_local_width_of_strips_discs_and_corners():
    """local_width(): the largest tangent disc at each outline vertex (a strip's half width along its sides, a disc's
    radius); the sliding max over ±b keeps a wide part's corner at the plain round edge (w ≥ b there)."""
    strip = _poly([(-0.5, -0.03), (0.5, -0.03), (0.5, 0.03), (-0.5, 0.03)])
    ol, D = _outline([strip], 0.08)
    w = H.local_width(ol, D, 2 * H.CHORD_TOL)
    side = np.abs(np.vstack(ol.rings)[:, 0]) < 0.4
    assert np.allclose(w[side], 0.03, atol=0.0015)
    disc = [G.circle_spline(0.0, 0.0, 0.05)]
    ol, D = _outline(disc, 0.08)
    assert np.allclose(H.local_width(ol, D, 2 * H.CHORD_TOL), 0.05, atol=0.003)
    sq = [G.rect_spline(-0.4, -0.4, 0.4, 0.4)]
    ol, D = _outline(sq, 0.08)
    raw = H.local_width(ol, D, 2 * H.CHORD_TOL)
    win = H.local_width(ol, D, 2 * H.CHORD_TOL, 0.08)
    corner = np.hypot(*(np.abs(np.vstack(ol.rings)) - 0.4).T) < 1e-9
    assert raw[corner].max() < 0.01 and win[corner].min() > 0.06       # the corner itself → 0; windowed: wide again
    # blend_width on the strip: the spine gets the half width from both sides, beside a side its own foot's value
    ol, D = _outline([strip], 0.08)
    w = H.local_width(ol, D, 2 * H.CHORD_TOL, 0.08)
    bw, gw, d, gd = H.blend_width(ol, w, np.array([(0.0, 0.0), (0.1, 0.02), (0.1, -0.025)]))
    assert np.allclose(bw, 0.03, atol=0.002) and np.allclose(d, [0.03, 0.01, 0.005], atol=1e-6)
    assert np.abs(gw).max() < 0.05                                       # constant width: no slope


def test_island_apex_is_deterministic():
    """Round 8: the inradius probe's start cell and moves break ties deterministically (a rectangle's medial plateau;
    float32 vs float64 noise): the apex of a key-like rounded rectangle is its centre, and a tiny perturbation of the
    distances (a mirrored copy) gives the mirrored apex."""
    r = G.rect_spline(-0.3, -0.1, 0.5, 0.1)
    ol, _ = _outline([r], 0.04)
    c: dict = {}
    H.island_inradius(ol, centres=c)
    assert c[0][0] == pytest.approx(0.1, abs=0.02) and c[0][1] == pytest.approx(0.0, abs=0.01)
    m = G.rect_spline(-0.5, -0.1, 0.3, 0.1)                            # the mirror image
    ol2, _ = _outline([m], 0.04)
    c2: dict = {}
    H.island_inradius(ol2, centres=c2)
    assert c2[0][0] == pytest.approx(-c[0][0], abs=0.02)


def test_ring_distances_are_graded_toward_the_rim():
    ds = H.ring_distances(0.04, 0.0, 1.0, 6)
    assert len(ds) == 6 and ds[-1] == pytest.approx(0.04)
    gaps = np.diff(np.concatenate([[0.0], ds]))
    assert (np.diff(gaps) > 0).all()                                  # densest where the profile is steepest
    # uniform in angle along the quarter circle
    ang = np.degrees(np.arccos(1.0 - ds / 0.04))
    assert np.allclose(ang, np.arange(1, 7) * 15.0)
    # rings never reach past the island's inradius; inflate adds dome rings
    assert (H.ring_distances(0.04, 0.0, 0.02, 6) < 0.02).all()
    dome = H.ring_distances(0.04, 1.0, 0.3, 6)
    assert len(dome) > 6 and dome.max() < 0.3


# ------------------------------------------------------------------------------------------------ outline
def test_open_subpath_with_coincident_end_is_a_closed_fill():
    """Canvas / Calculator dots: SVG paths without 'Z' ending with 'h0' arrive as closed:false with the last point on
    the first. They are fills: one closed disc."""
    K = 0.5522847498
    k = 0.1 * K
    pts = [{"co": [0.1, 0], "hl": [0.1, -k], "hr": [0.1, k]}, {"co": [0, 0.1], "hl": [k, 0.1], "hr": [-k, 0.1]},
           {"co": [-0.1, 0], "hl": [-0.1, k], "hr": [-0.1, -k]}, {"co": [0, -0.1], "hl": [-k, -0.1], "hr": [k, -0.1]},
           {"co": [0.1, 0], "hl": [0.1, -k], "hr": [0.1, 0]}]
    sp = {"closed": False, "points": pts}
    assert G.count_open([sp]) == 1
    rings, _shapes, hole, _isl = H.outline([sp], H.CHORD_TOL, H.MAX_EDGE, H.MERGE_EPS)
    assert len(rings) == 1 and not hole[0]
    assert abs(_ring_area(rings[0]) - math.pi * 0.01) < 2e-4


def test_open_subpath_with_gap_gets_a_straight_closing_edge():
    """Gmail: a 3-point spline left open with a gap; SVG fills it as if closed by a straight line."""
    sp = _poly([(0, 0), (0.4, 0), (0.4, 0.3)], closed=False)
    sp["points"][-1]["hr"] = [9.0, 9.0]       # dangling handles of an open path are meaningless
    sp["points"][0]["hl"] = [-9.0, -9.0]
    rings, *_ = H.outline([sp], H.CHORD_TOL, H.MAX_EDGE, H.MERGE_EPS)
    assert abs(abs(_ring_area(rings[0])) - 0.5 * 0.4 * 0.3) < 1e-6


def test_zero_width_slits_go_sharp_tips_stay():
    """A boolean-union seam (the outline runs into the shape and back along itself) is not a feature: it would
    cut a crack into the body. A genuinely sharp tip (Gemini) is kept."""
    seam = [(0, 0), (1, 0), (1, 0.4), (0.6, 0.4), (0.6, 0.15), (0.6 + 1e-5, 0.4), (0, 0.4)]
    seam_pts = np.array(seam, dtype=float)
    r = H.clean_ring(np.vstack([np.linspace(seam_pts[i], seam_pts[(i + 1) % 7], 6, endpoint=False)
                                for i in range(7)]), 1e-5)
    assert r[:, 1].min() >= 0.0 and not ((r[:, 1] < 0.39) & (r[:, 1] > 0.01) & (np.abs(r[:, 0] - 0.6) < 1e-3)).any()
    assert abs(_ring_area(r) - 0.4) < 1e-6
    tip = np.array([(0, 0), (1, 0.09), (0, 0.18)])                    # a 10° tip at (1, 0.09)
    assert len(H.clean_ring(tip, 1e-5)) == 3


def test_flattening_meets_the_chord_tolerance():
    """Wang's bound per segment: a circle keeps its curvature, a dense traced contour needs ~1 sample per segment,
    and a long straight run is split at MAX_EDGE (z only varies across the rings)."""
    c = G.circle_spline(0, 0, 0.6)
    ring = H.flatten_spline(c, H.CHORD_TOL, H.MAX_EDGE)
    rad = np.hypot(ring[:, 0], ring[:, 1])
    mids = 0.5 * (ring + np.roll(ring, -1, axis=0))
    assert np.abs(rad - 0.6).max() < 1e-3 and (0.6 - np.hypot(mids[:, 0], mids[:, 1])).max() < 1.5 * H.CHORD_TOL
    t = np.linspace(0, 2 * np.pi, 300, endpoint=False)
    dense = np.column_stack([0.7 * np.cos(t), 0.5 * np.sin(t)])
    pts = []
    for i in range(len(dense)):
        p, q, o = dense[i], dense[(i + 1) % len(dense)], dense[i - 1]
        tan = (q - o) / 6.0
        pts.append({"co": list(p), "hl": list(p - tan), "hr": list(p + tan)})
    assert len(H.flatten_spline({"closed": True, "points": pts}, H.CHORD_TOL, H.MAX_EDGE)) <= 2 * len(pts)
    line = H.flatten_spline(_poly([(0, 0), (1, 0), (1, 0.01)]), H.CHORD_TOL, H.MAX_EDGE)
    seg = np.hypot(*(np.roll(line, -1, axis=0) - line).T)
    assert seg.max() <= H.MAX_EDGE + 1e-9


def test_nesting_orients_outers_ccw_and_holes_cw():
    outer = G.rect_spline(-1, -1, 1, 1)
    hole = G.rect_spline(-0.6, -0.6, 0.6, 0.6)               # same winding as the outer on purpose
    island = G.circle_spline(0, 0, 0.3)
    rings, shapes, hole_f, isl = H.outline([outer, hole, island], H.CHORD_TOL, H.MAX_EDGE, H.MERGE_EPS)
    assert hole_f.tolist() == [False, True, False]
    assert [_ring_area(r) > 0 for r in rings] == [True, False, True]
    assert [_ring_area(r) > 0 for r in shapes] == [True, False, True]
    assert isl.tolist() == [0, 0, 2]                           # the hole belongs to the outer, the disc is its own


def test_shape_rings_are_the_sample_rings_without_collinear_points():
    sq = _poly([(0, 0), (1, 0), (1, 1), (0, 1)])
    rings, shapes, *_ = H.outline([sq], H.CHORD_TOL, H.MAX_EDGE, H.MERGE_EPS)
    assert len(rings[0]) > 40 and len(shapes[0]) <= 6
    assert _ring_area(shapes[0]) == pytest.approx(_ring_area(rings[0]))


# ------------------------------------------------------------------------------------------------ distances
def test_nearest_matches_brute_force():
    rng = np.random.default_rng(4)
    ring = np.array([(0, 0), (1, 0), (1.2, 0.7), (0.4, 1.1), (-0.2, 0.6)], dtype=float)
    A, B, _ = H._segments([ring])
    P = rng.uniform(-0.3, 1.3, size=(700, 2))
    d, seg, g = H.nearest(P, A, B)
    ab = B - A
    t = np.clip(((P[:, None, :] - A[None]) * ab[None]).sum(-1) / (ab * ab).sum(1)[None], 0, 1)
    q = A[None] + t[..., None] * ab[None]
    brute = np.sqrt(((P[:, None, :] - q) ** 2).sum(-1))
    assert np.allclose(d, brute.min(1), atol=2e-6)
    assert np.allclose(np.hypot(g[:, 0], g[:, 1]), 1.0, atol=1e-5)


def test_softmin_direction_is_symmetric_on_a_ridge():
    """The normal's inward direction g: unit length beside a thin strip's spine, ~0 on it (symmetric ridge normal),
    and still unit length along a smooth convex side (two-cluster average)."""
    strip = np.array([(0, -0.02), (1, -0.02), (1, 0.02), (0, 0.02)], dtype=float)
    A, B, _ = H._segments([strip])
    _, _, g = H.nearest(np.array([(0.5, 0.0), (0.5, 0.012), (0.5, -0.012)]), A, B, H.KAPPA, rel=H.KAPPA_REL)
    n = np.hypot(g[:, 0], g[:, 1])
    assert n[0] < 0.05 and n[1] > 0.95 and n[2] > 0.95
    assert g[1, 1] < -0.95 and g[2, 1] > 0.95                     # pointing away from the nearer side
    circ = H.flatten_spline(G.circle_spline(0, 0, 0.5), H.CHORD_TOL, H.MAX_EDGE)
    A, B, _ = H._segments([circ])
    _, _, g = H.nearest(np.array([(0.3, 0.0)]), A, B, H.KAPPA, rel=H.KAPPA_REL)
    assert np.hypot(*g[0]) > 0.97 and g[0, 0] < -0.97            # radial, inward


def test_round_blob_keeps_exact_gradient_directions():
    """A disc has ONE foot point per interior point: g stays the exact radial unit vector down to near the centre
    (a sphere head / dome keeps true normals; averaging every segment flattened them, or even reversed them)."""
    circ = H.flatten_spline(G.circle_spline(0, 0, 0.3), H.CHORD_TOL, H.MAX_EDGE)
    A, B, _ = H._segments([circ])
    ang = np.linspace(0, 2 * np.pi, 7, endpoint=False)
    for r in (0.01, 0.03, 0.08, 0.15, 0.25):
        P = np.column_stack([r * np.cos(ang), r * np.sin(ang)])
        _, _, g = H.nearest(P, A, B, H.KAPPA, rel=H.KAPPA_REL, nbr=H._ring_neighbours([circ]))
        radial = -P / r
        # (exact up to the outline polygon: a foot lies on a chord, its normal within half a chord's angle)
        assert ((g * radial).sum(1) > 0.997).all(), (r, g)
    # a ridge still blends both sides (second foot point across the strip)
    strip = np.array([(0, -0.02), (1, -0.02), (1, 0.02), (0, 0.02)], dtype=float)
    A, B, _ = H._segments([strip])
    _, _, g = H.nearest(np.array([(0.5, 0.0)]), A, B, H.KAPPA, rel=H.KAPPA_REL, nbr=H._ring_neighbours([strip]))
    assert np.hypot(*g[0]) < 0.05


# ------------------------------------------------------------------------------------------------ sampling
def _outline(splines, b=0.04):
    rings, shapes, hole, isl = H.outline(splines, H.CHORD_TOL, H.MAX_EDGE, H.MERGE_EPS, min(max(0.5 * b, H.GUARD_MIN),
                                                                                            H.GUARD_MAX))
    ol = H.Outline(rings, shapes, isl, max(H.NEAR, 16 * H.CHORD_TOL))
    return ol, H.island_inradius(ol)


def test_inradius_estimate():
    ol, D = _outline([G.circle_spline(0, 0, 0.3)])
    assert D[0] == pytest.approx(0.3, rel=0.01)
    ol, D = _outline([G.rect_spline(-0.5, -0.05, 0.5, 0.05)])
    assert D[0] == pytest.approx(0.05, rel=0.02)
    assert H.inradius([G.circle_spline(0, 0, 0.3), G.circle_spline(0, 0, 0.1, hole=True)]) == pytest.approx(0.1, rel=0.03)


def test_steiner_points_follow_the_rings_and_fill_thin_tips():
    """A sharp star-like tip (Gemini): ring points sit at their ring distances, every Steiner point is inside, and
    the tip's spine gets medial points (the tip tapers instead of folding over)."""
    tip = _poly([(-0.3, -0.12), (0.5, 0.0), (-0.3, 0.12)])
    b = 0.04
    ol, D = _outline([tip], b)
    pts = H.steiner_points(ol, D, b, 0.0, 6, H.MAX_EDGE, H.TAN_MIN, 1.0)
    assert len(pts) > 30
    inside = H._point_in_ring(pts, ol.rings[0])
    assert inside.all()
    d = ol.query(pts)[0]
    ds = H.ring_distances(b, 0.0, float(D[0]), 6)
    on_ring = np.abs(d[:, None] - ds[None, :]).min(axis=1) < 0.05 * ds.min() + 1e-6
    assert on_ring.mean() > 0.6
    # medial points along the spine toward the tip (y ≈ 0, x well past the inradius centre)
    spine = pts[(np.abs(pts[:, 1]) < 0.01) & (pts[:, 0] > 0.1)]
    assert len(spine) >= 2


def test_merge_points_drops_near_duplicates():
    P = np.array([(0.5, 1e-17), (0.5, -1e-17), (0.5 + 3e-8, 0.0), (0.7, 0.0)])
    out = H.merge_points(P, 2e-6, np.array([(0.7, 1e-9)]))
    assert len(out) == 1 and out[0][0] == pytest.approx(0.5)


def test_dome_and_bevelled_sphere_get_an_apex():
    """The rings stop short of the island's centre (≤ 0.74·D at 6 segments): a dome / bevelled sphere head ended in a
    flat cap 3.4 % low (13 % at 3 segments). The island's inradius centre is added as a Steiner point whenever the
    profile still rises there (inflate, or D < bevel), and never for a flat-topped body or next to a medial point."""
    disc = [G.circle_spline(0.05, -0.02, 0.3)]
    ol, _ = _outline(disc, 0.05)
    centres: dict = {}
    D = H.island_inradius(ol, centres=centres)
    assert list(centres) == [0] and np.hypot(centres[0][0] - 0.05, centres[0][1] + 0.02) < 0.01 * 0.3
    none = np.zeros((0, 2))
    assert len(H._with_apices(none, centres, D, 0.05, 1.0, 6)) == 1         # inflated dome
    assert len(H._with_apices(none, centres, D, 0.35, 0.0, 6)) == 1        # bevel > D: a bevelled sphere / lens
    assert len(H._with_apices(none, centres, D, 0.05, 0.0, 6)) == 0        # flat top beyond the round edge
    near = np.array([(centres[0][0] + 0.005, centres[0][1])])              # a medial point already at the centre
    assert len(H._with_apices(near, centres, D, 0.05, 1.0, 6)) == 1
    # every island gets its own apex
    two = [G.circle_spline(-0.5, 0, 0.2), G.circle_spline(0.5, 0, 0.1)]
    ol, _ = _outline(two, 0.05)
    centres = {}
    D = H.island_inradius(ol, centres=centres)
    apex = H._with_apices(none, centres, D, 0.05, 0.5, 6)
    assert sorted(np.round(apex[:, 0], 1).tolist()) == [-0.5, 0.5]


# ------------------------------------------------------------------------------------------------ Poisson dome (round 7)
def _delaunay(P):
    """CCW triangles of a point set (scipy here; the worker uses mathutils' CDT)."""
    sp = pytest.importorskip("scipy.spatial")
    T = sp.Delaunay(P).simplices.astype(np.int64)
    p = P[T]
    ar = (p[:, 1, 0] - p[:, 0, 0]) * (p[:, 2, 1] - p[:, 0, 1]) - (p[:, 2, 0] - p[:, 0, 0]) * (p[:, 1, 1] - p[:, 0, 1])
    return np.where((ar < 0)[:, None], T[:, [0, 2, 1]], T)


def _disc_mesh(R=0.3, n=72, seed=3):
    rng = np.random.default_rng(seed)
    a = np.linspace(0, 2 * np.pi, n, endpoint=False)
    rim = np.column_stack([R * np.cos(a), R * np.sin(a)])
    r = R * np.sqrt(rng.uniform(0.0, 0.85, 220))
    t = rng.uniform(0, 2 * np.pi, 220)
    P = np.vstack([rim, np.column_stack([r * np.cos(t), r * np.sin(t)])])
    return P, _delaunay(P), np.arange(len(P)) < n


def test_poisson_reproduces_the_disc_exactly():
    """−∇²u = 4, u = 0 on the rim: a disc of radius R has u = R² − r². The cotangent Laplacian with the circumcentric
    load reproduces quadratics, so the solution is exact at every vertex of an IRREGULAR Delaunay mesh."""
    P, T, rim = _disc_mesh()
    u, info = H.poisson(P, T, rim, tol=1e-10)
    exact = 0.3 ** 2 - (P ** 2).sum(1)
    exact[rim] = 0.0
    assert np.abs(u - exact).max() < 1e-7 * 0.09, np.abs(u - exact).max()
    u5, info = H.poisson(P, T, rim)                                    # the default stop: ample for a height field
    assert np.abs(u5 - exact).max() < 1e-3 * 0.09, np.abs(u5 - exact).max()
    assert info["residual"] <= H.POISSON_TOL and 0 < info["iterations"] < 300
    # the vertex gradient of a P1 field: exact for linear fields, ≈ −2r for the disc
    g = H.vertex_gradient(P, T, 2.0 * P[:, 0] - 0.5 * P[:, 1])
    assert np.allclose(g, [2.0, -0.5], atol=1e-9)
    gu = H.vertex_gradient(P, T, u)
    inner = (~rim) & (np.hypot(P[:, 0], P[:, 1]) < 0.2)
    assert np.abs(gu[inner] + 2.0 * P[inner]).max() < 1e-6                 # quadratic fit: exact for the disc


def test_poisson_ellipse_and_separate_parts():
    """An ellipse x²/a² + y²/b² ≤ 1 has u = c(1 − x²/a² − y²/b²), c = 2/(1/a² + 1/b²); two islands are separate parts."""
    a, b = 0.5, 0.15
    rng = np.random.default_rng(7)
    m = 96
    t = np.linspace(0, 2 * np.pi, m, endpoint=False)
    rim = np.column_stack([a * np.cos(t), b * np.sin(t)])
    q = rng.uniform(-1, 1, (900, 2))
    q = q[(q ** 2).sum(1) < 0.8]
    P = np.vstack([rim, q * [a, b]])
    T = _delaunay(P)
    u, _ = H.poisson(P, T, np.arange(len(P)) < m)
    c = 2.0 / (1 / a ** 2 + 1 / b ** 2)
    exact = np.maximum(c * (1 - (P[:, 0] / a) ** 2 - (P[:, 1] / b) ** 2), 0.0)
    assert np.abs(u - exact).max() < 0.03 * c                       # the 96-gon rim cuts the ellipse a little
    # two discs side by side, triangulated apart: two parts, each normalised on its own
    P1, T1, f1 = _disc_mesh(0.2, 48, 1)
    P2, T2, f2 = _disc_mesh(0.1, 48, 2)
    P2 = P2 + [0.6, 0.0]
    PP, TT, ff = np.vstack([P1, P2]), np.vstack([T1, T2 + len(P1)]), np.concatenate([f1, f2])
    comp = H.components(len(PP), TT, ff)
    assert (comp[ff] == -1).all() and len(np.unique(comp[~ff])) == 2
    assert len(np.unique(comp[:len(P1)][~f1])) == 1


def test_dome_rows_sample_thin_parts_across():
    """Inflated bodies get per-ray rows scaled to the LOCAL width: a thin strip gets several rows across (the Poisson
    dome is a round tube there), not one medial line; every point stays inside."""
    strip = _poly([(-0.4, -0.03), (0.4, -0.03), (0.4, 0.03), (-0.4, 0.03)])
    ol, D = _outline([strip], 0.01)
    pts = H.steiner_points(ol, D, 0.01, 1.0, 6, H.MAX_EDGE, H.TAN_MIN, 1.0)
    assert H._point_in_ring(pts, ol.rings[0]).all()
    mid = pts[np.abs(pts[:, 0]) < 0.2]
    rows = np.unique(np.round(np.abs(mid[:, 1]), 4))
    assert len(rows) >= 5, rows                                      # spine + ≥ 4 distances from it
    assert np.isclose(rows.min(), 0.0, atol=1e-3)                    # the spine (medial points)
    flat = H.steiner_points(ol, D, 0.01, 0.0, 6, H.MAX_EDGE, H.TAN_MIN, 1.0)     # no inflate: unchanged sampling
    assert len(np.unique(np.round(np.abs(flat[np.abs(flat[:, 0]) < 0.2][:, 1]), 4))) < len(rows)


# ------------------------------------------------------------------------------------------------ pieces of one layer
def test_touching_pieces_inset_and_overlapping_pieces_stack():
    a = H.piece_rings([G.rect_spline(-0.4, -0.2, 0.0, 0.2)])
    b = H.piece_rings([G.rect_spline(0.0, -0.3, 0.5, 0.3)])          # shares the edge x = 0 with a
    c = H.piece_rings([G.rect_spline(-0.2, -0.1, 0.3, 0.1)])         # overlaps both
    d = H.piece_rings([G.rect_spline(0.7, -0.1, 0.9, 0.1)])          # apart
    tol = 3 * H.CHORD_TOL
    assert H.rings_relation(a, b, tol) == 1 and H.rings_relation(a, c, tol) == 2 and H.rings_relation(a, d, tol) == 0
    ai = H.inset_rings(a, b, 0.003)
    assert np.vstack(ai)[:, 0].max() == pytest.approx(-0.003)       # pulled back from the shared edge ...
    assert np.vstack(ai)[:, 0].min() == pytest.approx(-0.4)         # ... the rest unchanged
    assert H.rings_relation(ai, b, tol) == 0
    sp = H.rings_to_splines(ai)
    assert sp[0]["closed"] and len(sp[0]["points"]) == len(ai[0])
    # real-height stacking: c (half 0.05) on a (half 0.05) on nothing; b only touches -> no shift
    s = H.stack_shifts(3, [(0, 2)], [0.05, 0.05, 0.05], 0.002)
    assert s.tolist() == pytest.approx([0.0, 0.0, 0.102])


def _self_crossings(rings) -> int:
    """Pairs of non-adjacent segments of the rings that properly cross (a valid outline has none)."""
    a, b, _ = H._segments(rings)

    def orient(p, q, r):
        return (q[..., 0] - p[..., 0]) * (r[..., 1] - p[..., 1]) - (q[..., 1] - p[..., 1]) * (r[..., 0] - p[..., 0])
    A0, A1, B0, B1 = a[:, None], b[:, None], a[None], b[None]
    x = (orient(A0, A1, B0) * orient(A0, A1, B1) < 0) & (orient(B0, B1, A0) * orient(B0, B1, A1) < 0)
    return int(np.triu(x, 2).sum())


def _clearance(ra, rb):
    """(min distance of A's vertices to B's outline, min distance of B's vertices to A's outline, A vertices inside B,
    B vertices inside A)."""
    sa, sb = H._segments(ra), H._segments(rb)
    PA, PB = np.vstack(ra), np.vstack(rb)
    return (float(H.nearest(PA, sb[0], sb[1])[0].min()), float(H.nearest(PB, sa[0], sa[1])[0].min()),
            int(H._inside(PA, rb).sum()), int(H._inside(PB, ra).sum()))


@pytest.mark.parametrize("tip", [(0.0, 0.01), (0.0005, 0.013), (-0.0008, 0.021), (0.0, 0.0)])
def test_inset_clears_the_other_pieces_corners(tip):
    """Round 9 (Calendar merged: the page's digit holes vs the digits, 79 intersecting face pairs): a corner of B that
    touches A BETWEEN two of A's outline samples. Pulling back only A's vertices left that corner on (or inside) A's
    outline; the contact stretch now follows B's outline offset by the gap, with B's corner rounded off at radius gap."""
    gap = 0.003
    a = H.piece_rings([G.rect_spline(-0.4, -0.2, 0.0, 0.2)])
    b = H.piece_rings([_poly([tip, (0.3, -0.2), (0.3, 0.2)])])
    assert H.rings_relation(a, b, 3 * H.CHORD_TOL) == 1
    ai = H.inset_rings(a, b, gap)
    da, db, a_in_b, b_in_a = _clearance(ai, b)
    assert a_in_b == 0 and b_in_a == 0
    assert da >= gap * (1 - 1e-4) and db >= 0.85 * gap, (da / gap, db / gap)     # HEAD: db 0.07-0.54 gap, or inside
    assert len(ai[0]) < len(a[0]) + 12                                            # thinned back: a few arc points
    assert np.vstack(ai)[:, 0].min() == pytest.approx(-0.4)                      # the rest of A unchanged
    assert _self_crossings(ai) == 0


def test_inset_at_corners_both_outlines_share():
    """Two pieces of equal height side by side (the shared edge ends at corners of BOTH): the corner points have no
    direction away from B of their own: moved along B's vertex normal they poked out of A as a crossed spike and the
    body failed to build (an element-material split of a merged layer lost a piece)."""
    a = H.piece_rings([G.rect_spline(-0.5, -0.3, 0.0, 0.3)])
    b = H.piece_rings([G.rect_spline(0.0, -0.3, 0.5, 0.3)])
    ai = H.inset_rings(a, b, 0.003)
    r = np.vstack(ai)
    assert r[:, 0].max() == pytest.approx(-0.003) and r[:, 1].min() == pytest.approx(-0.3)
    assert r[:, 1].max() == pytest.approx(0.3) and _self_crossings(ai) == 0
    assert _ring_area(ai[0]) == pytest.approx(0.497 * 0.6, rel=1e-6)


def test_inset_of_a_finer_and_a_coarser_outline_of_one_curve():
    """A disc in a hole of (nearly) the same circle, flattened with different sample counts (the outlines interleave
    within the touch tolerance): every vertex and every chord of A ends up at least ~gap inside the hole."""
    gap = 0.003

    def ngon(n, r, ph):
        return _poly([(r * math.cos(ph + 2 * math.pi * k / n), r * math.sin(ph + 2 * math.pi * k / n)) for k in range(n)])

    disc = H.piece_rings([ngon(36, 0.3, 0.0)])
    page = H.piece_rings([G.rect_spline(-0.6, -0.6, 0.6, 0.6), ngon(52, 0.3, 0.05)])
    assert H.rings_relation(disc, page, 3 * H.CHORD_TOL) == 1
    di = H.inset_rings(disc, page, gap)
    da, db, a_in_b, b_in_a = _clearance(di, page)
    assert a_in_b == 0 and b_in_a == 0 and da >= gap * (1 - 1e-4) and db >= 0.85 * gap, (da / gap, db / gap)
    assert H.rings_relation(di, page, 3 * H.CHORD_TOL) == 0 and _self_crossings(di) == 0


def _subdivided(corners, step, jitter, rng, closed=True):
    """Corners joined by straight runs sampled every ~step (interior samples jittered): one outline, flattened
    differently for each piece that shares it."""
    out = []
    n = len(corners) if closed else len(corners) - 1
    for i in range(n):
        a, b = np.asarray(corners[i], float), np.asarray(corners[(i + 1) % len(corners)], float)
        k = max(1, int(np.ceil(np.linalg.norm(b - a) / step)))
        out += [tuple(a + (b - a) * t / k + (rng.normal(0.0, jitter, 2) if t else 0.0)) for t in range(k)]
    if not closed:
        out.append(tuple(map(float, corners[-1])))
    return out


@pytest.mark.parametrize("half_angle", [12.0, 30.0, 60.0])
def test_inset_cuts_off_the_swallowtail_at_the_other_pieces_concave_corner(half_angle):
    """Review r9: A's tip filling B's V-notch (B's CONCAVE corner). The samples moved onto B's offset from both walls
    overshoot each other past the offset corner, so the outline crossed itself (a swallowtail: 83 of the corpus' 205 inset
    pieces with every layer split, and up to 22,000 points from the refinement in a sharp notch). The loop is cut off
    at the crossing: a simple ring, ≥ gap from B everywhere, bounded size."""
    gap, rng = 0.003, np.random.default_rng(3)
    h = 0.3 * math.tan(math.radians(half_angle))
    wall = [(0.0, -h), (0.3, 0.0), (0.0, h)]
    a = H.piece_rings([_poly([(-0.4, h), (-0.4, -h)] + _subdivided(wall, 0.011, 0.0, rng, closed=False)[:-1]
                             + [(0.0, h)])])
    b = H.piece_rings([_poly([(0.0, h), (0.6, h), (0.6, -h), (0.0, -h)] + _subdivided(wall, 0.017, 0.0, rng,
                                                                                         closed=False)[1:-1])])
    assert H.rings_relation(a, b, 3 * H.CHORD_TOL) == 1
    ai = H.inset_rings(a, b, gap)
    assert _self_crossings(ai) == 0
    da, db, a_in_b, b_in_a = _clearance(ai, b)
    assert a_in_b == 0 and b_in_a == 0 and da >= 0.97 * gap and db >= 0.85 * gap, (da / gap, db / gap)
    tip = np.vstack(ai)[:, 0].max()                    # the true offset corner: gap / sin(half angle) behind B's corner
    assert tip == pytest.approx(0.3 - gap / math.sin(math.radians(half_angle)), abs=0.15 * gap)
    assert sum(len(r) for r in ai) < 2 * sum(len(r) for r in a) + 40


def test_inset_of_randomly_cut_pieces_stays_simple_and_clear():
    """Review r9 fuzz: a square cut in two along a random zigzag / wave, each side flattened with its own spacing and
    jitter (the shared edge then interleaves within the touch tolerance), either side inset from the other: every
    result is a simple ring that keeps at least ~gap from the other piece, chords included."""
    gap, rng = 0.003, np.random.default_rng(11)
    tested = 0
    for it in range(24):
        m = int(rng.integers(3, 20))
        ys = np.linspace(-0.5, 0.5, m)
        amp = float(rng.choice([0.01, 0.05, 0.15]))
        xs = rng.uniform(-amp, amp, m) if it % 2 else amp * np.sin(ys * rng.uniform(3.0, 20.0))
        xs[0] = xs[-1] = 0.0
        cut = list(zip(xs, ys))
        jit = float(rng.choice([0.0, 1e-4, 4e-4]))
        left = _subdivided(cut, rng.uniform(0.005, 0.05), jit, rng, closed=False)
        right = _subdivided(cut, rng.uniform(0.005, 0.05), jit, rng, closed=False)
        a = H.piece_rings([_poly([(-0.5, 0.5), (-0.5, -0.5)] + left)])
        b = H.piece_rings([_poly([(0.5, -0.5), (0.5, 0.5)] + right[::-1])])
        if it % 3 == 0:
            a, b = b, a
        if H.rings_relation(a, b, 3 * H.CHORD_TOL) != 1:
            continue
        tested += 1
        ai = H.inset_rings(a, b, gap)
        assert _self_crossings(ai) == 0, it
        q = np.vstack([r + (np.roll(r, -1, axis=0) - r) * t for r in ai for t in np.linspace(0.0, 1.0, 9)[:-1]])
        sb = H._segments(b)
        assert not H._inside(q, b).any() and not H._inside(np.vstack(b), ai).any(), it
        assert H.nearest(q, sb[0], sb[1])[0].min() >= 0.5 * gap, it
        assert abs(_ring_area(ai[0]) - _ring_area(a[0])) < 0.02 * abs(_ring_area(a[0])), it
    assert tested >= 20


def test_inset_drops_slivers_along_the_other_piece():
    """An outer ring of A that lies wholly within gap of B (≤ 2·gap wide) has nothing left once pulled back. Kept as it
    was it cut into B (Files_1 / DJI / Translate with every piece split: 73-159 intersecting face pairs); left to the
    old vertex moves it became a degenerate outline. It is dropped: an island of a piece with other outline left goes,
    a piece that is only that sliver comes back empty (scene._layer skips it)."""
    gap = 0.003
    b = H.piece_rings([G.rect_spline(0.0, -0.3, 0.4, 0.3)])
    main = G.rect_spline(-0.4, -0.3, 0.0, 0.3)
    sliver = G.rect_spline(0.05, 0.3, 0.2, 0.302)            # 0.002 thin, lying on B's top edge
    a = H.piece_rings([main, sliver])
    assert len(a) == 2 and H.rings_relation(a, b, 3 * H.CHORD_TOL) == 1
    ai = H.inset_rings(a, b, gap)
    assert len(ai) == 1 and np.vstack(ai)[:, 1].max() == pytest.approx(0.3)
    assert np.vstack(ai)[:, 0].max() == pytest.approx(-gap)
    assert H.inset_rings(H.piece_rings([sliver]), b, gap) == []


def test_coincident_outlines_overlap_not_touch():
    """A translucent overlay with (nearly) the base's own outline has every vertex within tol of the other outline:
    it OVERLAPS (stacks), it does not TOUCH. Pulled back as a 'touch', the base grew outward around the overlay
    (0.399 -> 0.403) and the two bodies interpenetrated (538-751 intersecting face pairs in the scene)."""
    tol = 3 * H.CHORD_TOL
    sq = H.piece_rings([G.rect_spline(-0.4, -0.4, 0.4, 0.4)])
    for other in (G.rect_spline(-0.4, -0.4, 0.4, 0.4), G.rect_spline(-0.399, -0.399, 0.399, 0.399),
                  G.rect_spline(-0.4005, -0.4, 0.4, 0.4005)):
        o = H.piece_rings([other])
        assert H.rings_relation(sq, o, tol) == 2 and H.rings_relation(o, sq, tol) == 2
    disc = H.piece_rings([G.circle_spline(0.0, 0.0, 0.3)])
    assert H.rings_relation(disc, H.piece_rings([G.circle_spline(0.0, 0.0, 0.3)]), tol) == 2
    # still a TOUCH: a shared edge, a slight seam overlap (< tol), a disc filling the other's hole exactly
    left = H.piece_rings([G.rect_spline(-0.4, -0.2, 0.0005, 0.2)])
    right = H.piece_rings([G.rect_spline(0.0, -0.3, 0.5, 0.3)])
    assert H.rings_relation(left, right, tol) == 1 and H.rings_relation(right, left, tol) == 1
    ring = H.piece_rings([G.circle_spline(0.0, 0.0, 0.3), G.circle_spline(0.0, 0.0, 0.15)])
    assert H.rings_relation(ring, H.piece_rings([G.circle_spline(0.0, 0.0, 0.15)]), tol) == 1
