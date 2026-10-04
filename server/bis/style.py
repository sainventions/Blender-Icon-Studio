"""Looks, style extraction and style transfer (PLAN §10).

* :func:`resolve_look`  — a named look from ``shared/presets.json`` ("looks") → :class:`StyleSpec`
  (the look's partial style deep-merged over the StyleSpec defaults).
* :func:`extract_style` — the transferable style of a project ("Copy style").
* :func:`apply_style`   — apply a StyleSpec to a project (pure; see the StyleSpec docstring in models.py).
* :func:`restyle_project` — load → safe radii (``bis.svg.build_geometry``, hash-cached) → apply → save.

Apply rules (StyleSpec): every layer gets ``layerDefaults`` (material/depth/shadow) or
``layerMaterials[i]`` (by index from the bottom, clamped to the last entry); the requested bevel is clamped to
``0.9 × safeRadius`` of each layer; layers are restacked ``z_i = i × zGap`` (``zGap`` None = keep z); plate
material/thickness/bevel are copied (+ fill/shape when not None); lighting, camera, ``render.colorMode`` and
``appearances.tint`` are copied when given. Per-layer *material* overrides of the dark/mono appearances are
dropped so the look shows in every rendition.

Layer mode (round 5): ``layerDefaults.mode`` None — every look in presets.json, and every extracted style
unless its source's user fused its layers into 'combined' on purpose — keeps each layer's own mode. The SVG pipeline derives
that mode per icon from its art (tiles of one shape → one 'combined' body, ``bis.svg.tiling``), so a look or an
Icon Pack never turns a tiled icon's 'combined' layer back into seamed 'individual' pieces. A non-None mode (a
pasted/explicit StyleSpec) is applied to every layer, and the bevel is clamped to that mode's safe radius.
"""
from __future__ import annotations

import copy
import logging
import math
from collections import Counter
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from .models import Project, StyleLayerDefaults, StylePlate, StyleSpec

if TYPE_CHECKING:  # pragma: no cover
    from .projects import ProjectStore

log = logging.getLogger("bis.style")

BEVEL_SAFE_FACTOR = 0.9
#: plate fills that are a deliberate design choice (not the icon's own source colour) — copied by extract_style
DESIGN_PLATE_FILLS = ("system-light", "system-dark", "none")


class LookNotFound(KeyError):
    def __str__(self) -> str:
        return f"Look not found: {self.args[0] if self.args else ''}"


class StyleError(ValueError):
    """An invalid style request (e.g. not exactly one of look / style / fromProject)."""


# ---------------------------------------------------------------------------------------------- looks
def _raw(presets: Any) -> Mapping[str, Any]:
    if presets is None:
        return {}
    if hasattr(presets, "raw"):  # PresetStore
        return presets.raw()
    return presets


def looks(presets: Any) -> dict[str, Any]:
    """All looks of presets.json (deep copy): ``{id: {label, description, style}}``."""
    return copy.deepcopy(dict(_raw(presets).get("looks") or {}))


def deep_merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    """Recursive dict merge: mappings merge key by key, everything else (lists, scalars, None) replaces."""
    out = copy.deepcopy(dict(base))
    for k, v in over.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), Mapping):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def resolve_look(presets: Any, look_id: str) -> StyleSpec:
    """The StyleSpec of a named look: ``looks[look_id].style`` merged over the StyleSpec defaults."""
    look = (_raw(presets).get("looks") or {}).get(look_id) if isinstance(look_id, str) else None
    if not isinstance(look, Mapping):
        raise LookNotFound(look_id)
    partial = look.get("style") or {}
    return StyleSpec.model_validate(deep_merge(StyleSpec().model_dump(mode="json"), partial))


# ---------------------------------------------------------------------------------------------- extract
def extracted_mode(layers: list, auto_modes: Optional[Mapping[str, str]]) -> Optional[str]:
    """The ``layerDefaults.mode`` a copied style carries. Rule (round 5): 'combined' ONLY when the user fused
    the layers on purpose — every given layer is 'combined' and at least one of them is not what the SVG
    pipeline's tiling heuristic picks for its art (`auto_modes`: layer id → auto mode); otherwise None (= keep
    each target layer's own, art-derived mode). A mode is a property of an icon's geometry (tiles of one
    shape vs separate pieces), not of a look: copying an untouched icon's modes would fuse another icon's
    separate pieces or re-seam its tiled layers. 'individual' is never copied: it would turn the targets'
    tiled 'combined' bodies back into seamed pieces (QA round 4 #8), and it cannot be told apart from the
    default of a project saved before the tiling heuristic existed. Unknown auto modes (no SVG pipeline) →
    None."""
    if not layers or not auto_modes:
        return None
    if any(l.mode != "combined" for l in layers):
        return None
    if any(l.id in auto_modes and auto_modes[l.id] != "combined" for l in layers):
        return "combined"
    return None


def extract_style(project: Project, *, plate_fill: Optional[bool] = None, plate_shape: bool = False,
                  auto_modes: Optional[Mapping[str, str]] = None) -> StyleSpec:
    """The transferable look of `project` ("Copy style").

    * layerDefaults: material/shadow of the dominant material among the visible layers (most layers, ties →
      the higher one; its top-most layer is the representative). The bevel is the largest bevel among those
      layers (builder clamping only ever lowers a bevel, so the largest is closest to what was requested).
      ``mode`` is None unless the user fused the visible layers into 'combined' bodies on purpose — see
      :func:`extracted_mode`; `auto_modes` (layer id → the pipeline's auto mode, :meth:`ProjectStore.auto_modes`)
      tells the two apart. Without it the mode is never copied.
    * layerMaterials: every layer's material by index from the bottom — only when they are not all equal.
    * zGap: median z step between neighbouring layers (None for a single layer = keep the target's z).
    * plate: material/thickness/bevel always; ``fill`` when `plate_fill` is True, or (None = auto) when the
      fill is a deliberate system/none fill rather than the icon's own source colour; ``shape`` only when
      `plate_shape`.
    * lighting, camera, render.colorMode and appearances.tint are always included.
    """
    layers = list(project.layers)
    visible = [l for l in layers if l.visible] or layers
    defaults = StyleLayerDefaults()
    layer_materials = None
    if visible:
        counts = Counter(l.material.preset for l in visible)
        top_index = {l.material.preset: i for i, l in enumerate(visible)}  # last (top-most) occurrence
        preset = max(counts, key=lambda k: (counts[k], top_index[k]))
        same = [l for l in visible if l.material.preset == preset]
        rep = same[-1]
        depth = rep.depth.model_copy(deep=True)
        depth.z = 0.0
        depth.bevel = max(l.depth.bevel for l in same)
        defaults = StyleLayerDefaults(material=rep.material.model_copy(deep=True), depth=depth,
                                      shadow=rep.shadow.model_copy(deep=True),
                                      mode=extracted_mode(visible, auto_modes))
        mats = [l.material.model_copy(deep=True) for l in layers]
        if any(m != defaults.material for m in mats):
            layer_materials = mats

    z_gap: Optional[float] = None
    zs = [l.depth.z for l in layers]
    if len(zs) >= 2:
        gaps = sorted(b - a for a, b in zip(zs, zs[1:]))
        z_gap = round(max(gaps[len(gaps) // 2], 0.0), 5)

    plate = project.canvas.plate
    copy_fill = plate_fill if plate_fill is not None else plate.fill.type in DESIGN_PLATE_FILLS
    style_plate = StylePlate(
        material=plate.material.model_copy(deep=True),
        thickness=plate.thickness,
        bevel=plate.bevel,
        fill=plate.fill.model_copy(deep=True) if copy_fill else None,
        shape=project.canvas.shape if plate_shape else None,
    )
    return StyleSpec(
        layerDefaults=defaults,
        layerMaterials=layer_materials,
        zGap=z_gap,
        plate=style_plate,
        lighting=project.lighting.model_copy(deep=True),
        camera=project.camera.model_copy(deep=True),
        colorMode=project.render.colorMode,
        tint=project.appearances.tint.model_copy(deep=True),
    )


# ---------------------------------------------------------------------------------------------- apply
def clamp_bevel(bevel: float, safe_radius: float) -> float:
    """PLAN D3: never exceed 0.9 × the layer's safe radius (a bevel larger than that inverts thin features).
    A clamped value is rounded *down* (5 decimals) so it never ends up a hair above the limit."""
    limit = max(0.0, BEVEL_SAFE_FACTOR * float(safe_radius))
    if float(bevel) <= limit:
        return round(max(float(bevel), 0.0), 5)
    return math.floor(limit * 1e5) / 1e5


def apply_style(project: Project, style: StyleSpec, safe_radii: Optional[Mapping[str, float]] = None) -> Project:
    """Return a restyled deep copy of `project` (the input is not modified). `safe_radii` maps layer id →
    ``LayerGeometry.safeRadius`` (of the mode each layer ends up with); layers missing from it keep the requested
    bevel unclamped. ``layerDefaults.mode`` None keeps every layer's own mode."""
    p = project.model_copy(deep=True)
    radii = safe_radii or {}
    ld = style.layerDefaults
    mats = list(style.layerMaterials or [])
    for i, layer in enumerate(p.layers):
        material = mats[min(i, len(mats) - 1)] if mats else ld.material
        layer.material = material.model_copy(deep=True)
        depth = ld.depth.model_copy(deep=True)
        depth.z = round(i * float(style.zGap), 5) if style.zGap is not None else layer.depth.z
        sr = radii.get(layer.id)
        if sr is not None:
            depth.bevel = clamp_bevel(depth.bevel, sr)
        layer.depth = depth
        layer.shadow = ld.shadow.model_copy(deep=True)
        if ld.mode is not None:
            layer.mode = ld.mode
    for ov in (p.appearances.dark, p.appearances.mono):
        for lo in ov.layers.values():
            lo.material = None

    plate = p.canvas.plate
    sp = style.plate
    plate.material = sp.material.model_copy(deep=True)
    plate.thickness = sp.thickness
    plate.bevel = sp.bevel
    if sp.fill is not None:
        plate.fill = sp.fill.model_copy(deep=True)
    if sp.shape is not None:
        p.canvas.shape = sp.shape
    if style.lighting is not None:
        p.lighting = style.lighting.model_copy(deep=True)
    if style.camera is not None:
        p.camera = style.camera.model_copy(deep=True)
    if style.colorMode is not None:
        p.render.colorMode = style.colorMode
    if style.tint is not None:
        p.appearances.tint = style.tint.model_copy(deep=True)
    return p


# ---------------------------------------------------------------------------------------------- requests / storage
def resolve_style_request(
    req: Any,
    presets: Any,
    load_project: Callable[[str], Project],
    *,
    allow_none: bool = False,
    extract: Optional[Callable[[str], StyleSpec]] = None,
) -> Optional[StyleSpec]:
    """StyleSpec named by a StyleRequest / BatchRequest (exactly one of look / style / fromProject).
    `extract` (project id → its style; default: :func:`extract_style` of `load_project`, which never copies
    layer modes) — the server passes :func:`project_style`, which knows the pipeline's auto modes.

    Raises StyleError (none or several given; none is allowed with `allow_none` → returns None),
    LookNotFound, or the store's ProjectNotFound for an unknown ``fromProject``."""
    given = [n for n in ("look", "style", "fromProject") if getattr(req, n, None) is not None]
    if len(given) > 1:
        raise StyleError(f"Give exactly one of look, style or fromProject (got {', '.join(given)})")
    if not given:
        if allow_none:
            return None
        raise StyleError("Give exactly one of look, style or fromProject")
    if req.look is not None:
        return resolve_look(presets, req.look)
    if req.style is not None:
        return req.style.model_copy(deep=True)
    if extract is not None:
        return extract(req.fromProject)
    return extract_style(load_project(req.fromProject))


def project_style(store: "ProjectStore", pid: str, *, plate_fill: Optional[bool] = None,
                  plate_shape: bool = False) -> StyleSpec:
    """:func:`extract_style` of the stored project `pid`, with the pipeline's auto layer modes (so a mode the
    user picked on purpose is copied and an art-derived one is not)."""
    project = store.load(pid)
    return extract_style(project, plate_fill=plate_fill, plate_shape=plate_shape,
                         auto_modes=store.auto_modes(project))


def layer_safe_radii(store: "ProjectStore", project: Project, style: StyleSpec) -> dict[str, float]:
    """Safe radius of every layer *as the style will build it* (the radius depends on the layer mode; a None
    style mode keeps each layer's own). Uses the hash-cached geometry bundle, so the following render reuses it."""
    probe = project
    if style.layerDefaults.mode is not None:
        probe = project.model_copy(deep=True)
        for layer in probe.layers:
            layer.mode = style.layerDefaults.mode
    bundle, _ = store.geometry(probe)
    return {lid: float(lg.safeRadius) for lid, lg in bundle.layers.items()}


def restyle_project(store: "ProjectStore", pid: str, style: StyleSpec) -> Project:
    """Apply `style` to the stored project `pid` and save it (emits the project "saved" event)."""
    from .pipeline import SvgPipelineUnavailable

    with store.lock(pid):
        project = store.load(pid)
        try:
            radii = layer_safe_radii(store, project, style)
        except SvgPipelineUnavailable:
            raise
        except Exception as e:  # the builder clamps bevels as well; a style must still apply
            log.warning("safe radii for %s unavailable (%s); bevels left unclamped", pid, e)
            radii = {}
        return store.save(apply_style(project, style, radii))
