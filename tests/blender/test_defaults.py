"""The worker's plain-dict defaults (blender_worker/defaults.py) must mirror server/bis/models.py exactly.

Runs in the backend venv (no Blender needed): every partial dict normalised by the worker must equal the
pydantic model_dump of the same partial input.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "server"))

from blender_worker import defaults as D  # noqa: E402

models = pytest.importorskip("bis.models")

MINIMAL = {"id": "p1", "name": "Test", "createdAt": "2026-01-01", "updatedAt": "2026-01-01",
           "source": {"filename": "x.svg", "viewBox": [0, 0, 500, 500]}}


def _dump(model) -> dict:
    return model.model_dump(mode="json")


def _norm_tuples(x):
    if isinstance(x, (list, tuple)):
        return [_norm_tuples(v) for v in x]
    if isinstance(x, dict):
        return {k: _norm_tuples(v) for k, v in x.items()}
    return x


@pytest.mark.parametrize("partial", [
    {},
    {"layers": [{"id": "L0", "name": "a", "elementIds": ["e0"]}]},
    {"layers": [{"id": "L0", "name": "a", "elementIds": [], "fill": {"type": "solid"},
                 "depth": {"z": 0.2}, "material": {"preset": "chrome"}, "shadow": {"kind": "chromatic"}}]},
    {"layers": [{"id": "L1", "name": "b", "elementIds": [],
                 "fill": {"type": "linear", "stops": [{"offset": 0, "color": "#ff0000"}]}}]},
    {"layers": [{"id": "L2", "name": "c", "elementIds": [],
                 "fill": {"type": "radial", "stops": [{"offset": 0, "color": "#00ff00", "opacity": 0.5}]}}]},
    {"layers": [{"id": "L3", "name": "d", "elementIds": [],
                 "fill": {"type": "radial", "stops": [], "radius": 0, "focal": [0.2, 0.1]}}]},
    {"canvas": {"shape": "circle", "plate": {"fill": {"type": "system-dark"}, "thickness": 0.2}}},
    {"lighting": {"angle": 30}, "camera": {"view": "perspective", "tiltX": 10}},
    {"appearances": {"dark": {}, "tint": {"color": "#ff00ff"}}},
    {"appearances": {"mono": {"layers": {"L0": {"opacity": 0.5}}}}},
    {"render": {"quality": "preview", "colorMode": "agx"}, "appearance": "tinted-dark"},
])
def test_project_defaults_mirror_models(partial):
    raw = {**MINIMAL, **partial}
    expected = _norm_tuples(_dump(models.Project.model_validate(raw)))
    got = _norm_tuples(D.norm_project(raw))
    assert got == expected


def test_bundle_defaults_mirror_models():
    raw = {"projectId": "p", "hash": "h", "viewBox": [0, 0, 1, 1], "layers": {"L0": {
        "layerId": "L0", "hash": "x", "silhouette": [{"points": []}],
        "regions": [{"elementId": "e0", "paint": {"type": "solid", "color": "#123456"}, "splines": []}],
        "safeRadius": 0.03, "bbox": [-1, -1, 1, 1], "texture": "/t.png", "texturePath": "C:/t.png", "svg": "/s.svg"}}}
    expected = _norm_tuples(_dump(models.GeometryBundle.model_validate(raw)))
    got = _norm_tuples(D.norm_bundle(raw))
    assert got == expected


def test_fill_fallbacks():
    assert D.norm_fill(None) == {"type": "auto"}
    assert D.norm_fill({"type": "bogus"}) == {"type": "auto"}
    assert D.norm_fill({"type": "solid"}) == {"type": "solid", "color": "#ffffff", "opacity": 1.0}
