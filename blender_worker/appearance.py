"""The six Icon Composer renditions (PLAN §5) resolved into an *effective* project + environment.

* light        — base project.
* dark         — ``appearances.dark`` overrides (default plate fill system-dark); env × 0.6, key × 0.85.
* clear-*      — ``appearances.mono`` overrides; every visible layer → liquid_glass (tint 0, frost 0.3)
                 whose paint is the mono luminance (stretched to 0.25..1, brightest → white) driving the
                 milky-white frost; plate → frosted glass (tint 0) over a wallpaper backdrop.
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


def luminance_range(proj: dict, bundle: dict) -> tuple[float, float]:
    """Perceptual luminance range over all visible foreground paint (fill overrides or region paints)."""
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
    if not vals:
        return 0.0, 1.0
    return min(vals), max(vals)


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
        lo, hi = luminance_range(proj, bundle)
        tint_lin = hex_to_linear(tint.get("color", "#3b82f6"))
        strength = float(tint.get("strength", 0.8))
        dark = appearance.endswith("dark")
        if dark:
            env.update(envScale=0.6, keyScale=0.85)
        plate = canvas["plate"]
        if appearance.startswith("clear"):
            env.update(mono={"lo": lo, "hi": hi, "floor": MONO_FLOOR, "tint": None, "strength": 0.0},
                       clear=True, wallpaper="dark" if dark else "light")
            for L in proj["layers"]:
                if L.get("visible", True):
                    L["material"] = {"preset": "liquid_glass",
                                     "params": {"tint": 0.0, "frost": 0.3, "translucency": 0.35,
                                                "rim": 1.0, "specular": "auto"}}
                    if L.get("fill", {}).get("type") == "none":
                        L["fill"] = {"type": "auto"}
                    L["glass"] = True
            plate["material"] = {"preset": "frosted_glass", "params": {"tint": 0.0, "frost": 0.42, "grain": 0.04}}
            plate["fill"] = {"type": "solid", "color": "#ffffff", "opacity": 1.0}
        elif appearance == "tinted-light":
            env.update(mono={"lo": lo, "hi": hi, "floor": MONO_FLOOR, "tint": tint_lin, "strength": strength},
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
            env.update(mono={"lo": lo, "hi": hi, "floor": MONO_FLOOR, "tint": tint_lin, "strength": strength},
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


def _mix_hex(a: str, b: str, t: float) -> str:
    ca, cb = hex_to_srgb(a), hex_to_srgb(b)
    c = lerp(ca, cb, t)
    return "#%02x%02x%02x" % tuple(int(round(max(0, min(1, v)) * 255)) for v in c)


def wallpaper(kind: str) -> dict:
    """Linear-colour description of a wallpaper (also returned to the server so the UI can match)."""
    w = WALLPAPERS.get(kind) or WALLPAPERS["light"]
    return {"kind": kind, "top": w["top"], "bottom": w["bottom"],
            "blobs": [{"x": x, "y": y, "r": r, "color": c} for (x, y, r, c) in w["blobs"]]}
