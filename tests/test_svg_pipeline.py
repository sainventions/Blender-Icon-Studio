"""End-to-end tests of the SVG pipeline (bis.svg) on the research test icons and selected corpus
icons: import, plate/canvas mapping (D9), art space (D2), shadows, raster images, default layer
stack, geometry bundle (splines, regions, safe radius, textures) and fidelity."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import numpy as np
import pytest

from conftest import CORPUS_DIR, SVGTESTS_DIR

import bis.svg as svg
from bis.models import GeometryBundle, Project
from bis.svg import raster
from bis.svg.geometry import splines_to_d

SVGTESTS = sorted(SVGTESTS_DIR.glob("*.svg"))
STRATEGIES = ["smart", "group", "color", "element", "single"]
K500 = 2.0 / 500.0  # art units per SVG unit for the 500×500 corpus


def corpus(name: str) -> Path:
    return CORPUS_DIR / f"{name}.svg"


def recompose(pdir: Path, bundle: GeometryBundle, project: Project, w: int, h: int) -> np.ndarray:
    imgs = []
    if bundle.plate:
        imgs.append(raster.render_svg(Path(bundle.plate["svgPath"]).read_text(encoding="utf-8"), w, h))
    for L in project.layers:
        lg = bundle.layers[L.id]
        imgs.append(raster.render_svg((pdir / "cache" / Path(lg.svg).name).read_text(encoding="utf-8"), w, h))
    return raster.composite(imgs) if imgs else np.zeros((h, w, 4), np.uint8)


def layer_partition_ok(res) -> None:
    ids = [e.id for e in res.elements]
    in_layers = [i for L in res.layers for i in L.elementIds]
    assert len(in_layers) == len(set(in_layers)), "an element is in two layers"
    assert set(in_layers) <= set(ids)
    plate_ids = set(ids) - set(in_layers)
    if res.source.plateDetected:
        assert plate_ids, "plate detected but every element is in a layer"
    else:
        assert not plate_ids, f"elements missing from layers: {plate_ids}"


# ----------------------------------------------------------------------------------------------
# research test icons × strategies
# ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("path", SVGTESTS, ids=lambda p: p.stem)
@pytest.mark.parametrize("strategy", STRATEGIES)
def test_svgtests_import_split_and_recompose(path, strategy, import_icon):
    res, project, pdir = import_icon(path, strategy)
    layer_partition_ok(res)
    assert [L.id for L in res.layers] == [f"L{i + 1}" for i in range(len(res.layers))]
    for e in res.elements:
        assert re.fullmatch(r"(e|img)\d+", e.id)
        assert all(-1.0001 <= v <= 1.0001 for v in e.bbox), (e.id, e.bbox)
    # the project round-trips through JSON (contract with web/src/types.ts)
    assert Project.model_validate_json(project.model_dump_json()) == project
    bundle = svg.build_geometry(pdir, project, "/files/projects/p", texture_size=128)
    assert set(bundle.layers) == {L.id for L in project.layers}
    vb = res.source.viewBox
    w = 192
    h = max(1, int(round(w * vb[3] / vb[2])))
    normalized = raster.render_svg((pdir / "normalized.svg").read_text(encoding="utf-8"), w, h)
    assert raster.diff_pct(normalized, recompose(pdir, bundle, project, w, h)) <= 1.0
    if path.stem != "k_unsupported":  # k drops <text>/<mask>/<pattern> by design
        src = raster.render_svg(raster.strip_filters(path.read_text(encoding="utf-8")), w, h)
        assert raster.diff_pct(src, normalized) <= 1.0


def test_smart_split_matches_author_groups(import_icon):
    res, _p, _d = import_icon(SVGTESTS_DIR / "a_app_multilayer.svg")
    names = [L.name for L in res.layers]
    assert res.source.plateDetected
    assert "Sun" in names and "Ground" in names and "Cloud puffs" in names
    res, _p, _d = import_icon(SVGTESTS_DIR / "j_inkscape_layers.svg")
    assert res.source.plateDetected and [L.name for L in res.layers] == ["Face", "Shine"]


def test_single_glyph_is_exploded_into_islands(import_icon):
    res, _p, _d = import_icon(SVGTESTS_DIR / "l_single_path_glyph.svg")
    assert len(res.elements) == 4 and len(res.layers) >= 2


# ----------------------------------------------------------------------------------------------
# corpus specifics
# ----------------------------------------------------------------------------------------------
def test_plate_to_canvas_mapping_d9(import_icon):
    res, project, pdir = import_icon(corpus("Maps"))
    c = res.canvas
    assert res.source.plateDetected and c.plate.visible
    assert c.shape in ("rounded", "squircle")
    # corpus plate bbox 17..483 -> art width 466·k -> scale 2/that
    assert c.art.scale == pytest.approx(2.0 / (466.002 * K500), rel=1e-3)
    assert abs(c.art.x) < 1e-6 and abs(c.art.y) < 1e-6
    assert str(c.art.x) != "-0.0"
    # plate fill is the source gradient, in CANVAS coordinates (source points 364,25.6 -> 135.7,474.4)
    f = c.plate.fill
    assert f.type == "linear"
    s = c.art.scale
    t0 = ((364.376 - 250) * K500 * s, -(25.611 - 250) * K500 * s)
    t1 = ((135.709 - 250) * K500 * s, -(474.395 - 250) * K500 * s)
    d = np.subtract(t1, t0)
    # same gradient line (start/end may slide along the iso-lines, so compare t() at both ends)
    u = np.subtract(f.end, f.start)
    tt = lambda p: float(np.dot(np.subtract(p, f.start), u) / np.dot(u, u))  # noqa: E731
    assert tt(t0) == pytest.approx(0.0, abs=1e-4) and tt(t1) == pytest.approx(1.0, abs=1e-4)
    assert [st.color for st in f.stops] == ["#ffffff", "#ebebeb"]
    assert np.linalg.norm(d) > 0


def test_plate_shape_classifier_and_corner_radius(import_icon):
    res, _p, pdir = import_icon(corpus("Settings"))
    store = svg.read_store(pdir)
    assert store.plate["iou"] > 0.98
    if res.canvas.shape == "rounded":
        assert 0.3 < res.canvas.cornerRadius < 0.5


def test_offcanvas_junk_is_culled(import_icon):
    for name in ("Calendar", "Docs", "Health", "Mail"):
        res, _p, _d = import_icon(corpus(name))
        assert res.source.plateDetected, name
        assert all(e.paint.color != "#8a8a8a" for e in res.elements if e.paint.type == "solid"), name
        assert any("outside the viewBox" in w for w in res.warnings), name


def test_drop_shadow_filter_parsed_into_art_units(import_icon):
    res, _p, _d = import_icon(corpus("Maps"))
    fg = [e for e in res.elements if e.shadow]
    assert fg, "corpus drop shadow not parsed"
    sh = fg[0].shadow
    assert sh.blur == pytest.approx(23 * K500, abs=1e-6)
    assert sh.opacity == pytest.approx(0.3) and sh.dx == 0 and sh.dy == 0 and sh.color == "#000000"
    assert all(L.shadow.kind == "neutral" and L.shadow.opacity == pytest.approx(0.5) for L in res.layers)
    # iMessage: offset shadow dx=7 dy=15 (y down) σ=11 @ .36 on an <image transform="scale(.977)">
    res, _p, _d = import_icon(corpus("iMessage"))
    sh = next(e.shadow for e in res.elements if e.shadow)
    s = 0.977  # filter units = the image's user space, which includes its transform
    assert (sh.dx, sh.dy, sh.blur) == pytest.approx((7 * K500 * s, -15 * K500 * s, 11 * K500 * s), abs=1e-6)
    assert sh.opacity == pytest.approx(0.36)
    assert res.layers[0].shadow.opacity == pytest.approx(0.6)


def test_fill_plus_stroke_plate(import_icon):
    res, project, _d = import_icon(corpus("Syno Photos"))
    assert res.source.plateDetected
    in_layers = {i for L in res.layers for i in L.elementIds}
    plate = [e for e in res.elements if e.id not in in_layers]
    assert {e.role for e in plate} == {"fill", "stroke"}
    assert res.canvas.plate.fill.type == "linear"


def test_plate_only_and_no_plate(import_icon):
    res, _p, _d = import_icon(corpus("Template"))
    assert res.source.plateDetected and res.layers == []
    assert res.canvas.plate.fill.type == "solid" and res.canvas.plate.fill.color == "#ff00ff"
    # Earth has no plate element, but its art fills the plate shape edge to edge (full-bleed): the
    # canvas is framed to the art's outline with a plate in the art's rim colour (QA round 2, #5).
    # A real no-plate icon keeps the System Light plate at 0.78 (test_svg_geometry).
    res, _p, _d = import_icon(corpus("Earth"))
    assert not res.source.plateDetected
    assert res.canvas.plate.visible and res.canvas.plate.fill.type == "solid"
    assert res.canvas.art.scale == pytest.approx(2.0 / (466.0 * K500), rel=0.01)


def test_default_layer_stack(import_icon):
    res, project, pdir = import_icon(corpus("Photos"))
    bundle = svg.build_geometry(pdir, project, "/files/projects/p", texture_size=128)
    for i, L in enumerate(res.layers):
        assert L.depth.z == pytest.approx(i * 0.13)
        assert L.depth.thickness == pytest.approx(0.10)
        assert L.material.preset == "liquid_glass" and L.mode == "individual"
        sr = bundle.layers[L.id].safeRadius
        assert L.depth.bevel == pytest.approx(min(0.045, 0.9 * sr), abs=2e-4)
    assert res.canvas.plate.material.preset == "satin"
    assert res.canvas.plate.thickness == pytest.approx(0.16) and res.canvas.plate.bevel == pytest.approx(0.04)


def test_thin_features_clamp_bevel(import_icon):
    res, project, pdir = import_icon(corpus("Ti84"))
    bundle = svg.build_geometry(pdir, project, "/files/projects/p", texture_size=128)
    for L in res.layers:   # every default bevel fits its layer's (mode-dependent) safe radius
        assert L.depth.bevel <= 0.9 * bundle.layers[L.id].safeRadius + 1e-4
    # built piece by piece, the thin display text / graph lines clamp the bevel hard ('combined'
    # layers - Ti84's tiled screen + keypad face since round 4 - are built from their silhouette)
    for L in project.layers:
        L.mode = "individual"
    bundle = svg.build_geometry(pdir, project, "/files/projects/p", texture_size=128)
    srs = [lg.safeRadius for lg in bundle.layers.values()]
    assert min(srs) < 0.02


# ----------------------------------------------------------------------------------------------
# raster images
# ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["Feit", "Find Device", "Outlook", "Vanced Neon", "iMessage"])
def test_raster_icons_keep_images(name, import_icon):
    res, project, pdir = import_icon(corpus(name))
    imgs = [e for e in res.elements if e.kind == "image"]
    assert imgs and all(e.role == "image" and e.id.startswith("img") for e in imgs)
    assert res.source.plateDetected
    assert all((pdir / "images").glob(f"{e.id}.*") for e in imgs)
    bundle = svg.build_geometry(pdir, project, "/files/projects/p", texture_size=256)
    layer_of = {i: L.id for L in project.layers for i in L.elementIds}
    for e in imgs:
        lg = bundle.layers[layer_of[e.id]]
        assert lg.silhouette, "image silhouette (alpha contours) missing"
        card = next(c for c in lg.images if c["elementId"] == e.id)
        assert Path(card["path"]).exists() and card["url"].startswith("/files/projects/p/images/")
        assert "<image" in (pdir / "cache" / Path(lg.svg).name).read_text(encoding="utf-8")
        tex = raster.decode_rgba(Path(lg.texturePath).read_bytes())
        assert tex.shape == (256, 256, 4) and tex[..., 3].max() > 0
    vb = res.source.viewBox
    normalized = raster.render_svg((pdir / "normalized.svg").read_text(encoding="utf-8"), 160, 160)
    assert raster.diff_pct(normalized, recompose(pdir, bundle, project, 160, 160)) <= 1.0
    src = raster.render_svg(raster.strip_filters(corpus(name).read_text(encoding="utf-8")), 160, 160)
    assert raster.diff_pct(src, normalized) <= (3.0 if name == "Feit" else 0.5)


def test_feit_matte_background_becomes_plate(import_icon):
    res, project, pdir = import_icon(corpus("Feit"))
    assert res.source.plateDetected and res.canvas.plate.fill.type == "solid"
    r, g, b = (int(res.canvas.plate.fill.color[i:i + 2], 16) for i in (1, 3, 5))
    assert b > r and b > g  # the blue background of the PNG
    assert len(res.layers) == 1 and res.layers[0].elementIds == ["img0"]
    assert any("raster-only icon" in w for w in res.warnings)


# ----------------------------------------------------------------------------------------------
# geometry bundle
# ----------------------------------------------------------------------------------------------
def _spline_mask(splines, size):
    d = splines_to_d(splines)
    s = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="-1 -1 2 2" width="{size}" height="{size}">'
         f'<path transform="scale(1 -1)" fill="#000" d="{d}"/></svg>')
    return raster.render_svg(s, size, size)[..., 3].astype(int)


@pytest.mark.parametrize("name", ["Maps", "Spotify", "Discord", "Weatherbug", "Feit", "Canvas"])
def test_splines_round_trip_layer_masks(name, import_icon):
    from bis.svg import textures

    res, project, pdir = import_icon(corpus(name))
    bundle = svg.build_geometry(pdir, project, "/files/projects/p", texture_size=128)
    store = svg.read_store(pdir)
    sq = store.art.square_view_box()
    size = 256
    for L in project.layers:
        lg = bundle.layers[L.id]
        mask_svg = textures.layer_svg([store.get(i) for i in L.elementIds], store.gradients, sq, None,
                                      size=(size, size), silhouette="#000")
        alpha = raster.render_svg(mask_svg, size, size)[..., 3].astype(int)
        err = float((np.abs(_spline_mask(lg.silhouette, size) - alpha) > 64).mean() * 100)
        assert err <= 0.3, (L.id, err)
        # regions are disjoint pieces whose union is the silhouette
        reg = np.zeros((size, size), int)
        for r in lg.regions:
            reg = np.maximum(reg, _spline_mask(r.splines, size))
        assert float((np.abs(reg - alpha) > 64).mean() * 100) <= 0.3


def test_hole_annotation(import_icon):
    res, project, pdir = import_icon(corpus("Discord"))
    bundle = svg.build_geometry(pdir, project, "/files/projects/p", texture_size=128)
    sil = bundle.layers[project.layers[0].id].silhouette
    holes = [s for s in sil if s.hole]
    outers = [s for s in sil if not s.hole]
    assert len(holes) >= 2 and outers  # the two eyes
    assert all(s.parent == -1 and s.depth % 2 == 0 for s in outers)
    assert all(0 <= s.parent < len(sil) and not sil[s.parent].hole for s in holes)


def test_translucent_overlaps_get_zsub(import_icon):
    res, project, pdir = import_icon(SVGTESTS_DIR / "f_opacity.svg", "single")
    bundle = svg.build_geometry(pdir, project, "/files/projects/p", texture_size=128)
    zs = [r.zSub for lg in bundle.layers.values() for r in lg.regions]
    assert max(zs) > 0 and all(z >= 0 for z in zs)


def test_bundle_files_urls_and_texture(import_icon):
    res, project, pdir = import_icon(corpus("Maps"))
    bundle = svg.build_geometry(pdir, project, "/files/projects/p1")
    path = svg.geometry_path(pdir, bundle)
    assert path.exists() and path.parent == pdir / "cache"
    assert GeometryBundle.model_validate_json(path.read_text(encoding="utf-8")) == bundle
    assert bundle.plate and bundle.plate["shape"] == res.canvas.shape
    for L in project.layers:
        lg = bundle.layers[L.id]
        assert lg.texture.startswith("/files/projects/p1/cache/tex-") and lg.texture.endswith(".png")
        assert lg.svg.startswith("/files/projects/p1/cache/layer-")
        tex = raster.decode_rgba(Path(lg.texturePath).read_bytes())
        assert tex.shape == (2048, 2048, 4)
        a = tex[..., 3]
        assert (a == 0).any() and (a == 255).any()
        # edge padding: transparent pixels carry the layer colour, not black
        rgb_t = tex[..., :3][a == 0].astype(int)
        rgb_o = tex[..., :3][a == 255].astype(int)
        assert np.abs(rgb_t.mean(axis=0) - rgb_o.mean(axis=0)).max() < 40
        x0, y0, x1, y1 = lg.bbox
        assert -1 <= x0 < x1 <= 1 and -1 <= y0 < y1 <= 1


def test_gradients_are_exact_in_art_space(import_icon):
    """Render every gradient element twice - from the layer SVG (SVG units) and from the model
    paint (ART space, incl. radial matrices) - and compare (validates the ×2 art space, D2)."""
    from bis.svg import textures
    from bis.svg.paths import clean_d, transform

    res, project, pdir = import_icon(SVGTESTS_DIR / "d_gradients.svg", "element")
    store = svg.read_store(pdir)
    sq = store.art.square_view_box()
    size = 192
    by_id = {e.id: e for e in res.elements}
    checked = 0
    for e in store.elems:
        p = by_id[e.id].paint
        if p.type not in ("linear", "radial"):
            continue
        ref = raster.render_svg(textures.layer_svg([e], store.gradients, sq, None, size=(size, size)), size, size)
        stops = "".join(f'<stop offset="{s.offset}" stop-color="{s.color}" stop-opacity="{s.opacity}"/>' for s in p.stops)
        if p.type == "linear":
            grad = (f'<linearGradient id="g" gradientUnits="userSpaceOnUse" x1="{p.start[0]}" y1="{p.start[1]}" '
                    f'x2="{p.end[0]}" y2="{p.end[1]}">{stops}</linearGradient>')
        else:
            fx, fy = p.focal or p.center
            tf = f' gradientTransform="matrix({" ".join(str(v) for v in p.matrix)})"' if p.matrix else ""
            grad = (f'<radialGradient id="g" gradientUnits="userSpaceOnUse" cx="{p.center[0]}" cy="{p.center[1]}" '
                    f'r="{p.radius}" fx="{fx}" fy="{fy}"{tf}>{stops}</radialGradient>')
        # the path itself must be in art coordinates: userSpaceOnUse = the path's own user space
        d_art = clean_d(transform(e.path, store.art.matrix), 6)
        test = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="-1 -1 2 2" width="{size}" height="{size}">'
                f'<defs>{grad}</defs><g transform="scale(1 -1)"><path '
                f'd="{d_art}" fill="url(#g)" opacity="{e.opacity}"/></g></svg>')
        out = raster.render_svg(test, size, size)
        assert raster.diff_pct(ref, out, thr=12) <= 0.5, e.id
        checked += 1
    assert checked >= 3


def test_layer_and_icon_thumbnails(import_icon):
    res, project, pdir = import_icon(corpus("Maps"))
    png = svg.layer_thumbnail_png(pdir, project, project.layers[0].id, 96)
    img = raster.decode_rgba(png)
    assert img.shape == (96, 96, 4) and img[..., 3].max() > 0
    with pytest.raises(ValueError):
        svg.layer_thumbnail_png(pdir, project, "nope")
    th = raster.decode_rgba(svg.thumbnail_png(corpus("Maps").read_bytes(), 256))
    assert th.shape == (256, 256, 4)
    # Outlook's Illustrator filter region hides the PNG in strict renderers: thumbnail falls back
    th = raster.decode_rgba(svg.thumbnail_png(corpus("Outlook").read_bytes(), 128))
    blue = (th[..., 2] > 180) & (th[..., 0] < 80) & (th[..., 3] > 200)
    assert blue.mean() > 0.05
    # non-square viewBox is letterboxed into a square
    th = raster.decode_rgba(svg.thumbnail_png((SVGTESTS_DIR / "g_offset_viewbox.svg").read_bytes(), 64))
    assert th.shape == (64, 64, 4) and th[0, :, 3].max() == 0


def test_bad_input_raises_value_error(tmp_path):
    with pytest.raises(ValueError):
        svg.import_svg(b"not xml at all", "x.svg", tmp_path / "a")
    res = svg.import_svg(b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"/>', "e.svg", tmp_path / "b")
    assert res.elements == [] and res.layers == [] and not res.source.plateDetected


def test_svgz_and_encodings(tmp_path):
    import gzip

    data = (SVGTESTS_DIR / "c_holes_evenodd.svg").read_bytes()
    res = svg.import_svg(gzip.compress(data), "c.svgz", tmp_path / "z")
    assert res.elements
    utf16 = data.decode("utf-8").replace('encoding="UTF-8"', 'encoding="UTF-16"').encode("utf-16")
    res2 = svg.import_svg(utf16, "c16.svg", tmp_path / "u")
    assert len(res2.elements) == len(res.elements)


def test_elements_store_is_self_contained(import_icon):
    res, project, pdir = import_icon(corpus("Maps"))
    j = json.loads((pdir / "elements.json").read_text(encoding="utf-8"))
    assert j["format"] == "bis-elements" and len(j["elements"]) == len(res.elements)
    assert (pdir / "source.svg").read_bytes() == corpus("Maps").read_bytes()
    assert (pdir / "normalized.svg").exists()
    # re-split never re-parses the SVG: works even with the sources gone
    (pdir / "source.svg").unlink()
    (pdir / "normalized.svg").unlink()
    layers = svg.split_layers(pdir, project, "element")
    fg = sum(len(L.elementIds) for L in res.layers)
    assert sum(len(L.elementIds) for L in layers) == fg and len(layers) == fg == 5


def test_smart_split_semantics_on_corpus(import_icon):
    # monochrome rings: big petals vs small dots (similar size + stacking level), not by proximity
    res, _p, _d = import_icon(corpus("Canvas"))
    assert sorted(len(L.elementIds) for L in res.layers) == [8, 8]
    # shading details sitting on a shape of the same hue stay with it
    res, _p, _d = import_icon(corpus("Gmail"))
    assert len(res.layers) == 1
    # a calculator: body, keys and screen content end up on separate planes (max 4)
    res, _p, _d = import_icon(corpus("Ti84"))
    assert len(res.layers) == 4 and len(res.layers[0].elementIds) == 1


def test_only_plate_like_shapes_become_the_plate(tmp_path):
    """The canvas plate (square, parametric) REPLACES the detected source shape, so a big base
    shape that is not plate-like (triangle, star, wide banner) must stay art."""
    cases = {
        "triangle": b'<polygon fill="#09f" points="50,0 100,100 0,100"/><circle cx="50" cy="70" r="10" fill="#fff"/>',
        "banner": b'<rect y="30" width="100" height="40" rx="8" fill="#333"/><circle cx="50" cy="50" r="12" fill="#fc0"/>',
        "squircle": b'<rect width="100" height="100" rx="22" fill="#e11d48"/><circle cx="50" cy="50" r="20" fill="#fff"/>',
    }
    for name, body in cases.items():
        data = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">' + body + b"</svg>"
        res = svg.import_svg(data, f"{name}.svg", tmp_path / name)
        if name == "squircle":
            assert res.source.plateDetected and len(res.layers) == 1
        else:
            assert not res.source.plateDetected, name
            assert sum(len(L.elementIds) for L in res.layers) == 2, name


def test_thumbnail_sizes_are_clamped(import_icon):
    res, project, pdir = import_icon(SVGTESTS_DIR / "c_holes_evenodd.svg")
    assert raster.decode_rgba(svg.thumbnail_png((SVGTESTS_DIR / "c_holes_evenodd.svg").read_bytes(), 0)).shape[0] == 8
    assert raster.decode_rgba(svg.layer_thumbnail_png(pdir, project, project.layers[0].id, 10 ** 6)).shape[0] == 1024


def test_texture_budget_for_many_layers(import_icon):
    from bis.svg.geometry import texture_size_for

    assert [texture_size_for(n) for n in (1, 8, 9, 24, 25)] == [2048, 2048, 1024, 1024, 512]
    res, project, pdir = import_icon(corpus("Ti84"), "element")
    assert len(project.layers) > 8
    bundle = svg.build_geometry(pdir, project, "/files/projects/p")
    sizes = {raster.decode_rgba(Path(lg.texturePath).read_bytes()).shape[0] for lg in bundle.layers.values()}
    assert sizes == {texture_size_for(len(project.layers))}
