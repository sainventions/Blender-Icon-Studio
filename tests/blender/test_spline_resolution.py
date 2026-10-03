"""Adaptive bezier resolution (blender_worker.geometry.spline_resolution) — pure maths, no Blender needed
(``bpy`` is stubbed only while the module is imported; the function never touches it)."""
from __future__ import annotations

import importlib
import math
import sys
import types
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def _geometry():
    stub = "bpy" not in sys.modules
    if stub:
        sys.modules["bpy"] = types.ModuleType("bpy")
    try:
        return importlib.import_module("blender_worker.geometry")
    finally:
        if stub:
            del sys.modules["bpy"]


G = _geometry()
K = 0.5522847498


def _chord_error(points, n):
    """Max distance between the cubic segments and their n-chord polylines (sampled)."""
    worst = 0.0
    m = len(points)
    for i in range(m):
        a, b = points[i], points[(i + 1) % m]
        P = np.array([a["co"], a["hr"], b["hl"], b["co"]], dtype=float)
        t = np.linspace(0, 1, 8 * n + 1)[:, None]
        curve = ((1 - t) ** 3) * P[0] + 3 * ((1 - t) ** 2) * t * P[1] + 3 * (1 - t) * t * t * P[2] + t ** 3 * P[3]
        knots = curve[::8]
        for j in range(n):
            seg = curve[8 * j:8 * j + 9]
            p0, p1 = knots[j], knots[j + 1]
            d = p1 - p0
            L = np.hypot(*d) or 1e-12
            worst = max(worst, float(np.abs((seg[:, 0] - p0[0]) * d[1] - (seg[:, 1] - p0[1]) * d[0]).max() / L))
    return worst


def test_circle_keeps_full_resolution():
    r = 0.6
    pts = [{"co": [r, 0], "hl": [r, -K * r], "hr": [r, K * r]}, {"co": [0, r], "hl": [K * r, r], "hr": [-K * r, r]},
           {"co": [-r, 0], "hl": [-r, K * r], "hr": [-r, -K * r]}, {"co": [0, -r], "hl": [-K * r, -r], "hr": [K * r, -r]}]
    assert G.spline_resolution(pts) == G.RES_MAX


def test_one_big_arc_among_many_straight_segments_stays_smooth():
    """Play Store / Ti84 / Home: one rounded tip among 30+ straight segments must not collapse into a chord."""
    r = 0.5
    pts = [{"co": [r, 0], "hl": [r, -K * r], "hr": [r, K * r]}, {"co": [0, r], "hl": [K * r, r], "hr": [0, r]}]
    # back along a zig-zag of 40 straight segments to the start
    for k in range(1, 41):
        t = k / 41
        p = [(1 - t) * 0 + t * r + (0.002 if k % 2 else 0), (1 - t) * r - 0.3 * math.sin(math.pi * t)]
        pts.append({"co": p, "hl": p, "hr": p})
    pts[0]["hl"] = [r, -K * r]
    n = G.spline_resolution(pts)
    assert _chord_error(pts, n) < 0.002, n          # ≤ 0.5 px at 512 px
    assert n >= 8


def test_dense_traced_contour_gets_few_subdivisions():
    t = np.linspace(0, 2 * np.pi, 300, endpoint=False)
    ring = np.column_stack([0.7 * np.cos(t), 0.5 * np.sin(t)])
    pts = []
    for i in range(len(ring)):
        p, q, o = ring[i], ring[(i + 1) % len(ring)], ring[i - 1]
        tan = (q - o) / 6.0
        pts.append({"co": list(p), "hl": list(p - tan), "hr": list(p + tan)})
    n = G.spline_resolution(pts)
    assert n <= 2, n
    assert _chord_error(pts, n) < 0.002


def test_vertex_budget_caps_pathological_contours():
    rng = np.random.default_rng(3)
    t = np.linspace(0, 2 * np.pi, 2000, endpoint=False)
    ring = np.column_stack([0.7 * np.cos(t), 0.7 * np.sin(t)]) + rng.normal(scale=0.01, size=(2000, 2))
    pts = [{"co": list(p), "hl": list(p + rng.normal(scale=0.02, size=2)), "hr": list(p + rng.normal(scale=0.02, size=2))}
           for p in ring]
    assert G.spline_resolution(pts) == 1
