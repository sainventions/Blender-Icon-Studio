"""2D outline helpers of the Blender worker (blender_worker/geometry.py): plate outlines, test / swatch shapes,
flattening and scanline rasters — pure Python, no Blender needed. Bodies themselves are height fields
(test_heightfield.py / test_heightfield_corpus.py); the retired curve-bevel route (fillets, guard points, safe
radii, adaptive curve resolution) has no tests any more."""
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


def _ring(splines, tol=H.CHORD_TOL):
    rings, _shapes, hole, _isl = H.outline(splines, tol, H.MAX_EDGE, H.MERGE_EPS)
    return rings, hole


def _area(r):
    x, y = r[:, 0], r[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y))


@pytest.mark.parametrize("shape,area", [("square", 4.0), ("circle", math.pi), ("squircle", None),
                                        ("rounded", 4.0 - (4 - math.pi) * 0.45 ** 2)])
def test_plate_outlines_fill_the_canvas(shape, area):
    """Every plate outline spans the canvas −1..1 exactly and is one closed CCW ring."""
    spl = G.plate_outline(shape, 0.225)
    assert len(spl) == 1 and spl[0]["closed"]
    (r,), hole = _ring(spl)
    assert not hole[0] and _area(r) > 0
    assert np.allclose([r[:, 0].min(), r[:, 1].min(), r[:, 0].max(), r[:, 1].max()], [-1, -1, 1, 1], atol=2e-3)
    if area is not None:
        assert _area(r) == pytest.approx(area, rel=2e-3)
    else:
        assert math.pi < _area(r) < 4.0                     # between the circle and the square


def test_rounded_plate_corner_radius_and_degenerate_radius():
    (r,), _ = _ring(G.rounded_rect_splines(0.225))
    # the corner arc (radius 0.45 about (0.55, 0.55)) stays on its circle
    q = r[(r[:, 0] > 0.56) & (r[:, 1] > 0.56)]
    assert len(q) > 4 and np.allclose(np.hypot(q[:, 0] - 0.55, q[:, 1] - 0.55), 0.45, atol=1e-3)
    (sq,), _ = _ring(G.plate_outline("rounded", 0.0))           # radius 0: the square
    assert _area(sq) == pytest.approx(4.0, rel=1e-6)


def test_test_shapes_are_closed_and_holes_nest():
    disc = G.circle_spline(0.1, -0.2, 0.3)
    hole = G.circle_spline(0.1, -0.2, 0.1, hole=True)
    rings, holes = _ring([disc, hole])
    assert holes.tolist() == [False, True]
    assert _area(rings[0]) == pytest.approx(math.pi * 0.09, rel=2e-3)
    assert _area(rings[1]) == pytest.approx(-math.pi * 0.01, rel=1e-2)     # holes run clockwise (chord tol 6e-4)
    (rect,), _ = _ring([G.rect_spline(-0.5, -0.1, 0.5, 0.2)])
    assert _area(rect) == pytest.approx(0.3)
    (tri,), _ = _ring([G.poly_spline([(0, 0), (1, 0), (0, 1)])])
    assert _area(tri) == pytest.approx(0.5)


def test_count_open_flags_only_open_subpaths():
    a = dict(G.circle_spline(0, 0, 0.2), closed=False)
    b = G.circle_spline(0.5, 0, 0.1)
    c = {"closed": False, "points": [{"co": [0, 0]}]}          # a single point: not a fill
    assert G.count_open([a, b, c]) == 1


def test_flatten_ring_follows_the_bezier():
    s = G.circle_spline(0, 0, 0.5)
    ring = np.asarray(G._flatten_ring(s["points"], True, 8))
    assert len(ring) == 32
    assert np.allclose(np.hypot(ring[:, 0], ring[:, 1]), 0.5, atol=0.5 * 3e-4)   # cubic circle error 0.027 %


def test_scanline_raster_area():
    sq = [np.array([(0, 0), (1, 0), (1, 1), (0, 1)], dtype=float)]
    hole = [np.array([(0.25, 0.25), (0.75, 0.25), (0.75, 0.75), (0.25, 0.75)], dtype=float)]
    xs = ys = (np.arange(200) + 0.5) / 200
    m = G._scan_inside(G._ring_segments(sq + hole), xs, ys)
    assert abs(m.mean() - 0.75) < 0.01
