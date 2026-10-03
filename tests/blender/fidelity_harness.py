"""Corpus-wide colour-fidelity harness (not a pytest test): renders every corpus icon from FRESH ``bis.svg``
bundles in the draft (EEVEE) and preview (Cycles OptiX) tiers and measures how well the INTERIOR of every
painted face matches the SVG paint.

    .venv/Scripts/python.exe tests/blender/fidelity_harness.py --out DIR [--icons Gmail Notion ...]
        [--size 256] [--qualities draft preview] [--port 8601] [--reuse] [--analyse-only] [--label before]

Target (round 4): per icon and engine, the mean ΔE76 over the interior of each painted region ≤ 8 and over the
plate interior ≤ 5. Rims, bevels, edges and cast shadows are free to deviate, so they are masked out:

* reference  = the icon's ``normalized.svg`` (no drop-shadow filters: those become 3D shadows) rasterised with
  resvg straight into the render's pixel grid (canvas.art maps the source plate onto −1..1; front ortho
  camera, ``ortho_scale = 2.24 / zoom``).
* label map  = plate shape + every region of every visible layer (bundle splines, layer transforms) painted
  bottom → top, so each pixel belongs to the top-most piece the camera sees.
* interior   = pixels of a label farther than its bevel + 2 px from the label's visible boundary.
* shadows    = every icon is rendered a second time with all layer shadows off (``shadow.kind = none``);
  pixels where the two renders differ by ΔE76 > SHADOW_DE (dilated by 1 px) AND that lie in the (generous)
  shadow sweep of a HIGHER piece are cast-shadow pixels and are excluded per engine. A glass piece's own
  shadow on the plate, seen through its translucent body, is part of its colour and stays measured. Raster
  image cards are excluded too.

Writes ``DIR/fidelity.json`` (per icon / engine / region numbers + summary), ``DIR/sheet_worst_NN.png`` (worst
offenders: SVG | draft | preview | ΔE maps, interior masks outlined) and keeps the renders in
``DIR/renders/<quality>/``. GPU rules: draft/preview tiers only, ≤ 256 px by default.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import statistics
import sys
import time
import traceback
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CORPUS = REPO / "samle icons"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "server"))

TARGET_REGION = 8.0
TARGET_PLATE = 5.0
GAMUT_SLACK = 2.0           # a miss within this of the gamut floor counts as 'at the transform's limit'
MIN_PIXELS = 12             # regions with fewer interior pixels are reported as unmeasured
SHADOW_DE = 2.5            # shadow-on vs shadow-off difference that marks a cast-shadow pixel
LIGHT_HALF_ANGLE = 18.4     # key softbox (4 BU disk at 6 BU) half angle, degrees


# ------------------------------------------------------------------------------------------------ colour
def srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    c = np.asarray(rgb, np.float64) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    m = np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750],
                  [0.0193339, 0.1191920, 0.9503041]])
    xyz = lin @ m.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > (6 / 29) ** 3, np.cbrt(xyz), xyz / (3 * (6 / 29) ** 2) + 4 / 29)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


def load_rgba(path) -> np.ndarray:
    from PIL import Image
    return np.asarray(Image.open(path).convert("RGBA")).astype(np.float64)


_FLOOR: dict = {}


def gamut_floor(rgb, color_mode: str = "neutral") -> float:
    """Smallest ΔE76 any scene radiance can reach for the sRGB colour ``rgb`` through the colour mode's view
    transform (Khronos PBR Neutral cannot show bright saturated brand colours: #ff3b00 >= 8.9). 0 for the
    other modes. Cached per colour."""
    if color_mode != "neutral":
        return 0.0
    key = tuple(int(v) for v in rgb)
    if key in _FLOOR:
        return _FLOOR[key]
    from scipy.optimize import minimize
    sys.path.insert(0, str(REPO))
    from blender_worker.util import pbr_neutral
    t = np.asarray(key, np.float64)
    target = srgb_to_lab(t)
    lin = np.where(t / 255 <= 0.04045, t / 255 / 12.92, ((t / 255 + 0.055) / 1.055) ** 2.4)

    def f(x):
        y = np.clip(np.asarray(pbr_neutral(np.abs(x))), 0, 1)
        s = np.where(y <= 0.0031308, 12.92 * y, 1.055 * y ** (1 / 2.4) - 0.055) * 255
        return float(np.linalg.norm(srgb_to_lab(s) - target))
    best = min((minimize(f, x0, method="Nelder-Mead", options={"xatol": 1e-4, "fatol": 1e-3})
                for x0 in (lin, lin * 0.85 + 0.01, lin * 1.3 + 0.02)), key=lambda r: r.fun)
    _FLOOR[key] = round(float(best.fun), 2)
    return _FLOOR[key]


# ------------------------------------------------------------------------------------------------ geometry
def _bezier_ring(points: list, closed: bool = True, n: int = 12) -> np.ndarray:
    ring = []
    m = len(points)
    if m == 0:
        return np.zeros((0, 2))
    for i in range(m if closed else m - 1):
        a, b = points[i], points[(i + 1) % m]
        p0, c1, c2, p1 = (np.asarray(v, np.float64) for v in (a["co"], a["hr"], b["hl"], b["co"]))
        for t in np.arange(n) / n:
            ring.append((1 - t) ** 3 * p0 + 3 * (1 - t) ** 2 * t * c1 + 3 * (1 - t) * t * t * c2 + t ** 3 * p1)
    return np.asarray(ring)


class Frame:
    """Canvas (−h..h, y up) <-> pixel grid of a front ortho render."""

    def __init__(self, size: int, zoom: float = 1.0):
        self.n = int(size)
        self.h = 1.12 / max(0.05, float(zoom or 1.0))

    def px(self, pts: np.ndarray) -> np.ndarray:
        pts = np.asarray(pts, np.float64)
        return np.stack([(pts[:, 0] + self.h) / (2 * self.h) * self.n, (self.h - pts[:, 1]) / (2 * self.h) * self.n], 1)

    def units(self, d: float) -> float:          # canvas length -> pixels
        return d / (2 * self.h) * self.n


def fill_rings(frame: Frame, rings: list) -> np.ndarray:
    """Even-odd fill of canvas-space rings -> bool mask (pixel centres)."""
    from PIL import Image, ImageDraw
    acc = np.zeros((frame.n, frame.n), bool)
    for r in rings:
        if len(r) < 3:
            continue
        im = Image.new("L", (frame.n, frame.n), 0)
        ImageDraw.Draw(im).polygon([tuple(p) for p in frame.px(r)], fill=255)
        acc ^= np.asarray(im) > 127
    return acc


def plate_ring(shape: str, corner_radius: float, n: int = 256) -> np.ndarray:
    t = np.linspace(0, 2 * np.pi, n, endpoint=False)
    if shape == "circle":
        return np.stack([np.cos(t), np.sin(t)], 1)
    if shape == "squircle":
        c, s = np.cos(t), np.sin(t)
        return np.stack([np.sign(c) * np.abs(c) ** 0.4, np.sign(s) * np.abs(s) ** 0.4], 1)
    if shape == "rounded":
        r = max(0.0, min(1.0, 2.0 * float(corner_radius)))
        pts = []
        for cx, cy, a0 in ((1 - r, 1 - r, 0.0), (r - 1, 1 - r, 90.0), (r - 1, r - 1, 180.0), (1 - r, r - 1, 270.0)):
            for a in np.radians(np.linspace(a0, a0 + 90, 24)):
                pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        return np.asarray(pts)
    return np.array([(1, 1), (-1, 1), (-1, -1), (1, -1)], np.float64)


def edt(mask: np.ndarray) -> np.ndarray:
    from scipy import ndimage
    return ndimage.distance_transform_edt(mask)


def label_map(project: dict, bundle: dict, frame: Frame):
    """-> (labels int32 (−1 none, 0 plate, k ≥ 1 region k), info per label, card mask)."""
    canvas = project.get("canvas") or {}
    art = canvas.get("art") or {}
    sa, ax, ay = float(art.get("scale", 1.0)), float(art.get("x", 0.0)), float(art.get("y", 0.0))
    n = frame.n
    labels = np.full((n, n), -1, np.int32)
    infos = [None]
    plate = canvas.get("plate") or {}
    if plate.get("visible", True) and canvas.get("shape", "squircle") != "none" and \
            (plate.get("fill") or {}).get("type") != "none":
        pm = fill_rings(frame, [plate_ring(canvas.get("shape", "squircle"), canvas.get("cornerRadius", 0.225))])
        labels[pm] = 0
        infos[0] = {"kind": "plate", "bevel": float(plate.get("bevel", 0.04)), "ztop": 0.0,
                    "paint": plate.get("fill")}
    cards = np.zeros((n, n), bool)
    for L in project.get("layers") or []:
        if not L.get("visible", True):
            continue
        g = (bundle.get("layers") or {}).get(L["id"])
        if g is None:
            continue
        tr = L.get("transform") or {}
        sl, tx, ty = float(tr.get("scale", 1.0)), float(tr.get("x", 0.0)), float(tr.get("y", 0.0))
        dp = L.get("depth") or {}
        z0 = float(dp.get("z", 0.0)) * float((project.get("camera") or {}).get("explode", 1.0)) + 0.002
        th = float(dp.get("thickness", 0.1))
        bevel = min(float(dp.get("bevel", 0.045)), 0.9 * float(g.get("safeRadius", 1.0)) * sa * sl + 1e-6)

        def to_canvas(ring):
            return (ring * sa + np.array([ax, ay])) * sl + np.array([tx, ty])

        regions = g.get("regions") or []
        if L.get("mode") == "combined":
            regions = [{"elementId": "sil", "paint": {"type": "auto"}, "opacity": 1.0,
                        "splines": g.get("silhouette") or []}]
        for k, r in enumerate(regions):
            rings = [to_canvas(_bezier_ring(s.get("points") or [], True)) for s in r.get("splines") or []]
            m = fill_rings(frame, rings)
            if not m.any():
                continue
            idx = len(infos)
            labels[m] = idx
            p = r.get("paint") or {}
            infos.append({"kind": "region", "layer": L["id"], "layerName": L.get("name", L["id"]), "region": k,
                          "elementId": r.get("elementId"), "paintType": p.get("type"),
                          "color": p.get("color") if p.get("type") == "solid" else None,
                          "opacity": float(r.get("opacity", 1.0)) * float(L.get("opacity", 1.0)),
                          "preset": (L.get("material") or {}).get("preset"), "bevel": bevel, "z0": z0,
                          "ztop": z0 + th})
        region_ids = {r.get("elementId") for r in g.get("regions") or []}
        for im in g.get("images") or []:
            if im.get("elementId") in region_ids or not im.get("path"):
                continue
            q = _image_quad(im, g.get("bbox") or (-1, -1, 1, 1))
            cards |= fill_rings(frame, [to_canvas(np.asarray(q, np.float64))])
    return labels, infos, cards


def _image_quad(im: dict, bbox) -> list:
    m, w, h = im.get("matrix"), im.get("width"), im.get("height")
    if m and len(m) == 6 and w and h:
        a, b, c, d, e, f = (float(v) for v in m)
        return [(a * px + c * py + e, b * px + d * py + f) for px, py in ((0, h), (w, h), (w, 0), (0, 0))]
    x0, y0, x1, y1 = (float(v) for v in (im.get("bbox") or bbox))
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def interiors(labels: np.ndarray, infos: list, frame: Frame, cards: np.ndarray) -> dict:
    """label -> interior bool mask (eroded by the piece's bevel + 2 px; raster cards removed)."""
    out = {}
    for i, inf in enumerate(infos):
        if inf is None:
            continue
        m = labels == i
        if not m.any():
            continue
        margin = frame.units(inf["bevel"]) + 2.0
        out[i] = m & (edt(m) > margin) & ~cards
    return out


def sweep(caster: np.ndarray, dz_px: float, light_angle: float, elevation: float) -> np.ndarray:
    """Generous zone a caster may shade on a receiver dz_px (pixels of height) below it: the mask swept away
    from the key light (disk edge: elevation + LIGHT_HALF_ANGLE, +20 %) and dilated by the penumbra."""
    from scipy import ndimage
    if dz_px <= 0 or not caster.any():
        return np.zeros_like(caster)
    a = math.radians(light_angle)
    dx, dy = -math.sin(a), math.cos(a)        # away from the light; image rows grow downward
    far = 1.2 * dz_px * math.tan(math.radians(min(80.0, elevation + LIGHT_HALF_ANGLE)))
    zone = caster.copy()
    for t in np.linspace(0.0, far, max(2, int(far / 1.5) + 1)):
        zone |= ndimage.shift(caster.astype(np.uint8), (dy * t, dx * t), order=0, mode="constant", cval=0) > 0
    return ndimage.binary_dilation(zone, iterations=max(1, int(round(0.4 * dz_px + 3))))


def shadow_candidates(labels: np.ndarray, infos: list, frame: Frame, lighting: dict) -> dict:
    """label -> pixels where a HIGHER layer's cast shadow may fall (plate: everywhere)."""
    angle = float(lighting.get("angle", -45.0))
    elev = float(lighting.get("elevation", 50.0))
    layers = {}
    for i, inf in enumerate(infos):
        if inf is not None and inf["kind"] == "region":
            layers.setdefault(inf["layer"], {"ztop": inf["ztop"], "labels": []})["labels"].append(i)
    masks = {lid: np.isin(labels, v["labels"]) for lid, v in layers.items()}
    out = {}
    cache = {}
    for i, inf in enumerate(infos):
        if inf is None:
            continue
        if inf["kind"] == "plate":
            out[i] = np.ones(labels.shape, bool)
            continue
        zone = np.zeros(labels.shape, bool)
        for lid, v in layers.items():
            if v["ztop"] <= inf["ztop"] + 1e-6:
                continue
            key = (lid, round(v["ztop"] - inf["ztop"], 4))
            if key not in cache:
                cache[key] = sweep(masks[lid], frame.units(v["ztop"] - inf["ztop"]), angle, elev)
            zone |= cache[key]
        out[i] = zone
    return out


def shadow_mask(img: np.ndarray, img_ns: np.ndarray) -> np.ndarray:
    """Pixels darkened / changed by cast shadows: shadow-on vs shadow-off render differ by > SHADOW_DE."""
    from scipy import ndimage
    d = np.linalg.norm(srgb_to_lab(img[..., :3]) - srgb_to_lab(img_ns[..., :3]), axis=-1)
    return ndimage.binary_dilation(d > SHADOW_DE, iterations=1)


# ------------------------------------------------------------------------------------------------ reference
def reference_png(project_dir: Path, project: dict, frame: Frame, out: Path) -> np.ndarray:
    """normalized.svg (shadow filters already turned into 3D shadows) rasterised into the render grid."""
    import resvg_py
    from PIL import Image
    import io
    text = (project_dir / "normalized.svg").read_text(encoding="utf-8")
    vb = [float(v) for v in project["source"]["viewBox"]]
    k = 2.0 / max(vb[2], vb[3])
    cx, cy = vb[0] + vb[2] / 2, vb[1] + vb[3] / 2
    art = project["canvas"].get("art") or {}
    sa, ax, ay = float(art.get("scale", 1.0)), float(art.get("x", 0.0)), float(art.get("y", 0.0))
    h = frame.h
    x0 = cx + (-h - ax) / (k * sa)
    y0 = cy + (ay - h) / (k * sa)
    w = 2 * h / (k * sa)
    m = re.search(r"<svg\b[^>]*>", text)
    head = m.group(0)
    new = re.sub(r'\s(viewBox|width|height|preserveAspectRatio)="[^"]*"', "", head)
    new = new[:-1] + f' viewBox="{x0:.6f} {y0:.6f} {w:.6f} {w:.6f}" width="{frame.n}" height="{frame.n}">'
    text = text[:m.start()] + new + text[m.end():]
    png = resvg_py.svg_to_bytes(svg_string=text, width=frame.n, height=frame.n,
                                resources_dir=str(project_dir))
    out.write_bytes(bytes(png))
    return np.asarray(Image.open(io.BytesIO(bytes(png))).convert("RGBA")).astype(np.float64)


# ------------------------------------------------------------------------------------------------ import
def import_icon(name: str, ws: Path) -> dict:
    from bis import svg as bsvg
    from bis.models import Project
    d = ws / name.replace(" ", "_")
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    src = CORPUS / f"{name}.svg"
    t = time.perf_counter()
    res = bsvg.import_svg(src.read_bytes(), src.name, d)
    now = "2026-01-01T00:00:00Z"
    proj = Project(id=d.name.lower(), name=name, createdAt=now, updatedAt=now, source=res.source,
                   elements=res.elements, layers=res.layers, canvas=res.canvas)
    bundle = bsvg.build_geometry(d, proj, f"/files/fidelity/{d.name}")
    gpath = bsvg.geometry_path(d, bundle)
    (d / "project.json").write_text(proj.model_dump_json(indent=1), encoding="utf-8")
    return {"dir": str(d), "project": str(d / "project.json"), "geometryPath": str(gpath),
            "importSeconds": round(time.perf_counter() - t, 3)}


# ------------------------------------------------------------------------------------------------ analysis
def analyse_icon(name: str, entry: dict, out: Path, qualities: list, size: int) -> dict:
    project = json.loads(Path(entry["project"]).read_text(encoding="utf-8"))
    bundle = json.loads(Path(entry["geometryPath"]).read_text(encoding="utf-8"))
    frame = Frame(size, float((project.get("camera") or {}).get("zoom", 1.0)))
    ref = reference_png(Path(entry["dir"]), project, frame, out / "ref" / f"{name}.png")
    labels, infos, cards = label_map(project, bundle, frame)
    inner = interiors(labels, infos, frame, cards)
    cand = shadow_candidates(labels, infos, frame, project.get("lighting") or {})
    ref_lab = srgb_to_lab(ref[..., :3])
    rec = {"layers": len(project.get("layers") or []), "plateFill": (project["canvas"]["plate"].get("fill") or {}).get("type")}
    masks = {}
    for q in qualities:
        p = out / "renders" / q / f"{name}.png"
        if not p.is_file():
            continue
        img = load_rgba(p)
        ok = (img[..., 3] > 250) & (ref[..., 3] > 250)
        pns = out / "renders" / f"{q}_ns" / f"{name}.png"
        shade = shadow_mask(img, load_rgba(pns)) if pns.is_file() else np.zeros(ok.shape, bool)
        d = np.linalg.norm(srgb_to_lab(img[..., :3]) - ref_lab, axis=-1)
        dl = srgb_to_lab(img[..., :3])[..., 0] - ref_lab[..., 0]
        regions, plate = [], None
        meas = np.zeros(labels.shape, bool)
        for i, inf in enumerate(infos):
            if inf is None or i not in inner:
                continue
            m = inner[i] & ok & ~(shade & cand[i])
            npx = int(m.sum())
            item = {"n": npx}
            if npx >= MIN_PIXELS:
                vals = d[m]
                item.update(dE=round(float(vals.mean()), 2), dE90=round(float(np.percentile(vals, 90)), 2),
                            dL=round(float(dl[m].mean()), 2),
                            ref=[int(v) for v in ref[..., :3][m].mean(0).round()],
                            got=[int(v) for v in img[..., :3][m].mean(0).round()])
                item["floor"] = gamut_floor(item["ref"], (project.get("render") or {}).get("colorMode", "neutral"))
                meas |= m
            if inf["kind"] == "plate":
                plate = item
            else:
                item.update({k: inf[k] for k in ("layer", "layerName", "region", "elementId", "paintType", "color",
                                                 "opacity", "preset")})
                regions.append(item)
        masks[q] = (d, meas)
        rec[q] = {"plate": plate, "regions": regions, "shadowFrac": round(float(shade.mean()), 3)}
    rec["_masks"] = masks
    rec["_labels"] = labels
    return rec


def summarise(results: dict, qualities: list) -> dict:
    summ = {}
    for q in qualities:
        reg = [r["dE"] for n, rec in results.items() for r in (rec.get(q) or {}).get("regions", []) if "dE" in r]
        pl = [rec[q]["plate"]["dE"] for rec in results.values() if (rec.get(q) or {}).get("plate") and
              "dE" in rec[q]["plate"]]
        icon_worst = {n: max([r["dE"] for r in (rec.get(q) or {}).get("regions", []) if "dE" in r] or [0.0])
                      for n, rec in results.items()}
        secs = [rec["timing"][q] for rec in results.values() if rec.get("timing", {}).get(q)]
        regs_all = [r for rec in results.values() for r in (rec.get(q) or {}).get("regions", []) if "dE" in r]
        pls_all = [rec[q]["plate"] for rec in results.values() if "dE" in ((rec.get(q) or {}).get("plate") or {})]

        def beyond(items, target):
            # misses that are not explained by the view transform's gamut (dE > max(target, floor + slack))
            return [r for r in items if r["dE"] > max(target, r.get("floor", 0.0) + GAMUT_SLACK)]
        summ[q] = {
            "regions": len(reg),
            "regionMean": round(float(np.mean(reg)), 2) if reg else None,
            "regionP90": round(float(np.percentile(reg, 90)), 2) if reg else None,
            "regionMax": round(float(np.max(reg)), 2) if reg else None,
            "regionPass": round(float(np.mean(np.asarray(reg) <= TARGET_REGION)), 3) if reg else None,
            "regionFail": int(np.sum(np.asarray(reg) > TARGET_REGION)) if reg else 0,
            "plates": len(pl),
            "plateMean": round(float(np.mean(pl)), 2) if pl else None,
            "plateP90": round(float(np.percentile(pl, 90)), 2) if pl else None,
            "plateMax": round(float(np.max(pl)), 2) if pl else None,
            "platePass": round(float(np.mean(np.asarray(pl) <= TARGET_PLATE)), 3) if pl else None,
            "plateFail": int(np.sum(np.asarray(pl) > TARGET_PLATE)) if pl else 0,
            "regionFailBeyondGamut": len(beyond(regs_all, TARGET_REGION)),
            "plateFailBeyondGamut": len(beyond(pls_all, TARGET_PLATE)),
            "iconsAllPass": int(sum(1 for n, rec in results.items() if icon_worst[n] <= TARGET_REGION and
                                    (((rec.get(q) or {}).get("plate") or {}).get("dE", 0.0) <= TARGET_PLATE))),
            "secondsP50": round(statistics.median(secs), 3) if secs else None,
            "secondsP90": round(float(np.percentile(secs, 90)), 3) if secs else None,
        }
    return summ


def worst_list(results: dict, qualities: list, k: int = 30) -> list:
    rows = []
    for n, rec in results.items():
        for q in qualities:
            for r in (rec.get(q) or {}).get("regions", []):
                if "dE" in r:
                    rows.append((r["dE"], n, q, r["layerName"], r["region"], r.get("color") or r.get("paintType"),
                                 r["ref"], r["got"], r["n"], r.get("floor", 0.0)))
            pl = (rec.get(q) or {}).get("plate") or {}
            if "dE" in pl:
                rows.append((pl["dE"] * TARGET_REGION / TARGET_PLATE, n, q, "PLATE", -1, rec.get("plateFill"),
                             pl["ref"], pl["got"], pl["n"], pl.get("floor", 0.0)))
    rows.sort(reverse=True)
    return [{"score": round(r[0], 2), "icon": r[1], "q": r[2], "layer": r[3], "region": r[4], "paint": r[5],
             "ref": r[6], "got": r[7], "n": r[8], "floor": r[9]} for r in rows[:k]]


# ------------------------------------------------------------------------------------------------ sheet
def _heat(d: np.ndarray, meas: np.ndarray) -> np.ndarray:
    t = np.clip(d / 24.0, 0, 1)
    rgb = np.stack([np.clip(2 * t, 0, 1), np.clip(2 - 2 * t, 0, 1) * (d > 0), np.zeros_like(t)], -1) * 255
    out = np.full(d.shape + (3,), 40.0)
    out[meas] = rgb[meas]
    return out


def contact_sheets(results: dict, out: Path, qualities: list, names: list, size: int, per_page: int = 6) -> list:
    """Worst-offender pages ``sheet_worst_NN.png`` (``per_page`` icons each, most severe first)."""
    for old in out.glob("sheet_worst*.png"):
        old.unlink()
    return [contact_sheet(results, out, qualities, names[i:i + per_page], size, f"sheet_worst_{i // per_page + 1:02d}.png")
            for i in range(0, len(names), per_page)]


def contact_sheet(results: dict, out: Path, qualities: list, names: list, size: int,
                  filename: str = "sheet_worst.png") -> Path:
    from PIL import Image, ImageDraw
    from scipy import ndimage
    cols = 1 + 2 * len(qualities)
    cell = size
    pad = 18
    W = cols * cell
    sheet = Image.new("RGB", (W, len(names) * (cell + pad)), (30, 30, 34))
    dr = ImageDraw.Draw(sheet)
    for row, n in enumerate(names):
        rec = results[n]
        y = row * (cell + pad) + pad
        tiles = [out / "ref" / f"{n}.png"] + [out / "renders" / q / f"{n}.png" for q in qualities]
        for c, p in enumerate(tiles):
            bg = Image.new("RGBA", (cell, cell), (128, 128, 128, 255))
            if p.is_file():
                im = Image.open(p).convert("RGBA").resize((cell, cell))
                bg.alpha_composite(im)
            sheet.paste(bg.convert("RGB"), (c * cell, y))
        for k, q in enumerate(qualities):
            if q not in rec["_masks"]:
                continue
            d, meas = rec["_masks"][q]
            heat = _heat(d, meas)
            edge = meas & ~ndimage.binary_erosion(meas)
            heat[edge] = (255, 255, 255)
            sheet.paste(Image.fromarray(heat.astype(np.uint8)).resize((cell, cell), Image.NEAREST),
                        ((1 + len(qualities) + k) * cell, y))
        txt = n
        for q in qualities:
            qq = rec.get(q) or {}
            regs = [r["dE"] for r in qq.get("regions", []) if "dE" in r]
            pl = (qq.get("plate") or {}).get("dE")
            txt += f"   {q}: worst {max(regs) if regs else 0:.1f} mean {np.mean(regs) if regs else 0:.1f} plate {pl}"
        dr.text((4, y - pad + 3), txt, fill=(235, 235, 235))
    path = out / filename
    sheet.save(path)
    return path


# ------------------------------------------------------------------------------------------------ main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--icons", nargs="*")
    ap.add_argument("--size", type=int, default=256)
    ap.add_argument("--qualities", nargs="*", default=["draft", "preview"])
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--samples", type=int, default=None, help="preview spp (default: tier default)")
    ap.add_argument("--reuse", action="store_true", help="reuse the bundles of a previous run in --out")
    ap.add_argument("--analyse-only", action="store_true", help="re-measure existing renders")
    ap.add_argument("--worst", type=int, default=16, help="rows in the worst-offender sheet")
    ap.add_argument("--label", default="")
    ap.add_argument("--worker-root", default="", help="run the worker from another checkout (A/B against old code)")
    a = ap.parse_args(argv)
    size = max(64, min(256, a.size))
    out = Path(a.out)
    for sub in ["ref", "ws"] + [f"renders/{q}{t}" for q in a.qualities for t in ("", "_ns")]:
        (out / sub).mkdir(parents=True, exist_ok=True)
    names = a.icons or sorted((p.stem for p in CORPUS.glob("*.svg")), key=str.lower)
    index_path = out / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else {}
    errors = {}
    if not (a.reuse or a.analyse_only):
        for n in names:
            try:
                index[n] = import_icon(n, out / "ws")
            except Exception as ex:  # noqa: BLE001
                errors[n] = f"import: {ex}"
                traceback.print_exc()
        index_path.write_text(json.dumps(index, indent=1), encoding="utf-8")
        print(f"imported {len(names) - len(errors)} icons")
    timing = {n: {} for n in names}
    if not a.analyse_only:
        import worker_client as wc
        if a.worker_root:
            wc.REPO = Path(a.worker_root)
        extra = ["--port", str(a.port)] if a.port else []
        w = wc.Worker(warmup="full", extra=extra)
        try:
            for n in names:
                e = index.get(n)
                if not e:
                    continue
                proj = json.loads(Path(e["project"]).read_text(encoding="utf-8"))
                proj_ns = json.loads(json.dumps(proj))
                for L in proj_ns.get("layers") or []:
                    L["shadow"] = {"kind": "none", "opacity": 0.0}
                for q in a.qualities:
                    for tag, pj in (("", proj), ("_ns", proj_ns)):
                        args = {"project": pj, "geometryPath": e["geometryPath"], "quality": q, "size": size,
                                "out": str(out / "renders" / f"{q}{tag}" / f"{n}.png")}
                        if q != "draft" and a.samples:
                            args["samples"] = a.samples
                        try:
                            r = w.result("render", args)
                            if not tag:
                                timing[n][q] = r["seconds"]
                        except Exception as ex:  # noqa: BLE001
                            errors[n] = f"render {q}{tag}: {ex}"
                print(f"{n:18s} " + "  ".join(f"{q} {timing[n].get(q, float('nan')):.2f}s" for q in a.qualities),
                      flush=True)
        finally:
            w.close()
        (out / "timing.json").write_text(json.dumps(timing, indent=1), encoding="utf-8")
    elif (out / "timing.json").is_file():
        timing.update(json.loads((out / "timing.json").read_text(encoding="utf-8")))
    results = {}
    for n in names:
        if n not in index:
            continue
        try:
            rec = analyse_icon(n, index[n], out, a.qualities, size)
            rec["timing"] = timing.get(n, {})
            results[n] = rec
        except Exception as ex:  # noqa: BLE001
            errors[n] = f"analyse: {ex}"
            traceback.print_exc()
    summary = summarise(results, a.qualities)
    worst = worst_list(results, a.qualities)

    def icon_score(n):
        s = 0.0
        for q in a.qualities:
            qq = results[n].get(q) or {}
            regs = [r["dE"] for r in qq.get("regions", []) if "dE" in r]
            s = max(s, max(regs or [0.0]) / TARGET_REGION, ((qq.get("plate") or {}).get("dE") or 0.0) / TARGET_PLATE)
        return s
    order = sorted(results, key=icon_score, reverse=True)
    sheets = contact_sheets(results, out, a.qualities, order[:a.worst], size)
    clean = {n: {k: v for k, v in rec.items() if not k.startswith("_")} for n, rec in results.items()}
    report = {"label": a.label, "size": size, "qualities": a.qualities, "targets": {"region": TARGET_REGION,
              "plate": TARGET_PLATE}, "summary": summary, "worst": worst, "errors": errors,
              "iconOrder": order, "icons": clean}
    (out / "fidelity.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    print("worst:")
    for wr in worst[:20]:
        print(f"  {wr['score']:6.2f} {wr['icon']:16s} {wr['q']:8s} {wr['layer']:>14s} r{wr['region']:<3d} "
              f"{str(wr['paint']):10s} ref {wr['ref']} got {wr['got']} n {wr['n']} floor {wr['floor']}")
    if errors:
        print("errors:", json.dumps(errors, indent=1))
    print("sheets:", *sheets, sep="\n  ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
