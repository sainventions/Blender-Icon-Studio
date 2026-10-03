"""Regression tests for the QA round-2 render-quality defects — launches REAL Blender 5.0 (OptiX). Skipped when
Blender is missing. GPU rules: renders ≤ 256 px, ≤ 32 spp.

Synthetic scenes (scene builders below are also used by scratch before/after scripts) isolate each defect:
hollow open fills, bevel growth on thin rings, dark translucent overlays, plate colour under Khronos PBR
Neutral, EEVEE alpha speckle, clear-light glyph contrast, hidden shadow-caster layers, editable .blend files.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import worker_client as wc  # noqa: E402

pytestmark = pytest.mark.skipif(not wc.blender_available(), reason="Blender 5.0 not installed")
K = 0.5522847498
PX = 128


# ------------------------------------------------------------------------------------------------ scene builders
def circle(r, cx=0.0, cy=0.0, hole=False, closed=True, dup_end=False):
    k = K * r
    pts = [{"co": [cx + r, cy], "hl": [cx + r, cy - k], "hr": [cx + r, cy + k]},
           {"co": [cx, cy + r], "hl": [cx + k, cy + r], "hr": [cx - k, cy + r]},
           {"co": [cx - r, cy], "hl": [cx - r, cy + k], "hr": [cx - r, cy - k]},
           {"co": [cx, cy - r], "hl": [cx - k, cy - r], "hr": [cx + k, cy - r]}]
    if hole:
        pts = [{"co": p["co"], "hl": p["hr"], "hr": p["hl"]} for p in reversed(pts)]
    if dup_end:          # an SVG subpath without 'Z' that ends with 'h0' on its start point
        pts.append({"co": list(pts[0]["co"]), "hl": list(pts[0]["hl"]), "hr": list(pts[0]["co"])})
    return {"closed": closed, "hole": hole, "parent": 0 if hole else -1, "depth": 1 if hole else 0, "points": pts}


def square(h, cx=0.0, cy=0.0):
    c = [(cx + h, cy + h), (cx - h, cy + h), (cx - h, cy - h), (cx + h, cy - h)]
    pts = []
    for i, (x, y) in enumerate(c):
        px, py = c[i - 1]
        nx, ny = c[(i + 1) % 4]
        pts.append({"co": [x, y], "hl": [x + (px - x) / 3, y + (py - y) / 3], "hr": [x + (nx - x) / 3, y + (ny - y) / 3]})
    return {"closed": True, "hole": False, "parent": -1, "depth": 0, "points": pts}


def layer(lid, z=0.0, preset="liquid_glass", params=None, shadow=0.5, bevel=0.045, thickness=0.1):
    return {"id": lid, "name": lid, "visible": True, "fill": {"type": "auto"}, "mode": "individual",
            "material": {"preset": preset, "params": params or {}}, "shadow": {"kind": "neutral", "opacity": shadow},
            "depth": {"z": z, "thickness": thickness, "bevel": bevel, "bevelSegments": 6, "inflate": 0.0}}


def geo(regions, safe=0.3):
    """regions: [(element id, splines, hex colour, opacity)] -> LayerGeometry (solid paints, no texture)."""
    allp = [p["co"] for _, spl, _, _ in regions for s in spl for p in s["points"]]
    xs, ys = [p[0] for p in allp], [p[1] for p in allp]
    return {"hash": "q-" + str(abs(hash(json.dumps(regions, sort_keys=True))) % 10 ** 12),
            "silhouette": [s for _, spl, _, _ in regions for s in spl if not s.get("hole")],
            "regions": [{"elementId": e, "paint": {"type": "solid", "color": c}, "opacity": op, "splines": spl}
                        for e, spl, c, op in regions],
            "safeRadius": safe, "bbox": [min(xs), min(ys), max(xs), max(ys)], "images": []}


def scene(layers, geos, plate_fill="#ffffff", plate_preset="satin", shape="squircle", color_mode="neutral"):
    proj = {"id": "q", "layers": layers, "render": {"colorMode": color_mode, "backdrop": "transparent"},
            "canvas": {"shape": shape, "plate": {"fill": {"type": "solid", "color": plate_fill},
                                                  "material": {"preset": plate_preset, "params": {}}}}}
    return proj, {"projectId": "q", "hash": "q", "layers": geos}


def open_disc_scene():
    """Canvas / Calculator dots: an open subpath (closed:false, end == start) must render as a filled disc (a
    0.07-radius dot with the default 0.045 bevel was swept as a hollow tube)."""
    return scene([layer("A", preset="flat", shadow=0.0)],
                 {"A": geo([("e", [circle(0.07, closed=False, dup_end=True)], "#e72429", 1.0)], safe=0.0697)},
                 shape="none")


def thin_ring_scene():
    """Ti73's pie: a 0.02-wide ring in a layer whose safeRadius says 0.3 — the bevel must not fill its hole."""
    return scene([layer("A", preset="flat", shadow=0.0)],
                 {"A": geo([("e", [circle(0.4), circle(0.38, hole=True)], "#0e190b", 1.0)], safe=0.3)}, shape="none")


def dark_overlay_scene(opacity=0.33):
    """Camera lens / Calculator ÷: 33 % black over a flat mid-grey plate (SVG: 0.67 × 128 = 86 in sRGB)."""
    return scene([layer("A")], {"A": geo([("e", [circle(0.45)], "#000000", opacity)])},
                 plate_fill="#808080", plate_preset="flat", color_mode="standard")


def plate_scene(colour):
    return scene([], {}, plate_fill=colour)


def glyph_scene():
    """A white disc glyph on a saturated plate (Discord / Whatsapp)."""
    return scene([layer("A")], {"A": geo([("e", [circle(0.4)], "#ffffff", 1.0)])}, plate_fill="#5865f2")


def mono_scene():
    """Ti84: a dark and a white part side by side."""
    return scene([layer("A")], {"A": geo([("d", [circle(0.25, -0.4)], "#202020", 1.0),
                                          ("w", [circle(0.25, 0.4)], "#ffffff", 1.0)])}, plate_fill="#7f8590")


def rect(x0, y0, x1, y1, hole=False):
    """Straight-edged rectangle (CCW outer / CW hole), handles at thirds."""
    c = [(x1, y1), (x0, y1), (x0, y0), (x1, y0)]
    if hole:
        c = c[::-1]
    pts = []
    for i, (x, y) in enumerate(c):
        px, py = c[i - 1]
        nx, ny = c[(i + 1) % 4]
        pts.append({"co": [x, y], "hl": [x + (px - x) / 3, y + (py - y) / 3], "hr": [x + (nx - x) / 3, y + (ny - y) / 3]})
    return {"closed": True, "hole": hole, "parent": 0 if hole else -1, "depth": 1 if hole else 0, "points": pts}


HOLE = (-0.35, -0.15, 0.35, 0.15)


def cell_scene(zoom=1.6):
    """Sheets: a white glass slab with a rectangular hole (sharp concave corners, straight edges)."""
    proj, bundle = scene([layer("A", shadow=0.0)],
                         {"A": geo([("e", [rect(-0.6, -0.4, 0.6, 0.4), rect(*HOLE, hole=True)], "#ffffff", 1.0)],
                                   safe=0.15)}, plate_fill="#1f8f55")
    proj["camera"] = {"view": "front", "zoom": zoom}
    return proj, bundle


def hidden_scene():
    """Translate: a magenta shadow-caster layer completely under a higher (glass) card."""
    return scene([layer("M", z=0.0), layer("B", z=0.13)],
                 {"M": geo([("m", [square(0.25)], "#ff15dc", 1.0)]),
                  "B": geo([("b", [square(0.5)], "#477adb", 1.0)])})


# ------------------------------------------------------------------------------------------------ helpers
def rgba(path):
    from PIL import Image
    return np.asarray(Image.open(path).convert("RGBA")).astype(float)


def patch(a, cx, cy, r=6):
    """Pixels around canvas point (cx, cy) (front ortho view, 2.24 units across)."""
    h = a.shape[0]
    col, row = int((cx + 1.12) / 2.24 * h), int((1.12 - cy) / 2.24 * h)
    return a[row - r:row + r + 1, col - r:col + r + 1]


def lab(rgb):
    c = np.asarray(rgb, float) / 255
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = lin @ np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750],
                          [0.0193339, 0.1191920, 0.9503041]]).T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > (6 / 29) ** 3, np.cbrt(xyz), xyz / (3 * (6 / 29) ** 2) + 4 / 29)
    return np.array([116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])])


def render(w, proj_bundle, out, **kw):
    proj, bundle = proj_bundle
    args = {"project": proj, "geometry": bundle, "quality": "draft", "size": PX, "out": str(out)}
    if kw.get("quality") == "preview":
        args["samples"] = 24
    args.update(kw)
    return w.result("render", args)


@pytest.fixture(scope="module")
def worker():
    w = wc.Worker(warmup="basic")
    yield w
    w.close()


@pytest.fixture(scope="module")
def outdir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("bis_quality")


# ------------------------------------------------------------------------------------------------ geometry
@pytest.mark.parametrize("quality", ["draft", "preview"])
def test_open_subpath_renders_filled(worker, outdir, quality):
    out = outdir / f"open_disc_{quality}.png"
    r = render(worker, open_disc_scene(), out, quality=quality)
    a = rgba(out)
    centre = patch(a, 0.0, 0.0, 1)
    assert centre[..., 3].min() > 250, "hollow: the open subpath was swept as a tube"
    assert r["stats"].get("openSplines") == 1


def test_thin_ring_bevel_never_fills_the_hole(worker, outdir):
    out = outdir / "thin_ring.png"
    render(worker, thin_ring_scene(), out)
    a = rgba(out)
    assert patch(a, 0.0, 0.0, 10)[..., 3].max() == 0          # the hole stays empty
    assert patch(a, 0.39, 0.0, 1)[..., 3].max() > 200         # the ring is there
    assert patch(a, 0.0, 0.52, 3)[..., 3].max() == 0          # nothing outside the ring


PIECES = json.loads((HERE / "data" / "qa_round2_pieces.json").read_text(encoding="utf-8"))


def _flat_ring(points, n=16):
    ring = []
    m = len(points)
    for i in range(m):
        a, b = points[i], points[(i + 1) % m]
        p0, c1, c2, p1 = (np.array(v, float) for v in (a["co"], a["hr"], b["hl"], b["co"]))
        for t in np.arange(n) / n:
            ring.append((1 - t) ** 3 * p0 + 3 * (1 - t) ** 2 * t * c1 + 3 * (1 - t) * t * t * c2 + t ** 3 * p1)
    return np.array(ring)


def piece_scene(piece, size):
    """One real corpus piece (art units, original art scale / depth), centred and zoomed to fill the frame;
    flat material (exact silhouette), no plate. -> (project, bundle, expected fill mask (size², bool), px/bevel)"""
    sa = float(piece["artScale"])
    rings = [_flat_ring(s["points"]) for s in piece["splines"]]
    allr = np.vstack(rings)
    lo, hi = allr.min(axis=0), allr.max(axis=0)
    c = (lo + hi) / 2
    ext = float((hi - lo).max()) * sa
    zoom = 2.24 / (1.4 * ext)
    ortho = 2.24 / zoom
    L = layer("A", preset="flat", shadow=0.0, bevel=piece["depth"]["bevel"], thickness=piece["depth"]["thickness"])
    L["transform"] = {"x": float(-c[0] * sa), "y": float(-c[1] * sa), "scale": 1.0}
    g = geo([(piece["element"], piece["splines"], "#ff8800", 1.0)], safe=piece["safeRadius"])
    proj, bundle = scene([L], {"A": g}, shape="none")
    proj["canvas"]["art"] = {"scale": sa, "x": 0.0, "y": 0.0}
    proj["camera"] = {"view": "front", "zoom": zoom}
    # expected silhouette: even-odd of the outline (open subpaths filled as closed)
    xs = ((np.arange(size) + 0.5) / size - 0.5) * ortho
    X, Y = np.meshgrid(xs, -xs)
    inside = np.zeros(X.shape, bool)
    for r in rings:
        q = (r - c) * sa
        x0, y0 = q[:, 0][:, None, None], q[:, 1][:, None, None]
        q1 = np.roll(q, -1, axis=0)
        x1, y1 = q1[:, 0][:, None, None], q1[:, 1][:, None, None]
        cond = (y0 > Y) != (y1 > Y)
        with np.errstate(divide="ignore", invalid="ignore"):
            xi = x0 + (Y - y0) * (x1 - x0) / np.where(np.abs(y1 - y0) < 1e-12, 1e-12, y1 - y0)
        inside ^= (np.count_nonzero(cond & (X < xi), axis=0) % 2).astype(bool)
    return proj, bundle, inside, float(piece["depth"]["bevel"]) / ortho * size


@pytest.mark.parametrize("piece", PIECES, ids=lambda p: f"{p['icon']}-{p['layer']}-{p['element']}")
def test_qa_piece_renders_exactly_its_silhouette(worker, outdir, piece):
    """Real pieces from the QA round-2 report, rendered flat and zoomed: nothing outside the outline (inverted /
    growing bevels: DJI blade, Ti73 ring hole, '+' inner corners), its core filled (hollow open-subpath dots),
    and the baked mesh passes the worker's own cap / growth check (Scandit's hollow bracket and 'D')."""
    from scipy import ndimage
    size = 256
    proj, bundle, inside, bevel_px = piece_scene(piece, size)
    out = outdir / f"piece_{piece['icon']}_{piece['element']}.png"
    render(worker, (proj, bundle), out, size=size)
    alpha = rgba(out)[..., 3]
    d_out = ndimage.distance_transform_edt(~inside)
    d_in = ndimage.distance_transform_edt(inside)
    far_out = d_out > 2.5
    assert alpha[far_out].max() < 40, ("geometry outside the outline", piece["why"], int((alpha[far_out] >= 40).sum()))
    core = d_in > 1.3 * bevel_px + 2          # fillets only round corners within ~1.2 bevel of the outline
    if core.any():
        assert (alpha[core] > 200).mean() > 0.995, ("hollow core", piece["why"], float((alpha[core] > 200).mean()))
    info = worker.result("scene_info", {"check": True})
    (ob,) = [o for o in info["objects"] if o["name"] == "BIS A r0"]
    assert ob["check"]["holesN"] <= 1 and ob["check"]["growN"] <= 1, (ob["route"], ob["reason"], ob["check"])


@pytest.mark.parametrize("quality", ["draft", "preview"])
def test_sharp_concave_corners_shade_evenly(worker, outdir, quality):
    """Sheets' cells (review of round 3): concave corners stay sharp (no silhouette growth), but the round
    bevel's mitre normal at such a corner was interpolated along the whole straight edge (one quad strip), so
    every bevel band of a rectangular hole shaded as a skewed wedge (draft drift 16/255 along an edge). Guard
    points next to sharp corners keep the band even."""
    size, zoom = 256, 1.6
    out = outdir / f"cell_{quality}.png"
    render(worker, cell_scene(zoom), out, quality=quality, size=size)
    img = rgba(out)[..., :3].mean(axis=2)
    ortho = 2.24 / zoom

    def pix(x, y):
        return int(round((x + ortho / 2) / ortho * size)), int(round((ortho / 2 - y) / ortho * size))

    x0, y0, x1, y1 = HOLE
    drifts = []
    for y in (y1 + 0.012, y0 - 0.012):          # bevel bands above / below the hole, away from the corners
        (ca, r), (cb, _) = pix(x0 + 0.08, y), pix(x1 - 0.08, y)
        band = img[r, ca:cb]
        drifts.append(abs(np.polyfit(np.linspace(0, 1, len(band)), band, 1)[0]))
    for x in (x0 - 0.012, x1 + 0.012):          # left / right bands
        (c, ra), (_, rb) = pix(x, y1 - 0.05), pix(x, y0 + 0.05)
        band = img[ra:rb, c]
        drifts.append(abs(np.polyfit(np.linspace(0, 1, len(band)), band, 1)[0]))
    assert max(drifts) < 6.0, [round(d, 1) for d in drifts]
    # the hole keeps its sharp corners: the plate shows right next to each corner, inside the hole
    c, r = pix(x1 - 0.01, y1 - 0.01)       # (a concave fillet of 1.2 x bevel would cover this point)
    corner = rgba(out)[r - 1:r + 2, c - 1:c + 2, :3].mean(axis=(0, 1))
    assert corner[1] > corner[0] + 40, corner           # plate green, not the white slab


# ------------------------------------------------------------------------------------------------ materials
@pytest.mark.parametrize("quality", ["draft", "preview"])
def test_dark_overlay_composites_like_the_svg(worker, outdir, quality):
    out = outdir / f"overlay_{quality}.png"
    render(worker, dark_overlay_scene(), out, quality=quality)
    v = patch(rgba(out), 0.0, 0.0, 5)[..., :3].mean()
    assert abs(v - 0.67 * 128) < 6, v            # SVG: 33 % black over #808080 = 86 (was 94)
    if quality == "draft":   # alpha-blended in EEVEE: no dither speckle
        noise = patch(rgba(out), 0.0, 0.0, 8)[..., :3].mean(axis=2).std()
        assert noise < 4.0, noise
        mats = worker.result("scene_info")["materials"]
        assert mats["BIS Mat A"]["renderMethod"] == "BLENDED"


@pytest.mark.parametrize("colour,limit", [("#fc3a00", 18.0), ("#5865f2", 8.0), ("#f0f0f0", 5.0), ("#1d2230", 8.0)])
def test_plate_colour_close_to_svg(worker, outdir, colour, limit):
    """Satin plate under the default 'neutral' colour mode: mean plate colour ΔE76 vs the SVG fill."""
    out = outdir / f"plate_{colour[1:]}.png"
    render(worker, plate_scene(colour), out, quality="preview")
    a = rgba(out)
    m = np.vstack([patch(a, x, y, 5)[..., :3].reshape(-1, 3) for x in (-0.4, 0.0, 0.4) for y in (-0.4, 0.0, 0.4)])
    want = [int(colour[i:i + 2], 16) for i in (1, 3, 5)]
    de = float(np.linalg.norm(lab(m.mean(axis=0)) - lab(want)))
    assert de < limit, (colour, m.mean(axis=0).round(), de)


def test_white_glyph_keeps_contrast_on_saturated_plate(worker, outdir):
    out = outdir / "glyph.png"
    render(worker, glyph_scene(), out, quality="preview")
    a = rgba(out)
    glyph = patch(a, 0.0, 0.05, 6)[..., :3].mean(axis=(0, 1))
    assert glyph.min() > 239, glyph                # near-white body, not plate-tinted (was 237)


def test_clear_light_glyph_reads_against_the_plate(worker, outdir):
    out = outdir / "clear_light.png"
    render(worker, glyph_scene(), out, quality="preview", appearance="clear-light")
    a = rgba(out)
    g = lab(patch(a, 0.0, 0.05, 6)[..., :3].reshape(-1, 3).mean(axis=0))[0]
    p = lab(np.vstack([patch(a, x, y, 4)[..., :3].reshape(-1, 3) for x, y in ((-0.7, 0.7), (0.7, -0.7))]).mean(0))[0]
    assert g - p > 11.0, (g, p)                    # L* difference (was 7.6)


@pytest.mark.parametrize("appearance", ["clear-light", "clear-dark"])
def test_clear_renditions_keep_dark_parts_dark(worker, outdir, appearance):
    out = outdir / f"mono_{appearance}.png"
    render(worker, mono_scene(), out, quality="preview", appearance=appearance)
    a = rgba(out)
    dark = patch(a, -0.4, 0.0, 5)[..., :3].mean()
    white = patch(a, 0.4, 0.0, 5)[..., :3].mean()
    assert white - dark > 45, (dark, white)


def test_hidden_shadow_caster_is_not_seen_through_glass(worker, outdir):
    out = outdir / "hidden.png"
    r = render(worker, hidden_scene(), out, quality="preview")
    assert r["stats"].get("hiddenPieces") == 1
    c = patch(rgba(out), 0.0, 0.0, 6)[..., :3].mean(axis=(0, 1))
    assert not (c[0] > c[1] + 40 and c[2] > c[1] + 40), c          # no magenta through the blue glass
    objs = {o["name"]: o for o in worker.result("scene_info")["objects"]}
    assert objs["BIS M r0"]["visibleTransmission"] is False and objs["BIS B r0"]["visibleTransmission"] is True


# ------------------------------------------------------------------------------------------------ .blend
def test_save_blend_is_editable(worker, outdir):
    """'Open in Blender': the .blend holds live curves (round bevel) / GN modifier stacks, not baked meshes."""
    proj, bundle = scene([layer("A"), layer("B", z=0.13)],
                         {"A": geo([("a", [circle(0.4)], "#3366ff", 1.0)]),
                          "B": geo([("b", [square(0.2)], "#ffffff", 1.0)], safe=0.01)})
    out = outdir / "editable.blend"
    r = worker.result("save_blend", {"project": proj, "geometry": bundle, "out": str(out)})
    assert r["editable"]["meshes"] == 0 and r["editable"]["curves"] == 3
    assert r["editable"]["bevelCurves"] >= 2 and r["editable"]["modifierStacks"] >= 1   # B: GN (tiny safeRadius)
    script = outdir / "inspect.py"
    script.write_text(
        "import bpy, json\n"
        "dg = bpy.context.evaluated_depsgraph_get()\n"
        "o = {}\n"
        "for ob in bpy.data.objects:\n"
        "    if ob.name.startswith('BIS ') and ob.type == 'CURVE':\n"
        "        me = ob.evaluated_get(dg).to_mesh()\n"
        "        o[ob.name] = [ob.data.bevel_depth, ob.data.offset, [m.type for m in ob.modifiers], len(me.polygons)]\n"
        "        ob.evaluated_get(dg).to_mesh_clear()\n"
        "print('INSPECT ' + json.dumps(o))\n", encoding="utf-8")
    p = subprocess.run([str(wc.BLENDER), "-b", "--factory-startup", str(out), "--python", str(script)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    line = next(ln for ln in p.stdout.splitlines() if ln.startswith("INSPECT "))
    objs = json.loads(line[8:])
    plate = objs["BIS Plate"]
    assert plate[0] > 0.01 and abs(plate[1] + plate[0]) < 1e-6 and plate[3] > 100      # live bevel, offset −bevel
    a = objs["BIS A r0"]
    assert a[0] > 0.01 and a[2] == [] and a[3] > 100
    b = objs["BIS B r0"]
    assert b[2] == ["NODES", "SOLIDIFY", "BEVEL"] and b[3] > 6
    # the worker renders baked meshes again afterwards
    render(worker, (proj, bundle), outdir / "after_blend.png")
    assert all(o["type"] == "MESH" for o in worker.result("scene_info")["objects"] if o["name"] in ("BIS A r0", "BIS Plate"))
