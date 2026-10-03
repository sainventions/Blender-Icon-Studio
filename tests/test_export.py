"""Export packaging tests (FakeBridge + fake bis.svg; real bis.svg when importable)."""
from __future__ import annotations

import io
import json
import sys
import time
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from bis import export as ex  # noqa: E402
from bis.blender import FakeBridge  # noqa: E402
from bis.icon_format import canvas_viewbox, fill_json  # noqa: E402
from bis.main import create_app  # noqa: E402
from bis.models import ExportRequest, FillLinear, FillSolid, GradientStop, Project, SourceInfo  # noqa: E402
from bis.testing import TEST_SVG, FakeSvg, make_test_settings  # noqa: E402

ALL_TARGETS = ["ios", "macos", "watchos", "android", "windows", "web", "marketing", "icon", "blend"]


def wait_job(client, job_id: str, timeout: float = 60.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


def _project(**kw) -> Project:
    base = dict(id="p", name="P", createdAt="x", updatedAt="x",
                source=SourceInfo(filename="a.svg", viewBox=(0, 0, 500, 500)))
    base.update(kw)
    return Project.model_validate(base)


# ---------------------------------------------------------------------------------------------- unit
def test_plan_masters_per_target():
    p = _project()
    plan = ex.plan_export(p, ExportRequest(targets=["ios"], appearances=["light", "dark", "tinted-dark"]))
    assert set(plan.masters) == {"full-light", "full-dark", "full-tinted-dark"}
    assert all(m.full_bleed and not m.transparent for m in plan.masters.values())

    plan = ex.plan_export(p, ExportRequest(targets=ALL_TARGETS, appearances=["light", "dark"]))
    assert plan.targets == ALL_TARGETS
    assert set(plan.masters) == {"full-light", "full-dark", "macos", "circle", "android-fg", "android-bg",
                                 "android-mono", "rounded", "maskable", "hero-light", "hero-exploded-light",
                                 "hero-dark"}
    mac = plan.masters["macos"]
    assert mac.shape == "squircle" and abs(mac.zoom - 0.9014) < 1e-3  # Tahoe 824/1024
    assert abs(plan.masters["android-fg"].zoom - 1.12 * 72 / 108) < 1e-9 and not plan.masters["android-fg"].plate
    assert plan.masters["android-mono"].appearance == "clear-light"
    assert plan.masters["hero-light"].camera["view"] == "perspective"


def test_master_sizes_obey_gpu_rules():
    m = ex.Master("macos", "macOS", "light", 1024)
    assert ex.master_size(m, "draft", 512, None) == 512
    assert ex.master_size(m, "final", 512, None) == 1024
    assert ex.master_size(m, "final", 512, 128) == 128
    hero = ex.Master("hero-light", "Hero", "light", 1024, hero=True)
    assert ex.master_size(hero, "ultra", 512, None) == 2048


def test_make_variant_overrides_are_copies():
    p = _project()
    p.layers = [  # type: ignore[assignment]
        *[__import__("bis.models", fromlist=["Layer"]).Layer(id=f"l{i}", name="L", elementIds=["e"]) for i in range(2)]
    ]
    p.layers[1].transform.x = 0.5
    p.appearances.dark.layers["l0"] = __import__("bis.models", fromlist=["LayerOverride"]).LayerOverride(visible=True)
    bg = ex.make_variant(p, ex.Master("android-bg", "bg", "light", 432, full_bleed=True, layers=False))
    assert not any(l.visible for l in bg.layers) and bg.appearances.dark.layers["l0"].visible is False
    assert all(l.visible for l in p.layers)  # original untouched
    mk = ex.make_variant(p, ex.Master("maskable", "m", "light", 512, art_scale=0.8))
    assert mk.canvas.art.scale == pytest.approx(0.8) and mk.layers[1].transform.x == pytest.approx(0.4)
    fg = ex.make_variant(p, ex.Master("macos", "m", "light", 512, shape="circle", plate=False))
    assert fg.canvas.shape == "circle" and fg.canvas.plate.visible is False


def test_icon_viewbox_and_fills():
    p = _project()
    assert canvas_viewbox(p, (0, 0, 500, 500)) == pytest.approx((0, 0, 500, 500))
    p.canvas.art.scale = 500 / 466  # D9: the 466 px source plate fills the canvas
    vb = canvas_viewbox(p, (0, 0, 500, 500))
    assert vb == pytest.approx((17, 17, 466, 466))
    assert fill_json(FillSolid(color="#ff0000", opacity=0.5)) == {"solid": "srgb:1.00000,0.00000,0.00000,0.50000"}
    lin = fill_json(FillLinear(stops=[GradientStop(offset=0, color="#ffffff"), GradientStop(offset=1, color="#000000")],
                               start=(0, 1), end=(0, -1)))
    assert lin["orientation"] == {"start": {"x": 0.5, "y": 0.0}, "stop": {"x": 0.5, "y": 1.0}}
    assert fill_json(None) == "automatic" and fill_json(FillSolid.model_validate({"type": "solid"})) != "none"


def test_monochrome_and_grayscale_helpers():
    im = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    im.paste((255, 0, 0, 255), (2, 2, 6, 6))
    im.paste((255, 255, 255, 255), (3, 3, 5, 5))
    mono = ex.monochrome(im)
    assert mono.getpixel((0, 0))[3] == 0
    assert mono.getpixel((3, 3)) == (255, 255, 255, 255)        # brightest region → fully opaque white
    assert 40 < mono.getpixel((2, 2))[3] < 255                  # darker region → partial alpha
    assert ex.grayscale(im).mode == "RGB"
    small = ex.resize(Image.new("RGBA", (512, 512), (10, 200, 30, 255)), 16)
    assert small.size == (16, 16) and small.getpixel((8, 8))[3] == 255


# ---------------------------------------------------------------------------------------------- end to end
@pytest.fixture
def client(tmp_path):
    bridge = FakeBridge()
    app = create_app(make_test_settings(tmp_path), bridge=bridge, svg=FakeSvg())
    with TestClient(app) as c:
        c.bridge = bridge  # type: ignore[attr-defined]
        yield c


def _upload(c) -> str:
    r = c.post("/api/projects", files={"file": ("Test Icon.svg", TEST_SVG, "image/svg+xml")})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_full_export_package(client):
    pid = _upload(client)
    job = client.post(f"/api/projects/{pid}/export",
                      json={"targets": ALL_TARGETS, "appearances": ["light", "dark", "tinted-dark"],
                            "quality": "draft"}).json()
    job = wait_job(client, job["id"])
    assert job["state"] == "done", job
    res = job["result"]
    assert res["zip"] == f"/files/projects/{pid}/exports/{job['id']}.zip" == res["url"]
    assert res["targets"] == ALL_TARGETS and res["masters"] == 13

    # every master render ran in a one-shot process (D8) at ≤ 512 px (draft)
    renders = [(m, a) for m, cmd, a in client.bridge.calls if cmd == "render"]
    assert len(renders) == 13 and all(m == "oneshot" and a["size"] <= 512 for m, a in renders)
    assert all(a["quality"] == "draft" for _, a in renders)
    bg = next(a for _, a in renders if not any(l["visible"] for l in a["project"]["layers"]))
    assert bg["fullBleed"] is True
    fg = [a for _, a in renders if not a["project"]["canvas"]["plate"]["visible"]]
    assert {a["appearance"] for a in fg} == {"light", "clear-light"}

    z = zipfile.ZipFile(io.BytesIO(client.get(res["zip"]).content))
    names = set(z.namelist())
    root = "Test Icon icons/"
    expect = [
        "README.txt",
        "ios/AppIcon.appiconset/Contents.json", "ios/AppIcon.appiconset/AppIcon-1024.png",
        "ios/AppIcon.appiconset/AppIcon-1024-dark.png", "ios/AppIcon.appiconset/AppIcon-1024-tinted.png",
        "macos/AppIcon.icns", "macos/AppIcon.iconset/icon_16x16.png", "macos/AppIcon.iconset/icon_512x512@2x.png",
        "macos/AppIcon.appiconset/Contents.json",
        "watchos/AppIcon.appiconset/AppIcon-1024.png", "watchos/AppIcon-1088-circle.png",
        "android/res/mipmap-anydpi-v26/ic_launcher.xml", "android/res/mipmap-xxxhdpi/ic_launcher_foreground.png",
        "android/res/mipmap-mdpi/ic_launcher_background.png", "android/res/mipmap-hdpi/ic_launcher_monochrome.png",
        "android/res/mipmap-xhdpi/ic_launcher_round.png", "android/playstore-icon.png", "android/README.txt",
        "windows/app.ico", "windows/png/icon-256.png",
        "web/favicon.ico", "web/apple-touch-icon.png", "web/icon-192.png", "web/icon-512.png",
        "web/maskable-512.png", "web/manifest.webmanifest", "web/head-snippet.html", "web/favicon.svg",
        "marketing/hero.png", "marketing/hero-exploded.png", "marketing/hero-dark.png",
        "icon/Test Icon.icon/icon.json", "blend/Test Icon.blend",
    ]
    missing = [e for e in expect if root + e not in names]
    assert not missing, missing
    assert not any("_masters" in n for n in names)

    def img(name: str) -> Image.Image:
        return Image.open(io.BytesIO(z.read(root + name)))

    # iOS: opaque 1024, Contents.json with luminosity variants
    ios = img("ios/AppIcon.appiconset/AppIcon-1024.png")
    assert ios.size == (1024, 1024) and ios.mode == "RGB"
    contents = json.loads(z.read(root + "ios/AppIcon.appiconset/Contents.json"))
    assert [i.get("appearances", [{}])[0].get("value") for i in contents["images"]] == [None, "dark", "tinted"]
    assert all(i["size"] == "1024x1024" and i["platform"] == "ios" for i in contents["images"])
    t = img("ios/AppIcon.appiconset/AppIcon-1024-tinted.png").convert("RGB")
    r, g, b = t.getpixel((512, 512))
    assert r == g == b  # grayscale

    # macOS: transparent Tahoe margin, plate body inside 824 px; icns with all sizes
    mac = img("macos/AppIcon.iconset/icon_512x512@2x.png")
    assert mac.size == (1024, 1024) and mac.getpixel((40, 512))[3] == 0 and mac.getpixel((512, 512))[3] == 255
    assert mac.getpixel((512, 100 + 30))[3] == 255 and mac.getpixel((512, 100 - 30))[3] == 0
    icns = Image.open(io.BytesIO(z.read(root + "macos/AppIcon.icns")))
    assert {(s[0] * s[2]) for s in icns.info["sizes"]} >= {32, 64, 256, 512, 1024}
    assert img("macos/AppIcon.iconset/icon_16x16.png").size == (16, 16)

    # watchOS: 1088 circle with transparent corners
    w = img("watchos/AppIcon-1088-circle.png")
    assert w.size == (1088, 1088) and w.getpixel((5, 5))[3] == 0 and w.getpixel((544, 544))[3] == 255

    # Android adaptive: foreground transparent outside the art, background opaque, mono white
    fgi = img("android/res/mipmap-xxxhdpi/ic_launcher_foreground.png")
    assert fgi.size == (432, 432) and fgi.getpixel((3, 3))[3] == 0 and fgi.getpixel((216, 216))[3] > 0
    bgi = img("android/res/mipmap-xxxhdpi/ic_launcher_background.png")
    assert bgi.mode == "RGB" and bgi.size == (432, 432)
    mono = img("android/res/mipmap-xxxhdpi/ic_launcher_monochrome.png").convert("RGBA")
    assert mono.getpixel((216, 216))[:3] == (255, 255, 255)
    assert img("android/res/mipmap-mdpi/ic_launcher.png").size == (48, 48)
    rnd = img("android/res/mipmap-xxxhdpi/ic_launcher_round.png")
    assert rnd.size == (192, 192) and rnd.getpixel((2, 2))[3] == 0
    xml = z.read(root + "android/res/mipmap-anydpi-v26/ic_launcher.xml").decode()
    assert "<monochrome" in xml and "@mipmap/ic_launcher_foreground" in xml

    # Windows / web
    ico = Image.open(io.BytesIO(z.read(root + "windows/app.ico")))
    assert sorted(ico.ico.sizes()) == sorted((s, s) for s in ex.ICO_SIZES)
    fav = Image.open(io.BytesIO(z.read(root + "web/favicon.ico")))
    assert sorted(fav.ico.sizes()) == [(16, 16), (32, 32), (48, 48)]
    assert img("web/apple-touch-icon.png").size == (180, 180)
    manifest = json.loads(z.read(root + "web/manifest.webmanifest"))
    assert {i["purpose"] for i in manifest["icons"]} == {"any", "maskable"}

    # .icon bundle (beta): groups front→back, one asset per layer
    icon = json.loads(z.read(root + "icon/Test Icon.icon/icon.json"))
    assert [g["name"] for g in icon["groups"]] == ["Layer 2", "Layer 1"]
    assert icon["fill-specializations"] == [  # plate fill + the default dark appearance (System Dark)
        {"value": {"solid": "srgb:0.14510,0.38824,0.92157,1.00000"}},
        {"appearance": "dark", "value": "system-dark"},
    ]
    assert icon["supported-platforms"] == {"circles": ["watchOS"], "squares": "shared"}
    for g in icon["groups"]:
        asset = root + "icon/Test Icon.icon/Assets/" + g["layers"][0]["image-name"]
        svg = z.read(asset).decode()
        assert 'width="1024"' in svg and 'viewBox="2 2 96 96"' in svg  # plate (2..98) fills the artboard
        assert g["shadow"]["kind"] == "neutral" and g["translucency"]["enabled"] is True

    # result lists every file with a working URL + previews for the UI
    files = {f["name"]: f["url"] for f in res["files"]}
    assert "ios/AppIcon.appiconset/AppIcon-1024.png" in files
    assert client.get(files["web/icon-512.png"]).status_code == 200
    assert [p["name"] for p in res["previews"]][:3] == [
        "ios/AppIcon.appiconset/AppIcon-1024.png", "ios/AppIcon.appiconset/AppIcon-1024-dark.png",
        "ios/AppIcon.appiconset/AppIcon-1024-tinted.png"]


def test_export_cancel(client):
    pid = _upload(client)
    client.bridge.delay = 1.0
    job = client.post(f"/api/projects/{pid}/export", json={"targets": ["ios", "macos"], "quality": "draft"}).json()
    end = time.time() + 10
    while client.get(f"/api/jobs/{job['id']}").json()["state"] != "running" and time.time() < end:
        time.sleep(0.02)
    time.sleep(0.2)
    assert client.delete(f"/api/jobs/{job['id']}").json()["state"] == "cancelled"
    client.bridge.delay = 0
    nxt = client.post(f"/api/projects/{pid}/render", json={"size": 32}).json()
    assert wait_job(client, nxt["id"])["state"] == "done"
    assert wait_job(client, job["id"])["state"] == "cancelled"
    exports = Path(client.app.state.settings.projects_dir) / pid / "exports"
    assert not (exports / f"{job['id']}.zip").exists()
    end = time.time() + 5
    while (exports / job["id"]).exists() and time.time() < end:  # partial package cleaned up
        time.sleep(0.05)
    assert not (exports / job["id"]).exists()


def test_export_ios_only_light(client):
    pid = _upload(client)
    job = wait_job(client, client.post(f"/api/projects/{pid}/export",
                                       json={"targets": ["ios"], "appearances": ["light"], "quality": "draft"}
                                       ).json()["id"])
    assert job["state"] == "done"
    names = [f["name"] for f in job["result"]["files"]]
    assert sorted(names) == ["README.txt", "ios/AppIcon.appiconset/AppIcon-1024.png",
                             "ios/AppIcon.appiconset/Contents.json"]


# ---------------------------------------------------------------------------------------------- real svg pipeline
def _real_svg_available() -> bool:
    try:
        import bis.svg  # noqa: F401

        return hasattr(bis.svg, "import_svg") and hasattr(bis.svg, "build_geometry")
    except Exception:
        return False


@pytest.mark.skipif(not _real_svg_available(), reason="bis.svg (workstream A) not available yet")
def test_export_with_real_svg_pipeline(tmp_path):
    """Real bis.svg geometry + layer SVGs, fake renders: .icon bundle and package from a corpus icon."""
    app = create_app(make_test_settings(tmp_path, export_max_size=128), bridge=FakeBridge())
    with TestClient(app) as c:
        p = c.post("/api/projects", json={"sample": "Maps"}).json()
        assert p.get("layers"), p
        job = wait_job(c, c.post(f"/api/projects/{p['id']}/export",
                                 json={"targets": ["ios", "icon", "web"], "quality": "draft"}).json()["id"], 120)
        assert job["state"] == "done", job
        names = [f["name"] for f in job["result"]["files"]]
        assert any(n.endswith("icon.json") for n in names)
        assert sum(1 for n in names if "/Assets/" in n) == len([l for l in p["layers"]])


def test_export_path_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "long_paths_enabled", lambda: False)
    ex.check_path_budget(tmp_path / "exports" / "abcdef123456")       # pytest tmp paths are short enough
    deep = Path("C:/" + "d" * 150) / "exports" / "abcdef123456"
    with pytest.raises(ValueError, match="too long for Windows"):
        ex.check_path_budget(deep)
    monkeypatch.setattr(ex, "long_paths_enabled", lambda: True)
    ex.check_path_budget(deep)


def test_icon_bundle_names_are_capped(tmp_path):
    from bis.icon_format import ICON_ASSET_NAME_MAX, ICON_NAME_MAX, write_icon_bundle
    from bis.models import GeometryBundle, Layer, LayerGeometry

    svg = tmp_path / "layer.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="5" height="5"/></svg>')
    long = "An Extremely Long Project Name That Keeps Going And Going"
    p = _project(name=long, layers=[Layer(id="l0", name="L" * 80, elementIds=["e0"])])
    lg = LayerGeometry(layerId="l0", hash="h", silhouette=[], regions=[], safeRadius=0.1, bbox=(0, 0, 1, 1),
                       texture="", texturePath="", svg=str(svg))
    bundle = GeometryBundle(projectId="p", hash="h", viewBox=(0, 0, 10, 10), layers={"l0": lg})
    out = write_icon_bundle(p, bundle, tmp_path, tmp_path / "icon", long)
    assert len(out.name) <= ICON_NAME_MAX + len(".icon")
    assets = list((out / "Assets").iterdir())
    assert len(assets) == 1 and len(assets[0].stem) <= ICON_ASSET_NAME_MAX
    assert len(str(out / "Assets" / assets[0].name)) - len(str(tmp_path)) <= ex.EXPORT_PATH_BUDGET
