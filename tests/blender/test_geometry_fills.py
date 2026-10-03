"""Geometry hygiene of the Blender worker (QA round 2: hollow fills, inverted / growing bevels, pinched acute
corners) and the appearance rules of the clear renditions — pure Python, no Blender needed (``bpy`` is stubbed
only while the modules are imported)."""
from __future__ import annotations

import importlib
import math
import sys
import types
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def _import(name):
    stub = "bpy" not in sys.modules
    if stub:
        sys.modules["bpy"] = types.ModuleType("bpy")
    try:
        return importlib.import_module(name)
    finally:
        if stub:
            del sys.modules["bpy"]


G = _import("blender_worker.geometry")
A = _import("blender_worker.appearance")
K = 0.5522847498


def _poly(corners, closed=True):
    s = G.poly_spline(corners)
    s["closed"] = closed
    return s


def _area(spline, n=12):
    ring = G._flatten_ring(spline["points"], True, n)
    return 0.5 * sum(ring[i - 1][0] * ring[i][1] - ring[i][0] * ring[i - 1][1] for i in range(len(ring)))


def _dist_to_poly(poly, p):
    best = 1e9
    for a, b in zip(poly, poly[1:] + poly[:1]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)))
        best = min(best, math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy))
    return best


def _inside(poly, p):
    """even-odd point-in-polygon"""
    x, y = p
    c = False
    for i in range(len(poly)):
        (x0, y0), (x1, y1) = poly[i - 1], poly[i]
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            c = not c
    return c


# ------------------------------------------------------------------------------------------------ fills
def test_open_subpath_with_coincident_end_is_closed():
    """Canvas / Calculator dots: SVG paths without 'Z' that end with 'h0' arrive as closed:false with the last
    point on the first. They are fills: the spline must come back closed (it used to be swept as a hollow tube)."""
    k = 0.1 * K
    pts = [{"co": [0.1, 0], "hl": [0.1, -k], "hr": [0.1, k]}, {"co": [0, 0.1], "hl": [k, 0.1], "hr": [-k, 0.1]},
           {"co": [-0.1, 0], "hl": [-0.1, k], "hr": [-0.1, -k]}, {"co": [0, -0.1], "hl": [-k, -0.1], "hr": [k, -0.1]},
           {"co": [0.1, 0], "hl": [0.1, -k], "hr": [0.1, 0]}]
    sp = {"closed": False, "points": pts}
    assert G.count_open([sp]) == 1
    (out,) = G.sanitize([sp])
    assert out["closed"] is True and len(out["points"]) == 4
    assert abs(abs(_area(out)) - math.pi * 0.01) < 2e-3          # a full disc, not a sliver


def test_open_subpath_with_gap_gets_straight_closing_segment():
    """Gmail: a 3-point spline left open with a gap — SVG fills it as if closed by a straight line."""
    sp = _poly([(0, 0), (0.4, 0), (0.4, 0.3)], closed=False)
    sp["points"][-1]["hr"] = [9.0, 9.0]       # dangling handles of an open path are meaningless
    sp["points"][0]["hl"] = [-9.0, -9.0]
    (out,) = G.sanitize([sp])
    assert out["closed"] is True and len(out["points"]) == 3
    assert abs(abs(_area(out)) - 0.5 * 0.4 * 0.3) < 1e-6        # the straight-closed triangle


# ------------------------------------------------------------------------------------------------ corners
def test_collinear_runs_are_joined():
    """Boolean-op outlines split straight edges into short collinear segments; fillets are limited by the
    adjacent segment lengths, so acute corners between such runs stayed sharp (Home, Drive specks)."""
    corners = [(0, 0), (0.2, 0), (0.4, 0), (0.6, 0), (0.6, 0.15), (0.6, 0.3), (0.3, 0.3), (0, 0.3), (0, 0.15)]
    (out,) = G.merge_collinear([_poly(corners)])
    assert len(out["points"]) == 4
    # curved segments are never merged
    circ = G.circle_spline(0, 0, 0.3)
    assert len(G.merge_collinear([circ])[0]["points"]) == 4


def test_fillets_never_grow_the_silhouette():
    """Only convex corners are filleted: a fillet on a concave corner adds material outside the outline (the
    inner corners of Calculator's '+' grew)."""
    plus = [(-0.1, 0.3), (0.1, 0.3), (0.1, 0.1), (0.3, 0.1), (0.3, -0.1), (0.1, -0.1), (0.1, -0.3), (-0.1, -0.3),
            (-0.1, -0.1), (-0.3, -0.1), (-0.3, 0.1), (-0.1, 0.1)]
    (f,) = G.fillet_corners([_poly(plus)], 0.05, min_radius=0.04)
    ring = G._flatten_ring(f["points"], True, 16)
    # every point of the filleted outline lies inside (or on) the original outline
    outside = [p for p in ring if not _inside(plus, p) and _dist_to_poly(plus, p) > 1e-6]
    assert not outside, outside[:3]
    assert abs(_area(f)) < abs(_area(_poly(plus)))
    # the 4 concave corners stay exactly where they were
    cos = {tuple(round(c, 6) for c in p["co"]) for p in f["points"]}
    for c in ((0.1, 0.1), (0.1, -0.1), (-0.1, -0.1), (-0.1, 0.1)):
        assert c in cos


def test_unfilletable_acute_corner_is_reported():
    """A small triangle's acute tips cannot take a fillet of the bevel radius; they are reported (the worker then routes
    the piece to the Bevel-modifier mesh path instead of pinching the round curve bevel)."""
    tri = [(0, 0), (0.2, 0), (0.1, 0.12)]           # 50° tips on short edges
    skipped: list = []
    G.fillet_corners([_poly(tri)], 0.05, min_radius=0.04, skipped=skipped)
    assert skipped and max(skipped) > G.ACUTE_GN_DEG


def test_sharp_corners_get_guard_points_without_changing_the_outline():
    """Sheets' rectangular cells: the concave corners stay sharp, and the straight edges next to them get guard
    points at the bevel distance (the round bevel's mitre normal was interpolated along the whole edge)."""
    hole = _poly([(-0.35, 0.15), (-0.35, -0.15), (0.35, -0.15), (0.35, 0.15)])     # CW
    hole["hole"] = True
    outer = _poly([(-0.6, -0.4), (0.6, -0.4), (0.6, 0.4), (-0.6, 0.4)])
    shaped = G.fillet_corners([outer, hole], 0.054, min_radius=0.045)
    g_outer, g_hole = G.guard_corners(G.sanitize(shaped), 0.045)
    # the hole's 4 sharp corners: 2 guards per edge, at 0.045 from each corner, on the edge itself
    assert len(g_hole["points"]) == 12
    cos = [tuple(round(c, 6) for c in p["co"]) for p in g_hole["points"]]
    for c in ((-0.35, 0.15), (-0.35, -0.15), (0.35, -0.15), (0.35, 0.15)):
        assert c in cos
    assert (-0.305, -0.15) in cos and (0.35, 0.105) in cos
    assert abs(_area(g_hole) - _area(hole)) < 1e-9
    pts = g_hole["points"]              # every segment stays a straight line (handles on the chord)
    for i in range(len(pts)):
        q = pts[(i + 1) % len(pts)]
        assert G._seg_straight(pts[i]["co"], pts[i]["hr"], q["hl"], q["co"], 1e-9)
    # the filleted outer outline has no sharp corner left: untouched
    assert len(g_outer["points"]) == len(G.sanitize(shaped)[0]["points"])
    # smooth outlines and short edges are left alone
    circ = G.circle_spline(0, 0, 0.3)
    assert G.guard_corners([circ], 0.045)[0]["points"] == circ["points"]
    tiny = _poly([(0, 0), (0.01, 0), (0.01, 0.01), (0, 0.01)])
    assert len(G.guard_corners([tiny], 0.045)[0]["points"]) == 12      # guards at 0.3 x the short edge


def test_piece_safe_radius_of_a_thin_ring():
    """Ti73's pie ring (width 0.02) sat in a layer whose safeRadius was 0.27: the bevel inverted the ring and
    filled its hole. The per-piece safe radius sees the thin ring."""
    ring = [G.circle_spline(0, 0, 0.19), G.circle_spline(0, 0, 0.17, hole=True)]
    sr = G.piece_safe_radius(ring)
    assert 0.004 < sr < 0.013, sr
    assert G.piece_safe_radius([G.circle_spline(0, 0, 0.3)]) > 0.1      # far above any bevel


def test_scanline_raster_area():
    sq = [np.array([(0, 0), (1, 0), (1, 1), (0, 1)], dtype=float)]
    hole = [np.array([(0.25, 0.25), (0.75, 0.25), (0.75, 0.75), (0.25, 0.75)], dtype=float)]
    xs = ys = (np.arange(200) + 0.5) / 200
    m = G._scan_inside(G._ring_segments(sq + hole), xs, ys)
    assert abs(m.mean() - 0.75) < 0.01


# ------------------------------------------------------------------------------------------------ clear renditions
def _project():
    return {"layers": [{"id": "L0", "visible": True, "material": {"preset": "liquid_glass", "params": {}},
                        "shadow": {"kind": "neutral", "opacity": 0.3}, "fill": {"type": "auto"}}],
            "canvas": {"platform": "ios", "plate": {"fill": {"type": "solid", "color": "#3366ff"},
                                                    "material": {"preset": "satin", "params": {}}}},
            "appearances": {}}


def test_clear_light_glyph_separates_from_the_plate():
    res = A.resolve(_project(), "clear-light", {"layers": {}})
    L = res["project"]["layers"][0]
    assert L["material"]["params"]["tint"] == A.CLEAR_TINT > 0          # mono grey tints the glass
    assert L["material"]["params"]["glow"] == A.CLEAR_GLOW_LIGHT
    assert L["shadow"]["opacity"] >= A.CLEAR_LIGHT_SHADOW
    assert res["env"]["edgeDark"] == A.CLEAR_EDGE_DARK > 0
    plate = res["project"]["canvas"]["plate"]
    assert plate["fill"]["color"] == A.CLEAR_LIGHT_PLATE and plate["material"]["preset"] == "frosted_glass"


def test_clear_dark_keeps_its_rim_and_glow():
    res = A.resolve(_project(), "clear-dark", {"layers": {}})
    L = res["project"]["layers"][0]
    assert res["env"]["edgeDark"] == 0.0
    assert L["material"]["params"]["glow"] == A.CLEAR_GLOW_DARK
    assert L["shadow"]["opacity"] == 0.3
    assert res["project"]["canvas"]["plate"]["fill"]["color"] == "#ffffff"
