"""Apple Icon Composer ``.icon`` bundle writer (beta).

A ``.icon`` is a folder: ``<Name>.icon/icon.json`` + ``<Name>.icon/Assets/<files>``. The JSON schema is
community reverse-engineered (docs/research/apple-icon-composer.md §1.10, critic U1) — the export is labelled
beta and only uses Icon Composer 1.x keys (no ``features`` gate), so both IC 1.x and 2.0 can open it.

Mapping (critic C11): each project layer (a depth plane) → one IC **group** holding one IC **layer** whose
image is the layer's standalone SVG; groups are ordered **front → back** (reverse of our bottom → top);
``canvas.plate.fill`` → document ``fill``; dark / mono appearance overrides → ``*-specializations`` with
``appearance: dark`` / ``appearance: tinted``.

Every layer SVG is re-framed so that the canvas square (plate −1..1, after ``canvas.art``) maps exactly onto a
1024×1024 pt artboard; per-layer ``transform`` becomes the group ``position``.
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from .models import Fill, FillLinear, FillRadial, FillSolid, GeometryBundle, Layer, LayerOverride, Project
from .util import atomic_write_text, files_path, safe_filename

log = logging.getLogger("bis.icon_format")

CANVAS_PT = 1024.0
ICON_NAME_MAX = 40        # bundle name (Windows MAX_PATH budget, see export.EXPORT_PATH_BUDGET)
ICON_ASSET_NAME_MAX = 32  # per-layer SVG asset names
SHADOW_KIND = {"neutral": "neutral", "chromatic": "layer-color", "none": "none"}
GLASS_PRESETS = {"liquid_glass", "clear_glass", "frosted_glass", "dispersive_crystal", "tinted_glass", "jelly"}


# ---------------------------------------------------------------------------------------------- colours / fills
def _rgb(hex_color: str) -> tuple[float, float, float]:
    c = (hex_color or "#ffffff").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        return int(c[0:2], 16) / 255.0, int(c[2:4], 16) / 255.0, int(c[4:6], 16) / 255.0
    except ValueError:
        return 1.0, 1.0, 1.0


def color_string(hex_color: str, opacity: float = 1.0) -> str:
    r, g, b = _rgb(hex_color)
    return f"srgb:{r:.5f},{g:.5f},{b:.5f},{max(0.0, min(1.0, opacity)):.5f}"


def _norm_point(x: float, y: float) -> dict[str, float]:
    """Canvas (−1..1, y up) → IC normalised orientation point (0..1, y down)."""
    return {"x": round((x + 1) / 2, 5), "y": round((1 - y) / 2, 5)}


def fill_json(fill: Fill | None, art_to_canvas=None) -> Any:
    """bis Fill → icon.json fill. `art_to_canvas` maps gradient points given in art space (layer fills)."""
    if fill is None:
        return "automatic"
    t = fill.type
    if t == "auto":
        return "automatic"
    if t == "none":
        return "none"
    if t in ("system-light", "system-dark"):
        return t
    if isinstance(fill, FillSolid):
        return {"solid": color_string(fill.color, fill.opacity)}
    if isinstance(fill, FillLinear) and fill.stops:
        s, e = fill.start, fill.end
        if art_to_canvas is not None:
            s, e = art_to_canvas(s), art_to_canvas(e)
        first, last = fill.stops[0], fill.stops[-1]
        return {
            "linear-gradient": [color_string(first.color, first.opacity), color_string(last.color, last.opacity)],
            "orientation": {"start": _norm_point(*s), "stop": _norm_point(*e)},
        }
    if isinstance(fill, FillRadial) and fill.stops:
        # IC has no radial fill: approximate with a vertical two-stop gradient (centre colour on top).
        first, last = fill.stops[0], fill.stops[-1]
        return {
            "linear-gradient": [color_string(first.color, first.opacity), color_string(last.color, last.opacity)],
            "orientation": {"start": {"x": 0.5, "y": 0.0}, "stop": {"x": 0.5, "y": 1.0}},
        }
    return "automatic"


def _specialize(target: dict, key: str, base: Any, dark: Any = None, mono: Any = None, *, include_base: bool = True) -> None:
    """Set `key` (+ `key-specializations` when an appearance differs)."""
    if dark is None and mono is None:
        if include_base:
            target[key] = base
        return
    specs = [{"value": base}]
    if dark is not None:
        specs.append({"appearance": "dark", "value": dark})
    if mono is not None:
        specs.append({"appearance": "tinted", "value": mono})
    target[f"{key}-specializations"] = specs


# ---------------------------------------------------------------------------------------------- svg reframing
def canvas_viewbox(project: Project, viewbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """SVG viewBox that shows exactly the canvas square −1..1 (after canvas.art) of the source art."""
    x, y, w, h = viewbox
    k = 2.0 / max(w, h)
    cx, cy = x + w / 2.0, y + h / 2.0
    s = project.canvas.art.scale or 1.0
    ax, ay = project.canvas.art.x, project.canvas.art.y
    size = (2.0 / s) / k
    return cx + ((-1.0 - ax) / s) / k, cy - ((1.0 - ay) / s) / k, size, size


def reframe_svg(src: Path, dst: Path, viewbox: tuple[float, float, float, float]) -> None:
    try:
        from lxml import etree

        parser = etree.XMLParser(remove_blank_text=False, resolve_entities=False, no_network=True, huge_tree=True)
        tree = etree.parse(str(src), parser)
        root = tree.getroot()
        root.set("viewBox", " ".join(f"{v:.4f}".rstrip("0").rstrip(".") for v in viewbox))
        root.set("width", f"{CANVAS_PT:.0f}")
        root.set("height", f"{CANVAS_PT:.0f}")
        root.set("preserveAspectRatio", "xMidYMid meet")
        dst.parent.mkdir(parents=True, exist_ok=True)
        tree.write(str(dst), xml_declaration=True, encoding="UTF-8")
    except Exception as e:  # keep the export going with the raw SVG
        log.warning("could not reframe %s (%s); copying as-is", src, e)
        shutil.copyfile(src, dst)


# ---------------------------------------------------------------------------------------------- groups
def _param(layer: Layer, name: str, presets: dict | None, default: Any = None) -> Any:
    if name in layer.material.params:
        return layer.material.params[name]
    spec = (((presets or {}).get("materials") or {}).get(layer.material.preset) or {}).get("params", {}).get(name)
    return spec.get("default", default) if isinstance(spec, dict) else default


def _layer_override(project: Project, which: str, lid: str) -> LayerOverride | None:
    ov = getattr(project.appearances, which).layers.get(lid)
    return ov


def group_json(project: Project, layer: Layer, image_name: str, presets: dict | None, art_to_canvas) -> dict:
    is_glass = layer.glass and layer.material.preset in GLASS_PRESETS
    dark = _layer_override(project, "dark", layer.id)
    mono = _layer_override(project, "mono", layer.id)

    ic_layer: dict[str, Any] = {"name": layer.name, "image-name": image_name, "glass": bool(layer.glass)}
    if layer.fill.type != "auto" or (dark and dark.fill) or (mono and mono.fill):
        _specialize(
            ic_layer, "fill", fill_json(layer.fill, art_to_canvas),
            fill_json(dark.fill, art_to_canvas) if dark and dark.fill else None,
            fill_json(mono.fill, art_to_canvas) if mono and mono.fill else None,
        )
    if layer.opacity != 1.0 or (dark and dark.opacity is not None) or (mono and mono.opacity is not None):
        _specialize(ic_layer, "opacity", round(layer.opacity, 4),
                    dark.opacity if dark else None, mono.opacity if mono else None)
    if layer.blendMode != "normal" or (dark and dark.blendMode) or (mono and mono.blendMode):
        _specialize(ic_layer, "blend-mode", layer.blendMode,
                    dark.blendMode if dark else None, mono.blendMode if mono else None)

    group: dict[str, Any] = {"name": layer.name, "layers": [ic_layer]}
    hidden_dark = (not dark.visible) if dark and dark.visible is not None else None
    hidden_mono = (not mono.visible) if mono and mono.visible is not None else None
    if not layer.visible or hidden_dark is not None or hidden_mono is not None:
        _specialize(group, "hidden", not layer.visible, hidden_dark, hidden_mono)
    group["lighting"] = "combined" if layer.mode == "combined" else "individual"
    specular = _param(layer, "specular", presets, "auto")
    group["specular"] = bool(is_glass and specular != "off")
    frost = float(_param(layer, "frost", presets, 0.0) or 0.0)
    if is_glass and frost > 0:
        group["blur-material"] = round(min(1.0, frost / 0.24), 4)  # our default frost 0.12 ≈ IC default 50 %
    translucency = _param(layer, "translucency", presets, None)
    if is_glass:
        group["translucency"] = {"enabled": True, "value": round(float(translucency if translucency is not None else 0.5), 4)}
    else:
        group["translucency"] = {"enabled": False, "value": 0.5}
    group["shadow"] = {"kind": SHADOW_KIND.get(layer.shadow.kind, "neutral"), "opacity": round(layer.shadow.opacity, 4)}
    t = layer.transform
    if t.scale != 1.0 or t.x or t.y:
        group["position"] = {
            "scale": round(t.scale, 5),
            "translation-in-points": [round(t.x * CANVAS_PT / 2, 3), round(-t.y * CANVAS_PT / 2, 3)],
        }
    return group


# ---------------------------------------------------------------------------------------------- writer
def build_icon_json(project: Project, groups: list[dict]) -> dict:
    plate = project.canvas.plate
    doc: dict[str, Any] = {}
    if not plate.visible or project.canvas.shape == "none":
        doc["fill"] = "none"
    else:
        dark_fill = project.appearances.dark.plateFill
        mono_fill = project.appearances.mono.plateFill
        _specialize(doc, "fill", fill_json(plate.fill),
                    fill_json(dark_fill) if dark_fill else None,
                    fill_json(mono_fill) if mono_fill else None)
    doc["groups"] = groups
    doc["supported-platforms"] = {"circles": ["watchOS"], "squares": "shared"}
    return doc


def write_icon_bundle(
    project: Project,
    bundle: GeometryBundle,
    workspace: Path,
    dest_dir: Path,
    name: str | None = None,
    presets: dict | None = None,
) -> Path:
    """Write ``<dest_dir>/<name>.icon``; returns the bundle folder."""
    name = safe_filename(name or project.name, max_len=ICON_NAME_MAX, default="AppIcon")
    icon_dir = dest_dir / f"{name}.icon"
    assets = icon_dir / "Assets"
    if icon_dir.exists():
        shutil.rmtree(icon_dir)
    assets.mkdir(parents=True)
    vb = canvas_viewbox(project, tuple(bundle.viewBox) if bundle.viewBox else tuple(project.source.viewBox))
    s = project.canvas.art.scale or 1.0
    ax, ay = project.canvas.art.x, project.canvas.art.y

    def art_to_canvas(p):
        return p[0] * s + ax, p[1] * s + ay

    groups: list[dict] = []
    used: set[str] = set()
    for idx, layer in enumerate(reversed(project.layers)):  # front → back
        lg = bundle.layers.get(layer.id)
        if lg is None or not lg.svg:
            continue
        try:
            src = files_path(workspace, lg.svg)
        except ValueError:
            src = Path(lg.svg)
        if not src.is_file():
            log.warning("layer SVG missing for %s: %s", layer.id, src)
            continue
        base = safe_filename(layer.name, max_len=ICON_ASSET_NAME_MAX, default=layer.id)
        image_name = f"{base}.svg"
        n = 2
        while image_name.lower() in used:
            image_name = f"{base} {n}.svg"
            n += 1
        used.add(image_name.lower())
        reframe_svg(src, assets / image_name, vb)
        groups.append(group_json(project, layer, image_name, presets, art_to_canvas))
    if len(groups) > 4:
        log.info("%s: %d groups (Icon Composer recommends at most 4)", project.id, len(groups))
    atomic_write_text(icon_dir / "icon.json", json.dumps(build_icon_json(project, groups), indent=2) + "\n")
    return icon_dir
