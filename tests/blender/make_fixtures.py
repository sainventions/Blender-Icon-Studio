"""Generate realistic Project + GeometryBundle fixtures from corpus icons for the Blender worker tests.

Runs in the backend venv (Python 3.13, picosvg/skia-pathops/shapely/resvg-py/scipy):

    .venv/Scripts/python.exe tests/blender/make_fixtures.py [--icons Maps Photos ...] [--out DIR] [--tex 2048]

Preferred source is workstream A's package (``bis.svg.import_svg`` + ``build_geometry``) when it exists
and works; otherwise the research prototype ``docs/research/svg_prototype.py`` is used and its output is
converted to the shared contract (×2 to art space — D2, safe radius per layer, edge-padded RGBA layer
textures rasterised with resvg, plate → canvas.plate + canvas.art per D9).

Output per icon: ``<out>/<Icon>/project.json``, ``<out>/<Icon>/cache/geometry-<hash>.json`` and layer PNGs.
``<out>/index.json`` maps icon name -> {project, geometryPath, source}.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = Path(__file__).resolve().parent / "_fixtures"
CORPUS = REPO / "samle icons"
DEFAULT_ICONS = ["Maps", "Photos", "Discord", "Settings", "Spotify", "Calculator"]

LAYER_GAP = 0.13          # PLAN §2 default stack: z_i = i * 0.13
LAYER_THICKNESS = 0.10
LAYER_BEVEL = 0.045


# ----------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(c * 255))) for c in rgb[:3])


def _scale_splines(splines: list, k: float = 2.0) -> list:
    out = []
    for s in splines:
        pts = [{"co": [p["co"][0] * k, p["co"][1] * k],
                "hl": [p["hl"][0] * k, p["hl"][1] * k],
                "hr": [p["hr"][0] * k, p["hr"][1] * k]} for p in s["points"]]
        parent = s.get("parent")
        out.append({"closed": bool(s.get("closed", True)), "hole": bool(s.get("hole", False)),
                    "parent": -1 if parent is None else int(parent), "depth": int(s.get("depth", 0)),
                    "points": pts})
    return out


def _flatten(spline: dict, n: int = 8) -> list:
    pts = spline["points"]
    ring = []
    m = len(pts)
    for i in range(m if spline["closed"] else m - 1):
        a, b = pts[i], pts[(i + 1) % m]
        p0, c1, c2, p1 = a["co"], a["hr"], b["hl"], b["co"]
        for j in range(n):
            t = j / n
            mt = 1 - t
            ring.append((mt ** 3 * p0[0] + 3 * mt * mt * t * c1[0] + 3 * mt * t * t * c2[0] + t ** 3 * p1[0],
                         mt ** 3 * p0[1] + 3 * mt * mt * t * c1[1] + 3 * mt * t * t * c2[1] + t ** 3 * p1[1]))
    return ring


def _shapely_geom(splines: list):
    import shapely
    from shapely.geometry import Polygon
    geom = None
    for s in splines:
        ring = _flatten(s)
        if len(ring) < 3:
            continue
        poly = shapely.make_valid(Polygon(ring))
        if poly.is_empty:
            continue
        geom = poly if geom is None else geom.symmetric_difference(poly)
    return geom


def safe_radius(splines_list: list[list]) -> float:
    """Largest d such that morphological opening by d keeps >= 98 % of each piece's area (critic §2.5)."""
    best = 1.0
    for spl in splines_list:
        g = _shapely_geom(spl)
        if g is None or g.is_empty or g.area <= 1e-9:
            continue
        lo, hi = 0.0, 0.25
        for _ in range(14):
            mid = (lo + hi) / 2
            opened = g.buffer(-mid, quad_segs=8).buffer(mid, quad_segs=8)
            if opened.area >= 0.98 * g.area:
                lo = mid
            else:
                hi = mid
        best = min(best, lo)
    return round(best, 5)


def _bbox_of(splines: list) -> list:
    xs, ys = [], []
    for s in splines:
        for p in s["points"]:
            xs.append(p["co"][0])
            ys.append(p["co"][1])
    if not xs:
        return [0.0, 0.0, 0.0, 0.0]
    return [round(min(xs), 6), round(min(ys), 6), round(max(xs), 6), round(max(ys), 6)]


def _paint_from_proto(p: dict, vb) -> dict:
    """Prototype paint dict -> contract Paint (gradients in ART space, ×2 scale)."""
    if p["type"] == "solid":
        return {"type": "solid", "color": p["hex"], "opacity": 1.0}
    stops = [{"offset": float(s["offset"]), "color": s["hex"], "opacity": float(s.get("opacity", 1.0))}
             for s in p.get("stops", [])]
    from picosvg.svg_transform import Affine2D
    a, b, c, d, e, f = p["transform"]
    G = Affine2D(a, b, c, d, e, f)
    cx, cy = vb[0] + vb[2] / 2, vb[1] + vb[3] / 2
    k = 2.0 / max(vb[2], vb[3])

    def art(pt):
        x, y = G.map_point(pt)
        return [round((x - cx) * k, 6), round((cy - y) * k, 6)]

    co = p["coords"]
    if p["type"] == "linear":
        return {"type": "linear", "stops": stops, "start": art((co["x1"], co["y1"])), "end": art((co["x2"], co["y2"]))}
    # radial: matrix maps gradient space -> art space; centre/radius in gradient space
    N = Affine2D(k, 0, 0, -k, -cx * k, cy * k)   # svg user -> art
    M = N @ G
    return {"type": "radial", "stops": stops, "center": [co["cx"], co["cy"]], "radius": co["r"],
            "focal": [co.get("fx", co["cx"]), co.get("fy", co["cy"])],
            "matrix": [M.a, M.b, M.c, M.d, M.e, M.f]}


def _edge_pad_rgba(png_bytes: bytes):
    """Dilate RGB into transparent pixels (nearest opaque colour); alpha is kept (straight)."""
    import numpy as np
    from PIL import Image
    from scipy import ndimage
    im = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    arr = np.asarray(im).astype(np.float32)
    a = arr[..., 3] / 255.0
    rgb = arr[..., :3].copy()
    # resvg output is premultiplied-free (straight) PNG; un-blend faint edges is not needed
    solid = a > 0.5
    if solid.any() and not solid.all():
        _, (iy, ix) = ndimage.distance_transform_edt(~solid, return_indices=True)
        fill = ~solid
        rgb[fill] = rgb[iy[fill], ix[fill]]
    out = np.dstack([rgb, arr[..., 3]]).clip(0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(out, "RGBA").save(buf, "PNG", optimize=False, compress_level=6)
    return buf.getvalue()


def _defaults_layer(lid: str, name: str, element_ids: list, z: float, bevel: float, shadow_opacity: float) -> dict:
    return {
        "id": lid, "name": name, "elementIds": element_ids, "visible": True, "locked": False,
        "mode": "individual", "fill": {"type": "auto"}, "opacity": 1.0, "blendMode": "normal", "glass": True,
        "transform": {"x": 0.0, "y": 0.0, "scale": 1.0},
        "depth": {"z": z, "thickness": LAYER_THICKNESS, "bevel": bevel, "bevelSegments": 6, "inflate": 0.0},
        "material": {"preset": "liquid_glass", "params": {}},
        "shadow": {"kind": "physical", "opacity": shadow_opacity},
    }


# ----------------------------------------------------------------------------------------------
# preferred: workstream A's package
# ----------------------------------------------------------------------------------------------
def try_package(svg_path: Path, out_dir: Path):
    sys.path.insert(0, str(REPO / "server"))
    try:
        from bis import svg as bsvg  # type: ignore
        from bis.models import Project  # type: ignore
    except Exception:
        return None
    try:
        res = bsvg.import_svg(svg_path.read_bytes(), svg_path.name, out_dir)
        now = _now()
        proj = Project(id=out_dir.name.lower().replace(" ", "-"), name=svg_path.stem, createdAt=now, updatedAt=now,
                       source=res.source, elements=res.elements, layers=res.layers, canvas=res.canvas)
        bundle = bsvg.build_geometry(out_dir, proj, f"/files/fixtures/{out_dir.name}")
        gpath = bsvg.geometry_path(out_dir, bundle)
        (out_dir / "project.json").write_text(proj.model_dump_json(indent=1), encoding="utf-8")
        return {"project": str(out_dir / "project.json"), "geometryPath": str(gpath), "source": "bis.svg"}
    except Exception:
        traceback.print_exc()
        return None


# ----------------------------------------------------------------------------------------------
# fallback: research prototype
# ----------------------------------------------------------------------------------------------
def from_prototype(svg_path: Path, out_dir: Path, tex_size: int) -> dict:
    sys.path.insert(0, str(REPO / "docs" / "research"))
    import resvg_py
    import svg_prototype as proto  # type: ignore

    text = svg_path.read_text(encoding="utf-8")
    res = proto.process(text, "smart")
    vb = res["view_box"]
    pid = "fx-" + hashlib.md5(svg_path.name.encode()).hexdigest()[:8]
    cache = out_dir / "cache"
    cache.mkdir(parents=True, exist_ok=True)

    k = 2.0 / max(vb[2], vb[3])
    # cull layers entirely outside the viewBox (critic §2.3)
    layers_raw = []
    for L in res["layers"]:
        bx = L["bbox_svg"]
        if bx[2] < vb[0] or bx[0] > vb[0] + vb[2] or bx[3] < vb[1] or bx[1] > vb[1] + vb[3]:
            continue
        layers_raw.append(L)

    bg = [L for L in layers_raw if L["is_background"]]
    fg = [L for L in layers_raw if not L["is_background"]]

    canvas = {"platform": "ios", "shape": "squircle", "cornerRadius": 0.225,
              "plate": {"visible": True, "fill": {"type": "solid", "color": "#ffffff", "opacity": 1.0},
                        "material": {"preset": "satin", "params": {}}, "thickness": 0.16, "bevel": 0.04},
              "art": {"scale": 1.0, "x": 0.0, "y": 0.0}}
    plate_info = None
    if bg:
        P = bg[0]
        bb = P["bbox_icon"]                       # ±0.5 space: minx, maxy?, ... (x', y' of corners)
        xs = sorted([bb[0] * 2, bb[2] * 2])
        ys = sorted([bb[1] * 2, bb[3] * 2])
        w = xs[1] - xs[0]
        h = ys[1] - ys[0]
        scale = 2.0 / max(w, h)
        cxp, cyp = (xs[0] + xs[1]) / 2, (ys[0] + ys[1]) / 2
        canvas["art"] = {"scale": round(scale, 6), "x": round(-cxp * scale, 6), "y": round(-cyp * scale, 6)}
        paint = _paint_from_proto(P["regions"][0]["paint"], vb) if P["regions"] else {"type": "solid", "color": "#ffffff"}
        if paint.get("stops"):            # a trailing hard stop at offset 1 only shows past the source plate's
            st = paint["stops"]           # rounder corners (Settings.svg) - drop it for the canvas plate
            while len(st) > 2 and abs(st[-1]["offset"] - st[-2]["offset"]) < 1e-6:
                st.pop()
        if paint["type"] == "linear":     # art -> canvas coordinates
            for key in ("start", "end"):
                x, y = paint[key]
                paint[key] = [round(x * scale + canvas["art"]["x"], 6), round(y * scale + canvas["art"]["y"], 6)]
        canvas["plate"]["fill"] = paint
        plate_info = {"bbox": [xs[0], ys[0], xs[1], ys[1]], "shape": "squircle"}

    elements = []
    layers = []
    geo_layers = {}
    for i, L in enumerate(fg):
        lid = f"L{i}"
        regions = []
        for r in L["regions"]:
            regions.append({"elementId": r["uid"], "paint": _paint_from_proto(r["paint"], vb),
                            "opacity": float(r["opacity"]), "zSub": float(r["z_sub"]),
                            "splines": _scale_splines(r["splines"])})
            elements.append({"id": r["uid"], "name": r.get("orig_id") or r["uid"], "kind": "path",
                             "paint": _paint_from_proto(r["paint"], vb), "opacity": float(r["opacity"]),
                             "bbox": _bbox_of(_scale_splines(r["splines"])), "area": 0.0,
                             "groupPath": [], "role": "fill"})
        silhouette = _scale_splines(L["silhouette"])
        sr = safe_radius([r["splines"] for r in regions])
        bevel = round(min(LAYER_BEVEL, 0.9 * sr), 5)
        layers.append(_defaults_layer(lid, L["name"], [r["elementId"] for r in regions], round(i * LAYER_GAP, 4),
                                      bevel, 0.5))
        # texture: rasterise the standalone layer SVG over the art square, edge-pad RGB
        png = resvg_py.svg_to_bytes(svg_string=L["svg"], width=tex_size, height=tex_size)
        png = _edge_pad_rgba(bytes(png))
        h = hashlib.md5(json.dumps(silhouette).encode() + json.dumps([r["paint"] for r in regions]).encode()).hexdigest()[:12]
        tex = cache / f"{lid}-{h}.png"
        tex.write_bytes(png)
        lsvg = cache / f"{lid}-{h}.svg"
        lsvg.write_text(L["svg"], encoding="utf-8")
        geo_layers[lid] = {
            "layerId": lid, "hash": h, "silhouette": silhouette, "regions": regions, "safeRadius": sr,
            "bbox": _bbox_of(silhouette), "texture": f"/files/fixtures/{out_dir.name}/cache/{tex.name}",
            "texturePath": str(tex.resolve()), "svg": f"/files/fixtures/{out_dir.name}/cache/{lsvg.name}", "images": [],
        }

    bundle_hash = hashlib.md5(json.dumps([g["hash"] for g in geo_layers.values()]).encode()).hexdigest()[:12]
    bundle = {"projectId": pid, "hash": bundle_hash, "viewBox": list(vb), "plate": plate_info, "layers": geo_layers}
    gpath = cache / f"geometry-{bundle_hash}.json"
    gpath.write_text(json.dumps(bundle), encoding="utf-8")

    now = _now()
    project = {
        "version": 1, "id": pid, "name": svg_path.stem, "createdAt": now, "updatedAt": now,
        "source": {"filename": svg_path.name, "viewBox": list(vb), "warnings": res["warnings"][:20],
                   "plateDetected": bool(bg)},
        "strategy": "smart", "elements": elements, "layers": layers, "canvas": canvas,
    }
    (out_dir / "project.json").write_text(json.dumps(project, indent=1), encoding="utf-8")
    return {"project": str((out_dir / "project.json").resolve()), "geometryPath": str(gpath.resolve()),
            "source": "svg_prototype"}


def make(icons: list[str], out: Path, tex_size: int = 2048, prefer_package: bool = True) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    index_path = out / "index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        index = {}
    for name in icons:
        svg = CORPUS / f"{name}.svg"
        if not svg.exists():
            print(f"skip {name}: not found")
            continue
        d = out / name
        d.mkdir(parents=True, exist_ok=True)
        t = time.perf_counter()
        entry = try_package(svg, d) if prefer_package else None
        if entry is None:
            entry = from_prototype(svg, d, tex_size)
        index[name] = entry
        print(f"{name}: {entry['source']} in {time.perf_counter() - t:.2f}s")
    index_path.write_text(json.dumps(index, indent=1), encoding="utf-8")
    return index


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--icons", nargs="*", default=DEFAULT_ICONS)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--tex", type=int, default=2048)
    ap.add_argument("--prototype", action="store_true", help="skip bis.svg even if present")
    a = ap.parse_args(argv)
    make(a.icons, Path(a.out), a.tex, prefer_package=not a.prototype)


if __name__ == "__main__":
    main()
