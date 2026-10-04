"""Blender Icon Studio - SVG pipeline (workstream A).

    SVG bytes
      -> prepass        CSS, <use>, colours, gradients, strokes -> fills, filters -> shadows,
                        <image> -> placeholders, provenance ids              (prepass.py)
      -> picosvg        transforms, shapes -> paths, clip booleans, opacity groups (normalize.py)
      -> elements       simplified paths clipped to the viewBox, raster silhouettes, store
      -> plate          detected background -> Canvas (D9)                     (plate.py)
      -> split          z-order-consistent layer split; tiles of one shape share a layer
                        built as one 'combined' body                (split.py, ops.py, tiling.py)
      -> geometry       splines (art space; flush art snapped onto the plate outline), regions,
                        silhouettes, safe radius, max inscribed radius (real-height stacking), layer SVGs and
                        edge-padded 2048 px textures, hash-cached GeometryBundle (geometry.py)

Public API (PLAN.md §4 A): :func:`import_svg`, :func:`split_layers`, :func:`merge_layers`,
:func:`split_layer`, :func:`move_elements`, :func:`build_geometry`, :func:`geometry_path`,
:func:`thumbnail_png`, :func:`layer_thumbnail_png` (+ :func:`layer_auto_modes`). Invalid edits raise ``ValueError``
(:class:`ZOrderError` for stacking-order violations)."""
from __future__ import annotations

import gzip
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Literal, Optional

from bis.models import Canvas, Element, GeometryBundle, Layer, Project, SourceInfo, SplitStrategy
from .common import PIPELINE_VERSION, ArtSpace, atomic_write_bytes, atomic_write_text, dedupe, sha1
from .elements import ElementStore, Elem, element_name, to_model
from .geometry import TEXTURE_SIZE, build_bundle, geometry_file
from .layers import members_for
from .normalize import extract_elements, matte_opaque_image_plate, normalize
from .ops import ZOrderError, fresh_layers
from . import ops as _ops
from .paths import bounds, clean_d, islands, skia_from_d
from .plate import (CLIP_WARN_FRACTION, clipped_fraction, detect_full_bleed, detect_plate, make_canvas,
                    plate_record)
from .prepass import Options, auto_name, prepass
from .split import compute_analysis
from .tiling import auto_mode
from . import raster, textures

__all__ = [
    "ImportResult", "ZOrderError", "import_svg", "split_layers", "merge_layers", "split_layer",
    "move_elements", "layer_auto_modes", "build_geometry", "geometry_path", "thumbnail_png",
    "layer_thumbnail_png", "read_store", "PIPELINE_VERSION",
]

SOURCE_FILE = "source.svg"
NORMALIZED_FILE = "normalized.svg"
IMAGES_DIR = "images"


@dataclass
class ImportResult:
    source: SourceInfo
    elements: List[Element]
    layers: List[Layer]
    canvas: Canvas
    warnings: List[str] = field(default_factory=list)


def decode_svg_bytes(data: bytes) -> str:
    """bytes (svg or svgz, any declared encoding) -> text."""
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    m = re.match(rb"\s*<\?xml[^>]*encoding=[\"']([A-Za-z0-9._-]+)[\"']", data)
    enc = m.group(1).decode("ascii") if m else "utf-8"
    try:
        return data.decode(enc)
    except (LookupError, UnicodeDecodeError):
        return data.decode("utf-8", errors="replace")


# ----------------------------------------------------------------------------------------------
# import
# ----------------------------------------------------------------------------------------------
def import_svg(svg_bytes: bytes, filename: str, project_dir: Path, strategy: SplitStrategy = "smart") -> ImportResult:
    """Parse, normalise and split an SVG. Writes ``source.svg``, ``normalized.svg``,
    ``elements.json`` and ``images/*`` into `project_dir`. Raises ValueError on unreadable XML."""
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(project_dir / SOURCE_FILE, svg_bytes)
    text = decode_svg_bytes(svg_bytes)
    try:
        pre = prepass(text, Options())
    except Exception as ex:  # lxml.etree.XMLSyntaxError and friends
        raise ValueError(f"not a readable SVG: {ex}") from ex
    pico = normalize(pre)
    ex = extract_elements(pico, pre)
    matte_opaque_image_plate(ex, pre)
    elems = ex.elems
    vb = pre.view_box
    tol = ex.tolerance
    det = detect_plate(elems, vb, tol)
    if det is None:
        det = detect_full_bleed(elems, vb, tol)   # edge-to-edge art: frame the canvas to its outline
    plate_idx = set(det["indices"]) if det else set()

    # a single compound foreground shape (icon-font glyph): one element per island
    fg = [i for i in range(len(elems)) if i not in plate_idx]
    if len(fg) == 1 and not elems[fg[0]].image:
        e = elems[fg[0]]
        parts = islands(e.path, tol)
        if len(parts) > 1:
            reps = []
            for k, p in enumerate(parts):
                ne = Elem(id="", uid=f"{e.uid}#i{k}", d=clean_d(p, 4), paint=e.paint, opacity=e.opacity,
                          group_opacity=e.group_opacity, opacity_group=e.opacity_group,
                          meta={**e.meta, "uid": f"{e.uid}#i{k}", "island": k, "base": f"{e.uid}#i{k}"},
                          shadow=e.shadow)
                ne._path = skia_from_d(ne.d)
                ne.bbox = bounds(ne._path)
                ne.area = float(ne.geom(tol).area)
                reps.append(ne)
            elems = elems[:fg[0]] + reps + elems[fg[0] + 1:]

    # stable public ids in paint order
    n_path = n_img = 0
    for e in elems:
        if e.image:
            e.id = f"img{n_img}"
            n_img += 1
        else:
            e.id = f"e{n_path}"
            n_path += 1
        e.name = element_name(e, auto_name)

    # persist raster payloads
    for e in elems:
        if e.image:
            data = getattr(e, "_image_data", None)
            ext = "jpg" if e.image["mime"] == "image/jpeg" else "png"
            rel = f"{IMAGES_DIR}/{e.id}.{ext}"
            if data is not None:
                atomic_write_bytes(project_dir / rel, data)
            e.image["file"] = rel

    edges, gaps, inside = compute_analysis(elems, vb, tol)
    art = ArtSpace(vb)
    plate = plate_record(det, elems, art) if det else None
    if plate and plate.get("fullBleed"):
        pre.warnings.append(f"no plate element: the art fills a {plate['shape']} edge to edge, so the canvas "
                            f"plate is fitted to the art's outline (IoU {plate['iou']:.2f}, fill {plate['fill']})")
    if plate:
        cut = clipped_fraction(elems, plate, art, tol)
        if cut > CLIP_WARN_FRACTION:
            pre.warnings.append(f"{cut * 100:.1f}% of the art reaches past the plate edge: its 3D geometry is "
                                f"clipped to the plate outline")
    warnings = dedupe(pre.warnings)
    store = ElementStore(filename=filename, view_box=vb, elems=elems, gradients=ex.gradients, plate=plate,
                         warnings=warnings, edges=edges, gaps=gaps, inside=inside, tolerance=tol,
                         source_sha1=sha1(svg_bytes))
    store.save(project_dir)
    atomic_write_text(project_dir / NORMALIZED_FILE,
                      textures.layer_svg(elems, ex.gradients, vb, project_dir))

    canvas = make_canvas(plate, {e.id: e for e in elems}, art)
    layers, _info = fresh_layers(store, strategy, art_scale=canvas.art.scale)
    if not elems:
        warnings = dedupe(warnings + ["no paintable shapes found"])
    full_bleed = bool(plate and plate.get("fullBleed"))
    source = SourceInfo(filename=filename, viewBox=tuple(vb), warnings=warnings,
                        plateDetected=plate is not None and not full_bleed, fullBleed=full_bleed)
    return ImportResult(source=source, elements=[to_model(e, art) for e in elems], layers=layers,
                        canvas=canvas, warnings=warnings)


def read_store(project_dir: Path) -> ElementStore:
    """The project's element store (cached in memory, reloaded when the file changes)."""
    return ElementStore.load(Path(project_dir))


# ----------------------------------------------------------------------------------------------
# layer operations
# ----------------------------------------------------------------------------------------------
def split_layers(project_dir: Path, project: Project, strategy: SplitStrategy) -> List[Layer]:
    """Fresh default layers for all non-plate elements of `project` with `strategy` (default depth, stacked at
    their real heights)."""
    layers, _info = fresh_layers(read_store(project_dir), strategy, active=[e.id for e in project.elements],
                                 art_scale=project.canvas.art.scale)
    return layers


def merge_layers(project_dir: Path, project: Project, layer_ids: List[str]) -> List[Layer]:
    """Merge layers into the bottom-most selected one (keeps its id and settings).
    Raises ZOrderError when the merge would break the stacking order."""
    return _ops.merge(read_store(project_dir), project, layer_ids)


def split_layer(project_dir: Path, project: Project, layer_id: str,
                mode: Literal["elements", "islands"] = "elements") -> List[Layer]:
    """Split one layer: 'elements' = one layer per element (fill+stroke pairs and opacity groups
    stay together); 'islands' = by spatially separate pieces (multi-island compound paths are
    exploded into new elements - ``project.elements`` is then updated IN PLACE, persist it too)."""
    return _ops.split_layer(read_store(project_dir), project, layer_id, mode, Path(project_dir))


def move_elements(project_dir: Path, project: Project, element_ids: List[str],
                  to_layer_id: Optional[str]) -> List[Layer]:
    """Move elements to another layer (None = a new layer). Emptied layers are removed; the
    result is re-ordered to stay z-order consistent. Raises ZOrderError if impossible."""
    return _ops.move(read_store(project_dir), project, element_ids, to_layer_id)


def layer_auto_modes(project_dir: Path, project: Project) -> Dict[str, str]:
    """{layer id: the mode the tiling heuristic picks for the layer's elements}
    (:func:`bis.svg.tiling.auto_mode`: 'combined' for tiles of one shape / shading overlays). A layer
    whose ``mode`` differs was set by the user (the server's style copy only transfers such modes)."""
    store = read_store(project_dir)
    return {L.id: auto_mode(store, L.elementIds) for L in project.layers}


# ----------------------------------------------------------------------------------------------
# geometry / thumbnails
# ----------------------------------------------------------------------------------------------
def build_geometry(project_dir: Path, project: Project, url_prefix: str, *,
                   texture_size: int = TEXTURE_SIZE) -> GeometryBundle:
    """Geometry bundle for `project` (hash-cached under project_dir/cache): per layer the
    silhouette + occlusion-cut regions as art-space splines, safe bevel radius, a standalone
    layer SVG and an edge-padded RGBA texture of the art square. URLs = f"{url_prefix}/cache/..";
    texturePath / images[].path are absolute filesystem paths."""
    return build_bundle(Path(project_dir), project, url_prefix, texture_size)


def geometry_path(project_dir: Path, bundle: GeometryBundle) -> Path:
    return geometry_file(Path(project_dir), bundle.hash)


def _clamp_size(size, hi: int) -> int:
    """Thumbnail sizes come from HTTP query strings: keep them sane."""
    try:
        return max(8, min(hi, int(size)))
    except (TypeError, ValueError):
        return 96


def thumbnail_png(svg_bytes: bytes, size: int = 256) -> bytes:
    """Square transparent PNG thumbnail of an SVG file (resvg). `size` is clamped to 8..2048."""
    return raster.thumbnail(decode_svg_bytes(svg_bytes), _clamp_size(size, 2048))


def layer_thumbnail_png(project_dir: Path, project: Project, layer_id: str, size: int = 96) -> bytes:
    """Square PNG of one layer's art (covers the art square, like the layer texture).
    `size` is clamped to 8..1024."""
    size = _clamp_size(size, 1024)
    store = read_store(project_dir)
    layer = next((L for L in project.layers if L.id == layer_id), None)
    if layer is None:
        raise ValueError(f"unknown layer id: {layer_id}")
    members = members_for(store, layer.elementIds)
    svg = textures.layer_svg(members, store.gradients, store.art.square_view_box(), Path(project_dir),
                             size=(size, size))
    return raster.encode_png(raster.render_svg(svg, size, size), 6)
