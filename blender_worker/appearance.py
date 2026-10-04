"""The six Icon Composer renditions (PLAN §5) resolved into an *effective* project + environment.

* light        — base project.
* dark         — ``appearances.dark`` overrides (default plate fill system-dark); env × 0.6, key × 0.85.
* clear-*      — ``appearances.mono`` overrides; every visible layer → liquid_glass whose paint is the mono
                 luminance (stretched to MONO_FLOOR..1, brightest → white) tinting the glass (dark parts stay
                 dark-ish smoked glass instead of turning white) and driving the milky-white frost; plate →
                 frosted glass (tint 0) over a wallpaper backdrop. clear-light separates the white glyph from
                 the light plate with a darker lensed rim (env ``edgeDark``) and a deeper drop shadow; the
                 inner glow is low on the light plate and moderate on the dark one.
* tinted-light — mono luminance × tint colour into the glass base colour; plate = light frosted, tinted.
* tinted-dark  — plate system-dark; foreground = mono luminance × tint (bright, slightly emissive).

watchOS ignores appearances (always light). Per-layer LayerOverride fields replace base values.
"""
from __future__ import annotations

import copy

from .defaults import norm_fill
from .util import hex_to_linear, hex_to_srgb, lerp, srgb_to_linear

WALLPAPERS = {
    # soft pastel (light) / deep blue-black (dark) gradient + colour blobs, all sRGB hex
    "light": {"top": "#dfe9ff", "bottom": "#f7e8f2",
              "blobs": [(-0.9, 0.7, 0.9, "#9ec5ff"), (0.95, -0.55, 0.85, "#ffc7dc"), (0.2, 1.1, 0.6, "#c8f1e6")]},
    "dark": {"top": "#0b1630", "bottom": "#05060c",
             "blobs": [(-0.85, 0.6, 0.9, "#1d3f8f"), (0.9, -0.6, 0.85, "#3b1d6e"), (0.25, 1.15, 0.6, "#0f4a5c")]},
}

MONO_FLOOR = 0.3
# clear renditions: glass colour = mono luminance (tint 0.5 -> 94 % of the grey), clear-light rim darkening
# (fraction of the transmission removed at the outline), drop-shadow floor and inner glow per mode
CLEAR_TINT = 0.5
CLEAR_EDGE_DARK = 0.8
CLEAR_LIGHT_SHADOW = 0.8
CLEAR_GLOW_LIGHT = 0.1
CLEAR_GLOW_DARK = 0.3
CLEAR_LIGHT_PLATE = "#c9ccd6"
CLEAR_LIGHT_PLATE_TINT = 0.4
# round 5: clear renditions lift mid-lightness glyph parts toward a bright frosted white (Photos' petals / Maps' pin
# melted into the pane), and the clear-light pane is a smoky glass (CLEAR_LIGHT_PLATE scaled by CLEAR_LIGHT_SMOKE in
# linear light) so white glyphs read against it: glyph - plate L* +16 -> >= 25 (Discord, Settings, Photos, iMessage,
# Spotify). The map is floor + (1 - floor) s^CLEAR_MONO_GAMMA of the linear stretch s (gamma_lut): Photos' petals
# land at 0.85-0.90 while the darkest paint stays at CLEAR_MONO_FLOOR — a flat 0.7 floor (builder) washed out every
# dark detail: Secure Folder's keyhole (clear-light dL* 52 -> 19), Ti84's body / keys, Camera's lens, the black
# overlays of Calculator / Internet / Files (reviewer, round 5). Combined bodies (one bevel around several colours:
# Maps, Drive, Home, Play Store) map their colours by rank from CLEAR_COMBINED_FLOOR instead, so their internal
# boundaries keep visible steps
CLEAR_MONO_FLOOR = 0.3
CLEAR_MONO_GAMMA = 0.35
CLEAR_DARK_COAT = 0.3
CLEAR_COMBINED_FLOOR = 0.4
CLEAR_COMBINED_LINEAR = 0.5
CLEAR_LIGHT_SMOKE = 0.32
# tinted-light: the glyph takes mono x tint at TINT_LIGHT_GAIN + strength (capped at 1) — round 4's exact
# mono x tint at the tint's own strength halved the glyph/plate contrast (Discord -20.5 -> -13.4)
TINT_LIGHT_GAIN = 0.35
# tinted-dark: the rank-spread map starts higher (the darkest paint at MONO_FLOOR read as navy on the near-black plate)
TINT_DARK_FLOOR = 0.5
# mono luminance map: stretched to floor..1 by MONO_LINEAR x the linear stretch + (1 - MONO_LINEAR) x the rank of
# each distinct paint lightness, so neighbouring regions of similar lightness (Maps' red / blue / green: 0.50 /
# 0.53 / 0.58) keep visible steps on combined, texture-painted bodies whose internal edges have no bevel
MONO_LINEAR = 0.3
MONO_MERGE = 0.015
# wallpaper shader layout (object coords of the wallpaper plane == world XY): vertical gradient over
# ±WP_Y, blobs at (x, y)·WP_POS with smoothstep radius r·WP_R mixed by WP_MIX (scene._wallpaper and the
# EEVEE frosted-plate fallback in materials.py)
WP_Y, WP_POS, WP_R, WP_MIX = 2.2, 1.6, 1.9, 0.85


def _apply_override(proj: dict, ov: dict) -> None:
    if not ov:
        return
    if ov.get("plateFill") is not None:
        proj["canvas"]["plate"]["fill"] = copy.deepcopy(ov["plateFill"])
    layers = {L["id"]: L for L in proj["layers"]}
    for lid, lo in (ov.get("layers") or {}).items():
        L = layers.get(lid)
        if L is None or not lo:
            continue
        for k in ("fill", "opacity", "visible", "blendMode", "material"):
            if lo.get(k) is not None:
                L[k] = copy.deepcopy(lo[k])


def _perceptual(rgb_srgb) -> float:
    lin = [srgb_to_linear(c) for c in rgb_srgb]
    y = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    return max(0.0, y) ** (1 / 2.2)


def _fill_colors(fill: dict) -> list:
    t = fill.get("type")
    if t == "solid":
        return [hex_to_srgb(fill.get("color", "#ffffff"))]
    if t in ("linear", "radial"):
        return [hex_to_srgb(s.get("color", "#000000")) for s in fill.get("stops", [])]
    if t == "system-light":
        return [hex_to_srgb("#ffffff"), hex_to_srgb("#e4e5ea")]
    if t == "system-dark":
        return [hex_to_srgb("#3a3a3f"), hex_to_srgb("#111114")]
    return []


def paint_lightness(proj: dict, bundle: dict) -> list:
    """Perceptual lightness of every visible foreground paint colour (fill overrides or region paints)."""
    vals = []
    for L in proj["layers"]:
        if not L.get("visible", True):
            continue
        fill = L.get("fill") or {"type": "auto"}
        cols = []
        if fill.get("type") == "auto":
            geo = (bundle.get("layers") or {}).get(L["id"])
            for r in (geo or {}).get("regions", []):
                cols += _fill_colors(r.get("paint") or {})
        else:
            cols = _fill_colors(fill)
        vals += [_perceptual(c) for c in cols]
    return vals


def luminance_range(proj: dict, bundle: dict) -> tuple[float, float]:
    """Perceptual luminance range over all visible foreground paint (fill overrides or region paints)."""
    vals = paint_lightness(proj, bundle)
    if not vals:
        return 0.0, 1.0
    return min(vals), max(vals)


def mono_lut(vals: list, floor: float, min_range: float = 0.55, linear: float = MONO_LINEAR) -> list:
    """Monotone map perceptual lightness -> stretched mono value (floor..1) as [(lightness, value)] stops for a
    colour ramp (materials._mono): MONO_LINEAR x the linear stretch of the range + the rest by rank of the distinct
    lightnesses, so similar paints stay apart. The brightest paint maps to 1, values below / above clamp."""
    vs = []
    for v in sorted(float(x) for x in vals):
        if not vs or v - vs[-1] > MONO_MERGE:
            vs.append(v)
    if not vs:
        return [(0.0, floor), (1.0, 1.0)]
    hi = vs[-1]
    lo = min(vs[0], hi - min_range)
    n = len(vs)
    out = [] if lo >= vs[0] - 1e-6 else [(max(0.0, lo), floor)]
    for i, v in enumerate(vs):
        lin = (v - lo) / max(1e-6, hi - lo)
        rank = i / (n - 1) if n > 1 else 1.0
        out.append((v, floor + (1.0 - floor) * (linear * lin + (1.0 - linear) * rank)))
    if len(out) > 30:                      # colour ramps hold 32 stops: keep the ends, thin the middle
        step = (len(out) - 1) / 29.0
        out = [out[int(round(i * step))] for i in range(30)]
    return out


def gamma_lut(vals: list, floor: float, gamma: float, min_range: float = 0.55) -> list:
    """Monotone map perceptual lightness -> mono value ``floor + (1 - floor) s^gamma`` as [(lightness, value)] colour
    ramp stops (materials._mono), ``s`` the linear stretch of the paint range (lo <= hi - min_range, as
    materials.MONO_MIN_RANGE): the darkest paint keeps ``floor`` (dark details stay dark), mid paints are lifted toward
    white (gamma < 1), the brightest maps to 1. Stops sit on every distinct paint lightness plus along the curve
    (gradient pixels between paints follow it too)."""
    vs = []
    for v in sorted(float(x) for x in vals):
        if not vs or v - vs[-1] > MONO_MERGE:
            vs.append(v)
    if not vs:
        return [(0.0, floor), (1.0, 1.0)]
    hi = vs[-1]
    lo = max(0.0, min(vs[0], hi - min_range))
    span = max(1e-6, hi - lo)

    def f(v: float) -> float:
        s = min(1.0, max(0.0, (v - lo) / span))
        return floor + (1.0 - floor) * s ** gamma

    if len(vs) > 29:                       # colour ramps hold 32 stops: keep the ends, thin the middle
        step = (len(vs) - 1) / 28.0
        vs = [vs[int(round(i * step))] for i in range(29)]
    pts = set(vs) | {lo}
    for s in (0.01, 0.03, 0.07, 0.13, 0.22, 0.35, 0.5, 0.7, 0.85):
        if len(pts) >= 30:
            break
        c = lo + span * s
        if all(abs(c - v) > 0.004 for v in pts):
            pts.add(c)
    return [(v, f(v)) for v in sorted(pts)]


def resolve(project: dict, appearance: str, bundle: dict) -> dict:
    """-> {"project": effective project, "appearance": id, "env": {...}} (input is not modified)."""
    proj = copy.deepcopy(project)
    canvas = proj["canvas"]
    if canvas.get("platform") == "watchos":
        appearance = "light"
    env = {"envScale": 1.0, "keyScale": 1.0, "wallpaper": None, "mono": None, "clear": False,
           "emission": 0.0, "dark": appearance.endswith("dark")}
    aps = proj.get("appearances") or {}
    tint = aps.get("tint") or {"color": "#3b82f6", "strength": 0.8}

    if appearance == "light":
        pass
    elif appearance == "dark":
        _apply_override(proj, aps.get("dark") or {})
        env.update(envScale=0.6, keyScale=0.85)
    else:
        _apply_override(proj, aps.get("mono") or {})
        vals = paint_lightness(proj, bundle)
        lo, hi = (min(vals), max(vals)) if vals else (0.0, 1.0)
        tint_lin = hex_to_linear(tint.get("color", "#3b82f6"))
        strength = float(tint.get("strength", 0.8))
        dark = appearance.endswith("dark")
        if dark:
            env.update(envScale=0.6, keyScale=0.85)
        plate = canvas["plate"]
        if appearance.startswith("clear"):
            env.update(mono={"lo": lo, "hi": hi, "floor": CLEAR_MONO_FLOOR, "tint": None, "strength": 0.0,
                             "lut": gamma_lut(vals, CLEAR_MONO_FLOOR, CLEAR_MONO_GAMMA)},
                       monoCombined={"lo": lo, "hi": hi, "floor": CLEAR_COMBINED_FLOOR, "tint": None, "strength": 0.0,
                                     "lut": mono_lut(vals, CLEAR_COMBINED_FLOOR, linear=CLEAR_COMBINED_LINEAR)},
                       clear=True, wallpaper="dark" if dark else "light", edgeDark=0.0 if dark else CLEAR_EDGE_DARK)
            for L in proj["layers"]:
                if L.get("visible", True):
                    L["material"] = {"preset": "liquid_glass",
                                     "params": {"tint": CLEAR_TINT, "frost": 0.3, "translucency": 0.35,
                                                "rim": 1.0, "specular": "auto",
                                                "glow": CLEAR_GLOW_DARK if dark else CLEAR_GLOW_LIGHT}}
                    if L.get("fill", {}).get("type") == "none":
                        L["fill"] = {"type": "auto"}
                    L["glass"] = True
                    if not dark:
                        sh = dict(L.get("shadow") or {})
                        L["shadow"] = {"kind": "neutral",
                                       "opacity": max(CLEAR_LIGHT_SHADOW, float(sh.get("opacity", 0.5) or 0.0))}
            if dark:
                # a weaker coat / lower index: without PBR Neutral's toe the pane's studio reflection read as a grey
                # veil over the deep wallpaper ('brand')
                plate["material"] = {"preset": "frosted_glass", "params": {"tint": 0.0, "frost": 0.42, "grain": 0.04,
                                                                           "coat": CLEAR_DARK_COAT, "ior": 1.3}}
                plate["fill"] = {"type": "solid", "color": "#ffffff", "opacity": 1.0}
            else:
                # a faintly smoked pane: the white frosted glyph must read against the pale plate
                plate["material"] = {"preset": "frosted_glass",
                                     "params": {"tint": CLEAR_LIGHT_PLATE_TINT, "frost": 0.42, "grain": 0.04}}
                plate["fill"] = {"type": "solid", "color": _scale_hex(CLEAR_LIGHT_PLATE, CLEAR_LIGHT_SMOKE), "opacity": 1.0}
        elif appearance == "tinted-light":
            env.update(mono={"lo": lo, "hi": hi, "floor": MONO_FLOOR, "tint": tint_lin,
                             "strength": min(1.0, strength + TINT_LIGHT_GAIN), "lut": mono_lut(vals, MONO_FLOOR)},
                       wallpaper="light")
            for L in proj["layers"]:
                if L.get("visible", True):
                    L["material"] = {"preset": "liquid_glass",
                                     "params": {"tint": 0.55 + 0.4 * strength, "frost": 0.18, "translucency": 0.55}}
                    L["glass"] = True
            pale = _mix_hex("#ffffff", tint.get("color", "#3b82f6"), 0.18 + 0.2 * strength)
            plate["material"] = {"preset": "frosted_glass", "params": {"tint": 0.5, "frost": 0.4, "grain": 0.04}}
            plate["fill"] = {"type": "solid", "color": pale, "opacity": 1.0}
        else:  # tinted-dark
            env.update(mono={"lo": lo, "hi": hi, "floor": TINT_DARK_FLOOR, "tint": tint_lin, "strength": strength,
                             "lut": mono_lut(vals, TINT_DARK_FLOOR)},
                       emission=0.35, wallpaper="dark")
            for L in proj["layers"]:
                if L.get("visible", True):
                    L["material"] = {"preset": "liquid_glass",
                                     "params": {"tint": 0.85, "frost": 0.12, "translucency": 0.5, "rim": 1.0}}
                    L["glass"] = True
            plate["fill"] = {"type": "system-dark"}
            if plate["material"].get("preset") not in ("satin", "glossy_plastic", "matte_clay"):
                plate["material"] = {"preset": "satin", "params": {}}
    canvas["plate"]["fill"] = norm_fill(canvas["plate"]["fill"], {"type": "solid", "color": "#ffffff", "opacity": 1.0})
    return {"project": proj, "appearance": appearance, "env": env}


def _scale_hex(h: str, k: float) -> str:
    """sRGB hex colour scaled by ``k`` in linear light."""
    from .util import linear_to_srgb
    lin = hex_to_linear(h)
    return "#%02x%02x%02x" % tuple(int(round(max(0.0, min(1.0, linear_to_srgb(v * k))) * 255)) for v in lin)


def _mix_hex(a: str, b: str, t: float) -> str:
    ca, cb = hex_to_srgb(a), hex_to_srgb(b)
    c = lerp(ca, cb, t)
    return "#%02x%02x%02x" % tuple(int(round(max(0, min(1, v)) * 255)) for v in c)


def wallpaper_linear(kind: str, depth: float = 1.0) -> dict:
    """Linear-colour wallpaper for shader use (EEVEE frosted-plate fallback); ``depth`` = distance from the
    plate's front face down to the wallpaper plane."""
    w = WALLPAPERS.get(kind) or WALLPAPERS["light"]
    return {"kind": kind, "top": hex_to_linear(w["top"]), "bottom": hex_to_linear(w["bottom"]), "depth": float(depth),
            "blobs": [(x, y, r, hex_to_linear(c)) for (x, y, r, c) in w["blobs"]]}


def wallpaper(kind: str) -> dict:
    """Linear-colour description of a wallpaper (also returned to the server so the UI can match)."""
    w = WALLPAPERS.get(kind) or WALLPAPERS["light"]
    return {"kind": kind, "top": w["top"], "bottom": w["bottom"],
            "blobs": [{"x": x, "y": y, "r": r, "color": c} for (x, y, r, c) in w["blobs"]]}
