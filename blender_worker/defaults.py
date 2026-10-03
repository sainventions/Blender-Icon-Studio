"""Plain-dict defaults mirroring ``server/bis/models.py``.

The worker receives JSON dicts (no pydantic inside Blender). Every normaliser below returns a NEW dict
in which missing fields carry exactly the defaults declared in models.py, so the worker behaves the
same whether the server sends a full ``model_dump()`` or a hand-written partial dict (tests, CLI).

Keep in sync with models.py (field names are camelCase = JSON keys).
"""
from __future__ import annotations

import copy
from typing import Any, Optional

# ------------------------------------------------------------------------------------------------
# primitive defaults (models.py)
# ------------------------------------------------------------------------------------------------
LAYER_TRANSFORM = {"x": 0.0, "y": 0.0, "scale": 1.0}
LAYER_DEPTH = {"z": 0.0, "thickness": 0.10, "bevel": 0.045, "bevelSegments": 6, "inflate": 0.0}
LAYER_SHADOW = {"kind": "neutral", "opacity": 0.5}
MATERIAL_SPEC = {"preset": "liquid_glass", "params": {}}
ART_TRANSFORM = {"scale": 1.0, "x": 0.0, "y": 0.0}
LIGHTING = {"preset": "studio", "angle": -45.0, "elevation": 50.0, "intensity": 1.0, "rim": 1.0, "fill": 1.0,
            "environment": 1.0, "shadowSoftness": 0.5}
CAMERA = {"view": "front", "tiltX": 0.0, "tiltY": 0.0, "fov": 30.0, "zoom": 1.0, "explode": 1.0}
TINT = {"color": "#3b82f6", "strength": 0.8}
RENDER_SETTINGS = {"quality": "draft", "size": None, "colorMode": "neutral", "backdrop": "transparent",
                   "backdropColor": "#1c1c22", "autoPreview": True}

FILL_TYPES = ("auto", "none", "solid", "linear", "radial", "system-light", "system-dark")
APPEARANCE_IDS = ("light", "dark", "clear-light", "clear-dark", "tinted-light", "tinted-dark")
QUALITIES = ("draft", "preview", "final", "ultra")


def _d(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _over(defaults: dict, value: Any) -> dict:
    """Shallow: defaults overlaid by the (non-None) keys of value."""
    out = copy.deepcopy(defaults)
    for k, v in _d(value).items():
        if v is not None or k not in defaults or defaults[k] is None:
            out[k] = copy.deepcopy(v)
    return out


# ------------------------------------------------------------------------------------------------
# fills / paints
# ------------------------------------------------------------------------------------------------
def norm_stop(s: Any) -> dict:
    s = _d(s)
    return {"offset": float(s.get("offset", 0.0)), "color": str(s.get("color", "#000000")),
            "opacity": float(s.get("opacity", 1.0) if s.get("opacity") is not None else 1.0)}


def norm_fill(f: Any, default: Optional[dict] = None) -> dict:
    """Fill / Paint discriminated union. Unknown or missing -> ``default`` (FillAuto)."""
    if not isinstance(f, dict) or f.get("type") not in FILL_TYPES:
        return copy.deepcopy(default) if default is not None else {"type": "auto"}
    t = f["type"]
    if t == "solid":
        return {"type": "solid", "color": str(f.get("color") or "#ffffff"),
                "opacity": float(f.get("opacity", 1.0) if f.get("opacity") is not None else 1.0)}
    if t == "linear":
        return {"type": "linear", "stops": [norm_stop(s) for s in f.get("stops") or []],
                "start": list(f.get("start") or (0.0, 1.0)), "end": list(f.get("end") or (0.0, -1.0))}
    if t == "radial":
        radius = float(f["radius"]) if f.get("radius") is not None else 1.0   # 0 is legal (last stop colour)
        return {"type": "radial", "stops": [norm_stop(s) for s in f.get("stops") or []],
                "center": list(f.get("center") or (0.0, 0.0)), "radius": radius,
                "focal": list(f["focal"]) if f.get("focal") is not None else None,
                "matrix": list(f["matrix"]) if f.get("matrix") is not None else None}
    return {"type": t}


# ------------------------------------------------------------------------------------------------
# document
# ------------------------------------------------------------------------------------------------
def norm_material(m: Any, preset: str = "liquid_glass") -> dict:
    m = _d(m)
    return {"preset": str(m.get("preset") or preset), "params": dict(_d(m.get("params")))}


def norm_layer(layer: Any, index: int = 0) -> dict:
    L = _d(layer)
    return {
        "id": str(L.get("id", f"L{index}")),
        "name": str(L.get("name", f"Layer {index + 1}")),
        "elementIds": list(L.get("elementIds") or []),
        "visible": bool(L.get("visible", True)),
        "locked": bool(L.get("locked", False)),
        "mode": L.get("mode") if L.get("mode") in ("individual", "combined") else "individual",
        "fill": norm_fill(L.get("fill")),
        "opacity": float(L.get("opacity", 1.0)),
        "blendMode": str(L.get("blendMode") or "normal"),
        "glass": bool(L.get("glass", True)),
        "transform": _over(LAYER_TRANSFORM, L.get("transform")),
        "depth": _over(LAYER_DEPTH, L.get("depth")),
        "material": norm_material(L.get("material")),
        "shadow": _over(LAYER_SHADOW, L.get("shadow")),
    }


def norm_plate(p: Any) -> dict:
    p = _d(p)
    return {
        "visible": bool(p.get("visible", True)),
        "fill": norm_fill(p.get("fill"), {"type": "solid", "color": "#ffffff", "opacity": 1.0}),
        "material": norm_material(p.get("material"), "satin"),
        "thickness": float(p.get("thickness", 0.16)),
        "bevel": float(p.get("bevel", 0.04)),
    }


def norm_canvas(c: Any) -> dict:
    c = _d(c)
    return {
        "platform": str(c.get("platform") or "ios"),
        "shape": c.get("shape") if c.get("shape") in ("squircle", "circle", "rounded", "square", "none") else "squircle",
        "cornerRadius": float(c.get("cornerRadius", 0.225)),
        "plate": norm_plate(c.get("plate")),
        "art": _over(ART_TRANSFORM, c.get("art")),
    }


def norm_layer_override(o: Any) -> dict:
    o = _d(o)
    return {
        "fill": norm_fill(o["fill"]) if isinstance(o.get("fill"), dict) else None,
        "opacity": float(o["opacity"]) if o.get("opacity") is not None else None,
        "visible": bool(o["visible"]) if o.get("visible") is not None else None,
        "blendMode": o.get("blendMode"),
        "material": norm_material(o["material"]) if isinstance(o.get("material"), dict) else None,
    }


def norm_appearance_override(a: Any) -> dict:
    a = _d(a)
    pf = a.get("plateFill")
    return {
        "plateFill": norm_fill(pf) if isinstance(pf, dict) else None,
        "layers": {str(k): norm_layer_override(v) for k, v in _d(a.get("layers")).items()},
    }


def norm_appearances(a: Any) -> dict:
    a = _d(a)
    # models.Appearances.dark defaults to plateFill=system-dark only when the key is absent altogether
    dark = a["dark"] if isinstance(a.get("dark"), dict) else {"plateFill": {"type": "system-dark"}}
    return {
        "dark": norm_appearance_override(dark),
        "mono": norm_appearance_override(a.get("mono")),
        "tint": _over(TINT, a.get("tint")),
    }


def norm_project(p: Any) -> dict:
    """Full Project dict with every default filled in (see models.Project)."""
    p = _d(p)
    src = _d(p.get("source"))
    return {
        "version": int(p.get("version", 1)),
        "id": str(p.get("id", "project")),
        "name": str(p.get("name", "Untitled")),
        "createdAt": str(p.get("createdAt", "")),
        "updatedAt": str(p.get("updatedAt", "")),
        "source": {"filename": str(src.get("filename", "")), "viewBox": list(src.get("viewBox") or (0, 0, 1, 1)),
                   "warnings": list(src.get("warnings") or []), "plateDetected": bool(src.get("plateDetected", False)),
                   "fullBleed": bool(src.get("fullBleed", False))},
        "strategy": str(p.get("strategy") or "smart"),
        "elements": list(p.get("elements") or []),
        "layers": [norm_layer(L, i) for i, L in enumerate(p.get("layers") or [])],
        "canvas": norm_canvas(p.get("canvas")),
        "lighting": _over(LIGHTING, p.get("lighting")),
        "camera": _over(CAMERA, p.get("camera")),
        "appearance": p.get("appearance") if p.get("appearance") in APPEARANCE_IDS else "light",
        "appearances": norm_appearances(p.get("appearances")),
        "render": _over(RENDER_SETTINGS, p.get("render")),
    }


def norm_camera(c: Any, base: Optional[dict] = None) -> dict:
    return _over(base or CAMERA, c)


# ------------------------------------------------------------------------------------------------
# geometry bundle
# ------------------------------------------------------------------------------------------------
def norm_spline(s: Any) -> dict:
    s = _d(s)
    return {"closed": bool(s.get("closed", True)), "hole": bool(s.get("hole", False)),
            "parent": int(s.get("parent", -1) if s.get("parent") is not None else -1),
            "depth": int(s.get("depth", 0)), "points": list(s.get("points") or [])}


def norm_layer_geometry(g: Any, layer_id: str = "") -> dict:
    g = _d(g)
    return {
        "layerId": str(g.get("layerId", layer_id)),
        "hash": str(g.get("hash", "")),
        "silhouette": [norm_spline(s) for s in g.get("silhouette") or []],
        "regions": [{"elementId": str(_d(r).get("elementId", f"r{i}")),
                     "paint": norm_fill(_d(r).get("paint"), {"type": "solid", "color": "#ffffff", "opacity": 1.0}),
                     "opacity": float(_d(r).get("opacity", 1.0)),
                     "zSub": float(_d(r).get("zSub", 0.0)),
                     "splines": [norm_spline(s) for s in _d(r).get("splines") or []]}
                    for i, r in enumerate(g.get("regions") or [])],
        "safeRadius": float(g.get("safeRadius", 1.0)),
        "bbox": list(g.get("bbox") or (-1.0, -1.0, 1.0, 1.0)),
        "texture": str(g.get("texture", "")),
        "texturePath": str(g.get("texturePath", "")),
        "svg": str(g.get("svg", "")),
        "images": [dict(_d(im)) for im in g.get("images") or []],
    }


def norm_bundle(b: Any) -> dict:
    b = _d(b)
    return {
        "projectId": str(b.get("projectId", "")),
        "hash": str(b.get("hash", "")),
        "viewBox": list(b.get("viewBox") or (0, 0, 1, 1)),
        "plate": b.get("plate"),
        "layers": {str(k): norm_layer_geometry(v, str(k)) for k, v in _d(b.get("layers")).items()},
    }
