"""Round-5 release blockers (QA round 4): the 'brand' colour mode, display-space blending of translucent overlays,
clear / tinted rendition contrast, rank-spread mono steps on combined bodies, crisp Liquid Glass rims, flush-with-
plate art and the warm-up's shader variants.

* venv-only: the soft clip and its inverse, presets, overlay.py (film coefficients, beneath estimate, plate
  distance / flush detection), appearance.py (mono LUT, clear / tinted rules).
* real Blender 5.0 (OptiX), <= 256 px, <= 32 spp: brand plates, the compositor, overlay films, rendition contrast,
  mono steps, the rim band, flush edges.
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
from blender_worker import appearance as A  # noqa: E402
from blender_worker import overlay as O  # noqa: E402
from blender_worker import presets as P  # noqa: E402
from blender_worker.util import (BRAND_CAP, BRAND_KNEE, hex_to_linear, pbr_neutral, soft_clip,  # noqa: E402
                                 soft_clip_inverse)

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


def to_srgb255(lin):
    y = np.clip(np.asarray(lin, float), 0, 1)
    return np.where(y <= 0.0031308, 12.92 * y, 1.055 * y ** (1 / 2.4) - 0.055) * 255


# ------------------------------------------------------------------------------------------------ venv only
def test_soft_clip_is_identity_below_the_knee_and_rolls_off_above():
    assert soft_clip((0.0, 0.5, BRAND_KNEE)) == (0.0, 0.5, BRAND_KNEE)
    hi = soft_clip((1.0, 1.3, 1.6))
    assert BRAND_KNEE < hi[0] < hi[1] < hi[2] < 1.0                 # monotone, never reaches 1 (no hard clip)
    eps = 1e-4                                                       # C1 at the knee
    assert abs((soft_clip((BRAND_KNEE + eps,))[0] - BRAND_KNEE) / eps - 1.0) < 1e-2


@pytest.mark.parametrize("colour", ["#ff3b00", "#ff5a00", "#ff00ff", "#ffffff", "#6441ff", "#1d2230", "#f5f5f5"])
def test_brand_inverse_displays_every_paint(colour):
    """Brand plates were gamut-limited under PBR Neutral (Brave #ff3b00 >= dE 8.9, Template magenta 8.4):
    Standard + soft clip with the exact inverse shows them within 8-bit rounding (targets capped at BRAND_CAP)."""
    y = hex_to_linear(colour)
    shown = to_srgb255(soft_clip(soft_clip_inverse(y)))
    assert de(shown, hex_rgb(colour)) < 0.6
    if colour in ("#ff3b00", "#ff00ff"):
        neutral = to_srgb255(pbr_neutral(y))
        assert de(neutral, hex_rgb(colour)) > 5.0                    # what 'neutral' showed without compensation
    assert max(soft_clip_inverse((1.0, 1.0, 1.0))) == pytest.approx(soft_clip_inverse((BRAND_CAP,))[0])


def test_brand_colour_mode_is_the_worker_default():
    cm = P.load()["colorModes"]["brand"]
    assert cm["viewTransform"] == "Standard" and cm["look"] == "None"
    assert cm["softClip"] == pytest.approx(BRAND_KNEE)
    assert P.DEFAULT_COLOR_MODE == "brand"
    assert P.color_mode_id(None) == "brand" and P.color_mode_id("bogus") == "brand"
    assert P.color_mode_id("neutral") == "neutral" and P.soft_clip_knee("neutral") == 0.0
    assert P.soft_clip_knee(None) == pytest.approx(BRAND_KNEE)
    for mode in ("neutral", "standard", "agx", "agx-punchy"):          # the old modes stay
        assert mode in P.load()["colorModes"]


@pytest.mark.parametrize("cm", ["brand", "neutral", "standard"])
@pytest.mark.parametrize("paint,alpha,below", [("#000000", 0.33, "#7882ff"), ("#000000", 0.22, "#2e71dc"),
                                               ("#ffffff", 0.66, "#ff7c3b"), ("#000000", 0.44, "#9bbe04")])
def test_film_shows_the_svg_blend_at_the_estimate(cm, paint, alpha, below):
    """Internet's 33 % black ring over a bright blue plate read (51, 63, 179) for the SVG's (52, 58, 130): a linear
    alpha over (pre-compensated) radiance is not SVG's sRGB blend. The film T * R + E is exact at the estimate."""
    sp, sb = np.array(hex_rgb(paint)) / 255, np.array(hex_rgb(below)) / 255
    t, e = O.blend_coeffs(sp, alpha, sb, cm)
    fwd, inv = O._transform(cm)
    out = np.asarray(t) * inv(O.eotf(sb)) + np.asarray(e)
    got = O.oetf(fwd(out)) * 255
    want = ((1 - alpha) * sb + alpha * sp) * 255
    assert de(got, want) < (2.5 if cm == "neutral" else 0.5), (got.round(), want.round())
    tf, ef = O.blend_coeffs(sp, alpha, sb, cm, follow=O.FILM_FOLLOW)  # partial follow: still exact at the estimate
    got_f = O.oetf(fwd(np.asarray(tf) * inv(O.eotf(sb)) + np.asarray(ef))) * 255
    assert de(got_f, got) < 0.5
    assert all(0.0 <= v <= 1.0 for v in tf) and all(v >= 0.0 for v in ef)


def _square(cx, cy, h):
    c = [(cx + h, cy + h), (cx - h, cy + h), (cx - h, cy - h), (cx + h, cy - h)]
    return {"closed": True, "hole": False, "parent": -1, "depth": 0,
            "points": [{"co": list(p), "hl": list(p), "hr": list(p)} for p in c]}


def test_beneath_estimate_composites_plate_and_lower_layers():
    proj = {"canvas": {"shape": "squircle", "plate": {"fill": {"type": "linear", "start": [0, 1], "end": [0, -1],
                                                               "stops": [{"offset": 0, "color": "#ffffff"},
                                                                         {"offset": 1, "color": "#000000"}]}},
                       "art": {"scale": 1.0, "x": 0.0, "y": 0.0}},
            "layers": [{"id": "A", "visible": True, "opacity": 1.0, "fill": {"type": "auto"}},
                       {"id": "B", "visible": True, "opacity": 1.0, "fill": {"type": "auto"}}]}
    bundle = {"layers": {
        "A": {"regions": [{"elementId": "a", "paint": {"type": "solid", "color": "#ff0000"}, "opacity": 0.5,
                           "splines": [_square(0.4, 0.0, 0.2)]}]},
        "B": {"regions": [{"elementId": "b", "paint": {"type": "solid", "color": "#000000"}, "opacity": 0.3,
                           "splines": [_square(0.4, 0.0, 0.1)]}]}}}
    below_b = O.beneath_srgb(proj, bundle, "B", 0)        # 50 % red over the mid-grey plate (y = 0: 0.5)
    assert np.allclose(below_b, (0.75, 0.25, 0.25), atol=0.03), below_b
    below_a = O.beneath_srgb(proj, bundle, "A", 0)        # the plate alone
    assert np.allclose(below_a, (0.5, 0.5, 0.5), atol=0.03), below_a
    assert O.beneath_srgb({**proj, "canvas": {**proj["canvas"], "shape": "none"}}, bundle, "A", 0, "none") is None


def test_plate_distance_and_flush_detection():
    for shape in ("squircle", "rounded", "circle", "square"):
        d = O.plate_distance(shape, 0.225, np.array([[0.0, 0.0], [1.0, 0.0], [0.0, -1.0], [1.3, 0.0]]))
        assert d[0] > 0.9 and abs(d[1]) < 1e-6 and abs(d[2]) < 1e-6 and d[3] < 0
    t = np.linspace(0, 2 * np.pi, 200, endpoint=False)
    rim = np.stack([np.sign(np.cos(t)) * np.abs(np.cos(t)) ** 0.4, np.sign(np.sin(t)) * np.abs(np.sin(t)) ** 0.4], 1)
    fs = O.flush_spec([rim * 0.9995, rim * 0.85], "squircle", 0.225, 0.045)     # a frame along the plate edge
    assert fs and fs["shape"] == "squircle" and fs["band"][0] < 0.045 < fs["band"][1]
    assert O.flush_spec([rim * 0.6], "squircle", 0.225, 0.045) is None           # inner art: keeps its rim
    assert O.flush_spec([rim], "none", 0.225, 0.045) is None


def test_mono_lut_keeps_steps_between_similar_paints():
    """Maps' red / blue / green (lightness 0.50 / 0.53 / 0.58) stretched linearly were ~0.04 apart: a combined
    body (one bevel round all colours) lost its internal edges in the clear / tinted renditions."""
    vals = [A._perceptual(A.hex_to_srgb(h)) for h in ("#ea4335", "#4285f4", "#34a853", "#fbbc04", "#ffffff")]
    lut = A.mono_lut(vals, A.CLEAR_COMBINED_FLOOR, linear=A.CLEAR_COMBINED_LINEAR)
    ts = [t for _, t in lut]
    assert ts == sorted(ts) and ts[-1] == pytest.approx(1.0)
    stops = dict(lut)
    steps = np.diff([stops[v] for v in sorted(v for v in stops if v in vals)])
    assert steps.min() > 0.06, steps
    assert len(A.mono_lut([i / 100 for i in range(100)], 0.3)) <= 32               # colour-ramp limit


def _proj(fill="#5865f2", combined=False):
    return {"layers": [{"id": "L0", "visible": True, "material": {"preset": "liquid_glass", "params": {}},
                        "mode": "combined" if combined else "individual",
                        "shadow": {"kind": "neutral", "opacity": 0.5}, "fill": {"type": "auto"}}],
            "canvas": {"platform": "ios", "plate": {"fill": {"type": "solid", "color": fill},
                                                    "material": {"preset": "satin", "params": {}}}},
            "appearances": {"tint": {"color": "#3b82f6", "strength": 0.8}}}


def test_clear_and_tinted_rules():
    bundle = {"layers": {"L0": {"regions": [{"paint": {"type": "solid", "color": "#ffffff"}},
                                            {"paint": {"type": "solid", "color": "#4285f4"}}]}}}
    cl = A.resolve(_proj(), "clear-light", bundle)["env"]
    assert cl["mono"]["floor"] == A.CLEAR_MONO_FLOOR and cl["mono"]["lut"]
    # mid paints lifted toward white (Photos' petals 0.50-0.57 -> >= 0.84), the darkest paint stays dark (reviewer:
    # a flat 0.7 floor washed out Secure Folder's keyhole, Ti84's body, Camera's lens)
    photos = dict(A.gamma_lut([0.50, 0.53, 0.57, 0.77], A.CLEAR_MONO_FLOOR, A.CLEAR_MONO_GAMMA))
    assert min(photos[v] for v in (0.50, 0.53, 0.57)) >= 0.84 and photos[0.77] == pytest.approx(1.0)
    keyhole = dict(A.gamma_lut([0.30, 1.0], A.CLEAR_MONO_FLOOR, A.CLEAR_MONO_GAMMA))
    assert keyhole[0.30] == pytest.approx(A.MONO_FLOOR) and keyhole[1.0] == pytest.approx(1.0)
    lut = A.gamma_lut([i / 100 for i in range(100)], A.CLEAR_MONO_FLOOR, A.CLEAR_MONO_GAMMA)
    assert len(lut) <= 32 and all(a[0] < b[0] and a[1] <= b[1] for a, b in zip(lut, lut[1:]))
    assert cl["monoCombined"]["lut"] and cl["monoCombined"]["floor"] == A.CLEAR_COMBINED_FLOOR
    tl = A.resolve(_proj(), "tinted-light", bundle)["env"]
    assert tl["mono"]["strength"] == pytest.approx(min(1.0, 0.8 + A.TINT_LIGHT_GAIN))
    td = A.resolve(_proj(), "tinted-dark", bundle)["env"]
    assert td["mono"]["strength"] == pytest.approx(0.8) and td["mono"]["lut"]
    assert td["mono"]["floor"] == A.TINT_DARK_FLOOR > A.MONO_FLOOR
    cd = A.resolve(_proj(), "clear-dark", bundle)["project"]["canvas"]["plate"]["material"]
    assert cd["params"]["coat"] == A.CLEAR_DARK_COAT < 1.0


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
    return tmp_path_factory.mktemp("bis_round5")


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
@pytest.mark.parametrize("colour", ["#ff3b00", "#ff00ff", "#ffffff", "#6441ff"])
def test_brand_plates_are_exact(worker, outdir, quality, colour):
    """Brave / Reddit / Template / Life360 plates: dE 9-13 under 'neutral' (gamut), < 3 in 'brand'."""
    import test_worker_quality as T
    scn = T.scene([], {}, plate_fill=colour, color_mode="brand")
    out = outdir / f"brand_{colour[1:]}_{quality}.png"
    r = render(worker, scn, out, quality)
    assert r["colorMode"] == "brand"
    got = mean_rgb(rgba(out), [(x, y) for x in (-0.4, 0.0, 0.4) for y in (-0.4, 0.0, 0.4)])
    assert de(got, hex_rgb(colour)) < 3.0, (colour, quality, got.round())


@needs_blender
def test_missing_colour_mode_renders_brand_with_the_soft_clip(worker, outdir):
    import test_worker_quality as T
    proj, bundle = T.plate_scene("#ff3b00")
    proj["render"].pop("colorMode")
    r = render(worker, (proj, bundle), outdir / "default_cm.png")
    assert r["colorMode"] == "brand"
    comp = worker.result("scene_info")["compositor"]
    assert comp["viewTransform"] == "Standard" and comp["useCompositing"] and "Clip" in comp["group"]
    proj["render"]["colorMode"] = "neutral"
    render(worker, (proj, bundle), outdir / "neutral_cm.png")
    comp = worker.result("scene_info")["compositor"]
    assert comp["viewTransform"] == "Khronos PBR Neutral" and not comp["useCompositing"]


@needs_blender
@pytest.mark.parametrize("quality", ["draft", "preview"])
@pytest.mark.parametrize("paint,alpha,plate", [("#000000", 0.33, "#6a76ff"), ("#ffffff", 0.66, "#ff6436"),
                                               ("#000000", 0.33, "#e89000")])
def test_translucent_overlay_blends_like_the_svg(worker, outdir, quality, paint, alpha, plate):
    """Internet's ring (dE 26), Files' tab (14), Contacts' / Secure Folder's white cards (12-13 in 'brand' before
    the film): a translucent piece over the plate shows the SVG's sRGB blend."""
    import test_worker_quality as T
    scn = T.scene([T.layer("A", shadow=0.0)], {"A": T.geo([("e", [T.circle(0.42)], paint, alpha)])},
                  plate_fill=plate, color_mode="brand")
    out = outdir / f"film_{paint[1:]}_{plate[1:]}_{quality}.png"
    render(worker, scn, out, quality)
    film = worker.result("scene_info")["film"]
    assert film, "translucent Liquid Glass pieces carry film coefficients"
    got = patch(rgba(out), 0.0, 0.0, 8)[..., :3].reshape(-1, 3).mean(axis=0)
    want = (1 - alpha) * np.array(hex_rgb(plate)) + alpha * np.array(hex_rgb(paint))
    assert de(got, want) < 5.0, (got.round(), want.round())


@needs_blender
def test_clear_light_white_glyph_contrast(worker, outdir):
    """R2-14: clear-light glyph - plate L* +14..16 (faint). Now >= 25 (Discord, Settings, iMessage, Spotify)."""
    import test_worker_quality as T
    proj, bundle = T.glyph_scene()
    out = outdir / "clear_light.png"
    render(worker, (proj, bundle), out, "preview", appearance="clear-light")
    a = rgba(out)
    glyph = lab(patch(a, 0.0, 0.1, 6)[..., :3].reshape(-1, 3).mean(axis=0))[0]
    plate = lab(mean_rgb(a, [(-0.7, 0.7), (0.7, 0.7), (-0.7, -0.7), (0.7, -0.7)], r=4))[0]
    assert glyph - plate >= 25.0, (glyph, plate)


@needs_blender
def test_tinted_light_contrast(worker, outdir):
    """QA round 4 #4: tinted-light glyph - plate L* dropped -20.5 -> -13.4 (Discord). Restored (<= -20)."""
    import test_worker_quality as T
    proj, bundle = T.glyph_scene()
    out = outdir / "tinted_light.png"
    render(worker, (proj, bundle), out, "preview", appearance="tinted-light")
    a = rgba(out)
    glyph = lab(patch(a, 0.0, 0.1, 6)[..., :3].reshape(-1, 3).mean(axis=0))[0]
    plate = lab(mean_rgb(a, [(-0.7, 0.7), (0.7, 0.7), (-0.7, -0.7), (0.7, -0.7)], r=4))[0]
    assert glyph - plate <= -20.0, (glyph, plate)


@needs_blender
@pytest.mark.parametrize("appearance", ["clear-light", "tinted-light"])
def test_combined_body_keeps_internal_steps(worker, outdir, appearance):
    """Maps (one combined body: red / blue side by side, lightness 0.50 / 0.53) lost its internal edges in the
    clear / tinted renditions: the mono LUT keeps a visible lightness step between them."""
    import test_worker_quality as T
    left, right = T.rect(-0.5, -0.35, 0.0, 0.35), T.rect(0.0, -0.35, 0.5, 0.35)
    g = T.geo([("l", [left], "#ea4335", 1.0), ("r", [right], "#4285f4", 1.0)], safe=0.2)
    g["silhouette"] = [T.rect(-0.5, -0.35, 0.5, 0.35)]
    from PIL import Image                 # the layer texture (bis.svg rasterises the layer art: u=(x+1)/2)
    tex = np.zeros((64, 64, 4), np.uint8)
    tex[..., :3] = hex_rgb("#ea4335")
    tex[:, 32:, :3] = hex_rgb("#4285f4")
    tex[..., 3] = 255
    g["texturePath"] = str(outdir / "steps_tex.png")
    Image.fromarray(tex, "RGBA").save(g["texturePath"])
    lay = T.layer("A", shadow=0.0)
    lay["mode"] = "combined"
    proj, bundle = T.scene([lay], {"A": g}, plate_fill="#ffffff", color_mode="brand")
    out = outdir / f"steps_{appearance}.png"
    render(worker, (proj, bundle), out, "preview", appearance=appearance)
    a = rgba(out)
    ll = lab(mean_rgb(a, [(-0.25, y) for y in (-0.15, 0.0, 0.15)], r=3))[0]
    lr = lab(mean_rgb(a, [(0.25, y) for y in (-0.15, 0.0, 0.15)], r=3))[0]
    assert abs(ll - lr) >= 6.0, (ll, lr)


@needs_blender
def test_rim_is_a_thin_line_not_a_pale_band(worker, outdir):
    """Gmail / Canvas / Lens / Photos: a wide pale pink band on the bevels of saturated glyphs on white plates
    (red disc: bevel band dE 23-25 against the paint). The rim is a thin highlight now (band dE < 16)."""
    import test_worker_quality as T
    n = 192
    scn = T.scene([T.layer("A")], {"A": T.geo([("e", [T.circle(0.5)], "#ea4335", 1.0)], safe=0.3)},
                  plate_fill="#ffffff", color_mode="brand")
    out = outdir / "band.png"
    render(worker, scn, out, "preview", size=n)
    a = rgba(out)
    yy, xx = np.mgrid[0:n, 0:n] + 0.5
    r = np.hypot((xx - n / 2) / n * 2.24, (n / 2 - yy) / n * 2.24)
    band = (r > 0.5 - 0.045) & (r < 0.5 - 0.008)
    d = np.linalg.norm(lab(a[..., :3])[band] - lab(np.array(hex_rgb("#ea4335"), float)), axis=-1)
    assert d.mean() < 16.0, d.mean()


@needs_blender
def test_flush_art_has_no_white_hairline(worker, outdir):
    """Classroom / CRD / DJI: art flush with the plate outline drew the Liquid Glass rim as a white hairline round
    the icon. A frame along the plate edge now reads as part of the plate edge."""
    import test_worker_quality as T
    g = T.geo([("f", [T.circle(0.9995), T.circle(0.8, hole=True)], "#f4b400", 1.0)], safe=0.09)
    scn = T.scene([T.layer("A", shadow=0.0)], {"A": g}, plate_fill="#0f9d58", shape="circle", color_mode="brand")
    out = outdir / "flush.png"
    render(worker, scn, out, "preview", size=192)
    a = rgba(out)
    n = 192
    yy, xx = np.mgrid[0:n, 0:n] + 0.5
    r = np.hypot((xx - n / 2) / n * 2.24, (n / 2 - yy) / n * 2.24)
    outer = (r > 0.975) & (r < 0.995) & (a[..., 3] > 128)          # HEAD: +10 L* (the rim's white hairline)
    body = (r > 0.87) & (r < 0.92)
    lo, lb = lab(a[..., :3][outer]).mean(0)[0], lab(a[..., :3][body]).mean(0)[0]
    assert lo < lb + 4.0, (lo, lb)


# ------------------------------------------------------------------------------------------------ review (round 5)
def _mixed_layer(grad_op=0.5, solid_op=0.4, layer_op=1.0):
    """One Liquid Glass layer: a translucent SOLID disc (a film candidate) + a GRADIENT disc (never a film)."""
    import test_worker_quality as T
    g = T.geo([("s", [T.circle(0.25, cx=-0.45)], "#000000", solid_op), ("g", [T.circle(0.25, cx=0.45)], "#ff0000", grad_op)])
    g["regions"][1]["paint"] = {"type": "linear", "start": [0.2, 0.0], "end": [0.7, 0.0],
                                "stops": [{"offset": 0, "color": "#ff0000"}, {"offset": 1, "color": "#0000ff"}]}
    lay = T.layer("A", shadow=0.0)
    lay["opacity"] = layer_op
    return T.scene([lay], {"A": g}, plate_fill="#ffffff", color_mode="brand")


@needs_blender
@pytest.mark.parametrize("case", ["translucent_gradient", "layer_opacity"])
def test_film_layer_never_renders_a_non_film_piece_black(worker, outdir, case):
    """Reviewer: the layer material turned film on for every piece with object alpha < 1, but only solid pieces get
    bis_blend_t / _e — a translucent gradient piece in the same layer (or any piece once the layer opacity < 1)
    read (12, 12, 12). Such a layer keeps the alpha model; no film coefficients are left on its pieces."""
    scn = _mixed_layer() if case == "translucent_gradient" else _mixed_layer(grad_op=1.0, solid_op=1.0, layer_op=0.6)
    out = outdir / f"mixed_{case}.png"
    render(worker, scn, out, "preview")
    assert not worker.result("scene_info")["film"]
    a = rgba(out)
    grad = patch(a, 0.45, 0.0, 5)[..., :3].reshape(-1, 3).mean(axis=0)
    assert grad.min() > 120 and grad[1] < grad[0] - 15, grad.round()       # a pale purple over white, never black
    solid = patch(a, -0.45, 0.0, 5)[..., :3].reshape(-1, 3).mean(axis=0)
    assert 80 < solid.mean() < 200, solid.round()


@needs_blender
def test_touching_body_with_layer_opacity_is_translucent(worker, outdir):
    """Reviewer: touching opaque pieces render as one 'sil' body; with the layer opacity < 1 their regions became
    film candidates, the body got the film material without coefficients and rendered opaque black (4, 4, 4)."""
    import test_worker_quality as T
    from PIL import Image
    left, right = T.rect(-0.7, -0.25, -0.2, 0.25), T.rect(-0.2, -0.25, 0.3, 0.25)
    g = T.geo([("l", [left], "#000000", 1.0), ("r", [right], "#000000", 1.0)], safe=0.2)
    g["silhouette"] = [T.rect(-0.7, -0.25, 0.3, 0.25)]
    tex = np.zeros((64, 64, 4), np.uint8)
    tex[..., 3] = 255
    g["texturePath"] = str(outdir / "touch_tex.png")
    Image.fromarray(tex, "RGBA").save(g["texturePath"])
    lay = T.layer("A", shadow=0.0)
    lay["opacity"] = 0.6
    out = outdir / "touch_opacity.png"
    render(worker, T.scene([lay], {"A": g}, plate_fill="#ffffff", color_mode="brand"), out, "preview")
    got = patch(rgba(out), -0.45, 0.0, 5)[..., :3].reshape(-1, 3).mean(axis=0)
    assert de(got, [102, 102, 102]) < 12.0, got.round()                    # SVG: 60 % black over white


@needs_blender
@pytest.mark.parametrize("appearance", ["clear-light", "clear-dark"])
def test_clear_renditions_keep_dark_details(worker, outdir, appearance):
    """Reviewer: the round-5 clear floor 0.7 lifted every paint to a pale frosted white — Secure Folder's navy
    keyhole on its white folder went from |dL*| 52 / 67 (clear-light / clear-dark) to 19 / 30, Ti84's black body
    merged with its keys. Dark details keep a clear lightness step against the white glyph they sit on."""
    import test_worker_quality as T
    folder = T.layer("F", shadow=0.0)
    key = T.layer("K", z=0.13, shadow=0.0)
    geos = {"F": T.geo([("f", [T.rect(-0.55, -0.4, 0.55, 0.4)], "#ffffff", 1.0)], safe=0.3),
            "K": T.geo([("k", [T.circle(0.16)], "#1c2a63", 1.0)], safe=0.15)}
    proj, bundle = T.scene([folder, key], geos, plate_fill="#2f4fd0", color_mode="brand")
    out = outdir / f"keyhole_{appearance}.png"
    render(worker, (proj, bundle), out, "preview", appearance=appearance)
    a = rgba(out)
    lk = lab(patch(a, 0.0, 0.0, 4)[..., :3].reshape(-1, 3).mean(axis=0))[0]
    lf = lab(mean_rgb(a, [(-0.4, 0.25), (0.4, 0.25), (-0.4, -0.25), (0.4, -0.25)], r=4))[0]
    assert lf - lk >= 30.0, (lk, lf)
