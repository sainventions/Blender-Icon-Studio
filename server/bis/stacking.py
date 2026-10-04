"""Real-height, overlap-aware layer stacking (PLAN §11 round 7 + round 8; ``shared/presets.json`` "geometry").

Every layer is a set of height-field bodies whose lowest point sits on ``depth.z`` (the worker lifts inflated layers
so the mirrored dome never dips below z). Its height is (round 8: identical in server, worker framing and web)

    H = max(thickness + 2 · inflate · maxRadius · S,  in-layer stacked height)

* ``maxRadius`` = ``LayerGeometry.maxRadius``: the largest inscribed-circle radius over the layer's bodies, in ART
  units (the dome of a body with inscribed radius D rises inflate · D above the flat half height);
* ``S`` = ``canvas.art.scale`` × ``layer.transform.scale`` (art units → canvas / Blender units: thickness is a
  canvas length, the worker scales a body's D by S as well);
* in-layer stacked height: the pieces (occlusion-cut regions, paint order) of an 'individual' layer that OVERLAP an
  earlier piece (a translucent piece over another) are stacked inside the layer by their real heights - a port of
  the worker's ``SceneBuilder._body_height`` / ``_relations`` (``heightfield.rings_relation`` == 2,
  ``heightfield.stack_shifts`` with ``scene.STACK_GAP``): H_in = max_j (shift_j + h_j) + max_j h_j, h_j = the
  piece's half height ``heightfield.half_height`` (thickness / 2 when not inflated).

Layers stack bottom → top, OVERLAP-AWARE (round 8): a layer only stacks above the lower layers it overlaps in XY -
footprints (the layer silhouettes after the art / layer transforms) closer than the clearance (the presets'
stackGap) - so layers that sit side by side share the base::

    z(i) = max(stackLift, max over overlapped lower j of z(j) + H(j) + gap)     gap = StyleSpec.zGap or stackGap

EVERY layer takes part (a hidden one keeps its slot so toggling it never moves the others). A layer whose footprint is
unknown (only a maxRadius number was given) overlaps every other layer, which gives the round-7 sequential stack
z(i+1) = z(i) + H(i) + gap - never lower than the overlap-aware one.

Used by the SVG pipeline's import defaults and structural edits (``bis.svg.layers`` / ``bis.svg.ops``, footprints from
the element store: :func:`bis.svg.geometry.layer_shape`), by looks / pasted / copied styles (``bis.style.apply_style``)
and so by the Icon Pack batch (footprints from the pipeline or from a geometry bundle: :func:`shape_from_geometry`).
The bevel (round-edge radius) is clamped to thickness / 2 only - height-field bodies taper thin parts. Layers of
soft-alpha rasters are flat cards (:data:`IMAGE_CARD`, :func:`is_card_layer`): no dome, thin, a small round edge;
crisp rasters are bodies (round 9).

No pydantic import: works on ``bis.models.Layer`` objects or anything with the same attributes. numpy / shapely are
imported where footprints are handled.
"""
from __future__ import annotations

import json
import math
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

#: fallbacks when presets.json has no "geometry" section (or cannot be read)
DEFAULT_RULES: dict[str, float] = {"stackLift": 0.0, "stackGap": 0.03}
#: z values are rounded to this many decimals (stable JSON, exact comparisons in tests)
Z_DECIMALS = 5
#: two z values closer than this are "the same" when a stack is recognised
Z_TOL = 2e-4
#: the pre-round-7 default stack (z_i = i × 0.13) - recognised so legacy projects re-stack on structural edits
LEGACY_STEP = 0.13

# --- the worker's in-layer stacking (blender_worker/scene.py, heightfield.py) - keep in sync --------------------
#: scene.STACK_GAP: overlapping pieces of one layer are stacked this far apart (world units)
PIECE_STACK_GAP = 0.002
#: scene.TOUCH_TOL: pieces closer than this (world units) touch
TOUCH_TOL = 0.0018
#: heightfield.WALL_MIN: the round edge keeps at least this fraction of the half thickness as a vertical wall
WALL_MIN = 0.15
#: heightfield.MAX_EDGE: outline sample spacing of the relation test (finer here: never misses an overlap)
RELATION_EDGE = 0.02
#: footprints are simplified by this much (art units) before the overlap test (≪ the clearance)
FOOTPRINT_SIMPLIFY = 0.002
#: Bezier flattening tolerance of bundle splines (art units)
FLATTEN_TOL = 5e-4
#: GEOS tolerance of a piece's max inscribed radius (art units; bis.svg.geometry.MAX_RADIUS_TOL)
PIECE_RADIUS_TOL = 5e-4

#: Layers of SOFT-alpha rasters only (glows, shines, shadows - round 9; round 8: every raster) are flat cards (PLAN
#: §11): no dome, thin, a small round edge - at import and under every look / style. Crisp rasters are bodies.
IMAGE_CARD: dict[str, float] = {"thickness": 0.02, "bevel": 0.006, "inflate": 0.0}

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
    """The rule height H = thickness + 2 · inflate · maxRadius · scale (canvas units) of a layer with `depth`."""
    t = max(0.0, _num(getattr(depth, "thickness", 0.0)))
    k = min(1.0, max(0.0, _num(getattr(depth, "inflate", 0.0))))
    return t + 2.0 * k * max(0.0, _num(max_radius)) * max(0.0, _num(scale, 1.0))


def layer_scale(layer: Any, art_scale: float = 1.0) -> float:
    """S of a layer: canvas.art.scale × layer.transform.scale."""
    tr = getattr(layer, "transform", None)
    return max(0.0, _num(art_scale, 1.0)) * max(0.0, _num(getattr(tr, "scale", 1.0), 1.0))


def rim_bevel(thickness: float, bevel: float) -> float:
    """Port of ``heightfield.rim_bevel``: the round-edge radius a body really gets, min(bevel, (1 − WALL_MIN) · t/2)
    (round 8: a minimum vertical wall of WALL_MIN × the half thickness)."""
    return max(0.0, min(max(0.0, bevel), (1.0 - WALL_MIN) * max(0.0, thickness) / 2.0))


def half_height(thickness: float, bevel: float, inflate: float, D: float) -> float:
    """Port of ``heightfield.half_height``: the top of an island with inradius D (world units) above its mid-plane,
    max(t/2 − b, 0) + min(b, D) + inflate · D with b = :func:`rim_bevel` (round 8 local bevel cap: at the island's
    apex the local half-width is D, so the round edge radius there is min(b, D) and its rim rises by exactly that;
    the minimum wall keeps max(t/2 − b, 0) ≥ WALL_MIN · t/2)."""
    t, k, d = max(0.0, thickness), max(0.0, inflate), max(0.0, D)
    b = rim_bevel(t, bevel)
    return max(t / 2.0 - b, 0.0) + min(b, d) + k * d


def stack_shifts(n: int, pairs: Iterable[tuple[int, int]], halves: Sequence[float], gap: float,
                 base: Optional[Sequence[float]] = None) -> list[float]:
    """Port of ``heightfield.stack_shifts`` (paint order): a piece j overlapping an earlier piece i (`pairs` (i, j),
    i < j) is lifted until its lowest point clears i's top by `gap`:
    shift_j = max(0, max_i (base_i + shift_i + h_i + gap + h_j) − base_j)."""
    b = [0.0] * n if base is None else [float(x) for x in base]
    s = [0.0] * n
    below: dict[int, list[int]] = {}
    for i, j in pairs:
        below.setdefault(j, []).append(i)
    for j in range(n):
        for i in below.get(j, ()):
            s[j] = max(s[j], b[i] + s[i] + halves[i] + gap + halves[j] - b[j])
    return s


# ---------------------------------------------------------------------------------------------- shapes
class LayerShape:
    """What the stack needs of one layer's geometry (ART units, before the art / layer transforms):

    * ``maxRadius`` - ``LayerGeometry.maxRadius`` (rule height);
    * ``footprint`` - the layer's silhouette as a shapely geometry; None = unknown (the layer then overlaps every
      other layer: the sequential stack); an empty geometry overlaps nothing;
    * ``pieces``   - the occlusion-cut region polygons in paint order (in-layer stacking of overlapping pieces).

    The in-layer relation (:meth:`pairs`, at the piece tolerance of scale `S`) and the pieces' inscribed radii are
    computed on first use and kept."""

    __slots__ = ("maxRadius", "footprint", "pieces", "S", "_pairs", "_radii")

    def __init__(self, max_radius: float = 0.0, footprint: Any = None, pieces: Optional[Sequence[Any]] = None,
                 S: float = 1.0) -> None:
        self.maxRadius = max(0.0, _num(max_radius))
        self.footprint = _simplified(footprint)
        self.pieces = [p for p in (pieces or []) if p is not None and not p.is_empty]
        self.S = max(1e-6, _num(S, 1.0))
        self._pairs: Optional[list[tuple[int, int]]] = None
        self._radii: Optional[list[float]] = None

    def __repr__(self) -> str:   # pragma: no cover - debugging aid
        fp = "?" if self.footprint is None else f"{self.footprint.geom_type}"
        return f"LayerShape(maxRadius={self.maxRadius}, footprint={fp}, pieces={len(self.pieces)})"

    def pairs(self) -> list[tuple[int, int]]:
        """(i, j), i < j: piece j OVERLAPS the earlier piece i (``heightfield.rings_relation`` == 2)."""
        if self._pairs is None:
            self._pairs = overlapping_pieces(self.pieces, TOUCH_TOL / self.S) if len(self.pieces) > 1 else []
        return self._pairs

    def piece_radii(self) -> list[float]:
        """Max inscribed radius of every piece (art units)."""
        if self._radii is None:
            self._radii = [_inradius(p) for p in self.pieces]
        return self._radii


def as_shape(value: Any) -> LayerShape:
    """A :class:`LayerShape` from a LayerShape, a maxRadius number (footprint unknown) or a LayerGeometry
    (model / dict with "silhouette")."""
    if isinstance(value, LayerShape):
        return value
    if value is None:
        return LayerShape(0.0)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return LayerShape(value)
    sil = value.get("silhouette") if isinstance(value, Mapping) else getattr(value, "silhouette", None)
    if sil is not None:
        return shape_from_geometry(value)
    return LayerShape(_num(value))


def _shape(shapes: Mapping[str, Any] | Sequence[Any] | None, layer: Any, i: int) -> LayerShape:
    if shapes is None:
        return LayerShape(0.0)
    if isinstance(shapes, Mapping):
        return as_shape(shapes.get(getattr(layer, "id", None)))
    return as_shape(shapes[i]) if i < len(shapes) else LayerShape(0.0)


def in_layer_height(layer: Any, shape: LayerShape, scale: float = 1.0) -> float:
    """Height of a layer's pieces stacked inside it (0 when no piece overlaps an earlier one, or the layer is one
    'combined' body) - the worker's ``_body_height``: H_in = max(shift + h) + max(h)."""
    if getattr(layer, "mode", "individual") == "combined" or len(shape.pieces) < 2:
        return 0.0
    pairs = shape.pairs()
    if not pairs:
        return 0.0
    dp = getattr(layer, "depth", None)
    th = max(0.0, _num(getattr(dp, "thickness", 0.1)))
    k = max(0.0, min(1.0, _num(getattr(dp, "inflate", 0.0))))
    b = min(max(0.0, _num(getattr(dp, "bevel", 0.045))), th / 2.0)
    S = max(0.0, _num(scale, 1.0))
    hs = [th / 2.0 if k <= 0.0 else half_height(th, b, k, D * S) for D in shape.piece_radii()]
    sh = stack_shifts(len(hs), pairs, hs, PIECE_STACK_GAP)
    return max(s + h for s, h in zip(sh, hs)) + max(hs)


def layer_height(layer: Any, shape: Any, scale: float = 1.0) -> float:
    """H of a layer (canvas units): max(rule height, in-layer stacked height); `scale` = S (:func:`layer_scale`)."""
    sh = as_shape(shape)
    rule = body_height(getattr(layer, "depth", None), sh.maxRadius, scale)
    return max(rule, in_layer_height(layer, sh, scale))


def heights(layers: Sequence[Any], radii: Mapping[str, Any] | Sequence[Any] | None,
            art_scale: float = 1.0) -> list[float]:
    """Body height H of every layer (`radii`: layer id → LayerShape / maxRadius number / LayerGeometry, or a list
    by index; missing = 0)."""
    return [layer_height(L, _shape(radii, L, i), layer_scale(L, art_scale)) for i, L in enumerate(layers)]


# ---------------------------------------------------------------------------------------------- footprints
def _simplified(geom: Any) -> Any:
    if geom is None:
        return None
    import shapely

    if geom.is_empty:
        return geom
    try:
        g = shapely.simplify(geom, FOOTPRINT_SIMPLIFY, preserve_topology=True)
        return g if not g.is_empty else geom
    except Exception:  # noqa: BLE001 - GEOS trouble: keep the exact outline
        return geom


def _offset(art_offset: Any) -> tuple[float, float]:
    if art_offset is None:
        return 0.0, 0.0
    if hasattr(art_offset, "x") and hasattr(art_offset, "y"):
        return _num(art_offset.x), _num(art_offset.y)
    try:
        return _num(art_offset[0]), _num(art_offset[1])
    except (TypeError, IndexError, KeyError):
        return 0.0, 0.0


def footprints(layers: Sequence[Any], shapes: Mapping[str, Any] | Sequence[Any] | None, art_scale: float = 1.0,
               art_offset: Any = (0.0, 0.0)) -> list[Any]:
    """Every layer's footprint in CANVAS units (the worker's transform: (p · art.scale + art.xy) · layer.scale +
    layer.xy); None = unknown."""
    from shapely import affinity

    ax, ay = _offset(art_offset)
    out = []
    for i, L in enumerate(layers):
        fp = _shape(shapes, L, i).footprint
        if fp is None or fp.is_empty:
            out.append(fp)
            continue
        tr = getattr(L, "transform", None)
        sl = max(0.0, _num(getattr(tr, "scale", 1.0), 1.0))
        a = max(0.0, _num(art_scale, 1.0)) * sl
        out.append(affinity.affine_transform(fp, [a, 0.0, 0.0, a, ax * sl + _num(getattr(tr, "x", 0.0)),
                                                  ay * sl + _num(getattr(tr, "y", 0.0))]))
    return out


def overlap_lists(layers: Sequence[Any], shapes: Mapping[str, Any] | Sequence[Any] | None, *,
                  art_scale: float = 1.0, art_offset: Any = (0.0, 0.0), clearance: Optional[float] = None,
                  presets: Any = None) -> list[list[int]]:
    """For every layer i the lower layers j < i it overlaps in XY: footprints closer than `clearance` (None = the
    presets' stackGap). An unknown footprint overlaps every layer; an empty one none."""
    import shapely

    c = geometry_rules(presets)["stackGap"] if clearance is None else max(0.0, _num(clearance))
    fps = footprints(layers, shapes, art_scale, art_offset)
    out: list[list[int]] = []
    for i, fi in enumerate(fps):
        low = []
        for j in range(i):
            fj = fps[j]
            if fi is None or fj is None:
                low.append(j)
            elif fi.is_empty or fj.is_empty:
                continue
            elif shapely.dwithin(fi, fj, c) if c > 0 else shapely.intersects(fi, fj):
                low.append(j)
        out.append(low)
    return out


def overlap_z(hs: Sequence[float], lower: Sequence[Sequence[int]], gap: float, lift: float = 0.0) -> list[float]:
    """z of every layer: z(i) = max(lift, max over j in lower[i] of z(j) + H(j) + gap) (unrounded running values,
    the output rounded to Z_DECIMALS)."""
    g = max(0.0, _num(gap))
    l0 = max(0.0, _num(lift))
    z: list[float] = []
    for i, h in enumerate(hs):
        z.append(max([l0] + [z[j] + hs[j] + g for j in lower[i] if j < i]))
    return [round(v, Z_DECIMALS) for v in z]


def stack_z(hs: Sequence[float], gap: float, lift: float = 0.0) -> list[float]:
    """The sequential stack (every layer overlaps the one below): z0 = lift, z(i+1) = z(i) + H(i) + gap."""
    return overlap_z(hs, [[i - 1] if i else [] for i in range(len(hs))], gap, lift)


def restack(layers: Sequence[Any], radii: Mapping[str, Any] | Sequence[Any] | None, *,
            gap: Optional[float] = None, lift: Optional[float] = None, art_scale: float = 1.0,
            presets: Any = None, art_offset: Any = (0.0, 0.0), clearance: Optional[float] = None) -> list[float]:
    """Set ``depth.z`` of every layer (in place) to the overlap-aware real-height stack; returns the z values.
    `radii`: layer id → LayerShape (footprint + maxRadius) / maxRadius number (footprint unknown: stacks on every
    lower layer) / LayerGeometry. `gap` / `lift` / `clearance` None → presets.json "geometry" (stackGap /
    stackLift / stackGap)."""
    rules = geometry_rules(presets)
    g = rules["stackGap"] if gap is None else gap
    l0 = rules["stackLift"] if lift is None else lift
    lower = overlap_lists(layers, radii, art_scale=art_scale, art_offset=art_offset,
                          clearance=rules["stackGap"] if clearance is None else clearance)
    zs = overlap_z(heights(layers, radii, art_scale), lower, g, l0)
    for L, z in zip(layers, zs):
        L.depth.z = z
    return zs


def stack_gaps(layers: Sequence[Any], radii: Mapping[str, Any] | Sequence[Any] | None,
               art_scale: float = 1.0) -> list[float]:
    """Clearance between NEIGHBOURS: z(i+1) − (z(i) + H(i)) (the sequential stack's gaps; a negative value between
    layers that do not overlap in XY is fine - see :func:`stack_clearances` / :func:`interpenetrations`)."""
    hs = heights(layers, radii, art_scale)
    return [_num(b.depth.z) - (_num(a.depth.z) + h) for a, b, h in zip(layers, layers[1:], hs)]


def stack_clearances(layers: Sequence[Any], radii: Mapping[str, Any] | Sequence[Any] | None, *,
                     art_scale: float = 1.0, art_offset: Any = (0.0, 0.0), clearance: Optional[float] = None,
                     presets: Any = None) -> list[Optional[float]]:
    """For every layer: z(i) − max over its overlapped lower layers j of (z(j) + H(j)) - the gap it was stacked
    with; None for a layer that overlaps no lower layer (it sits on the base)."""
    hs = heights(layers, radii, art_scale)
    lower = overlap_lists(layers, radii, art_scale=art_scale, art_offset=art_offset, clearance=clearance,
                          presets=presets)
    zs = [_num(L.depth.z) for L in layers]
    return [None if not lower[i] else zs[i] - max(zs[j] + hs[j] for j in lower[i]) for i in range(len(layers))]


def interpenetrations(layers: Sequence[Any], radii: Mapping[str, Any] | Sequence[Any] | None, *,
                      art_scale: float = 1.0, art_offset: Any = (0.0, 0.0),
                      tol: float = Z_TOL) -> list[tuple[int, int, float]]:
    """Pairs of layers (j, i), j < i, whose bodies may cut into each other: footprints that touch or intersect in XY
    and z ranges [z, z + H] that overlap by more than `tol` → [(j, i, overlap depth)]. Hidden layers count too."""
    hs = heights(layers, radii, art_scale)
    lower = overlap_lists(layers, radii, art_scale=art_scale, art_offset=art_offset, clearance=1e-9)
    zs = [_num(L.depth.z) for L in layers]
    out = []
    for i, low in enumerate(lower):
        for j in low:
            d = min(zs[i] + hs[i], zs[j] + hs[j]) - max(zs[i], zs[j])
            if d > tol:
                out.append((j, i, d))
    return out


def stack_gap(layers: Sequence[Any], radii: Mapping[str, Any] | Sequence[Any] | None, *,
              art_scale: float = 1.0, presets: Any = None, art_offset: Any = (0.0, 0.0),
              clearance: Optional[float] = None) -> Optional[float]:
    """The gap of a rule stack, None for a custom stack (a user-placed z somewhere). Rule stacks: the
    overlap-aware real-height stack (round 8: base layers at stackLift, every other layer one gap above its
    overlapped lower layers), the round-7 sequential real-height stack (one gap between every pair of neighbours) -
    both return their gap - and the pre-round-7 default stack (z_i = i × 0.13) and a stack with no overlapping
    layers at all (every layer at stackLift) - both return the presets' stackGap. Structural edits re-stack a rule
    stack (so round-7 / legacy stacks convert to the overlap-aware one)."""
    if not layers:
        return None
    rules = geometry_rules(presets)
    zs = [_num(L.depth.z) for L in layers]
    if len(zs) > 1 and all(abs(z - i * LEGACY_STEP) <= Z_TOL for i, z in enumerate(zs)):
        return rules["stackGap"]
    if abs(zs[0] - rules["stackLift"]) > Z_TOL:
        return None
    cl = stack_clearances(layers, radii, art_scale=art_scale, art_offset=art_offset, clearance=clearance,
                          presets=presets)
    gaps = [g for g in cl if g is not None]
    base_ok = all(abs(zs[i] - rules["stackLift"]) <= Z_TOL for i, g in enumerate(cl) if g is None)
    if base_ok:
        if not gaps:
            return rules["stackGap"]
        if max(gaps) - min(gaps) <= 2 * Z_TOL and min(gaps) >= -Z_TOL:
            return round(max(0.0, sum(gaps) / len(gaps)), Z_DECIMALS)
    seq = stack_gaps(layers, radii, art_scale)     # the round-7 sequential stack
    if not seq:
        return rules["stackGap"]
    if max(seq) - min(seq) > 2 * Z_TOL or min(seq) < -Z_TOL:
        return None
    return round(max(0.0, sum(seq) / len(seq)), Z_DECIMALS)


def lift_overlaps(layers: Sequence[Any], radii: Mapping[str, Any] | Sequence[Any] | None, *,
                  gap: Optional[float] = None, art_scale: float = 1.0, presets: Any = None,
                  art_offset: Any = (0.0, 0.0), clearance: Optional[float] = None) -> None:
    """A custom stack after a structural edit: keep every z, except that a layer starting below the top of a lower
    layer it overlaps in XY (a new layer slotted in, a layer that grew) is lifted to sit `gap` above the highest
    such top. In place (bottom → top, so lifts propagate)."""
    g = geometry_rules(presets)["stackGap"] if gap is None else gap
    hs = heights(layers, radii, art_scale)
    lower = overlap_lists(layers, radii, art_scale=art_scale, art_offset=art_offset, clearance=clearance,
                          presets=presets)
    for i in range(1, len(layers)):
        if not lower[i]:
            continue
        top = max(_num(layers[j].depth.z) + hs[j] for j in lower[i])
        if _num(layers[i].depth.z) < top - Z_TOL:
            layers[i].depth.z = round(top + g, Z_DECIMALS)


def bbox_radius(boxes: Sequence[Sequence[float]]) -> float:
    """Upper bound of the inscribed radius of art inside the union of `boxes` (art bboxes): half the smaller
    side of their union - the fallback when no geometry bundle is at hand (never under-estimates H)."""
    bs = [b for b in boxes if b and len(b) == 4]
    if not bs:
        return 0.0
    w = max(b[2] for b in bs) - min(b[0] for b in bs)
    h = max(b[3] for b in bs) - min(b[1] for b in bs)
    return max(0.0, min(w, h) / 2.0)


# ---------------------------------------------------------------------------------------------- image cards
def is_image_layer(layer: Any, kinds: Mapping[str, str]) -> bool:
    """True when every element of the layer is a raster image (`kinds`: element id → Element.kind) - crisp or soft
    (round 9 cards: :func:`is_card_layer`)."""
    ids = list(getattr(layer, "elementIds", None) or [])
    return bool(ids) and all(kinds.get(i) == "image" for i in ids)


def card_elements(elements: Iterable[Any]) -> set[str]:
    """Ids of the elements that make a flat card (PLAN §11 round 9): rasters with SOFT alpha (``Element.softAlpha``
    True - glows, shines, shadows). A raster not measured yet (softAlpha None: a project imported before round 9)
    counts as a card, as it was imported; a crisp raster (False) is a body like vector art."""
    out = set()
    for e in elements or ():
        get = (lambda k, d=None: e.get(k, d)) if isinstance(e, Mapping) else (lambda k, d=None: getattr(e, k, d))
        if get("kind") == "image" and get("softAlpha") is not False:
            out.add(str(get("id")))
    return out


def is_card_layer(layer: Any, cards: Iterable[str]) -> bool:
    """True when every element of the layer is a card element (:func:`card_elements`): the layer is a flat card."""
    ids = list(getattr(layer, "elementIds", None) or [])
    cs = cards if isinstance(cards, (set, frozenset)) else set(cards)
    return bool(ids) and all(i in cs for i in ids)


def card_depth(depth: Any) -> Any:
    """`depth` (LayerDepth, in place, returned) as a flat image card: no dome, at most IMAGE_CARD's thickness and
    round edge."""
    depth.inflate = IMAGE_CARD["inflate"]
    depth.thickness = min(max(0.0, _num(depth.thickness, IMAGE_CARD["thickness"])), IMAGE_CARD["thickness"])
    depth.bevel = clamp_bevel(min(max(0.0, _num(depth.bevel)), IMAGE_CARD["bevel"]), depth.thickness)
    return depth


# ---------------------------------------------------------------------------------------------- geometry
def _polygonal(g: Any) -> Any:
    """The polygonal part of a geometry (make_valid may leave lines / points)."""
    import shapely
    from shapely.geometry import Polygon

    if g is None or g.is_empty:
        return Polygon()
    if g.geom_type in ("Polygon", "MultiPolygon"):
        return g
    parts = [p for p in shapely.get_parts(g) if p.geom_type in ("Polygon", "MultiPolygon") and not p.is_empty]
    if not parts:
        return Polygon()
    return shapely.union_all(parts) if len(parts) > 1 else parts[0]


def rings_polygon(rings: Sequence[Any]) -> Any:
    """Even-odd composition of closed rings ((n, 2) arrays) → a (Multi)Polygon."""
    import shapely
    from shapely.geometry import Polygon

    polys = []
    for r in rings:
        if r is None or len(r) < 3:
            continue
        p = _polygonal(shapely.make_valid(Polygon(r)))
        if not p.is_empty and p.area > 0:
            polys.append(p)
    if not polys:
        return Polygon()
    if len(polys) == 1:
        return polys[0]
    polys.sort(key=lambda p: -p.area)
    try:   # disjoint contours: even-odd == fold with XOR (bis.svg.paths.shapely_from_path)
        geom = polys[0]
        for p in polys[1:]:
            geom = shapely.symmetric_difference(geom, p)
        return _polygonal(shapely.make_valid(geom))
    except Exception:  # noqa: BLE001 - numerically nasty: the union of the rings (never smaller)
        return _polygonal(shapely.union_all(polys))


def _pts(s: Any) -> list:
    pts = s.get("points") if isinstance(s, Mapping) else getattr(s, "points", None)
    return list(pts or [])


def _xy(p: Any, key: str) -> tuple[float, float]:
    v = p.get(key) if isinstance(p, Mapping) else getattr(p, key, None)
    return (_num(v[0]), _num(v[1])) if v is not None else (0.0, 0.0)


def spline_ring(s: Any, tol: float = FLATTEN_TOL):
    """A closed cubic spline (``bis.models.Spline`` or its dict) flattened to an (n, 2) array (art units)."""
    import numpy as np

    pts = _pts(s)
    if len(pts) < 2:
        return None
    co = np.array([_xy(p, "co") for p in pts], dtype=np.float64)
    hl = np.array([_xy(p, "hl") for p in pts], dtype=np.float64)
    hr = np.array([_xy(p, "hr") for p in pts], dtype=np.float64)
    P0, P1, P2, P3 = co, hr, np.roll(hl, -1, axis=0), np.roll(co, -1, axis=0)
    M = np.maximum(np.linalg.norm(P0 - 2 * P1 + P2, axis=1), np.linalg.norm(P1 - 2 * P2 + P3, axis=1))
    m = np.clip(np.ceil(np.sqrt(0.75 * M / max(tol, 1e-9))), 1, 64).astype(np.int64)
    seg = np.repeat(np.arange(len(m)), m)
    t = ((np.arange(int(m.sum())) - np.repeat(np.cumsum(m) - m, m)) / m[seg])[:, None]
    u = 1.0 - t
    ring = (u ** 3) * P0[seg] + 3 * (u ** 2) * t * P1[seg] + 3 * u * (t ** 2) * P2[seg] + (t ** 3) * P3[seg]
    return ring


def splines_polygon(splines: Sequence[Any], tol: float = FLATTEN_TOL) -> Any:
    """Bundle splines (art space) → a (Multi)Polygon (even-odd)."""
    return rings_polygon([spline_ring(s, tol) for s in splines or []])


def _inradius(geom: Any) -> float:
    import shapely

    try:
        parts = [p for p in shapely.get_parts(geom) if not p.is_empty and p.area > 0]
        if not parts:
            return 0.0
        return float(max(shapely.length(shapely.maximum_inscribed_circle(p, tolerance=PIECE_RADIUS_TOL))
                         for p in parts))
    except Exception:  # noqa: BLE001 - √(area/π) bound
        return math.sqrt(max(0.0, geom.area) / math.pi)


def pieces_relation(a: Any, b: Any, tol: float) -> int:
    """Port of ``heightfield.rings_relation`` on polygons: 0 apart, 1 TOUCH (outlines within `tol`, interiors do not
    overlap), 2 OVERLAP (a boundary point of one lies inside the other farther than `tol` from its outline - or the
    outlines (nearly) coincide: probes 4·tol inside one piece lie inside the other, farther than `tol` from it)."""
    import numpy as np
    import shapely

    if a is None or b is None or a.is_empty or b.is_empty:
        return 0
    ax0, ay0, ax1, ay1 = a.bounds
    bx0, by0, bx1, by1 = b.bounds
    if ax0 > bx1 + tol or bx0 > ax1 + tol or ay0 > by1 + tol or by0 > ay1 + tol:
        return 0
    if not shapely.dwithin(a, b, tol):
        return 0

    def deep_inside(src, dst) -> bool:
        if src is None or src.is_empty:
            return False
        P = shapely.get_coordinates(shapely.segmentize(src.boundary, RELATION_EDGE))
        if not len(P):
            return False
        ins = shapely.contains_xy(dst, P[:, 0], P[:, 1])
        if not ins.any():
            return False
        d = shapely.distance(dst.boundary, shapely.points(P[ins]))
        return bool((np.asarray(d) > tol).any())

    if deep_inside(a, b) or deep_inside(b, a):
        return 2
    for p, q in ((a, b), (b, a)):
        probe = shapely.buffer(p, -4.0 * tol, join_style="mitre")
        if deep_inside(probe, q):
            return 2
    return 1


def overlapping_pieces(pieces: Sequence[Any], tol: float) -> list[tuple[int, int]]:
    """(i, j), i < j, for every piece j that OVERLAPS an earlier piece i (:func:`pieces_relation` == 2)."""
    out = []
    bbs = [p.bounds for p in pieces]
    for j in range(len(pieces)):
        bj = bbs[j]
        for i in range(j):
            bi = bbs[i]
            if bi[0] > bj[2] + tol or bj[0] > bi[2] + tol or bi[1] > bj[3] + tol or bj[1] > bi[3] + tol:
                continue
            if pieces_relation(pieces[i], pieces[j], tol) == 2:
                out.append((i, j))
    return out


_GEO_CACHE: "OrderedDict[tuple, LayerShape]" = OrderedDict()
_GEO_LOCK = threading.Lock()


def shape_from_geometry(lg: Any, S: float = 1.0) -> LayerShape:
    """The :class:`LayerShape` of a ``LayerGeometry`` (model or its dict): maxRadius, the silhouette (+ the quads of
    raster cards that have no region) as the footprint, the regions as pieces. Cached by the layer hash."""
    get = (lambda k, d=None: lg.get(k, d)) if isinstance(lg, Mapping) else (lambda k, d=None: getattr(lg, k, d))
    h = get("hash")
    key = (h, round(_num(S, 1.0), 6)) if h else None
    if key is not None:
        with _GEO_LOCK:
            hit = _GEO_CACHE.get(key)
            if hit is not None:
                _GEO_CACHE.move_to_end(key)
                return hit
    import shapely
    from shapely.geometry import box

    regions = list(get("regions") or [])
    rsplines = [(r.get("splines") if isinstance(r, Mapping) else getattr(r, "splines", None)) or [] for r in regions]
    pieces = [splines_polygon(s) for s in rsplines]
    fp = splines_polygon(get("silhouette") or [])
    rid = {(r.get("elementId") if isinstance(r, Mapping) else getattr(r, "elementId", None)) for r in regions}
    cards = []
    for im in get("images") or []:
        im = im if isinstance(im, Mapping) else dict(im)
        bb = im.get("bbox")
        if im.get("elementId") not in rid and bb and len(bb) == 4:
            cards.append(box(*[_num(v) for v in bb]))
    parts = [g for g in [fp, *pieces, *cards] if g is not None and not g.is_empty]
    if parts:
        fp = _polygonal(shapely.union_all(parts))
    shape = LayerShape(_num(get("maxRadius", 0.0)), fp, pieces, S)
    if key is not None:
        with _GEO_LOCK:
            _GEO_CACHE[key] = shape
            while len(_GEO_CACHE) > 512:
                _GEO_CACHE.popitem(last=False)
    return shape


def shapes_from_bundle(bundle: Any, layers: Sequence[Any] = (), art_scale: float = 1.0) -> dict[str, LayerShape]:
    """{layer id: LayerShape} of a GeometryBundle (model or dict); `layers` give each layer's S (piece tolerance)."""
    lgs = bundle.get("layers") if isinstance(bundle, Mapping) else getattr(bundle, "layers", None)
    scale = {getattr(L, "id", None): layer_scale(L, art_scale) for L in layers}
    return {lid: shape_from_geometry(lg, scale.get(lid, max(1e-6, _num(art_scale, 1.0))))
            for lid, lg in (lgs or {}).items()}
