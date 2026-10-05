"""Integration tests across workstreams.

* Real ``bis.svg`` (workstream A) + FakeBridge: runs whenever the pipeline is importable.
* Real Blender 5.0 worker (workstream B), opt-in (``BIS_REAL_BLENDER=1``): imports a corpus icon, builds
  geometry and renders a tiny draft (128 px) through the persistent worker; plus a tiny one-shot preview.
  GPU rule: ≤ 256 px, ≤ 32 spp.
"""
from __future__ import annotations

import io
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
from bis.testing import make_test_settings  # noqa: E402
from bis.util import files_path  # noqa: E402


def _real_svg() -> bool:
    try:
        import bis.svg as s

        return all(hasattr(s, n) for n in ("import_svg", "build_geometry", "geometry_path", "split_layers"))
    except Exception:
        return False


def wait_job(client, job_id: str, timeout: float) -> dict:
    end = time.time() + timeout
    job: dict = {}
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s: {job}")


@pytest.mark.skipif(not _real_svg(), reason="bis.svg (workstream A) not importable yet")
def test_real_svg_pipeline_through_api(tmp_path):
    app = create_app(make_test_settings(tmp_path), bridge=FakeBridge())
    with TestClient(app) as c:
        p = c.post("/api/projects", json={"sample": "Maps"}).json()
        assert p["source"]["plateDetected"] is True and p["layers"], p
        pid = p["id"]
        g = c.get(f"/api/projects/{pid}/geometry").json()
        assert set(g["layers"]) == {l["id"] for l in p["layers"]}
        lg = next(iter(g["layers"].values()))
        assert c.get(lg["texture"]).status_code == 200 and Path(lg["texturePath"]).is_file()
        assert c.get(lg["svg"]).status_code == 200
        r = c.get(f"/api/projects/{pid}/layers/{p['layers'][0]['id']}/thumbnail.png?size=64")
        assert r.status_code == 200 and Image.open(io.BytesIO(r.content)).size == (64, 64)
        if len(p["layers"]) >= 2:
            ids = [l["id"] for l in p["layers"][:2]]
            r = c.post(f"/api/projects/{pid}/layers/merge", json={"layerIds": ids})
            assert r.status_code in (200, 400), r.text  # 400 = z-order-illegal merge (ZOrderError)
        s = c.post(f"/api/projects/{pid}/split", json={"strategy": "element"}).json()
        assert s["strategy"] == "element" and len(s["layers"]) >= 1
        job = wait_job(c, c.post(f"/api/projects/{pid}/render", json={"size": 64}).json()["id"], 60)
        assert job["state"] == "done", job
        thumbs = c.get("/api/projects").json()
        assert thumbs[0]["thumbnail"] and c.get(thumbs[0]["thumbnail"]).status_code == 200


REAL = os.environ.get("BIS_REAL_BLENDER") == "1"


@pytest.mark.skipif(not REAL, reason="set BIS_REAL_BLENDER=1 to run against real Blender 5.0 (GPU)")
@pytest.mark.skipif(not _real_svg(), reason="bis.svg (workstream A) not importable yet")
def test_real_blender_draft_render(tmp_path):
    settings = make_test_settings(tmp_path, blender_exe="__auto__", start_worker=True, auto_preview=False)
    if not settings.blender_found:
        pytest.skip("Blender 5.0 not found")
    if not settings.worker_script.is_file():
        pytest.skip("blender_worker/worker.py (workstream B) not available yet")
    app = create_app(settings)
    with TestClient(app) as c:
        p = c.post("/api/projects", json={"sample": "Maps"}).json()
        pid = p["id"]
        t0 = time.time()
        job = wait_job(c, c.post(f"/api/projects/{pid}/render",
                                 json={"quality": "draft", "size": 128}).json()["id"], 600)
        assert job["state"] == "done", job
        res = job["result"]
        png = Path(res["path"])
        assert png.is_file() and png.stat().st_size > 1000
        im = Image.open(png)
        assert im.size == (128, 128)
        alpha = im.convert("RGBA").getchannel("A")
        assert alpha.getbbox() is not None  # something was rendered
        assert c.get(res["url"]).status_code == 200
        status = c.get("/api/system").json()
        assert status["worker"]["state"] in ("ready", "busy") and status["gpu"]["device"] == "OPTIX"
        first = time.time() - t0

        # warm second draft goes through the same persistent worker
        t1 = time.time()
        job2 = wait_job(c, c.post(f"/api/projects/{pid}/render",
                                  json={"quality": "draft", "size": 128, "appearance": "dark"}).json()["id"], 300)
        assert job2["state"] == "done", job2
        print(f"\nreal draft: first {first:.1f}s (incl. worker start), warm {time.time() - t1:.2f}s, "
              f"engine={res['engine']} device={res['device']}")

        # tiny one-shot Cycles render (final tier routed to a one-shot process; 64 px keeps it cheap)
        job3 = wait_job(c, c.post(f"/api/projects/{pid}/render",
                                  json={"quality": "final", "size": 64}).json()["id"], 600)
        assert job3["state"] == "done", job3
        assert Path(job3["result"]["path"]).stat().st_size > 500


@pytest.mark.skipif(not REAL, reason="set BIS_REAL_BLENDER=1 to run against real Blender 5.0 (GPU)")
@pytest.mark.skipif(not _real_svg(), reason="bis.svg (workstream A) not importable yet")
def test_real_blender_small_export(tmp_path):
    """iOS + Android + macOS + .icon export through real one-shot Blender processes, masters capped at 128 px."""
    settings = make_test_settings(tmp_path, blender_exe="__auto__", start_worker=False, auto_preview=False,
                                  export_max_size=128)
    if not settings.blender_found or not settings.oneshot_script.is_file():
        pytest.skip("Blender 5.0 / blender_worker not available")
    app = create_app(settings)
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"sample": "Maps"}).json()["id"]
        job = wait_job(c, c.post(f"/api/projects/{pid}/export", json={
            "targets": ["ios", "android", "macos", "icon"], "appearances": ["light", "dark"],
            "quality": "draft"}).json()["id"], 900)
        assert job["state"] == "done", job
        names = {f["name"] for f in job["result"]["files"]}
        for n in ("ios/AppIcon.appiconset/AppIcon-1024.png", "ios/AppIcon.appiconset/AppIcon-1024-dark.png",
                  "android/res/mipmap-xxxhdpi/ic_launcher_foreground.png", "macos/AppIcon.icns"):
            assert n in names, n
        out = Path(job["result"]["folder"])
        fg = Image.open(out / "android/res/mipmap-xxxhdpi/ic_launcher_foreground.png").convert("RGBA")
        assert fg.getpixel((2, 2))[3] == 0 and fg.getchannel("A").getbbox() is not None
        mac = Image.open(out / "macos/AppIcon.iconset/icon_512x512@2x.png").convert("RGBA")
        assert mac.getpixel((20, 512))[3] == 0 and mac.getpixel((512, 512))[3] > 200
        print(f"\nreal export: {job['result']['masters']} masters in {job['result']['seconds']:.1f}s")


def _alpha_aspect(im: Image.Image) -> float:
    """height / width of the rendered subject (alpha bbox)."""
    x0, y0, x1, y1 = im.convert("RGBA").getchannel("A").point(lambda a: 255 if a > 32 else 0).getbbox()
    return (y1 - y0) / max(1, x1 - x0)


@pytest.mark.skipif(not REAL, reason="set BIS_REAL_BLENDER=1 to run against real Blender 5.0 (GPU)")
@pytest.mark.skipif(not _real_svg(), reason="bis.svg (workstream A) not importable yet")
def test_real_blender_cad_pov_heroes_and_iso_animation(tmp_path):
    """PLAN §11 View, end to end: the marketing heroes really ARE the CAD-style POV - head-on → hero (iso 0.55) →
    hero-iso (isometric) foreshorten the square plate more and more (orthographic, real distances) - and the 'iso'
    animation (also reached through the legacy 'explode' kind) moves the camera. Before the worker honoured
    camera.iso, both heroes and every animation frame came out as the same head-on picture."""
    settings = make_test_settings(tmp_path, blender_exe="__auto__", start_worker=False, auto_preview=False,
                                  export_max_size=96)
    if not settings.blender_found or not settings.oneshot_script.is_file():
        pytest.skip("Blender 5.0 / blender_worker not available")
    app = create_app(settings)
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"sample": "Gemini"}).json()["id"]
        job = wait_job(c, c.post(f"/api/projects/{pid}/export", json={
            "targets": ["marketing", "macos"], "appearances": ["light"], "quality": "draft"}).json()["id"], 900)
        assert job["state"] == "done", job
        out = Path(job["result"]["folder"])
        head = Image.open(out / "macos/AppIcon.iconset/icon_512x512@2x.png")      # head-on master, same plate
        hero = Image.open(out / "marketing/hero.png")
        iso = Image.open(out / "marketing/hero-iso.png")
        a_head, a_hero, a_iso = _alpha_aspect(head), _alpha_aspect(hero), _alpha_aspect(iso)
        assert a_head > 0.9 and a_iso < 0.8 and a_iso + 0.03 < a_hero < a_head - 0.03, (a_head, a_hero, a_iso)

        anim = wait_job(c, c.post(f"/api/projects/{pid}/animate", json={
            "kind": "explode", "frames": 3, "size": 64, "quality": "draft", "format": "png"}).json()["id"], 600)
        assert anim["state"] == "done" and anim["result"]["kind"] == "iso", anim
        frames = [Image.open(files_path(settings.workspace, u)) for u in anim["result"]["frames"]]
        aspects = [_alpha_aspect(f) for f in frames]
        assert len(frames) == 3 and min(aspects) < max(aspects) - 0.1, aspects   # the POV really moves
        print(f"\nCAD POV aspect h/w: head-on {a_head:.2f}, hero {a_hero:.2f}, hero-iso {a_iso:.2f}; "
              f"iso animation frames {[round(a, 2) for a in aspects]}")


@pytest.mark.skipif(not _real_svg(), reason="bis.svg (workstream A) not importable yet")
def test_real_svg_upload_encodings(tmp_path):
    """.svgz and UTF-16 files are SVGs too: the server must hand them to the pipeline, not reject them."""
    import gzip

    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
           '<circle cx="50" cy="50" r="30" fill="#0a0"/></svg>')
    app = create_app(make_test_settings(tmp_path), bridge=FakeBridge())
    with TestClient(app) as c:
        for name, data in (("zipped.svgz", gzip.compress(svg.encode())),
                           ("wide.svg", ('<?xml version="1.0" encoding="UTF-16"?>' + svg).encode("utf-16"))):
            r = c.post("/api/projects", files={"file": (name, data, "image/svg+xml")})
            assert r.status_code == 200, (name, r.text)
            assert len(r.json()["elements"]) == 1
        r = c.post("/api/projects", files={"file": ("broken.svg", b"<svg><rect", "image/svg+xml")})
        assert r.status_code == 400 and "not a readable SVG" in r.json()["detail"]
