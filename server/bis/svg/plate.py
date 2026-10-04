"""Background-plate detection and the canvas it implies (PLAN D9).

The bottom-most element is the icon's plate when it is big (>= 45 % of the viewBox), holds
(>= 90 % of) everything painted above it and looks like a plate (near-square bbox, IoU >= 0.85
with a fitted squircle / rounded rect / circle / square - the parametric canvas plate replaces
it). Its own stroke outline (fill + stroke plates) belongs to it. The plate is not a layer: it becomes ``canvas.plate`` (parametric shape, fill converted
to canvas coordinates) and ``canvas.art`` maps the source plate exactly onto −1..1.

Full-bleed art (no plate element, but the union of all art IS a plate shape - Earth's waves) is
framed the same way: the fitted outline maps onto −1..1, the plate takes the art's rim colour and
every element stays art. With a (detected or full-bleed) plate, extruded art is clipped to the
fitted plate outline (:func:`plate_clip_path`); art flush with the plate edge (within PLATE_SNAP -
a frame drawn on the source plate's own outline, which the fitted shape matches only to ~0.01) is
first grown onto the outline (:func:`plate_snap_band`), so no sliver of plate shows around it."""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

import numpy as np
import pathops
import shapely
import shapely.errors
from picosvg.svg_transform import Affine2D
from shapely.geometry import Polygon, box

from bis.models import ArtTransform, Canvas, FillSolid, FillSystem, Plate
from .colors import to_hex
from .common import ArtSpace, rnd
from .elements import Elem, model_paint

MIN_PLATE_AREA = 0.45        # fraction of the viewBox area
MAX_OUTSIDE = 0.10           # fraction of the art above that may stick out of the plate
MIN_PLATE_IOU = 0.85         # the canvas plate replaces the source shape: it must look like one
PLATE_ONLY_MIN_IOU = 0.90    # a lone element counts as a plate only if it looks like one
MAX_PLATE_ASPECT = 1.06      # the canvas plate is square (-1..1): wide pills / banners are art
NO_PLATE_ART_SCALE = 0.78
FULL_BLEED_MIN_IOU = 0.90    # no plate element, but the art's union IS a plate shape (edge-to-edge art)
FULL_BLEED_MIN_SIZE = 0.80   # ... spanning >= this fraction of the longer viewBox side
PLATE_CLIP_INSET = 0.0005    # art units: extruded art is clipped to the plate outline shrunk by this
                             # (0.1 px: keeps the clip off art lying exactly ON the outline). Round 3
                             # used 0.004 (below ~0.003 the clip ran into the source plate's own outline:
                             # a ragged edge) - which left a plate-coloured hairline around art flush
                             # with the edge (Classroom's / CRD's frames, QA r3 #2). Snapping (PLATE_SNAP)
                             # makes the near-exact outline safe.
PLATE_SNAP = 0.012           # art units: art edges within this of the plate outline are snapped onto it
                             # (the Illustrator template outline strays up to ~0.01 from the fitted shape)
CLIP_WARN_FRACTION = 0.005   # warn when clipping removes more than this share of the foreground art


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


def _best_corner_radius(geom, bbox: Sequence[float], r0: float) -> float:
    """Corner radius of the rounded rect that best matches `geom` (max IoU). The equal-area radius
    `r0` is a good start but too round for 'continuous' (squircle-like) corners, which bulge
    further into the corner than a circular arc of the same area: golden-section search on
    [0.4 r0, 1.2 r0]."""
    x0, y0, x1, y1 = bbox
    rmax = min(x1 - x0, y1 - y0) / 2
    if r0 <= 1e-9 or rmax <= 0:
        return r0
    # the corners decide: compare only the four corner squares (fast, and the IoU signal is not
    # diluted by the identical interior)
    s = min(rmax, 1.4 * r0)
    corners = shapely.union_all([box(x0, y0, x0 + s, y0 + s), box(x1 - s, y0, x1, y0 + s),
                                 box(x0, y1 - s, x0 + s, y1), box(x1 - s, y1 - s, x1, y1)])
    g = geom.intersection(corners)

    def score(r):
        return _iou(g, _rounded(x0, y0, x1, y1, r).intersection(corners))

    lo, hi = 0.4 * r0, min(1.2 * r0, rmax)
    if hi <= lo:
        return r0
    phi = (math.sqrt(5) - 1) / 2
    a, b = hi - phi * (hi - lo), lo + phi * (hi - lo)
    fa, fb = score(a), score(b)
    for _ in range(18):
        if fa >= fb:
            hi, b, fb = b, a, fa
            a = hi - phi * (hi - lo)
            fa = score(a)
        else:
            lo, a, fa = a, b, fb
            b = lo + phi * (hi - lo)
            fb = score(b)
    r = (lo + hi) / 2
    return r if score(r) >= score(r0) else r0


def classify_shape(geom, bbox: Sequence[float]) -> dict:
    """Fit squircle / rounded rect / circle / square to a plate polygon (SVG units).

    Returns {shape, cornerRadius (fraction of plate size, 'rounded' only), iou, scores}."""
    x0, y0, x1, y1 = bbox
    w, h = x1 - x0, y1 - y0
    size = max(w, h)
    area = geom.area
    r = _best_corner_radius(geom, bbox, math.sqrt(max(0.0, w * h - area) / (4 - math.pi)))
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


def detect_full_bleed(elems: Sequence[Elem], view_box, tol: float) -> Optional[dict]:
    """No plate element, but the UNION of all art is a plate shape (edge-to-edge artwork such as
    Earth's waves filling a squircle). -> a plate record source like :func:`detect_plate` with
    ``indices=[]`` (every element stays art), ``fullBleed`` and the plate ``fill`` colour: the
    paint that covers most of the plate's rim, so no default white peeks out between the art and
    the fitted plate edge."""
    all_geoms = [e.geom(tol) for e in elems]
    geoms = [g for g in all_geoms if not g.is_empty]
    if not geoms:
        return None
    union = shapely.make_valid(shapely.union_all(geoms))
    vb_area = view_box[2] * view_box[3]
    if union.area < MIN_PLATE_AREA * vb_area:
        return None
    x0, y0, x1, y1 = union.bounds
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0 or max(w, h) / min(w, h) > MAX_PLATE_ASPECT:
        return None
    if max(w, h) < FULL_BLEED_MIN_SIZE * max(view_box[2], view_box[3]):
        return None
    info = classify_shape(union, (x0, y0, x1, y1))
    if info["iou"] < FULL_BLEED_MIN_IOU:
        return None
    return {"indices": [], "bboxSvg": [round(v, 4) for v in (x0, y0, x1, y1)], "fullBleed": True,
            "fill": _rim_colour(elems, all_geoms, info["shape"], info["cornerRadius"], (x0, y0, x1, y1)),
            **info}


def _rim_colour(elems: Sequence[Elem], geoms: Sequence, shape: str, corner: float, bbox) -> str:
    """Hex colour of the visible paint covering most of the plate's rim band."""
    x0, y0, x1, y1 = bbox
    size = max(x1 - x0, y1 - y0)
    outline = Polygon(_outline_points(shape, corner, ((x0 + x1) / 2, (y0 + y1) / 2), size / 2, 0.0, 128))
    band = outline.difference(outline.buffer(-0.04 * size))
    above = None
    best: Dict[str, float] = {}
    for e, g in reversed(list(zip(elems, geoms))):   # top-most first: visible part = g - (union above)
        if g.is_empty:
            continue
        try:
            vis = g if above is None else g.difference(above)
            a = vis.intersection(band).area
        except shapely.errors.GEOSException:
            a = 0.0
        if a > 0 and e.total_opacity >= 0.5:
            key = e.paint.get("hex") or to_hex(e.rgb)
            best[key] = best.get(key, 0.0) + a
        if e.opaque:
            above = g if above is None else shapely.union(above, g)
    if not best:
        return elems[0].paint.get("hex") or "#ffffff"
    return max(best, key=lambda k: best[k])


def _outline_points(shape: str, corner: float, center, half: float, inset: float, n: int = 96) -> List[tuple]:
    """Polygon of the canvas plate shape (PLAN §2 formulas) centred at `center` with half size
    `half`, shrunk by `inset` (exact for square / rounded / circle, by scaling for the squircle)."""
    cx, cy = center
    h = max(1e-9, half - inset)
    if shape == "circle":
        t = np.linspace(0, 2 * math.pi, n, endpoint=False)
        return list(zip(cx + h * np.cos(t), cy + h * np.sin(t)))
    if shape == "squircle":
        t = np.linspace(0, 2 * math.pi, n, endpoint=False)
        c, s = np.cos(t), np.sin(t)
        return list(zip(cx + h * np.sign(c) * np.abs(c) ** 0.4, cy + h * np.sign(s) * np.abs(s) ** 0.4))
    if shape == "rounded":
        r = max(0.0, min(1.0, corner * 2) * half - inset)
        pts = []
        for ax, ay, a0 in ((1, 1, 0), (-1, 1, 90), (-1, -1, 180), (1, -1, 270)):
            ccx, ccy = cx + ax * (h - r), cy + ay * (h - r)
            for k in range(n // 4 + 1):
                a = math.radians(a0 + 90 * k / (n // 4))
                pts.append((ccx + r * math.cos(a), ccy + r * math.sin(a)))
        return pts
    return [(cx + h, cy + h), (cx - h, cy + h), (cx - h, cy - h), (cx + h, cy - h)]


def _outline_cubics(shape: str, corner: float, center, half: float, inset: float) -> List[tuple]:
    """Closed chain of cubic segments ((p0, c1, c2, p1), ...) of the plate outline (art space)."""
    cx, cy = center
    h = max(1e-9, half - inset)
    k = 0.5522847498

    def line(p, q):
        return (p, (p[0] + (q[0] - p[0]) / 3, p[1] + (q[1] - p[1]) / 3),
                (p[0] + 2 * (q[0] - p[0]) / 3, p[1] + 2 * (q[1] - p[1]) / 3), q)

    def arc(ccx, ccy, r, a0):   # quarter arc a0 -> a0 + 90 (degrees, ccw)
        a, b = math.radians(a0), math.radians(a0 + 90)
        p0 = (ccx + r * math.cos(a), ccy + r * math.sin(a))
        p1 = (ccx + r * math.cos(b), ccy + r * math.sin(b))
        c1 = (p0[0] - k * r * math.sin(a), p0[1] + k * r * math.cos(a))
        c2 = (p1[0] + k * r * math.sin(b), p1[1] - k * r * math.cos(b))
        return (p0, c1, c2, p1)

    if shape == "circle":
        return [arc(cx, cy, h, a0) for a0 in (0, 90, 180, 270)]
    if shape == "squircle":
        pts = np.asarray(_outline_points("squircle", corner, center, half, inset, 96))
        n = len(pts)
        out = []
        for i in range(n):
            p0, p1 = pts[i], pts[(i + 1) % n]
            c1 = p0 + (p1 - pts[i - 1]) / 6.0
            c2 = p1 - (pts[(i + 2) % n] - p0) / 6.0
            out.append((tuple(p0), tuple(c1), tuple(c2), tuple(p1)))
        return out
    r = max(0.0, min(1.0, corner * 2) * half - inset) if shape == "rounded" else 0.0
    out = []
    corners = ((1, 1, 0), (-1, 1, 90), (-1, -1, 180), (1, -1, 270))
    for i, (ax, ay, a0) in enumerate(corners):
        ccx, ccy = cx + ax * (h - r), cy + ay * (h - r)
        if r > 1e-9:
            out.append(arc(ccx, ccy, r, a0))
        nx, ny, na = corners[(i + 1) % 4]
        end = (ccx + r * math.cos(math.radians(a0 + 90)), ccy + r * math.sin(math.radians(a0 + 90)))
        ncx, ncy = cx + nx * (h - r), cy + ny * (h - r)
        start = (ncx + r * math.cos(math.radians(na)), ncy + r * math.sin(math.radians(na)))
        if math.dist(end, start) > 1e-12:
            out.append(line(end, start))
    return out


def plate_clip_path(store, inset: float = PLATE_CLIP_INSET) -> Optional[pathops.Path]:
    """The detected (or full-bleed) plate outline, shrunk by `inset` art units, as an exact cubic
    pathops path in SVG space - extruded art is clipped to it. None without a plate. Memoised on
    the store object."""
    plate = getattr(store, "plate", None)
    if not plate or plate.get("shape") in (None, "none"):
        return None
    memo = getattr(store, "_plate_clips", None)
    if not isinstance(memo, dict):
        memo = {}
        try:
            store._plate_clips = memo
        except AttributeError:
            pass
    key = (tuple(plate.get("bbox") or ()), plate.get("shape"), plate.get("cornerRadius"), round(inset, 9))
    if key in memo:
        return memo[key]
    x0, y0, x1, y1 = plate["bbox"]
    half = max(x1 - x0, y1 - y0) / 2
    segs = _outline_cubics(plate["shape"], float(plate.get("cornerRadius") or 0.225),
                           ((x0 + x1) / 2, (y0 + y1) / 2), half, inset)
    inv = store.art.inverse

    def X(p):
        return (inv.a * p[0] + inv.c * p[1] + inv.e, inv.b * p[0] + inv.d * p[1] + inv.f)

    path = pathops.Path()
    path.moveTo(*X(segs[0][0]))
    for _p0, c1, c2, p1 in segs:
        path.cubicTo(*X(c1), *X(c2), *X(p1))
    path.close()
    try:
        path.simplify(fix_winding=True)
    except pathops.PathOpsError:
        pass
    memo[key] = path
    return path


def plate_snap_band(store) -> Optional[pathops.Path]:
    """The ring between the clip outline (PLATE_CLIP_INSET) and the outline shrunk by PLATE_SNAP
    (SVG space): art reaching into it is grown onto the clip outline. None without a plate."""
    outer = plate_clip_path(store, PLATE_CLIP_INSET)
    inner = plate_clip_path(store, PLATE_SNAP)
    if outer is None or inner is None:
        return None
    memo = getattr(store, "_plate_clips", {})
    key = ("band", id(outer), id(inner))
    if key not in memo:
        try:
            memo[key] = pathops.op(outer, inner, pathops.PathOp.DIFFERENCE, fix_winding=True)
        except pathops.PathOpsError:
            memo[key] = None
    return memo[key]


def clipped_fraction(elems: Sequence[Elem], plate: dict, art: ArtSpace, tol: float) -> float:
    """Share of the foreground art's area that reaches past the plate outline itself (what the 3D
    clip removes - art merely flush with the edge, like Classroom's frame, does not count)."""
    ids = set(plate.get("elementIds") or ())
    geoms = [e.geom(tol) for e in elems if e.id not in ids and not e.geom(tol).is_empty]
    if not geoms:
        return 0.0
    x0, y0, x1, y1 = plate["bbox"]
    half = max(x1 - x0, y1 - y0) / 2
    pts = _outline_points(plate["shape"], float(plate.get("cornerRadius") or 0.225),
                          ((x0 + x1) / 2, (y0 + y1) / 2), half, 0.0, 192)
    inv = art.inverse
    outline = Polygon([(inv.a * x + inv.c * y + inv.e, inv.b * x + inv.d * y + inv.f) for x, y in pts])
    try:
        u = shapely.union_all(geoms)
        return float(u.difference(outline).area / u.area) if u.area > 0 else 0.0
    except shapely.errors.GEOSException:
        return 0.0


def plate_record(det: dict, elems: Sequence[Elem], art: ArtSpace) -> dict:
    """Store record for a detected plate (ids are assigned by then)."""
    rec = {"elementIds": [elems[i].id for i in det["indices"]], "bboxSvg": det["bboxSvg"],
           "bbox": [rnd(v, 6) for v in art.bbox(det["bboxSvg"])], "shape": det["shape"],
           "cornerRadius": det["cornerRadius"], "iou": det["iou"], "scores": det["scores"]}
    if det.get("fullBleed"):
        rec["fullBleed"] = True
        rec["fill"] = det.get("fill")
    return rec


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


PAD_STOP_EPS = 1e-6


def trim_pad_stops(fill):
    """A plate gradient without hard stops AT its ends: a stop that shares offset 1 with the stop before it (or
    offset 0 with the stop after it) paints only the PAD region beyond the gradient's span - Illustrator exports
    such a stray end stop (Twitter's plate: ``... 1 #1a6ed4, 1 #bd4012``). In the flat SVG the plate never
    reaches that region, but the 3D plate's bevel and side walls (and the canvas outside the source plate) do:
    a red sliver along one edge in every iso view. The stop the gradient actually ends on is kept."""
    stops = getattr(fill, "stops", None)
    if not stops or len(stops) < 2:
        return fill
    out = list(stops)
    while len(out) >= 2 and out[-1].offset >= 1.0 - PAD_STOP_EPS and out[-2].offset >= 1.0 - PAD_STOP_EPS:
        out.pop()
    while len(out) >= 2 and out[0].offset <= PAD_STOP_EPS and out[1].offset <= PAD_STOP_EPS:
        out.pop(0)
    if len(out) == len(stops):
        return fill
    return fill.model_copy(update={"stops": out})


def make_canvas(plate: Optional[dict], elems_by_id: Dict[str, Elem], art: ArtSpace) -> Canvas:
    """Canvas for a project: detected plate -> parametric plate with the source fill in CANVAS
    coordinates; no plate -> visible System Light plate with the art scaled to 0.78."""
    at = art_transform_for(plate)
    if not plate:
        return Canvas(shape="squircle", plate=Plate(visible=True, fill=FillSystem(type="system-light")), art=at)
    members = [elems_by_id[i] for i in plate["elementIds"] if i in elems_by_id]
    src = next((m for m in members if m.role == "fill"), members[0] if members else None)
    post = Affine2D(at.scale, 0.0, 0.0, at.scale, at.x, at.y)  # art -> canvas
    if plate.get("fullBleed") and plate.get("fill"):
        fill = FillSolid(color=plate["fill"])   # full-bleed art: the plate only shows at the rim
    elif src is None:
        fill = FillSystem(type="system-light")
    else:
        fill = trim_pad_stops(model_paint(src.paint, art, src.total_opacity, post=post))
    shape = plate["shape"]
    return Canvas(shape=shape, cornerRadius=plate.get("cornerRadius") or 0.225,
                  plate=Plate(visible=True, fill=fill), art=at)
