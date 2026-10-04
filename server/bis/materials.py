"""Principled material params on the server side (PLAN §11).

Every material is ONE Principled BSDF per shape; ``shared/presets.json`` holds the shared param schema (the
28 params every preset lists, grouped like Blender's panel) and presets are only starting values. A
:class:`bis.models.MaterialSpec` stores ``params`` = *overrides* of its preset's defaults; per-shape overrides
live in ``Layer.elementMaterials`` (element id → MaterialSpec, merged over ``Layer.material`` by the renderers).

This module keeps stored documents on that schema:

* :func:`clean_params` — drops params the schema does not know (the round ≤ 5 fakes: ``frost``, ``glow``,
  ``rim``, ``translucency``, ``dispersion``, ``absorption``, ``bloom``, ...; and the viewport's never-saved
  ``__*`` intent flags), renames the few legacy params that ARE a Principled input under a new name
  (:data:`LEGACY_PARAM_RENAMES`), and drops/clamps values of the wrong type or outside the slider range.
* :func:`normalize_project` — what :class:`bis.projects.ProjectStore` applies on every load and save: cleans
  every material (layers, per-shape overrides, appearance overrides, plate), drops per-shape overrides of
  elements that are no longer in their layer, turns the legacy art-directed shadow kinds (``neutral`` /
  ``chromatic``) into ``physical`` (Cycles' true shadow — the only kind besides ``none`` §11 renders) and resets
  the legacy ``camera.explode`` z-gap multiplier to 1 (renders show REAL distances; the CAD-style POV is
  ``camera.iso``).
* :func:`carry_element_materials` — keeps per-shape overrides attached to their elements across layer edits
  (merge / split / move / re-split hand back new Layer objects).

Without a readable schema (presets.json missing) params are left untouched — never wiped.
"""
from __future__ import annotations

import math
import re
from typing import Any, Iterable, Mapping, Optional

from .models import CameraSpec, Layer, LayerShadow, MaterialSpec, Project

#: legacy (round ≤ 5) param → the Principled param it always drove (same units / meaning). Everything else
#: from that era was a fake (emission rims, glow cards, volume tricks) and is dropped.
LEGACY_PARAM_RENAMES: dict[str, str] = {
    "frost": "roughness",            # 'Frost / Blur' was the transmission roughness
    "coat": "coatWeight",
    "sheen": "sheenWeight",
    "subsurface": "subsurfaceWeight",
    "anisotropy": "anisotropic",
    "film": "thinFilmThickness",     # nm
    "filmIor": "thinFilmIor",
    "strength": "emissionStrength",  # neon 'Brightness'
}
#: shadow kinds §11 renders: Cycles' true shadow or none. The rest are legacy names of 'physical'.
SHADOW_KINDS = ("physical", "none")
_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _raw(presets: Any) -> Mapping[str, Any]:
    if presets is None:
        return {}
    if hasattr(presets, "raw"):  # PresetStore
        return presets.raw()
    return presets


def param_schema(presets: Any) -> dict[str, dict]:
    """``{param: spec}`` — the union of every preset's params (they all share the one Principled schema)."""
    out: dict[str, dict] = {}
    for mat in (_raw(presets).get("materials") or {}).values():
        for key, spec in ((mat or {}).get("params") or {}).items():
            if isinstance(spec, Mapping):
                out.setdefault(key, dict(spec))
    return out


def _coerce(value: Any, spec: Mapping[str, Any]) -> Any:
    """`value` valid for `spec` (numbers clamped to the slider range), or None when it cannot be."""
    kind = spec.get("type", "number")
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return None
        lo, hi = spec.get("min"), spec.get("max")
        if isinstance(lo, (int, float)) and value < lo:
            value = lo
        if isinstance(hi, (int, float)) and value > hi:
            value = hi
        return value
    if kind == "enum":
        options = spec.get("options") or []
        return value if isinstance(value, str) and (not options or value in options) else None
    if kind == "bool":
        return value if isinstance(value, bool) else None
    if kind == "color":
        return value if isinstance(value, str) and _HEX.match(value) else None
    return value


def clean_params(params: Mapping[str, Any] | None, schema: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """`params` restricted to the Principled schema (see the module docstring). An empty schema keeps them."""
    params = dict(params or {})
    if not schema:
        return params
    out: dict[str, Any] = {}
    for key, value in params.items():
        spec = schema.get(key)
        if spec is not None:
            v = _coerce(value, spec)
            if v is not None:
                out[key] = v
    for old, new in LEGACY_PARAM_RENAMES.items():
        if old in params and old not in schema and new not in out and new in schema:
            v = _coerce(params[old], schema[new])
            if v is not None:
                out[new] = v
    return out


def clean_material(spec: MaterialSpec, schema: Mapping[str, Mapping[str, Any]]) -> bool:
    """Clean `spec.params` in place → True when anything changed."""
    cleaned = clean_params(spec.params, schema)
    if cleaned == spec.params:
        return False
    spec.params = cleaned
    return True


def normalize_shadow(shadow: LayerShadow) -> bool:
    if shadow.kind in SHADOW_KINDS:
        return False
    shadow.kind = "physical"
    return True


def normalize_camera(camera: Optional[CameraSpec]) -> bool:
    """Legacy ``explode`` → 1 (real distances); ``iso`` clamped to its 0 (head-on) .. 1 (isometric) range."""
    if camera is None:
        return False
    iso = camera.iso if math.isfinite(camera.iso) else 0.0
    iso = min(max(iso, 0.0), 1.0)
    if camera.explode == 1.0 and iso == camera.iso:
        return False
    camera.explode, camera.iso = 1.0, iso
    return True


def prune_element_materials(layer: Layer) -> bool:
    """Drop per-shape overrides of elements that are not (any more) in `layer`."""
    ids = set(layer.elementIds)
    stale = [eid for eid in layer.elementMaterials if eid not in ids]
    for eid in stale:
        del layer.elementMaterials[eid]
    return bool(stale)


def normalize_project(project: Project, presets: Any) -> bool:
    """Bring `project` (in place) onto the §11 contract → True when anything changed. Idempotent."""
    schema = param_schema(presets)
    changed = False
    for layer in project.layers:
        changed |= clean_material(layer.material, schema)
        changed |= prune_element_materials(layer)
        for spec in layer.elementMaterials.values():
            changed |= clean_material(spec, schema)
        changed |= normalize_shadow(layer.shadow)
    for ov in (project.appearances.dark, project.appearances.mono):
        for lo in ov.layers.values():
            if lo.material is not None:
                changed |= clean_material(lo.material, schema)
    changed |= clean_material(project.canvas.plate.material, schema)
    changed |= normalize_camera(project.camera)
    return changed


def _origin(eid: str, known: Mapping[str, Any]) -> Optional[str]:
    """The element an island piece came from (``e5-2`` → ``e5``, bis.svg's island split ids)."""
    base, sep, tail = eid.rpartition("-")
    return base if sep and tail.isdigit() and base in known else None


def carry_element_materials(before: Iterable[Layer], after: Iterable[Layer]) -> None:
    """Re-attach per-shape overrides (by element id) to the layers that hold those elements after a layer
    edit; pieces of an island split inherit their source element's override. In place on `after`."""
    known: dict[str, MaterialSpec] = {}
    for layer in before:
        for eid, spec in layer.elementMaterials.items():
            if eid in layer.elementIds:
                known.setdefault(eid, spec)
    for layer in after:
        mats: dict[str, MaterialSpec] = {}
        for eid in layer.elementIds:
            src = eid if eid in known else _origin(eid, known)
            if src is not None:
                mats[eid] = known[src].model_copy(deep=True)
        layer.elementMaterials = mats
