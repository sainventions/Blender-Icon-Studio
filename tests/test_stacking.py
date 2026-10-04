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
