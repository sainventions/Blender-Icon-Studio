"""Layer SVG writer and layer textures.

Every material reads its paint from a per-layer RGBA texture covering the ART square −1..1
(``u=(x+1)/2, v=(y+1)/2``): the layer's SVG rasterised by resvg at 2048 px, with the RGB of
transparent pixels filled from the nearest opaque pixel (edge padding) so bilinear filtering and
mip-mapping never bleed black/white fringes into the extruded caps and bevels. Alpha is kept."""
from __future__ import annotations

import base64
import os
import threading
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from .common import fmt
from .elements import Elem
from . import raster

SVG_HEAD = ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            'viewBox="{vb}" width="{w}" height="{h}">')


class _DataUriCache:
    def __init__(self, cap: int = 24):
        self._d: Dict[Tuple[str, int], str] = {}
        self._lock = threading.Lock()
        self.cap = cap

    def get(self, path: Path, mime: str) -> Optional[str]:
        try:
            st = os.stat(path)
        except OSError:
            return None
        key = (str(path), st.st_mtime_ns)
        with self._lock:
            if key in self._d:
                return self._d[key]
        uri = f"data:{mime};base64," + base64.b64encode(Path(path).read_bytes()).decode("ascii")
        with self._lock:
            self._d[key] = uri
            while len(self._d) > self.cap:
                self._d.pop(next(iter(self._d)))
        return uri


_URIS = _DataUriCache()


def image_href(e: Elem, project_dir: Optional[Path]) -> Optional[str]:
    if not e.image:
        return None
    data = getattr(e, "_image_data", None)
    if data is not None:
        return f"data:{e.image['mime']};base64," + base64.b64encode(data).decode("ascii")
    if project_dir is None or not e.image.get("file"):
        return None
    return _URIS.get(Path(project_dir) / e.image["file"], e.image["mime"])


def layer_svg(members: Sequence[Elem], gradients: Dict[str, str], view_box: Sequence[float],
              project_dir: Optional[Path] = None, *, size: Optional[Tuple[float, float]] = None,
              silhouette: Optional[str] = None) -> str:
    """Standalone SVG of `members` (paint order). `silhouette` = a fill colour to paint every
    member flat (masks / silhouette checks); images are then drawn as their geometry."""
    w, h = size if size else (view_box[2], view_box[3])
    parts = [SVG_HEAD.format(vb=" ".join(fmt(v, 6) for v in view_box), w=fmt(w, 6), h=fmt(h, 6))]
    defs: List[str] = []
    if silhouette is None:
        used = sorted({m.paint.get("id") for m in members if m.paint["type"] in ("linear", "radial")} - {None})
        defs += [gradients[g] for g in used if g in gradients]
        for m in members:
            if m.image:
                defs.append(f'<clipPath id="clip-{m.id}"><path d="{m.image["clipD"]}"/></clipPath>')
    if defs:
        parts.append("<defs>" + "".join(defs) + "</defs>")
    open_group: Optional[str] = None
    for m in members:
        if m.opacity_group != open_group:
            if open_group is not None:
                parts.append("</g>")
            if m.opacity_group is not None and silhouette is None:
                parts.append(f'<g opacity="{fmt(m.group_opacity)}">')
                open_group = m.opacity_group
            else:
                open_group = None if silhouette is not None else m.opacity_group
        op = "" if silhouette is not None or m.opacity >= 0.999 else f' opacity="{fmt(m.opacity)}"'
        if m.image and silhouette is None:
            href = image_href(m, project_dir)
            if href is None:
                continue
            a, b, c, d, e, f = m.image["matrix"]
            parts.append(
                f'<g clip-path="url(#clip-{m.id})"{op} data-id="{m.id}">'
                f'<image width="{m.image["width"]}" height="{m.image["height"]}" preserveAspectRatio="none" '
                f'transform="matrix({fmt(a, 9)} {fmt(b, 9)} {fmt(c, 9)} {fmt(d, 9)} {fmt(e, 6)} {fmt(f, 6)})" '
                f'xlink:href="{href}"/></g>')
            continue
        if silhouette is not None:
            fill = silhouette
        elif m.paint["type"] in ("linear", "radial"):
            fill = f"url(#{m.paint['id']})"
        else:
            fill = m.paint["hex"]
        parts.append(f'<path data-id="{m.id}" fill="{fill}"{op} d="{m.d}"/>')
    if open_group is not None:
        parts.append("</g>")
    parts.append("</svg>")
    return "".join(parts)


def edge_pad(rgba: np.ndarray, alpha_min: int = 128) -> np.ndarray:
    """Fill the RGB of every pixel with alpha < `alpha_min` with the colour of the nearest pixel
    with alpha >= `alpha_min` (exact Euclidean nearest via OpenCV's labelled distance transform).
    Alpha is preserved. Covers the whole texture, i.e. far more than the required 16 px."""
    alpha = rgba[:, :, 3]
    holes = (alpha < alpha_min).astype(np.uint8)
    if not holes.any():
        return rgba
    if holes.all():
        if (alpha > 0).any():  # only faint pixels: pad from those instead
            return edge_pad(rgba, 1)
        out = rgba.copy()
        out[:, :, :3] = 128
        return out
    _dist, labels = cv2.distanceTransformWithLabels(holes, cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
    flat_labels = labels.ravel()
    src_idx = np.flatnonzero(holes.ravel() == 0)
    lut = np.zeros(int(flat_labels.max()) + 1, dtype=np.int64)
    lut[flat_labels[src_idx]] = src_idx
    nearest = lut[flat_labels]
    rgb = rgba[:, :, :3].reshape(-1, 3)
    out = rgba.copy()
    out_rgb = out[:, :, :3].reshape(-1, 3)
    hole_idx = np.flatnonzero(holes.ravel())
    out_rgb[hole_idx] = rgb[nearest[hole_idx]]
    out[:, :, :3] = out_rgb.reshape(rgba.shape[0], rgba.shape[1], 3)
    return out


def render_texture(svg_text: str, size: int) -> np.ndarray:
    """Rasterise an art-square SVG to an edge-padded RGBA texture (size × size)."""
    img = raster.render_svg(svg_text, size, size)
    if img.shape[0] != size or img.shape[1] != size:
        img = cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)
    return edge_pad(img)
