"""The six Icon Composer renditions (PLAN §5, §11) resolved into an *effective* project + environment.

Appearances only change Principled inputs (and the plate fill / backdrop), never the shader graph's nature:

* light:         base project.
* dark:          ``appearances.dark`` overrides (default plate fill system-dark); env × 0.6, key × 0.85; glyphs that
                 transmit more than DARK_GLYPH allows become lit glass (transmission 0.5, subsurface 1, art base)
                 unless the user set the layer's dark material.
* clear-*:       ``appearances.mono`` overrides; every visible layer becomes clear glass: white base (tint 0),
                 transmission 1, roughness CLEAR_GLYPH['roughness']; the plate is frosted glass over the wallpaper.
* tinted-light:  mono luminance × tint colour into the glass Base Color; plate = pale tinted frosted glass over
                 the light wallpaper.
* tinted-dark:   plate system-dark satin; foreground = mono luminance × tint as DARK_GLYPH lit glass (no emission).

watchOS ignores appearances (always light). Per-layer LayerOverride fields replace base values.
"""
from __future__ import annotations

import copy
from typing import Optional

from . import presets as P
from .defaults import norm_fill
from .util import hex_to_linear, hex_to_srgb, lerp, srgb_to_linear

WALLPAPERS = {
    # soft pastel (light) / deep blue-black (dark) gradient + colour blobs, all sRGB hex
    "light": {"top": "#dfe9ff", "bottom": "#f7e8f2",
              "blobs": [(-0.9, 0.7, 0.9, "#9ec5ff"), (0.95, -0.55, 0.85, "#ffc7dc"), (0.2, 1.1, 0.6, "#c8f1e6")]},
    "dark": {"top": "#0b1630", "bottom": "#05060c",
             "blobs": [(-0.85, 0.6, 0.9, "#1d3f8f"), (0.9, -0.6, 0.85, "#3b1d6e"), (0.25, 1.15, 0.6, "#0f4a5c")]},
}
# wallpaper shader layout (object coords of the wallpaper plane == world XY): vertical gradient over ±WP_Y, blobs at
# (x, y)·WP_POS with smoothstep radius r·WP_R mixed by WP_MIX (scene._wallpaper)
WP_Y, WP_POS, WP_R, WP_MIX = 2.2, 1.6, 1.9, 0.85

# clear renditions: frosted white glass glyphs over a frosted pane (Principled values only)
CLEAR_GLYPH = {"tint": 0.0, "transmission": 1.0, "roughness": 0.22, "ior": 1.5, "metallic": 0.0,
               "coatWeight": 0.6, "coatRoughness": 0.03, "emissionStrength": 0.0, "paintMode": "base"}
CLEAR_PLATE = {"tint": 0.0, "transmission": 1.0, "roughness": 0.45, "ior": 1.45, "coatWeight": 0.3}
# tinted renditions: glyph Base Color = mono luminance (stretched to MONO_FLOOR..1, linear light) x tint colour; the
# stretch spans at least MONO_MIN_RANGE (a single white glyph keeps the full tint instead of dropping to the floor)
MONO_FLOOR = 0.08
MONO_FLOOR_DARK = 0.3           # tinted-dark: the darkest art still reflects 30 % of the tint (lit, not self-lit)
MONO_MIN_RANGE = 0.5
TINTED_GLYPH = {"tint": 1.0, "transmission": 1.0, "roughness": 0.15, "ior": 1.5, "metallic": 0.0,
                "coatWeight": 0.6, "coatRoughness": 0.03, "paintMode": "base", "emissionStrength": 0.0}
TINTED_PLATE = {"tint": 0.6, "transmission": 1.0, "roughness": 0.45, "ior": 1.45, "coatWeight": 0.3}
# dark renditions (dark, tinted-dark): clear glass over a near-black plate only transmits that plate: the glyphs
# vanished (QA r8 #2). Physically, Principled inputs only: at most DARK_GLYPH['transmission'] of the glyph stays
# specular transmission, the rest scatters inside (subsurface, radius = the art colour) and the art colour is the base
# (tint ≥ DARK_GLYPH['tintMin']): lit glass / opal that keeps its colour over any plate.
DARK_GLYPH = {"transmission": 0.5, "subsurfaceWeight": 1.0, "tintMin": 0.9}
TINTED_DARK_GLOW = 0.5          # LEGACY (unused since round 7: tinted-dark glyphs are lit, no emission); the viewport's
                                # appearance.ts mirror still reads it until it ports DARK_GLYPH


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


def luminance_range(proj: dict, bundle: dict) -> tuple[float, float]:
    """Linear-light luminance range over all visible foreground paint (fill overrides or region paints)."""
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
        for c in cols:
            lin = [srgb_to_linear(v) for v in c]
            vals.append(0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2])
    if not vals:
        return 0.0, 1.0
    return min(vals), max(vals)


def _glass_layers(proj: dict, params: dict) -> None:
    """Every visible layer -> Liquid Glass with ``params`` (per-shape overrides dropped: one look per rendition)."""
    for L in proj["layers"]:
        if L.get("visible", True):
            L["material"] = {"preset": "liquid_glass", "params": dict(params)}
            L["elementMaterials"] = {}
            if (L.get("fill") or {}).get("type") == "none":
                L["fill"] = {"type": "auto"}
            L["glass"] = True


def _dark_params(preset: str, params: dict) -> Optional[dict]:
    """DARK_GLYPH applied to one material's params (None: not transmissive enough to need it)."""
    full = P.material_params(preset, params)
    try:
        tr = float(full.get("transmission", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    if tr <= DARK_GLYPH["transmission"] + 1e-6:
        return None
    out = dict(params)
    out["transmission"] = DARK_GLYPH["transmission"]
    out["subsurfaceWeight"] = max(float(full.get("subsurfaceWeight", 0.0) or 0.0), DARK_GLYPH["subsurfaceWeight"])
    out["tint"] = max(float(full.get("tint", 1.0) if full.get("tint") is not None else 1.0), DARK_GLYPH["tintMin"])
    return out


def _dark_glyphs(proj: dict, explicit: set) -> None:
    """Dark renditions: every visible layer (and its per-shape materials) that transmits more than DARK_GLYPH allows
    gets the DARK_GLYPH inputs, except layers whose dark material the user set explicitly (``explicit``)."""
    for L in proj["layers"]:
        if not L.get("visible", True) or L["id"] in explicit or not L.get("glass", True):
            continue
        lm = L.get("material") or {}
        preset = str(lm.get("preset") or "liquid_glass")
        new = _dark_params(preset, dict(lm.get("params") or {}))
        if new is not None:
            L["material"] = {"preset": preset, "params": new}
        ems = {}
        for eid, em in (L.get("elementMaterials") or {}).items():
            ep, epr = P.resolve_material(L["material"], em)
            fix = _dark_params(ep, epr)
            ems[eid] = {"preset": ep, "params": fix} if fix is not None else em
        if ems:
            L["elementMaterials"] = ems


def resolve(project: dict, appearance: str, bundle: dict) -> dict:
    """-> {"project": effective project, "appearance": id, "env": {...}} (input is not modified).

    env: envScale / keyScale (lighting multipliers), wallpaper ('light' | 'dark' | None: a wallpaper plane under the
    plate, seen through glass), mono (tinted: {lo, hi, floor, tint} paint transform), dark."""
    proj = copy.deepcopy(project)
    canvas = proj["canvas"]
    if canvas.get("platform") == "watchos":
        appearance = "light"
    env = {"envScale": 1.0, "keyScale": 1.0, "wallpaper": None, "mono": None, "dark": appearance.endswith("dark")}
    aps = proj.get("appearances") or {}
    tint = aps.get("tint") or {"color": "#3b82f6", "strength": 0.8}

    if appearance == "dark":
        dov = aps.get("dark") or {}
        _apply_override(proj, dov)
        env.update(envScale=0.6, keyScale=0.85)
        _dark_glyphs(proj, {lid for lid, lo in (dov.get("layers") or {}).items()
                            if lo and lo.get("material") is not None})
    elif appearance != "light":
        _apply_override(proj, aps.get("mono") or {})
        dark = appearance.endswith("dark")
        if dark:
            env.update(envScale=0.6, keyScale=0.85)
        plate = canvas["plate"]
        if appearance.startswith("clear"):
            env.update(wallpaper="dark" if dark else "light")
            _glass_layers(proj, CLEAR_GLYPH)
            plate["material"] = {"preset": "frosted_glass", "params": dict(CLEAR_PLATE)}
            plate["fill"] = {"type": "solid", "color": "#ffffff", "opacity": 1.0}
        else:
            lo, hi = luminance_range(proj, bundle)
            strength = max(0.0, min(1.0, float(tint.get("strength", 0.8))))
            tint_lin = lerp((1.0, 1.0, 1.0), hex_to_linear(tint.get("color", "#3b82f6")), strength)
            env.update(mono={"lo": min(lo, hi - MONO_MIN_RANGE), "hi": hi,
                             "floor": MONO_FLOOR_DARK if dark else MONO_FLOOR, "tint": tint_lin},
                       wallpaper="dark" if dark else "light")
            if dark:
                # lit by the rig like the dark rendition (no self-lit glyphs): DARK_GLYPH over the near-black plate
                _glass_layers(proj, TINTED_GLYPH)
                _dark_glyphs(proj, set())
                plate["fill"] = {"type": "system-dark"}
                if plate["material"].get("preset") not in ("satin", "glossy_plastic", "matte_clay"):
                    plate["material"] = {"preset": "satin", "params": {}}
            else:
                _glass_layers(proj, TINTED_GLYPH)
                plate["material"] = {"preset": "frosted_glass", "params": dict(TINTED_PLATE)}
                plate["fill"] = {"type": "solid", "color": _mix_hex("#ffffff", tint.get("color", "#3b82f6"),
                                                                    0.18 + 0.2 * strength), "opacity": 1.0}
    canvas["plate"]["fill"] = norm_fill(canvas["plate"]["fill"], {"type": "solid", "color": "#ffffff", "opacity": 1.0})
    return {"project": proj, "appearance": appearance, "env": env}


def _mix_hex(a: str, b: str, t: float) -> str:
    ca, cb = hex_to_srgb(a), hex_to_srgb(b)
    c = lerp(ca, cb, t)
    return "#%02x%02x%02x" % tuple(int(round(max(0, min(1, v)) * 255)) for v in c)


def wallpaper(kind: str) -> dict:
    """sRGB description of a wallpaper (also returned to the server so the UI can match)."""
    w = WALLPAPERS.get(kind) or WALLPAPERS["light"]
    return {"kind": kind, "top": w["top"], "bottom": w["bottom"],
            "blobs": [{"x": x, "y": y, "r": r, "color": c} for (x, y, r, c) in w["blobs"]]}
