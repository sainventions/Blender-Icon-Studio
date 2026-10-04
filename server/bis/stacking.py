"""Real-height layer stacking (PLAN §11 round 7; ``shared/presets.json`` "geometry").

Every layer is a set of height-field bodies whose lowest point sits on ``depth.z`` (the worker lifts inflated layers
so the mirrored dome never dips below z). Its height is

    H = thickness + 2 · inflate · maxRadius · S

* ``maxRadius`` = ``LayerGeometry.maxRadius``: the largest inscribed-circle radius over the layer's bodies, in ART
  units (the dome of a body with inscribed radius D rises inflate · D above the flat half height);
* ``S`` = ``canvas.art.scale`` × ``layer.transform.scale`` (art units → canvas / Blender units: thickness is a
  canvas length, the worker scales a body's D by S as well).

Layers stack bottom → top (EVERY layer: a hidden one keeps its slot so toggling it never moves the others)::

    z0 = stackLift,  z(i+1) = z(i) + H(i) + gap          gap = StyleSpec.zGap when given, else stackGap

Used by the SVG pipeline's import defaults and structural edits (``bis.svg.layers``), by looks / pasted / copied
styles (``bis.style.apply_style``) and so by the Icon Pack batch. The bevel (round-edge radius) is clamped to
thickness / 2 only - height-field bodies taper thin parts, so no safe-radius clamp is needed.

Pure Python (no pydantic import): works on ``bis.models.Layer`` objects or anything with the same attributes.
"""
from __future__ import annotations

import json
import math
import os
import threading
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

#: fallbacks when presets.json has no "geometry" section (or cannot be read)
DEFAULT_RULES: dict[str, float] = {"stackLift": 0.0, "stackGap": 0.03}
#: z values are rounded to this many decimals (stable JSON, exact comparisons in tests)
Z_DECIMALS = 5
#: two z values closer than this are "the same" when a stack is recognised
Z_TOL = 2e-4
#: the pre-round-7 default stack (z_i = i × 0.13) - recognised so legacy projects re-stack on structural edits
LEGACY_STEP = 0.13

_FILE_LOCK = threading.Lock()
_FILE_CACHE: dict[str, Any] = {"key": None, "rules": None}


# ---------------------------------------------------------------------------------------------- rules
def _presets_path() -> Path:
    root = os.environ.get("BIS_ROOT")
    if root and (Path(root) / "shared" / "presets.json").is_file():
        return Path(root) / "shared" / "presets.json"
    return Path(__file__).resolve().parents[2] / "shared" / "presets.json"


def _rules_from(raw: Mapping[str, Any] | None) -> dict[str, float]:
    out = dict(DEFAULT_RULES)
    geo = raw.get("geometry") if isinstance(raw, Mapping) else None
    if isinstance(geo, Mapping):
        for key in DEFAULT_RULES:
            v = geo.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
                out[key] = max(0.0, float(v))
    return out


def geometry_rules(presets: Any = None) -> dict[str, float]:
    """``{"stackLift", "stackGap"}`` from presets.json "geometry". `presets` = a PresetStore, the raw presets dict,
    or None (read ``shared/presets.json`` from the repository, re-read when it changes)."""
    if presets is not None:
        raw = presets.raw() if hasattr(presets, "raw") else presets
        return _rules_from(raw)
    path = _presets_path()
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return dict(DEFAULT_RULES)
    with _FILE_LOCK:
        if _FILE_CACHE["key"] == key and _FILE_CACHE["rules"] is not None:
            return dict(_FILE_CACHE["rules"])
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):   # caught mid-write (edited live): the last good copy, else the defaults
            return dict(_FILE_CACHE["rules"] or DEFAULT_RULES)
        rules = _rules_from(raw)
        _FILE_CACHE.update(key=key, rules=rules)
        return dict(rules)


# ---------------------------------------------------------------------------------------------- heights
def _num(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def clamp_bevel(bevel: float, thickness: float) -> float:
    """The round-edge radius a body can have: 0 .. thickness / 2 (rounded DOWN to 5 decimals, never above)."""
    limit = max(0.0, _num(thickness)) / 2.0
    b = max(0.0, _num(bevel))
    if b <= limit:
        return round(b, 5)
    return math.floor(limit * 1e5) / 1e5


def body_height(depth: Any, max_radius: float, scale: float = 1.0) -> float:
    """H = thickness + 2 · inflate · maxRadius · scale (canvas units) of a layer with `depth` (LayerDepth)."""
    t = max(0.0, _num(getattr(depth, "thickness", 0.0)))
    k = min(1.0, max(0.0, _num(getattr(depth, "inflate", 0.0))))
    return t + 2.0 * k * max(0.0, _num(max_radius)) * max(0.0, _num(scale, 1.0))


def layer_scale(layer: Any, art_scale: float = 1.0) -> float:
    """S of a layer: canvas.art.scale × layer.transform.scale."""
    tr = getattr(layer, "transform", None)
    return max(0.0, _num(art_scale, 1.0)) * max(0.0, _num(getattr(tr, "scale", 1.0), 1.0))


def _radius(radii: Mapping[str, float] | Sequence[float] | None, layer: Any, i: int) -> float:
    if radii is None:
        return 0.0
    if isinstance(radii, Mapping):
        return max(0.0, _num(radii.get(getattr(layer, "id", None), 0.0)))
    return max(0.0, _num(radii[i])) if i < len(radii) else 0.0


def heights(layers: Sequence[Any], radii: Mapping[str, float] | Sequence[float] | None,
            art_scale: float = 1.0) -> list[float]:
    """Body height H of every layer (`radii`: layer id → maxRadius, or a list by index; missing = 0)."""
    return [body_height(L.depth, _radius(radii, L, i), layer_scale(L, art_scale)) for i, L in enumerate(layers)]


def stack_z(hs: Sequence[float], gap: float, lift: float = 0.0) -> list[float]:
    """z of every layer for heights `hs`: z0 = lift, z(i+1) = z(i) + H(i) + gap."""
    out, z = [], max(0.0, _num(lift))
    g = max(0.0, _num(gap))
    for h in hs:
        out.append(round(z, Z_DECIMALS))
        z += h + g
    return out


def restack(layers: Sequence[Any], radii: Mapping[str, float] | Sequence[float] | None, *,
            gap: Optional[float] = None, lift: Optional[float] = None, art_scale: float = 1.0,
            presets: Any = None) -> list[float]:
    """Set ``depth.z`` of every layer (in place) to the real-height stack; returns the z values. `gap` / `lift`
    None → presets.json "geometry" (stackGap / stackLift)."""
    rules = geometry_rules(presets) if gap is None or lift is None else DEFAULT_RULES
    g = rules["stackGap"] if gap is None else gap
    l0 = rules["stackLift"] if lift is None else lift
    zs = stack_z(heights(layers, radii, art_scale), g, l0)
    for L, z in zip(layers, zs):
        L.depth.z = z
    return zs


def stack_gaps(layers: Sequence[Any], radii: Mapping[str, float] | Sequence[float] | None,
               art_scale: float = 1.0) -> list[float]:
    """Clearance between neighbours: z(i+1) − (z(i) + H(i)) (negative = the bodies may interpenetrate)."""
    hs = heights(layers, radii, art_scale)
    return [_num(b.depth.z) - (_num(a.depth.z) + h) for a, b, h in zip(layers, layers[1:], hs)]


def stack_gap(layers: Sequence[Any], radii: Mapping[str, float] | Sequence[float] | None, *,
              art_scale: float = 1.0, presets: Any = None) -> Optional[float]:
    """The gap of a real-height stack (z0 = stackLift and one gap between every pair of neighbours), the
    stackGap for the pre-round-7 default stack (z_i = i × 0.13, legacy projects) and for a single layer at
    stackLift - None for a custom stack (a user-placed z somewhere)."""
    if not layers:
        return None
    rules = geometry_rules(presets)
    zs = [_num(L.depth.z) for L in layers]
    if all(abs(z - i * LEGACY_STEP) <= Z_TOL for i, z in enumerate(zs)) and len(zs) > 1:
        return rules["stackGap"]
    if abs(zs[0] - rules["stackLift"]) > Z_TOL:
        return None
    gaps = stack_gaps(layers, radii, art_scale)
    if not gaps:
        return rules["stackGap"]
    if max(gaps) - min(gaps) > 2 * Z_TOL or min(gaps) < -Z_TOL:
        return None
    return round(max(0.0, sum(gaps) / len(gaps)), Z_DECIMALS)


def lift_overlaps(layers: Sequence[Any], radii: Mapping[str, float] | Sequence[float] | None, *,
                  gap: Optional[float] = None, art_scale: float = 1.0, presets: Any = None) -> None:
    """A custom stack after a structural edit: keep every z, except that a layer below the top of its lower
    neighbour (a new layer slotted in, a layer that grew) is lifted to sit `gap` above it. In place."""
    g = geometry_rules(presets)["stackGap"] if gap is None else gap
    hs = heights(layers, radii, art_scale)
    for i in range(1, len(layers)):
        lo = _num(layers[i - 1].depth.z) + hs[i - 1]
        if _num(layers[i].depth.z) < lo - Z_TOL:
            layers[i].depth.z = round(lo + g, Z_DECIMALS)


def bbox_radius(boxes: Sequence[Sequence[float]]) -> float:
    """Upper bound of the inscribed radius of art inside the union of `boxes` (art bboxes): half the smaller
    side of their union - the fallback when no geometry bundle is at hand (never under-estimates H)."""
    bs = [b for b in boxes if b and len(b) == 4]
    if not bs:
        return 0.0
    w = max(b[2] for b in bs) - min(b[0] for b in bs)
    h = max(b[3] for b in bs) - min(b[1] for b in bs)
    return max(0.0, min(w, h) / 2.0)
