"""API tests for the server (workstream C): FakeBridge + fake bis.svg, no Blender / GPU needed."""
from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from bis.blender import FakeBridge  # noqa: E402
from bis.main import create_app  # noqa: E402
from bis.pipeline import SvgPipeline  # noqa: E402
from bis.testing import TEST_SVG, FakeSvg, make_test_settings  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------------------------- fixtures
@pytest.fixture
def ctx(tmp_path):
    bridge = FakeBridge()
    svg = FakeSvg()
    opened: list[tuple[Path, Path]] = []
    settings = make_test_settings(tmp_path)
    app = create_app(settings, bridge=bridge, svg=svg, opener=lambda exe, p: opened.append((exe, p)))
    with TestClient(app) as client:
        yield type("Ctx", (), {"client": client, "bridge": bridge, "svg": svg, "app": app,
                               "settings": settings, "opened": opened})


def upload(client, data: bytes = TEST_SVG, name: str = "Test Icon.svg", strategy: str = "smart") -> dict:
    r = client.post("/api/projects", files={"file": (name, data, "image/svg+xml")}, data={"strategy": strategy})
    assert r.status_code == 200, r.text
    return r.json()


def wait_job(client, job_id: str, timeout: float = 15.0, states=("done", "error", "cancelled")) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] in states:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish: {job}")


def fetch_png(client, url: str) -> Image.Image:
    r = client.get(url)
    assert r.status_code == 200, url
    return Image.open(io.BytesIO(r.content))


# ---------------------------------------------------------------------------------------------- system / library
def test_system_status_shape(ctx):
    s = ctx.client.get("/api/system").json()
    assert set(s) == {"blender", "worker", "gpu", "queue"}
    assert s["worker"]["state"] in ("stopped", "starting", "ready", "busy", "error")
    assert s["queue"] == {"queued": 0, "running": 0}
    assert {"name", "device", "memoryUsedMB", "memoryTotalMB", "utilization", "temperature"} <= set(s["gpu"])


def test_presets_and_swatches(ctx, tmp_path):
    p = ctx.client.get("/api/presets").json()
    assert {"materials", "lighting", "platforms", "appearances", "quality", "colorModes"} <= set(p)
    assert "liquid_glass" in p["materials"] and "$comment" not in p
    assert p["quality"]["draft"]["size"] == 512
    # every colour mode of shared/presets.json is served verbatim (round 5: the 'brand' mode + its softClip)
    repo = json.loads((ROOT / "shared" / "presets.json").read_text(encoding="utf-8"))
    assert p["colorModes"] == repo["colorModes"]


def test_presets_serve_the_principled_schema(ctx):
    """PLAN §11: every material is ONE Principled BSDF - all presets share one grouped param schema (presets are
    only starting values), served as-is; the server validates stored params against the same schema."""
    from bis.materials import param_schema

    p = ctx.client.get("/api/presets").json()
    groups = p["principledSchema"]["groups"]
    assert groups[:2] == ["Paint", "Base"] and "Transmission" in groups and "Thin Film" in groups
    keys = {k: set(m["params"]) for k, m in p["materials"].items()}
    first = next(iter(keys.values()))
    assert len(first) == 28 and all(v == first for v in keys.values())
    assert all(spec["group"] in groups for m in p["materials"].values() for spec in m["params"].values())
    assert {"roughness", "ior", "transmission", "coatWeight", "sheenWeight", "emissionStrength",
            "thinFilmThickness", "paintMode", "tint", "grain", "filmVariation"} <= first
    assert not {"frost", "glow", "rim", "translucency", "dispersion", "absorption", "bloom"} & first   # no fakes
    assert set(param_schema(ctx.app.state.presets)) == first


def test_presets_serve_new_color_modes_live(tmp_path):
    """The server hard-codes no colour-mode list: a mode added to presets.json (e.g. round 5's brand-exact
    mode with its extra fields) reaches /api/presets without a restart."""
    root = tmp_path / "root"
    (root / "shared").mkdir(parents=True)
    path = root / "shared" / "presets.json"
    data = json.loads((ROOT / "shared" / "presets.json").read_text(encoding="utf-8"))
    path.write_text(json.dumps(data), encoding="utf-8")
    app = create_app(make_test_settings(tmp_path, root=root), bridge=FakeBridge())
    with TestClient(app) as c:
        assert c.get("/api/presets").json()["colorModes"] == data["colorModes"]
        data["colorModes"]["brand-test"] = {"label": "Brand test", "viewTransform": "Standard", "look": "None",
                                            "softClip": 0.9, "description": "x"}
        path.write_text(json.dumps(data), encoding="utf-8")
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        modes = c.get("/api/presets").json()["colorModes"]
        assert modes["brand-test"] == data["colorModes"]["brand-test"] and set(modes) == set(data["colorModes"])


def test_half_written_presets_never_break_projects(tmp_path):
    """presets.json is edited live and every project load/save cleans params against its schema (PLAN §11): a
    read that catches the file mid-write must keep the last good copy (never a 500, never wiped params)."""
    root = tmp_path / "root"
    (root / "shared").mkdir(parents=True)
    path = root / "shared" / "presets.json"
    text = (ROOT / "shared" / "presets.json").read_text(encoding="utf-8")
    path.write_text(text, encoding="utf-8")
    app = create_app(make_test_settings(tmp_path, root=root), bridge=FakeBridge())
    with TestClient(app) as c:
        p = upload(c)
        p["layers"][0]["material"] = {"preset": "frosted_glass", "params": {"roughness": 0.3, "glow": 1.0}}
        assert c.put(f"/api/projects/{p['id']}", json=p).status_code == 200
        path.write_text(text[: len(text) // 2], encoding="utf-8")                    # caught mid-write
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        r = c.get(f"/api/projects/{p['id']}")
        assert r.status_code == 200 and r.json()["layers"][0]["material"]["params"] == {"roughness": 0.3}
        assert c.put(f"/api/projects/{p['id']}", json=r.json()).status_code == 200
        assert c.get("/api/presets").json()["materials"]["frosted_glass"]["params"]["roughness"]["default"] == 0.267
        path.write_text(text, encoding="utf-8")                                         # the write completes
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 9_000_000_000))
        assert "frosted_glass" in c.get("/api/presets").json()["materials"]


def test_samples_and_thumbnail(ctx):
    samples = ctx.client.get("/api/samples").json()
    names = {s["name"] for s in samples}
    assert "Maps" in names and "Find Device" in names and "a_app_multilayer" in names
    maps = next(s for s in samples if s["name"] == "Maps")
    assert maps["file"] == "Maps.svg" and maps["url"].endswith("/source.svg")
    assert ctx.client.get(maps["url"]).text.lstrip().startswith("<?xml")
    fd = next(s for s in samples if s["name"] == "Find Device")
    r = ctx.client.get(fd["thumbnail"] + "?size=64")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(r.content)).size == (64, 64)
    assert ctx.client.get("/api/samples/Nope/thumbnail.png").status_code == 404


# ---------------------------------------------------------------------------------------------- projects
def test_project_crud_cycle(ctx):
    c = ctx.client
    p = upload(c)
    assert p["name"] == "Test Icon" and p["source"]["plateDetected"] is True
    assert len(p["layers"]) == 2 and p["canvas"]["plate"]["fill"]["color"] == "#2563eb"
    pid = p["id"]
    assert pid.startswith("test-icon-")

    summaries = c.get("/api/projects").json()
    assert summaries[0]["id"] == pid and summaries[0]["layerCount"] == 2
    assert summaries[0]["thumbnail"].startswith(f"/files/projects/{pid}/thumbnail.png")
    assert c.get(summaries[0]["thumbnail"]).status_code == 200
    assert c.get(f"/api/projects/{pid}/source.svg").content == TEST_SVG

    # PUT = full replace; id forced, updatedAt refreshed
    p["name"] = "Renamed"
    p["id"] = "something-else"
    p["layers"][0]["material"] = {"preset": "chrome", "params": {"roughness": 0.1}}
    before = p["updatedAt"]
    time.sleep(0.01)
    r = c.put(f"/api/projects/{pid}", json=p)
    assert r.status_code == 200, r.text
    q = c.get(f"/api/projects/{pid}").json()
    assert q["id"] == pid and q["name"] == "Renamed" and q["updatedAt"] > before
    assert q["layers"][0]["material"]["preset"] == "chrome"
    assert c.put(f"/api/projects/{pid}", json={"bad": True}).status_code == 422

    dup = c.post(f"/api/projects/{pid}/duplicate").json()
    assert dup["id"] != pid and dup["name"] == "Renamed copy" and len(dup["layers"]) == 2
    assert {s["id"] for s in c.get("/api/projects").json()} == {pid, dup["id"]}

    assert c.delete(f"/api/projects/{pid}").json()["ok"] is True
    assert c.get(f"/api/projects/{pid}").status_code == 404
    assert c.delete(f"/api/projects/{pid}").status_code == 404
    assert not (ctx.settings.projects_dir / pid).exists()


def test_create_from_sample_and_errors(ctx):
    c = ctx.client
    p = c.post("/api/projects", json={"sample": "Maps", "strategy": "element"}).json()
    assert p["name"] == "Maps" and p["strategy"] == "element"
    assert c.post("/api/projects", json={"sample": "does-not-exist"}).status_code == 404
    assert c.post("/api/projects", json={"sample": "Maps", "strategy": "bogus"}).status_code == 422
    r = c.post("/api/projects", files={"file": ("x.svg", b"hello world", "image/svg+xml")})
    assert r.status_code == 400
    assert c.get("/api/projects/..%5C..%5Cetc").status_code == 404
    assert c.get("/api/projects/..%2F..%2Fetc").status_code == 404
    assert c.get("/api/nope").status_code == 404


def test_svg_pipeline_unavailable_is_503(tmp_path):
    app = create_app(make_test_settings(tmp_path), bridge=FakeBridge(),
                     svg=SvgPipeline(module_name="bis_no_such_pipeline"))
    with TestClient(app) as c:
        r = c.post("/api/projects", files={"file": ("x.svg", TEST_SVG, "image/svg+xml")})
        assert r.status_code == 503 and "not available" in r.json()["detail"]
        assert c.get("/api/system").status_code == 200  # the rest of the app keeps working
        assert c.get("/api/samples/Maps/thumbnail.png").status_code == 200  # resvg fallback


def test_layer_operations(ctx):
    c = ctx.client
    p = upload(c, strategy="element")
    pid = p["id"]
    assert [l["elementIds"] for l in p["layers"]] == [["e1"], ["e2"], ["e3"]]
    # appearance override on a layer that will disappear is pruned
    p["appearances"]["dark"]["layers"] = {"l1": {"opacity": 0.5}, "l0": {"visible": False}}
    c.put(f"/api/projects/{pid}", json=p)

    m = c.post(f"/api/projects/{pid}/layers/merge", json={"layerIds": ["l0", "l1"]}).json()
    assert [l["elementIds"] for l in m["layers"]] == [["e1", "e2"], ["e3"]]
    assert set(m["appearances"]["dark"]["layers"]) == {"l0"}
    assert c.post(f"/api/projects/{pid}/layers/merge", json={"layerIds": ["l0"]}).status_code == 400
    assert c.post(f"/api/projects/{pid}/layers/merge", json={"layerIds": ["l0", "zz"]}).status_code == 400

    s = c.post(f"/api/projects/{pid}/layers/l0/split", json={"mode": "elements"}).json()
    assert [l["elementIds"] for l in s["layers"]] == [["e1"], ["e2"], ["e3"]]
    assert c.post(f"/api/projects/{pid}/layers/nope/split", json={}).status_code == 400

    # move into an existing layer (keeps source paint order), empty layer removed
    ids = [l["id"] for l in s["layers"]]
    mv = c.post(f"/api/projects/{pid}/elements/move", json={"elementIds": ["e3"], "toLayerId": ids[0]}).json()
    assert [l["elementIds"] for l in mv["layers"]] == [["e1", "e3"], ["e2"]]
    # move into a brand-new layer: inserted above the top-most source layer, z between neighbours
    nw = c.post(f"/api/projects/{pid}/elements/move", json={"elementIds": ["e1"], "toLayerId": None}).json()
    assert [l["elementIds"] for l in nw["layers"]] == [["e3"], ["e1"], ["e2"]]
    zs = [l["depth"]["z"] for l in nw["layers"]]
    assert zs[0] < zs[1] < zs[2]
    assert nw["layers"][1]["name"] == "ring"
    assert c.post(f"/api/projects/{pid}/elements/move", json={"elementIds": ["e99"]}).status_code == 400

    resplit = c.post(f"/api/projects/{pid}/split", json={"strategy": "single"}).json()
    assert resplit["strategy"] == "single" and len(resplit["layers"]) == 1


def test_geometry_and_layer_thumbnail(ctx):
    c = ctx.client
    p = upload(c)
    g = c.get(f"/api/projects/{p['id']}/geometry").json()
    assert g["projectId"] == p["id"] and set(g["layers"]) == {"l0", "l1"}
    tex = g["layers"]["l0"]["texture"]
    assert tex.startswith(f"/files/projects/{p['id']}/cache/") and c.get(tex).status_code == 200
    r = c.get(f"/api/projects/{p['id']}/layers/l0/thumbnail.png?size=48")
    assert r.status_code == 200 and Image.open(io.BytesIO(r.content)).size == (48, 48)
    assert c.get(f"/api/projects/{p['id']}/layers/zz/thumbnail.png").status_code == 400


# ---------------------------------------------------------------------------------------------- render jobs
def test_draft_render_flow(ctx):
    c = ctx.client
    p = upload(c)
    pid = p["id"]
    thumb_before = (ctx.settings.projects_dir / pid / "thumbnail.png").read_bytes()
    job = c.post(f"/api/projects/{pid}/render", json={"quality": "draft", "size": 128}).json()
    assert job["kind"] == "render" and job["state"] in ("queued", "running") and job["projectId"] == pid
    done = wait_job(c, job["id"])
    assert done["state"] == "done", done
    res = done["result"]
    assert res["url"] == f"/files/projects/{pid}/renders/{job['id']}.png"
    assert (res["width"], res["height"]) == (128, 128) and res["engine"] and res["device"] == "OPTIX"
    assert res["appearance"] == "light" and res["quality"] == "draft"
    img = fetch_png(c, res["url"])
    assert img.size == (128, 128) and img.getpixel((64, 64))[3] == 255 and img.getpixel((0, 0))[3] == 0
    mode, cmd, args = ctx.bridge.calls[-1]
    assert (mode, cmd) == ("persistent", "render")
    assert args["geometryPath"].endswith(".json") and Path(args["geometryPath"]).is_file()
    assert args["project"]["id"] == pid and args["out"].endswith(f"{job['id']}.png")
    # latest draft becomes the library thumbnail
    assert (ctx.settings.projects_dir / pid / "thumbnail.png").read_bytes() != thumb_before
    assert any(j["id"] == job["id"] for j in c.get(f"/api/jobs?projectId={pid}").json())


def test_quality_routing_and_size_rules(ctx):
    c = ctx.client
    pid = upload(c)["id"]
    j1 = wait_job(c, c.post(f"/api/projects/{pid}/render", json={"quality": "preview", "size": 4096}).json()["id"])
    assert j1["result"]["width"] == 512  # interactive tiers clamp to 512 px (GPU rule)
    j2 = wait_job(c, c.post(f"/api/projects/{pid}/render",
                            json={"quality": "final", "size": 200, "fullBleed": True}).json()["id"])
    assert j2["state"] == "done" and j2["result"]["width"] == 200
    modes = [(m, a["quality"]) for m, cmd, a in ctx.bridge.calls if cmd == "render"]
    assert modes == [("persistent", "preview"), ("oneshot", "final")]
    full = fetch_png(c, j2["result"]["url"])
    assert full.getpixel((0, 0))[3] == 255  # full-bleed: opaque corners


def test_live_coalescing_and_cancel(ctx):
    c = ctx.client
    pid = upload(c)["id"]
    ctx.bridge.delay = 0.3
    blocker = c.post(f"/api/projects/{pid}/render", json={"quality": "final", "size": 64}).json()
    lives = [c.post(f"/api/projects/{pid}/render", json={"quality": "draft", "size": 64, "live": True}).json()
             for _ in range(4)]
    other = c.post(f"/api/projects/{pid}/render", json={"quality": "draft", "size": 64}).json()
    to_cancel = c.post(f"/api/projects/{pid}/render", json={"quality": "draft", "size": 64}).json()
    cancelled = c.delete(f"/api/jobs/{to_cancel['id']}").json()
    assert cancelled["state"] == "cancelled"
    finals = [wait_job(c, j["id"]) for j in lives]
    assert [j["state"] for j in finals[:-1]] == ["cancelled"] * 3
    assert all(j["message"].startswith("Superseded") for j in finals[:-1])
    assert finals[-1]["state"] == "done"
    assert wait_job(c, other["id"])["state"] == "done"
    assert wait_job(c, blocker["id"])["state"] == "done"
    # the live draft jumped ahead of the earlier-queued normal draft
    assert finals[-1]["startedAt"] < wait_job(c, other["id"])["startedAt"]
    assert c.delete("/api/jobs/doesnotexist").status_code == 404


def test_cancel_running_oneshot_kills(ctx):
    c = ctx.client
    pid = upload(c)["id"]
    ctx.bridge.delay = 5.0
    job = c.post(f"/api/projects/{pid}/render", json={"quality": "final", "size": 64}).json()
    wait_job(c, job["id"], states=("running",))
    t0 = time.time()
    assert c.delete(f"/api/jobs/{job['id']}").json()["state"] == "cancelled"
    ctx.bridge.delay = 0
    nxt = c.post(f"/api/projects/{pid}/render", json={"quality": "draft", "size": 32}).json()
    assert wait_job(c, nxt["id"])["state"] == "done"
    assert time.time() - t0 < 3.0  # the one-shot stopped right away (no 5 s wait)
    assert c.get(f"/api/jobs/{job['id']}").json()["state"] == "cancelled"


def test_render_error_becomes_job_error(ctx):
    c = ctx.client
    pid = upload(c)["id"]
    ctx.bridge.fail["render"] = "OptiX out of memory"
    job = wait_job(c, c.post(f"/api/projects/{pid}/render", json={"size": 32}).json()["id"])
    assert job["state"] == "error" and "OptiX out of memory" in job["error"]
    assert job["result"]["traceback"]


def test_auto_preview_after_live_draft(ctx):
    c = ctx.client
    pid = upload(c)["id"]
    d = wait_job(c, c.post(f"/api/projects/{pid}/render",
                           json={"quality": "draft", "size": 64, "live": True, "appearance": "dark"}).json()["id"])
    assert d["state"] == "done"
    end = time.time() + 5
    auto = None
    while time.time() < end and auto is None:
        auto = next((j for j in c.get(f"/api/jobs?projectId={pid}").json() if j["request"].get("auto")), None)
        time.sleep(0.02)
    assert auto is not None, "auto preview was not queued"
    auto = wait_job(c, auto["id"])
    assert auto["state"] == "done" and auto["request"]["quality"] == "preview"
    assert auto["request"]["live"] is True and auto["result"]["appearance"] == "dark"

    # disabled per project
    p = c.get(f"/api/projects/{pid}").json()
    p["render"]["autoPreview"] = False
    c.put(f"/api/projects/{pid}", json=p)
    n_before = len(c.get(f"/api/jobs?projectId={pid}").json())
    wait_job(c, c.post(f"/api/projects/{pid}/render", json={"size": 64, "live": True}).json()["id"])
    time.sleep(0.3)
    assert len(c.get(f"/api/jobs?projectId={pid}").json()) == n_before + 1


def test_new_edit_cancels_queued_auto_preview(ctx):
    c = ctx.client
    pid = upload(c)["id"]
    ctx.bridge.delay = 0.4
    d1 = c.post(f"/api/projects/{pid}/render", json={"size": 32, "live": True}).json()
    blocker = c.post(f"/api/projects/{pid}/render", json={"quality": "final", "size": 32}).json()
    wait_job(c, d1["id"])
    wait_job(c, blocker["id"], states=("running",))
    end = time.time() + 5
    auto = None
    while auto is None and time.time() < end:  # auto preview queued behind the running final
        auto = next((j for j in c.get(f"/api/jobs?projectId={pid}").json() if j["request"].get("auto")), None)
        time.sleep(0.01)
    assert auto is not None and auto["state"] == "queued"
    d2 = c.post(f"/api/projects/{pid}/render", json={"size": 32, "live": True}).json()
    assert c.get(f"/api/jobs/{auto['id']}").json()["state"] == "cancelled"
    ctx.bridge.delay = 0
    assert wait_job(c, d2["id"])["state"] == "done"


def test_renditions(ctx):
    c = ctx.client
    pid = upload(c)["id"]
    jobs = c.post(f"/api/projects/{pid}/renditions", json={"quality": "draft"}).json()
    assert [j["request"]["appearance"] for j in jobs] == [
        "light", "dark", "clear-light", "clear-dark", "tinted-light", "tinted-dark"]
    done = [wait_job(c, j["id"]) for j in jobs]
    assert all(j["state"] == "done" and j["result"]["width"] == 256 for j in done)
    # watchOS has no appearance variants
    p = c.get(f"/api/projects/{pid}").json()
    p["canvas"]["platform"] = "watchos"
    c.put(f"/api/projects/{pid}", json=p)
    assert [j["request"]["appearance"] for j in c.post(f"/api/projects/{pid}/renditions", json={}).json()] == ["light"]


def test_renditions_are_head_on_whatever_the_cad_pov(ctx):
    """The six appearance renditions (+ size waterfall) preview the exported icon, so - like every platform master -
    they are head-on even while the project sits in the CAD-style POV (camera.iso); an explicit camera still wins."""
    c = ctx.client
    p = upload(c)
    p["camera"].update(iso=0.6, zoom=1.3)
    assert c.put(f"/api/projects/{p['id']}", json=p).status_code == 200
    jobs = c.post(f"/api/projects/{p['id']}/renditions", json={"appearances": ["light", "dark"]}).json()
    assert all(wait_job(c, j["id"])["state"] == "done" for j in jobs)
    sent = [a for _m, cmd, a in ctx.bridge.calls if cmd == "render"][-2:]
    assert all(a["camera"]["iso"] == 0.0 and a["camera"]["zoom"] == 1.3 for a in sent), sent
    assert all(a["project"]["camera"]["iso"] == 0.6 for a in sent)
    jobs = c.post(f"/api/projects/{p['id']}/renditions",
                  json={"appearances": ["light"], "camera": {"iso": 0.5}}).json()
    assert wait_job(c, jobs[0]["id"])["state"] == "done" and ctx.bridge.calls[-1][2]["camera"]["iso"] == 0.5


@pytest.mark.parametrize("fmt", ["gif", "webp", "png", "mp4"])
def test_animate(ctx, fmt):
    c = ctx.client
    pid = upload(c)["id"]
    job = c.post(f"/api/projects/{pid}/animate",
                 json={"kind": "turntable", "frames": 6, "fps": 12, "size": 64, "format": fmt}).json()
    job = wait_job(c, job["id"])
    assert job["state"] == "done", job
    res = job["result"]
    assert res["frameCount"] == 6 and len(res["frames"]) == 6 and res["width"] == 64
    assert c.get(res["url"]).status_code == 200
    mode, cmd, args = ctx.bridge.calls[-1]
    assert (mode, cmd) == ("oneshot", "animate") and args["format"] == ("mp4" if fmt == "mp4" else "png")
    if fmt in ("gif", "webp"):
        im = Image.open(io.BytesIO(c.get(res["url"]).content))
        assert im.n_frames == 6
    if fmt == "mp4":
        assert res["video"] == res["url"]


def test_blend_and_open(tmp_path):
    bridge, opened = FakeBridge(), []
    import sys as _sys

    settings = make_test_settings(tmp_path, blender_exe=_sys.executable)  # any existing file
    app = create_app(settings, bridge=bridge, svg=FakeSvg(), opener=lambda exe, p: opened.append((exe, p)))
    with TestClient(app) as c:
        pid = upload(c)["id"]
        job = wait_job(c, c.post(f"/api/projects/{pid}/blend", json={"open": True}).json()["id"])
        assert job["state"] == "done" and job["result"]["opened"] is True
        assert job["result"]["url"] == f"/files/projects/{pid}/blend/Test%20Icon.blend"
        assert opened and opened[0][1].name == "Test Icon.blend" and opened[0][1].is_file()
        assert bridge.calls[-1][:2] == ("oneshot", "save_blend") and bridge.calls[-1][2]["pack"] is True


def test_swatches_job(ctx):
    job = wait_job(ctx.client, ctx.client.post("/api/system/swatches", json={"size": 64}).json()["id"])
    assert job["state"] == "done" and job["kind"] == "swatches"
    assert ctx.bridge.calls[-1][:2] == ("oneshot", "swatches")


def test_worker_restart_endpoint(ctx):
    r = ctx.client.post("/api/system/worker/restart")
    assert r.status_code == 200
    end = time.time() + 2
    while ctx.bridge.restarts == 0 and time.time() < end:
        time.sleep(0.01)
    assert ctx.bridge.restarts == 1
    assert ctx.client.get("/api/system").json()["worker"]["state"] == "ready"


def test_websocket_stream(ctx):
    c = ctx.client
    pid = upload(c)["id"]
    with c.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "system" and "worker" in first["status"]
        job = c.post(f"/api/projects/{pid}/render", json={"size": 32}).json()
        seen: dict[str, str] = {}
        end = time.time() + 10
        while time.time() < end:
            ev = ws.receive_json()
            if ev["type"] == "job" and ev["job"]["id"] == job["id"]:
                seen[ev["job"]["state"]] = ev["job"]["message"]
                if ev["job"]["state"] == "done":
                    assert ev["job"]["result"]["url"].endswith(".png")
                    break
        assert {"queued", "running", "done"} <= set(seen)
        ws.send_text("ping")
        # project events are published too
        c.delete(f"/api/projects/{pid}")
        end = time.time() + 5
        while time.time() < end:
            msg = ws.receive()
            if msg.get("text") == "pong":
                continue
            ev = json.loads(msg["text"])
            if ev["type"] == "project":
                assert ev == {"type": "project", "projectId": pid, "event": "deleted"}
                break


def test_spa_fallback(ctx):
    r = ctx.client.get("/some/client/route")
    dist = ctx.settings.web_dist / "index.html"
    if dist.is_file():
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    else:
        assert r.status_code == 404
    assert ctx.client.get("/files/projects/nope/x.png").status_code == 404


# ---------------------------------------------------------------------------------------------- review fixes
def test_full_bleed_render_pins_camera_zoom(ctx):
    """fullBleed = plate exactly fills the frame (ortho 2.0) head-on; neither the project's camera zoom nor its
    CAD-style POV (camera.iso) may change that."""
    c = ctx.client
    p = upload(c)
    p["camera"]["zoom"] = 2.0
    p["camera"]["iso"] = 0.6
    p["camera"]["fov"] = 40.0
    c.put(f"/api/projects/{p['id']}", json=p)
    job = c.post(f"/api/projects/{p['id']}/render", json={"quality": "final", "size": 32, "fullBleed": True}).json()
    assert wait_job(c, job["id"])["state"] == "done"
    args = ctx.bridge.calls[-1][2]
    assert args["fullBleed"] is True and args["camera"]["zoom"] == 1.0 and args["camera"]["view"] == "front"
    assert args["camera"]["iso"] == 0.0 and args["camera"]["fov"] == 40.0  # the rest follows the project camera
    wait_job(c, c.post(f"/api/projects/{p['id']}/render", json={"size": 32}).json()["id"])
    sent = ctx.bridge.calls[-1][2]
    assert "camera" not in sent and sent["project"]["camera"]["iso"] == 0.6  # the project's own POV as is


def test_cad_view_renders_keep_the_head_on_thumbnail(ctx):
    """A render from an explicit CAD-style POV (iso > 0) is not the icon: it never replaces the library thumbnail."""
    c = ctx.client
    pid = upload(c)["id"]
    thumb = ctx.settings.projects_dir / pid / "thumbnail.png"
    before = thumb.read_bytes()
    j = wait_job(c, c.post(f"/api/projects/{pid}/render", json={"size": 48, "camera": {"iso": 0.55}}).json()["id"])
    assert j["state"] == "done" and ctx.bridge.calls[-1][2]["camera"]["iso"] == 0.55
    assert thumb.read_bytes() == before
    j = wait_job(c, c.post(f"/api/projects/{pid}/render", json={"size": 48, "camera": {"iso": 0.0}}).json()["id"])
    assert j["state"] == "done" and thumb.read_bytes() != before
    # an explicit camera follows the project camera's rules: iso within 0..1, layers never spread
    j = wait_job(c, c.post(f"/api/projects/{pid}/render",
                           json={"size": 32, "camera": {"iso": 7.0, "explode": 3.0}}).json()["id"])
    assert j["state"] == "done" and ctx.bridge.calls[-1][2]["camera"]["iso"] == 1.0
    assert ctx.bridge.calls[-1][2]["camera"]["explode"] == 1.0
    # the project's own POV (the iso slider: live renders send no camera) is not the head-on icon either
    p = c.get(f"/api/projects/{pid}").json()
    p["camera"]["iso"] = 0.4
    assert c.put(f"/api/projects/{pid}", json=p).status_code == 200
    thumb.unlink()
    j = wait_job(c, c.post(f"/api/projects/{pid}/render", json={"size": 48, "live": True}).json()["id"])
    assert j["state"] == "done" and ctx.bridge.calls[-1][2]["project"]["camera"]["iso"] == 0.4
    assert not thumb.exists()
    j = wait_job(c, c.post(f"/api/projects/{pid}/render", json={"size": 48, "camera": {"iso": 0.0}}).json()["id"])
    assert j["state"] == "done" and thumb.exists()           # an explicit head-on camera is the icon again


def test_legacy_explode_animation_is_the_iso_pov_move(ctx):
    """PLAN §11: 'explode' no longer spreads layers - the legacy kind runs the CAD-style head-on -> iso -> head-on
    move (kind 'iso'), with the real layer distances (camera.explode stays 1)."""
    c = ctx.client
    pid = upload(c)["id"]
    for kind in ("explode", "iso"):
        job = wait_job(c, c.post(f"/api/projects/{pid}/animate",
                                 json={"kind": kind, "frames": 4, "size": 32, "format": "png"}).json()["id"])
        assert job["state"] == "done" and job["result"]["kind"] == "iso" and job["request"]["kind"] == "iso"
        mode, cmd, args = ctx.bridge.calls[-1]
        assert (mode, cmd, args["kind"]) == ("oneshot", "animate", "iso")
        assert args["project"]["camera"]["explode"] == 1.0


# ---------------------------------------------------------------------------------------------- PLAN 11 contract
def _write_raw(ctx, pid: str, raw: dict) -> Path:
    path = ctx.settings.projects_dir / pid / "project.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_legacy_project_loads_on_the_principled_contract(ctx):
    """A project saved by the round <= 5 server (fake material params, art-directed shadows, explode spreading,
    stale per-shape overrides) loads cleaned; a PUT of such a document is stored cleaned."""
    c = ctx.client
    p = upload(c)
    pid = p["id"]
    raw = json.loads(json.dumps(p))
    l0, l1 = raw["layers"]
    e0 = l0["elementIds"][0]
    l0["material"] = {"preset": "frosted_glass", "params": {
        "frost": 0.2, "glow": 0.5, "rim": 2.0, "translucency": 0.7, "specular": "auto", "tint": 0.4,
        "ior": 9.0, "paintMode": "bogus", "coatWeight": True, "__intent": "clear"}}
    l0["shadow"] = {"kind": "neutral", "opacity": 0.4}
    l0["elementMaterials"] = {e0: {"preset": "chrome", "params": {"anisotropy": 0.8, "brush": "radial"}},
                              "e99": {"preset": "neon", "params": {}}}
    l1["material"] = {"preset": "neon", "params": {"strength": 6.0, "bloom": 0.6, "core": 0.3, "roughness": 0.3}}
    l1["shadow"] = {"kind": "chromatic", "opacity": 0.5}
    raw["camera"]["explode"] = 2.5
    raw["camera"]["iso"] = 1.7
    raw["canvas"]["plate"]["material"] = {"preset": "satin", "params": {"sheen": 0.3, "scatter": 4}}
    raw["appearances"]["dark"]["layers"] = {"l0": {"material": {"preset": "iridescent",
                                                                "params": {"film": 400, "filmIor": 1.4, "bands": 3}}}}
    path = _write_raw(ctx, pid, raw)

    q = c.get(f"/api/projects/{pid}").json()
    a, b = q["layers"]
    assert a["material"] == {"preset": "frosted_glass", "params": {"tint": 0.4, "ior": 3.0, "roughness": 0.2}}
    assert a["elementMaterials"] == {e0: {"preset": "chrome", "params": {"anisotropic": 0.8}}}
    assert b["material"]["params"] == {"roughness": 0.3, "emissionStrength": 6.0}   # an explicit value wins
    assert a["shadow"] == {"kind": "physical", "opacity": 0.4} and b["shadow"]["kind"] == "physical"
    assert q["camera"]["explode"] == 1.0 and q["camera"]["iso"] == 1.0
    assert q["canvas"]["plate"]["material"]["params"] == {"sheenWeight": 0.3}
    assert q["appearances"]["dark"]["layers"]["l0"]["material"]["params"] == {"thinFilmThickness": 400,
                                                                              "thinFilmIor": 1.4}
    assert q["updatedAt"] == p["updatedAt"]                                          # reading is not an edit
    assert json.loads(path.read_text(encoding="utf-8"))["layers"][0]["shadow"]["kind"] == "neutral"

    r = c.put(f"/api/projects/{pid}", json=raw)                                      # a stale client's legacy PUT
    assert r.status_code == 200 and r.json()["layers"] == q["layers"]
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["layers"] == q["layers"] and stored["camera"]["explode"] == 1.0
    # the copied style of the legacy project carries only Principled params
    style = c.get(f"/api/projects/{pid}/style").json()
    assert style["layerMaterials"][0]["params"] == {"tint": 0.4, "ior": 3.0, "roughness": 0.2}


def test_element_materials_follow_their_elements(ctx):
    """Per-shape materials (Layer.elementMaterials, element id -> MaterialSpec) stay on their shape through merge,
    split, move and re-split; overrides of elements a layer no longer holds are dropped."""
    c = ctx.client
    p = upload(c, strategy="element")
    pid = p["id"]
    assert [l["elementIds"] for l in p["layers"]] == [["e1"], ["e2"], ["e3"]]
    chrome = {"preset": "chrome", "params": {"roughness": 0.1}}
    candy = {"preset": "candy", "params": {"subsurfaceWeight": 0.5}}
    p["layers"][0]["elementMaterials"] = {"e1": chrome}
    p["layers"][1]["elementMaterials"] = {"e2": candy, "e3": chrome}       # e3 is not in this layer -> dropped
    q = c.put(f"/api/projects/{pid}", json=p).json()
    assert [l["elementMaterials"] for l in q["layers"]] == [{"e1": chrome}, {"e2": candy}, {}]

    m = c.post(f"/api/projects/{pid}/layers/merge", json={"layerIds": ["l0", "l1"]}).json()
    assert [l["elementMaterials"] for l in m["layers"]] == [{"e1": chrome, "e2": candy}, {}]
    s = c.post(f"/api/projects/{pid}/layers/l0/split", json={"mode": "elements"}).json()
    assert [l["elementMaterials"] for l in s["layers"]] == [{"e1": chrome}, {"e2": candy}, {}]
    mv = c.post(f"/api/projects/{pid}/elements/move", json={"elementIds": ["e2"], "toLayerId": None}).json()
    holder = next(l for l in mv["layers"] if l["elementIds"] == ["e2"])
    assert holder["elementMaterials"] == {"e2": candy}
    assert all("e2" not in l["elementMaterials"] for l in mv["layers"] if l is not holder)
    one = c.post(f"/api/projects/{pid}/split", json={"strategy": "single"}).json()
    assert one["layers"][0]["elementMaterials"] == {"e1": chrome, "e2": candy}
    assert c.get(f"/api/projects/{pid}").json()["layers"] == one["layers"]


def test_export_requires_a_target(ctx):
    pid = upload(ctx.client)["id"]
    r = ctx.client.post(f"/api/projects/{pid}/export", json={"targets": []})
    assert r.status_code == 422 and "target" in r.json()["detail"]


def test_live_drafts_run_between_export_masters(ctx):
    """A long export yields the GPU queue to live drafts between its master renders."""
    c = ctx.client
    pid = upload(c)["id"]
    ctx.bridge.delay = 0.25
    export = c.post(f"/api/projects/{pid}/export",
                    json={"targets": ["ios", "macos", "android"], "quality": "draft"}).json()
    wait_job(c, export["id"], states=("running",))
    draft = c.post(f"/api/projects/{pid}/render", json={"size": 32, "live": True}).json()
    d = wait_job(c, draft["id"])
    e = c.get(f"/api/jobs/{export['id']}").json()
    assert d["state"] == "done" and e["state"] == "running", (d["state"], e["state"])
    e = wait_job(c, export["id"], timeout=30)
    assert e["state"] == "done" and d["finishedAt"] < e["finishedAt"]
    # the draft ran inside the export, not after it; the queue counters are consistent afterwards
    end = time.time() + 5
    while c.get("/api/system").json()["queue"] != {"queued": 0, "running": 0} and time.time() < end:
        time.sleep(0.02)  # a follow-up auto preview may still be queued/running
    assert c.get("/api/system").json()["queue"] == {"queued": 0, "running": 0}


def test_cancel_export_while_live_job_runs_inside_it(ctx):
    c = ctx.client
    pid = upload(c)["id"]
    ctx.bridge.delay = 0.3
    export = c.post(f"/api/projects/{pid}/export", json={"targets": ["ios", "macos"], "quality": "draft"}).json()
    wait_job(c, export["id"], states=("running",))
    draft = c.post(f"/api/projects/{pid}/render", json={"size": 32, "live": True}).json()
    wait_job(c, draft["id"], states=("running", "done"))
    assert c.delete(f"/api/jobs/{export['id']}").json()["state"] == "cancelled"
    assert wait_job(c, draft["id"])["state"] == "done"
    assert wait_job(c, export["id"])["state"] == "cancelled"
    ctx.bridge.delay = 0
    assert wait_job(c, c.post(f"/api/projects/{pid}/render", json={"size": 16}).json()["id"])["state"] == "done"


def test_delete_project_during_persistent_render_leaves_nothing_behind(ctx):
    c = ctx.client
    pid = upload(c)["id"]
    ctx.bridge.delay = 0.6
    job = c.post(f"/api/projects/{pid}/render", json={"size": 32}).json()
    wait_job(c, job["id"], states=("running",))
    assert c.delete(f"/api/projects/{pid}").status_code == 200
    folder = ctx.settings.projects_dir / pid
    end = time.time() + 10
    while folder.exists() and time.time() < end:  # the worker finishes writing, then the folder is purged
        time.sleep(0.05)
    assert not folder.exists()
    assert c.get(f"/api/jobs/{job['id']}").json()["state"] == "cancelled"
    assert pid not in {s["id"] for s in c.get("/api/projects").json()}


def test_blend_open_failure_keeps_the_saved_file(tmp_path):
    def boom(exe, path):
        raise OSError("no display")

    settings = make_test_settings(tmp_path, blender_exe=sys.executable)
    app = create_app(settings, bridge=FakeBridge(), svg=FakeSvg(), opener=boom)
    with TestClient(app) as c:
        pid = upload(c)["id"]
        job = wait_job(c, c.post(f"/api/projects/{pid}/blend", json={"open": True}).json()["id"])
        assert job["state"] == "done" and job["result"]["opened"] is False
        assert "no display" in job["result"]["openError"] and Path(job["result"]["path"]).is_file()


def test_missing_blender_fails_jobs_cleanly(tmp_path):
    """Real BlenderBridge without blender.exe: the app runs, renders fail with a clear job error."""
    settings = make_test_settings(tmp_path, blender_exe=None, start_worker=True)
    app = create_app(settings, svg=FakeSvg())
    with TestClient(app) as c:
        s = c.get("/api/system").json()
        assert s["blender"]["found"] is False and s["worker"]["state"] in ("stopped", "error")
        pid = upload(c)["id"]
        job = wait_job(c, c.post(f"/api/projects/{pid}/render", json={"size": 32}).json()["id"])
        assert job["state"] == "error" and "Blender not found" in job["error"]
        job = wait_job(c, c.post(f"/api/projects/{pid}/render", json={"quality": "final", "size": 32}).json()["id"])
        assert job["state"] == "error" and "Blender not found" in job["error"]
        assert c.post("/api/system/worker/restart").status_code == 503
        assert c.get("/api/health").json()["ok"] is True


def test_static_mime_types_do_not_depend_on_the_registry(ctx):
    import mimetypes

    assert mimetypes.guess_type("index-abc.js")[0] == "text/javascript"
    assert mimetypes.guess_type("font.woff2")[0] == "font/woff2"
    assert mimetypes.guess_type("export.zip")[0] == "application/zip"
    assert mimetypes.guess_type("site.webmanifest")[0] == "application/manifest+json"


def test_svg_sniffing():
    import gzip

    from bis.util import looks_like_svg

    svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"/>'
    assert looks_like_svg(svg) and looks_like_svg(b"\xef\xbb\xbf" + svg)
    assert looks_like_svg(gzip.compress(svg))                                       # .svgz
    assert looks_like_svg(('<?xml version="1.0" encoding="UTF-16"?>' + svg.decode()).encode("utf-16"))
    assert looks_like_svg(b"<?xml version='1.0'?>\n<!-- big comment -->")         # let the parser decide
    assert not looks_like_svg(b"hello world") and not looks_like_svg(b"\x89PNG\r\n\x1a\n....")
    assert not looks_like_svg(b"\x1f\x8bnot really gzip")


def test_open_in_blender_launches_detached(tmp_path):
    """The real launcher with Python standing in for blender.exe: the GUI process must be fully detached:
    on Windows outside the server's process tree and outside every job object (it must survive the server)."""
    from bis.rendering import open_in_blender, pid_alive

    scene = tmp_path / "scene.py"  # stands in for the .blend
    marker = tmp_path / "opened.txt"
    scene.write_text("import os, sys, time\n"
                     f"open({str(marker)!r}, 'w').write(f'{{os.getpid()}} {{os.getppid()}} {{os.getcwd()}}')\n"
                     "time.sleep(1.5)\n")
    launched = open_in_blender(Path(sys.executable), scene)
    assert launched.pid > 0
    end = time.time() + 30
    while not (marker.is_file() and marker.read_text()) and time.time() < end:
        time.sleep(0.05)
    pid, ppid, cwd = marker.read_text().split(" ", 2)
    assert Path(cwd) == tmp_path
    assert int(ppid) != os.getpid()  # not our child
    if sys.platform == "win32":
        assert launched.method == "wmi", launched
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        # (the venv python.exe is a redirector that runs the real interpreter in its own job: check the
        # process the launcher started, i.e. Blender in production)
        h = k32.OpenProcess(0x1000, False, launched.pid)
        if h:  # still running (the script sleeps 1.5 s): it must not be in any job object
            in_job = wintypes.BOOL()
            assert k32.IsProcessInJob(h, None, ctypes.byref(in_job))
            k32.CloseHandle(h)
            assert not in_job.value
    end = time.time() + 30
    while launched.alive() and time.time() < end:
        time.sleep(0.05)
    assert not pid_alive(launched.pid)
