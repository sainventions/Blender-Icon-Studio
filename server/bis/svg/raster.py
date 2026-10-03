"""Raster helpers: resvg rendering, PNG IO, image silhouettes (alpha contours via OpenCV),
background matting of opaque images, thumbnails and image-diff utilities."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Tuple

import cv2
import numpy as np
import pathops
import resvg_py
from PIL import Image

from .common import compose
from picosvg.svg_transform import Affine2D

# ----------------------------------------------------------------------------------------------
# rendering / encoding
# ----------------------------------------------------------------------------------------------


def render_svg(svg_text: str, width: int, height: Optional[int] = None, *, fonts: bool = False) -> np.ndarray:
    """Render SVG text with resvg -> (H, W, 4) uint8 RGBA (straight alpha)."""
    png = resvg_py.svg_to_bytes(svg_string=svg_text, width=int(width),
                                height=int(height) if height else None, skip_system_fonts=not fonts)
    return decode_rgba(bytes(png))


def decode_rgba(data: bytes) -> np.ndarray:
    im = Image.open(io.BytesIO(data))
    if im.mode != "RGBA":
        im = im.convert("RGBA")
    return np.asarray(im)


def encode_png(rgba: np.ndarray, level: int = 3) -> bytes:
    """Fast PNG encode (OpenCV) of an RGBA / RGB / gray array."""
    if rgba.ndim == 3 and rgba.shape[2] == 4:
        arr = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)
    elif rgba.ndim == 3 and rgba.shape[2] == 3:
        arr = cv2.cvtColor(rgba, cv2.COLOR_RGB2BGR)
    else:
        arr = rgba
    ok, buf = cv2.imencode(".png", arr, [cv2.IMWRITE_PNG_COMPRESSION, int(level)])
    if not ok:
        raise RuntimeError("PNG encoding failed")
    return buf.tobytes()


def svg_has_text(svg_text: str) -> bool:
    return "<text" in svg_text or ":text" in svg_text


def render_source(svg_text: str, w: int, h: int) -> np.ndarray:
    """Render an ORIGINAL (user) SVG as faithfully as possible.

    Some exporters (Illustrator) emit filter regions in the filtered element's transformed user
    space that hide the content in spec-strict renderers such as resvg (corpus: Outlook). When the
    filters erase real content, the unfiltered art is shown instead."""
    fonts = svg_has_text(svg_text)
    img = render_svg(svg_text, w, h, fonts=fonts)
    if "<filter" in svg_text:
        plain = render_svg(strip_filters(svg_text), w, h, fonts=fonts)
        solid = plain[..., 3] > 200
        if solid.any():
            lost = (np.abs(on_color(img).astype(np.int16) - on_color(plain).astype(np.int16)).max(axis=2) > 128) & solid
            if lost.mean() > 0.005:
                return plain
    return img


def thumbnail(svg_text: str, size: int = 256) -> bytes:
    """Square transparent PNG with the SVG fitted (aspect preserved) and centred."""
    from .prepass import parse_svg_root, parse_viewbox

    try:
        vb = parse_viewbox(parse_svg_root(svg_text))
    except Exception:  # noqa: BLE001
        vb = None
    if vb and vb[2] > 0 and vb[3] > 0:
        if vb[2] >= vb[3]:
            w, h = size, max(1, int(round(size * vb[3] / vb[2])))
        else:
            w, h = max(1, int(round(size * vb[2] / vb[3]))), size
    else:
        w, h = size, size
    try:
        img = render_source(svg_text, w, h)
    except Exception:  # noqa: BLE001 - fall back to the normalised SVG (handles exotic input)
        from .normalize import normalize
        from .prepass import prepass
        img = render_svg(normalize(prepass(svg_text)).tostring(), w, h)
    if img.shape[0] != size or img.shape[1] != size:
        canvas = np.zeros((size, size, 4), np.uint8)
        y0 = (size - img.shape[0]) // 2
        x0 = (size - img.shape[1]) // 2
        canvas[y0:y0 + img.shape[0], x0:x0 + img.shape[1]] = img[:size, :size]
        img = canvas
    return encode_png(img, 6)


# ----------------------------------------------------------------------------------------------
# masks -> paths
# ----------------------------------------------------------------------------------------------


def mask_to_path(mask: np.ndarray, matrix: Affine2D, eps_px: float = 0.7,
                 min_area_px: float = 6.0) -> pathops.Path:
    """Binary mask -> simplified pathops.Path (outer contours + holes), mapped by `matrix`
    (pixel space -> target space). OpenCV contours run through the centres of the boundary
    pixels, so the polygon is grown by half a pixel to sit on the true pixel edges."""
    from .paths import path_from_shapely, shapely_from_path

    m = (mask > 0).astype(np.uint8)
    if not m.any():
        return pathops.Path()
    contours, _hier = cv2.findContours(m, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    path = pathops.Path()
    path.fillType = pathops.FillType.EVEN_ODD
    n_added = 0
    for cnt in contours:
        if len(cnt) < 3 or abs(cv2.contourArea(cnt)) < min_area_px:
            continue
        approx = cv2.approxPolyDP(cnt, eps_px, True)[:, 0, :].astype(np.float64) + 0.5
        if len(approx) < 3:
            continue
        path.moveTo(float(approx[0, 0]), float(approx[0, 1]))
        for x, y in approx[1:]:
            path.lineTo(float(x), float(y))
        path.close()
        n_added += 1
    if not n_added:
        return pathops.Path()
    try:
        path.simplify(fix_winding=True)
    except pathops.PathOpsError:
        pass
    geom = shapely_from_path(path, 0.25).buffer(0.5, join_style="mitre", mitre_limit=2.0)
    grown = path_from_shapely(geom)
    return grown.transform(matrix.a, matrix.b, matrix.c, matrix.d, matrix.e, matrix.f)


def path_to_mask(path: pathops.Path, inv_matrix: Affine2D, shape: Tuple[int, int], tol_px: float = 0.5) -> np.ndarray:
    """Rasterise a (target-space) pathops path into a pixel mask (inverse of mask_to_path)."""
    from .paths import flatten_contours

    h, w = shape
    mask = np.zeros((h, w), np.uint8)
    rings: list = []
    for ring in flatten_contours(path.transform(inv_matrix.a, inv_matrix.b, inv_matrix.c, inv_matrix.d,
                                                inv_matrix.e, inv_matrix.f), tol_px):
        pts = np.round((np.asarray(ring) - 0.5) * 16).astype(np.int32)
        rings.append(pts)
    # even-odd composition of the rings == the simplified (disjoint contour) path semantics
    for r in rings:
        tmp = np.zeros((h, w), np.uint8)
        cv2.fillPoly(tmp, [r], 1, lineType=cv2.LINE_8, shift=4)
        mask ^= tmp
    return mask


# ----------------------------------------------------------------------------------------------
# raster images
# ----------------------------------------------------------------------------------------------
MAX_TRACE_PX = 1024


@dataclass
class ImageAnalysis:
    silhouette: pathops.Path      # target (SVG) space, alpha-thresholded contours
    coverage: float               # fraction of image pixels inside the silhouette
    mean_alpha: float             # mean alpha (0..1) inside the silhouette
    opaque: bool                  # practically fully opaque inside the silhouette
    avg_rgb: Tuple[float, float, float]
    threshold: int


def alpha_threshold(alpha: np.ndarray) -> int:
    """Adaptive alpha threshold: half of the (robust) maximum alpha, clamped to [8, 128].
    Subtle overlay images (e.g. a 20 % radar sweep) still get a silhouette."""
    nz = alpha[alpha > 0]
    if nz.size == 0:
        return 128
    p99 = float(np.percentile(nz, 99))
    return int(min(128, max(8, round(p99 * 0.5))))


def _trace_scale(w: int, h: int) -> float:
    return min(1.0, MAX_TRACE_PX / max(w, h))


def analyse_image(data: bytes, matrix: Sequence[float]) -> Tuple[ImageAnalysis, np.ndarray]:
    """Silhouette of a raster image from its alpha channel. Returns (analysis, rgba)."""
    rgba = decode_rgba(data)
    h, w = rgba.shape[:2]
    s = _trace_scale(w, h)
    alpha = rgba[:, :, 3]
    if s < 1.0:
        alpha_s = cv2.resize(alpha, (max(1, int(round(w * s))), max(1, int(round(h * s)))),
                             interpolation=cv2.INTER_AREA)
    else:
        alpha_s = alpha
    thr = alpha_threshold(alpha_s)
    smooth = cv2.GaussianBlur(alpha_s, (0, 0), 0.8)
    mask = (smooth >= thr).astype(np.uint8)
    m = Affine2D(*matrix)
    pix = compose(Affine2D(w / alpha_s.shape[1], 0, 0, h / alpha_s.shape[0], 0, 0), m)
    sil = mask_to_path(mask, pix)
    inside = alpha_s[mask > 0]
    mean_alpha = float(inside.mean()) / 255.0 if inside.size else 0.0
    opaque = bool(inside.size) and float((inside >= 250).mean()) >= 0.97
    a = rgba[:, :, 3].astype(np.float64)
    wsum = a.sum()
    if wsum > 0:
        avg = tuple(float((rgba[:, :, k].astype(np.float64) * a).sum() / wsum) / 255.0 for k in range(3))
    else:
        avg = (0.5, 0.5, 0.5)
    return ImageAnalysis(sil, float(mask.mean()), mean_alpha, opaque, avg, thr), rgba  # type: ignore[arg-type]


def matte_background(rgba: np.ndarray, matrix: Sequence[float], region: pathops.Path,
                     min_delta_e: float = 14.0) -> Optional[Tuple[Tuple[float, float, float], pathops.Path]]:
    """Separate an opaque raster icon into a flat background colour and a foreground silhouette.

    The background colour is the median colour of a band just inside the visible `region`
    (SVG space); foreground = pixels whose Lab distance from it exceeds `min_delta_e`.
    Returns None when the image has no dominant flat background."""
    h, w = rgba.shape[:2]
    s = _trace_scale(w, h)
    small = cv2.resize(rgba, (max(1, int(round(w * s))), max(1, int(round(h * s)))),
                       interpolation=cv2.INTER_AREA) if s < 1.0 else rgba
    sh, sw = small.shape[:2]
    m = Affine2D(*matrix)
    pix = compose(Affine2D(w / sw, 0, 0, h / sh, 0, 0), m)
    region_mask = path_to_mask(region, pix.inverse(), (sh, sw))
    if region_mask.sum() < 64:
        return None
    k = max(3, int(round(max(sh, sw) * 0.006)) | 1)
    # zero border: a region touching the image edge must erode from that edge too
    inner = cv2.erode(region_mask, np.ones((k, k), np.uint8), borderType=cv2.BORDER_CONSTANT, borderValue=0)
    band_w = max(5, int(round(max(sh, sw) * 0.03)) | 1)
    deep = cv2.erode(region_mask, np.ones((band_w, band_w), np.uint8), borderType=cv2.BORDER_CONSTANT,
                     borderValue=0)
    band = (inner > 0) & (deep == 0)
    if band.sum() < 16:
        return None
    rgb = np.ascontiguousarray(small[:, :, :3])
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    lab[:, :, 0] *= 100.0 / 255.0
    lab[:, :, 1:] -= 128.0
    bg_lab = np.median(lab[band], axis=0)
    spread = np.median(np.linalg.norm(lab[band] - bg_lab, axis=1))
    if spread > 8.0:  # border is not a flat colour -> no clean background to separate
        return None
    de = np.linalg.norm(lab - bg_lab, axis=2)
    fg = ((de > min_delta_e) & (inner > 0)).astype(np.uint8)
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(fg, connectivity=8)
    min_area = 0.0008 * region_mask.sum()
    keep = np.zeros(n, bool)
    keep[1:] = stats[1:, cv2.CC_STAT_AREA] >= min_area
    fg = keep[labels].astype(np.uint8)
    frac = fg.sum() / max(1, region_mask.sum())
    if frac < 0.01 or frac > 0.9:
        return None
    bg_rgb = np.median(rgb[band].astype(np.float64), axis=0) / 255.0
    return (float(bg_rgb[0]), float(bg_rgb[1]), float(bg_rgb[2])), mask_to_path(fg, pix)


# ----------------------------------------------------------------------------------------------
# compositing / diff utilities (tests, CLI)
# ----------------------------------------------------------------------------------------------


def over(dst: np.ndarray, src: np.ndarray) -> np.ndarray:
    """Straight-alpha 'source over' of two uint8 RGBA images (sRGB space, like browsers/resvg)."""
    d = dst.astype(np.float64) / 255.0
    s = src.astype(np.float64) / 255.0
    sa, da = s[..., 3:4], d[..., 3:4]
    oa = sa + da * (1 - sa)
    rgb = np.where(oa > 0, (s[..., :3] * sa + d[..., :3] * da * (1 - sa)) / np.maximum(oa, 1e-12), 0)
    out = np.concatenate([rgb, oa], axis=2)
    return np.clip(np.round(out * 255), 0, 255).astype(np.uint8)


def composite(layers: Iterable[np.ndarray]) -> np.ndarray:
    out = None
    for im in layers:
        out = im.copy() if out is None else over(out, im)
    return out


def on_color(img: np.ndarray, rgb=(255, 255, 255)) -> np.ndarray:
    bg = np.zeros_like(img)
    bg[..., :3] = rgb
    bg[..., 3] = 255
    return over(bg, img)[..., :3]


def diff_pct(a: np.ndarray, b: np.ndarray, thr: int = 24) -> float:
    """% of pixels whose max channel difference (both composited on white AND on black) > thr."""
    worst = 0.0
    for col in ((255, 255, 255), (0, 0, 0)):
        A = on_color(a, col).astype(np.int16)
        B = on_color(b, col).astype(np.int16)
        worst = max(worst, float((np.abs(A - B).max(axis=2) > thr).mean() * 100.0))
    return round(worst, 3)


def strip_filters(svg_text: str) -> str:
    """Remove filter references (attributes and CSS) - the pipeline turns them into 3D shadows,
    so fidelity checks compare against the unfiltered source."""
    svg_text = re.sub(r"filter\s*:\s*url\([^)]*\)\s*;?", "", svg_text)
    return re.sub(r"\sfilter\s*=\s*\"[^\"]*\"", "", svg_text)
