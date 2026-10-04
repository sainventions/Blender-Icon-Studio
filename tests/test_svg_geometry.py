"""Geometry-quality regressions (QA round 2): closed fills, spline hygiene, plate clipping,
full-bleed framing, plate corner fit and sub-pixel raster tracing."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
import shapely
from shapely.geometry import LinearRing, Point, Polygon

from conftest import CORPUS_DIR

import bis.svg as svg
from bis.svg import hygiene, raster
from bis.svg.geometry import path_to_splines, splines_to_d
from bis.svg.paths import skia_from_d
from bis.svg.plate import PLATE_CLIP_INSET, _outline_points
from picosvg.svg_transform import Affine2D

CORPUS = sorted(CORPUS_DIR.glob("*.svg"))


def corpus(name: str) -> Path:
    return CORPUS_DIR / f"{name}.svg"


def _ring(spline, n: int = 16) -> np.ndarray:
    """Independent dense sampling (the pipeline's own checks use 8 samples per segment)."""
    pts = spline.points
    m = len(pts)
    t = np.arange(n) / n
    out = []
    for i in range(m):
        a, b = pts[i], pts[(i + 1) % m]
        p0, c1, c2, p1 = (np.array(v) for v in (a.co, a.hr, b.hl, b.co))
        out.append(((1 - t) ** 3)[:, None] * p0 + (3 * (1 - t) ** 2 * t)[:, None] * c1
                   + (3 * (1 - t) * t * t)[:, None] * c2 + (t ** 3)[:, None] * p1)
    return np.concatenate(out)


def spline_problems(spline) -> list:
    out = []
    if not spline.closed:
        out.append("open")
    if len(spline.points) < 3:
        out.append(f"{len(spline.points)} points")
    r = _ring(spline)
    keep = np.r_[True, np.any(np.abs(np.diff(r, axis=0)) > 1e-12, axis=1)]
    r = r[keep]
    if len(r) < 3 or abs(Polygon(r).area) < 1e-6:
        out.append("degenerate area")
    elif not LinearRing(r).is_simple:
        out.append("self-intersection")
    return out


def _bundle(import_icon, name, strategy="smart"):
    res, project, pdir = import_icon(corpus(name), strategy)
    bundle = svg.build_geometry(pdir, project, "/files/projects/p", texture_size=128)
    return res, project, pdir, bundle


# ----------------------------------------------------------------------------------------------
# corpus-wide invariant (#1, #13)
# ----------------------------------------------------------------------------------------------
@pytest.mark.corpus
@pytest.mark.parametrize("path", CORPUS, ids=lambda p: p.stem)
def test_every_spline_is_a_clean_closed_fill(path, import_icon):
    """Every region / silhouette spline of every corpus icon is closed, has >= 3 points, a
    non-degenerate area and does not self-intersect."""
    res, project, pdir = import_icon(path)
    bundle = svg.build_geometry(pdir, project, "/files/projects/p", texture_size=64)
    bad = []
    for lid, lg in bundle.layers.items():
        for k, s in enumerate(lg.silhouette):
            bad += [f"{lid} silhouette[{k}]: {p}" for p in spline_problems(s)]
        assert lg.regions, lid
        for r in lg.regions:
            assert r.splines, (lid, r.elementId)
            for k, s in enumerate(r.splines):
                bad += [f"{lid} {r.elementId}[{k}]: {p}" for p in spline_problems(s)]
    assert not bad, bad[:10]


# ----------------------------------------------------------------------------------------------
# #1 filled subpaths without 'Z'
# ----------------------------------------------------------------------------------------------
def test_unclosed_convex_subpaths_are_closed():
    # skia's Simplify returns convex input unchanged - including a missing close
    dot = skia_from_d("M145.8,232.1 L145.8,232.1 C155.4,232.1 163.3,240 163.3,249.6 C163.3,259.2 155.4,267 "
                      "145.8,267 C136.2,267 128.4,259.2 128.4,249.6 C128.4,240 136.2,232.1 145.8,232.1")
    tri = skia_from_d("M177.044,246.986 L125.65,208.709 L177.044,280.45")
    for sk in (dot, tri):
        verbs = [v for v, _ in sk]
        assert verbs[-1].name == "CLOSE"
        spl = path_to_splines(sk, Affine2D(2 / 500, 0, 0, -2 / 500, -1, 1))
        assert len(spl) == 1 and spl[0]["closed"] and len(spl[0]["points"]) >= 3
    # the triangle's implicit closing edge is part of the fill
    spl = path_to_splines(tri, Affine2D.identity(), clean=False)
    assert len(spl[0]["points"]) == 3


@pytest.mark.parametrize("name, ids", [("Canvas", ["e2", "e10", "e12"]), ("Calculator", ["e1", "e2"]),
                                       ("Gmail", ["e2", "e3"]), ("Camera", ["e1"]), ("Contacts", ["e1", "e2"])])
def test_qa_open_dots_are_closed(name, ids, import_icon):
    from bis.svg.geometry import layer_regions, members_of

    res, project, pdir, bundle = _bundle(import_icon, name)
    store = svg.read_store(pdir)
    seen = set()
    for L in project.layers:
        cut = {m.id: abs(p.area) * store.art.k ** 2
               for m, p in layer_regions(store, members_of(store, L.elementIds))}
        for r in bundle.layers[L.id].regions:
            if r.elementId in ids:
                seen.add(r.elementId)
                assert all(s.closed and len(s.points) >= 3 for s in r.splines), (name, r.elementId)
                # a filled disc / wedge (not a tube along its outline): spline area == region area
                area = sum(abs(Polygon(_ring(s)).area) * (-1 if s.hole else 1) for s in r.splines)
                assert area == pytest.approx(cut[r.elementId], rel=0.02), (name, r.elementId)
    assert seen == set(ids)


# ----------------------------------------------------------------------------------------------
# hygiene units
# ----------------------------------------------------------------------------------------------
def _poly(points):
    n = len(points)
    out = []
    for i, p in enumerate(points):
        a, b = points[i - 1], points[(i + 1) % n]
        out.append({"co": p, "hl": (p[0] + (a[0] - p[0]) / 3, p[1] + (a[1] - p[1]) / 3),
                    "hr": (p[0] + (b[0] - p[0]) / 3, p[1] + (b[1] - p[1]) / 3)})
    return out


def test_hygiene_removes_micro_loops_spikes_and_twists():
    sq = [(0, 0), (0.4, 0), (0.4, 0.4), (0, 0.4)]
    # a micro loop (rounding debris) that comes back to its start point
    loop = sq[:2] + [(0.4005, 0.0003), (0.4002, 0.0006), (0.4, 0)] + sq[2:]
    # an out-and-back spike
    spike = sq[:2] + [(0.4, 0.2), (0.405, 0.2), (0.4, 0.2)] + sq[2:]
    # a figure-8 twist with a tiny lobe
    twist = [(0, 0), (0.4, 0), (0.4, 0.4), (0.21, 0.4), (0.19, 0.41), (0.19, 0.39), (0.21, 0.41),
             (0.2, 0.4), (0, 0.4)]
    for pts in (loop, spike, twist):
        out = hygiene.clean_spline(_poly(pts))
        assert len(out) == 1, pts
        assert hygiene.spline_issues(out[0]) == []
        assert abs(hygiene.signed_area(out[0])) == pytest.approx(0.16, rel=0.02)
    # slivers vanish, tiny but real shapes survive (as >= 3 points)
    assert hygiene.clean_spline(_poly([(0, 0), (0.3, 0), (0.3, 1e-7)])) == []
    tiny = hygiene.clean_spline(_poly([(0, 0), (0.003, 0), (0.003, 0.003), (0, 0.003)]))
    assert len(tiny) == 1 and len(tiny[0]) >= 3


def test_hygiene_keeps_fine_smooth_detail():
    """A finely subdivided smooth curve (stroker round caps: dozens of tiny tangent-continuous
    quads) is real geometry, not debris."""
    t = np.linspace(0, 2 * math.pi, 160, endpoint=False)
    circle = [(0.01 * math.cos(a), 0.01 * math.sin(a)) for a in t]   # 0.0004-long segments
    out = hygiene.clean_spline(_poly(circle))
    assert len(out) == 1 and len(out[0]) == 160


def test_smoothing_never_self_intersects():
    # a dense zig-zag polyline whose G1 smoothing would overshoot into loops
    pts = []
    for i in range(60):
        x = i / 60
        pts.append((x, 0.02 if i % 2 else 0.0))
    pts += [(1.0, 0.3), (0.0, 0.3)]
    d = "M" + " L".join(f"{x * 500} {y * 500}" for x, y in pts) + " Z"
    spl = path_to_splines(skia_from_d(d), Affine2D(2 / 500, 0, 0, 2 / 500, 0, 0))
    assert spl and all(hygiene.spline_issues(s["points"]) == [] for s in spl)


# ----------------------------------------------------------------------------------------------
# #13 Maps & co: self-intersections from boolean-op debris
# ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["Maps", "DJI", "Health", "Translate", "Twitter", "Weatherbug", "Wallet"])
def test_qa_self_intersections_are_gone(name, import_icon):
    res, project, pdir, bundle = _bundle(import_icon, name)
    for lid, lg in bundle.layers.items():
        for s in lg.silhouette + [s for r in lg.regions for s in r.splines]:
            assert spline_problems(s) == [], (name, lid)


def test_maps_regions_share_edges(import_icon):
    """Adjacent colour pieces still tile the pin: their union matches the layer art (no gaps)."""
    from bis.svg import textures

    res, project, pdir, bundle = _bundle(import_icon, "Maps", "single")
    store = svg.read_store(pdir)
    size = 512
    L = project.layers[0]
    mask_svg = textures.layer_svg([store.get(i) for i in L.elementIds], store.gradients,
                                  store.art.square_view_box(), None, size=(size, size), silhouette="#000")
    alpha = raster.render_svg(mask_svg, size, size)[..., 3] > 127
    union = np.zeros_like(alpha)
    for r in bundle.layers[L.id].regions:
        d = splines_to_d(r.splines)
        s = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="-1 -1 2 2" width="{size}" height="{size}">'
             f'<path transform="scale(1 -1)" fill="#000" fill-rule="evenodd" d="{d}"/></svg>')
        union |= raster.render_svg(s, size, size)[..., 3] > 127
    assert float((alpha & ~union).mean()) < 0.0005


# ----------------------------------------------------------------------------------------------
# #4 Internet: legal split, exact recomposition
# ----------------------------------------------------------------------------------------------
def test_internet_split_recomposes_and_keeps_paint_order(import_icon):
    res, project, pdir, bundle = _bundle(import_icon, "Internet")
    store = svg.read_store(pdir)
    w = h = 200
    imgs = [raster.render_svg(Path(bundle.plate["svgPath"]).read_text(encoding="utf-8"), w, h)]
    for L in project.layers:
        imgs.append(raster.render_svg((pdir / "cache" / Path(bundle.layers[L.id].svg).name)
                                      .read_text(encoding="utf-8"), w, h))
    recomposed = raster.composite(imgs)
    source = raster.render_svg(raster.strip_filters(corpus("Internet").read_text(encoding="utf-8")), w, h)
    assert raster.diff_pct(source, recomposed) <= 1.0
    # the sphere halves and the 33 % ring only TOUCH (shared edges, no overlap): the stack is a
    # legal topological order of the overlap constraints
    g = {e.id: e.geom(store.tolerance) for e in store.elems}
    assert g["e1"].intersection(g["e2"]).area < 1e-2 and g["e2"].intersection(g["e3"]).area < 1e-2
    pos = {i: k for k, L in enumerate(project.layers) for i in L.elementIds}
    for a, b in store.edges:
        ea, eb = store.elems[a].id, store.elems[b].id
        if ea in pos and eb in pos:
            assert pos[ea] <= pos[eb]
    # every piece is a clean closed fill (the upper half's micro corners used to break its cap)
    for lg in bundle.layers.values():
        for r in lg.regions:
            assert all(spline_problems(s) == [] for s in r.splines)


# ----------------------------------------------------------------------------------------------
# #5 full-bleed art / #11 plate clipping
# ----------------------------------------------------------------------------------------------
def test_earth_full_bleed_frames_the_canvas(import_icon):
    res, project, pdir, bundle = _bundle(import_icon, "Earth")
    c = res.canvas
    assert not res.source.plateDetected                      # no plate ELEMENT ...
    assert sum(len(L.elementIds) for L in res.layers) == len(res.elements)   # ... every element is art
    assert c.shape in ("rounded", "squircle")
    assert c.art.scale > 1.0                                  # the art's outline maps onto -1..1
    assert c.plate.fill.type == "solid"
    r, g, b = (int(c.plate.fill.color[i:i + 2], 16) for i in (1, 3, 5))
    assert b > r and b > g                                    # Earth blue, not default white
    assert any("edge to edge" in w for w in res.warnings)
    # the art covers the fitted plate (canvas space) almost completely
    store = svg.read_store(pdir)
    art = store.art
    u = shapely.union_all([e.geom(store.tolerance) for e in store.elems])
    s, tx, ty = c.art.scale, c.art.x, c.art.y
    plate_svg = Polygon([((x - tx) / s / art.k + art.cx, art.cy - (y - ty) / s / art.k)
                         for x, y in _outline_points(c.shape, c.cornerRadius, (0, 0), 1.0, 0.0, 256)])
    assert u.intersection(plate_svg).area / plate_svg.area > 0.97


def test_icon_without_plate_keeps_default_frame(tmp_path):
    data = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle cx="50" cy="50" r="20" '
            b'fill="#e11"/><rect x="40" y="40" width="20" height="20" fill="#fff"/></svg>')
    res = svg.import_svg(data, "small.svg", tmp_path / "s")
    assert not res.source.plateDetected
    assert res.canvas.plate.fill.type == "system-light" and res.canvas.art.scale == pytest.approx(0.78)


def _inside_plate(points, canvas, inset):
    shape = Polygon(_outline_points(canvas.shape, canvas.cornerRadius, (0, 0), 1.0, 0.0, 512))
    s, tx, ty = canvas.art.scale, canvas.art.x, canvas.art.y
    for p in points:
        q = Point(p[0] * s + tx, p[1] * s + ty)
        if not shape.buffer(-inset * s + 3e-4).contains(q):   # 3e-4: cubic quarter-arc error (2.7e-4 r)
            return False
    return True


@pytest.mark.parametrize("name", ["Desmos", "DJI", "Classroom"])
def test_art_is_clipped_to_the_plate(name, import_icon):
    res, project, pdir, bundle = _bundle(import_icon, name)
    # this art is flush with the plate edge (< 0.5 % really reaches past the fitted outline): it is
    # clipped silently - the warning used to count the clip's own inset band ("3.1 %" on Classroom)
    assert not any("past the plate edge" in w for w in res.warnings), res.warnings
    for lg in bundle.layers.values():
        pts = [_ring(s, 4) for s in lg.silhouette]
        assert _inside_plate(np.concatenate(pts), res.canvas, PLATE_CLIP_INSET * 0.9), name
        x0, y0, x1, y1 = lg.bbox
        assert all(abs(v) <= 1.0 for v in (x0 * res.canvas.art.scale, x1 * res.canvas.art.scale))


def test_art_inside_the_plate_is_untouched(import_icon):
    res, project, pdir, bundle = _bundle(import_icon, "Spotify")
    assert not any("past the plate edge" in w for w in res.warnings)
    store = svg.read_store(pdir)
    for L in project.layers:
        lg = bundle.layers[L.id]
        u = shapely.union_all([store.get(i).geom(store.tolerance) for i in L.elementIds])
        area = sum(abs(Polygon(_ring(s)).area) * (-1 if s.hole else 1) for s in lg.silhouette)
        assert area == pytest.approx(u.area * store.art.k ** 2, rel=0.01)


# ----------------------------------------------------------------------------------------------
# #16 plate corner fit
# ----------------------------------------------------------------------------------------------
def test_rounded_plate_corner_fit(import_icon):
    from bis.svg.plate import _iou, _rounded

    res, project, pdir = import_icon(corpus("Maps"))
    store = svg.read_store(pdir)
    plate = store.plate
    assert plate["shape"] == "rounded" and plate["iou"] >= 0.990
    g = store.get(plate["elementIds"][0]).geom(store.tolerance)
    x0, y0, x1, y1 = g.bounds
    w, h = x1 - x0, y1 - y0
    r_equal_area = math.sqrt((w * h - g.area) / (4 - math.pi))
    r_fit = plate["cornerRadius"] * max(w, h)
    assert r_fit < r_equal_area      # continuous corners: fuller than the equal-area arc
    assert _iou(g, _rounded(x0, y0, x1, y1, r_fit)) > _iou(g, _rounded(x0, y0, x1, y1, r_equal_area))


# ----------------------------------------------------------------------------------------------
# #2 / iMessage: sub-pixel raster tracing
# ----------------------------------------------------------------------------------------------
def test_soft_trace_is_subpixel_accurate_and_smooth():
    n = 128
    yy, xx = np.mgrid[0:n, 0:n] + 0.5
    # anti-aliased disc of radius 40.3 px and a hole of radius 12.7 px
    d = np.hypot(xx - 64, yy - 64)
    soft = np.clip(40.3 - d + 0.5, 0, 1) * (1 - np.clip(12.7 - d + 0.5, 0, 1))
    p = raster.soft_to_path(soft, 0.5, Affine2D.identity())
    assert abs(p.area) == pytest.approx(math.pi * (40.3 ** 2 - 12.7 ** 2), rel=0.01)
    spl = path_to_splines(p, Affine2D(2 / n, 0, 0, 2 / n, -1, -1), traced=True)
    assert len(spl) == 2
    for s in spl:
        pts = s["points"]
        assert max(hygiene._kink_deg(pts, i) for i in range(len(pts))) < 1.0   # G1: no staircase kinks
        r = np.hypot(*np.array([p["co"] for p in pts]).T) * n / 2
        assert np.ptp(r) < 0.3      # round within a third of a pixel


def _kinks(spline, deg: float) -> int:
    pts = [{"co": p.co, "hl": p.hl, "hr": p.hr} for p in spline.points]
    return sum(1 for i in range(len(pts)) if hygiene._kink_deg(pts, i) > deg)


def test_imessage_rim_has_no_kinks(import_icon):
    res, project, pdir, bundle = _bundle(import_icon, "iMessage")
    sil = bundle.layers[project.layers[0].id].silhouette
    outer = max(sil, key=lambda s: abs(Polygon(_ring(s)).area))
    assert len(outer.points) >= 24
    assert _kinks(outer, 15) <= 3     # the tail's tip and its two joins with the bubble


def test_feit_traced_foreground_keeps_structure(import_icon):
    res, project, pdir, bundle = _bundle(import_icon, "Feit")
    lg = bundle.layers[project.layers[0].id]
    # house outline + 3 wi-fi parts (2 arcs ... ) + dot: separate filled islands, all clean
    assert len(lg.silhouette) >= 5
    assert all(spline_problems(s) == [] for s in lg.silhouette)
    # the traced silhouette matches the matted foreground (texture alpha) closely
    tex = raster.decode_rgba(Path(lg.texturePath).read_bytes())
    size = tex.shape[0]
    d = splines_to_d(lg.silhouette)
    s = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="-1 -1 2 2" width="{size}" height="{size}">'
         f'<path transform="scale(1 -1)" fill="#000" fill-rule="evenodd" d="{d}"/></svg>')
    m = raster.render_svg(s, size, size)[..., 3] > 127
    a = tex[..., 3] > 127
    assert float((m ^ a).sum()) / a.sum() < 0.08


# ----------------------------------------------------------------------------------------------
# review of round 3: plate-rim hairline, honest clip warning, raster pinholes, curve-preserving
# smoothing
# ----------------------------------------------------------------------------------------------
def _silhouette_union(bundle, canvas):
    s, tx, ty = canvas.art.scale, canvas.art.x, canvas.art.y
    return shapely.union_all([Polygon(_ring(sp, 8) * s + (tx, ty)).buffer(0)
                              for lg in bundle.layers.values() for sp in lg.silhouette if not sp.hole])


@pytest.mark.parametrize("name", ["Classroom", "Earth"])
def test_art_flush_with_the_plate_edge_still_reaches_it(name, import_icon):
    """Clipping must not open a visible rim of plate colour around art that is flush with the
    plate edge (Classroom's yellow frame, Earth's full-bleed waves): the art still covers the band
    0.008..0.014 inside the plate outline (0.44 with the former 0.01 inset - a green / dark-blue
    outline in every render), and never reaches past the outline."""
    res, project, pdir, bundle = _bundle(import_icon, name)
    c = res.canvas
    plate = Polygon(_outline_points(c.shape, c.cornerRadius, (0, 0), 1.0, 0.0, 512))
    u = _silhouette_union(bundle, c)
    band = plate.buffer(-0.008).difference(plate.buffer(-0.014))
    assert band.intersection(u).area / band.area > 0.85, name
    assert u.difference(plate.buffer(1e-4)).area < 1e-5, name


def test_clip_warning_only_for_art_past_the_plate(import_icon):
    plate = '<rect x="10" y="10" width="480" height="480" rx="110" fill="#2563eb"/>'
    over = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 500 500">' + plate.encode()
            + b'<circle cx="420" cy="250" r="120" fill="#fff"/></svg>')
    flush = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 500 500">' + plate.encode()
             + b'<rect x="10" y="200" width="480" height="100" fill="#fff"/></svg>')
    res, project, pdir = import_icon(over, name="over.svg")
    assert res.source.plateDetected
    assert any("past the plate edge" in w for w in res.warnings), res.warnings
    bundle = svg.build_geometry(pdir, project, "/files/projects/p", texture_size=64)
    u = _silhouette_union(bundle, res.canvas)
    shape = Polygon(_outline_points(res.canvas.shape, res.canvas.cornerRadius, (0, 0), 1.0, 0.0, 512))
    assert u.difference(shape.buffer(1e-4)).area < 1e-5     # the overhang is gone from the 3D geometry
    res, project, pdir = import_icon(flush, name="flush.svg")
    assert not any("past the plate edge" in w for w in res.warnings), res.warnings


def test_soft_trace_fills_pinholes_but_keeps_real_holes():
    from bis.svg.paths import shapely_from_path

    n = 512    # pinholes narrower than 1 % of the image (5 px here) are filled
    yy, xx = np.mgrid[0:n, 0:n] + 0.5
    soft = np.clip(200.3 - np.hypot(xx - 256, yy - 256) + 0.5, 0, 1)        # disc r = 200 px
    soft[np.hypot(xx - 256, yy - 256) < 60] = 0.0                              # real hole r = 60 px
    for cx, cy in ((120, 256), (392, 256), (256, 120)):                        # 2 px alpha pinholes
        soft[cy:cy + 2, cx:cx + 2] = 0.0
    soft[400:403, 250:253] = 0.0                                               # a 3 px pinhole ...
    soft[401, 251] = 1.0                                                       # ... with a speck inside
    p = raster.soft_to_path(soft, 0.5, Affine2D.identity())
    g = shapely_from_path(p, 0.05)
    polys = list(getattr(g, "geoms", [g]))
    assert len(polys) == 1 and len(polys[0].interiors) == 1                   # only the real hole
    assert g.area == pytest.approx(math.pi * (200.3 ** 2 - 60 ** 2), rel=0.01)
    # opting out keeps every hole
    q = shapely_from_path(raster.soft_to_path(soft, 0.5, Affine2D.identity(), min_hole_frac=0.0), 0.05)
    assert sum(len(pp.interiors) for pp in getattr(q, "geoms", [q])) >= 2   # + the 3 px pinhole


def test_find_device_sweep_has_no_pinholes(import_icon):
    """The 20 % radar-sweep image used to trace with ~30 sub-pixel pinholes (31 hole splines),
    which collapsed its safe bevel radius 0.121 -> 0.034 (bevel clamped 0.045 -> 0.031)."""
    res, project, pdir, bundle = _bundle(import_icon, "Find Device")
    img_layers = [L for L in project.layers
                  if all(e.kind == "image" for e in res.elements if e.id in L.elementIds)]
    assert img_layers
    for L in img_layers:
        lg = bundle.layers[L.id]
        holes = [abs(Polygon(_ring(s)).area) for s in lg.silhouette if s.hole]
        assert not [a for a in holes if a < 2e-4], holes
        assert lg.safeRadius > 0.1
        assert L.depth.bevel == pytest.approx(L.depth.thickness / 2)


@pytest.mark.parametrize("name,limit", [("Ti84", 0.041), ("Google Calendar", 0.03)])
def test_combined_layers_with_small_parts_limit_the_safe_radius(name, limit, import_icon):
    """svg review round 4 #3: a 'combined' body's safe radius is limited by its smallest separate part
    (Ti84's keypad face: keys 0.079 tall, was 0.326; Google Calendar's body: the "31" digits, was 0.456).
    Round 7: the bevel is no longer clamped to it (height-field bodies taper thin parts; thickness/2 only), while
    maxRadius - the stack height - is set by the LARGEST part."""
    res, project, pdir, bundle = _bundle(import_icon, name)
    combined = [L for L in project.layers if L.mode == "combined"]
    assert combined
    for L in combined:
        lg = bundle.layers[L.id]
        outers = [Polygon(_ring(s)) for s in lg.silhouette if not s.hole]
        if len(outers) < 2:
            continue
        smallest = min(outers, key=lambda p: p.area)
        inr = 0.0
        for r in np.linspace(0.001, 0.2, 400):      # inradius of the smallest part (holes ignored: an upper bound)
            if smallest.buffer(-r).is_empty:
                break
            inr = r
        assert lg.safeRadius <= inr + 2e-3 and lg.safeRadius < limit, (L.id, lg.safeRadius, inr)
        assert L.depth.bevel == pytest.approx(L.depth.thickness / 2)
        # maxRadius (holes respected) <= the largest part's inscribed radius with its holes ignored, and the
        # largest part is far thicker than the smallest one that limits the safe radius
        biggest = max(shapely.maximum_inscribed_circle(o, tolerance=1e-4).length for o in outers)
        assert lg.safeRadius < lg.maxRadius <= biggest + 2e-3, (L.id, lg.maxRadius, biggest)


def test_smoothing_keeps_the_few_curve_segments_exact():
    """A mostly-straight contour (dense polyline arc + ONE cubic, e.g. the plate clip's corner arc
    on a traced outline) is smoothed through its vertices - the cubic must not be flattened."""
    pts = [(250 + 200 * math.cos(a), 250 + 200 * math.sin(a)) for a in np.linspace(0, math.pi, 41)]
    d = "M" + " L".join(f"{x:.4f} {y:.4f}" for x, y in pts)
    d += " C 50 0 450 0 450 250 Z"                        # one big bulging cubic back to the start
    sk = skia_from_d(d)
    m = Affine2D(2 / 500, 0, 0, -2 / 500, -1, 1)
    exact = path_to_splines(sk, m, smooth=False)
    smooth = path_to_splines(sk, m)
    assert len(exact) == len(smooth) == 1 and smooth[0].get("smoothed")
    a_exact = abs(hygiene.signed_area(exact[0]["points"]))
    assert abs(hygiene.signed_area(smooth[0]["points"])) == pytest.approx(a_exact, rel=0.005)
