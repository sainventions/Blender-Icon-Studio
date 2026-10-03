"""Background-plate detection and the canvas it implies (PLAN D9).

The bottom-most element is the icon's plate when it is big (>= 45 % of the viewBox), holds
(>= 90 % of) everything painted above it and looks like a plate (near-square bbox, IoU >= 0.85
with a fitted squircle / rounded rect / circle / square - the parametric canvas plate replaces
it). Its own stroke outline (fill + stroke plates) belongs to it. The plate is not a layer: it becomes ``canvas.plate`` (parametric shape, fill converted
to canvas coordinates) and ``canvas.art`` maps the source plate exactly onto −1..1."""
from __future__ import annotations

import math
from typing import Dict, Optional, Sequence

import numpy as np
import shapely
from picosvg.svg_transform import Affine2D
from shapely.geometry import Polygon, box

from bis.models import ArtTransform, Canvas, FillSystem, Plate
from .common import ArtSpace, rnd
from .elements import Elem, model_paint

MIN_PLATE_AREA = 0.45        # fraction of the viewBox area
MAX_OUTSIDE = 0.10           # fraction of the art above that may stick out of the plate
MIN_PLATE_IOU = 0.85         # the canvas plate replaces the source shape: it must look like one
PLATE_ONLY_MIN_IOU = 0.90    # a lone element counts as a plate only if it looks like one
MAX_PLATE_ASPECT = 1.06      # the canvas plate is square (-1..1): wide pills / banners are art
NO_PLATE_ART_SCALE = 0.78


def _superellipse(x0, y0, x1, y1, n: float = 5.0, samples: int = 360) -> Polygon:
    cx, cy, a, b = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
    t = np.linspace(0, 2 * math.pi, samples, endpoint=False)
    c, s = np.cos(t), np.sin(t)
    x = cx + a * np.sign(c) * np.abs(c) ** (2 / n)
    y = cy + b * np.sign(s) * np.abs(s) ** (2 / n)
    return Polygon(np.c_[x, y])


def _ellipse(x0, y0, x1, y1, samples: int = 360) -> Polygon:
    return _superellipse(x0, y0, x1, y1, 2.0, samples)


def _rounded(x0, y0, x1, y1, r: float) -> Polygon:
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    if r <= 1e-9:
        return box(x0, y0, x1, y1)
    return box(x0 + r, y0 + r, x1 - r, y1 - r).buffer(r, quad_segs=24)


def _iou(a, b) -> float:
    u = a.union(b).area
    return float(a.intersection(b).area / u) if u > 0 else 0.0


def classify_shape(geom, bbox: Sequence[float]) -> dict:
    """Fit squircle / rounded rect / circle / square to a plate polygon (SVG units).

    Returns {shape, cornerRadius (fraction of plate size, 'rounded' only), iou, scores}."""
    x0, y0, x1, y1 = bbox
    w, h = x1 - x0, y1 - y0
    size = max(w, h)
    area = geom.area
    r = math.sqrt(max(0.0, w * h - area) / (4 - math.pi))
    cands = {
        "squircle": _superellipse(x0, y0, x1, y1),
        "rounded": _rounded(x0, y0, x1, y1, r),
        "circle": _ellipse(x0, y0, x1, y1),
        "square": box(x0, y0, x1, y1),
    }
    scores = {k: round(_iou(geom, v), 5) for k, v in cands.items()}
    if abs(w - h) > 0.04 * size:
        scores["circle"] = min(scores["circle"], 0.0)  # an ellipse is not a circle plate
    best = max(scores.values())
    # preference order on near-ties: squircle > rounded > circle > square
    shape = next(k for k in ("squircle", "rounded", "circle", "square") if scores[k] >= best - 0.002)
    out = {"shape": shape, "iou": scores[shape], "scores": scores, "cornerRadius": 0.225}
    if shape == "rounded":
        out["cornerRadius"] = round(min(0.5, r / size), 5) if size > 0 else 0.225  # 0.5 = full round
    return out


def detect_plate(elems: Sequence[Elem], view_box, tol: float) -> Optional[dict]:
    """-> {'indices': [...], 'bboxSvg', 'shape', 'cornerRadius', 'iou', 'scores'} or None."""
    if not elems:
        return None
    vb_area = view_box[2] * view_box[3]
    e0 = elems[0]
    if e0.area < MIN_PLATE_AREA * vb_area or e0.image:
        return None  # a raster can't be a parametric plate (opaque ones are matted beforehand)
    idxs = [0]
    # the plate's own stroke outline (fill + stroke plate) belongs to the plate
    for j in range(1, len(elems)):
        if elems[j].base == e0.base and elems[j].role == "stroke":
            idxs.append(j)
    plate_geom = shapely.union_all([elems[i].geom(tol) for i in idxs])
    rest = [elems[j].geom(tol) for j in range(len(elems)) if j not in idxs and not elems[j].geom(tol).is_empty]
    if rest:
        rest_u = shapely.union_all(rest)
        if rest_u.area > 0 and rest_u.difference(plate_geom).area > MAX_OUTSIDE * rest_u.area:
            return None
    x0, y0, x1, y1 = plate_geom.bounds
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0 or max(w, h) / min(w, h) > MAX_PLATE_ASPECT:
        return None
    info = classify_shape(plate_geom, (x0, y0, x1, y1))
    if info["iou"] < (MIN_PLATE_IOU if rest else PLATE_ONLY_MIN_IOU):
        return None  # e.g. a big triangle / star / heart under a highlight: keep it as art
    return {"indices": idxs, "bboxSvg": [round(v, 4) for v in (x0, y0, x1, y1)], **info}


def plate_record(det: dict, elems: Sequence[Elem], art: ArtSpace) -> dict:
    """Store record for a detected plate (ids are assigned by then)."""
    return {"elementIds": [elems[i].id for i in det["indices"]], "bboxSvg": det["bboxSvg"],
            "bbox": [rnd(v, 6) for v in art.bbox(det["bboxSvg"])], "shape": det["shape"],
            "cornerRadius": det["cornerRadius"], "iou": det["iou"], "scores": det["scores"]}


def art_transform_for(plate: Optional[dict]) -> ArtTransform:
    if not plate:
        return ArtTransform(scale=NO_PLATE_ART_SCALE, x=0.0, y=0.0)
    x0, y0, x1, y1 = plate["bbox"]
    size = max(x1 - x0, y1 - y0)
    if size <= 0:
        return ArtTransform(scale=NO_PLATE_ART_SCALE, x=0.0, y=0.0)
    s = 2.0 / size
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return ArtTransform(scale=rnd(s, 6), x=rnd(-cx * s, 6), y=rnd(-cy * s, 6))


def make_canvas(plate: Optional[dict], elems_by_id: Dict[str, Elem], art: ArtSpace) -> Canvas:
    """Canvas for a project: detected plate -> parametric plate with the source fill in CANVAS
    coordinates; no plate -> visible System Light plate with the art scaled to 0.78."""
    at = art_transform_for(plate)
    if not plate:
        return Canvas(shape="squircle", plate=Plate(visible=True, fill=FillSystem(type="system-light")), art=at)
    members = [elems_by_id[i] for i in plate["elementIds"] if i in elems_by_id]
    src = next((m for m in members if m.role == "fill"), members[0] if members else None)
    post = Affine2D(at.scale, 0.0, 0.0, at.scale, at.x, at.y)  # art -> canvas
    if src is None:
        fill = FillSystem(type="system-light")
    else:
        fill = model_paint(src.paint, art, src.total_opacity, post=post)
    shape = plate["shape"]
    return Canvas(shape=shape, cornerRadius=plate.get("cornerRadius") or 0.225,
                  plate=Plate(visible=True, fill=fill), art=at)
