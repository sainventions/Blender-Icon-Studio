"""Default layers: naming, depth stack, bevel clamp and shadow defaults (PLAN §2 'Default stack
on import'), plus helpers to re-stack layers after structural edits."""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence

from bis.models import Layer, LayerDepth, LayerShadow, MaterialSpec
from .colors import color_name
from .elements import ElementStore, Elem
from .prepass import auto_name

Z_STEP = 0.13
DEFAULT_THICKNESS = 0.10
DEFAULT_BEVEL = 0.045
BEVEL_SAFE_FACTOR = 0.9
DEFAULT_MATERIAL = "liquid_glass"
DEFAULT_SHADOW_OPACITY = 0.5


def default_bevel(safe_r: float) -> float:
    return round(max(0.0, min(DEFAULT_BEVEL, BEVEL_SAFE_FACTOR * safe_r)), 5)


def shadow_for(members: Sequence[Elem]) -> LayerShadow:
    ops = [m.shadow["opacity"] for m in members if m.shadow]
    if ops:
        return LayerShadow(kind="neutral", opacity=round(min(1.0, max(ops) / 0.6), 4))
    return LayerShadow(kind="neutral", opacity=DEFAULT_SHADOW_OPACITY)


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


def make_layer(lid: str, members: Sequence[Elem], index: int, safe_r: float,
               template: Optional[Layer] = None, mode: Optional[str] = None) -> Layer:
    """A default layer (or a copy of `template`) holding `members`. `mode` ('individual' /
    'combined', see :func:`bis.svg.tiling.auto_mode`) overrides the template's; `safe_r` must be
    the safe radius for the layer's resulting mode."""
    ids = [m.id for m in members]
    name = layer_name(members)
    if template is None:
        return Layer(id=lid, name=name, elementIds=ids, mode=mode or "individual",
                     depth=LayerDepth(z=round(index * Z_STEP, 6), thickness=DEFAULT_THICKNESS,
                                      bevel=default_bevel(safe_r)),
                     material=MaterialSpec(preset=DEFAULT_MATERIAL), shadow=shadow_for(members))
    lay = template.model_copy(deep=True)
    lay.id = lid
    lay.name = name
    lay.elementIds = ids
    if mode is not None:
        lay.mode = mode
    lay.depth.bevel = round(min(lay.depth.bevel, BEVEL_SAFE_FACTOR * safe_r) if safe_r > 0 else lay.depth.bevel, 5)
    return lay


def has_default_stack(layers: Sequence[Layer]) -> bool:
    return all(abs(L.depth.z - i * Z_STEP) < 1e-6 for i, L in enumerate(layers))


def restack(layers: List[Layer], was_default: bool) -> None:
    """After a structural edit: re-apply the default z spacing if the stack used it, otherwise
    make sure z never decreases bottom -> top (new layers slot between their neighbours)."""
    if was_default:
        for i, L in enumerate(layers):
            L.depth.z = round(i * Z_STEP, 6)
        return
    for i in range(1, len(layers)):
        if layers[i].depth.z < layers[i - 1].depth.z:
            layers[i].depth.z = round(layers[i - 1].depth.z + Z_STEP / 4, 6)


def members_for(store: ElementStore, ids: Sequence[str]) -> List[Elem]:
    idx = store.index
    return sorted((store.elems[idx[i]] for i in ids if i in idx), key=lambda e: idx[e.id])
