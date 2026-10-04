"""Display-space blending of translucent pieces (round 5). bpy-free (numpy only): unit-tested in the venv.

SVG composites a piece of opacity ``a`` and paint ``P`` over what lies beneath it in gamma-encoded sRGB:

    s_out = (1 - a) * s_B + a * s_P

A path tracer composites scene-linear radiance, and the colour mode's view transform sits between radiance and
the display. A translucent piece with a plain alpha (transparent share 1 - a, paint share a) therefore blends in
the wrong space: a 33 % black ring over a bright blue plate kept far too much of the plate's (pre-compensated,
above-the-knee) blue radiance and read saturated blue (Internet: (51, 63, 179) for the SVG's (52, 58, 130)), and
a 66 % white card washed out what lay beneath.

Translucent Liquid Glass pieces are rendered as a FILM instead (materials._film): per channel the piece transmits
``T`` of the radiance beneath it and adds ``E`` of its own,

    out = T * R_B + E,

with (T, E) the tangent, at an estimate of what lies beneath (:func:`beneath_srgb`: plate fill + lower layers +
lower pieces of the same layer, composited like the SVG), of the exact display-space blend through the colour
mode's view transform (:func:`blend_coeffs`). The blend is exact at the estimate and first-order right where the
beneath varies — plate gradients, the studio shading and cast shadows still show through, scaled like the SVG.
"""
from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

from .util import BRAND_CAP, BRAND_KNEE, hex_to_srgb, pbr_neutral, pbr_neutral_inverse, soft_clip, soft_clip_inverse

FILM_MODES = ("brand", "neutral", "standard")
# Share of the beneath's deviation from the estimate a film passes on (the rest is folded into its emission at
# the estimate): 1 = the exact tangent. What lies beneath an overlay in 3D is often shaded by the pieces around
# it — Notes' curl sits in the page's notch, Messages' dots in the bubble's holes — which the flat SVG never
# shows; the film keeps most of the beneath's variation (gradients, the studio shading) but not all of it.
FILM_FOLLOW = 0.55
SAMPLES = 14                 # sample grid (per side of the piece's bbox) for the beneath estimate


# ------------------------------------------------------------------------------------------------ colour
def eotf(s):
    s = np.clip(np.asarray(s, np.float64), 0.0, 1.0)
    return np.where(s <= 0.04045, s / 12.92, ((s + 0.055) / 1.055) ** 2.4)


def oetf(x):
    x = np.clip(np.asarray(x, np.float64), 0.0, 1.0)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * x ** (1 / 2.4) - 0.055)


def _transform(cm: str, knee: float = BRAND_KNEE):
    """-> (forward: radiance -> display-linear, inverse: display-linear -> radiance) of a colour mode."""
    if cm == "brand":
        return (lambda r: np.asarray(soft_clip(r, knee)),
                lambda d: np.asarray(soft_clip_inverse(d, knee, BRAND_CAP)))
    if cm == "neutral":
        return (lambda r: np.clip(np.asarray(pbr_neutral(np.maximum(r, 0.0))), 0.0, 1.0),
                lambda d: np.asarray(pbr_neutral_inverse(d)))
    return (lambda r: np.clip(np.asarray(r, np.float64), 0.0, 1.0),
            lambda d: np.clip(np.asarray(d, np.float64), 0.0, 1.0))


def blend_coeffs(paint_srgb: Sequence[float], alpha: float, beneath_srgb: Sequence[float], cm: str = "brand",
                 knee: float = BRAND_KNEE, follow: float = 1.0) -> tuple[tuple, tuple]:
    """(T, E) per channel (radiance domain) such that ``T * R_B + E`` displays, through colour mode ``cm``, the SVG
    blend ``(1 - a) s_B + a s_P`` — exactly at the beneath estimate, to first order around it (``follow`` < 1:
    that share of the slope only, the rest folded into E so the estimate itself stays exact)."""
    a = min(1.0, max(0.0, float(alpha)))
    sp = np.clip(np.asarray(paint_srgb, np.float64), 0.0, 1.0)
    sb = np.clip(np.asarray(beneath_srgb, np.float64), 0.0, 1.0)
    fwd, inv = _transform(cm, knee)

    def h(r):
        s = oetf(fwd(r))
        return inv(eotf((1.0 - a) * s + a * sp))

    r0 = inv(eotf(sb))
    h0 = h(r0)
    dr = np.maximum(r0 * 0.02, 2e-4)
    t = np.clip((h(r0 + dr) - h0) / dr, 0.0, 1.0) * min(1.0, max(0.0, float(follow)))  # (PBR Neutral couples
    e = np.maximum(h0 - t * r0, 0.0)                                                     # the channels: together)
    return tuple(float(v) for v in t), tuple(float(v) for v in e)


# ------------------------------------------------------------------------------------------------ paint
def _stops(paint: dict):
    st = sorted(paint.get("stops") or [], key=lambda s: float(s.get("offset", 0.0)))
    if not st:
        return np.array([0.0, 1.0]), np.ones((2, 3)), np.ones(2)
    off = np.array([min(1.0, max(0.0, float(s.get("offset", 0.0)))) for s in st])
    col = np.array([hex_to_srgb(s.get("color", "#000000")) for s in st])
    op = np.array([float(s.get("opacity", 1.0) if s.get("opacity") is not None else 1.0) for s in st])
    return off, col, op


def _ramp(paint: dict, t: np.ndarray):
    off, col, op = _stops(paint)
    t = np.clip(t, 0.0, 1.0)
    rgb = np.stack([np.interp(t, off, col[:, i]) for i in range(3)], -1)      # SVG: sRGB interpolation
    return rgb, np.interp(t, off, op)


def paint_at(paint: dict, pts: np.ndarray, bbox=(-1.0, -1.0, 1.0, 1.0)):
    """sRGB colour (N, 3) + opacity (N,) of a Fill / Paint dict at points (its own coordinate space)."""
    pts = np.asarray(pts, np.float64).reshape(-1, 2)
    n = len(pts)
    t = (paint or {}).get("type", "solid")
    if t == "solid":
        return (np.tile(np.asarray(hex_to_srgb(paint.get("color", "#ffffff"))), (n, 1)),
                np.full(n, float(paint.get("opacity", 1.0) if paint.get("opacity") is not None else 1.0)))
    if t in ("system-light", "system-dark"):
        a, b = ("#ffffff", "#e4e5ea") if t == "system-light" else ("#3a3a3f", "#111114")
        paint = {"type": "linear", "start": (0.0, bbox[3]), "end": (0.0, bbox[1]),
                 "stops": [{"offset": 0.0, "color": a}, {"offset": 1.0, "color": b}]}
        t = "linear"
    if t == "linear":
        sx, sy = paint.get("start") or (0.0, 1.0)
        ex, ey = paint.get("end") or (0.0, -1.0)
        dx, dy = ex - sx, ey - sy
        l2 = dx * dx + dy * dy or 1e-9
        return _ramp(paint, ((pts[:, 0] - sx) * dx + (pts[:, 1] - sy) * dy) / l2)
    if t == "radial":
        cx, cy = paint.get("center") or (0.0, 0.0)
        r = float(paint.get("radius", 1.0)) or 1e-9
        q = pts
        m = paint.get("matrix")
        if m:
            a_, b_, c_, d_, e_, f_ = (float(v) for v in m)
            det = a_ * d_ - b_ * c_ or 1e-12
            x, y = pts[:, 0] - e_, pts[:, 1] - f_
            q = np.stack([(d_ * x - c_ * y) / det, (-b_ * x + a_ * y) / det], -1)
        return _ramp(paint, np.hypot(q[:, 0] - cx, q[:, 1] - cy) / r)
    return np.full((n, 3), 0.5), np.zeros(n)              # 'none' / 'auto' without a known paint


# ------------------------------------------------------------------------------------------------ geometry
def _flatten(points: list, n: int = 4) -> list:
    ring = []
    m = len(points)
    for i in range(m):
        a, b = points[i], points[(i + 1) % m]
        p0, c1, c2, p1 = a["co"], a.get("hr", a["co"]), b.get("hl", b["co"]), b["co"]
        for j in range(n):
            t = j / n
            mt = 1 - t
            ring.append((mt ** 3 * p0[0] + 3 * mt * mt * t * c1[0] + 3 * mt * t * t * c2[0] + t ** 3 * p1[0],
                         mt ** 3 * p0[1] + 3 * mt * mt * t * c1[1] + 3 * mt * t * t * c2[1] + t ** 3 * p1[1]))
    return ring


def _inside(rings: list, pts: np.ndarray) -> np.ndarray:
    """Even-odd point-in-polygon of (N, 2) points against closed rings."""
    inside = np.zeros(len(pts), bool)
    px, py = pts[:, 0][None, :], pts[:, 1][None, :]
    for r in rings:
        if len(r) < 3:
            continue
        r = np.asarray(r, np.float64)
        r1 = np.roll(r, -1, axis=0)
        x0, y0, x1, y1 = r[:, 0][:, None], r[:, 1][:, None], r1[:, 0][:, None], r1[:, 1][:, None]
        cond = (y0 > py) != (y1 > py)
        with np.errstate(divide="ignore", invalid="ignore"):
            xi = x0 + (py - y0) * (x1 - x0) / np.where(np.abs(y1 - y0) < 1e-12, 1e-12, y1 - y0)
        inside ^= (np.count_nonzero(cond & (px < xi), axis=0) % 2).astype(bool)
    return inside


def _to_canvas(art: dict, tr: dict):
    sa, ax, ay = float(art.get("scale", 1.0)), float(art.get("x", 0.0)), float(art.get("y", 0.0))
    sl, tx, ty = float(tr.get("scale", 1.0)), float(tr.get("x", 0.0)), float(tr.get("y", 0.0))
    k = sa * sl
    return k, np.array([ax * sl + tx, ay * sl + ty])


def plate_mask(shape: str, corner_radius: float, pts: np.ndarray) -> np.ndarray:
    x, y = np.abs(pts[:, 0]), np.abs(pts[:, 1])
    if shape == "circle":
        return x * x + y * y <= 1.0
    if shape == "squircle":
        return x ** 5 + y ** 5 <= 1.0
    if shape == "rounded":
        r = max(0.0, min(1.0, 2.0 * float(corner_radius)))
        qx, qy = np.maximum(x - (1 - r), 0.0), np.maximum(y - (1 - r), 0.0)
        return (x <= 1.0) & (y <= 1.0) & (qx * qx + qy * qy <= r * r + 1e-12)
    if shape == "square":
        return (x <= 1.0) & (y <= 1.0)
    return np.zeros(len(pts), bool)


def plate_distance(shape: str, corner_radius: float, pts: np.ndarray, grow: float = 1.0) -> np.ndarray:
    """Approximate signed distance (canvas units, + inside) of points to the plate outline — the same field as
    materials._flush_keep (squircle: 1 - (|x|^5 + |y|^5)^(1/5))."""
    q = np.abs(np.asarray(pts, np.float64).reshape(-1, 2)) / max(grow, 1e-6)
    x, y = q[:, 0], q[:, 1]
    if shape == "circle":
        d = 1.0 - np.hypot(x, y)
    elif shape == "square":
        d = 1.0 - np.maximum(x, y)
    elif shape == "rounded":
        r = max(0.0, min(1.0, 2.0 * float(corner_radius)))
        qx, qy = x - (1.0 - r), y - (1.0 - r)
        d = r - (np.hypot(np.maximum(qx, 0.0), np.maximum(qy, 0.0)) + np.minimum(np.maximum(qx, qy), 0.0))
    else:
        d = 1.0 - (x ** 5 + y ** 5) ** 0.2
    return d * grow


FLUSH_TOL = 0.006           # outline points this close to the plate outline (canvas units) are flush with it


def flush_spec(rings_canvas: list, shape: str, corner_radius: float, bevel: float, grow: float = 1.0) -> Optional[dict]:
    """materials 'flush' spec when a layer's outline runs along the plate outline (Classroom's and CRD's frames,
    DJI's body, Vanced's ring), else None."""
    if shape == "none" or not rings_canvas:
        return None
    pts = np.vstack([np.asarray(r, np.float64) for r in rings_canvas if len(r)])
    if not len(pts):
        return None
    near = np.abs(plate_distance(shape, corner_radius, pts, grow)) < FLUSH_TOL
    if near.sum() < max(6, 0.02 * len(pts)):
        return None
    b = max(0.004, float(bevel))
    return {"shape": shape, "r": max(0.0, min(1.0, 2.0 * float(corner_radius))), "grow": float(grow),
            "band": (round(0.5 * b, 5), round(1.3 * b + 0.004, 5))}


def beneath_srgb(project: dict, bundle: dict, layer_id: str, region_index: int, shape: Optional[str] = None,
                 samples: int = SAMPLES) -> Optional[tuple]:
    """Mean sRGB colour of what the SVG shows beneath region ``region_index`` of layer ``layer_id`` (its interior
    sampled on a grid): the plate fill, every lower visible layer and the lower regions of the same layer,
    composited like the SVG (sRGB, region opacity × layer opacity). None when nothing lies beneath (no plate)."""
    canvas = project.get("canvas") or {}
    art = canvas.get("art") or {}
    layers = [L for L in project.get("layers") or [] if L.get("visible", True)]
    ids = [L["id"] for L in layers]
    if layer_id not in ids:
        return None
    geos = bundle.get("layers") or {}
    me = layers[ids.index(layer_id)]
    g = geos.get(layer_id) or {}
    regions = g.get("regions") or []
    if not 0 <= region_index < len(regions):
        return None
    k, off = _to_canvas(art, me.get("transform") or {})
    rings = [np.asarray(_flatten(s.get("points") or []), np.float64) * k + off
             for s in regions[region_index].get("splines") or [] if len(s.get("points") or []) >= 2]
    rings = [r for r in rings if len(r) >= 3]
    if not rings:
        return None
    allp = np.vstack(rings)
    (x0, y0), (x1, y1) = allp.min(0), allp.max(0)
    gx, gy = np.meshgrid(np.linspace(x0, x1, samples + 2)[1:-1], np.linspace(y0, y1, samples + 2)[1:-1])
    pts = np.stack([gx.ravel(), gy.ravel()], -1)
    pts = pts[_inside(rings, pts)]
    if len(pts) < 3:
        pts = np.array([allp.mean(0)])
    shape = shape or canvas.get("shape", "squircle")
    plate = canvas.get("plate") or {}
    col = np.full((len(pts), 3), np.nan)
    if plate.get("visible", True) and shape != "none" and (plate.get("fill") or {}).get("type") != "none":
        pm = plate_mask(shape, float(canvas.get("cornerRadius", 0.225)), pts)
        pc, _ = paint_at(plate.get("fill") or {"type": "solid", "color": "#ffffff"}, pts)
        col[pm] = pc[pm]
    for L in layers[:ids.index(layer_id) + 1]:
        gl = geos.get(L["id"]) or {}
        fill = L.get("fill") or {"type": "auto"}
        if fill.get("type") == "none":
            continue
        kl, ol = _to_canvas(art, L.get("transform") or {})
        lop = float(L.get("opacity", 1.0))
        regs = gl.get("regions") or []
        if L["id"] == layer_id:
            regs = regs[:region_index]
        art_pts = (pts - ol) / max(kl, 1e-9)
        for r in regs:
            rr = [np.asarray(_flatten(s.get("points") or []), np.float64) for s in r.get("splines") or []
                  if len(s.get("points") or []) >= 2]
            hit = _inside([q for q in rr if len(q) >= 3], art_pts)
            if not hit.any():
                continue
            paint = fill if fill.get("type") != "auto" else (r.get("paint") or {})
            pc, po = paint_at(paint, art_pts[hit], tuple(gl.get("bbox") or (-1, -1, 1, 1)))
            a = np.clip(po * float(r.get("opacity", 1.0)) * lop, 0.0, 1.0)[:, None]
            below = col[hit]
            below = np.where(np.isnan(below), pc, below)
            col[hit] = (1.0 - a) * below + a * pc
    ok = ~np.isnan(col).any(axis=1)
    if not ok.any():
        return None
    return tuple(float(v) for v in col[ok].mean(0))


def film_params(paint_srgb, alpha: float, beneath: Optional[Sequence[float]], cm: str) -> dict:
    """-> {'t': (r, g, b), 'e': (r, g, b)} for a translucent piece (beneath None: a mid-grey estimate)."""
    t, e = blend_coeffs(paint_srgb, alpha, beneath if beneath is not None else (0.5, 0.5, 0.5), cm,
                        follow=FILM_FOLLOW)
    return {"t": t, "e": e}

