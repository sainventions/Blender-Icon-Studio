"""Looks, style extraction and style transfer (PLAN §10) — pure functions + the REST endpoints.

Unit tests need nothing; the API tests use the FakeBridge and the real ``bis.svg`` pipeline (corpus icons) so the
bevel clamp is checked against real safe radii.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from fastapi.testclient import TestClient  # noqa: E402

from bis.blender import FakeBridge  # noqa: E402
from bis.main import create_app  # noqa: E402
from bis.models import (  # noqa: E402
    CameraSpec,
    FillSolid,
    FillSystem,
    Layer,
    LayerDepth,
    LayerOverride,
    Lighting,
    MaterialSpec,
    Project,
    SourceInfo,
    StyleSpec,
    Tint,
)
from bis.style import (  # noqa: E402
    LookNotFound,
    StyleError,
    apply_style,
    clamp_bevel,
    deep_merge,
    extract_style,
    resolve_look,
    resolve_style_request,
)
from bis.models import StyleRequest  # noqa: E402
from bis.testing import make_test_settings  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")
PRESETS = json.loads((ROOT / "shared" / "presets.json").read_text(encoding="utf-8"))


def _real_svg() -> bool:
    try:
        import bis.svg as s

        return all(hasattr(s, n) for n in ("import_svg", "build_geometry", "geometry_path"))
    except Exception:
        return False


def _project(n_layers: int = 3, **kw) -> Project:
    layers = [Layer(id=f"l{i}", name=f"Layer {i}", elementIds=[f"e{i}"],
                    depth=LayerDepth(z=0.2 * i, thickness=0.08, bevel=0.03))
              for i in range(n_layers)]
    return Project(id="p", name="P", createdAt="t", updatedAt="t",
                   source=SourceInfo(filename="p.svg", viewBox=(0, 0, 100, 100)), layers=layers, **kw)


# ---------------------------------------------------------------------------------------------- looks
@pytest.mark.parametrize("look_id", sorted(PRESETS["looks"]))
def test_every_look_resolves(look_id):
    look = PRESETS["looks"][look_id]
    style = resolve_look(PRESETS, look_id)
    assert isinstance(style, StyleSpec)
    want = look["style"]
    assert style.layerDefaults.material.preset == want["layerDefaults"]["material"]["preset"]
    assert style.layerDefaults.material.preset in PRESETS["materials"]
    assert style.plate.material.preset in PRESETS["materials"]
    if "lighting" in want:
        assert style.lighting is not None and style.lighting.preset == want["lighting"]["preset"]
        assert style.lighting.preset in PRESETS["lighting"]
    assert style.zGap == want.get("zGap", StyleSpec().zGap)
    # round 5: looks never carry a layer mode - each icon keeps its art-derived ('combined' for tiles) mode
    assert "mode" not in want["layerDefaults"] and style.layerDefaults.mode is None


def test_resolve_look_merges_over_defaults_and_rejects_unknown():
    style = resolve_look(PRESETS, "crystal")
    assert style.layerDefaults.material.preset == "clear_glass"
    assert style.layerDefaults.material.params == {"tint": 0.35}
    assert style.zGap == 0.15 and style.lighting.preset == "darkfield"
    assert style.plate.fill is None and style.camera is None          # not in the look → keep the icon's own
    neon = resolve_look(PRESETS, "neon")
    assert neon.plate.fill is not None and neon.plate.fill.type == "system-dark"
    partial = {"looks": {"x": {"label": "X", "style": {"layerDefaults": {"depth": {"bevel": 0.07}},
                                                       "lighting": {"angle": 30}}}}}
    s = resolve_look(partial, "x")
    assert s.layerDefaults.depth.bevel == 0.07 and s.layerDefaults.depth.thickness == 0.10   # default kept
    assert s.layerDefaults.material.preset == "liquid_glass"
    assert s.lighting.angle == 30 and s.lighting.preset == "studio"                       # Lighting defaults
    with pytest.raises(LookNotFound):
        resolve_look(PRESETS, "nope")
    assert deep_merge({"a": {"b": 1, "c": 2}, "l": [1]}, {"a": {"b": 3}, "l": [2]}) == {"a": {"b": 3, "c": 2}, "l": [2]}


# ---------------------------------------------------------------------------------------------- apply
def test_apply_style_rules():
    p = _project(3)
    p.canvas.plate.fill = FillSolid(color="#123456")
    p.appearances.dark.layers["l1"] = LayerOverride(material=MaterialSpec(preset="chrome"), opacity=0.5)
    style = StyleSpec.model_validate({
        "layerDefaults": {"material": {"preset": "candy", "params": {"tint": 0.4}},
                          "depth": {"z": 9, "thickness": 0.11, "bevel": 0.05, "bevelSegments": 8, "inflate": 0.2},
                          "shadow": {"kind": "chromatic", "opacity": 0.6}, "mode": "combined"},
        "zGap": 0.15,
        "plate": {"material": {"preset": "glossy_plastic"}, "thickness": 0.2, "bevel": 0.05},
        "lighting": {"preset": "dramatic", "angle": -30},
        "camera": {"view": "perspective", "tiltX": 10},
        "colorMode": "agx",
        "tint": {"color": "#ff0000", "strength": 0.5},
    })
    out = apply_style(p, style, {"l0": 0.01, "l1": 0.5})
    assert p.layers[0].material.preset == "liquid_glass" and p.layers[0].depth.z == 0.0  # input untouched
    assert [l.material.preset for l in out.layers] == ["candy"] * 3
    assert out.layers[0].material.params == {"tint": 0.4}
    assert [l.depth.z for l in out.layers] == [0.0, 0.15, 0.3]
    assert out.layers[0].depth.bevel == pytest.approx(0.009)        # 0.9 × safeRadius 0.01
    assert out.layers[1].depth.bevel == 0.05                         # safe radius large enough
    assert out.layers[2].depth.bevel == 0.05                         # unknown radius → requested
    assert all(l.depth.thickness == 0.11 and l.depth.bevelSegments == 8 and l.depth.inflate == 0.2
               for l in out.layers)
    assert all(l.shadow.kind == "chromatic" and l.shadow.opacity == 0.6 and l.mode == "combined"
               for l in out.layers)
    assert out.canvas.plate.material.preset == "glossy_plastic"
    assert (out.canvas.plate.thickness, out.canvas.plate.bevel) == (0.2, 0.05)
    assert out.canvas.plate.fill == FillSolid(color="#123456")      # fill None → keep
    assert out.canvas.shape == "squircle"                            # shape None → keep
    assert out.lighting.preset == "dramatic" and out.lighting.angle == -30
    assert out.camera.view == "perspective" and out.camera.tiltX == 10
    assert out.render.colorMode == "agx" and out.appearances.tint == Tint(color="#ff0000", strength=0.5)
    ov = out.appearances.dark.layers["l1"]
    assert ov.material is None and ov.opacity == 0.5                 # the look shows in every appearance

    keep = apply_style(p, StyleSpec(zGap=None, plate={"fill": {"type": "system-dark"}, "shape": "circle"}))
    assert [l.depth.z for l in keep.layers] == [0.0, 0.2, 0.4]       # zGap None → keep z
    assert keep.canvas.plate.fill == FillSystem(type="system-dark") and keep.canvas.shape == "circle"
    assert keep.lighting == p.lighting and keep.camera == p.camera and keep.render.colorMode == p.render.colorMode


def test_style_without_mode_keeps_each_layers_mode():
    """QA round 4 #8: a look (mode None) never turns a tiling-derived 'combined' layer back into 'individual'
    (and never fuses an 'individual' one); the result stays a valid Project (round-4 HEAD wrote mode null)."""
    p = _project(3)
    p.layers[0].mode = "combined"
    p.layers[2].mode = "combined"
    for look_id in sorted(PRESETS["looks"]):
        out = apply_style(p, resolve_look(PRESETS, look_id))
        assert [l.mode for l in out.layers] == ["combined", "individual", "combined"], look_id
        Project.model_validate(out.model_dump(mode="json"))          # never null
    forced = apply_style(p, StyleSpec(layerDefaults={"mode": "individual"}))
    assert {l.mode for l in forced.layers} == {"individual"}           # an explicit mode still applies
    assert [l.mode for l in p.layers] == ["combined", "individual", "combined"]   # input untouched


def test_extracted_mode_only_when_the_user_set_it():
    p = _project(3)
    auto = {"l0": "combined", "l1": "individual", "l2": "individual"}
    p.layers[0].mode = "combined"
    assert extract_style(p).layerDefaults.mode is None                       # auto modes unknown → never copied
    assert extract_style(p, auto_modes=auto).layerDefaults.mode is None      # all art-derived (mixed)
    for l in p.layers:
        l.mode = "combined"                                                  # the user fused l1 + l2
    assert extract_style(p, auto_modes=auto).layerDefaults.mode == "combined"
    assert extract_style(p).layerDefaults.mode is None
    p.layers[1].mode = "individual"                                          # mixed → None
    assert extract_style(p, auto_modes=auto).layerDefaults.mode is None
    for l in p.layers:
        l.mode = "individual"   # the user split l0's body - or a pre-round-4 project: never copied (QA r4 #8)
    assert extract_style(p, auto_modes=auto).layerDefaults.mode is None
    q = _project(2)
    for l in q.layers:
        l.mode = "combined"
    assert extract_style(q, auto_modes={"l0": "combined", "l1": "combined"}).layerDefaults.mode is None  # art-derived
    assert extract_style(q, auto_modes={"zz": "individual"}).layerDefaults.mode is None   # unknown ids don't count
    q.layers[1].visible = False
    q.layers[1].mode = "individual"                                          # hidden layers are not the look
    assert extract_style(q, auto_modes={"l0": "individual", "l1": "individual"}).layerDefaults.mode == "combined"


def test_layer_materials_by_index_clamped():
    style = StyleSpec(layerMaterials=[MaterialSpec(preset="satin"), MaterialSpec(preset="neon")])
    out = apply_style(_project(4), style)
    assert [l.material.preset for l in out.layers] == ["satin", "neon", "neon", "neon"]
    assert apply_style(_project(0), style).layers == []


def test_clamp_bevel():
    assert clamp_bevel(0.045, 0.1) == 0.045
    assert clamp_bevel(0.045, 0.02) == 0.018
    assert clamp_bevel(0.045, 0.0) == 0.0
    assert clamp_bevel(0.05, 0.02403) == 0.02162 <= 0.9 * 0.02403   # rounded down, never above the limit


# ---------------------------------------------------------------------------------------------- extract
def test_extract_style_roundtrip():
    src = _project(3)
    src.layers[0].material = MaterialSpec(preset="satin")
    for l in src.layers[1:]:
        l.material = MaterialSpec(preset="candy", params={"tint": 0.3})
    src.layers[1].depth = LayerDepth(z=0.15, thickness=0.12, bevel=0.012, bevelSegments=8)   # clamped bevel
    src.layers[2].depth = LayerDepth(z=0.30, thickness=0.12, bevel=0.05, bevelSegments=8, inflate=0.2)
    src.layers[2].mode = "combined"
    src.lighting = Lighting(preset="soft", angle=20)
    src.camera = CameraSpec(zoom=1.1)
    src.render.colorMode = "standard"
    src.canvas.plate.fill = FillSolid(color="#ff00ff")
    s = extract_style(src)
    assert s.layerDefaults.material == MaterialSpec(preset="candy", params={"tint": 0.3})
    assert s.layerDefaults.depth.bevel == 0.05 and s.layerDefaults.depth.thickness == 0.12  # largest bevel
    assert s.layerDefaults.depth.inflate == 0.2 and s.layerDefaults.depth.z == 0.0
    assert [m.preset for m in s.layerMaterials] == ["satin", "candy", "candy"]
    assert s.zGap == 0.15
    assert s.plate.fill is None and s.plate.shape is None            # the icon's own colour stays its own
    assert s.lighting.preset == "soft" and s.camera.zoom == 1.1 and s.colorMode == "standard"
    assert extract_style(src, plate_fill=True, plate_shape=True).plate.fill == FillSolid(color="#ff00ff")
    src.canvas.plate.fill = FillSystem(type="system-dark")
    assert extract_style(src).plate.fill == FillSystem(type="system-dark")   # deliberate fills travel

    dst = apply_style(_project(5), s, {"l3": 0.02})
    assert [l.material.preset for l in dst.layers] == ["satin", "candy", "candy", "candy", "candy"]
    assert [l.depth.z for l in dst.layers] == [0.0, 0.15, 0.3, 0.45, 0.6]
    assert dst.layers[3].depth.bevel == 0.018 and dst.layers[4].depth.bevel == 0.05
    assert dst.lighting == src.lighting and dst.render.colorMode == "standard"

    uniform = extract_style(_project(1))
    assert uniform.layerMaterials is None and uniform.zGap is None


def test_resolve_style_request_rules():
    loader = {"src": _project(2)}.__getitem__
    assert resolve_style_request(StyleRequest(look="clay"), PRESETS, loader).layerDefaults.material.preset == "matte_clay"
    assert resolve_style_request(StyleRequest(fromProject="src"), PRESETS, loader).zGap == 0.2
    assert resolve_style_request(StyleRequest(style=StyleSpec(zGap=0.3)), PRESETS, loader).zGap == 0.3
    for bad in (StyleRequest(), StyleRequest(look="clay", fromProject="src")):
        with pytest.raises(StyleError):
            resolve_style_request(bad, PRESETS, loader)
    assert resolve_style_request(StyleRequest(), PRESETS, loader, allow_none=True) is None
    with pytest.raises(LookNotFound):
        resolve_style_request(StyleRequest(look="zzz"), PRESETS, loader)
    seen = []
    custom = resolve_style_request(StyleRequest(fromProject="src"), PRESETS, loader,
                                   extract=lambda pid: seen.append(pid) or StyleSpec(zGap=0.5))
    assert seen == ["src"] and custom.zGap == 0.5


# ---------------------------------------------------------------------------------------------- API
@pytest.fixture
def client(tmp_path):
    app = create_app(make_test_settings(tmp_path, auto_preview=False), bridge=FakeBridge())
    with TestClient(app) as c:
        yield c


def test_looks_endpoint_and_presets(client):
    lk = client.get("/api/looks").json()
    assert set(lk) == set(PRESETS["looks"]) and lk["crystal"]["label"] == "Crystal"
    assert client.get("/api/presets").json()["looks"] == lk


@pytest.mark.skipif(not _real_svg(), reason="bis.svg not importable")
def test_style_endpoints_with_real_geometry(client):
    c = client
    maps = c.post("/api/projects", json={"sample": "Maps"}).json()
    other = c.post("/api/projects", json={"sample": "Find Device"}).json()
    pid, oid = maps["id"], other["id"]

    s = c.get(f"/api/projects/{pid}/style").json()
    assert StyleSpec.model_validate(s).layerDefaults.material.preset == "liquid_glass"
    assert s["plate"]["fill"] is None and s["lighting"]["preset"] == "studio"
    full = c.get(f"/api/projects/{pid}/style?plateFill=true&shape=true").json()["plate"]
    assert full["shape"] == maps["canvas"]["shape"] and full["fill"] == maps["canvas"]["plate"]["fill"]

    with c.websocket_connect("/ws") as ws:
        ws.receive_json()  # system status
        r = c.post(f"/api/projects/{pid}/style", json={"look": "crystal"})
        assert r.status_code == 200, r.text
        end = time.time() + 5
        while time.time() < end:
            ev = ws.receive_json()
            if ev.get("type") == "project":
                assert ev == {"type": "project", "projectId": pid, "event": "saved"}
                break
        else:  # pragma: no cover
            raise AssertionError("no project saved event")
    p = r.json()
    assert c.get(f"/api/projects/{pid}").json() == p                 # saved
    assert p["updatedAt"] > maps["updatedAt"]
    assert all(l["material"]["preset"] == "clear_glass" for l in p["layers"])
    assert [l["depth"]["z"] for l in p["layers"]] == [round(i * 0.15, 5) for i in range(len(p["layers"]))]
    assert p["lighting"]["preset"] == "darkfield" and p["canvas"]["plate"]["fill"] == maps["canvas"]["plate"]["fill"]
    geo = c.get(f"/api/projects/{pid}/geometry").json()
    for l in p["layers"]:
        sr = geo["layers"][l["id"]]["safeRadius"]
        assert l["depth"]["bevel"] <= 0.9 * sr + 1e-9 and l["depth"]["bevel"] == pytest.approx(min(0.055, 0.9 * sr), abs=1e-5)

    # copy / paste: the style of Maps onto Find Device (by project id and as a pasted StyleSpec)
    r = c.post(f"/api/projects/{oid}/style", json={"fromProject": pid})
    assert r.status_code == 200
    q = r.json()
    assert all(l["material"]["preset"] == "clear_glass" for l in q["layers"]) and q["lighting"]["preset"] == "darkfield"
    pasted = c.get(f"/api/projects/{pid}/style").json()
    pasted["layerDefaults"]["material"] = {"preset": "chrome", "params": {}}
    pasted["layerMaterials"] = None
    r = c.post(f"/api/projects/{oid}/style", json={"style": pasted})
    assert r.status_code == 200 and {l["material"]["preset"] for l in r.json()["layers"]} == {"chrome"}

    # errors
    assert c.post(f"/api/projects/{pid}/style", json={}).status_code == 400
    assert c.post(f"/api/projects/{pid}/style").status_code == 400
    assert c.post(f"/api/projects/{pid}/style", json={"look": "clay", "fromProject": oid}).status_code == 400
    assert c.post(f"/api/projects/{pid}/style", json={"look": "nope"}).status_code == 404
    assert c.post(f"/api/projects/{pid}/style", json={"fromProject": "nope-123456"}).status_code == 404
    assert c.post("/api/projects/nope-123456/style", json={"look": "clay"}).status_code == 404
    assert c.get("/api/projects/nope-123456/style").status_code == 404
    assert c.post(f"/api/projects/{pid}/style", json={"style": {"zGap": "x"}}).status_code == 422


@pytest.mark.skipif(not _real_svg(), reason="bis.svg not importable")
def test_looks_and_copied_styles_keep_tiling_combined_layers(client):
    """QA round 4 #8 (Maps / Gmail): their tiled pieces form ONE 'combined' body; applying any look, or the
    copied style of an icon whose modes are art-derived, keeps it. A mode the user picked does travel."""
    c = client
    maps = c.post("/api/projects", json={"sample": "Maps"}).json()
    gmail = c.post("/api/projects", json={"sample": "Gmail"}).json()
    calc = c.post("/api/projects", json={"sample": "Calculator"}).json()
    assert [l["mode"] for l in maps["layers"]] == ["combined"] and [l["mode"] for l in gmail["layers"]] == ["combined"]
    assert {l["mode"] for l in calc["layers"]} == {"individual"}
    for p in (maps, gmail):
        for look in sorted(PRESETS["looks"]):
            r = c.post(f"/api/projects/{p['id']}/style", json={"look": look})
            assert r.status_code == 200, r.text
            assert [l["mode"] for l in r.json()["layers"]] == ["combined"], (p["name"], look)
        stored = c.get(f"/api/projects/{p['id']}")
        assert stored.status_code == 200 and [l["mode"] for l in stored.json()["layers"]] == ["combined"]
        geo = c.get(f"/api/projects/{p['id']}/geometry").json()
        for l in stored.json()["layers"]:   # the bevel was clamped against the combined body's safe radius
            assert l["depth"]["bevel"] <= 0.9 * geo["layers"][l["id"]]["safeRadius"] + 1e-9

    # copy style: Calculator's modes are its own art's -> not copied; pasting it keeps Maps combined
    s = c.get(f"/api/projects/{calc['id']}/style").json()
    assert s["layerDefaults"]["mode"] is None
    assert [l["mode"] for l in c.post(f"/api/projects/{maps['id']}/style",
                                      json={"fromProject": calc["id"]}).json()["layers"]] == ["combined"]
    assert [l["mode"] for l in c.post(f"/api/projects/{gmail['id']}/style",
                                      json={"style": s}).json()["layers"]] == ["combined"]
    # Maps' own (art-derived) combined mode is not copied onto Calculator's separate symbols either
    assert c.get(f"/api/projects/{maps['id']}/style").json()["layerDefaults"]["mode"] is None
    r = c.post(f"/api/projects/{calc['id']}/style", json={"fromProject": maps["id"]}).json()
    assert {l["mode"] for l in r["layers"]} == {"individual"}
    # Maps split into pieces by its user (or saved before round 4): 'individual' is never copied onto Gmail
    split = c.get(f"/api/projects/{maps['id']}").json()
    split["layers"][0]["mode"] = "individual"
    assert c.put(f"/api/projects/{maps['id']}", json=split).status_code == 200
    assert c.get(f"/api/projects/{maps['id']}/style").json()["layerDefaults"]["mode"] is None
    assert [l["mode"] for l in c.post(f"/api/projects/{gmail['id']}/style",
                                      json={"fromProject": maps["id"]}).json()["layers"]] == ["combined"]

    # a mode the user set on purpose (Calculator fused into combined bodies) is part of the copied look
    edited = c.get(f"/api/projects/{calc['id']}").json()
    for l in edited["layers"]:
        l["mode"] = "combined"
    assert c.put(f"/api/projects/{calc['id']}", json=edited).status_code == 200
    s2 = c.get(f"/api/projects/{calc['id']}/style").json()
    assert s2["layerDefaults"]["mode"] == "combined"
    other = c.post("/api/projects", json={"sample": "Find Device"}).json()
    r = c.post(f"/api/projects/{other['id']}/style", json={"fromProject": calc["id"]}).json()
    assert {l["mode"] for l in r["layers"]} == {"combined"}
    geo = c.get(f"/api/projects/{other['id']}/geometry").json()
    assert all(l["depth"]["bevel"] <= 0.9 * geo["layers"][l["id"]]["safeRadius"] + 1e-9 for l in r["layers"])


@pytest.mark.skipif(not _real_svg(), reason="bis.svg not importable")
def test_project_saved_with_null_layer_modes_is_repaired(client, tmp_path):
    """The round-4 server wrote ``mode: null`` when a mode-less look was applied: such a project failed to load
    (HTTP 500, and a batch failed every icon). Loading restores each layer's art-derived mode and re-saves."""
    c = client
    maps = c.post("/api/projects", json={"sample": "Maps"}).json()
    calc = c.post("/api/projects", json={"sample": "Calculator"}).json()
    store = c.app.state.store
    for p in (maps, calc):
        path = store.dir(p["id"]) / "project.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        for l in raw["layers"]:
            l["mode"] = None
        path.write_text(json.dumps(raw), encoding="utf-8")
    r = c.get(f"/api/projects/{maps['id']}")
    assert r.status_code == 200 and [l["mode"] for l in r.json()["layers"]] == ["combined"]
    assert r.json()["updatedAt"] == maps["updatedAt"]                 # a quiet repair, not an edit
    assert {l["mode"] for l in c.get(f"/api/projects/{calc['id']}").json()["layers"]} == {"individual"}
    saved = json.loads((store.dir(maps["id"]) / "project.json").read_text(encoding="utf-8"))
    assert [l["mode"] for l in saved["layers"]] == ["combined"]

@pytest.mark.skipif(not _real_svg(), reason="bis.svg not importable")
def test_null_mode_repair_never_overwrites_a_newer_save(client):
    """load() reads project.json without the project lock; its quiet re-save of repaired null modes must not
    clobber a version saved while the modes were being derived (a PUT / style apply landing in that window)."""
    c = client
    maps = c.post("/api/projects", json={"sample": "Maps"}).json()
    store = c.app.state.store
    path = store.dir(maps["id"]) / "project.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["layers"][0]["mode"] = None
    path.write_text(json.dumps(raw), encoding="utf-8")
    real_auto = store.auto_modes

    def auto_with_concurrent_put(project):            # a PUT lands while the auto modes are derived
        newer = dict(maps, name="Renamed meanwhile", updatedAt="2099-01-01T00:00:00.000Z")
        path.write_text(json.dumps(newer), encoding="utf-8")
        return real_auto(project)

    store.auto_modes = auto_with_concurrent_put
    try:
        got = store.load(maps["id"])
    finally:
        store.auto_modes = real_auto
    assert [l.mode for l in got.layers] == ["combined"]                       # the caller still gets a valid project
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["name"] == "Renamed meanwhile" and saved["updatedAt"] == "2099-01-01T00:00:00.000Z"
    # without a concurrent save the repaired copy IS written back (quietly)
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert [l.mode for l in store.load(maps["id"]).layers] == ["combined"]
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert [l["mode"] for l in saved["layers"]] == ["combined"] and saved["updatedAt"] == raw["updatedAt"]
