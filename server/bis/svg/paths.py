"""pathops / shapely path utilities shared by extraction, splitting and geometry export."""
from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

import pathops
import shapely
import shapely.errors
from picosvg import svg_pathops
from picosvg.svg_types import SVGPath
from shapely.geometry import Polygon

Ring = List[Tuple[float, float]]


def skia_from_d(d: str, fill_rule: str = "nonzero", simplify: bool = True) -> pathops.Path:
    """SVG path data -> pathops.Path (conics as quads, optionally simplified: disjoint contours).
    Every contour of the result is closed (see :func:`close_contours`)."""
    if not d or not d.strip():
        return pathops.Path()
    sk = svg_pathops.skia_path(SVGPath(d=d).as_cmd_seq(), fill_rule)
    sk.convertConicsToQuads(0.001)
    if simplify:
        try:
            sk.simplify(fix_winding=True)
        except pathops.PathOpsError:
            pass
    return close_contours(sk)


def close_contours(sk: pathops.Path) -> pathops.Path:
    """Close every open contour. A fill implicitly closes its subpaths (SVG painting rules), but
    skia's Simplify() returns CONVEX input unchanged - an unclosed dot (``…h0`` without ``Z``) or a
    3-point triangle stays an open contour, which the 3D builder would sweep as an open tube.
    Returns `sk` itself when nothing needed closing."""
    open_ = False
    started = False
    for verb, _pts in sk:
        if verb == pathops.PathVerb.MOVE:
            if started:
                open_ = True
                break
            started = True
        elif verb == pathops.PathVerb.CLOSE:
            started = False
    if not open_ and not started:
        return sk
    out = pathops.Path()
    out.fillType = sk.fillType
    started = False
    for verb, pts in sk:
        if verb == pathops.PathVerb.MOVE:
            if started:
                out.close()
            out.moveTo(*pts[0])
            started = True
        elif verb == pathops.PathVerb.LINE:
            out.lineTo(*pts[0])
        elif verb == pathops.PathVerb.QUAD:
            out.quadTo(*pts[0], *pts[1])
        elif verb == pathops.PathVerb.CUBIC:
            out.cubicTo(*pts[0], *pts[1], *pts[2])
        elif verb == pathops.PathVerb.CONIC:
            out.conicTo(*pts[0], *pts[1], pts[2] if len(pts) > 2 else 1.0)
        elif verb == pathops.PathVerb.CLOSE:
            out.close()
            started = False
    if started:
        out.close()
    return out


def clean_d(sk: pathops.Path, ndigits: int = 4) -> str:
    """pathops.Path -> compact absolute SVG path data (M/L/Q/C/Z only)."""
    if is_empty(sk):
        return ""
    return SVGPath.from_commands(svg_pathops.svg_commands(sk)).round_floats(ndigits).d


def is_empty(sk: pathops.Path) -> bool:
    for _verb, _pts in sk:
        return False
    return True


def bounds(sk: pathops.Path) -> Tuple[float, float, float, float]:
    return tuple(sk.bounds) if not is_empty(sk) else (0.0, 0.0, 0.0, 0.0)  # type: ignore[return-value]


def op(a: pathops.Path, b: pathops.Path, kind: pathops.PathOp) -> pathops.Path:
    try:
        return pathops.op(a, b, kind, fix_winding=True)
    except pathops.PathOpsError:
        # extremely rare numerical failure: retry on a re-simplified copy
        a2, b2 = pathops.Path(a), pathops.Path(b)
        for p in (a2, b2):
            try:
                p.simplify(fix_winding=True)
            except pathops.PathOpsError:
                pass
        return pathops.op(a2, b2, kind, fix_winding=True)


def union_all(paths: Sequence[pathops.Path]) -> pathops.Path:
    """Union of many paths by balanced pairwise ops. (pathops.OpBuilder is NOT used: it was
    observed to drop holes when unioning disjoint shapes that both have holes.)"""
    items = [p for p in paths if not is_empty(p)]
    if not items:
        return pathops.Path()
    if len(items) == 1:
        return pathops.Path(items[0])
    while len(items) > 1:
        nxt = []
        for k in range(0, len(items) - 1, 2):
            nxt.append(op(items[k], items[k + 1], pathops.PathOp.UNION))
        if len(items) % 2:
            nxt.append(items[-1])
        items = nxt
    return items[0]


def rect_path(x0: float, y0: float, x1: float, y1: float) -> pathops.Path:
    p = pathops.Path()
    p.moveTo(x0, y0)
    p.lineTo(x1, y0)
    p.lineTo(x1, y1)
    p.lineTo(x0, y1)
    p.close()
    return p


def transform(sk: pathops.Path, m) -> pathops.Path:
    return sk.transform(m.a, m.b, m.c, m.d, m.e, m.f)


def flatten_contours(sk: pathops.Path, tol: float) -> List[Ring]:
    """Flatten a path into closed polylines (curves subdivided to ~`tol`)."""
    rings: List[Ring] = []
    ring: Ring = []
    cur = start = None
    for verb, pts in sk:
        if verb == pathops.PathVerb.MOVE:
            if len(ring) > 2:
                rings.append(ring)
            start = cur = pts[0]
            ring = [cur]
        elif verb == pathops.PathVerb.LINE:
            cur = pts[0]
            ring.append(cur)
        elif verb in (pathops.PathVerb.QUAD, pathops.PathVerb.CUBIC, pathops.PathVerb.CONIC):
            ctrl = (cur,) + tuple(pts[:3] if verb != pathops.PathVerb.CONIC else pts[:2])
            length = sum(math.dist(ctrl[i], ctrl[i + 1]) for i in range(len(ctrl) - 1))
            n = max(2, min(96, int(math.ceil(math.sqrt(length / max(tol, 1e-12))))))
            for i in range(1, n + 1):
                t = i / n
                mt = 1 - t
                if len(ctrl) == 3:
                    p0, c, p1 = ctrl
                    ring.append((mt * mt * p0[0] + 2 * mt * t * c[0] + t * t * p1[0],
                                 mt * mt * p0[1] + 2 * mt * t * c[1] + t * t * p1[1]))
                else:
                    p0, c1, c2, p1 = ctrl
                    ring.append((mt ** 3 * p0[0] + 3 * mt * mt * t * c1[0] + 3 * mt * t * t * c2[0] + t ** 3 * p1[0],
                                 mt ** 3 * p0[1] + 3 * mt * mt * t * c1[1] + 3 * mt * t * t * c2[1] + t ** 3 * p1[1]))
            cur = ctrl[-1]
        elif verb == pathops.PathVerb.CLOSE:
            if len(ring) > 2:
                rings.append(ring)
            ring = []
            cur = start
    if len(ring) > 2:
        rings.append(ring)
    return rings


def shapely_from_path(sk: pathops.Path, tol: float):
    """Simplified (disjoint-contour) path -> shapely geometry (even-odd composition of rings)."""
    polys = []
    for ring in flatten_contours(sk, tol):
        poly = shapely.make_valid(Polygon(ring))
        if not poly.is_empty and poly.area > 0:
            polys.append(poly)
    if not polys:
        return Polygon()
    if len(polys) == 1:
        return polys[0]
    # disjoint contours: even-odd == nonzero, so fold the rings with XOR
    try:
        geom = polys[0]
        for p in polys[1:]:
            geom = geom.symmetric_difference(p)
    except shapely.errors.GEOSException:
        # numerically nasty input (near-coincident edges): snap to a fine grid and retry
        grid = max(tol * 1e-3, 1e-9)
        geom = shapely.set_precision(polys[0], grid)
        for p in polys[1:]:
            geom = shapely.symmetric_difference(geom, shapely.set_precision(p, grid), grid_size=grid)
    return shapely.make_valid(geom)


def path_from_shapely(geom) -> pathops.Path:
    """shapely (Multi)Polygon -> simplified pathops.Path (exteriors + interiors as contours)."""
    path = pathops.Path()
    path.fillType = pathops.FillType.EVEN_ODD
    for poly in getattr(geom, "geoms", [geom]):
        if poly.is_empty or poly.geom_type != "Polygon":
            continue
        for ring in [poly.exterior, *poly.interiors]:
            pts = list(ring.coords)[:-1]
            if len(pts) < 3:
                continue
            path.moveTo(*pts[0])
            for p in pts[1:]:
                path.lineTo(*p)
            path.close()
    try:
        path.simplify(fix_winding=True)
    except pathops.PathOpsError:
        pass
    return path


def split_contours(sk: pathops.Path) -> List[pathops.Path]:
    """One pathops.Path per contour."""
    out: List[pathops.Path] = []
    cur: Optional[pathops.Path] = None
    for verb, pts in sk:
        if verb == pathops.PathVerb.MOVE:
            cur = pathops.Path()
            out.append(cur)
            cur.moveTo(*pts[0])
        elif cur is None:
            continue
        elif verb == pathops.PathVerb.LINE:
            cur.lineTo(*pts[0])
        elif verb == pathops.PathVerb.QUAD:
            cur.quadTo(*pts[0], *pts[1])
        elif verb == pathops.PathVerb.CUBIC:
            cur.cubicTo(*pts[0], *pts[1], *pts[2])
        elif verb == pathops.PathVerb.CLOSE:
            cur.close()
    return out


def islands(sk: pathops.Path, tol: float) -> List[pathops.Path]:
    """Split a simplified path into islands: an outer contour plus its direct holes (an island
    sitting inside a hole is its own island)."""
    from shapely.geometry import Point

    contours = split_contours(sk)
    if len(contours) < 2:
        return [sk]
    rings = [flatten_contours(c, tol) for c in contours]
    polys = [shapely.make_valid(Polygon(r[0])) if r and len(r[0]) > 2 else Polygon() for r in rings]
    depth, parent = [], []
    for i, r in enumerate(rings):
        if not r:
            depth.append(0)
            parent.append(None)
            continue
        probe = Point(r[0][0])
        cont = [j for j, pj in enumerate(polys) if j != i and not pj.is_empty
                and pj.buffer(tol * 0.01).contains(probe)]
        depth.append(len(cont))
        parent.append(min(cont, key=lambda j: polys[j].area) if cont else None)
    groups = {i: [i] for i in range(len(contours)) if depth[i] % 2 == 0}
    for j in range(len(contours)):
        if depth[j] % 2 == 1 and parent[j] in groups:
            groups[parent[j]].append(j)
    if len(groups) < 2:
        return [sk]
    out = []
    for _root, idxs in sorted(groups.items()):
        p = pathops.Path()
        for j in idxs:
            p.addPath(contours[j])
        try:
            p.simplify(fix_winding=True)
        except pathops.PathOpsError:
            pass
        if not is_empty(p):
            out.append(p)
    return out or [sk]
