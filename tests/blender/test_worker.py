"""Integration tests for the Blender worker, launching REAL Blender 5.0 (OptiX). Skipped when Blender is
missing. GPU rules: renders ≤ 256 px, ≤ 32 spp.

    .venv/Scripts/python.exe -m pytest -q tests/blender
"""
from __future__ import annotations

import copy
import json
import os
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import worker_client as wc  # noqa: E402

pytestmark = pytest.mark.skipif(not wc.blender_available(), reason="Blender 5.0 not installed")

PRESETS = list(json.loads((wc.REPO / "shared" / "presets.json").read_text(encoding="utf-8"))["materials"])
APPEARANCES = ["light", "dark", "clear-light", "clear-dark", "tinted-light", "tinted-dark"]
CORPUS = ["Maps", "Photos", "Discord", "Settings", "Spotify", "Calculator"]
SPP = 24          # preview samples in tests (≤ 32)
PX = 128


# ------------------------------------------------------------------------------------------------
# fixtures
# ------------------------------------------------------------------------------------------------
@pytest.fixture(scope="session")
def fixtures() -> dict:
    import make_fixtures
    out = HERE / "_fixtures"
    index_path = out / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    missing = [n for n in CORPUS if n not in index or not Path(index[n]["geometryPath"]).exists()]
    if missing:
        index = make_fixtures.make(missing, out)
    data = {}
    for n in CORPUS:
        e = index[n]
        data[n] = {"project": json.loads(Path(e["project"]).read_text(encoding="utf-8")),
                   "geometryPath": e["geometryPath"]}
    return data


@pytest.fixture(scope="session")
def worker():
    w = wc.Worker(warmup="full")
    yield w
    w.close()


@pytest.fixture(scope="session")
def outdir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("bis_worker")


def _png(path: str):
    from PIL import Image
    im = Image.open(path)
    im.load()
    return im


def _alpha_stats(path: str):
    import numpy as np
    a = np.asarray(_png(path).convert("RGBA")).astype(float)
    return a, a[..., 3]


def _render(worker, fx, out, **kw):
    args = {"project": fx["project"], "geometryPath": fx["geometryPath"], "quality": "draft", "size": PX,
            "out": str(out)}
    if kw.get("quality") == "preview":
        args["samples"] = SPP
    args.update(kw)
    ev, prog = worker.call("render", args)
    return ev["result"], prog


# ------------------------------------------------------------------------------------------------
# protocol / device
# ------------------------------------------------------------------------------------------------
def test_ready_line(worker):
    r = worker.ready
    assert r["device"] == "OPTIX"
    assert "NVIDIA" in r["gpu"]
    assert r["version"].startswith("5.0")
    assert r["port"] > 0 and r["pid"] > 0
    assert "warmupSeconds" in r


def test_ping_and_system_info(worker):
    assert worker.result("ping")["pong"] is True
    info = worker.result("system_info")
    assert info["device"] == "OPTIX" and info["deviceOk"] is True
    used = [d for d in info["devices"] if d["use"]]
    assert used and all(d["type"] == "OPTIX" for d in used)
    assert not any(d["use"] for d in info["devices"] if d["type"] in ("CUDA", "CPU"))


def test_bad_requests_never_crash(worker):
    ev = worker.send_raw(b"this is not json\n")
    assert ev["event"] == "error" and "bad JSON" in ev["error"]
    ev, _ = worker.call("does_not_exist", check=False)
    assert ev["event"] == "error"
    ev, _ = worker.call("render", {"project": {"id": "x"}}, check=False)
    assert ev["event"] == "error" and "geometryPath" in ev["error"]
    ev, _ = worker.call("render", {"project": {"id": "x"}, "geometryPath": "Z:/nope.json", "out": "x.png"},
                        check=False)
    assert ev["event"] == "error"
    ev, _ = worker.call("render", {"project": {"id": "x"}, "geometry": {}, "appearance": "sepia", "out": "x.png"},
                        check=False)
    assert ev["event"] == "error" and "appearance" in ev["error"]
    assert worker.result("ping")["pong"] is True


# ------------------------------------------------------------------------------------------------
# materials: every preset, draft + preview
# ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("preset", PRESETS)
def test_preset_draft_and_preview(worker, fixtures, outdir, preset):
    fx = copy.deepcopy(fixtures["Spotify"])
    for L in fx["project"]["layers"]:
        L["material"] = {"preset": preset, "params": {}}
    for q in ("draft", "preview"):
        out = outdir / f"preset_{preset}_{q}.png"
        r, _ = _render(worker, fx, out, quality=q)
        assert r["device"] == "OPTIX"
        assert r["engine"] == ("eevee" if q == "draft" else "cycles")
        assert (r["width"], r["height"]) == (PX, PX)
        a, alpha = _alpha_stats(str(out))
        assert alpha.max() == 255 and alpha.min() == 0          # icon on a transparent backdrop
        assert a[..., :3][alpha > 250].std() > 3.0             # not a flat blob


def test_swatches_command(worker, outdir):
    r = worker.result("swatches", {"outDir": str(outdir / "swatches"), "size": 64, "quality": "preview",
                                   "presets": ["liquid_glass", "chrome", "neon"]})
    assert len(r["files"]) == 3 and all(Path(f).is_file() for f in r["files"])


# ------------------------------------------------------------------------------------------------
# corpus geometry (workstream A bundles when available)
# ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", CORPUS)
def test_corpus_icon_preview(worker, fixtures, outdir, name):
    fx = fixtures[name]
    r, prog = _render(worker, fx, outdir / f"corpus_{name}.png", quality="preview")
    assert r["stats"]["layers"] == len([L for L in fx["project"]["layers"] if L.get("visible", True)])
    assert not r["warnings"]
    assert prog and all(0.0 <= p["progress"] <= 1.0 for p in prog)


# ------------------------------------------------------------------------------------------------
# appearances
# ------------------------------------------------------------------------------------------------
def test_six_appearances(worker, fixtures, outdir):
    import numpy as np
    lum = {}
    for ap in APPEARANCES:
        out = outdir / f"appearance_{ap}.png"
        r, _ = _render(worker, fixtures["Photos"], out, appearance=ap, quality="preview")
        assert r["appearance"] == ap
        a, alpha = _alpha_stats(str(out))
        rgb = a[..., :3][alpha > 250]
        lum[ap] = float(rgb.mean())
        if ap.startswith(("clear", "tinted")):
            assert r["wallpaper"] is not None
        if ap == "tinted-light":      # glyphs = mono luminance x the default blue tint
            c = a[..., :3][PX // 2 - 8:PX // 2 + 8, PX // 2 - 8:PX // 2 + 8].reshape(-1, 3).mean(axis=0)
            assert c[2] > c[0] + 20, c
    assert lum["dark"] < lum["light"] - 40
    assert lum["clear-dark"] < lum["clear-light"]
    assert lum["tinted-dark"] < lum["tinted-light"]


def test_watchos_ignores_appearance(worker, fixtures, outdir):
    fx = copy.deepcopy(fixtures["Discord"])
    fx["project"]["canvas"]["platform"] = "watchos"
    r, _ = _render(worker, fx, outdir / "watch_dark.png", appearance="dark")
    assert r["appearance"] == "light"


# ------------------------------------------------------------------------------------------------
# render options
# ------------------------------------------------------------------------------------------------
def test_full_bleed(worker, fixtures, outdir):
    out = outdir / "fullbleed.png"
    _render(worker, fixtures["Discord"], out, fullBleed=True, quality="preview")
    _, alpha = _alpha_stats(str(out))
    assert alpha[0, 0] == 255 and alpha[-1, -1] == 255 and alpha.min() == 255   # square plate edge to edge
    out2 = outdir / "framed.png"
    _render(worker, fixtures["Discord"], out2)
    _, alpha2 = _alpha_stats(str(out2))
    assert alpha2[0, 0] == 0


def test_backdrop_and_color_modes(worker, fixtures, outdir):
    fx = copy.deepcopy(fixtures["Spotify"])
    fx["project"]["render"] = {"backdrop": "color", "backdropColor": "#ff0000", "colorMode": "standard"}
    out = outdir / "backdrop_color.png"
    _render(worker, fx, out)
    a, alpha = _alpha_stats(str(out))
    assert alpha.min() == 255 and a[0, 0, 0] > 200 and a[0, 0, 1] < 40
    fx["project"]["render"] = {"backdrop": "wallpaper", "colorMode": "agx-punchy"}
    out = outdir / "backdrop_wallpaper.png"
    r, _ = _render(worker, fx, out)
    _, alpha = _alpha_stats(str(out))
    assert alpha.min() == 255 and r["wallpaper"]["kind"] == "light"


def test_perspective_camera(worker, fixtures, outdir):
    out = outdir / "persp.png"
    r, _ = _render(worker, fixtures["Photos"], out, quality="preview",
                   camera={"view": "perspective", "tiltX": 18, "tiltY": -24, "fov": 30})
    _, alpha = _alpha_stats(str(out))
    # whole icon in frame: border rows/cols stay transparent
    assert alpha[0].max() == 0 and alpha[-1].max() == 0 and alpha[:, 0].max() == 0 and alpha[:, -1].max() == 0


def test_light_angle_sign(worker, outdir):
    """angle 0 = light from the top, +90 = from the right, −45 = top-left: the lit plate rim is brighter."""
    import numpy as np
    proj = {"id": "lightcheck", "layers": [], "canvas": {"shape": "circle",
            "plate": {"fill": {"type": "solid", "color": "#808080"}, "material": {"preset": "satin"}}}}
    bundle = {"projectId": "lightcheck", "hash": "x", "layers": {}}
    lum = {}
    for ang in (0, 90, -45):
        proj["lighting"] = {"angle": ang, "elevation": 50}
        out = outdir / f"angle_{ang}.png"
        worker.result("render", {"project": proj, "geometry": bundle, "quality": "draft", "size": 96,
                                 "out": str(out)})
        a = np.asarray(_png(str(out)).convert("L")).astype(float)
        h, w = a.shape
        band = 10
        lum[ang] = {"top": a[6:6 + band, w // 3:2 * w // 3].mean(), "bottom": a[-6 - band:-6, w // 3:2 * w // 3].mean(),
                    "left": a[h // 3:2 * h // 3, 6:6 + band].mean(), "right": a[h // 3:2 * h // 3, -6 - band:-6].mean()}
    assert lum[0]["top"] > lum[0]["bottom"]
    assert lum[90]["right"] > lum[90]["left"]
    assert lum[-45]["top"] > lum[-45]["bottom"] and lum[-45]["left"] > lum[-45]["right"]


def test_partial_project_gets_model_defaults(worker, outdir):
    """The worker receives plain dicts: a hand-written partial project must render with models.py defaults."""
    from make_fixtures import _scale_splines  # noqa: F401  (import check only)
    k = 0.5522847498 * 0.5
    pts = [{"co": [0.5, 0], "hl": [0.5, -k], "hr": [0.5, k]}, {"co": [0, 0.5], "hl": [k, 0.5], "hr": [-k, 0.5]},
           {"co": [-0.5, 0], "hl": [-0.5, k], "hr": [-0.5, -k]}, {"co": [0, -0.5], "hl": [-k, -0.5], "hr": [k, -0.5]}]
    bundle = {"layers": {"A": {"silhouette": [{"points": pts}], "safeRadius": 0.3, "bbox": [-0.5, -0.5, 0.5, 0.5],
                               "regions": [{"elementId": "e", "paint": {"type": "solid", "color": "#ff3366"},
                                            "splines": [{"points": pts}]}]}}}
    proj = {"id": "partial", "layers": [{"id": "A", "fill": {"type": "solid", "color": "#ff3366"}}]}
    r = worker.result("render", {"project": proj, "geometry": bundle, "quality": "preview", "samples": 16,
                                 "size": 96, "out": str(outdir / "partial.png")})
    assert r["stats"] == {"layers": 1, "pieces": 1}


def test_combined_mode_one_body_per_layer(worker, fixtures, outdir):
    fx = copy.deepcopy(fixtures["Calculator"])
    for L in fx["project"]["layers"]:
        L["mode"] = "combined"
    r, _ = _render(worker, fx, outdir / "combined.png")
    assert r["stats"]["pieces"] == r["stats"]["layers"] == len(fx["project"]["layers"])


def test_layer_transform_moves_art(worker, fixtures, outdir):
    import numpy as np

    def centroid_x(path):
        a = np.asarray(_png(str(path)).convert("RGBA")).astype(float)
        lum = a[..., :3].mean(axis=2)
        mask = (lum > 235) & (a[..., 3] > 250)          # the white glyph
        ys, xs = np.nonzero(mask)
        return xs.mean()

    fx = copy.deepcopy(fixtures["Discord"])
    fx["project"]["layers"][0]["material"] = {"preset": "flat", "params": {}}
    _render(worker, fx, outdir / "tr0.png")
    fx["project"]["layers"][0]["transform"] = {"x": 0.4, "y": 0.0, "scale": 0.6}
    r, _ = _render(worker, fx, outdir / "tr1.png")
    assert centroid_x(outdir / "tr1.png") - centroid_x(outdir / "tr0.png") > 0.15 * PX


def test_one_material_per_shape(worker, fixtures, outdir):
    """PLAN §11: every shape object has its own single-Principled material 'BIS <layer name> / <element id>', and a
    value-only change updates it in place (tests/blender/test_materials.py covers the graph in depth)."""
    fx = copy.deepcopy(fixtures["Spotify"])
    L0 = fx["project"]["layers"][0]
    r, _ = _render(worker, fx, outdir / "inplace0.png")
    info = worker.result("scene_info")
    shapes = [o for o in info["objects"] if o["type"] == "MESH" and o["name"] not in ("BIS Plate", "BIS Wallpaper")]
    assert len({o["material"] for o in shapes}) == len(shapes) == r["stats"]["pieces"]
    name = next(o["material"] for o in shapes if o["material"].startswith(f"BIS {L0['name']} / "))
    before = info["materials"][name]
    L0["material"]["params"] = {"tint": 0.9, "roughness": 0.3, "ior": 1.7}
    fx["project"]["lighting"] = {"angle": 30}
    _render(worker, fx, outdir / "inplace1.png")
    after = worker.result("scene_info")["materials"][name]
    assert after["key"] == before["key"] and after["nodeIds"] == before["nodeIds"]   # same nodes, new values
    assert after["principled"]["IOR"] == pytest.approx(1.7)


def test_raster_image_regions(worker, outdir):
    """Raster <image> elements (A: alpha-contour regions + images[] PNG) extrude and show their pixels."""
    import make_fixtures
    import numpy as np
    index = make_fixtures.make(["Outlook"], HERE / "_fixtures")
    e = index["Outlook"]
    proj = json.loads(Path(e["project"]).read_text(encoding="utf-8"))
    out = outdir / "raster.png"
    worker.result("render", {"project": proj, "geometryPath": e["geometryPath"], "quality": "preview",
                             "samples": SPP, "size": PX, "out": str(out)})
    a = np.asarray(_png(str(out)).convert("RGB")).astype(float)
    centre = a[PX // 3:2 * PX // 3, PX // 3:2 * PX // 3]
    blue = (centre[..., 2] > centre[..., 0] + 60).mean()
    assert blue > 0.15, blue          # the Outlook logo's blues are visible on the white plate


def test_raster_image_exact_placement(worker, outdir):
    """images[].matrix (pixel -> art) places the PNG exactly. iMessage's PNG has transparent margins, so its
    alpha-traced bbox is smaller than the image: projecting the PNG over that bbox shifts every pixel.
    Rendered colours at known art points must match the source PNG sampled through the matrix."""
    import make_fixtures
    import numpy as np
    from PIL import Image
    index = make_fixtures.make(["iMessage"], HERE / "_fixtures")
    e = index["iMessage"]
    proj = json.loads(Path(e["project"]).read_text(encoding="utf-8"))
    bundle = json.loads(Path(e["geometryPath"]).read_text(encoding="utf-8"))
    (im,) = [im for g in bundle["layers"].values() for im in g["images"]]
    assert im.get("matrix") and im.get("width") and im.get("height")
    for L in proj["layers"]:      # flat = colour-exact (glass would let the plate shine through)
        L["material"] = {"preset": "flat", "params": {}}
        L["shadow"] = {"kind": "none", "opacity": 0.0}
    proj["render"] = {"colorMode": "standard"}
    size = 128
    out = outdir / "raster_placement.png"
    worker.result("render", {"project": proj, "geometryPath": e["geometryPath"], "quality": "draft", "size": size,
                             "out": str(out)})
    got = np.asarray(_png(str(out)).convert("RGB")).astype(float)
    src = np.asarray(Image.open(im["path"]).convert("RGB")).astype(float)
    a, b, c, d, ex, f = im["matrix"]
    det = a * d - b * c
    art = proj["canvas"]["art"]
    diffs = []
    for x, y in ((0.0, 0.4), (-0.4, 0.1), (0.4, 0.1), (0.0, -0.25), (0.25, 0.3)):
        px = (d * (x - ex) - c * (y - f)) / det                  # art -> image pixel (inverse matrix)
        py = (-b * (x - ex) + a * (y - f)) / det
        want = src[int(py), int(px)]
        cx, cy = x * art["scale"] + art["x"], y * art["scale"] + art["y"]   # art -> canvas -> render pixel
        col, row = int((cx + 1.12) / 2.24 * size), int((1.12 - cy) / 2.24 * size)
        diffs.append(float(np.abs(got[row, col] - want).max()))
    assert max(diffs) < 24, diffs


def _circle_splines(r: float, cx: float = 0.0) -> list:
    k = 0.5522847498 * r
    pts = [{"co": [cx + r, 0], "hl": [cx + r, -k], "hr": [cx + r, k]},
           {"co": [cx, r], "hl": [cx + k, r], "hr": [cx - k, r]},
           {"co": [cx - r, 0], "hl": [cx - r, k], "hr": [cx - r, -k]},
           {"co": [cx, -r], "hl": [cx - k, -r], "hr": [cx + k, -r]}]
    return [{"closed": True, "points": pts}]


def _flat_disc(r: float, fill: dict, extra_geo: dict | None = None, hashed: bool = False):
    spl = _circle_splines(r)
    geo = {"silhouette": spl, "safeRadius": 0.3, "bbox": [-r, -r, r, r],
           "regions": [{"elementId": "e", "paint": {"type": "solid", "color": "#ffffff"}, "splines": spl}]}
    if hashed:
        geo["hash"] = f"disc-{r}"
    geo.update(extra_geo or {})
    proj = {"id": "disc", "layers": [{"id": "A", "fill": fill, "material": {"preset": "flat", "params": {}},
                                      "shadow": {"kind": "none"}}], "canvas": {"shape": "none"}}
    return proj, {"layers": {"A": geo}}


def test_hashless_bundles_never_reuse_cached_geometry(worker, outdir):
    """Hand-written bundles without LayerGeometry.hash: layer ids repeat across projects, so the curve
    cache must key on the splines, not on the id (a stale disc of another project would render)."""
    cover = {}
    for r in (0.8, 0.3):
        proj, bundle = _flat_disc(r, {"type": "solid", "color": "#ffffff"})
        out = outdir / f"hashless_{r}.png"
        worker.result("render", {"project": proj, "geometry": bundle, "quality": "draft", "size": 64, "out": str(out)})
        _, alpha = _alpha_stats(str(out))
        cover[r] = float((alpha > 128).mean())
    assert cover[0.8] > 4 * cover[0.3] > 0, cover


def test_missing_raster_file_is_skipped(worker, outdir):
    import numpy as np
    proj, bundle = _flat_disc(0.6, {"type": "solid", "color": "#808080"}, {
        "images": [{"elementId": "img9", "path": "Z:/does/not/exist.png", "bbox": [-0.3, -0.3, 0.3, 0.3]}]})
    out = outdir / "missing_raster.png"
    r = worker.result("render", {"project": proj, "geometry": bundle, "quality": "preview", "samples": 8,
                                 "size": 64, "out": str(out)})
    assert any("not found" in w for w in r["warnings"]), r["warnings"]
    a = np.asarray(_png(str(out)).convert("RGB")).astype(int)
    magenta = (a[..., 0] > 180) & (a[..., 2] > 180) & (a[..., 1] < 90)
    assert not magenta.any()


def test_radial_focal_point(worker, outdir):
    """SVG focal radial gradients: the t = 0 colour sits at the focal point, not at the centre."""
    import numpy as np
    stops = [{"offset": 0, "color": "#ffffff"}, {"offset": 1, "color": "#000000"}]
    peak = {}
    for name, focal in (("centred", None), ("focal", [0.5, 0.0])):
        fill = {"type": "radial", "stops": stops, "center": [0.0, 0.0], "radius": 0.8, "focal": focal}
        proj, bundle = _flat_disc(0.8, fill, hashed=True)
        out = outdir / f"radial_{name}.png"
        worker.result("render", {"project": proj, "geometry": bundle, "quality": "draft", "size": 64,
                                 "out": str(out)})
        lum = np.asarray(_png(str(out)).convert("L")).astype(float)
        ys, xs = np.nonzero(lum >= lum.max() - 6)
        peak[name] = xs.mean() / lum.shape[1]
    assert abs(peak["centred"] - 0.5) < 0.06, peak
    assert peak["focal"] > 0.6, peak


# ------------------------------------------------------------------------------------------------
# .blend, animation, one-shot
# ------------------------------------------------------------------------------------------------
def test_save_blend(worker, fixtures, outdir):
    out = outdir / "icon.blend"
    fx = fixtures["Maps"]
    r = worker.result("save_blend", {"project": fx["project"], "geometryPath": fx["geometryPath"], "out": str(out),
                                     "pack": True})
    assert out.is_file() and out.stat().st_size > 50_000
    assert r["packed"] >= 1
    # the worker keeps rendering fine afterwards
    _render(worker, fx, outdir / "after_blend.png")


def test_tiny_animation_mp4(worker, fixtures, outdir):
    fx = fixtures["Photos"]
    ev, prog = worker.call("animate", {"project": fx["project"], "geometryPath": fx["geometryPath"],
                                       "quality": "draft", "size": 96, "kind": "tilt", "frames": 6, "fps": 12,
                                       "format": "mp4", "outDir": str(outdir / "anim")})
    r = ev["result"]
    assert len(r["frames"]) == 6 and all(Path(f).is_file() for f in r["frames"])
    assert Path(r["video"]).is_file() and Path(r["video"]).stat().st_size > 1000
    assert len(prog) >= 6


def test_animation_odd_size_mp4_and_bad_args(worker, fixtures, outdir):
    """H.264 needs even dimensions: an odd size is rounded up; bad arguments fail before any frame."""
    fx = fixtures["Discord"]
    base = {"project": fx["project"], "geometryPath": fx["geometryPath"], "quality": "draft", "kind": "light-sweep",
            "frames": 2, "fps": 12, "format": "mp4"}
    r = worker.result("animate", {**base, "size": 63, "outDir": str(outdir / "anim odd #1")})
    assert r["size"] == 64 and _png(r["frames"][0]).size == (64, 64)
    assert Path(r["video"]).is_file() and Path(r["video"]).parent == (outdir / "anim odd #1")
    for bad in ({"quality": "insane"}, {"appearance": "sepia"}, {"kind": "wobble"}):
        ev, _ = worker.call("animate", {**base, "size": 32, "outDir": str(outdir / "anim_bad"), **bad}, check=False)
        assert ev["event"] == "error"
    assert not (outdir / "anim_bad").exists() or not list((outdir / "anim_bad").glob("*.png"))
    ev, _ = worker.call("save_blend", {"project": fx["project"], "geometryPath": fx["geometryPath"],
                                       "appearance": "sepia", "out": str(outdir / "bad.blend")}, check=False)
    assert ev["event"] == "error" and "appearance" in ev["error"]


@pytest.mark.parametrize("kind", ["turntable", "float", "light-sweep", "iso", "explode"])
def test_animation_kinds_png(worker, fixtures, outdir, kind):
    fx = fixtures["Discord"]
    r = worker.result("animate", {"project": fx["project"], "geometryPath": fx["geometryPath"], "quality": "draft",
                                  "size": 64, "kind": kind, "frames": 3, "fps": 12, "format": "png",
                                  "outDir": str(outdir / f"anim_{kind}")})
    assert len(r["frames"]) == 3 and "video" not in r


def test_oneshot_render_and_error(fixtures, outdir):
    fx = fixtures["Settings"]
    out = outdir / "oneshot.png"
    code, events, lines = wc.run_oneshot({"id": "job1", "cmd": "render", "args": {
        "project": fx["project"], "geometryPath": fx["geometryPath"], "quality": "preview", "samples": SPP,
        "size": PX, "out": str(out)}}, outdir / "job1.json")
    assert code == 0, "\n".join(lines[-30:])
    assert events[-1]["event"] == "done" and events[-1]["id"] == "job1"
    assert events[-1]["result"]["device"] == "OPTIX"
    assert any(e["event"] == "progress" for e in events)
    assert out.is_file()
    code, events, _ = wc.run_oneshot({"cmd": "render", "args": {}}, outdir / "job_bad.json")
    assert code == 1 and events[-1]["event"] == "error"


# ------------------------------------------------------------------------------------------------
# performance / resources
# ------------------------------------------------------------------------------------------------
def test_warm_draft_is_fast(worker, fixtures, outdir):
    fx = fixtures["Maps"]
    _render(worker, fx, outdir / "warm0.png", size=256)
    times = []
    for i in range(3):
        r, _ = _render(worker, fx, outdir / f"warm{i}.png", size=256)
        times.append(r["seconds"])
    assert min(times) < 0.5, times


def test_no_vram_growth_over_20_renders(worker, fixtures, outdir):
    """20 mixed draft/preview renders: the worker's own Cycles memory (render_stats 'Mem'/'Peak', per
    process) must stay flat; whole-GPU nvidia-smi is noisy (other GPU users), so it gets a looser bound."""
    names = ["Maps", "Photos", "Calculator", "Discord"]
    for i in range(4):   # settle: both engines initialised, caches populated
        _render(worker, fixtures[names[i % 4]], outdir / "vram.png", quality="preview" if i % 2 else "draft")
    time.sleep(1.0)
    before = wc.gpu_memory_used_mib()
    peaks = []
    for i in range(20):
        q = "preview" if i % 2 else "draft"
        r, _ = _render(worker, fixtures[names[i % 4]], outdir / "vram.png", quality=q,
                       appearance=APPEARANCES[i % len(APPEARANCES)])
        if q == "preview":
            peaks.append(((names[i % 4], APPEARANCES[i % len(APPEARANCES)]), r["memPeakMB"]))
    time.sleep(1.0)
    after = wc.gpu_memory_used_mib()
    by_icon: dict = {}
    for n, mb in peaks:
        by_icon.setdefault(n, []).append(mb)
    for n, mbs in by_icon.items():           # same scene (icon + rendition) -> same device memory, render after render
        assert max(mbs) - min(mbs) < 32, (n, mbs)
    info = worker.result("system_info")
    assert info["datablocks"]["objects"] < 60 and info["datablocks"]["images"] < 30
    if before is not None and after is not None:
        print(f"VRAM whole GPU: {before} -> {after} MiB; worker Cycles peaks {by_icon}")
        assert after - before < 300, (before, after)


# ------------------------------------------------------------------------------------------------
# round 2: auto-framing, EEVEE clear/tinted plate, raster performance
# ------------------------------------------------------------------------------------------------
def _alpha_box(path: str, thr: int = 8):
    import numpy as np
    _, alpha = _alpha_stats(path)
    ys, xs = np.nonzero(alpha > thr)
    h, w = alpha.shape
    return {"touch": bool(xs.min() == 0 or ys.min() == 0 or xs.max() == w - 1 or ys.max() == h - 1),
            "cx": (xs.min() + xs.max() + 1) / 2 / w, "cy": (ys.min() + ys.max() + 1) / 2 / h,
            "ext": max(xs.max() - xs.min() + 1, ys.max() - ys.min() + 1) / max(w, h)}


@pytest.mark.parametrize("name", ["Photos", "Maps"])
def test_perspective_views_are_auto_framed(worker, fixtures, outdir, name):
    """Tilted perspective views: never cropped, centred, filling ~84 % (8 % margin per side)."""
    cams = {"tilt": {"view": "perspective", "tiltX": 20, "tiltY": -25, "fov": 30},
            "steep": {"view": "perspective", "tiltX": 35, "tiltY": 30, "fov": 30},
            "wide": {"view": "perspective", "tiltX": -12, "tiltY": 40, "fov": 60}}
    for key, cam in cams.items():
        out = outdir / f"framing_{name}_{key}.png"
        _render(worker, fixtures[name], out, camera=cam)
        b = _alpha_box(str(out))
        assert not b["touch"], (key, b)
        assert abs(b["cx"] - 0.5) < 0.03 and abs(b["cy"] - 0.5) < 0.03, (key, b)
        assert 0.78 < b["ext"] < 0.9, (key, b)


def test_front_framing_unchanged(worker, fixtures, outdir):
    """Front orthographic framing stays exactly ortho_scale 2.24 (plate edge 5.4 % from the border)."""
    out = outdir / "front_framing.png"
    _render(worker, fixtures["Photos"], out, size=224)
    b = _alpha_box(str(out), thr=128)
    assert abs(b["ext"] - 2.0 / 2.24) < 0.012, b
    assert abs(b["cx"] - 0.5) < 0.006 and abs(b["cy"] - 0.5) < 0.006, b


@pytest.mark.parametrize("kind", ["iso", "turntable", "tilt"])
def test_animation_frames_share_one_framing(worker, fixtures, outdir, kind):
    """Animations fit the union of all frames: no frame is cropped and the clip never zooms/jitters."""
    fx = fixtures["Photos"]
    r = worker.result("animate", {"project": fx["project"], "geometryPath": fx["geometryPath"], "quality": "draft",
                                  "size": 96, "kind": kind, "frames": 8, "fps": 12, "format": "png",
                                  "outDir": str(outdir / f"anim_frame_{kind}")})
    boxes = [_alpha_box(f) for f in r["frames"]]
    assert not any(b["touch"] for b in boxes), boxes
    assert max(b["ext"] for b in boxes) > 0.7          # the widest frame fills the frame (no tiny subject)
    cam = worker.result("scene_info")["objects"]
    assert any(o["name"] == "BIS Camera" for o in cam)


def test_raster_icons_render_fast(worker, outdir):
    """Icons with embedded raster images: baked meshes (no per-render curve/modifier evaluation) and
    adaptive spline resolution keep warm drafts well under a second (Vanced Neon took 12.7 s)."""
    import make_fixtures
    index = make_fixtures.make(["Vanced Neon", "Find Device"], HERE / "_fixtures")
    for name in ("Vanced Neon", "Find Device"):
        e = index[name]
        proj = json.loads(Path(e["project"]).read_text(encoding="utf-8"))
        args = {"project": proj, "geometryPath": e["geometryPath"], "quality": "draft", "size": 256,
                "out": str(outdir / f"rasterperf_{name}.png")}
        worker.result("render", args)                                   # cold: bake + shader compile
        warm = min(worker.result("render", args)["seconds"] for _ in range(2))
        assert warm < 1.0, (name, warm)
        pv = worker.result("render", {**args, "quality": "preview", "samples": SPP})["seconds"]
        assert pv < 2.5, (name, pv)


def test_raster_region_perspective_framing(worker, outdir):
    """An extruded raster region is framed by its traced contour, not its PNG placement quad (Find Device's
    image overhangs the plate: the view was framed ~8 % too loose and off-centre)."""
    import make_fixtures
    index_path = HERE / "_fixtures" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    if "Find Device" not in index or not Path(index["Find Device"]["geometryPath"]).exists():
        index = make_fixtures.make(["Find Device"], HERE / "_fixtures")
    e = index["Find Device"]
    proj = json.loads(Path(e["project"]).read_text(encoding="utf-8"))
    for L in proj["layers"]:            # the raster region under test (round 9 imports this shine hidden)
        L["visible"] = True
    out = outdir / "framing_find_device.png"
    worker.result("render", {"project": proj, "geometryPath": e["geometryPath"], "quality": "draft", "size": PX,
                             "out": str(out), "camera": {"view": "perspective", "tiltX": 20, "tiltY": -25, "fov": 30}})
    b = _alpha_box(str(out))
    assert not b["touch"] and abs(b["cx"] - 0.5) < 0.02 and abs(b["cy"] - 0.5) < 0.02, b
    assert 0.8 < b["ext"] < 0.9, b
