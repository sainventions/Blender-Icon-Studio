"""Unit tests of the SVG pipeline building blocks."""
from __future__ import annotations

import math

import numpy as np
import pytest
from lxml import etree
from shapely.geometry import Point, box

from bis.svg import raster
from bis.svg.colors import color_name, delta_e, parse_color, to_hex
from bis.svg.common import ArtSpace
from bis.svg.css import parse_stylesheet, selector_matches
from bis.svg.geometry import path_to_splines, safe_radius, smooth_polyline, splines_to_d
from bis.svg.paths import islands, rect_path, skia_from_d
from bis.svg.prepass import auto_name, parse_filter_shadow, prepass
from bis.svg.textures import edge_pad
from picosvg.svg_transform import Affine2D


@pytest.mark.parametrize("s, rgba", [
    ("#f00", (1, 0, 0, 1)), ("#ff000080", (1, 0, 0, 128 / 255)), ("red", (1, 0, 0, 1)),
    ("rgb(255 0 0 / 50%)", (1, 0, 0, 0.5)), ("rgba(0,0,255,.25)", (0, 0, 1, 0.25)),
    ("hsl(120deg 100% 50%)", (0, 1, 0, 1)), ("transparent", (0, 0, 0, 0)),
])
def test_parse_color(s, rgba):
    assert parse_color(s) == pytest.approx(rgba, abs=1e-3)


def test_color_helpers():
    assert parse_color("nonsense") is None
    assert to_hex((1, 0.5, 0)) == "#ff8000"
    assert delta_e((1, 0, 0), (1, 0, 0)) == 0
    assert color_name((0.26, 0.52, 0.96)) == "Blue"
    assert color_name((1, 1, 1)) == "White"
    assert color_name((0.06, 0.06, 0.06)) == "Black"
    assert color_name((0.92, 0.26, 0.21)) == "Red"


def test_css_specificity_and_descendant():
    w = []
    rules = parse_stylesheet(".a{fill:red} #x{fill:blue} g .a{fill:green} @media print{.a{fill:pink}} a:hover{fill:x}", w)
    assert any("a:hover" in m for m in w)
    root = etree.fromstring('<svg><g><path id="x" class="a"/><path class="a"/></g></svg>')
    p1, p2 = root[0]
    decl = {}
    for comps, _spec, _o, d in rules:
        if selector_matches(p1, comps):
            decl.update(d)
    assert decl["fill"] == "blue"
    decl = {}
    for comps, _spec, _o, d in rules:
        if selector_matches(p2, comps):
            decl.update(d)
    assert decl["fill"] == "green"


def test_auto_names():
    assert auto_name("g12") and auto_name("Layer_1") and auto_name("a") and auto_name("clip0_1_2")
    assert not auto_name("Mountains") and not auto_name("flame-core")


def _filter(xml: str):
    return etree.fromstring(f'<filter xmlns="http://www.w3.org/2000/svg">{xml}</filter>')


def test_filter_shadow_variants():
    ill = _filter('<feOffset dx="3" dy="4"/><feGaussianBlur result="d" stdDeviation="23"/>'
                  '<feFlood flood-color="#000" flood-opacity=".3"/><feComposite in2="d" operator="in"/>'
                  '<feComposite in="SourceGraphic"/>')
    sh, prob = parse_filter_shadow(ill)
    assert prob is None and sh == {"dx": 3.0, "dy": 4.0, "blur": 23.0, "opacity": 0.3, "color": "#000000"}
    ds, _ = parse_filter_shadow(_filter('<feDropShadow dx="1" dy="2" stdDeviation="3" flood-color="rgba(255,0,0,.5)"/>'))
    assert ds["color"] == "#ff0000" and ds["opacity"] == pytest.approx(0.5) and ds["blur"] == 3
    figma = _filter('<feFlood flood-opacity="0" result="BackgroundImageFix"/>'
                    '<feColorMatrix in="SourceAlpha" type="matrix" values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 127 0"/>'
                    '<feOffset dy="4"/><feGaussianBlur stdDeviation="2"/>'
                    '<feColorMatrix type="matrix" values="0 0 0 0 0.1 0 0 0 0 0.2 0 0 0 0 0.3 0 0 0 0.25 0"/>'
                    '<feBlend in2="BackgroundImageFix"/>')
    fs, _ = parse_filter_shadow(figma)
    assert fs["dy"] == 4 and fs["blur"] == 2 and fs["opacity"] == pytest.approx(0.25) and fs["color"] == "#1a334c"
    blur_only, prob = parse_filter_shadow(_filter('<feGaussianBlur stdDeviation="5"/>'))
    assert blur_only is None and "not a drop shadow" in prob


def test_prepass_shadow_and_image_placeholder():
    png = raster.encode_png(np.full((4, 8, 4), 255, np.uint8))
    import base64
    uri = "data:image/png;base64," + base64.b64encode(png).decode()
    src = f'''<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 100 100">
      <defs><filter id="f"><feOffset dx="2" dy="2"/><feGaussianBlur stdDeviation="4"/><feFlood flood-opacity=".5"/>
      <feComposite operator="in" in2="SourceGraphic"/></filter></defs>
      <g filter="url(#f)" transform="scale(2)"><rect width="10" height="10" fill="red"/>
      <image x="10" y="10" width="20" height="20" xlink:href="{uri}"/></g></svg>'''
    pre = prepass(src)
    shapes = [m for m in pre.meta.values() if m["role"] == "fill"]
    assert shapes[0]["shadow"]["dx"] == pytest.approx(4) and shapes[0]["shadow"]["blur"] == pytest.approx(8)
    (img,) = pre.images.values()
    # 8×4 px image into a 20×20 box, xMidYMid meet -> 20×10 centred at y=15, then scale(2)
    a, b, c, d, e, f = img.matrix
    assert (a, d) == pytest.approx((5.0, 5.0)) and (e, f) == pytest.approx((20.0, 30.0))
    assert f'id="{img.uid}"' in pre.svg_text


def test_smoothing_circle_and_square():
    n = 64
    circle = [(math.cos(2 * math.pi * k / n), math.sin(2 * math.pi * k / n)) for k in range(n)]
    pts = smooth_polyline(circle)
    # sample the curve: radius stays within 0.1 % of 1 (the polyline itself sags 0.12 %)
    worst = 0.0
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        for t in (0.25, 0.5, 0.75):
            mt = 1 - t
            x = mt ** 3 * a["co"][0] + 3 * mt * mt * t * a["hr"][0] + 3 * mt * t * t * b["hl"][0] + t ** 3 * b["co"][0]
            y = mt ** 3 * a["co"][1] + 3 * mt * mt * t * a["hr"][1] + 3 * mt * t * t * b["hl"][1] + t ** 3 * b["co"][1]
            worst = max(worst, abs(math.hypot(x, y) - 1))
    assert worst < 1e-3
    # a dense square keeps its corners and straight sides
    side = [(i / 10, 0) for i in range(10)] + [(1, i / 10) for i in range(10)] + \
           [(1 - i / 10, 1) for i in range(10)] + [(0, 1 - i / 10) for i in range(10)]
    sq = smooth_polyline(side)
    for p in sq:
        for h in ("hl", "hr"):
            assert min(abs(p[h][0]), abs(p[h][0] - 1), abs(p[h][1]), abs(p[h][1] - 1)) < 1e-9


def test_long_edges_stay_straight():
    # a long edge followed by a dense arc: the long edge must stay exactly straight
    arc = [(1 + 0.2 * math.cos(math.pi / 2 - k * math.pi / 20), 0.2 * math.sin(math.pi / 2 - k * math.pi / 20) - 0.2)
           for k in range(21)]
    poly = [(0, 0)] + arc + [(0, -0.4)]
    sp = smooth_polyline(poly)
    p0, p1 = sp[0], sp[1]  # segment (0,0) -> (1,0)
    assert abs(p0["hr"][1]) < 1e-9 and abs(p1["hl"][1]) < 1e-9


def test_path_to_splines_exact_for_curves():
    sk = skia_from_d("M10 10 C 20 0 40 0 50 10 Q 60 30 30 40 L 10 40 Z")
    spl = path_to_splines(sk, Affine2D.identity(), smooth=False)
    assert len(spl) == 1 and spl[0]["closed"] and len(spl[0]["points"]) == 4  # C, Q, L + closing line
    assert spl[0]["points"][0]["hr"] == (20.0, 0.0)


def test_safe_radius():
    assert safe_radius([box(0, 0, 1, 0.2)]) == pytest.approx(0.1, rel=0.08)
    assert safe_radius([Point(0, 0).buffer(0.5, 64)]) == pytest.approx(0.5, rel=0.02)
    thin = box(0, 0, 1, 1).union(box(1, 0.45, 1.9, 0.5))  # big square + a long thin bar (0.05 wide, 4 % area)
    assert 0.0 < safe_radius([thin]) < 0.026
    # a tiny hairline (< 2 % of the area) is tolerated, plain corners never limit the radius
    assert safe_radius([box(0, 0, 1, 1).union(box(1, 0.5, 1.5, 0.505))]) > 0.3
    assert safe_radius([]) == 0.0


def test_islands():
    sk = skia_from_d("M0 0h10v10h-10z M2 2v6h6v-6z M4 4h2v2h-2z M20 0h5v5h-5z")
    parts = islands(sk, 0.01)
    assert len(parts) == 3  # frame (with hole), island inside the hole, separate square


def test_edge_pad_nearest_colour_keeps_alpha():
    img = np.zeros((32, 32, 4), np.uint8)
    img[:, :16] = (255, 0, 0, 255)
    img[:, 16:20] = (0, 0, 255, 60)   # faint pixels get recoloured, alpha kept
    out = edge_pad(img)
    assert (out[..., 3] == img[..., 3]).all()
    assert (out[:, 20:, :3] == (255, 0, 0)).all()
    assert (out[:, 16:20, :3] == (255, 0, 0)).all()
    empty = edge_pad(np.zeros((8, 8, 4), np.uint8))
    assert (empty[..., 3] == 0).all()


def test_mask_to_path_area():
    m = np.zeros((100, 100), np.uint8)
    m[20:80, 30:70] = 1
    m[40:60, 45:55] = 0
    p = raster.mask_to_path(m, Affine2D(2, 0, 0, 2, 0, 0))
    assert abs(p.area) == pytest.approx(4 * (60 * 40 - 20 * 10), rel=0.05)


def test_matte_background():
    rgba = np.zeros((200, 200, 4), np.uint8)
    rgba[...] = (40, 80, 160, 255)
    rgba[60:140, 60:140] = (250, 250, 250, 255)
    res = raster.matte_background(rgba, (1, 0, 0, 1, 0, 0), rect_path(0, 0, 200, 200))
    assert res is not None
    bg, fg = res
    assert to_hex(bg) == "#2850a0"
    assert abs(fg.area) == pytest.approx(80 * 80, rel=0.05)


def test_art_space():
    art = ArtSpace((100.0, 200.0, 64.0, 32.0))
    assert art.k == pytest.approx(2 / 64)
    assert art.pt((132, 216)) == (0.0, 0.0)
    assert art.pt((100, 200)) == pytest.approx((-1.0, 0.5))
    assert art.bbox((100, 200, 164, 232)) == pytest.approx((-1, -0.5, 1, 0.5))
    assert art.square_view_box() == (100.0, 184.0, 64.0, 64.0)


def test_splines_to_d_roundtrip():
    sk = skia_from_d("M0 0 L 10 0 L 10 10 Z")
    d = splines_to_d(path_to_splines(sk, Affine2D.identity(), smooth=False))
    assert d.startswith("M0 0 C") and d.endswith("Z")


# ----------------------------------------------------------------------------------------------
# robustness (review fixes)
# ----------------------------------------------------------------------------------------------
def test_recursive_use_is_dropped_not_exploded():
    import time

    selfref = ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
               'viewBox="0 0 100 100"><g id="g1"><use xlink:href="#g1"/><rect width="10" height="10"/></g></svg>')
    cycle = ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
             'viewBox="0 0 100 100"><defs><g id="A"><rect width="5" height="5"/><use href="#B"/></g>'
             '<g id="B"><circle cx="50" cy="50" r="5"/><use href="#A"/></g></defs><use href="#A"/></svg>')
    for src, n_shapes in ((selfref, 1), (cycle, 2)):
        t = time.perf_counter()
        pre = prepass(src)
        assert time.perf_counter() - t < 2.0
        assert any("recursive" in w for w in pre.warnings)
        assert len([m for m in pre.meta.values() if m["role"] == "fill"]) == n_shapes
    # a legit symbol reused inside a reused group still expands
    nested = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><defs>'
              '<circle id="dot" r="4"/><g id="pair"><use href="#dot" x="10"/><use href="#dot" x="30"/></g></defs>'
              '<use href="#pair" y="10"/><use href="#pair" y="50"/></svg>')
    pre = prepass(nested)
    assert len([m for m in pre.meta.values() if m["role"] == "fill"]) == 4 and not pre.warnings


def test_doctype_internal_entities_are_expanded(tmp_path):
    import bis.svg as svg

    src = b'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd" [
    <!ENTITY ns_svg "http://www.w3.org/2000/svg">
    <!ENTITY ns_xlink "http://www.w3.org/1999/xlink">
    <!ENTITY st0 "fill:#FF0000;">
]>
<svg version="1.1" xmlns="&ns_svg;" xmlns:xlink="&ns_xlink;" viewBox="0 0 100 100">
<rect x="10" y="10" width="30" height="30" style="&st0;"/><rect x="50" y="50" width="30" height="30"/></svg>'''
    res = svg.import_svg(src, "ai.svg", tmp_path / "ai")
    assert [e.paint.color for e in res.elements] == ["#ff0000", "#000000"]
    # no external / parameter entity resolution, ever
    xxe = b'''<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///C:/Windows/win.ini">]>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="5" height="5"/>&x;</svg>'''
    with pytest.raises(ValueError):
        svg.import_svg(xxe, "x.svg", tmp_path / "x")


def test_invalid_and_css_transforms(tmp_path):
    import bis.svg as svg

    src = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
      <rect x="10" y="10" width="10" height="10" transform="matrix(1 0 0 1 nan 0)"/>
      <rect x="0" y="0" width="10" height="10" style="transform: translate(50px, 50px) rotate(0deg)"/>
      <g transform="scale(0)"><rect width="10" height="10"/></g></svg>'''
    res = svg.import_svg(src, "t.svg", tmp_path / "t")
    assert any("invalid transform" in w for w in res.warnings)
    boxes = sorted(tuple(round(v, 3) for v in e.bbox) for e in res.elements)
    assert (-0.8, 0.6, -0.6, 0.8) in boxes   # nan transform dropped -> drawn untransformed
    assert (0.0, -0.2, 0.2, 0.0) in boxes     # CSS translate(50px, 50px) honoured


def test_degenerate_gradients_paint_last_stop():
    from bis.svg.elements import model_paint

    art = ArtSpace((0.0, 0.0, 100.0, 100.0))
    stops = [{"offset": 0.0, "hex": "#ff0000", "opacity": 1.0}, {"offset": 1.0, "hex": "#0000ff", "opacity": 0.5}]
    lin = {"type": "linear", "coords": {"x1": 5.0, "y1": 5.0, "x2": 5.0, "y2": 5.0}, "transform": [1, 0, 0, 1, 0, 0],
           "stops": stops}
    rad = {"type": "radial", "coords": {"cx": 5.0, "cy": 5.0, "r": 0.0}, "transform": [1, 0, 0, 1, 0, 0], "stops": stops}
    sing = {**lin, "coords": {"x1": 0.0, "y1": 0.0, "x2": 10.0, "y2": 0.0}, "transform": [0, 0, 0, 0, 0, 0]}
    for p in (lin, rad, sing):
        f = model_paint(p, art, 0.8)
        assert f.type == "solid" and f.color == "#0000ff" and f.opacity == pytest.approx(0.4)
    ok = model_paint({**lin, "coords": {"x1": 0.0, "y1": 0.0, "x2": 100.0, "y2": 0.0}}, art)
    assert ok.type == "linear" and ok.start == pytest.approx((-1.0, 0.0)) and ok.end == pytest.approx((1.0, 0.0))


def test_hole_annotation_exact_on_thin_rings():
    import pathops
    from bis.svg.geometry import annotate_holes
    from bis.svg.paths import op

    def circ(cx, cy, r):
        k = 0.5523 * r
        return (f"M{cx + r} {cy} C{cx + r} {cy + k} {cx + k} {cy + r} {cx} {cy + r} C{cx - k} {cy + r} {cx - r} "
                f"{cy + k} {cx - r} {cy} C{cx - r} {cy - k} {cx - k} {cy - r} {cx} {cy - r} C{cx + k} {cy - r} "
                f"{cx + r} {cy - k} {cx + r} {cy} Z")

    a = math.radians(11.25)  # inner circle parameterised off-phase: sampled polygons misjudge it
    for width in (2.0, 0.5):
        inner = skia_from_d(circ(0, 0, 200 - width)).transform(math.cos(a), math.sin(a), -math.sin(a),
                                                               math.cos(a), 250, 250)
        ring = op(skia_from_d(circ(250, 250, 200)), inner, pathops.PathOp.DIFFERENCE)
        spl = path_to_splines(ring, Affine2D(2 / 500, 0, 0, -2 / 500, -1, 1))
        annotate_holes(spl)
        assert [(s["hole"], s["parent"], s["depth"]) for s in spl] == [(False, -1, 0), (True, 0, 1)], width
