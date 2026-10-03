"""Debug CLI for the SVG pipeline.

    .venv/Scripts/python.exe -m bis.svg.cli "<svg or folder>" [...] --out <dir>
        [--strategy smart] [--tile 192] [--texture 512] [--quiet]

(run from ``server/`` or with ``server/`` on ``sys.path``; e.g. ``PYTHONPATH=server``).

For every SVG it writes ``<out>/<stem>/``: ``project/`` (the project dir: source/normalized SVG,
elements.json, cache/ with layer SVGs + textures + geometry), ``project.json``, ``geometry.json``,
``layer-<id>.png`` and ``sheet.png`` - a contact sheet:

    original | normalized | plate | layer 1..n | recomposed | silhouette splines | canvas (D9)

With several inputs it prints a summary table (icon, elements, layers, plate, warnings, timings,
fidelity diffs) and writes ``<out>/summary.json``. Exit code 1 if any import failed."""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import json
import math
import re
import sys
import time
import traceback
from pathlib import Path
from typing import List, Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bis.models import Project  # noqa: E402
from bis.svg import build_geometry, import_svg, read_store  # noqa: E402
from bis.svg import raster, textures  # noqa: E402
from bis.svg.common import fmt  # noqa: E402
from bis.svg.geometry import splines_to_d  # noqa: E402


def make_project(stem: str, res) -> Project:
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    return Project(id=re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-") or "icon", name=stem, createdAt=now,
                   updatedAt=now, source=res.source, elements=res.elements, layers=res.layers, canvas=res.canvas)


# ----------------------------------------------------------------------------------------------
# canvas preview (parametric plate + art mapped by canvas.art) - mirrors what Blender builds
# ----------------------------------------------------------------------------------------------
def plate_outline(shape: str, corner: float, n: int = 256) -> List[tuple]:
    if shape == "circle":
        return [(math.cos(t), math.sin(t)) for t in np.linspace(0, 2 * math.pi, n, endpoint=False)]
    if shape == "squircle":
        out = []
        for t in np.linspace(0, 2 * math.pi, n, endpoint=False):
            c, s = math.cos(t), math.sin(t)
            out.append((math.copysign(abs(c) ** 0.4, c), math.copysign(abs(s) ** 0.4, s)))
        return out
    if shape == "rounded":
        r = max(0.0, min(1.0, corner * 2))
        out = []
        for cx, cy, a0 in ((1 - r, 1 - r, 0), (-1 + r, 1 - r, 90), (-1 + r, -1 + r, 180), (1 - r, -1 + r, 270)):
            for k in range(17):
                a = math.radians(a0 + 90 * k / 16)
                out.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        return out
    return [(1, 1), (-1, 1), (-1, -1), (1, -1)]


def _fill_svg(fill: dict, gid: str) -> tuple:
    t = fill.get("type")
    if t == "solid":
        return f'fill="{fill["color"]}" fill-opacity="{fill.get("opacity", 1)}"', ""
    if t in ("system-light", "system-dark"):
        a, b = ("#ffffff", "#e4e5ea") if t == "system-light" else ("#3a3a3f", "#111114")
        return (f'fill="url(#{gid})"', f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse" x1="0" y1="1" '
                f'x2="0" y2="-1"><stop offset="0" stop-color="{a}"/><stop offset="1" stop-color="{b}"/></linearGradient>')
    if t == "linear":
        stops = "".join(f'<stop offset="{s["offset"]}" stop-color="{s["color"]}" stop-opacity="{s["opacity"]}"/>'
                        for s in fill["stops"])
        (x1, y1), (x2, y2) = fill["start"], fill["end"]
        return (f'fill="url(#{gid})"', f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse" x1="{x1}" '
                f'y1="{y1}" x2="{x2}" y2="{y2}">{stops}</linearGradient>')
    if t == "radial":
        stops = "".join(f'<stop offset="{s["offset"]}" stop-color="{s["color"]}" stop-opacity="{s["opacity"]}"/>'
                        for s in fill["stops"])
        cx, cy = fill["center"]
        fx, fy = fill.get("focal") or fill["center"]
        mt = fill.get("matrix")
        tf = f' gradientTransform="matrix({" ".join(str(v) for v in mt)})"' if mt else ""
        return (f'fill="url(#{gid})"', f'<radialGradient id="{gid}" gradientUnits="userSpaceOnUse" cx="{cx}" cy="{cy}" '
                f'r="{fill["radius"]}" fx="{fx}" fy="{fy}"{tf}>{stops}</radialGradient>')
    return 'fill="none"', ""


def canvas_preview(project: Project, art_png: np.ndarray, size: int) -> np.ndarray:
    """Render the canvas as the 3D scene sees it from the front: plate (parametric shape, canvas
    fill) + the layer art composite mapped by canvas.art. Canvas space is y-up -1..1."""
    c = project.canvas
    pts = plate_outline(c.shape, c.cornerRadius)
    d = "M" + " L".join(f"{fmt(x, 5)} {fmt(y, 5)}" for x, y in pts) + " Z"
    attr, defs = _fill_svg(c.plate.fill.model_dump(), "pf")
    s, tx, ty = c.art.scale, c.art.x, c.art.y
    uri = "data:image/png;base64," + base64.b64encode(raster.encode_png(art_png, 1)).decode()
    # art square [-1,1]² (y up) -> canvas; the PNG is y-down, so flip it back inside the y-up frame
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
           f'viewBox="-1.12 -1.12 2.24 2.24" width="{size}" height="{size}"><defs>{defs}</defs>'
           f'<g transform="scale(1 -1)"><path d="{d}" {attr}/>'
           f'<g transform="matrix({s} 0 0 {s} {tx} {ty})"><image x="-1" y="-1" width="2" height="2" '
           f'transform="matrix(1 0 0 -1 0 0)" preserveAspectRatio="none" xlink:href="{uri}"/></g></g></svg>')
    return raster.render_svg(svg, size, size)


# ----------------------------------------------------------------------------------------------
# sheet
# ----------------------------------------------------------------------------------------------
def _checker(w: int, h: int, n: int = 12) -> np.ndarray:
    yy, xx = np.mgrid[0:h, 0:w]
    v = np.where(((xx // n) + (yy // n)) % 2 == 0, 255, 228).astype(np.uint8)
    out = np.zeros((h, w, 4), np.uint8)
    out[..., 0] = out[..., 1] = out[..., 2] = v
    out[..., 3] = 255
    return out


def contact_sheet(tiles: List[tuple], path: Path, title: str) -> None:
    pad, lab = 8, 30
    w = max(t[1].shape[1] for t in tiles)
    h = max(t[1].shape[0] for t in tiles)
    sheet = Image.new("RGB", (pad + len(tiles) * (w + pad), h + 2 * pad + lab + 18), (250, 250, 252))
    dr = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("arial.ttf", 13)
        small = ImageFont.truetype("arial.ttf", 11)
    except OSError:
        font = small = ImageFont.load_default()
    dr.text((pad, 4), title, fill=(20, 20, 30), font=font)
    for k, (label, im) in enumerate(tiles):
        x = pad + k * (w + pad)
        y = pad + 18
        tile = raster.over(_checker(im.shape[1], im.shape[0]), im)
        sheet.paste(Image.fromarray(tile[..., :3]), (x, y))
        for li, line in enumerate(label.split("\n")[:2]):
            dr.text((x, y + h + 3 + li * 13), line[:34], fill=(40, 40, 50), font=small)
    sheet.save(path)


def silhouette_tile(bundle, project: Project, base: np.ndarray, size: int) -> np.ndarray:
    paths = []
    palette = ["#e11d48", "#2563eb", "#16a34a", "#d97706", "#7c3aed", "#0891b2", "#db2777", "#65a30d"]
    for k, L in enumerate(project.layers):
        lg = bundle.layers.get(L.id)
        if lg is None:
            continue
        d = splines_to_d(lg.silhouette)
        if d:
            col = palette[k % len(palette)]
            paths.append(f'<path d="{d}" fill="{col}" fill-opacity="0.18" stroke="{col}" stroke-width="0.008"/>')
            for s in lg.silhouette:
                for p in s.points[:: max(1, len(s.points) // 64)]:
                    paths.append(f'<circle cx="{p.co[0]}" cy="{p.co[1]}" r="0.006" fill="{col}"/>')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="-1 -1 2 2" width="{size}" height="{size}">'
           f'<g transform="scale(1 -1)">{"".join(paths)}</g></svg>')
    over = raster.render_svg(svg, size, size)
    faded = base.copy()
    faded[..., 3] = (faded[..., 3] * 0.35).astype(np.uint8)
    return raster.over(faded, over)


def spline_error(bundle, project: Project, store, size: int) -> float:
    """% of pixels where silhouette splines and the layer's flat mask (clipped to the plate like
    the 3D geometry) disagree by > 25 % coverage."""
    from bis.svg.paths import clean_d
    from bis.svg.plate import plate_clip_path

    worst = 0.0
    sq = store.art.square_view_box()
    clip = plate_clip_path(store)
    clip_mask = None
    if clip is not None:
        cs = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{" ".join(fmt(v, 6) for v in sq)}" '
              f'width="{size}" height="{size}"><path fill="#000" d="{clean_d(clip, 4)}"/></svg>')
        clip_mask = raster.render_svg(cs, size, size)[..., 3].astype(int)
    for L in project.layers:
        lg = bundle.layers.get(L.id)
        if lg is None:
            continue
        members = [store.get(e) for e in L.elementIds if e in store.index]
        mask_svg = textures.layer_svg(members, store.gradients, sq, None, size=(size, size), silhouette="#000")
        alpha = raster.render_svg(mask_svg, size, size)[..., 3].astype(int)
        if clip_mask is not None:
            alpha = alpha * clip_mask // 255
        d = splines_to_d(lg.silhouette)
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="-1 -1 2 2" width="{size}" height="{size}">'
               f'<path transform="scale(1 -1)" fill="#000" d="{d}"/></svg>')
        m = raster.render_svg(svg, size, size)[..., 3].astype(int)
        worst = max(worst, float((np.abs(m - alpha) > 64).mean() * 100))
    return round(worst, 3)


# ----------------------------------------------------------------------------------------------
# driver
# ----------------------------------------------------------------------------------------------
def run_one(svg_path: Path, out: Path, strategy: str, tile: int, texture: int) -> dict:
    stem = svg_path.stem
    odir = out / re.sub(r"[^A-Za-z0-9_. -]+", "_", stem)
    pdir = odir / "project"
    data = svg_path.read_bytes()
    t0 = time.perf_counter()
    res = import_svg(data, svg_path.name, pdir, strategy)  # type: ignore[arg-type]
    t_import = time.perf_counter() - t0
    project = make_project(stem, res)
    t0 = time.perf_counter()
    bundle = build_geometry(pdir, project, f"/files/projects/{project.id}", texture_size=texture)
    t_geom = time.perf_counter() - t0
    t0 = time.perf_counter()
    build_geometry(pdir, project, f"/files/projects/{project.id}", texture_size=texture)
    t_hit = time.perf_counter() - t0
    (odir / "project.json").write_text(project.model_dump_json(indent=1), encoding="utf-8")
    (odir / "geometry.json").write_text(bundle.model_dump_json(), encoding="utf-8")
    store = read_store(pdir)
    vb = store.view_box
    W = tile
    H = max(1, int(round(tile * vb[3] / vb[2])))
    src_text = data.decode("utf-8", "replace")
    original = raster.render_svg(raster.strip_filters(src_text), W, H)
    original_fx = raster.render_source(src_text, W, H)
    normalized = raster.render_svg((pdir / "normalized.svg").read_text(encoding="utf-8"), W, H)
    tiles = [("original", original_fx), ("normalized", normalized)]
    comps = []
    if bundle.plate:
        plate_img = raster.render_svg(Path(bundle.plate["svgPath"]).read_text(encoding="utf-8"), W, H)
        tiles.append((f"plate · {bundle.plate['shape']}", plate_img))
        comps.append(plate_img)
    for L in project.layers:
        lg = bundle.layers[L.id]
        svg = (pdir / "cache" / Path(lg.svg).name).read_text(encoding="utf-8")
        img = raster.render_svg(svg, W, H)
        raster_png = odir / f"layer-{L.id}.png"
        Image.fromarray(img).save(raster_png)
        tiles.append((f"{L.id} {L.name}\n{len(L.elementIds)} el · bevel {L.depth.bevel:.3f} · sr {lg.safeRadius:.3f}", img))
        comps.append(img)
    recomposed = raster.composite(comps) if comps else np.zeros_like(normalized)
    tiles.append(("recomposed", recomposed))
    sq = store.art.square_view_box()
    art_sq = raster.composite([raster.render_svg(textures.layer_svg(
        [store.get(e) for e in L.elementIds if e in store.index], store.gradients, sq, pdir, size=(tile, tile)),
        tile, tile) for L in project.layers]) if project.layers else np.zeros((tile, tile, 4), np.uint8)
    tiles.append(("silhouette splines", silhouette_tile(bundle, project, art_sq, tile)))
    tiles.append((f"canvas · art×{project.canvas.art.scale:.3f}", canvas_preview(project, art_sq, tile)))
    raster_only = any(e.kind == "image" for e in res.elements)
    metrics = {
        "icon": stem, "elements": len(res.elements), "layers": len(res.layers),
        "layerNames": [L.name for L in res.layers],
        "plate": res.canvas.shape if res.source.plateDetected else None,
        "plateIou": bundle.plate.get("iou") if bundle.plate else None,
        "artScale": res.canvas.art.scale, "warnings": res.warnings, "raster": raster_only,
        "importMs": round(t_import * 1000, 1), "geometryMs": round(t_geom * 1000, 1),
        "cacheHitMs": round(t_hit * 1000, 2),
        "normDiff": raster.diff_pct(original, normalized),
        "recompDiff": raster.diff_pct(normalized, recomposed),
        "sourceDiff": raster.diff_pct(original, recomposed),
        "splineErr": spline_error(bundle, project, store, tile),
        "safeRadius": {L.id: bundle.layers[L.id].safeRadius for L in project.layers},
        "shadows": sum(1 for e in res.elements if e.shadow),
    }
    title = (f"{stem} - {len(res.elements)} elements, {len(res.layers)} layers, plate={metrics['plate']}, "
             f"import {metrics['importMs']:.0f} ms, geometry {metrics['geometryMs']:.0f} ms, "
             f"diff src {metrics['sourceDiff']}% / recomp {metrics['recompDiff']}%")
    contact_sheet(tiles, odir / "sheet.png", title)
    return metrics


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bis.svg.cli", description=__doc__.split("\n")[0])
    ap.add_argument("inputs", nargs="+", help="SVG files and/or folders")
    ap.add_argument("--out", required=True)
    ap.add_argument("--strategy", default="smart", choices=["smart", "group", "color", "element", "single"])
    ap.add_argument("--tile", type=int, default=192, help="contact sheet tile size (px)")
    ap.add_argument("--texture", type=int, default=512, help="layer texture size for the debug run (px)")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    files: List[Path] = []
    for inp in a.inputs:
        p = Path(inp)
        files += sorted(p.glob("*.svg")) if p.is_dir() else [p]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rows, failed = [], 0
    for f in files:
        try:
            m = run_one(f, out, a.strategy, a.tile, a.texture)
        except Exception as ex:  # noqa: BLE001
            failed += 1
            m = {"icon": f.stem, "error": f"{type(ex).__name__}: {ex}", "traceback": traceback.format_exc()}
            print(f"{f.stem}: ERROR {m['error']}", file=sys.stderr)
        rows.append(m)
        if not a.quiet and "error" not in m:
            print(f"{m['icon'][:22]:22s} el={m['elements']:3d} layers={m['layers']} plate={str(m['plate']):8s} "
                  f"warn={len(m['warnings'])} import={m['importMs']:6.0f}ms geom={m['geometryMs']:6.0f}ms "
                  f"hit={m['cacheHitMs']:5.1f}ms norm={m['normDiff']:.3f}% recomp={m['recompDiff']:.3f}% "
                  f"src={m['sourceDiff']:.3f}% spline={m['splineErr']:.3f}%{' [raster]' if m['raster'] else ''}")
    (out / "summary.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    ok = [r for r in rows if "error" not in r]
    if len(files) > 1 and ok:
        print(f"\n{len(ok)}/{len(rows)} imported | plates {sum(1 for r in ok if r['plate'])} | "
              f"max recomp diff (non-raster) {max([r['recompDiff'] for r in ok if not r['raster']] or [0]):.3f}% | "
              f"median import {np.median([r['importMs'] for r in ok]):.0f} ms")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
