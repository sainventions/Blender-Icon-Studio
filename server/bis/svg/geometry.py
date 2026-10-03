"""Geometry export: pathops paths -> cubic Bezier splines in art space, occlusion-cut regions,
silhouettes, safe bevel radius, corner-preserving polyline smoothing, layer SVGs + textures, and
the hash-cached :class:`GeometryBundle`."""
from __future__ import annotations

import json
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pathops
import shapely
import shapely.errors
from picosvg.svg_transform import Affine2D
from shapely import affinity

from bis.models import GeometryBundle, Layer, LayerGeometry, Project, Region, Spline, SplinePoint
from .common import PIPELINE_VERSION, atomic_write_bytes, atomic_write_text, sha1
from .elements import ElementStore, Elem, model_paint
from .paths import bounds, is_empty, op, shapely_from_path, union_all
from . import raster, textures

# ----------------------------------------------------------------------------------------------
# tunables
# ----------------------------------------------------------------------------------------------
SMOOTH_POLYLINES = True       # corner-preserving smoothing of dense all-straight contours
SMOOTH_MIN_SEGMENTS = 24      # ... with at least this many segments
SMOOTH_STRAIGHT_FRAC = 0.95   # ... of which at least this fraction are straight lines
SMOOTH_CORNER_DEG = 25.0      # turning angles above this stay sharp corners
SMOOTH_MAX_BULGE = 0.006      # art units (~1.5 px on a 500 px icon): straighten if a curve bulges more
SAFE_RADIUS_AREA_TOL = 0.02   # morphological opening may remove <= 2 % of the area
SAFE_RADIUS_CAP = 0.5         # art units
REGION_Z_STEP = 0.001         # zSub per translucent overlap level
TEXTURE_SIZE = 2048
TEXTURE_BUDGET = ((8, 2048), (24, 1024))  # (max layers, max texture px); more layers -> 512 px
CACHE_DIR = "cache"
CACHE_KEEP_SECONDS = 3600     # unreferenced cache files older than this are pruned


# ----------------------------------------------------------------------------------------------
# splines
# ----------------------------------------------------------------------------------------------
def _lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _contours(sk: pathops.Path) -> List[Tuple[bool, List[tuple]]]:
    """-> [(closed, [(p0, c1, c2, p1, is_line), ...]), ...] (cubic segments, SVG space)."""
    out: List[Tuple[bool, List[tuple]]] = []
    segs: List[tuple] = []
    start = cur = None
    for verb, pts in sk:
        if verb == pathops.PathVerb.MOVE:
            if segs:
                out.append((False, segs))
            segs = []
            start = cur = pts[0]
        elif verb == pathops.PathVerb.LINE:
            p = pts[0]
            segs.append((cur, _lerp(cur, p, 1 / 3), _lerp(cur, p, 2 / 3), p, True))
            cur = p
        elif verb == pathops.PathVerb.QUAD:
            c, p = pts
            segs.append((cur, _lerp(cur, c, 2 / 3), _lerp(p, c, 2 / 3), p, False))
            cur = p
        elif verb == pathops.PathVerb.CUBIC:
            segs.append((cur, pts[0], pts[1], pts[2], False))
            cur = pts[2]
        elif verb == pathops.PathVerb.CONIC:
            raise ValueError("convertConicsToQuads() first")
        elif verb == pathops.PathVerb.CLOSE:
            if cur is not None and start is not None and math.dist(cur, start) > 1e-9:
                segs.append((cur, _lerp(cur, start, 1 / 3), _lerp(cur, start, 2 / 3), start, True))
            if segs:
                out.append((True, segs))
            segs = []
            cur = start
    if segs:
        out.append((False, segs))
    return out


def _drop_degenerate(segs: List[tuple], eps: float) -> List[tuple]:
    return [s for s in segs if not (math.dist(s[0], s[3]) < eps and math.dist(s[0], s[1]) < eps
                                    and math.dist(s[2], s[3]) < eps)]


def smooth_polyline(pts: List[Tuple[float, float]], corner_deg: float = SMOOTH_CORNER_DEG,
                    max_bulge: float = SMOOTH_MAX_BULGE) -> List[dict]:
    """Closed polyline -> G1 cubic spline through every vertex.

    * turning angles above `corner_deg` stay sharp corners;
    * long segments (straight edges between dense curve samples) stay exactly straight and the
      neighbouring curve leaves them tangentially;
    * elsewhere the tangent is the bisector of the adjacent segment directions (Catmull-Rom-like,
      robust to uneven spacing), handle length = |segment| / 3;
    * a segment whose curve would bulge more than `max_bulge` (target units) from its chord is
      straightened (guards against overshoot)."""
    n = len(pts)
    P = np.asarray(pts, dtype=float)
    d_next = np.roll(P, -1, axis=0) - P          # segment i: P[i] -> P[i+1]
    l_seg = np.linalg.norm(d_next, axis=1)
    u_seg = d_next / np.maximum(l_seg, 1e-12)[:, None]
    l_prev, u_prev = np.roll(l_seg, 1), np.roll(u_seg, 1, axis=0)   # segment i-1 ends at P[i]
    med = float(np.median(l_seg)) or 1e-12
    shorter_nb = np.minimum(np.roll(l_seg, 1), np.roll(l_seg, -1))
    straight = (l_seg > 4.0 * med) | ((l_seg > 2.0 * med) & (l_seg > 3.0 * shorter_nb))
    straight_prev = np.roll(straight, 1)
    cosang = np.clip((u_seg * u_prev).sum(axis=1), -1.0, 1.0)
    corner = np.degrees(np.arccos(cosang)) > corner_deg
    t_in = np.empty_like(P)
    t_out = np.empty_like(P)
    for i in range(n):
        if corner[i] or (straight[i] and straight_prev[i]):
            t_in[i], t_out[i] = u_prev[i], u_seg[i]
        elif straight_prev[i]:
            t_in[i] = t_out[i] = u_prev[i]
        elif straight[i]:
            t_in[i] = t_out[i] = u_seg[i]
        else:
            t = u_prev[i] + u_seg[i]
            nt = np.linalg.norm(t)
            t_in[i] = t_out[i] = (t / nt) if nt > 1e-12 else u_seg[i]
    hr = P + t_out * (l_seg / 3.0)[:, None]                    # handle leaving P[i] on segment i
    hl = P - t_in * (l_prev / 3.0)[:, None]                    # handle entering P[i] on segment i-1
    # overshoot guard: bulge of segment i = distance of B(0.5) from its chord
    nxt_hl = np.roll(hl, -1, axis=0)
    mid = (P + 3 * hr + 3 * nxt_hl + np.roll(P, -1, axis=0)) / 8.0
    chord_mid = P + d_next / 2.0
    dm = mid - chord_mid
    bulge = np.abs(u_seg[:, 0] * dm[:, 1] - u_seg[:, 1] * dm[:, 0])
    bad = bulge > np.maximum(max_bulge, 0.0)
    if bad.any():
        idx = np.nonzero(bad)[0]
        hr[idx] = P[idx] + d_next[idx] / 3.0
        nxt = (idx + 1) % n
        hl[nxt] = P[nxt] - d_next[idx] / 3.0
    return [{"co": (P[i, 0], P[i, 1]), "hl": (hl[i, 0], hl[i, 1]), "hr": (hr[i, 0], hr[i, 1])} for i in range(n)]


def path_to_splines(sk: pathops.Path, m: Affine2D, smooth: bool = SMOOTH_POLYLINES,
                    eps: float = 1e-9, nd: int = 6) -> List[dict]:
    """pathops.Path (SVG space) -> [{closed, points:[{co, hl, hr}]}] in the target space of `m`.

    Lines become cubics with handles at 1/3 and 2/3 (exact); quads are degree-elevated exactly.
    Dense all-straight contours (polyline exports, traced rasters) are optionally smoothed."""
    splines = []
    a, b, c, d, e, f = m.a, m.b, m.c, m.d, m.e, m.f

    def X(p):
        return (a * p[0] + c * p[1] + e, b * p[0] + d * p[1] + f)

    def R(p):
        return (round(p[0], nd) + 0.0, round(p[1], nd) + 0.0)

    for closed, segs in _contours(sk):
        segs = _drop_degenerate(segs, eps)
        if not segs:
            continue
        n_lines = sum(1 for s in segs if s[4])
        if (smooth and closed and len(segs) >= SMOOTH_MIN_SEGMENTS
                and n_lines >= SMOOTH_STRAIGHT_FRAC * len(segs)):
            pts = smooth_polyline([X(s[0]) for s in segs])
            splines.append({"closed": True, "smoothed": True,
                            "points": [{k: R(v) for k, v in p.items()} for p in pts]})
            continue
        pts = []
        if closed:
            for k, (p0, c1, _c2, _p1, _l) in enumerate(segs):
                pts.append({"co": p0, "hl": segs[k - 1][2], "hr": c1})
        else:
            for k, (p0, c1, _c2, _p1, _l) in enumerate(segs):
                pts.append({"co": p0, "hl": segs[k - 1][2] if k else p0, "hr": c1})
            pts.append({"co": segs[-1][3], "hl": segs[-1][2], "hr": segs[-1][3]})
        splines.append({"closed": closed,
                        "points": [{"co": R(X(p["co"])), "hl": R(X(p["hl"])), "hr": R(X(p["hr"]))} for p in pts]})
    return splines


def _bez(a, b, t):
    mt = 1 - t
    return tuple(mt ** 3 * a["co"][k] + 3 * mt * mt * t * a["hr"][k] + 3 * mt * t * t * b["hl"][k] + t ** 3 * b["co"][k]
                 for k in range(2))


def _spline_path(s: dict) -> pathops.Path:
    """One spline as an exact cubic pathops contour (closed)."""
    p = pathops.Path()
    pts = s["points"]
    n = len(pts)
    p.moveTo(*pts[0]["co"])
    for i in range(1, n + 1):
        a, b = pts[i - 1], pts[i % n]
        p.cubicTo(*a["hr"], *b["hl"], *b["co"])
    p.close()
    return p


def annotate_holes(splines: List[dict]) -> None:
    """depth / hole / parent for each spline (three.js Shape + holes). Contours never cross after
    simplify, so a point on the curve itself (segment midpoint) is an unambiguous probe. The
    containment test runs on the exact cubic contours (skia winding), not on a sampled polygon -
    sampling misclassifies the hole of a thin ring (outline circles a few px wide)."""
    paths = [_spline_path(s) if len(s["points"]) >= 2 else pathops.Path() for s in splines]
    areas = [abs(p.area) if len(s["points"]) >= 2 else 0.0 for p, s in zip(paths, splines)]
    bbs = [p.bounds if a > 0 else None for p, a in zip(paths, areas)]
    for i, s in enumerate(splines):
        pts = s["points"]
        probe = _bez(pts[0], pts[1 % len(pts)], 0.5) if len(pts) > 1 else tuple(pts[0]["co"])
        containers = []
        for j, pj in enumerate(paths):
            b = bbs[j]
            if (j == i or b is None or areas[j] <= areas[i] or not (b[0] <= probe[0] <= b[2])
                    or not (b[1] <= probe[1] <= b[3])):
                continue
            if pj.contains(probe):
                containers.append(j)
        s["depth"] = len(containers)
        s["hole"] = len(containers) % 2 == 1
        s["parent"] = min(containers, key=lambda j: areas[j]) if (containers and s["hole"]) else -1


def to_model_splines(splines: List[dict]) -> List[Spline]:
    return [Spline(closed=s["closed"], hole=s.get("hole", False), parent=s.get("parent", -1),
                   depth=s.get("depth", 0),
                   points=[SplinePoint(co=p["co"], hl=p["hl"], hr=p["hr"]) for p in s["points"]])
            for s in splines]


def splines_to_d(splines: Sequence) -> str:
    """Inverse of path_to_splines (validation, three.js SVGLoader). Accepts dicts or models."""
    from .common import fmt
    out = []
    for s in splines:
        s = s if isinstance(s, dict) else s.model_dump()
        pts = s["points"]
        if not pts:
            continue
        out.append(f"M{fmt(pts[0]['co'][0], 6)} {fmt(pts[0]['co'][1], 6)}")
        seq = list(range(1, len(pts))) + ([0] if s["closed"] else [])
        prev = pts[0]
        for k in seq:
            p = pts[k]
            out.append("C" + " ".join(fmt(v, 6) for v in (*prev["hr"], *p["hl"], *p["co"])))
            prev = p
        if s["closed"]:
            out.append("Z")
    return " ".join(out)


# ----------------------------------------------------------------------------------------------
# safe radius
# ----------------------------------------------------------------------------------------------
def safe_radius(geoms: Sequence, area_tol: float = SAFE_RADIUS_AREA_TOL, cap: float = SAFE_RADIUS_CAP) -> float:
    """Largest r such that a morphological opening with radius r removes at most `area_tol` of the
    total area as *thin features* - lost pieces longer than 2.5 r (bars, hairlines, spikes).
    Plain convex-corner rounding (pieces ~r in size) is not a thin feature: a round bevel simply
    rounds such corners. Geometries in art units; binary search on vectorised shapely buffers."""
    arr = np.array([g for g in geoms if g is not None and not g.is_empty and g.area > 0], dtype=object)
    if len(arr) == 0:
        return 0.0
    arr = shapely.make_valid(shapely.simplify(arr, 5e-4))  # 0.025 % of the icon: plenty, much faster
    arr = arr[~shapely.is_empty(arr)]
    if len(arr) == 0:
        return 0.0
    areas = shapely.area(arr)
    total = float(areas.sum())
    hi = float(min(cap, max(math.sqrt(a / math.pi) for a in areas)))

    def loss(r: float) -> float:
        opened = shapely.buffer(shapely.buffer(arr, -r, quad_segs=6), r, quad_segs=6)
        try:
            lost = shapely.difference(arr, opened)
        except shapely.errors.GEOSException:
            try:
                lost = shapely.difference(arr, opened, grid_size=max(r * 1e-3, 1e-9))
            except shapely.errors.GEOSException:  # last resort: plain area difference
                return float(np.clip(areas - shapely.area(opened), 0.0, None).sum()) / total
        parts = shapely.get_parts(lost)
        if len(parts) == 0:
            return 0.0
        b = shapely.bounds(parts)
        extent = np.maximum(b[:, 2] - b[:, 0], b[:, 3] - b[:, 1])
        return float(shapely.area(parts[extent > 2.5 * r]).sum()) / total

    if loss(hi) <= area_tol:
        return round(hi, 5)
    lo = 0.0
    for _ in range(14):
        mid = (lo + hi) / 2
        if loss(mid) <= area_tol:
            lo = mid
        else:
            hi = mid
    return round(lo, 5)


# ----------------------------------------------------------------------------------------------
# per-layer geometry
# ----------------------------------------------------------------------------------------------
def members_of(store: ElementStore, element_ids: Sequence[str]) -> List[Elem]:
    idx = store.index
    return sorted((store.elems[idx[i]] for i in element_ids if i in idx), key=lambda e: idx[e.id])


def _bbox_overlap(a, b) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def occlusion_regions(members: Sequence[Elem]) -> List[Tuple[Elem, pathops.Path]]:
    """Each member minus every opaque member above it (same opacity group): disjoint pieces."""
    out = []
    for k, m in enumerate(members):
        region = m.path
        for up in members[k + 1:]:
            if up.opaque_in_group and up.opacity_group == m.opacity_group and _bbox_overlap(m.bbox, up.bbox):
                region = op(region, up.path, pathops.PathOp.DIFFERENCE)
                if is_empty(region):
                    break
        if not is_empty(region):
            out.append((m, region))  # may be m.path itself: treat as read-only
    return out


def _region_levels(regions: List[Tuple[Elem, pathops.Path]], eps_area: float) -> List[int]:
    levels: List[int] = []
    bbs = [bounds(p) for _, p in regions]
    for k, (_m, p) in enumerate(regions):
        lvl = 0
        for j in range(k):
            if levels[j] + 1 <= lvl or not _bbox_overlap(bbs[k], bbs[j]):
                continue
            inter = op(p, regions[j][1], pathops.PathOp.INTERSECTION)
            if not is_empty(inter) and abs(inter.area) > eps_area:
                lvl = levels[j] + 1
        levels.append(lvl)
    return levels


def layer_safe_radius(store: ElementStore, element_ids: Sequence[str], mode: str = "individual") -> float:
    members = members_of(store, element_ids)
    if not members:
        return 0.0
    art = store.art
    tol = store.tolerance
    if mode == "combined":
        paths = [union_all([m.path for m in members])]
    else:
        paths = [p for _m, p in occlusion_regions(members)]
    return safe_radius([_art_scale_geom(shapely_from_path(p, tol), art.k) for p in paths])


def _art_scale_geom(g, k: float):
    """Scale an SVG-space geometry to art units (only lengths/areas matter for the safe radius)."""
    return affinity.scale(g, k, k, origin=(0, 0))


def layer_hash(store: ElementStore, layer: Layer, texture_size: int) -> str:
    """Content hash of one layer's geometry: member element contents (not the whole store, so an
    edit elsewhere never invalidates this layer's texture), mode and pipeline tunables."""
    members = members_of(store, layer.elementIds)
    grads = sorted({m.paint.get("id") for m in members if m.paint.get("id")})
    return sha1(PIPELINE_VERSION, list(store.view_box), [m.content_hash for m in members],
                [store.gradients.get(g, "") for g in grads], layer.mode, SMOOTH_POLYLINES,
                SMOOTH_MIN_SEGMENTS, SMOOTH_STRAIGHT_FRAC, SMOOTH_CORNER_DEG, SMOOTH_MAX_BULGE, SAFE_RADIUS_AREA_TOL,
                texture_size)[:20]


def _build_layer_entry(store: ElementStore, layer: Layer, h: str, cache: Path, project_dir: Path,
                       texture_size: int) -> dict:
    """Compute + write one layer's geometry, SVG and texture. Returns the cache entry dict."""
    art = store.art
    M = art.matrix
    members = members_of(store, layer.elementIds)
    regions = occlusion_regions(members)
    eps_area = art.area * 1e-6
    levels = _region_levels(regions, eps_area)
    region_out = []
    for (m, p), lvl in zip(regions, levels):
        spl = path_to_splines(p, M)
        annotate_holes(spl)
        region_out.append({"elementId": m.id, "paint": model_paint(m.paint, art).model_dump(),
                           "opacity": round(m.total_opacity, 6), "zSub": round(lvl * REGION_Z_STEP, 6),
                           "splines": spl})
    sil = union_all([m.path for m in members])
    sil.convertConicsToQuads(0.001)
    sil_spl = path_to_splines(sil, M)
    annotate_holes(sil_spl)
    tol = store.tolerance
    if layer.mode == "combined":
        sr_paths = [sil]
    else:
        sr_paths = [p for _m, p in regions]
    sr = safe_radius([_art_scale_geom(shapely_from_path(p, tol), art.k) for p in sr_paths])
    bb = art.bbox(bounds(sil)) if not is_empty(sil) else (0.0, 0.0, 0.0, 0.0)

    # standalone layer SVG (source viewBox) + texture (art square)
    svg_name = f"layer-{h}.svg"
    tex_name = f"tex-{h}.png"
    svg_text = textures.layer_svg(members, store.gradients, store.view_box, project_dir)
    atomic_write_text(cache / svg_name, svg_text)
    sq = art.square_view_box()
    tex_svg = textures.layer_svg(members, store.gradients, sq, project_dir, size=(texture_size, texture_size))
    tex = textures.render_texture(tex_svg, texture_size)
    atomic_write_bytes(cache / tex_name, raster.encode_png(tex, 3))

    images = []
    for m in members:
        if m.image and m.image.get("file"):
            mat = Affine2D(*m.image["matrix"])
            to_art = Affine2D.compose_ltr((mat, M))
            images.append({"elementId": m.id, "file": m.image["file"], "bbox": [round(v, 6) for v in art.bbox(m.bbox)],
                           "opacity": round(m.total_opacity, 6), "width": m.image["width"],
                           "height": m.image["height"],
                           "matrix": [round(v, 9) for v in (to_art.a, to_art.b, to_art.c, to_art.d, to_art.e, to_art.f)],
                           "opaque": bool(m.image.get("opaque"))})
    entry = {"hash": h, "silhouette": sil_spl, "regions": region_out, "safeRadius": sr,
             "bbox": [round(v, 6) for v in bb], "svg": svg_name, "texture": tex_name, "images": images}
    atomic_write_text(cache / f"lg-{h}.json", json.dumps(entry, separators=(",", ":")))
    return entry


def _layer_geometry(entry: dict, layer_id: str, cache: Path, project_dir: Path, url_prefix: str) -> LayerGeometry:
    prefix = url_prefix.rstrip("/")
    images = []
    for im in entry["images"]:
        images.append({**im, "path": str((Path(project_dir) / im["file"]).resolve()),
                       "url": f"{prefix}/{im['file']}"})
    return LayerGeometry(
        layerId=layer_id, hash=entry["hash"],
        silhouette=to_model_splines(entry["silhouette"]),
        regions=[Region(elementId=r["elementId"], paint=r["paint"], opacity=r["opacity"], zSub=r["zSub"],
                        splines=to_model_splines(r["splines"])) for r in entry["regions"]],
        safeRadius=entry["safeRadius"], bbox=tuple(entry["bbox"]),
        texture=f"{prefix}/{CACHE_DIR}/{entry['texture']}",
        texturePath=str((cache / entry["texture"]).resolve()),
        svg=f"{prefix}/{CACHE_DIR}/{entry['svg']}",
        images=images,
    )


# ----------------------------------------------------------------------------------------------
# bundle (cached)
# ----------------------------------------------------------------------------------------------
_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_BUNDLES: Dict[Tuple[str, str], GeometryBundle] = {}
_BUNDLE_CAP = 32


def _project_lock(project_dir: Path) -> threading.Lock:
    key = str(Path(project_dir).resolve())
    with _LOCKS_GUARD:
        if key not in _LOCKS:
            _LOCKS[key] = threading.Lock()
        return _LOCKS[key]


def plate_element_ids(store: ElementStore, project: Project) -> List[str]:
    in_layers = {eid for L in project.layers for eid in L.elementIds}
    return [i for i in store.plate_ids if i not in in_layers and i in store.index]


def bundle_hash(store: ElementStore, project: Project, url_prefix: str, texture_size: int) -> Tuple[str, Dict[str, str]]:
    lh = {L.id: layer_hash(store, L, texture_size) for L in project.layers}
    h = sha1(PIPELINE_VERSION, project.id, url_prefix, store.hash, [(L.id, lh[L.id]) for L in project.layers],
             plate_element_ids(store, project))[:20]
    return h, lh


def geometry_file(project_dir: Path, h: str) -> Path:
    return Path(project_dir) / CACHE_DIR / f"geometry-{h}.json"


def texture_size_for(n_layers: int, requested: int = TEXTURE_SIZE) -> int:
    """Per-layer texture size under a total budget: every layer texture covers the whole art
    square and is loaded by Blender and three.js, so an 'element' split with dozens of layers
    must not mean dozens of 2048 px RGBA textures (~21 MB of VRAM each incl. mipmaps)."""
    for max_layers, px in TEXTURE_BUDGET:
        if n_layers <= max_layers:
            return min(requested, px)
    return min(requested, 512)


def build_bundle(project_dir: Path, project: Project, url_prefix: str,
                 texture_size: int = TEXTURE_SIZE) -> GeometryBundle:
    project_dir = Path(project_dir)
    store = ElementStore.load(project_dir)
    texture_size = texture_size_for(len(project.layers), texture_size)
    h, lh = bundle_hash(store, project, url_prefix, texture_size)
    key = (str(project_dir.resolve()), h)
    gfile = geometry_file(project_dir, h)
    hit = _BUNDLES.get(key)
    if hit is not None and _keep_alive(gfile, hit):
        return hit.model_copy()
    if gfile.exists():
        try:
            bundle = GeometryBundle.model_validate_json(gfile.read_bytes())
            _remember(key, bundle)
            _keep_alive(gfile, bundle)
            return bundle.model_copy()
        except Exception:  # noqa: BLE001 - corrupt cache file: rebuild
            pass
    with _project_lock(project_dir):
        cache = project_dir / CACHE_DIR
        cache.mkdir(parents=True, exist_ok=True)
        entries: Dict[str, dict] = {}
        todo = []
        for L in project.layers:
            f = cache / f"lg-{lh[L.id]}.json"
            if f.exists() and (cache / f"tex-{lh[L.id]}.png").exists():
                try:
                    entries[L.id] = json.loads(f.read_text(encoding="utf-8"))
                    continue
                except Exception:  # noqa: BLE001
                    pass
            todo.append(L)
        if todo:
            workers = min(len(todo), max(1, (os.cpu_count() or 4) // 2), 6)
            if workers > 1:
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futs = {L.id: ex.submit(_build_layer_entry, store, L, lh[L.id], cache, project_dir, texture_size)
                            for L in todo}
                    for lid, fut in futs.items():
                        entries[lid] = fut.result()
            else:
                for L in todo:
                    entries[L.id] = _build_layer_entry(store, L, lh[L.id], cache, project_dir, texture_size)
        layers = {L.id: _layer_geometry(entries[L.id], L.id, cache, project_dir, url_prefix) for L in project.layers}
        plate = _plate_dict(store, project, cache, project_dir, url_prefix)
        bundle = GeometryBundle(projectId=project.id, hash=h, viewBox=tuple(store.view_box), plate=plate,
                                layers=layers)
        atomic_write_text(gfile, bundle.model_dump_json())
        _remember(key, bundle)
        _prune(cache, bundle, plate)
    return bundle.model_copy()


def _keep_alive(gfile: Path, bundle: GeometryBundle) -> bool:
    """True if the bundle file exists. Bundles in use get their files' mtimes refreshed (at most
    every 10 min) so cache pruning never deletes textures a recent render may still read."""
    try:
        age = time.time() - gfile.stat().st_mtime
    except OSError:
        return False
    if age > 600:
        cache = gfile.parent
        names = [gfile.name] + [f"{p}-{lg.hash}.{ext}" for lg in bundle.layers.values()
                                for p, ext in (("lg", "json"), ("tex", "png"), ("layer", "svg"))]
        for n in names:
            try:
                os.utime(cache / n)
            except OSError:
                pass
    return True


def _remember(key, bundle: GeometryBundle) -> None:
    with _LOCKS_GUARD:  # request threads share this LRU
        _BUNDLES.pop(key, None)
        _BUNDLES[key] = bundle
        while len(_BUNDLES) > _BUNDLE_CAP:
            _BUNDLES.pop(next(iter(_BUNDLES)))


def _plate_dict(store: ElementStore, project: Project, cache: Path, project_dir: Path,
                url_prefix: str) -> Optional[dict]:
    if not store.plate:
        return None
    ids = plate_element_ids(store, project)
    if not ids:
        return None
    members = members_of(store, ids)
    name = f"plate-{sha1(store.hash, ids)[:16]}.svg"
    if not (cache / name).exists():
        atomic_write_text(cache / name, textures.layer_svg(members, store.gradients, store.view_box, project_dir))
    p = store.plate
    return {"bbox": p["bbox"], "shape": p["shape"], "cornerRadius": p.get("cornerRadius"),
            "iou": p.get("iou"), "elementIds": ids, "svg": f"{url_prefix.rstrip('/')}/{CACHE_DIR}/{name}",
            "svgPath": str((cache / name).resolve())}


def _prune(cache: Path, bundle: GeometryBundle, plate: Optional[dict]) -> None:
    keep = {f"geometry-{bundle.hash}.json"}
    for lg in bundle.layers.values():
        keep |= {f"lg-{lg.hash}.json", f"tex-{lg.hash}.png", f"layer-{lg.hash}.svg"}
    if plate:
        keep.add(Path(plate["svgPath"]).name)
    now = time.time()
    try:
        for f in cache.iterdir():
            if f.name in keep or not f.name.split("-")[0] in ("geometry", "lg", "tex", "layer", "plate"):
                continue
            try:
                if now - f.stat().st_mtime > CACHE_KEEP_SECONDS:
                    f.unlink()
            except OSError:
                pass
    except OSError:
        pass
