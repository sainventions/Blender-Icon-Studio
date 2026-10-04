"""Default layers: naming, depth stack, bevel and shadow defaults (PLAN §2 'Default stack on import',
§11 rounds 7 + 8), plus helpers to re-stack layers after structural edits.

Import defaults (round 7): Liquid Glass bodies that read like thick, fully rounded glass - thickness 0.16, round
edge radius = thickness / 2 (a pill edge; height-field bodies taper thin parts, so the bevel is clamped to
thickness / 2 only - no safe-radius clamp), a gentle Poisson dome (inflate 0.25), physical shadows, and REAL-HEIGHT,
OVERLAP-AWARE stacking (:mod:`bis.stacking`, round 8): a layer stacks only above the lower layers it overlaps in XY,
z(i) = max(stackLift, max over overlapped lower j of z(j) + H(j) + stackGap) with H = max(thickness + 2 · inflate ·
maxRadius · S, the in-layer stacked height) (footprints + radii: :func:`bis.svg.geometry.layer_shape`).
Raster image layers (every member an <image>) are flat cards (round 8, ``bis.stacking.IMAGE_CARD``): no dome,
thickness 0.02, round edge 0.006."""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from bis import stacking
from bis.models import Layer, LayerDepth, LayerShadow, MaterialSpec
from .colors import color_name
from .elements import ElementStore, Elem
from .prepass import auto_name

DEFAULT_THICKNESS = 0.16
DEFAULT_BEVEL = 0.08          # = DEFAULT_THICKNESS / 2: fully rounded (pill) edges
DEFAULT_INFLATE = 0.25
DEFAULT_SEGMENTS = 8
DEFAULT_MATERIAL = "liquid_glass"
DEFAULT_SHADOW_OPACITY = 0.5


def default_depth() -> LayerDepth:
    """Depth of a freshly imported layer (z is set by the stack)."""
    return LayerDepth(z=0.0, thickness=DEFAULT_THICKNESS, bevel=stacking.clamp_bevel(DEFAULT_BEVEL, DEFAULT_THICKNESS),
                      bevelSegments=DEFAULT_SEGMENTS, inflate=DEFAULT_INFLATE)


def clamp_bevel(layer: Layer) -> None:
    """Keep a layer's round-edge radius within thickness / 2 (the only clamp: bodies taper thin parts). In place."""
    layer.depth.bevel = stacking.clamp_bevel(layer.depth.bevel, layer.depth.thickness)


def shadow_for(members: Sequence[Elem]) -> LayerShadow:
    """Real (Cycles) shadows on by default (PLAN §11); the opacity keeps the source drop-shadow strength for the
    .icon export and the viewport."""
    ops = [m.shadow["opacity"] for m in members if m.shadow]
    if ops:
        return LayerShadow(kind="physical", opacity=round(min(1.0, max(ops) / 0.6), 4))
    return LayerShadow(kind="physical", opacity=DEFAULT_SHADOW_OPACITY)


def layer_name(members: Sequence[Elem]) -> str:
    """Deepest meaningfully named group shared by all members > meaningful original ids >
    colour names ('Blue', 'White & Blue', 'Red, Yellow +2', 'Image')."""
    if not members:
        return "Empty"
    chains = [m.meta.get("ancestors", []) for m in members]
    shared = None
    for depth in range(min((len(c) for c in chains), default=0)):
        names = {(c[depth].get("name"), c[depth].get("auto_name")) for c in chains}
        if len(names) != 1:
            break
        nm, auto = next(iter(names))
        if nm and not auto:
            shared = nm
    if shared:
        return _pretty(shared)
    ids = []
    for m in members:
        oid = m.meta.get("orig_id")
        if oid and not auto_name(oid) and oid not in ids:
            ids.append(oid)
    if len(ids) == 1:
        return _pretty(ids[0])
    if ids and len(ids) <= 3:
        return " + ".join(_pretty(i) for i in ids)
    if all(m.image for m in members):
        return "Image" if len(members) == 1 else "Images"
    # colour names, ordered by painted area
    by_name: Dict[str, float] = {}
    for m in members:
        nm = "Image" if m.image else color_name(m.rgb)
        by_name[nm] = by_name.get(nm, 0.0) + m.area
    names = sorted(by_name, key=lambda k: -by_name[k])
    if len(names) == 1:
        base = names[0]
        if all(m.role == "stroke" for m in members):
            base += " Outline"
        elif any(m.paint["type"] in ("linear", "radial") for m in members) and not any(m.image for m in members):
            base += " Gradient"
        return base
    if len(names) == 2:
        return f"{names[0]} & {names[1]}"
    return f"{names[0]}, {names[1]} +{len(names) - 2}"


def _pretty(name: str) -> str:
    """'flame-core' -> 'Flame core', 'Layer_Sun' -> 'Layer Sun' (keeps intentional casing)."""
    s = re.sub(r"[_\-]+", " ", name).strip()
    s = re.sub(r"\s+", " ", s)
    return s[:1].upper() + s[1:] if s else name


def dedupe_names(layers: List[Layer]) -> None:
    counts: Dict[str, int] = {}
    for L in layers:
        counts[L.name] = counts.get(L.name, 0) + 1
    seen: Dict[str, int] = {}
    for L in layers:
        if counts[L.name] > 1:
            seen[L.name] = seen.get(L.name, 0) + 1
            if seen[L.name] > 1:
                L.name = f"{L.name} {seen[L.name]}"


def next_layer_ids(existing: Iterable[str], n: int) -> List[str]:
    used = set(existing)
    nums = [int(m.group(1)) for i in used if (m := re.fullmatch(r"L(\d+)", i))]
    k = max(nums, default=0)
    out = []
    while len(out) < n:
        k += 1
        if f"L{k}" not in used:
            out.append(f"L{k}")
    return out


def is_image_only(members: Sequence[Elem]) -> bool:
    """Every member is a raster <image>: the layer is a flat card (round 8)."""
    return bool(members) and all(m.image for m in members)


def make_layer(lid: str, members: Sequence[Elem], template: Optional[Layer] = None,
               mode: Optional[str] = None) -> Layer:
    """A default layer (or a copy of `template`) holding `members`. `mode` ('individual' / 'combined', see
    :func:`bis.svg.tiling.auto_mode`) overrides the template's. ``depth.z`` is left for the stack
    (:func:`restack`). A layer of raster images only is a flat card (:func:`bis.stacking.card_depth`: no dome, at
    most 0.02 thick) - also when it is split off a vector layer."""
    ids = [m.id for m in members]
    name = layer_name(members)
    if template is None:
        lay = Layer(id=lid, name=name, elementIds=ids, mode=mode or "individual", depth=default_depth(),
                    material=MaterialSpec(preset=DEFAULT_MATERIAL), shadow=shadow_for(members))
    else:
        lay = template.model_copy(deep=True)
        lay.id = lid
        lay.name = name
        lay.elementIds = ids
        if mode is not None:
            lay.mode = mode
    if is_image_only(members):
        stacking.card_depth(lay.depth)
    clamp_bevel(lay)
    return lay


Radii = Mapping[str, Any]   # layer id -> bis.stacking.LayerShape (or a bare maxRadius)


def stack_gap(layers: Sequence[Layer], radii: Radii, art_scale: float = 1.0,
              art_offset=(0.0, 0.0)) -> Optional[float]:
    """The gap of the layers' rule stack (the overlap-aware import default, a look's zGap, a round-7 sequential or a
    legacy i x 0.13 stack); None when the user placed layers by hand (:func:`bis.stacking.stack_gap`). `radii`:
    layer id → :class:`bis.stacking.LayerShape` (or a bare maxRadius: footprint unknown)."""
    return stacking.stack_gap(layers, radii, art_scale=art_scale, art_offset=art_offset)


def restack(layers: List[Layer], radii: Radii, gap: Optional[float], art_scale: float = 1.0,
            art_offset=(0.0, 0.0)) -> None:
    """After a structural edit (in place): a stack that was a rule stack (`gap` = its gap, see :func:`stack_gap`)
    is re-stacked overlap-aware with the new layers' heights; a hand-placed stack (`gap` None) keeps every z except
    that a layer reaching into a lower layer it overlaps is lifted onto it (new layers slot in above their
    source)."""
    if gap is not None:
        stacking.restack(layers, radii, gap=gap, art_scale=art_scale, art_offset=art_offset)
        return
    stacking.lift_overlaps(layers, radii, art_scale=art_scale, art_offset=art_offset)


def members_for(store: ElementStore, ids: Sequence[str]) -> List[Elem]:
    idx = store.index
    return sorted((store.elems[idx[i]] for i in ids if i in idx), key=lambda e: idx[e.id])
