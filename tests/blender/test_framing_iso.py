"""CAD iso view in REAL Blender 5.0 renders (PLAN §11 View; the maths is in test_framing.py).

``camera.iso`` 0..1 turns an orthographic camera from head-on to isometric (pitch 35.264°, yaw 45°): layers stay at
their REAL z (``camera.explode`` is legacy and ignored), every view is auto-framed (iso 0 == the front framing),
and the bodies seen from the side are clean height-field meshes. Draft renders at 128 px."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import worker_client as wc  # noqa: E402

pytestmark = pytest.mark.skipif(not wc.blender_available(), reason="Blender 5.0 not installed")

PX = 128
LAYER_EPS = 0.002          # scene.LAYER_EPS


@pytest.fixture(scope="module")
def fx() -> dict:
    import make_fixtures
    out = HERE / "_fixtures" / "corpus"          # shared with test_heightfield_corpus.py (never the worker suites')
    index_path = out / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    names = ["Photos", "Ti84", "Gemini"]
    if any(n not in index or not Path(index[n]["geometryPath"]).exists() for n in names):
        index = make_fixtures.make([n for n in names if n not in index or not Path(index[n]["geometryPath"]).exists()],
                                   out)
    return {n: {"project": json.loads(Path(index[n]["project"]).read_text(encoding="utf-8")),
                "geometryPath": index[n]["geometryPath"]} for n in names}


@pytest.fixture(scope="module")
def worker():
    w = wc.Worker(warmup="none")
    yield w
    w.close()


def _box(path: str, thr: int = 128) -> dict:
    import numpy as np
    from PIL import Image
    a = np.asarray(Image.open(path).convert("RGBA"))[..., 3]
    ys, xs = np.nonzero(a > thr)
    h, w = a.shape
    return {"touch": bool(xs.min() == 0 or ys.min() == 0 or xs.max() == w - 1 or ys.max() == h - 1),
            "cx": (xs.min() + xs.max() + 1) / 2 / w, "cy": (ys.min() + ys.max() + 1) / 2 / h,
            "ext": max(xs.max() - xs.min() + 1, ys.max() - ys.min() + 1) / max(w, h)}


def _render(worker, f, out, camera, **kw):
    args = {"project": f["project"], "geometryPath": f["geometryPath"], "quality": "draft", "size": PX,
            "out": str(out), "camera": camera, **kw}
    return worker.result("render", args)


def test_iso_views_are_framed_with_real_distances(worker, fx, tmp_path):
    f = fx["Photos"]
    proj = f["project"]
    frames = {}
    for iso in (0.0, 0.5, 1.0):
        out = tmp_path / f"iso_{iso}.png"
        # explode 3 is legacy: it must not move anything
        _render(worker, f, out, {"view": "front", "iso": iso, "explode": 3.0})
        info = worker.result("scene_info")
        objs = {o["name"]: o for o in info["objects"]}
        frames[iso] = (_box(str(out)), objs)
    b0 = frames[0.0][0]
    assert abs(b0["ext"] - 2.0 / 2.24) < 0.015, b0                 # iso 0 continues the front framing exactly
    assert abs(b0["cx"] - 0.5) < 0.01 and abs(b0["cy"] - 0.5) < 0.01, b0
    for iso in (0.5, 1.0):
        b, objs = frames[iso]
        assert not b["touch"], (iso, b)
        assert abs(b["cx"] - 0.5) < 0.03 and abs(b["cy"] - 0.5) < 0.03, (iso, b)
        assert 0.8 < b["ext"] < 0.93, (iso, b)                       # fitted: (2.0 / 2.24 of the frame)
        for L in proj["layers"]:                                     # REAL z: depth.z + ε, whatever the view
            loc = objs[f"BIS Layer {L['id']}"]["location"]
            assert loc[2] == pytest.approx(float(L["depth"]["z"]) + LAYER_EPS, abs=1e-4), (iso, L["id"], loc)
    # the camera sits on the isometric diagonal (front-right-top): direction (1, −1, 1)/√3 from the subject
    cam = frames[1.0][1]["BIS Camera"]["location"]
    n = math.sqrt(sum(c * c for c in cam))
    assert all(abs(c / n - s / math.sqrt(3)) < 0.06 for c, s in zip(cam, (1, -1, 1))), cam
    cam0 = frames[0.0][1]["BIS Camera"]["location"]
    assert abs(cam0[0]) < 0.05 and abs(cam0[1]) < 0.05 and cam0[2] > 1.0, cam0


def test_iso_side_view_bodies_are_clean(worker, fx, tmp_path):
    """Ti84 at maximum roundness seen from the side: every body is a watertight, non-self-intersecting height
    field with outward normals (what the iso view exposes: thin keys taper instead of folding)."""
    f = fx["Ti84"]
    proj = json.loads(json.dumps(f["project"]))
    for L in proj["layers"]:
        L["depth"]["bevel"] = float(L["depth"]["thickness"]) / 2.0
    _render(worker, {**f, "project": proj}, tmp_path / "ti84_iso.png", {"view": "front", "iso": 1.0})
    objs = worker.result("scene_info", {"check": True})["objects"]
    bodies = [o for o in objs if o.get("check")]
    assert len(bodies) >= 10
    for o in bodies:
        c = o["check"]
        assert c["nonManifold"] == 0 and c["selfIntersections"] == 0 and c["invertedNormals"] == 0, (o["name"], c)
        assert c["volume"] > 0, (o["name"], c)
    assert not _box(str(tmp_path / "ti84_iso.png"))["touch"]


def test_light_rig_turns_with_the_camera(worker, fx, tmp_path):
    """PLAN §11 round 7, camera-relative lighting: the key / rims / fill sit at the head-on positions turned by the
    camera's rotation (lighting.angle is relative to the VIEW). At iso 1 a world-fixed key (−45°, elevation 50°) sat
    ~5° from the mirror direction of the flat tops and Gemini's blue star rendered pure white (254, 254, 254)."""
    import numpy as np
    sys.path.insert(0, str(HERE.parents[1]))
    from blender_worker import framing
    f = fx["Gemini"]
    lights = {}
    for iso in (0.0, 1.0):
        out = tmp_path / f"gem_iso{iso}.png"
        _render(worker, f, out, {"view": "front", "iso": iso}, quality="preview", samples=16)
        objs = {o["name"]: o for o in worker.result("scene_info")["objects"]}
        lights[iso] = {n: np.array(objs[n]["location"]) for n in ("BIS Key", "BIS RimTop", "BIS RimOpposite", "BIS Fill")}
        if iso == 1.0:
            from PIL import Image
            a = np.asarray(Image.open(out).convert("RGB")).astype(float)
            h, w = a.shape[:2]
            c = a[h // 2 - 3:h // 2 + 4, w // 2 - 3:w // 2 + 4].reshape(-1, 3).mean(0)
            assert c[2] > c[0] + 60 and c.mean() < 200, c.round()          # the star stays blue, no mirror glare
    _d, R = framing.iso_basis(1.0)
    for name, p0 in lights[0.0].items():
        assert np.allclose(lights[1.0][name], R @ p0, atol=1e-3), (name, lights[1.0][name], R @ p0)
    # head-on: the classic rig (angle −45 = up-left of the screen, in front of the icon)
    key0 = lights[0.0]["BIS Key"]
    assert key0[0] < 0 and key0[1] > 0 and key0[2] > 0, key0
