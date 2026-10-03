"""Internal element records and the per-project element store (``elements.json``).

The store holds everything split / merge / geometry need, so the SVG is parsed exactly once at
import: simplified path data (SVG units, clipped to the viewBox), paint (incl. the gradient
definitions used to write layer SVGs), opacity / opacity groups, provenance metadata, shadows,
raster image placement, the detected plate and the pairwise overlap/gap analysis."""
from __future__ import annotations

import json
import math
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pathops

from bis.models import (DropShadow, Element, FillLinear, FillRadial, FillSolid, GradientStop)
from .colors import color_name
from .common import PIPELINE_VERSION, ArtSpace, atomic_write_text, compose, is_similarity, rnd, sha1
from .paths import skia_from_d, shapely_from_path
from picosvg.svg_transform import Affine2D

STORE_FILE = "elements.json"
STORE_FORMAT = "bis-elements"


@dataclass
class Elem:
    id: str                              # public id: e0, e1, ... / img0, img1, ...
    uid: str                             # prepass provenance uid (e12, e12s, i1, e3#i0, ...)
    d: str                               # simplified, viewBox-clipped path data (SVG units)
    paint: dict                          # internal paint record (see normalize._paint_record)
    opacity: float = 1.0                 # element opacity incl. fill-opacity (excl. group opacity)
    group_opacity: float = 1.0           # product of retained <g opacity> ancestors
    opacity_group: Optional[str] = None  # id of the retained opacity group (composites together)
    meta: dict = field(default_factory=dict)
    bbox: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # SVG units
    area: float = 0.0                    # SVG units²
    name: str = ""
    shadow: Optional[dict] = None        # SVG root units: {dx, dy, blur, opacity, color}
    image: Optional[dict] = None         # raster: {file, mime, width, height, matrix, clipD, opaque, meanAlpha}
    synthetic: Optional[str] = None      # 'image-background' for a plate matted out of a raster
    _path: Any = field(default=None, repr=False, compare=False)
    _geom: Any = field(default=None, repr=False, compare=False)
    _hash: Optional[str] = field(default=None, repr=False, compare=False)

    # ---------------------------------------------------------------- derived
    @property
    def content_hash(self) -> str:
        if self._hash is None:
            self._hash = sha1(self.to_json())[:16]
        return self._hash

    @property
    def kind(self) -> str:
        return "image" if self.image else "path"

    @property
    def role(self) -> str:
        if self.image:
            return "image"
        return "stroke" if self.meta.get("role") == "stroke" else "fill"

    @property
    def base(self) -> str:
        return self.meta.get("base") or self.uid

    @property
    def path(self) -> pathops.Path:
        if self._path is None:
            self._path = skia_from_d(self.d)
        return self._path

    def geom(self, tol: float):
        if self._geom is None:
            self._geom = shapely_from_path(self.path, tol)
        return self._geom

    @property
    def opaque(self) -> bool:
        return self.opacity >= 0.999 and self.group_opacity >= 0.999 and self.paint.get("opaque", True)

    @property
    def opaque_in_group(self) -> bool:
        return self.opacity >= 0.999 and self.paint.get("opaque", True)

    @property
    def total_opacity(self) -> float:
        return self.opacity * self.group_opacity

    @property
    def paint_key(self) -> str:
        return self.paint["key"] + f"@{round(self.total_opacity, 2)}"

    @property
    def rgb(self) -> Tuple[float, float, float]:
        return tuple(self.paint.get("avg_rgb") or self.paint.get("rgb") or (0.5, 0.5, 0.5))  # type: ignore

    # ---------------------------------------------------------------- (de)serialisation
    def to_json(self) -> dict:
        out = {
            "id": self.id, "uid": self.uid, "name": self.name, "d": self.d, "paint": self.paint,
            "opacity": round(self.opacity, 6), "groupOpacity": round(self.group_opacity, 6),
            "opacityGroup": self.opacity_group, "meta": self.meta,
            "bbox": [round(v, 4) for v in self.bbox], "area": round(self.area, 4),
        }
        if self.shadow:
            out["shadow"] = self.shadow
        if self.image:
            out["image"] = self.image
        if self.synthetic:
            out["synthetic"] = self.synthetic
        return out

    @classmethod
    def from_json(cls, j: dict) -> "Elem":
        return cls(id=j["id"], uid=j.get("uid", j["id"]), d=j["d"], paint=j["paint"],
                   opacity=j.get("opacity", 1.0), group_opacity=j.get("groupOpacity", 1.0),
                   opacity_group=j.get("opacityGroup"), meta=j.get("meta", {}),
                   bbox=tuple(j.get("bbox", (0, 0, 0, 0))), area=j.get("area", 0.0), name=j.get("name", ""),
                   shadow=j.get("shadow"), image=j.get("image"), synthetic=j.get("synthetic"))


# ----------------------------------------------------------------------------------------------
# paint -> models.Paint (ART space)
# ----------------------------------------------------------------------------------------------
def _stops(paint: dict, opacity: float = 1.0) -> List[GradientStop]:
    return [GradientStop(offset=float(s["offset"]), color=s["hex"],
                         opacity=rnd(float(s.get("opacity", 1.0)) * opacity, 6)) for s in paint["stops"]]


def model_paint(paint: dict, art: ArtSpace, opacity: float = 1.0, post: Optional[Affine2D] = None):
    """Internal paint -> models Paint. Gradient geometry is mapped SVG -> art (-> `post`, e.g.
    art -> canvas for the plate). `opacity` is multiplied into the solid / stop opacities."""
    t = paint["type"]
    if t in ("solid", "image"):
        return FillSolid(color=paint["hex"], opacity=rnd(opacity, 6))
    G = Affine2D(*paint["transform"])                     # gradient space -> SVG user space
    M = compose(G, art.matrix, post) if post is not None else compose(G, art.matrix)  # gradient -> target
    c = paint["coords"]
    last = paint["stops"][-1]
    degenerate = abs(M.a * M.d - M.b * M.c) < 1e-18
    if t == "linear":
        degenerate |= math.hypot(c["x2"] - c["x1"], c["y2"] - c["y1"]) < 1e-12
    else:
        degenerate |= c["r"] <= 1e-12
    if degenerate:
        # SVG: a zero-length / zero-radius (or singular) gradient paints its last stop colour
        return FillSolid(color=last["hex"], opacity=rnd(float(last.get("opacity", 1.0)) * opacity, 6))
    if t == "linear":
        # exact for any affine: t(p) = u·p + off in target space; start/end on the gradient axis
        x1, y1, x2, y2 = c["x1"], c["y1"], c["x2"], c["y2"]
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        Minv = M.inverse()
        # t(p) = ((Minv p) - p1)·d / L2
        ux = (Minv.a * dx + Minv.b * dy) / L2
        uy = (Minv.c * dx + Minv.d * dy) / L2
        off = ((Minv.e - x1) * dx + (Minv.f - y1) * dy) / L2
        u2 = ux * ux + uy * uy or 1e-12
        sx, sy = -off * ux / u2, -off * uy / u2
        ex, ey = sx + ux / u2, sy + uy / u2
        return FillLinear(stops=_stops(paint, opacity), start=(rnd(sx, 6), rnd(sy, 6)),
                          end=(rnd(ex, 6), rnd(ey, 6)))
    cx, cy, r = c["cx"], c["cy"], c["r"]
    fx, fy = c.get("fx", cx), c.get("fy", cy)
    has_focal = abs(fx - cx) + abs(fy - cy) > 1e-9
    if is_similarity(M):
        scale = math.hypot(M.a, M.b)
        ctr = M.map_point((cx, cy))
        foc = M.map_point((fx, fy)) if has_focal else None
        return FillRadial(stops=_stops(paint, opacity), center=(rnd(ctr.x, 6), rnd(ctr.y, 6)),
                          radius=rnd(r * scale, 6),
                          focal=(rnd(foc.x, 6), rnd(foc.y, 6)) if foc is not None else None)
    return FillRadial(stops=_stops(paint, opacity), center=(cx, cy), radius=r,
                      focal=(fx, fy) if has_focal else None,
                      matrix=tuple(rnd(v, 8) for v in (M.a, M.b, M.c, M.d, M.e, M.f)))


def model_shadow(shadow: Optional[dict], art: ArtSpace) -> Optional[DropShadow]:
    if not shadow:
        return None
    return DropShadow(dx=rnd(art.length(shadow["dx"]), 6), dy=rnd(-art.length(shadow["dy"]), 6),
                      blur=rnd(art.length(shadow["blur"]), 6), opacity=rnd(shadow["opacity"], 4),
                      color=shadow["color"])


def group_path(meta: dict) -> List[str]:
    out = []
    for a in meta.get("ancestors", []):
        nm = a.get("name")
        out.append(nm if nm and not a.get("auto_name") else (nm or "group"))
    return out


def to_model(e: Elem, art: ArtSpace) -> Element:
    return Element(
        id=e.id, name=e.name or e.id, kind=e.kind,  # type: ignore[arg-type]
        paint=model_paint(e.paint, art),
        opacity=round(e.total_opacity, 6),
        bbox=tuple(rnd(v, 6) for v in art.bbox(e.bbox)),  # type: ignore[arg-type]
        area=round(art.area_fraction(e.area), 6),
        groupPath=group_path(e.meta),
        origId=e.meta.get("orig_id"),
        shadow=model_shadow(e.shadow, art),
        wasStroke=e.role == "stroke",
        role=e.role,  # type: ignore[arg-type]
    )


def element_name(e: Elem, auto_name) -> str:
    """Human element name: original id if meaningful, else '<Colour> <shape kind>'."""
    oid = e.meta.get("orig_id")
    if e.synthetic == "image-background":
        return "Background (from image)"
    if e.image:
        return f"Image {oid}" if oid and not auto_name(oid) else "Image"
    base = oid if oid and not auto_name(oid) else None
    tag = e.meta.get("tag", "path")
    kind = {"rect": "rectangle", "circle": "circle", "ellipse": "ellipse", "line": "line",
            "polyline": "polyline", "polygon": "polygon"}.get(tag, "shape")
    if e.role == "stroke":
        kind = "stroke"
    col = color_name(e.rgb)
    if e.paint["type"] in ("linear", "radial"):
        col += " gradient"
    name = f"{base} ({kind})" if base and e.role == "stroke" else (base or f"{col} {kind}")
    if "island" in e.meta:
        name += f" · part {e.meta['island'] + 1}"
    return name


# ----------------------------------------------------------------------------------------------
# store
# ----------------------------------------------------------------------------------------------
class ElementStore:
    """Everything the pipeline knows about a project's source art (``elements.json``).

    Elements are never deleted: an islands split inserts the island elements right after the
    element they replace and records ``replaced[parent] = [island ids]``. The parent stays
    resolvable, so an undone split (the web UI restores the previous ``project.json``) still
    finds every element it references. Which elements are *active* is the project's business
    (``project.elements``); without a project, superseded parents count as inactive."""

    def __init__(self, *, filename: str, view_box, elems: List[Elem], gradients: Dict[str, str],
                 plate: Optional[dict], warnings: List[str], edges: List[Tuple[int, int]],
                 gaps: Optional[np.ndarray], tolerance: float, source_sha1: str = "",
                 hash: str = "", inside: Optional[List[Tuple[int, int]]] = None,
                 replaced: Optional[Dict[str, List[str]]] = None):
        self.filename = filename
        self.view_box = tuple(float(v) for v in view_box)
        self.elems = elems
        self.gradients = gradients
        self.plate = plate
        self.warnings = warnings
        self.edges = [tuple(e) for e in edges]
        self.gaps = gaps if gaps is not None else np.zeros((len(elems), len(elems)))
        self.inside = [tuple(e) for e in (inside or [])]
        self.tolerance = tolerance
        self.source_sha1 = source_sha1
        self.replaced: Dict[str, List[str]] = {k: list(v) for k, v in (replaced or {}).items()}
        self.hash = hash or self.compute_hash()
        self._index: Optional[Dict[str, int]] = None

    # ---------------------------------------------------------------- lookups
    @property
    def art(self) -> ArtSpace:
        return ArtSpace(self.view_box)  # type: ignore[arg-type]

    @property
    def index(self) -> Dict[str, int]:
        if self._index is None:
            self._index = {e.id: i for i, e in enumerate(self.elems)}
        return self._index

    def get(self, eid: str) -> Elem:
        return self.elems[self.index[eid]]

    @property
    def plate_ids(self) -> List[str]:
        return list(self.plate["elementIds"]) if self.plate else []

    def resolve(self, ids) -> List[str]:
        """Element ids with superseded (islands-split) elements replaced by their islands."""
        out: List[str] = []
        stack, seen = list(reversed(list(ids))), set()
        while stack:
            i = stack.pop()
            if i in self.replaced and i not in seen:
                seen.add(i)  # (guards a corrupt, cyclic map)
                stack += reversed(self.replaced[i])
            else:
                out.append(i)
        return out

    def overlap_pairs(self) -> set:
        return set(self.edges)

    # ---------------------------------------------------------------- persistence
    def compute_hash(self) -> str:
        return sha1(PIPELINE_VERSION, self.view_box, [e.to_json() for e in self.elems], self.gradients,
                    self.plate, self.replaced)[:20]

    def to_json(self) -> dict:
        n = len(self.elems)
        iu = np.triu_indices(n, 1)
        return {
            "format": STORE_FORMAT, "version": 1, "pipeline": PIPELINE_VERSION, "hash": self.hash,
            "filename": self.filename, "viewBox": list(self.view_box), "sourceSha1": self.source_sha1,
            "tolerance": self.tolerance, "warnings": self.warnings, "plate": self.plate,
            "gradients": self.gradients, "elements": [e.to_json() for e in self.elems],
            "replaced": self.replaced,
            "analysis": {"n": n, "edges": [list(e) for e in self.edges], "inside": [list(e) for e in self.inside],
                         "gaps": [round(float(v), 4) if np.isfinite(v) else -1.0 for v in self.gaps[iu]]},
        }

    @classmethod
    def from_json(cls, j: dict) -> "ElementStore":
        if j.get("format") != STORE_FORMAT:
            raise ValueError("not an elements.json store")
        elems = [Elem.from_json(e) for e in j["elements"]]
        n = len(elems)
        gaps = np.zeros((n, n))
        an = j.get("analysis") or {}
        if an.get("n") == n and n > 1:
            iu = np.triu_indices(n, 1)
            vals = np.asarray(an.get("gaps", []), dtype=float)
            vals[vals < 0] = np.inf
            if len(vals) == len(iu[0]):
                gaps[iu] = vals
                gaps = gaps + gaps.T
        return cls(filename=j.get("filename", ""), view_box=j["viewBox"], elems=elems,
                   gradients=j.get("gradients", {}), plate=j.get("plate"), warnings=j.get("warnings", []),
                   edges=[tuple(e) for e in an.get("edges", [])], gaps=gaps,
                   inside=[tuple(e) for e in an.get("inside", [])],
                   tolerance=j.get("tolerance", 0.25), source_sha1=j.get("sourceSha1", ""),
                   hash=j.get("hash", ""), replaced=j.get("replaced") or {})

    def save(self, project_dir: Path) -> None:
        self.hash = self.compute_hash()
        path = Path(project_dir) / STORE_FILE
        atomic_write_text(path, json.dumps(self.to_json(), separators=(",", ":")))
        _CACHE.put(path, self)

    @classmethod
    def load(cls, project_dir: Path) -> "ElementStore":
        path = Path(project_dir) / STORE_FILE
        hit = _CACHE.get(path)
        if hit is not None:
            return hit
        if not path.exists():
            raise FileNotFoundError(f"{path} not found - import the SVG first")
        store = cls.from_json(json.loads(path.read_text(encoding="utf-8")))
        _CACHE.put(path, store)
        return store


class _StoreCache:
    """Tiny LRU keyed by (path, mtime_ns, size) so repeated API calls never re-read the store."""

    def __init__(self, cap: int = 16):
        self.cap = cap
        self._d: Dict[str, Tuple[Tuple[int, int], ElementStore]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _stamp(path: Path) -> Optional[Tuple[int, int]]:
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def get(self, path: Path) -> Optional[ElementStore]:
        key = str(Path(path).resolve())
        stamp = self._stamp(path)
        with self._lock:
            hit = self._d.get(key)
            if hit and stamp and hit[0] == stamp:
                self._d[key] = self._d.pop(key)  # LRU bump
                return hit[1]
        return None

    def put(self, path: Path, store: ElementStore) -> None:
        key = str(Path(path).resolve())
        stamp = self._stamp(path)
        if stamp is None:
            return
        with self._lock:
            self._d.pop(key, None)
            self._d[key] = (stamp, store)
            while len(self._d) > self.cap:
                self._d.pop(next(iter(self._d)))


_CACHE = _StoreCache()
