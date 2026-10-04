"""Small helpers shared by the worker modules (colour maths, logging, hashing). bpy-free."""
from __future__ import annotations

import hashlib
import json
import math
import sys
import time
from typing import Iterable, Sequence


def log(*args) -> None:
    """Diagnostics go to stderr so stdout stays reserved for protocol lines."""
    print("[bis-worker]", *args, file=sys.stderr, flush=True)


def srgb_to_linear(c: float) -> float:
    c = max(0.0, min(1.0, float(c)))
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def linear_to_srgb(c: float) -> float:
    c = max(0.0, float(c))
    return c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def hex_to_srgb(h: str) -> tuple[float, float, float]:
    h = (h or "#000000").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    if len(h) < 6:
        h = (h + "000000")[:6]
    try:
        return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return (0.0, 0.0, 0.0)


def hex_to_linear(h: str) -> tuple[float, float, float]:
    r, g, b = hex_to_srgb(h)
    return (srgb_to_linear(r), srgb_to_linear(g), srgb_to_linear(b))


def rgba(h: str, a: float = 1.0) -> tuple[float, float, float, float]:
    return (*hex_to_linear(h), float(a))


def luminance_linear(rgb: Sequence[float]) -> float:
    """Rec.709 luminance of a LINEAR colour (Blender's RGB to BW uses the scene's coefficients)."""
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def lerp(a, b, t):
    if isinstance(a, (tuple, list)):
        return tuple(x + (y - x) * t for x, y in zip(a, b))
    return a + (b - a) * t


def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def stable_hash(obj, n: int = 16) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:n]


def light_dir(angle_deg: float, elev_deg: float) -> tuple[float, float, float]:
    """Unit vector from the icon toward a light (PLAN D1 axes: icon in XY, camera on +Z).

    angle 0 = from the top (+Y), positive = clockwise (toward +X); elevation = degrees away from the
    view axis (0 = from the camera, 90 = grazing)."""
    a, e = math.radians(angle_deg), math.radians(elev_deg)
    v = (math.sin(a) * math.sin(e), math.cos(a) * math.sin(e), math.cos(e))
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return (v[0] / n, v[1] / n, v[2] / n)


# ------------------------------------------------------------------------------------------------
# Khronos PBR Neutral (the 'neutral' colour mode's view transform) and its paint pre-compensation
# ------------------------------------------------------------------------------------------------
PBR_START = 0.76          # 0.8 - 0.04: highlight compression starts at this (offset) peak
PBR_DESAT = 0.15
# Target-peak caps of the inverse: a near-neutral paint may ask for a peak up to 0.975 (white glyphs display
# at ~252/255); a fully saturated one at most 0.86 — the transform desaturates every colour whose peak it has
# to compress, so beyond that point the hue error grows faster than the lightness gain (Brave #ff3b00: best
# achievable ΔE76 ≈ 9 under PBR Neutral). Interpolated by saturation^12 (fitted on the corpus' 197 paints:
# the optimum peak stays ≥ 0.93 up to saturation ~0.95, then drops).
NEUTRAL_CAP = (0.975, 0.86)
NEUTRAL_CAP_POW = 12.0


def pbr_neutral(rgb: Sequence[float]) -> tuple[float, float, float]:
    """Khronos PBR Neutral tone mapping (scene-linear Rec.709 -> display-linear), reference GLSL port."""
    c = [float(v) for v in rgb]
    x = min(c)
    off = x - 6.25 * x * x if x < 0.08 else 0.04
    c = [v - off for v in c]
    peak = max(c)
    if peak < PBR_START:
        return (c[0], c[1], c[2])
    d = 1.0 - PBR_START
    new_peak = 1.0 - d * d / (peak + d - PBR_START)
    c = [v * new_peak / peak for v in c]
    g = 1.0 - 1.0 / (PBR_DESAT * (peak - new_peak) + 1.0)
    return tuple(v * (1.0 - g) + new_peak * g for v in c)  # type: ignore[return-value]


def pbr_neutral_inverse(rgb: Sequence[float], cap: Sequence[float] = NEUTRAL_CAP,
                        max_peak: float = 1.0) -> tuple[float, float, float]:
    """Scene-linear radiance that PBR Neutral displays as the display-linear colour ``rgb`` (best effort:
    peaks are capped by saturation, see NEUTRAL_CAP, and by ``max_peak``). Mirrors the node graph of
    materials.display_paint."""
    y = [max(0.0, float(v)) for v in rgb]
    mx, mn = max(y), min(y)
    sat = 1.0 - min(1.0, mn / max(mx, 1e-5))
    cp = min(max_peak, cap[0] + (cap[1] - cap[0]) * sat ** NEUTRAL_CAP_POW)
    npk = min(mx, cp)
    y = [v * npk / max(mx, 1e-5) for v in y]
    if npk > PBR_START:
        d = 1.0 - PBR_START
        peak = PBR_START - d + d * d / (1.0 - npk)
        inv = PBR_DESAT * (peak - npk) + 1.0          # 1 / (1 - g)
        g = 1.0 - 1.0 / inv
        x1 = [max(0.0, (v - g * npk) * inv) * peak / max(npk, 1e-5) for v in y]
    else:
        x1 = y
    m = min(x1)
    off = 0.04 if m >= 0.04 else 0.4 * math.sqrt(max(m, 0.0)) - m
    return tuple(v + off for v in x1)  # type: ignore[return-value]


# ------------------------------------------------------------------------------------------------
# 'brand' colour mode: Standard view transform + a compositor highlight soft clip (render.configure_compositor)
# ------------------------------------------------------------------------------------------------
# Per channel, scene-linear: identity up to the knee, then an exponential roll-off toward 1.0 (C1 at the knee):
#     y = x                                      x <= k
#     y = 1 - (1 - k) * exp(-(x - k) / (1 - k))  x >  k
# Paints are pre-compensated with the exact inverse (identity below the knee), so every SVG colour displays
# exactly — saturated brand colours included (Brave #ff3b00 is out of Khronos PBR Neutral's gamut) — while
# specular rims and glints roll off smoothly instead of hard-clipping (and hue-skewing) like plain Standard.
BRAND_KNEE = 0.9
BRAND_CAP = 0.998           # brightest displayed target of the inverse (255/255 after 8-bit rounding)


def soft_clip(rgb: Sequence[float], knee: float = BRAND_KNEE) -> tuple[float, float, float]:
    """The 'brand' mode's compositor highlight roll-off (scene-linear -> display-linear, per channel)."""
    w = 1.0 - knee
    return tuple(v if v <= knee else 1.0 - w * math.exp(-(v - knee) / w) for v in (float(c) for c in rgb))  # type: ignore[return-value]


def soft_clip_inverse(rgb: Sequence[float], knee: float = BRAND_KNEE, cap: float = BRAND_CAP) -> tuple[float, float, float]:
    """Scene-linear radiance that the 'brand' mode displays as the display-linear colour ``rgb`` (targets capped
    at ``cap``: 1.0 itself needs infinite radiance). Mirrors materials._brand_graph."""
    w = 1.0 - knee
    out = []
    for v in rgb:
        y = min(max(0.0, float(v)), cap)
        out.append(y if y <= knee else knee - w * math.log(1.0 - (y - knee) / w))
    return tuple(out)  # type: ignore[return-value]


class Timer:
    def __init__(self):
        self.t0 = time.perf_counter()
        self.marks: dict[str, float] = {}

    def mark(self, name: str) -> float:
        dt = time.perf_counter() - self.t0
        self.marks[name] = round(dt, 4)
        return dt

    def elapsed(self) -> float:
        return time.perf_counter() - self.t0


def gradient_samples(stops: Iterable[dict], per_interval: int = 3, limit: int = 32) -> list[tuple[float, tuple, float]]:
    """SVG gradient stops (sRGB hex) -> (pos, linear rgb, alpha) samples for a Blender colour ramp.

    SVG interpolates in sRGB while Blender ramps interpolate the (linear) values given, so each interval
    is densified with sRGB-interpolated samples converted to linear (blender_curve_builder.py)."""
    st = sorted((dict(s) for s in stops), key=lambda s: float(s.get("offset", 0.0)))
    if not st:
        return [(0.0, (1.0, 1.0, 1.0), 1.0), (1.0, (1.0, 1.0, 1.0), 1.0)]
    out = []
    for i, s in enumerate(st):
        c0 = hex_to_srgb(s.get("color", "#000000"))
        o0 = float(s.get("opacity", 1.0))
        p0 = clamp(float(s.get("offset", 0.0)))
        out.append((p0, tuple(srgb_to_linear(c) for c in c0), o0))
        if i + 1 < len(st):
            n = st[i + 1]
            c1 = hex_to_srgb(n.get("color", "#000000"))
            o1 = float(n.get("opacity", 1.0))
            p1 = clamp(float(n.get("offset", 0.0)))
            if p1 - p0 > 1e-4:
                for k in range(1, per_interval + 1):
                    t = k / (per_interval + 1)
                    out.append((p0 + (p1 - p0) * t, tuple(srgb_to_linear(x + (y - x) * t) for x, y in zip(c0, c1)),
                                o0 + (o1 - o0) * t))
    if len(out) > limit:          # keep the real stops, thin the interpolated ones
        step = len(out) / limit
        out = [out[min(len(out) - 1, int(i * step))] for i in range(limit - 1)] + [out[-1]]
    if len(out) == 1:
        out.append((1.0, out[0][1], out[0][2]))
    return out
