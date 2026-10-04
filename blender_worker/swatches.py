"""Material swatches + synthetic scenes (swatch gallery, worker warm-up, tests).

``render_swatches`` renders every preset of shared/presets.json on a standard swatch scene — a light
squircle plate with three dark bars (so glass shows its refraction/lensing) under a thick disc in the
preset material painted with a vivid gradient — with Cycles preview quality at 192 px into
``web/public/swatches/<preset>.png``.
"""
from __future__ import annotations

import os
import tempfile
from typing import Callable, Optional

import bpy

from . import presets as P
from . import render as R
from .geometry import circle_spline, rect_spline

SWATCH_PAINT = {"type": "linear", "start": [-0.55, 0.6], "end": [0.55, -0.6],
                "stops": [{"offset": 0.0, "color": "#38bdf8", "opacity": 1.0},
                          {"offset": 0.5, "color": "#6366f1", "opacity": 1.0},
                          {"offset": 1.0, "color": "#d946ef", "opacity": 1.0}]}


DARK_SWATCHES = {"neon"}


def _layer(lid: str, z: float, preset: str, fill: dict, thickness: float = 0.16, bevel: float = 0.07,
           params: Optional[dict] = None, shadow: float = 0.55) -> dict:
    return {"id": lid, "name": lid, "elementIds": [lid], "visible": True, "mode": "individual", "fill": fill,
            "opacity": 1.0, "glass": True,
            "depth": {"z": z, "thickness": thickness, "bevel": bevel, "bevelSegments": 6, "inflate": 0.0},
            "material": {"preset": preset, "params": params or {}},
            "shadow": {"kind": "physical" if shadow > 0 else "none", "opacity": shadow}}


def _geo(lid: str, splines: list, paint: dict, safe: float = 0.2, texture: str = "") -> dict:
    xs = [p["co"][0] for s in splines for p in s["points"]]
    ys = [p["co"][1] for s in splines for p in s["points"]]
    return {"layerId": lid, "hash": f"syn-{lid}-{len(xs)}-{round(sum(xs), 3)}", "silhouette": splines,
            "regions": [{"elementId": lid, "paint": paint, "opacity": 1.0, "zSub": 0, "splines": splines}],
            "safeRadius": safe, "bbox": [min(xs), min(ys), max(xs), max(ys)], "texture": "", "texturePath": texture,
            "svg": "", "images": []}


def swatch_scene(preset: str) -> tuple[dict, dict]:
    """(project, bundle) for one material swatch."""
    bars = [rect_spline(-0.62 + i * 0.5, -0.7, -0.5 + i * 0.5, 0.7) for i in range(3)]
    disc = [circle_spline(0.0, 0.0, 0.56)]
    layers = [
        _layer("bars", 0.0, "flat", {"type": "solid", "color": "#1f2937", "opacity": 1.0}, 0.02, 0.0, shadow=0.0),
        _layer("disc", 0.06, preset, dict(SWATCH_PAINT), 0.22, 0.1),
    ]
    layers[1]["depth"]["inflate"] = 0.5
    dark = preset in DARK_SWATCHES        # emissive presets read best on a dark plate
    plate_stops = ([{"offset": 0, "color": "#2a2b35"}, {"offset": 1, "color": "#0d0e14"}] if dark else
                   [{"offset": 0, "color": "#f4f5f8"}, {"offset": 1, "color": "#d9dce4"}])
    if dark:
        layers[0]["fill"] = {"type": "solid", "color": "#3a3d4a", "opacity": 1.0}
    project = {
        "id": "swatch", "name": f"swatch {preset}", "layers": layers,
        "canvas": {"shape": "squircle",
                   "plate": {"fill": {"type": "linear", "start": [0, 1], "end": [0, -1], "stops": plate_stops},
                             "material": {"preset": "satin"}, "thickness": 0.14, "bevel": 0.05}},
        "lighting": {"preset": "studio", "angle": -45.0},
        "camera": {"view": "front", "zoom": 1.06},
        "render": {"colorMode": P.DEFAULT_COLOR_MODE, "backdrop": "transparent"},
    }
    bundle = {"projectId": "swatch", "hash": "swatch", "viewBox": [0, 0, 1, 1], "layers": {
        "bars": _geo("bars", bars, {"type": "solid", "color": "#1f2937", "opacity": 1.0}, 0.05),
        "disc": _geo("disc", disc, SWATCH_PAINT, 0.5),
    }}
    return project, bundle


def warmup_texture() -> str:
    """A small RGBA PNG used by warm-up scenes so the texture-paint shader variant gets compiled."""
    path = os.path.join(tempfile.gettempdir(), "bis_warmup_paint.png")
    if not os.path.isfile(path):
        img = bpy.data.images.new("bis_warmup_paint", 16, 16, alpha=True)
        px = []
        for y in range(16):
            for x in range(16):
                px += [x / 15.0, y / 15.0, 0.6, 1.0]
        img.pixels = px
        img.filepath_raw = path
        img.file_format = "PNG"
        img.save()
        bpy.data.images.remove(img)
    return path


def warmup_scene(full: bool = True, mono: bool = False) -> tuple[dict, dict]:
    """The shader variants the app generates, in one scene (EEVEE compiles one shader per graph shape): every preset
    (grain / film / anisotropy / tint / emission variants) on texture, gradient and solid paints, a translucent piece,
    a raster region (UV affine + alpha), a flat raster card and a combined body. ``mono``: the reduced scene for the
    clear / tinted renditions (every layer becomes Liquid Glass there)."""
    tex = warmup_texture()
    glass_presets = ["liquid_glass"] + (["clear_glass", "frosted_glass", "dispersive_crystal", "tinted_glass"]
                                        if full else [])
    solid_presets = ["satin", "flat"] + (["glossy_plastic", "candy", "gummy", "jelly", "chrome", "brushed_metal",
                                          "matte_clay", "iridescent", "neon"] if full else [])
    if mono:
        glass_presets, solid_presets = ["liquid_glass", "liquid_glass", "liquid_glass"], []
    layers, geos = [], {}
    n = 0
    # bottom: opaque presets in a grid, then glass presets, then the top glass covering everything
    for i, pr in enumerate(solid_presets + glass_presets):
        lid = f"w{n}"
        x = -0.75 + (i % 5) * 0.37
        y = 0.6 - (i // 5) * 0.4
        fill = {"type": "auto"} if i % 3 == 0 else SWATCH_PAINT if i % 3 == 1 else {"type": "solid", "color": "#ff8800"}
        L = _layer(lid, 0.0 + 0.02 * i, pr, fill, 0.08, 0.03)
        if i == 1:
            L["opacity"] = 0.8
        layers.append(L)
        geos[lid] = _geo(lid, [circle_spline(x, y, 0.15)], SWATCH_PAINT, 0.1, tex)
        n += 1
    # Liquid Glass (the default material) in the piece kinds a corpus icon produces: a raster card, a translucent
    # piece, a raster region (UV affine + alpha) and a combined body
    lg = [("lgfake", -0.45, 0.05, {"type": "solid", "color": "#3366ff", "opacity": 1.0}, 1.0, "card"),
          ("lgfilm", 0.0, 0.05, {"type": "solid", "color": "#000000", "opacity": 1.0}, 0.4, None),
          ("lgraster", 0.45, 0.05, {"type": "solid", "color": "#ff8800", "opacity": 1.0}, 1.0, "img"),
          ("lgcomb", 0.0, -0.35, {"type": "solid", "color": "#22aa55", "opacity": 1.0}, 1.0, "combined")]
    for k, (lid, x, y, paint, op, extra) in enumerate(lg):
        L = _layer(lid, 0.3 + 0.01 * k, "liquid_glass", {"type": "auto"}, 0.08, 0.03)
        g = _geo(lid, [circle_spline(x, y, 0.14)], paint, 0.1, tex)
        g["regions"][0]["opacity"] = op
        if extra == "img":
            g["regions"][0]["elementId"] = f"{lid}img"
            g["images"] = [{"elementId": f"{lid}img", "path": tex, "bbox": [x - 0.14, y - 0.14, x + 0.14, y + 0.14],
                            "width": 16, "height": 16, "matrix": [0.0175, 0.0, 0.0, -0.0175, x - 0.14, y + 0.14]}]
        if extra == "card":         # a flat raster card (glow halo / unextruded <image>)
            g["images"] = [{"elementId": f"{lid}card", "path": tex, "bbox": [x - 0.1, y - 0.1, x + 0.1, y + 0.1],
                            "width": 16, "height": 16, "matrix": [0.0125, 0.0, 0.0, -0.0125, x - 0.1, y + 0.1]}]
        if extra == "combined":
            L["mode"] = "combined"
        layers.append(L)
        geos[lid] = g
    # a combined body on top
    L = _layer("lgcombtop", 0.7, "liquid_glass", {"type": "auto"}, 0.08, 0.03)
    L["mode"] = "combined"
    layers.append(L)
    geos["lgcombtop"] = _geo("lgcombtop", [circle_spline(0.84, -0.84, 0.09)],
                             {"type": "solid", "color": "#aa2255", "opacity": 1.0}, 0.06, tex)
    lid = "top"
    layers.append(_layer(lid, 0.5, "liquid_glass", {"type": "auto"}, 0.1, 0.045))
    geos[lid] = _geo(lid, [circle_spline(0.0, 0.0, 0.9)], SWATCH_PAINT, 0.3, tex)
    # a raster <image> region on the top glass (texture + exact UV affine + alpha variant)
    raster = [circle_spline(0.0, -0.5, 0.2)]
    geos[lid]["regions"].append({"elementId": "img0", "paint": {"type": "solid", "color": "#ff8800", "opacity": 1.0},
                                 "opacity": 1.0, "zSub": 0.001, "splines": raster})
    geos[lid]["images"] = [{"elementId": "img0", "path": tex, "bbox": [-0.2, -0.7, 0.2, -0.3],
                            "width": 16, "height": 16, "matrix": [0.025, 0.0, 0.0, -0.025, -0.2, -0.3]}]
    project = {"id": "warmup", "name": "warmup", "layers": layers,
               "canvas": {"shape": "squircle"}, "render": {"backdrop": "transparent", "colorMode": P.DEFAULT_COLOR_MODE}}
    bundle = {"projectId": "warmup", "hash": "warmup", "layers": geos}
    return project, bundle


def render_swatches(builder, out_dir: str, size: int = 192, quality: str = "preview",
                    progress: Optional[Callable[[float, str], None]] = None, only: Optional[list] = None) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    ids = [p for p in P.material_ids() if not only or p in only]
    files = []
    scene = bpy.context.scene
    for i, preset in enumerate(ids):
        project, bundle = swatch_scene(preset)
        info = builder.build(project, bundle, "light", engine=R.tier(quality)["engine"])
        cm = P.color_mode_id(project["render"]["colorMode"])
        R.configure(scene, quality, size, transparent=True, color_mode=cm, max_glass_roughness=info["maxGlassRoughness"])
        R.configure_compositor(scene, info["bloom"], True, P.soft_clip_knee(cm))
        out = os.path.join(out_dir, f"{preset}.png")
        R.render_still(scene, out)
        files.append(out)
        if progress:
            progress((i + 1) / len(ids), preset)
    return files
