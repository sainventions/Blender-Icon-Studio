"""bis.stacking - real-height layer stacking (PLAN §11 round 7, shared/presets.json "geometry"). Pure unit tests."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from bis import stacking  # noqa: E402
from bis.models import Layer, LayerDepth, LayerTransform  # noqa: E402

PRESETS = json.loads((ROOT / "shared" / "presets.json").read_text(encoding="utf-8"))


def _layers(*depths, scales=None):
    return [Layer(id=f"l{i}", name=f"L{i}", elementIds=[f"e{i}"], depth=LayerDepth(**d),
                  transform=LayerTransform(scale=(scales or {}).get(i, 1.0)))
            for i, d in enumerate(depths)]


def test_presets_geometry_section_is_read():
    geo = PRESETS["geometry"]
    assert stacking.geometry_rules(PRESETS) == {"stackLift": geo["stackLift"], "stackGap": geo["stackGap"]}
    assert stacking.geometry_rules() == stacking.geometry_rules(PRESETS)          # the repo file by default
    assert stacking.geometry_rules({"geometry": {"stackGap": -1, "stackLift": "x"}}) == {"stackLift": 0.0,
                                                                                         "stackGap": 0.0}
    assert stacking.geometry_rules(None) is not stacking.geometry_rules(None)      # callers get copies


def test_body_height_and_stack():
    d = LayerDepth(thickness=0.16, inflate=0.25)
    assert stacking.body_height(d, 0.4) == pytest.approx(0.16 + 2 * 0.25 * 0.4)
    assert stacking.body_height(d, 0.4, scale=1.5) == pytest.approx(0.16 + 2 * 0.25 * 0.4 * 1.5)
    assert stacking.body_height(LayerDepth(thickness=0.1, inflate=3.0), 0.2) == pytest.approx(0.1 + 2 * 0.2)  # 0..1
    assert stacking.stack_z([0.2, 0.3, 0.1], gap=0.03, lift=0.01) == pytest.approx([0.01, 0.24, 0.57])
    layers = _layers({"thickness": 0.1}, {"thickness": 0.2, "inflate": 0.5}, {"thickness": 0.1},
                     scales={1: 2.0})
    zs = stacking.restack(layers, {"l1": 0.1}, gap=0.05, lift=0.0, art_scale=1.0)
    assert zs == pytest.approx([0.0, 0.15, 0.15 + 0.2 + 2 * 0.5 * 0.1 * 2.0 + 0.05])
    assert [L.depth.z for L in layers] == zs
    assert stacking.stack_gaps(layers, {"l1": 0.1}) == pytest.approx([0.05, 0.05])


def test_stack_gap_recognises_real_legacy_and_custom_stacks():
    rules = stacking.geometry_rules()
    layers = _layers({"thickness": 0.16, "inflate": 0.25}, {"thickness": 0.16, "inflate": 0.25}, {"thickness": 0.1})
    radii = {"l0": 0.3, "l1": 0.1}
    stacking.restack(layers, radii, gap=0.07)
    assert stacking.stack_gap(layers, radii) == pytest.approx(0.07)
    stacking.restack(layers, radii)
    assert stacking.stack_gap(layers, radii) == pytest.approx(rules["stackGap"])
    for i, L in enumerate(layers):                      # the pre-round-7 default: z = i x 0.13
        L.depth.z = round(i * 0.13, 6)
    assert stacking.stack_gap(layers, radii) == pytest.approx(rules["stackGap"])
    layers[2].depth.z = 0.9                             # hand-placed
    assert stacking.stack_gap(layers, radii) is None
    assert stacking.stack_gap([], radii) is None


def test_lift_overlaps_only_moves_layers_that_would_interpenetrate():
    layers = _layers({"z": 0.0, "thickness": 0.2}, {"z": 0.1, "thickness": 0.1}, {"z": 2.0, "thickness": 0.1})
    stacking.lift_overlaps(layers, {}, gap=0.03)
    assert [L.depth.z for L in layers] == pytest.approx([0.0, 0.23, 2.0])


def test_clamp_bevel_and_bbox_radius():
    assert stacking.clamp_bevel(0.08, 0.16) == 0.08
    assert stacking.clamp_bevel(0.2, 0.16) == 0.08
    assert stacking.clamp_bevel(0.1, 0.00003) == 0.00001
    assert stacking.bbox_radius([(-0.5, -0.2, 0.1, 0.2), (0.2, -0.1, 0.5, 0.1)]) == pytest.approx(0.2)
    assert stacking.bbox_radius([]) == 0.0


# ---------------------------------------------------------------------------------------------- round 8: overlap-aware
def _box(x0, y0, x1, y1):
    from shapely.geometry import box

    return box(x0, y0, x1, y1)


def _shapes(**boxes):
    return {k: stacking.LayerShape(r, _box(*b) if b is not None else None) for k, (r, b) in boxes.items()}


def test_overlap_aware_restack_shares_the_base():
    """z(i) = max(stackLift, max over overlapped lower j of z(j) + H(j) + gap): layers side by side share the base;
    footprints closer than the clearance (stackGap) count as overlapping; an unknown footprint overlaps all."""
    layers = _layers({"thickness": 0.1}, {"thickness": 0.2}, {"thickness": 0.1}, {"thickness": 0.1})
    shapes = _shapes(l0=(0.0, (-0.9, -0.2, -0.5, 0.2)),     # left
                     l1=(0.0, (0.5, -0.2, 0.9, 0.2)),       # right
                     l2=(0.0, (-0.8, -0.1, -0.6, 0.1)),     # on the left
                     l3=(0.0, (-0.6, -0.1, 0.6, 0.1)))      # a bar reaching both
    zs = stacking.restack(layers, shapes, gap=0.03, lift=0.0, clearance=0.03)
    assert zs == pytest.approx([0.0, 0.0, 0.13, 0.26])     # l3 clears l2 (0.13 + 0.1 + 0.03) and l1 (0.2 + 0.03)
    assert stacking.overlap_lists(layers, shapes, clearance=0.03) == [[], [], [0], [0, 1, 2]]
    assert stacking.stack_gap(layers, shapes, presets={"geometry": {"stackGap": 0.03}}) == pytest.approx(0.03)
    assert stacking.stack_clearances(layers, shapes, clearance=0.03) == pytest.approx([None, None, 0.03, 0.03])
    assert stacking.interpenetrations(layers, shapes) == []
    # the clearance: 0.02 apart overlaps (stacked), 0.05 apart does not
    near = _shapes(l0=(0.0, (0.0, 0.0, 0.2, 0.2)), l1=(0.0, (0.22, 0.0, 0.4, 0.2)))
    far = _shapes(l0=(0.0, (0.0, 0.0, 0.2, 0.2)), l1=(0.0, (0.25, 0.0, 0.4, 0.2)))
    two = _layers({"thickness": 0.1}, {"thickness": 0.1})
    assert stacking.restack(two, near, gap=0.03, lift=0.0, clearance=0.03) == pytest.approx([0.0, 0.13])
    assert stacking.restack(two, far, gap=0.03, lift=0.0, clearance=0.03) == pytest.approx([0.0, 0.0])
    # unknown footprint (a bare maxRadius) overlaps everything: the sequential stack; an empty one overlaps nothing
    assert stacking.restack(two, {"l0": 0.0, "l1": 0.0}, gap=0.03, lift=0.0) == pytest.approx([0.0, 0.13])
    from shapely.geometry import Polygon
    empty = {"l0": stacking.LayerShape(0.0, Polygon()), "l1": stacking.LayerShape(0.0, _box(0, 0, 1, 1))}
    assert stacking.restack(two, empty, gap=0.03, lift=0.0) == pytest.approx([0.0, 0.0])


def test_footprints_follow_the_art_and_layer_transforms():
    """Footprints are compared in canvas units: (p * art.scale + art.xy) * layer.scale + layer.xy (the worker's
    transform) - moving / scaling a layer changes what it overlaps."""
    layers = _layers({"thickness": 0.1}, {"thickness": 0.1})
    shapes = _shapes(l0=(0.0, (-0.2, -0.2, 0.2, 0.2)), l1=(0.0, (-0.1, -0.1, 0.1, 0.1)))
    assert stacking.restack(layers, shapes, gap=0.03, lift=0.0) == pytest.approx([0.0, 0.13])
    layers[1].transform.x = 0.6                                     # moved off the lower layer
    assert stacking.restack(layers, shapes, gap=0.03, lift=0.0) == pytest.approx([0.0, 0.0])
    layers[1].transform.x = 0.0
    layers[1].transform.scale = 2.0                                 # the art offset scales with the layer
    off = _shapes(l0=(0.0, (0.0, 0.0, 0.2, 0.2)), l1=(0.0, (0.0, 0.0, 0.2, 0.2)))
    fps = stacking.footprints(layers, off, art_scale=1.0, art_offset=(0.5, 0.0))
    assert fps[0].bounds == pytest.approx((0.5, 0.0, 0.7, 0.2))
    assert fps[1].bounds == pytest.approx((1.0, 0.0, 1.4, 0.4))
    assert stacking.restack(layers, off, gap=0.03, lift=0.0, art_offset=(0.5, 0.0)) == pytest.approx([0.0, 0.0])
    assert stacking.restack(layers, off, gap=0.03, lift=0.0) == pytest.approx([0.0, 0.13])


def test_stack_gap_recognises_overlap_aware_sequential_and_custom_stacks():
    g = stacking.geometry_rules()["stackGap"]
    layers = _layers({"thickness": 0.1}, {"thickness": 0.1}, {"thickness": 0.1})
    shapes = _shapes(l0=(0.0, (-0.9, -0.2, -0.5, 0.2)), l1=(0.0, (0.5, -0.2, 0.9, 0.2)),
                     l2=(0.0, (-0.8, -0.1, -0.6, 0.1)))
    stacking.restack(layers, shapes, gap=0.05)
    assert [L.depth.z for L in layers] == pytest.approx([0.0, 0.0, 0.15])
    assert stacking.stack_gap(layers, shapes) == pytest.approx(0.05)
    stacking.restack(layers, {"l0": 0.0, "l1": 0.0, "l2": 0.0}, gap=0.05)      # the round-7 sequential stack
    assert [L.depth.z for L in layers] == pytest.approx([0.0, 0.15, 0.3])
    assert stacking.stack_gap(layers, shapes) == pytest.approx(0.05)            # recognised -> converts on edit
    for L in layers:
        L.depth.z = 0.0
    layers[2].depth.z = 0.13
    assert stacking.stack_gap(layers, shapes) == pytest.approx(0.03)
    layers[2].depth.z = 0.4                                     # one stacked layer: its distance is the gap
    assert stacking.stack_gap(layers, shapes) == pytest.approx(0.3)
    four = layers + _layers({}, {}, {}, {"z": 0.5, "thickness": 0.1})[3:]
    shapes4 = {**shapes, "l3": stacking.LayerShape(0.0, _box(0.6, -0.1, 0.8, 0.1))}   # on the right one
    assert stacking.stack_gap(four, shapes4) is None                           # gaps 0.3 and 0.4: hand-placed
    four[3].depth.z = 0.4
    assert stacking.stack_gap(four, shapes4) == pytest.approx(0.3)
    layers[1].depth.z = 0.2                                                     # a base layer off the base
    layers[2].depth.z = 0.13
    assert stacking.stack_gap(layers, shapes) is None
    side = _shapes(l0=(0.0, (0, 0, 0.1, 0.1)), l1=(0.0, (0.5, 0, 0.6, 0.1)), l2=(0.0, (-0.6, 0, -0.5, 0.1)))
    for L in layers:
        L.depth.z = 0.0
    assert stacking.stack_gap(layers, side) == pytest.approx(g)                 # all on the base


def test_lift_overlaps_and_interpenetrations_are_overlap_aware():
    layers = _layers({"z": 0.0, "thickness": 0.2}, {"z": 0.05, "thickness": 0.1}, {"z": 0.1, "thickness": 0.1})
    shapes = _shapes(l0=(0.0, (0, 0, 0.4, 0.4)), l1=(0.0, (0.6, 0, 0.9, 0.4)), l2=(0.0, (0.1, 0.1, 0.2, 0.2)))
    assert stacking.interpenetrations(layers, shapes) == [(0, 2, pytest.approx(0.1))]
    stacking.lift_overlaps(layers, shapes, gap=0.03)
    assert [L.depth.z for L in layers] == pytest.approx([0.0, 0.05, 0.23])     # l1 is beside l0: not lifted
    assert stacking.interpenetrations(layers, shapes) == []


def test_hidden_layers_take_no_stack_slot():
    """QA r11 N12 (Find Device: the dot floated over the hidden baked sweep): no layer stacks above a HIDDEN layer; the
    hidden layer's own z is still stacked over the visible layers it overlaps (unhiding puts it there, and a re-stack
    then lifts the layers above it); hidden layers never collide. A round-9 stack (hidden layers kept their slot) is
    still recognised as a rule stack, so the next edit converts it."""
    g = 0.03
    rules = {"geometry": {"stackGap": g, "stackLift": 0.0}}
    layers = _layers({"thickness": 0.3}, {"thickness": 0.02}, {"thickness": 0.1})
    layers[1].visible = False                                       # the baked sweep over the dome
    shapes = _shapes(l0=(0.0, (-0.8, -0.8, 0.8, 0.8)), l1=(0.0, (-0.6, -0.2, 0.6, 0.4)),
                     l2=(0.0, (-0.1, -0.1, 0.1, 0.1)))              # the dot: on the dome and under the sweep
    assert stacking.overlap_lists(layers, shapes, clearance=g) == [[], [0], [0, 1]]
    assert stacking.stack_lower(layers, [[], [0], [0, 1]]) == [[], [0], [0]]
    zs = stacking.restack(layers, shapes, presets=rules)
    assert zs == pytest.approx([0.0, 0.33, 0.33])                   # the dot sits on the dome, not on the sweep
    assert stacking.stack_clearances(layers, shapes, presets=rules) == pytest.approx([None, g, g])
    assert stacking.stack_gap(layers, shapes, presets=rules) == pytest.approx(g)
    assert stacking.interpenetrations(layers, shapes) == []        # the hidden sweep shares the dot's z range
    layers[1].visible = True                                        # unhidden: it sits on the dome, the dot cuts it
    assert [L.depth.z for L in layers] == pytest.approx([0.0, 0.33, 0.33])
    assert stacking.interpenetrations(layers, shapes) == [(1, 2, pytest.approx(0.02))]
    assert stacking.restack(layers, shapes, presets=rules) == pytest.approx([0.0, 0.33, 0.38])
    assert stacking.interpenetrations(layers, shapes) == []
    # a round-9 stack (the hidden sweep kept its slot: the dot floats 0.05 higher) is still a rule stack
    layers[1].visible = False
    assert stacking.stack_gap(layers, shapes, presets=rules) == pytest.approx(g)
    assert stacking.restack(layers, shapes, presets=rules) == pytest.approx([0.0, 0.33, 0.33])
    layers[2].depth.z = 0.5                                         # neither rule: hand-placed
    assert stacking.stack_gap(layers, shapes, presets=rules) is None
    # a hand-placed stack after a structural edit: a layer reaching into a hidden one is not lifted, one reaching into a
    # visible one is (and a hidden layer is lifted onto the visible ones below it too)
    layers[2].depth.z = 0.34
    stacking.lift_overlaps(layers, shapes, gap=g)
    assert [L.depth.z for L in layers] == pytest.approx([0.0, 0.33, 0.34])
    layers[1].depth.z, layers[2].depth.z = 0.1, 0.2
    stacking.lift_overlaps(layers, shapes, gap=g)
    assert [L.depth.z for L in layers] == pytest.approx([0.0, 0.33, 0.33])
    # a hidden layer at the bottom: the layers above stack on the visible ones only (here: on the base)
    layers[0].visible, layers[1].visible = False, True
    assert stacking.restack(layers, shapes, presets=rules) == pytest.approx([0.0, 0.0, 0.05])


def _pieces_shape(*boxes, r=0.0, S=1.0):
    from shapely import union_all

    ps = [_box(*b) for b in boxes]
    return stacking.LayerShape(r, union_all(ps), ps, S)


def test_in_layer_stacked_height():
    """H = max(rule, in-layer stacked height): pieces of an 'individual' layer that OVERLAP an earlier piece are
    stacked on it inside the layer (the worker's _body_height); touching pieces and 'combined' layers are not."""
    L = _layers({"thickness": 0.16, "bevel": 0.08, "inflate": 0.25})[0]
    over = _pieces_shape((0, 0, 0.6, 0.4), (0.3, 0.1, 0.7, 0.3), r=0.2)
    assert over.pairs() == [(0, 1)]
    assert over.piece_radii() == pytest.approx([0.2, 0.1], abs=1e-3)
    rule = stacking.body_height(L.depth, 0.2)
    h = [stacking.half_height(0.16, 0.08, 0.25, over.piece_radii()[0]),
         stacking.half_height(0.16, 0.08, 0.25, over.piece_radii()[1])]
    assert h == pytest.approx([0.08 + 0.05, 0.08 + 0.025], abs=1e-3)
    shift = h[0] + stacking.PIECE_STACK_GAP + h[1]
    assert stacking.layer_height(L, over) == pytest.approx(max(rule, shift + h[1] + h[0]))
    assert stacking.layer_height(L, over) > rule + 0.2
    L.mode = "combined"
    assert stacking.layer_height(L, over) == pytest.approx(rule)
    L.mode = "individual"
    touch = _pieces_shape((0, 0, 0.3, 0.4), (0.3, 0, 0.6, 0.4), r=0.15)          # a shared edge only
    assert touch.pairs() == []
    assert stacking.layer_height(L, touch) == pytest.approx(stacking.body_height(L.depth, 0.15))
    flat = _layers({"thickness": 0.1, "bevel": 0.02, "inflate": 0.0})[0]
    assert stacking.layer_height(flat, over) == pytest.approx(0.05 + 0.002 + 0.05 + 0.05 + 0.05)
    # the stack uses it: the layer above clears the real top
    two = [L, _layers({"thickness": 0.1})[0]]
    two[1].id = "top"
    zs = stacking.restack(two, {"l0": over, "top": _pieces_shape((0.4, 0.1, 0.5, 0.2))}, gap=0.03, lift=0.0)
    assert zs[1] == pytest.approx(stacking.layer_height(L, over) + 0.03, abs=1e-5)


def test_pieces_relation():
    t = 0.0018
    assert stacking.pieces_relation(_box(0, 0, 1, 1), _box(2, 0, 3, 1), t) == 0
    assert stacking.pieces_relation(_box(0, 0, 1, 1), _box(1, 0, 2, 1), t) == 1                 # shared edge
    assert stacking.pieces_relation(_box(0, 0, 1, 1), _box(1.001, 0, 2, 1), t) == 1             # within tol
    assert stacking.pieces_relation(_box(0, 0, 1, 1), _box(0.5, 0.2, 1.5, 0.8), t) == 2         # overlap
    assert stacking.pieces_relation(_box(0, 0, 1, 1), _box(0.2, 0.2, 0.8, 0.8), t) == 2         # inside
    assert stacking.pieces_relation(_box(0, 0, 1, 1), _box(0, 0, 1, 1), t) == 2                 # coincident
    assert stacking.pieces_relation(_box(0, 0, 1, 1), _box(0.0005, 0.0005, 1.0005, 1.0005), t) == 2
    assert stacking.pieces_relation(_box(0, 0, 1, 1), _box(0.9995, 0, 2, 1), t) == 1            # 0.0005 overlap


def test_port_matches_the_worker_heightfield():
    """half_height / stack_shifts / pieces_relation are ports of blender_worker.heightfield (pure numpy)."""
    import numpy as np

    if str(ROOT) not in sys.path:
        sys.path.append(str(ROOT))
    try:
        from blender_worker import heightfield as hf
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"blender_worker.heightfield not importable: {e}")
    assert stacking.WALL_MIN == hf.WALL_MIN
    for args in ((0.16, 0.08, 0.25, 0.3), (0.16, 0.08, 0.0, 0.05), (0.1, 0.02, 1.0, 0.01), (0.2, 0.0, 0.5, 0.4),
                 (0.16, 0.08, 0.25, 0.01), (0.16, 0.08, 1.0, 0.07), (0.1, 0.05, 0.5, 0.2), (0.02, 0.006, 0.0, 0.3)):
        assert stacking.half_height(*args) == pytest.approx(hf.half_height(*args)), args   # incl. the minimum wall
    pairs, halves = [(0, 1), (1, 2), (0, 3)], [0.1, 0.05, 0.2, 0.07]
    assert stacking.stack_shifts(4, pairs, halves, 0.002, [0.1, 0.1, 0.12, 0.1]) == pytest.approx(
        list(hf.stack_shifts(4, pairs, halves, 0.002, [0.1, 0.1, 0.12, 0.1])))

    def rings(b):
        x0, y0, x1, y1 = b
        sq = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=float)
        return hf.piece_rings(hf.rings_to_splines([sq]))

    t = 0.0018
    cases = [((0, 0, 1, 1), (2, 0, 3, 1)), ((0, 0, 1, 1), (1, 0, 2, 1)), ((0, 0, 1, 1), (0.5, 0.2, 1.5, 0.8)),
             ((0, 0, 1, 1), (0.2, 0.2, 0.8, 0.8)), ((0, 0, 1, 1), (0, 0, 1, 1)), ((0, 0, 1, 1), (1.001, 0, 2, 1))]
    for a, b in cases:
        assert stacking.pieces_relation(_box(*a), _box(*b), t) == hf.rings_relation(rings(a), rings(b), t), (a, b)


def test_shape_from_geometry_reads_bundle_splines():
    """A LayerGeometry (dict) -> footprint (even-odd: a ring's hole is not footprint), pieces, maxRadius."""
    import math

    def spl(pts, hole=False):
        return {"closed": True, "hole": hole, "points": [{"co": p, "hl": p, "hr": p} for p in pts]}

    k = 0.5522847498 * 0.3                                            # a circle of radius 0.3 at (0.5, 0)
    circle = {"closed": True, "points": [
        {"co": [0.8, 0.0], "hl": [0.8, -k], "hr": [0.8, k]},
        {"co": [0.5, 0.3], "hl": [0.5 + k, 0.3], "hr": [0.5 - k, 0.3]},
        {"co": [0.2, 0.0], "hl": [0.2, k], "hr": [0.2, -k]},
        {"co": [0.5, -0.3], "hl": [0.5 - k, -0.3], "hr": [0.5 + k, -0.3]}]}
    ring = [spl([(-1, -1), (0, -1), (0, 0), (-1, 0)]),
            spl([(-0.8, -0.8), (-0.2, -0.8), (-0.2, -0.2), (-0.8, -0.2)], True)]
    lg = {"layerId": "a", "hash": "h-test-geom", "maxRadius": 0.3, "silhouette": ring + [circle],
          "regions": [{"elementId": "e0", "splines": ring}, {"elementId": "e1", "splines": [circle]}],
          "images": [{"elementId": "img9", "bbox": [0.9, 0.9, 1.0, 1.0]}]}
    sh = stacking.shape_from_geometry(lg)
    assert sh.maxRadius == 0.3 and len(sh.pieces) == 2
    assert sh.pieces[1].area == pytest.approx(math.pi * 0.09, rel=2e-3)
    assert sh.pieces[0].area == pytest.approx(1.0 - 0.36, abs=1e-6)
    assert sh.footprint.area == pytest.approx(0.64 + math.pi * 0.09 + 0.01, rel=3e-3)   # + the raster card
    assert stacking.as_shape(lg) is sh                                 # cached by hash
    dot = stacking.LayerShape(0.0, _box(-0.6, -0.6, -0.4, -0.4))      # inside the hole: no overlap
    two = _layers({"thickness": 0.1}, {"thickness": 0.1})
    assert stacking.restack(two, {"l0": sh, "l1": dot}, gap=0.03, lift=0.0, clearance=0.03) == [0.0, 0.0]


def test_image_cards():
    """Round 8: raster image layers are flat cards (no dome, at most 0.02 thick, round edge at most 0.006)."""
    kinds = {"e0": "path", "img0": "image", "img1": "image"}
    L = _layers({"thickness": 0.16, "bevel": 0.08, "inflate": 0.25})[0]
    L.elementIds = ["img0", "img1"]
    assert stacking.is_image_layer(L, kinds)
    L.elementIds = ["img0", "e0"]
    assert not stacking.is_image_layer(L, kinds)
    L.elementIds = []
    assert not stacking.is_image_layer(L, kinds)
    d = stacking.card_depth(LayerDepth(thickness=0.16, bevel=0.08, inflate=0.25))
    assert (d.thickness, d.bevel, d.inflate) == (0.02, 0.006, 0.0)
    d = stacking.card_depth(LayerDepth(thickness=0.01, bevel=0.08, inflate=1.0))
    assert (d.thickness, d.bevel, d.inflate) == (0.01, 0.005, 0.0)


def test_card_elements_are_soft_rasters():
    """Round 9: only SOFT-alpha rasters make a flat card (Element.softAlpha True; None = imported before round 9 - a
    card, as it was imported); a crisp raster (False) is a body like vector art. Models and plain dicts alike."""
    from bis.models import Element, FillSolid

    els = [Element(id="e0", name="e0", paint=FillSolid(), bbox=(-1, -1, 1, 1), area=1.0),
           Element(id="img0", name="i0", kind="image", role="image", paint=FillSolid(), bbox=(-1, -1, 1, 1), area=1.0,
                   softAlpha=True),
           Element(id="img1", name="i1", kind="image", role="image", paint=FillSolid(), bbox=(-1, -1, 1, 1), area=1.0,
                   softAlpha=False),
           Element(id="img2", name="i2", kind="image", role="image", paint=FillSolid(), bbox=(-1, -1, 1, 1), area=1.0)]
    cards = stacking.card_elements(els)
    assert cards == {"img0", "img2"}
    assert stacking.card_elements([e.model_dump(mode="json") for e in els]) == cards
    L = _layers({"thickness": 0.16})[0]
    for ids, want in ((["img0"], True), (["img0", "img2"], True), (["img1"], False), (["img0", "img1"], False),
                      (["img0", "e0"], False), ([], False)):
        L.elementIds = ids
        assert stacking.is_card_layer(L, cards) is want, ids
