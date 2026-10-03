"""Round-4 colour-fidelity regressions (QA round 3 #1-#9, #11, #12). The corpus-wide measurement lives in
``fidelity_harness.py``; these tests pin the mechanisms on synthetic scenes.

* venv-only: the inverse Khronos PBR Neutral used to pre-compensate paints, the harness' gamut floor.
* real Blender 5.0 (OptiX), ≤ 256 px, ≤ 32 spp: plate and Liquid Glass colours in both engines, thin glass
  keeping its colour, touching pieces as one body, glow rasters as cards, crystal white glyphs, neon bloom.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))

import worker_client as wc  # noqa: E402
from blender_worker.util import hex_to_linear, pbr_neutral, pbr_neutral_inverse  # noqa: E402

needs_blender = pytest.mark.skipif(not wc.blender_available(), reason="Blender 5.0 not installed")
PX = 128
SPP = 24


def lab(rgb):
    c = np.asarray(rgb, float) / 255
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = lin @ np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750],
                          [0.0193339, 0.1191920, 0.9503041]]).T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > (6 / 29) ** 3, np.cbrt(xyz), xyz / (3 * (6 / 29) ** 2) + 4 / 29)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


def de(a, b) -> float:
    return float(np.linalg.norm(lab(a) - lab(b)))


def hex_rgb(h: str):
    return [int(h[i:i + 2], 16) for i in (1, 3, 5)]


def display(x) -> np.ndarray:
    y = np.clip(np.asarray(pbr_neutral(x)), 0, 1)
    return np.where(y <= 0.0031308, 12.92 * y, 1.055 * y ** (1 / 2.4) - 0.055) * 255


# ------------------------------------------------------------------------------------------------ venv only
@pytest.mark.parametrize("colour", ["#3d3d3d", "#282727", "#808080", "#e84a27", "#5865f2", "#13294b", "#f5f5f5",
                                   "#34a853", "#feafb5", "#57bb8a"])
def test_neutral_inverse_displays_in_gamut_paints_exactly(colour):
    """Khronos PBR Neutral darkened every paint by its 0.04 offset (#3d3d3d -> 31) and compressed peaks above
    0.76 (#e84a27 -> 229, 64, 10): the inverse used by materials.display_paint gives them back."""
    x = pbr_neutral_inverse(hex_to_linear(colour))
    assert de(display(x), hex_rgb(colour)) < 0.6


@pytest.mark.parametrize("colour,limit", [("#ffffff", 1.5), ("#ff3b00", 11.0), ("#fbbc04", 8.0), ("#007aff", 6.5),
                                          ("#fff763", 5.5)])
def test_neutral_inverse_out_of_gamut_paints_stay_close(colour, limit):
    """Bright saturated paints cannot be shown exactly through PBR Neutral (#ff3b00: ΔE >= 8.9 for any input);
    the saturation-capped inverse stays near that floor and never makes them worse than no compensation."""
    y = hex_to_linear(colour)
    got = de(display(pbr_neutral_inverse(y)), hex_rgb(colour))
    assert got < limit
    assert got <= de(display(y), hex_rgb(colour)) + 0.5


def test_harness_gamut_floor():
    import fidelity_harness as fh
    assert fh.gamut_floor([128, 128, 128]) < 0.2
    assert 8.0 < fh.gamut_floor([255, 59, 0]) < 9.8
    assert fh.gamut_floor([255, 59, 0], "standard") == 0.0


def test_harness_label_map_and_interiors():
    """The harness' label map paints the plate and the regions bottom -> top and erodes each by its bevel."""
    import fidelity_harness as fh
    import test_worker_quality as T
    proj, bundle = T.scene([T.layer("A", bevel=0.05), T.layer("B", z=0.13, bevel=0.05)],
                           {"A": T.geo([("a", [T.square(0.5)], "#ff0000", 1.0)]),
                            "B": T.geo([("b", [T.circle(0.2)], "#0000ff", 1.0)])})
    frame = fh.Frame(128)
    labels, infos, cards = fh.label_map(proj, bundle, frame)
    assert [i["kind"] for i in infos] == ["plate", "region", "region"]
    c = frame.px(np.array([[0.0, 0.0], [0.35, 0.35], [0.8, -0.8]])).astype(int)
    assert labels[c[0, 1], c[0, 0]] == 2 and labels[c[1, 1], c[1, 0]] == 1 and labels[c[2, 1], c[2, 0]] == 0
    inner = fh.interiors(labels, infos, frame, cards)
    edge = frame.px(np.array([[0.49, 0.0]])).astype(int)[0]
    assert labels[edge[1], edge[0]] == 1 and not inner[1][edge[1], edge[0]]      # bevel band excluded
    assert inner[1][c[1, 1], c[1, 0]]


# ------------------------------------------------------------------------------------------------ Blender
@pytest.fixture(scope="module")
def worker():
    if not wc.blender_available():
        pytest.skip("Blender 5.0 not installed")
    w = wc.Worker(warmup="basic")
    yield w
    w.close()


@pytest.fixture(scope="module")
def outdir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("bis_fidelity")


def render(w, proj_bundle, out, quality="draft", size=PX, **kw):
    proj, bundle = proj_bundle
    args = {"project": proj, "geometry": bundle, "quality": quality, "size": size, "out": str(out)}
    if quality == "preview":
        args["samples"] = SPP
    args.update(kw)
    return w.result("render", args)


def rgba(path):
    from PIL import Image
    return np.asarray(Image.open(path).convert("RGBA")).astype(float)


def patch(a, cx, cy, r=6, ortho=2.24):
    h = a.shape[0]
    col, row = int((cx + ortho / 2) / ortho * h), int((ortho / 2 - cy) / ortho * h)
    return a[row - r:row + r + 1, col - r:col + r + 1]


def mean_rgb(a, pts, r=5):
    return np.vstack([patch(a, x, y, r)[..., :3].reshape(-1, 3) for x, y in pts]).mean(axis=0)


@needs_blender
@pytest.mark.parametrize("quality", ["draft", "preview"])
@pytest.mark.parametrize("colour,limit", [("#ffffff", 4.0), ("#f5f5f5", 2.5), ("#e84a27", 3.0), ("#5865f2", 2.5),
                                          ("#3d3d3d", 2.5), ("#1d2230", 2.5), ("#808080", 2.5)])
def test_plate_matches_svg_in_both_engines(worker, outdir, quality, colour, limit):
    """#12 follow-up + #1: white, mid and dark satin plates under 'neutral' match the SVG in drafts AND previews
    (round 3: drafts ~10 % darker, white plates ΔE 4.7, dark plates ΔE 7.7)."""
    import test_worker_quality as T
    out = outdir / f"plate_{colour[1:]}_{quality}.png"
    render(worker, T.plate_scene(colour), out, quality)
    got = mean_rgb(rgba(out), [(x, y) for x in (-0.4, 0.0, 0.4) for y in (-0.4, 0.0, 0.4)])
    assert de(got, hex_rgb(colour)) < limit, (colour, quality, got.round())


@needs_blender
@pytest.mark.parametrize("quality", ["draft", "preview"])
@pytest.mark.parametrize("paint,limit", [("#e84a27", 3.5), ("#3d3d3d", 3.5), ("#feafb5", 3.5), ("#ffffff", 3.0),
                                         ("#f4b400", 7.5)])
@pytest.mark.parametrize("plate", ["#ffffff", "#13294b"])
def test_liquid_glass_body_carries_the_paint(worker, outdir, quality, paint, limit, plate):
    """#1/#6/#7: the Liquid Glass face shows its paint over light and dark plates, in both engines (Illinois'
    'I' draft ΔE 25.5, Notion's #3d3d3d faces darker than the plate, Recorder's pink bars clear capsules)."""
    import test_worker_quality as T
    scn = T.scene([T.layer("A", shadow=0.0)], {"A": T.geo([("e", [T.circle(0.45)], paint, 1.0)])}, plate_fill=plate)
    out = outdir / f"lg_{paint[1:]}_{plate[1:]}_{quality}.png"
    render(worker, scn, out, quality)
    got = patch(rgba(out), 0.0, 0.0, 8)[..., :3].reshape(-1, 3).mean(axis=0)
    assert de(got, hex_rgb(paint)) < limit, (paint, plate, quality, got.round())


@needs_blender
def test_drafts_match_previews(worker, outdir):
    """#1: EEVEE drafts and Cycles previews of the same glass face agree (round 3: 20-30 % darker drafts)."""
    import test_worker_quality as T
    scn = T.scene([T.layer("A", shadow=0.0)], {"A": T.geo([("e", [T.circle(0.45)], "#e84a27", 1.0)])},
                  plate_fill="#13294b")
    vals = {}
    for q in ("draft", "preview"):
        out = outdir / f"parity_{q}.png"
        render(worker, scn, out, q)
        vals[q] = patch(rgba(out), 0.0, 0.0, 8)[..., :3].reshape(-1, 3).mean(axis=0)
    assert de(vals["draft"], vals["preview"]) < 3.0, vals


def ring(r_out, r_in):
    import test_worker_quality as T
    return [T.circle(r_out), T.circle(r_in, hole=True)]


@needs_blender
@pytest.mark.parametrize("quality", ["draft", "preview"])
def test_thin_glass_ring_keeps_its_colour(worker, outdir, quality):
    """#3: Vanced's thin gradient ring rendered silver/white with colour only at its edges — a thin Liquid
    Glass piece is all bevel, and the bevel was clear glass mirroring the studio."""
    import test_worker_quality as T
    scn = T.scene([T.layer("A", shadow=0.0, bevel=0.03)],
                  {"A": T.geo([("e", ring(0.5, 0.42), "#3a74ff", 1.0)], safe=0.04)}, plate_fill="#2a1830")
    out = outdir / f"ring_{quality}.png"
    render(worker, scn, out, quality, size=192)
    a = rgba(out)
    got = mean_rgb(a, [(0.0, -0.46), (0.46, 0.0), (-0.33, -0.33), (0.33, -0.33)], r=1)
    assert de(got, hex_rgb("#3a74ff")) < 16.0, got.round()
    assert got[2] > got[0] + 90, got.round()           # blue, not silver


@needs_blender
@pytest.mark.parametrize("quality", ["draft", "preview"])
def test_touching_pieces_render_as_one_body(worker, outdir, quality):
    """#5: Gmail's dark shading wedge sits in a notch of the M; bevelled one by one, the shared edge became a
    V-groove showing the white plate (a white sliver with a rim). Touching opaque pieces are one body now."""
    import test_worker_quality as T
    left, right = T.rect(-0.5, -0.35, 0.0, 0.35), T.rect(0.0, -0.35, 0.5, 0.35)
    g = T.geo([("l", [left], "#ea4335", 1.0), ("r", [right], "#c5221f", 1.0)], safe=0.2)
    g["silhouette"] = [T.rect(-0.5, -0.35, 0.5, 0.35)]          # bis.svg: the union of the members
    scn = T.scene([T.layer("A", shadow=0.0)], {"A": g})
    out = outdir / f"touch_{quality}.png"
    r = render(worker, scn, out, quality, size=192)
    assert r["stats"].get("mergedLayers") == 1 and r["stats"]["pieces"] == 1
    a = rgba(out)
    seam = mean_rgb(a, [(0.0, y) for y in (-0.2, 0.0, 0.2)], r=1)
    sides = [mean_rgb(a, [(x, y) for y in (-0.2, 0.0, 0.2)], r=1) for x in (-0.06, 0.06)]
    assert lab(seam)[0] < max(lab(s)[0] for s in sides) + 4.0, (seam.round(), [s.round() for s in sides])


def _glow_png(path: Path) -> None:
    from PIL import Image
    n = 64
    yy, xx = np.mgrid[0:n, 0:n] + 0.5
    r = np.hypot(xx - n / 2, yy - n / 2)
    alpha = np.where(r < 12, 1.0, np.clip(0.5 * (28 - r) / 16, 0.0, 0.5))
    img = np.dstack([np.full((n, n), 255), np.full((n, n), 60), np.full((n, n), 200), alpha * 255]).astype(np.uint8)
    Image.fromarray(img, "RGBA").save(path)


@needs_blender
def test_glow_raster_region_is_a_flat_card(worker, outdir):
    """#4: Vanced Neon's glow layer (a raster that is 55 % soft alpha) was extruded along its traced contour into
    a second glass ring around the tube. A mostly-soft raster region renders as its flat card only."""
    import test_worker_quality as T
    png = outdir / "glow.png"
    _glow_png(png)
    g = T.geo([("img0", [T.circle(0.28)], "#ff3cc8", 1.0)], safe=0.2)
    g["images"] = [{"elementId": "img0", "path": str(png), "bbox": [-0.32, -0.32, 0.32, 0.32], "opacity": 1.0,
                    "width": 64, "height": 64, "matrix": [0.01, 0.0, 0.0, -0.01, -0.32, 0.32], "opaque": False}]
    scn = T.scene([T.layer("A", shadow=0.0)], {"A": g}, plate_fill="#16101c")
    r = render(worker, scn, outdir / "glow_draft.png", "draft")
    assert r["stats"].get("glowCards") == 1 and r["stats"]["pieces"] == 0
    objs = {o["name"]: o for o in worker.result("scene_info")["objects"]}
    assert "BIS A img0" in objs and "BIS A r0" not in objs


@needs_blender
def test_crystal_white_glyph_stays_readable(worker, outdir):
    """#8: water-clear crystal glass made white glyphs vanish into the plate they lens (Discord, Calculator)."""
    import test_worker_quality as T
    scn = T.scene([T.layer("A", preset="clear_glass", params={"tint": 0.35})],
                  {"A": T.geo([("e", [T.circle(0.4)], "#ffffff", 1.0)])}, plate_fill="#5865f2")
    out = outdir / "crystal.png"
    render(worker, scn, out, "preview")
    a = rgba(out)
    glyph = lab(patch(a, 0.0, 0.1, 6)[..., :3].reshape(-1, 3).mean(axis=0))[0]
    plate = lab(mean_rgb(a, [(-0.7, 0.7), (0.7, 0.7), (-0.7, -0.7)], r=4))[0]
    assert glyph - plate > 20.0, (glyph, plate)


@needs_blender
def test_neon_bloom_hugs_the_icon(worker, outdir):
    """#8: the neon look's bloom spilled far outside the plate (transparent film: the glow is folded into alpha).
    A white tube filling the plate: no glow beyond the plate's box (round 3: mean alpha 11, max 35)."""
    import test_worker_quality as T
    proj, bundle = T.scene([T.layer("A", preset="neon", shadow=0.0, thickness=0.06, bevel=0.028)],
                           {"A": T.geo([("e", [T.circle(0.85)], "#ffffff", 1.0)])}, plate_fill="#1b1b22")
    out = outdir / "neon.png"
    render(worker, (proj, bundle), out, "preview", size=160)
    a = rgba(out)[..., 3]
    n = a.shape[0]
    yy, xx = np.mgrid[0:n, 0:n] + 0.5
    x, y = (xx - n / 2) / n * 2.24, (n / 2 - yy) / n * 2.24
    outside = np.maximum(np.abs(x), np.abs(y)) > 1.03
    assert a[outside].mean() < 3.0 and a[outside].max() < 20, (a[outside].mean(), a[outside].max())


@needs_blender
def test_save_blend_lights_match_its_engine(worker, outdir):
    """lighting.ENGINE_CAL is per engine: a .blend saved at a draft (EEVEE) quality carries the draft rig and a
    final (Cycles) one the preview rig (save_blend used to build with the Cycles rig whatever the quality)."""
    import test_worker_quality as T
    proj, bundle = T.scene([T.layer("A")], {"A": T.geo([("e", [T.circle(0.4)], "#e84a27", 1.0)])})
    key = {}
    for q in ("draft", "final"):
        worker.result("save_blend", {"project": proj, "geometry": bundle, "quality": q, "pack": False,
                                     "out": str(outdir / f"cal_{q}.blend")})
        key[f"blend_{q}"] = worker.result("scene_info")["lights"]["BIS Key"]
    for q in ("draft", "preview"):
        render(worker, (proj, bundle), outdir / f"cal_{q}.png", q, size=64)
        key[q] = worker.result("scene_info")["lights"]["BIS Key"]
    assert abs(key["blend_draft"] - key["draft"]) < 1e-3, key
    assert abs(key["blend_final"] - key["preview"]) < 1e-3, key
    assert key["draft"] > 1.05 * key["preview"], key


@needs_blender
@pytest.mark.parametrize("quality", ["draft", "preview"])
@pytest.mark.parametrize("preset,params", [("frosted_glass", {"tint": 0.55}), ("dispersive_crystal", {"tint": 0.25})])
def test_white_glyph_in_frosted_and_prism_glass_stays_readable(worker, outdir, quality, preset, params):
    """The frosted and prism looks had the crystal look's #8 defect: colourless glass tints nothing, so a white
    glyph took the plate's colour and vanished (glyph L* 34-48 on a plate of L* 50; Discord, Spotify, Settings,
    Brave). White paints get a frosted-white body (materials._white_ice); coloured paints are unchanged."""
    import test_worker_quality as T
    scn = T.scene([T.layer("A", preset=preset, params=params)],
                  {"A": T.geo([("e", [T.circle(0.4)], "#ffffff", 1.0)])}, plate_fill="#5865f2")
    out = outdir / f"ice_{preset}_{quality}.png"
    render(worker, scn, out, quality)
    a = rgba(out)
    glyph = lab(patch(a, 0.0, 0.1, 6)[..., :3].reshape(-1, 3).mean(axis=0))[0]
    plate = lab(mean_rgb(a, [(-0.7, 0.7), (0.7, 0.7), (-0.7, -0.7)], r=4))[0]
    assert glyph - plate > 25.0, (glyph, plate)
    red = T.scene([T.layer("A", preset=preset, params=params)],
                  {"A": T.geo([("e", [T.circle(0.4)], "#e84a27", 1.0)])}, plate_fill="#ffffff")
    out = outdir / f"ice_{preset}_red_{quality}.png"
    render(worker, red, out, quality)
    c = lab(patch(rgba(out), 0.0, 0.1, 6)[..., :3].reshape(-1, 3).mean(axis=0))
    assert c[1] > 15.0 and c[2] > 12.0, c          # still red glass, not milk


@needs_blender
def test_clear_glass_plate_over_white_fill_stays_clear(worker, outdir):
    """The crystal fix (white paints -> frosted crystal) applies to layer pieces only: a clear-glass plate with a
    white fill turned into opaque milk (Cycles: alpha 209 / 255 at the plate centre; clear glass: ~40)."""
    import test_worker_quality as T
    out = outdir / "clear_plate.png"
    render(worker, T.scene([], {}, plate_fill="#ffffff", plate_preset="clear_glass"), out, "preview")
    alpha = patch(rgba(out), 0.0, 0.0, 10)[..., 3].mean()
    assert alpha < 120, alpha
