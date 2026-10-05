"""Regression tests for the QA round-2 render-quality defects, launching REAL Blender 5.0 (OptiX). Skipped when
Blender is missing. GPU rules: renders ≤ 256 px, ≤ 32 spp.

Synthetic scenes (scene builders below are also used by test_materials.py and scratch before/after scripts) isolate
each defect: hollow open fills, bevel growth on thin rings, sharp concave corners, hidden shadow-caster layers,
editable .blend files.
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
            "material": {"preset": preset, "params": params or {}},
            "shadow": {"kind": "physical" if shadow > 0 else "none", "opacity": shadow},
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
    """Ti73's pie: a 0.02-wide ring in a layer whose safeRadius says 0.3; the bevel must not fill its hole."""
    return scene([layer("A", preset="flat", shadow=0.0)],
                 {"A": geo([("e", [circle(0.4), circle(0.38, hole=True)], "#0e190b", 1.0)], safe=0.3)}, shape="none")


def plate_scene(colour):
    return scene([], {}, plate_fill=colour)


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
    """Sheets: a white slab with a rectangular hole (sharp concave corners, straight edges). Opaque satin lit by the key
    on the view axis only: the bevel geometry is under test; physical glass would show the refracted plate in the
    bands, and a grazing key puts the bands at its terminator, where real shading varies along them (12-20/255)."""
    proj, bundle = scene([layer("A", preset="satin", shadow=0.0)],
                         {"A": geo([("e", [rect(-0.6, -0.4, 0.6, 0.4), rect(*HOLE, hole=True)], "#ffffff", 1.0)],
                                   safe=0.15)}, plate_fill="#1f8f55")
    proj["camera"] = {"view": "front", "zoom": zoom}
    proj["lighting"] = {"preset": "studio", "elevation": 0, "rim": 0, "fill": 0, "environment": 0.5}
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
    and the body mesh passes the worker's own check (watertight, no self-intersections, no flipped normals)."""
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
    ck = ob["check"]
    assert ck["nonManifold"] == 0 and ck["selfIntersections"] == 0 and ck["invertedNormals"] == 0, (ob["route"], ck)


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


# ------------------------------------------------------------------------------------------------ scene
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
    """'Open in Blender': the .blend holds the same watertight body meshes the renders use (PLAN §11 height-field
    bodies; the curve-bevel route is retired), each with its own single-Principled material."""
    proj, bundle = scene([layer("A"), layer("B", z=0.13)],
                         {"A": geo([("a", [circle(0.4)], "#3366ff", 1.0)]),
                          "B": geo([("b", [square(0.2)], "#ffffff", 1.0)], safe=0.01)})
    out = outdir / "editable.blend"
    r = worker.result("save_blend", {"project": proj, "geometry": bundle, "out": str(out)})
    assert r["editable"]["meshes"] == 3 and r["editable"]["curves"] == 0
    script = outdir / "inspect.py"
    script.write_text(
        "import bpy, bmesh, json\n"
        "o = {}\n"
        "for ob in bpy.data.objects:\n"
        "    if ob.name.startswith('BIS ') and ob.type == 'MESH' and ob.name != 'BIS Wallpaper':\n"
        "        bm = bmesh.new(); bm.from_mesh(ob.data)\n"
        "        o[ob.name] = [len(ob.data.polygons), sum(1 for e in bm.edges if not e.is_manifold),\n"
        "                      ob.active_material.name if ob.active_material else None]\n"
        "        bm.free()\n"
        "print('INSPECT ' + json.dumps(o))\n", encoding="utf-8")
    p = subprocess.run([str(wc.BLENDER), "-b", "--factory-startup", str(out), "--python", str(script)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    line = next(ln for ln in p.stdout.splitlines() if ln.startswith("INSPECT "))
    objs = json.loads(line[8:])
    assert set(objs) == {"BIS Plate", "BIS A r0", "BIS B r0"}, objs
    for name, (faces, nonmanifold, mat) in objs.items():
        assert faces > 100 and nonmanifold == 0 and mat and mat.startswith("BIS "), (name, objs[name])
    assert objs["BIS A r0"][2] == "BIS A / a" and objs["BIS B r0"][2] == "BIS B / b"
    # the worker keeps rendering afterwards
    render(worker, (proj, bundle), outdir / "after_blend.png")
    assert all(o["type"] == "MESH" for o in worker.result("scene_info")["objects"] if o["name"] in ("BIS A r0", "BIS Plate"))
