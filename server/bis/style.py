"""Looks, style extraction and style transfer (PLAN §10).

* :func:`resolve_look`  — a named look from ``shared/presets.json`` ("looks") → :class:`StyleSpec`
  (the look's partial style deep-merged over the StyleSpec defaults).
* :func:`extract_style` — the transferable style of a project ("Copy style").
* :func:`apply_style`   — apply a StyleSpec to a project (pure; see the StyleSpec docstring in models.py).
* :func:`restyle_project` — load → max radii (``bis.svg.build_geometry``, hash-cached) → apply → save.

Apply rules (StyleSpec): every layer gets ``layerDefaults`` (material/depth/shadow) or
``layerMaterials[i]`` (by index from the bottom, clamped to the last entry); the bevel is clamped to thickness / 2
(the only clamp - height-field bodies taper thin parts; round 7 dropped the safe-radius clamp); layers of soft-alpha
rasters stay flat cards (rounds 8 + 9: no dome, at most ``bis.stacking.IMAGE_CARD`` thick; ``Element.softAlpha`` -
crisp rasters take the look's depth like vector art); layers are re-stacked at
their REAL heights, overlap-aware (PLAN §11 rounds 7 + 8, :mod:`bis.stacking`): a layer stacks only above the lower
layers it overlaps in XY, z(i) = max(stackLift, max over overlapped lower j of z(j) + H(j) + gap) with
H = max(thickness + 2 · inflate · maxRadius · S, in-layer stacked height) and gap = ``zGap`` (None → the presets'
stackGap) - so bodies never interpenetrate, whatever the look's thickness and inflate, and layers side by side
share the base; plate material/thickness/bevel are copied (+ fill/shape when not None); lighting, camera,
``render.colorMode`` and ``appearances.tint`` are copied when given. Per-layer *material* overrides of the dark/mono appearances and the
per-shape overrides (``Layer.elementMaterials``) are dropped so the look shows in every rendition and on every
shape.

Materials (round 6, PLAN §11): ONE Principled BSDF per shape; a MaterialSpec's ``params`` are overrides keyed by the
shared Principled schema of presets.json. Every style that enters the server (a look, a pasted StyleSpec, another
project's style) goes through :func:`clean_style`: params outside the schema are dropped (legacy renames kept,
see ``bis.materials``), the legacy shadow kinds ``neutral`` / ``chromatic`` become ``physical`` and
``camera.explode`` is reset to 1 (real distances; the POV is ``camera.iso``). Per-shape overrides are never part
of a style - element ids belong to one icon.

Layer mode (round 5): ``layerDefaults.mode`` None — every look in presets.json, and every extracted style
unless its source's user fused its layers into 'combined' on purpose — keeps each layer's own mode. The SVG pipeline derives
that mode per icon from its art (tiles of one shape → one 'combined' body, ``bis.svg.tiling``), so a look or an
Icon Pack never turns a tiled icon's 'combined' layer back into seamed 'individual' pieces. A non-None mode (a
pasted/explicit StyleSpec) is applied to every layer, and the stack uses the max radius of that mode's bodies.
"""
from __future__ import annotations

import copy
import logging
from collections import Counter
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from . import stacking
from .materials import clean_material, normalize_camera, normalize_shadow, param_schema
from .models import Project, StyleLayerDefaults, StylePlate, StyleSpec

if TYPE_CHECKING:  # pragma: no cover
    from .projects import ProjectStore

log = logging.getLogger("bis.style")

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
    """The StyleSpec of a named look: ``looks[look_id].style`` merged over the StyleSpec defaults (cleaned)."""
    look = (_raw(presets).get("looks") or {}).get(look_id) if isinstance(look_id, str) else None
    if not isinstance(look, Mapping):
        raise LookNotFound(look_id)
    partial = look.get("style") or {}
    return clean_style(StyleSpec.model_validate(deep_merge(StyleSpec().model_dump(mode="json"), partial)), presets)


def clean_style(style: StyleSpec, presets: Any) -> StyleSpec:
    """`style` (in place, returned) on the §11 contract: material params restricted to the Principled schema,
    legacy shadow kinds → 'physical', legacy ``camera.explode`` → 1."""
    schema = param_schema(presets)
    for spec in (style.layerDefaults.material, *(style.layerMaterials or []), style.plate.material):
        clean_material(spec, schema)
    normalize_shadow(style.layerDefaults.shadow)
    normalize_camera(style.camera)
    return style


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
                  auto_modes: Optional[Mapping[str, str]] = None,
                  max_radii: Optional[Mapping[str, Any]] = None) -> StyleSpec:
    """The transferable look of `project` ("Copy style").

    * layerDefaults: material/shadow of the dominant material among the visible layers (most layers, ties →
      the higher one; its top-most layer is the representative). The bevel is the largest bevel among those
      layers (builder clamping only ever lowers a bevel, so the largest is closest to what was requested). Flat
      cards (soft-alpha raster layers) only give the depth when the project has no other layer.
      ``mode`` is None unless the user fused the visible layers into 'combined' bodies on purpose — see
      :func:`extracted_mode`; `auto_modes` (layer id → the pipeline's auto mode, :meth:`ProjectStore.auto_modes`)
      tells the two apart. Without it the mode is never copied.
    * layerMaterials: every layer's material by index from the bottom — only when they are not all equal.
    * zGap: median clearance of the stacked layers, z(i) − max over its overlapped lower layers j of
      (z(j) + H(j)) ≥ 0 (overlap-aware real heights: `max_radii` = layer id → ``bis.stacking.LayerShape`` /
      ``LayerGeometry.maxRadius`` (footprint unknown: every layer overlaps), else a bbox bound); None when no layer
      sits on another (a single layer, side-by-side layers: the target then uses the presets' stackGap).
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
        cards = stacking.card_elements(project.elements)
        shaped = [l for l in same if not stacking.is_card_layer(l, cards)] or same   # cards are not the look's depth
        depth = shaped[-1].depth.model_copy(deep=True)
        depth.z = 0.0
        depth.bevel = max(l.depth.bevel for l in shaped)
        defaults = StyleLayerDefaults(material=rep.material.model_copy(deep=True), depth=depth,
                                      shadow=rep.shadow.model_copy(deep=True),
                                      mode=extracted_mode(visible, auto_modes))
        mats = [l.material.model_copy(deep=True) for l in layers]
        if any(m != defaults.material for m in mats):
            layer_materials = mats

    z_gap: Optional[float] = None
    if len(layers) >= 2:
        radii = radii_or_bbox(project, max_radii)
        gaps = sorted(g for g in stacking.stack_clearances(layers, radii, art_scale=project.canvas.art.scale,
                                                           art_offset=project.canvas.art) if g is not None)
        if gaps:
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
def clamp_bevel(bevel: float, thickness: float) -> float:
    """PLAN §11 round 7: the round-edge radius is clamped to thickness / 2 only (height-field bodies taper thin
    parts, so no safe-radius clamp). A clamped value is rounded *down* (5 decimals), never a hair above."""
    return stacking.clamp_bevel(bevel, thickness)


def bbox_radii(project: Project) -> dict[str, float]:
    """Fallback max radius per layer when no geometry bundle is at hand: half the smaller side of the union bbox
    of its elements (an upper bound - the stack never under-estimates a body's height)."""
    boxes = {e.id: e.bbox for e in project.elements}
    return {l.id: stacking.bbox_radius([boxes[i] for i in l.elementIds if i in boxes]) for l in project.layers}


def element_kinds(project: Project) -> dict[str, str]:
    """Element id → kind ('path' / 'image')."""
    return {e.id: e.kind for e in project.elements}


def radii_or_bbox(project: Project, max_radii: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """`max_radii` (layer id → ``bis.stacking.LayerShape`` / ``LayerGeometry.maxRadius``) with every missing layer
    filled by :func:`bbox_radii` (footprint unknown: it stacks on every lower layer - never under-estimated)."""
    given = dict(max_radii or {})
    if all(l.id in given for l in project.layers):
        return given
    fallback = bbox_radii(project)
    # given values stay as they are (a LayerShape or a number: bis.stacking.as_shape reads both)
    return {l.id: given[l.id] if l.id in given else fallback[l.id] for l in project.layers}


def apply_style(project: Project, style: StyleSpec, max_radii: Optional[Mapping[str, Any]] = None,
                presets: Any = None) -> Project:
    """Return a restyled deep copy of `project` (the input is not modified). `max_radii` maps layer id →
    ``bis.stacking.LayerShape`` (maxRadius + XY footprint + pieces of the mode each layer ends up with) or a bare
    ``LayerGeometry.maxRadius`` (footprint unknown); missing layers use a bbox bound - for the overlap-aware
    real-height stack; `presets` (PresetStore / dict / None = shared/presets.json) gives stackLift / stackGap.
    ``layerDefaults.mode`` None keeps every layer's own mode. Layers of soft-alpha rasters stay flat cards (crisp
    rasters are bodies: the look's depth)."""
    p = project.model_copy(deep=True)
    ld = style.layerDefaults
    mats = list(style.layerMaterials or [])
    cards = stacking.card_elements(p.elements)
    for i, layer in enumerate(p.layers):
        material = mats[min(i, len(mats) - 1)] if mats else ld.material
        layer.material = material.model_copy(deep=True)
        layer.elementMaterials = {}   # the look replaces per-shape tweaks too (element ids are icon-specific)
        depth = ld.depth.model_copy(deep=True)
        if stacking.is_card_layer(layer, cards):
            stacking.card_depth(depth)   # a soft raster is a flat card under every look (PLAN §11 rounds 8 + 9)
        depth.bevel = clamp_bevel(depth.bevel, depth.thickness)
        layer.depth = depth
        layer.shadow = ld.shadow.model_copy(deep=True)
        normalize_shadow(layer.shadow)
        if ld.mode is not None:
            layer.mode = ld.mode
    stacking.restack(p.layers, radii_or_bbox(p, max_radii), gap=style.zGap, art_scale=p.canvas.art.scale,
                     presets=presets, art_offset=p.canvas.art)
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
        normalize_camera(p.camera)
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
    LookNotFound, or the store's ProjectNotFound for an unknown ``fromProject``. The result is cleaned
    (:func:`clean_style`)."""
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
        return clean_style(req.style.model_copy(deep=True), presets)
    if extract is not None:
        return clean_style(extract(req.fromProject), presets)
    return clean_style(extract_style(load_project(req.fromProject)), presets)


def project_style(store: "ProjectStore", pid: str, *, plate_fill: Optional[bool] = None,
                  plate_shape: bool = False) -> StyleSpec:
    """:func:`extract_style` of the stored project `pid`, with the pipeline's auto layer modes (so a mode the
    user picked on purpose is copied and an art-derived one is not) and its layers' max radii (the copied zGap
    is the clearance between real body heights)."""
    project = store.load(pid)
    try:
        radii: Optional[dict[str, Any]] = project_layer_shapes(store, project)
    except Exception as e:  # noqa: BLE001 - a bbox bound still gives a sensible gap
        log.warning("layer shapes of %s unavailable (%s); using bbox bounds", pid, e)
        radii = None
    return extract_style(project, plate_fill=plate_fill, plate_shape=plate_shape,
                         auto_modes=store.auto_modes(project), max_radii=radii)


def project_layer_shapes(store: "ProjectStore", project: Project, mode: Optional[str] = None) -> dict[str, Any]:
    """``bis.stacking.LayerShape`` (maxRadius, XY footprint, pieces) of every layer of `project` (built as `mode`
    when given, else each layer's own mode) - from the SVG pipeline's element store (no geometry bundle needed);
    a pipeline without that op (tests' fake): from the hash-cached geometry bundle (the following render reuses
    it)."""
    shapes = store.layer_shapes(project, mode) if hasattr(store, "layer_shapes") else None
    if shapes is not None:
        return shapes
    probe = project
    if mode is not None:
        probe = project.model_copy(deep=True)
        for layer in probe.layers:
            layer.mode = mode  # type: ignore[assignment]
    bundle, _ = store.geometry(probe)
    return stacking.shapes_from_bundle(bundle, probe.layers, probe.canvas.art.scale)


def project_max_radii(store: "ProjectStore", project: Project, mode: Optional[str] = None) -> dict[str, float]:
    """``LayerGeometry.maxRadius`` of every layer of `project` (built as `mode` when given, else each layer's own)."""
    return {lid: float(stacking.as_shape(sh).maxRadius)
            for lid, sh in project_layer_shapes(store, project, mode).items()}


def layer_max_radii(store: "ProjectStore", project: Project, style: StyleSpec) -> dict[str, Any]:
    """The shapes of every layer's bodies *as the style will build them* (maxRadius and pieces depend on the layer
    mode; a None style mode keeps each layer's own) - the overlap-aware real-height stack of :func:`apply_style`."""
    return project_layer_shapes(store, project, style.layerDefaults.mode)


def restyle_project(store: "ProjectStore", pid: str, style: StyleSpec) -> Project:
    """Apply `style` to the stored project `pid` and save it (emits the project "saved" event)."""
    from .pipeline import SvgPipelineUnavailable

    with store.lock(pid):
        project = store.load(pid)
        try:
            radii = layer_max_radii(store, project, style)
        except SvgPipelineUnavailable:
            raise
        except Exception as e:  # a style must still apply: bbox bounds over-estimate heights, never under
            log.warning("layer shapes for %s unavailable (%s); stacking with bbox bounds", pid, e)
            radii = {}
        return store.save(apply_style(project, style, radii, presets=store.presets))
