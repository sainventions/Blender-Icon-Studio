"""picosvg normalisation + element extraction.

After the prepass, picosvg bakes transforms, converts shapes to paths, applies clip paths as
booleans and flattens groups (keeping only meaningful ``<g opacity>``). This module walks the
result, re-attaches provenance metadata by id, simplifies every path with skia-pathops (disjoint
contours: holes purely by nesting), culls/clips everything to the viewBox, and turns image
placeholders back into raster elements whose geometry is the alpha silhouette."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

import pathops
from lxml import etree
from picosvg.svg import SVG
from picosvg.svg_transform import Affine2D

from .colors import color_name, parse_color, to_hex
from .common import local, q
from .elements import Elem
from .paths import bounds, clean_d, is_empty, op, rect_path, skia_from_d
from .prepass import GRADIENT_ATTRS, GRADIENT_TAGS, Options, Prepass
from . import raster


def normalize(pre: Prepass, opts: Optional[Options] = None) -> SVG:
    opts = opts or Options()
    try:
        return SVG.fromstring(pre.svg_text).topicosvg(ndigits=opts.ndigits)
    except ValueError as ex:
        pre.warnings.append(f"normaliser fallback: {str(ex)[:160]}")
        return SVG.fromstring(pre.svg_text).topicosvg(ndigits=opts.ndigits, drop_unsupported=True)


def _gradient_record(grad_el) -> dict:
    tag = local(grad_el.tag)
    g = {k: float(grad_el.get(k)) for k in GRADIENT_ATTRS[tag] if grad_el.get(k) is not None}
    if tag == "radialGradient":
        g.setdefault("cx", 0.5)
        g.setdefault("cy", 0.5)
        g.setdefault("r", 0.5)
        g.setdefault("fx", g["cx"])
        g.setdefault("fy", g["cy"])
    else:
        for k, v in (("x1", 0.0), ("y1", 0.0), ("x2", 1.0), ("y2", 0.0)):
            g.setdefault(k, v)
    tf = Affine2D.fromstring(grad_el.get("gradientTransform")) if grad_el.get("gradientTransform") else Affine2D.identity()
    stops = []
    for s in grad_el.iter(q("stop")):
        col = parse_color(s.get("stop-color", "#000")) or (0, 0, 0, 1)
        stops.append({"offset": float(s.get("offset", 0)), "hex": to_hex(col), "rgb": [round(c, 6) for c in col[:3]],
                      "opacity": float(s.get("stop-opacity", 1))})
    if not stops:
        stops = [{"offset": 0.0, "hex": "#000000", "rgb": [0.0, 0.0, 0.0], "opacity": 1.0}]
    avg = [sum(s["rgb"][i] for s in stops) / len(stops) for i in range(3)]
    key = "grad:" + hashlib.md5(json.dumps([[s["hex"], s["opacity"]] for s in stops]).encode()).hexdigest()[:8]
    return {
        "type": "linear" if tag == "linearGradient" else "radial",
        "coords": g, "transform": [round(v, 9) for v in tf], "spread": grad_el.get("spreadMethod", "pad"),
        "stops": stops, "avg_rgb": [round(v, 6) for v in avg], "hex": to_hex(avg), "key": key,
        "opaque": all(s["opacity"] >= 0.999 for s in stops), "id": grad_el.get("id"),
    }


def _solid_record(col) -> dict:
    rgb = [round(c, 6) for c in col[:3]]
    return {"type": "solid", "hex": to_hex(col), "rgb": rgb, "avg_rgb": rgb, "key": to_hex(col), "opaque": True}


@dataclass
class Extraction:
    elems: List[Elem]
    gradients: Dict[str, str]   # gradient id -> serialized <linearGradient>/<radialGradient>
    tolerance: float            # flattening tolerance (SVG units)


def extract_elements(pico: SVG, pre: Prepass, opts: Optional[Options] = None) -> Extraction:
    opts = opts or Options()
    vb = pre.view_box
    tol = math.hypot(vb[2], vb[3]) * opts.curve_tolerance_pct / 100.0
    grads = {g.get("id"): g for g in pico.svg_root.iter() if local(g.tag) in GRADIENT_TAGS}
    vb_rect = rect_path(vb[0], vb[1], vb[0] + vb[2], vb[1] + vb[3])
    out: List[Elem] = []
    seen: Dict[str, int] = {}
    used_grads: Dict[str, str] = {}
    culled: List[str] = []
    clipped = 0

    def walk(node, gop: float, gid: Optional[str], depth: int):
        nonlocal clipped
        for child in node:
            if not isinstance(child.tag, str):
                continue
            name = local(child.tag)
            if name == "g":
                gop_here = float(child.get("opacity", 1))
                walk(child, gop * gop_here, f"og{len(out)}_{depth}" if gop_here < 1 else gid, depth + 1)
                continue
            if name != "path":
                continue
            uid = child.get("id") or f"anon{len(out)}"
            if uid in seen:
                seen[uid] += 1
                uid = f"{uid}.{seen[uid]}"
            else:
                seen[uid] = 0
            base = uid.split(".")[0]
            meta = pre.meta.get(base, {"uid": base, "role": "fill", "base": base, "ancestors": [],
                                       "index_path": [], "orig_id": None, "tag": "path"})
            img_ref = pre.images.get(base)
            fill = child.get("fill", "#000000")
            m = re.match(r"url\(#(.+)\)", fill)
            if img_ref is not None:
                paint = {"type": "image", "key": f"image:{base}", "opaque": False,
                         "hex": "#808080", "rgb": [0.5, 0.5, 0.5], "avg_rgb": [0.5, 0.5, 0.5]}
            elif m and m.group(1) in grads:
                paint = _gradient_record(grads[m.group(1)])
                used_grads[paint["id"]] = etree.tostring(grads[m.group(1)], encoding="unicode").replace(
                    ' xmlns="http://www.w3.org/2000/svg"', "")
            else:
                paint = _solid_record(parse_color(fill) or (0, 0, 0, 1))
            opacity = float(child.get("opacity", 1)) * float(child.get("fill-opacity", 1))
            sk = skia_from_d(child.get("d", ""), child.get("fill-rule", "nonzero"))
            if is_empty(sk):
                continue
            # viewBox cull / clip (off-canvas junk, bleeding strokes)
            b = bounds(sk)
            inside = (b[0] >= vb[0] - 1e-6 and b[1] >= vb[1] - 1e-6 and
                      b[2] <= vb[0] + vb[2] + 1e-6 and b[3] <= vb[1] + vb[3] + 1e-6)
            if not inside:
                sk = op(sk, vb_rect, pathops.PathOp.INTERSECTION)
                if is_empty(sk) or abs(sk.area) < 1e-9 * vb[2] * vb[3]:
                    desc = "image" if img_ref else f"{meta.get('role', 'fill')} {paint['hex']}"
                    culled.append(f"{meta.get('orig_id') or meta.get('tag', 'path')} ({desc})")
                    continue
                clipped += 1
            elem = Elem(id="", uid=uid, d="", paint=paint, opacity=opacity, group_opacity=gop,
                        opacity_group=gid, meta=meta, shadow=meta.get("shadow"))
            if img_ref is not None:
                if not _attach_image(elem, img_ref, sk, pre, tol):
                    continue
            else:
                elem._path = sk
            elem.d = clean_d(elem._path, opts.ndigits)
            elem._path = skia_from_d(elem.d)  # exactly what the store will reproduce
            if is_empty(elem._path):
                continue
            elem.bbox = bounds(elem._path)
            g = elem.geom(tol)
            elem.area = float(g.area)
            if elem.area <= 0:
                continue
            out.append(elem)

    walk(pico.svg_root, 1.0, None, 0)
    if culled:
        pre.warnings.append(f"removed {len(culled)} element(s) entirely outside the viewBox: "
                            + ", ".join(culled[:4]) + (" …" if len(culled) > 4 else ""))
    if clipped:
        pre.warnings.append(f"clipped {clipped} element(s) that extend beyond the viewBox")
    return Extraction(out, used_grads, tol)


def _attach_image(elem: Elem, ref, placed: pathops.Path, pre: Prepass, tol: float) -> bool:
    """Image placeholder -> raster element. `placed` = visible image area (transform, clip and
    viewBox applied by picosvg / us). Geometry = alpha silhouette ∩ placed area."""
    try:
        info, rgba = raster.analyse_image(ref.data, ref.matrix)
    except Exception as ex:  # noqa: BLE001
        pre.warnings.append(f"embedded image could not be decoded ({ex}) - dropped")
        return False
    sil = op(info.silhouette, placed, pathops.PathOp.INTERSECTION) if not is_empty(info.silhouette) else info.silhouette
    if is_empty(sil):
        pre.warnings.append("embedded image is fully transparent inside its visible area - dropped")
        return False
    elem._path = sil
    elem.paint = {**elem.paint, "hex": to_hex(info.avg_rgb), "rgb": [round(c, 6) for c in info.avg_rgb],
                  "avg_rgb": [round(c, 6) for c in info.avg_rgb], "opaque": info.opaque}
    elem.image = {
        "uid": ref.uid, "mime": ref.mime, "width": ref.width, "height": ref.height,
        "matrix": [round(v, 9) for v in ref.matrix], "clipD": clean_d(placed, 4),
        "opaque": info.opaque, "meanAlpha": round(info.mean_alpha, 4), "alphaThreshold": info.threshold,
        # round 9: soft alpha (glow / shine / shadow) → flat card; crisp silhouette → a real body. Measured over the
        # WHOLE stored image like the worker's art_alpha_is_soft (unrounded: the 0.25 threshold must agree)
        "alphaSoftness": raster.data_alpha_softness(ref.data, rgba),
        "file": None,  # filled in when the store is written (images/<id>.png)
    }
    elem._image_data = ref.data  # type: ignore[attr-defined]
    return True


def matte_opaque_image_plate(ex: Extraction, pre: Prepass) -> None:
    """A fully opaque raster that is the bottom-most, plate-sized element (e.g. an icon exported
    as one clipped PNG) is split into a flat 'background' element (its visible area, filled with
    the image's border colour - this becomes the canvas plate) and the image element restricted
    to the pixels that differ from that background (the foreground art)."""
    if not ex.elems:
        return
    first = ex.elems[0]
    vb = pre.view_box
    if not first.image or not first.image.get("opaque") or first.area < 0.45 * vb[2] * vb[3]:
        return
    placed = skia_from_d(first.image["clipD"])
    data = getattr(first, "_image_data", None)
    if data is None:
        return
    rgba = raster.decode_rgba(data)
    res = raster.matte_background(rgba, first.image["matrix"], placed)
    if res is None:
        return
    bg_rgb, fg = res
    fg = op(fg, placed, pathops.PathOp.INTERSECTION)
    if is_empty(fg):
        return
    plate = Elem(id="", uid=first.uid + "#bg", d=clean_d(placed, 4), paint=_solid_record(bg_rgb),
                 opacity=first.opacity, group_opacity=first.group_opacity, opacity_group=first.opacity_group,
                 meta={**first.meta, "uid": first.uid + "#bg", "role": "fill", "base": first.uid + "#bg",
                       "tag": "path"},
                 synthetic="image-background")
    plate._path = skia_from_d(plate.d)
    plate.bbox = bounds(plate._path)
    plate.area = float(plate.geom(ex.tolerance).area)
    first.d = clean_d(fg, 4)
    first._path = skia_from_d(first.d)
    first._geom = None
    first.bbox = bounds(first._path)
    first.area = float(first.geom(ex.tolerance).area)
    first.image["clipD"] = first.d
    first.image["matted"] = True
    first.image["background"] = to_hex(bg_rgb)
    ex.elems.insert(0, plate)
    pre.warnings.append(f"raster-only icon: background {to_hex(bg_rgb)} separated from the image "
                        f"foreground ({color_name(bg_rgb).lower()} plate)")
