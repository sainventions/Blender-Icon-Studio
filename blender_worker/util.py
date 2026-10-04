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
# 'brand' colour mode: Standard view transform + a compositor highlight soft clip (render.configure_compositor)
# ------------------------------------------------------------------------------------------------
# Per channel, scene-linear: identity up to the knee, then an exponential roll-off toward 1.0 (C1 at the knee):
#     y = x                                      x <= k
#     y = 1 - (1 - k) * exp(-(x - k) / (1 - k))  x >  k
# Rims and glints roll off smoothly instead of hard-clipping (and hue-skewing) like plain Standard.
BRAND_KNEE = 0.9


def soft_clip(rgb: Sequence[float], knee: float = BRAND_KNEE) -> tuple[float, float, float]:
    """The 'brand' mode's compositor highlight roll-off (scene-linear -> display-linear, per channel)."""
    w = 1.0 - knee
    return tuple(v if v <= knee else 1.0 - w * math.exp(-(v - knee) / w) for v in (float(c) for c in rgb))  # type: ignore[return-value]


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
