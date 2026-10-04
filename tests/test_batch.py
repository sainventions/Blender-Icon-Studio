"""Batch "Icon Pack" jobs (PLAN §10): FakeBridge + the real ``bis.svg`` pipeline (corpus samples).

Opt-in real-Blender test: ``BIS_REAL_BLENDER=1`` — a batch of 3 samples at draft 128 px through the persistent
worker (GPU rule: ≤ 256 px drafts).
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from bis.batch import Tile, make_contact_sheet  # noqa: E402
from bis.blender import FakeBridge  # noqa: E402
from bis.main import create_app  # noqa: E402
from bis.testing import make_test_settings  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _real_svg() -> bool:
    try:
        import bis.svg as s

        return all(hasattr(s, n) for n in ("import_svg", "build_geometry", "geometry_path"))
    except Exception:
        return False


needs_svg = pytest.mark.skipif(not _real_svg(), reason="bis.svg not importable")
SAMPLES = ["Maps", "Find Device", "Calculator"]


def wait_job(client, job_id: str, timeout: float = 120.0, states=("done", "error", "cancelled")) -> dict:
    end = time.time() + timeout
    job: dict = {}
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] in states:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach {states}: {job}")


@pytest.fixture
def ctx(tmp_path):
    bridge = FakeBridge()
    settings = make_test_settings(tmp_path, auto_preview=False)
    app = create_app(settings, bridge=bridge)
    with TestClient(app) as client:
        yield type("Ctx", (), {"client": client, "bridge": bridge, "settings": settings})


def _png(client, url: str) -> Image.Image:
    r = client.get(url)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png", url
    return Image.open(io.BytesIO(r.content))


# ---------------------------------------------------------------------------------------------- contact sheet
def test_contact_sheet_layout(tmp_path):
    imgs = []
    for i, color in enumerate([(255, 0, 0, 255), (0, 255, 0, 255)]):
        p = tmp_path / f"{i}.png"
        Image.new("RGBA", (64, 64), color).save(p)
        imgs.append(p)
    tiles = [Tile("Red", imgs[0]), Tile("A very long icon name that must be truncated", imgs[1]),
             Tile("Broken", None, "Sample not found: Broken")]
    out = make_contact_sheet(tiles, tmp_path / "sheet.png", "Icon Pack · Crystal", "3 icons")
    im = Image.open(out).convert("RGB")
    assert im.size[0] > 3 * 192 and im.size[1] > 192
    assert im.getpixel((2, 2)) == (18, 18, 22)                       # dark background
    reds = sum(1 for px in im.getdata() if px == (255, 0, 0))
    assert reds > 150 * 150                                          # the red icon fills its tile
    assert make_contact_sheet([], tmp_path / "empty.png").is_file()
    # multi-line error messages (pydantic validation errors) used to fail the whole sheet - and the batch job
    multi = [Tile("Maps", None, "1 validation error for Project\nlayers.0.mode\n  Input should be 'individual'")]
    assert make_contact_sheet(multi, tmp_path / "multi.png", "Icon Pack\nx", "1 icon\n1 failed").is_file()


# ---------------------------------------------------------------------------------------------- validation
def test_batch_validation(ctx):
    c = ctx.client
    assert c.post("/api/batch", json={"sources": []}).status_code == 400
    assert c.post("/api/batch", json={"sources": [{}]}).status_code == 400
    assert c.post("/api/batch", json={"sources": [{"sample": "Maps", "projectId": "x"}]}).status_code == 400
    assert c.post("/api/batch", json={"sources": [{"sample": "Maps"}], "look": "clay",
                                      "fromProject": "x"}).status_code == 400
    assert c.post("/api/batch", json={"sources": [{"sample": "Maps"}], "look": "nope"}).status_code == 404
    assert c.post("/api/batch", json={"sources": [{"sample": "Maps"}], "fromProject": "nope-1"}).status_code == 404
    assert c.post("/api/batch", json={"sources": [{"sample": "Maps"}],
                                      "export": {"targets": []}}).status_code == 400
    assert c.post("/api/batch", json={}).status_code == 422
    assert c.get("/api/jobs").json() == []


# ---------------------------------------------------------------------------------------------- runs
@needs_svg
def test_batch_of_samples_with_a_look(ctx):
    c = ctx.client
    r = c.post("/api/batch", json={"sources": [{"sample": s} for s in SAMPLES], "look": "candy",
                                   "size": 2048, "appearance": "light"})
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["kind"] == "batch" and job["projectId"] is None and job["request"]["look"] == "candy"
    job = wait_job(c, job["id"])
    assert job["state"] == "done", job
    res = job["result"]
    assert [it["name"] for it in res["items"]] == SAMPLES and res["failed"] == 0 and "zip" not in res
    renders = [a for mode, cmd, a in ctx.bridge.calls if cmd == "render"]
    assert len(renders) == 3 and {a["size"] for a in renders} == {512}       # draft clamped to ≤ 512 px
    assert {mode for mode, cmd, _ in ctx.bridge.calls if cmd == "render"} == {"persistent"}
    projects = {p["id"]: p for p in c.get("/api/projects").json()}
    for it, sample in zip(res["items"], SAMPLES):
        assert it["source"] == {"sample": sample} and it["projectId"] in projects
        assert "error" not in it
        assert _png(c, it["renderUrl"]).size == (512, 512)
        p = c.get(f"/api/projects/{it['projectId']}").json()
        assert p["name"] == sample
        assert {l["material"]["preset"] for l in p["layers"]} == {"candy"}
        assert p["canvas"]["plate"]["material"]["preset"] == "glossy_plastic"
        geo = c.get(f"/api/projects/{it['projectId']}/geometry").json()
        assert all(l["depth"]["bevel"] <= 0.9 * geo["layers"][l["id"]]["safeRadius"] + 1e-9 for l in p["layers"])
    sheet = _png(c, res["contactSheet"])
    assert res["contactSheet"] == f"/files/batches/{job['id']}/contact-sheet.png" and res["url"] == res["contactSheet"]
    assert sheet.size[0] >= 3 * 192 and sheet.convert("RGB").getpixel((3, 3)) == (18, 18, 22)


@needs_svg
@pytest.mark.parametrize("style", [{"look": "crystal"}, {"look": "liquid-glass"}, {"fromProject": "calc"}])
def test_batch_keeps_tiling_combined_layers(ctx, style):
    """QA round 4 #8: in an Icon Pack with a look (or another icon's copied style), Maps and Gmail - whose tiled
    pieces the pipeline builds as ONE 'combined' body - came back 'individual' (seams in the viewport). With
    the round-5 contract (looks carry no mode) the round-4 server even wrote ``mode: null`` and failed both."""
    c = ctx.client
    if "fromProject" in style:
        style = {"fromProject": c.post("/api/projects", json={"sample": "Calculator"}).json()["id"]}
    r = c.post("/api/batch", json={"sources": [{"sample": "Maps"}, {"sample": "Gmail"}], "size": 64, **style})
    assert r.status_code == 200, r.text
    job = wait_job(c, r.json()["id"])
    assert job["state"] == "done" and job["result"]["failed"] == 0, job
    renders = {a["out"]: a for _m, cmd, a in ctx.bridge.calls if cmd == "render"}
    for it in job["result"]["items"]:
        p = c.get(f"/api/projects/{it['projectId']}").json()
        assert [l["mode"] for l in p["layers"]] == ["combined"], (it["name"], style)
        sent = next(a for out, a in renders.items() if it["projectId"] in out.replace("\\", "/"))
        assert [l["mode"] for l in sent["project"]["layers"]] == ["combined"]   # what the worker was asked to build


@needs_svg
def test_per_item_errors_do_not_abort_and_existing_projects_keep_their_split(ctx):
    c = ctx.client
    existing = c.post("/api/projects", json={"sample": "Maps", "strategy": "single"}).json()
    r = c.post("/api/batch", json={"sources": [{"projectId": existing["id"]}, {"sample": "No Such Icon"},
                                               {"projectId": "gone-000000"}, {"sample": "Find Device"}],
                                   "fromProject": existing["id"], "quality": "preview", "size": 64})
    job = wait_job(c, r.json()["id"])
    assert job["state"] == "done", job
    items = job["result"]["items"]
    assert [bool(it.get("error")) for it in items] == [False, True, True, False]
    assert "Sample not found" in items[1]["error"] and "Project not found" in items[2]["error"]
    assert items[0]["projectId"] == existing["id"] and items[0]["name"] == "Maps"
    assert job["result"]["failed"] == 2 and _png(c, job["result"]["contactSheet"])
    p = c.get(f"/api/projects/{existing['id']}").json()
    assert len(p["layers"]) == len(existing["layers"]) == 1          # not re-split
    assert c.get(f"/api/projects/{items[3]['projectId']}").json()["lighting"] == p["lighting"]


@needs_svg
def test_render_failure_is_an_item_error(ctx):
    c = ctx.client
    ctx.bridge.fail["render"] = "boom (fake)"
    job = wait_job(c, c.post("/api/batch", json={"sources": [{"sample": "Maps"}, {"sample": "Calculator"}],
                                                 "size": 64}).json()["id"])
    assert job["state"] == "done" and job["result"]["failed"] == 2
    assert all("boom" in it["error"] and it.get("projectId") for it in job["result"]["items"])


def _wait_message(c, job_id: str, prefix: str, timeout: float = 60) -> dict:
    end = time.time() + timeout
    j = c.get(f"/api/jobs/{job_id}").json()
    while not j["message"].startswith(prefix) and time.time() < end:
        assert j["state"] in ("queued", "running"), j
        time.sleep(0.01)
        j = c.get(f"/api/jobs/{job_id}").json()
    assert j["message"].startswith(prefix), j["message"]
    return j


@needs_svg
def test_progress_messages_and_live_drafts_between_items(ctx):
    c = ctx.client
    ctx.bridge.delay = 0.6
    job = c.post("/api/batch", json={"sources": [{"sample": s} for s in SAMPLES], "size": 32}).json()
    j = _wait_message(c, job["id"], "Icon 2/3: Find Device")
    assert 0.25 < j["progress"] < 0.7
    # finished icons are published while the batch runs (the pack page fills its tiles)
    assert j["result"]["partial"] is True and j["result"]["count"] == 3
    assert [it["name"] for it in j["result"]["items"]] == ["Maps"] and j["result"]["items"][0]["renderUrl"]
    # a live draft of the first (finished) icon runs between two icons of the batch, not after it
    first = next(p for p in c.get("/api/projects").json() if p["name"] == "Maps")
    draft = c.post(f"/api/projects/{first['id']}/render", json={"size": 32, "live": True}).json()
    d = wait_job(c, draft["id"], timeout=60)
    assert d["state"] == "done" and c.get(f"/api/jobs/{job['id']}").json()["state"] == "running"
    j = wait_job(c, job["id"])
    assert j["state"] == "done" and d["finishedAt"] < j["finishedAt"] and j["result"]["failed"] == 0


@needs_svg
@pytest.mark.parametrize("quality", ["draft", "final"])
def test_cancel_stops_after_the_current_item(ctx, quality):
    """Persistent drafts finish the current icon; one-shot (final) renders are killed at once."""
    c = ctx.client
    ctx.bridge.delay = 1.0
    job = c.post("/api/batch", json={"sources": [{"sample": s} for s in SAMPLES], "size": 32,
                                     "quality": quality}).json()
    _wait_message(c, job["id"], "Icon 2/3: Find Device")
    while not any(cmd == "render" and a["out"].endswith(f"{job['id']}.png") and "find-device" in a["out"]
                  for _, cmd, a in list(ctx.bridge.calls)):
        time.sleep(0.01)  # the second icon's render has started
    t0 = time.time()
    assert c.delete(f"/api/jobs/{job['id']}").json()["state"] == "cancelled"
    j = wait_job(c, job["id"])
    assert j["state"] == "cancelled"
    # the icons finished before the cancel stay visible (partial result), the interrupted one is not listed
    assert j["result"]["partial"] is True and [it["name"] for it in j["result"]["items"]] == ["Maps"]
    assert j["result"]["items"][0]["renderUrl"] and "contactSheet" not in j["result"]
    end = time.time() + 10
    while c.get("/api/system").json()["queue"] != {"queued": 0, "running": 0} and time.time() < end:
        time.sleep(0.01)
    if quality == "final":
        assert time.time() - t0 < 0.9  # the one-shot render was killed, not waited for
    assert c.get("/api/system").json()["queue"] == {"queued": 0, "running": 0}
    names = {p["name"] for p in c.get("/api/projects").json()}
    assert names == {"Maps", "Find Device"}                           # the third icon never started
    assert sum(1 for _, cmd, _a in ctx.bridge.calls if cmd == "render") == 2


@needs_svg
def test_project_deleted_mid_item_is_a_failed_item_without_orphans(ctx):
    c = ctx.client
    ctx.bridge.delay = 0.8
    job = c.post("/api/batch", json={"sources": [{"sample": "Maps"}, {"sample": "Calculator"}], "size": 32}).json()
    _wait_message(c, job["id"], "Icon 2/2: Calculator")
    while not any(cmd == "render" and "calculator" in a["out"] for _, cmd, a in list(ctx.bridge.calls)):
        time.sleep(0.01)  # Calculator's render is running
    pid = next(p["id"] for p in c.get("/api/projects").json() if p["name"] == "Calculator")
    assert c.delete(f"/api/projects/{pid}").status_code in (200, 204)
    j = wait_job(c, job["id"])
    assert j["state"] == "done" and j["result"]["failed"] == 1, j
    it = j["result"]["items"][1]
    assert "deleted" in it["error"] and "renderUrl" not in it
    assert not (ctx.settings.projects_dir / pid).exists()             # no orphan renders/ folder


@needs_svg
def test_batch_with_export_packs_one_zip(ctx):
    c = ctx.client
    r = c.post("/api/batch", json={"sources": [{"sample": "Maps"}, {"sample": "Maps"}], "look": "clay", "size": 64,
                                   "export": {"targets": ["ios", "web"], "appearances": ["light"],
                                              "quality": "draft"}})
    job = wait_job(c, r.json()["id"], timeout=180)
    assert job["state"] == "done", job
    res = job["result"]
    assert res["zip"] == f"/files/batches/{job['id']}/icon-pack.zip" and res["url"] == res["zip"]
    z = c.get(res["zip"])
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    names = set(zipfile.ZipFile(io.BytesIO(z.content)).namelist())
    for folder in ("Maps", "Maps (2)"):
        assert f"Icon Pack/{folder}/ios/AppIcon.appiconset/AppIcon-1024.png" in names
        assert f"Icon Pack/{folder}/web/favicon.ico" in names
        assert not any(n.startswith(f"Icon Pack/{folder}/_masters") for n in names)
    assert "Icon Pack/contact-sheet.png" in names
    for it in res["items"]:
        assert it["exportUrl"].endswith(".zip") and c.get(it["exportUrl"]).status_code == 200
    oneshots = [a for mode, cmd, a in ctx.bridge.calls if mode == "oneshot" and cmd == "render"]
    assert oneshots and max(a["size"] for a in oneshots) <= 512           # draft export masters stay small
    assert "partial" not in res


@needs_svg
def test_same_project_twice_keeps_both_exports(ctx):
    """Each item exports into its own folder/zip: listing a project twice never overwrites its first export."""
    c = ctx.client
    pid = c.post("/api/projects", json={"sample": "Calculator"}).json()["id"]
    r = c.post("/api/batch", json={"sources": [{"projectId": pid}, {"projectId": pid}], "size": 32,
                                   "export": {"targets": ["web"], "appearances": ["light"], "quality": "draft"}})
    job = wait_job(c, r.json()["id"], timeout=180)
    assert job["state"] == "done" and job["result"]["failed"] == 0, job
    urls = [it["exportUrl"] for it in job["result"]["items"]]
    assert len(set(urls)) == 2 and all(c.get(u).status_code == 200 for u in urls)
    names = set(zipfile.ZipFile(io.BytesIO(c.get(job["result"]["zip"]).content)).namelist())
    assert {"Icon Pack/Calculator/web/favicon.ico", "Icon Pack/Calculator (2)/web/favicon.ico"} <= names


@needs_svg
def test_batch_applies_a_principled_style_and_cad_view_heroes(ctx):
    """PLAN 11: a pasted (legacy) style is cleaned to the Principled schema before it reaches any icon, per-shape
    tweaks give way to the look, shadows are physical, and the pack's marketing heroes are the CAD-style POV
    (iso 0.55 + isometric, real distances)."""
    c = ctx.client
    schema = set(json.loads((ROOT / "shared" / "presets.json").read_text(encoding="utf-8"))
                 ["materials"]["liquid_glass"]["params"])
    calc = c.get(f"/api/projects/{c.post('/api/projects', json={'sample': 'Calculator'}).json()['id']}").json()
    first = calc["layers"][0]
    first["elementMaterials"] = {first["elementIds"][0]: {"preset": "chrome", "params": {"roughness": 0.1}}}
    assert c.put(f"/api/projects/{calc['id']}", json=calc).json()["layers"][0]["elementMaterials"]
    style = {"layerDefaults": {"material": {"preset": "frosted_glass", "params": {"frost": 0.3, "glow": 1.0}},
                               "shadow": {"kind": "chromatic", "opacity": 0.5}},
             "plate": {"material": {"preset": "satin", "params": {"rim": 2.0, "coatWeight": 0.4}}},
             "camera": {"iso": 0.0, "explode": 2.0}}
    r = c.post("/api/batch", json={"sources": [{"projectId": calc["id"]}, {"sample": "Maps"}], "style": style,
                                   "size": 32, "export": {"targets": ["marketing"], "appearances": ["light"],
                                                          "quality": "draft"}})
    assert r.status_code == 200, r.text
    job = wait_job(c, r.json()["id"], timeout=180)
    assert job["state"] == "done" and job["result"]["failed"] == 0, job
    for it in job["result"]["items"]:
        p = c.get(f"/api/projects/{it['projectId']}").json()
        for l in p["layers"]:
            assert l["material"] == {"preset": "frosted_glass", "params": {"roughness": 0.3}}
            assert l["elementMaterials"] == {} and l["shadow"]["kind"] == "physical"
            assert set(l["material"]["params"]) <= schema
        assert p["canvas"]["plate"]["material"]["params"] == {"coatWeight": 0.4} and p["camera"]["explode"] == 1.0
    renders = [a for _m, cmd, a in ctx.bridge.calls if cmd == "render"]
    assert all(a["project"]["camera"]["explode"] == 1.0 for a in renders)
    heroes = sorted(a["camera"]["iso"] for a in renders if (a.get("camera") or {}).get("iso", 0) > 0)
    assert heroes == [0.55, 0.55, 1.0, 1.0]                            # hero + hero-iso for each of the 2 icons
    names = set(zipfile.ZipFile(io.BytesIO(c.get(job["result"]["zip"]).content)).namelist())
    assert {"Icon Pack/Maps/marketing/hero.png", "Icon Pack/Maps/marketing/hero-iso.png"} <= names
    assert not any("exploded" in n for n in names)


@needs_svg
def test_batch_render_from_a_cad_pov_keeps_the_head_on_thumbnail(ctx):
    """PLAN §11: the library thumbnail is the head-on icon. A pack item whose project sits in the CAD-style POV
    (camera.iso > 0 - its own, or copied with a fromProject style) still renders, but never replaces it."""
    c = ctx.client
    p = c.post("/api/projects", json={"sample": "Maps", "strategy": "single"}).json()
    thumb = ctx.settings.projects_dir / p["id"] / "thumbnail.png"
    p["camera"]["iso"] = 0.6
    assert c.put(f"/api/projects/{p['id']}", json=p).status_code == 200
    before = thumb.read_bytes()
    job = wait_job(c, c.post("/api/batch", json={"sources": [{"projectId": p["id"]}], "size": 48}).json()["id"])
    assert job["state"] == "done" and job["result"]["items"][0]["renderUrl"], job
    assert thumb.read_bytes() == before
    p["camera"]["iso"] = 0.0
    assert c.put(f"/api/projects/{p['id']}", json=p).status_code == 200
    job = wait_job(c, c.post("/api/batch", json={"sources": [{"projectId": p["id"]}], "size": 48}).json()["id"])
    assert job["state"] == "done" and thumb.read_bytes() != before


# ---------------------------------------------------------------------------------------------- real Blender
REAL = os.environ.get("BIS_REAL_BLENDER") == "1"


@pytest.mark.skipif(not REAL, reason="set BIS_REAL_BLENDER=1 to run against real Blender 5.0 (GPU)")
@needs_svg
def test_real_blender_batch_contact_sheet(tmp_path):
    settings = make_test_settings(tmp_path, blender_exe="__auto__", start_worker=True, auto_preview=False)
    if not settings.blender_found or not settings.worker_script.is_file():
        pytest.skip("Blender 5.0 / blender_worker not available")
    app = create_app(settings)
    with TestClient(app) as c:
        t0 = time.time()
        r = c.post("/api/batch", json={"sources": [{"sample": s} for s in SAMPLES], "look": "crystal",
                                       "quality": "draft", "size": 128})
        job = wait_job(c, r.json()["id"], timeout=900)
        assert job["state"] == "done", job
        res = job["result"]
        assert res["failed"] == 0, res["items"]
        for it in res["items"]:
            png = _png(c, it["renderUrl"]).convert("RGBA")
            assert png.size == (128, 128) and png.getchannel("A").getbbox() is not None
        sheet_path = settings.batches_dir / job["id"] / "contact-sheet.png"
        assert sheet_path.is_file() and sheet_path.stat().st_size > 5000
        print(f"\nreal batch: 3 icons at draft 128 px in {time.time() - t0:.1f}s; sheet {sheet_path}")
