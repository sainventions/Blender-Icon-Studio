"""2D outline helpers (bpy-free): plate outlines, simple test / swatch shapes, flattening and scanline rasters.

Every BODY — layer pieces, silhouettes, raster contours, image cards and the plate — is a height-field mesh built
by :mod:`blender_worker.heightfield` (PLAN §11 Geometry). The curve-bevel route (round curve bevel with
``offset = −bevel``, convex-corner fillets, guard points, safe-radius clamps, the Fill Curve → Solidify → Bevel
fallback and the ray-cast cap verification) is retired: thin parts and tips taper by construction there.
"""
from __future__ import annotations

import math


def count_open(splines: list) -> int:
    """Splines flagged ``closed: false`` (SVG subpaths without 'Z', e.g. ending in 'h0'). Fills: the height
    field closes them with a straight segment."""
    return sum(1 for s in splines if not s.get("closed", True) and len(s.get("points") or []) > 1)


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



# ------------------------------------------------------------------------------------------------
# flattening / scanline rasters (scene: covered-piece test, framing hulls)
# ------------------------------------------------------------------------------------------------
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
