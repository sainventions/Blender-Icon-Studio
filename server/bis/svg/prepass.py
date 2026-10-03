"""lxml prepass that turns real-world SVG into something picosvg can normalise losslessly.

What it does (in order):
  * secure parse (no entities / network), drop foreign-namespace content, title/desc/metadata
  * CSS: ``<style>`` rules by specificity, then ``style=""``; written back as attributes
  * ``display:none`` removal, ``<a>``/``<switch>`` -> ``<g>``, nested ``<svg>`` -> clipped ``<g>``
  * ``<use>``/``<symbol>`` expansion (picosvg rejects SVG2 ``href`` and drops ids of clones)
  * ``<filter>`` drop shadows (feOffset / feGaussianBlur / feFlood / feDropShadow / Figma colour
    matrices) are parsed into per-element shadow metadata; other filters are dropped with a warning
  * ``<image>`` elements are decoded and replaced by a placeholder ``<rect id="i<n>">`` covering the
    visible image area, so picosvg applies transforms and clip paths to it while the pixels and
    their exact pixel->user-space matrix are kept on the side (``Prepass.images``)
  * computed-style walk: inherited paint written onto every shape, ``currentColor`` resolved,
    colours parsed, objectBoundingBox gradients baked to userSpaceOnUse from the ORIGINAL geometry
  * stroke -> fill with a pre-scaled skia stroker (accurate on small viewBoxes)
  * provenance: every painted leaf gets a synthetic ``id`` and a ``meta`` record
"""
from __future__ import annotations

import base64
import copy
import io
import math
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pathops
from lxml import etree
from picosvg import svg_pathops
from picosvg.svg import from_element as pico_from_element
from picosvg.svg_transform import Affine2D
from picosvg.svg_types import SVGPath

from .colors import parse_color, to_hex
from .common import (INKSCAPE_NS, SVG_NS, XLINK_HREF, XLINK_NS, affine_tuple, compose, fmt, local,
                     matrix_scale, ns_of, num, num_list, q)
from .css import parse_decls, parse_stylesheet, selector_matches

SHAPE_TAGS = {"path", "rect", "circle", "ellipse", "line", "polyline", "polygon"}
GEOM_ATTRS = {
    "path": {"d"},
    "rect": {"x", "y", "width", "height", "rx", "ry"},
    "circle": {"cx", "cy", "r"},
    "ellipse": {"cx", "cy", "rx", "ry"},
    "line": {"x1", "y1", "x2", "y2"},
    "polyline": {"points"},
    "polygon": {"points"},
}
GRADIENT_TAGS = {"linearGradient", "radialGradient"}
GRADIENT_ATTRS = {
    "linearGradient": {"x1", "y1", "x2", "y2"},
    "radialGradient": {"cx", "cy", "r", "fx", "fy", "fr"},
}
COMMON_GRADIENT_ATTRS = {"gradientUnits", "gradientTransform", "spreadMethod"}
INHERITED = [
    "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-linecap",
    "stroke-linejoin", "stroke-miterlimit", "stroke-dasharray", "stroke-dashoffset",
    "stroke-opacity", "color", "paint-order", "visibility", "clip-rule",
]
PRESENTATION = set(INHERITED) | {"opacity", "transform", "clip-path", "display", "stop-color",
                                 "stop-opacity", "mask", "filter", "mix-blend-mode", "marker-start",
                                 "marker-mid", "marker-end", "vector-effect", "flood-color",
                                 "flood-opacity"}
UNSUPPORTED_ELEMENTS = {"text", "foreignObject", "marker", "script", "audio", "video", "canvas",
                        "iframe", "animate", "animateTransform", "animateMotion", "set"}
DROP_SILENTLY = {"title", "desc", "metadata", "style"}
IMAGE_MARKER = "#ff00ff"


@dataclass
class Options:
    current_color: str = "#000000"      # what `currentColor` resolves to (line icons)
    stroke_scale_target: float = 256.0  # stroke geometry is scaled so width ~ this before stroking
    ndigits: int = 4                    # picosvg float rounding
    curve_tolerance_pct: float = 0.05   # flattening tolerance for analysis polygons, % of vb diag


@dataclass
class ImageRef:
    """A raster ``<image>`` replaced by the placeholder rect ``uid``."""
    uid: str
    data: bytes                 # encoded PNG / JPEG
    mime: str                   # image/png | image/jpeg
    width: int                  # pixels
    height: int
    matrix: Tuple[float, float, float, float, float, float]  # pixel space -> SVG root user space


@dataclass
class Prepass:
    svg_text: str
    view_box: Tuple[float, float, float, float]
    meta: Dict[str, dict]
    warnings: List[str]
    images: Dict[str, ImageRef] = field(default_factory=dict)


def _secure_parser():
    return etree.XMLParser(resolve_entities=False, no_network=True, remove_comments=True,
                           remove_pis=True, huge_tree=True, recover=False)


_DOCTYPE_RE = re.compile(r"<!DOCTYPE[^>\[]*(\[([^\]]*)\])?\s*>", re.S)
_ENTITY_RE = re.compile(r"<!ENTITY\s+([A-Za-z_][\w.:-]*)\s+([\"'])(.*?)\2\s*>", re.S)
_MAX_ENTITY_GROWTH = 4 * 1024 * 1024  # bytes an entity expansion may add (billion-laughs guard)


def _strip_doctype(text: str) -> str:
    """Remove the DOCTYPE. Simple *internal* general entities of its subset (old Illustrator
    exports: ``<!ENTITY ns_svg "http://www.w3.org/2000/svg">`` + ``xmlns="&ns_svg;"``) are
    expanded textually first; external / parameter entities are never resolved."""
    m = _DOCTYPE_RE.search(text)
    if not m:
        return text
    entities = {name: value for name, _q, value in _ENTITY_RE.findall(m.group(2) or "")}
    text = text[:m.start()] + text[m.end():]
    if not entities:
        return text
    pattern = re.compile("&(" + "|".join(re.escape(k) for k in entities) + ");")
    limit = len(text) + _MAX_ENTITY_GROWTH
    for _ in range(4):  # entity values may reference other entities (bounded)
        text, n = pattern.subn(lambda mm: entities[mm.group(1)], text)
        if not n:
            break
        if len(text) > limit:
            raise ValueError("entity expansion too large")
    return text


def parse_svg_root(svg_text: str):
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", svg_text.lstrip("﻿"))
    text = _strip_doctype(text)
    root = etree.fromstring(text.encode("utf-8"), _secure_parser())
    if local(root.tag) != "svg":
        raise ValueError("root element is not <svg>")
    return root


def _href(el) -> Optional[str]:
    return el.get(XLINK_HREF) or el.get("href")


def _par_mapping(vb, w: float, h: float, par: Optional[str]) -> Tuple[float, float, float, float]:
    """preserveAspectRatio: map box vb=(x,y,w,h) into a w×h viewport -> (sx, sy, tx, ty)."""
    vx, vy, vw, vh = vb
    sx, sy = w / vw, h / vh
    parts = (par or "xMidYMid meet").split()
    if parts and parts[0] == "defer":
        parts = parts[1:]
    align = parts[0] if parts else "xMidYMid"
    if align != "none":
        s = min(sx, sy) if (len(parts) < 2 or parts[1] == "meet") else max(sx, sy)
        sx = sy = s
    tx, ty = -vx * sx, -vy * sy
    if align != "none":
        if "xMid" in align:
            tx += (w - vw * sx) / 2
        elif "xMax" in align:
            tx += w - vw * sx
        if "YMid" in align:
            ty += (h - vh * sy) / 2
        elif "YMax" in align:
            ty += h - vh * sy
    return sx, sy, tx, ty


def _viewbox_transform(vb, w, h, par="xMidYMid meet") -> str:
    if vb[2] <= 0 or vb[3] <= 0:
        return ""
    sx, sy, tx, ty = _par_mapping(vb, w, h, par)
    return f"matrix({fmt(sx, 6)} 0 0 {fmt(sy, 6)} {fmt(tx, 6)} {fmt(ty, 6)})"


def parse_viewbox(root) -> Optional[Tuple[float, float, float, float]]:
    vb = root.get("viewBox")
    if vb:
        vals = num_list(vb)
        if len(vals) == 4 and vals[2] > 0 and vals[3] > 0:
            return tuple(vals)  # type: ignore[return-value]
    w, h = num(root.get("width"), 0), num(root.get("height"), 0)
    if w > 0 and h > 0:
        return (0.0, 0.0, w, h)
    return None


def auto_name(name: Optional[str]) -> bool:
    """True for generator noise: g12, path3, Layer_1, clip0_12_34, Group 5, Vector, a, b ..."""
    if not name:
        return True
    n = name.strip().lower()
    if len(n) <= 2 and re.fullmatch(r"[a-z]{1,2}", n):
        return True  # Illustrator short ids: a, b, ..., aa
    return bool(re.fullmatch(
        r"(g|path|rect|circle|ellipse|polygon|polyline|line|image|layer|group|svg|clip|clippath|mask|"
        r"vector|shape|frame|union|subtract|use|defs|linear|radial|gradient|lineargradient|"
        r"radialgradient|filter|_?x[0-9a-f]+_?|object|compound path)[\s_-]*[\d_]*(\s*copy[\s\d]*)?", n))


def _parse_transform(s: Optional[str], warnings: List[str]) -> Affine2D:
    if not s or not s.strip():
        return Affine2D.identity()
    try:
        m = Affine2D.fromstring(s)
    except Exception:  # noqa: BLE001
        warnings.append(f"unparseable transform '{s[:40]}' ignored")
        return Affine2D.identity()
    if not all(math.isfinite(v) for v in m):
        warnings.append(f"non-finite transform '{s[:40]}' ignored")
        return Affine2D.identity()
    return m


_CSS_TF_UNITS = re.compile(r"(?<=[\d.])(px|deg)\b")


def _sanitize_transforms(root, warnings: List[str]) -> None:
    """picosvg parses transforms itself: rewrite CSS-style values (``translate(10px, 5px)``,
    ``rotate(45deg)``) and drop unparseable or non-finite (nan/inf) ones up front, so one bad
    attribute degrades to 'no transform' instead of failing the whole import."""
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for attr in ("transform", "gradientTransform", "patternTransform"):
            v = el.get(attr)
            if v is None:
                continue
            fixed = _CSS_TF_UNITS.sub("", v).strip()
            if not fixed:
                del el.attrib[attr]
                continue
            try:
                ok = all(math.isfinite(x) for x in Affine2D.fromstring(fixed))
            except Exception:  # noqa: BLE001
                ok = False
            if ok:
                if fixed != v:
                    el.set(attr, fixed)
            else:
                warnings.append(f"invalid {attr} '{v[:40]}' ignored")
                del el.attrib[attr]


class _Ctx:
    def __init__(self, root, vb, opts: Options, warnings: List[str]):
        self.root, self.vb, self.opts, self.warnings = root, vb, opts, warnings
        self.meta: Dict[str, dict] = {}
        self.images: Dict[str, ImageRef] = {}
        self.n = 0
        self.n_img = 0
        self.defs = None
        self.by_id: Dict[str, object] = {}
        self.filters: Dict[str, Optional[dict]] = {}

    def defs_el(self):
        if self.defs is None:
            self.defs = etree.Element(q("defs"))
            self.root.insert(0, self.defs)
        return self.defs


# ----------------------------------------------------------------------------------------------
# public entry
# ----------------------------------------------------------------------------------------------
def prepass(svg_text: str, opts: Optional[Options] = None) -> Prepass:
    opts = opts or Options()
    warnings: List[str] = []
    root = parse_svg_root(svg_text)
    vb = parse_viewbox(root)
    if vb is None:
        warnings.append("SVG has no viewBox/width/height: assuming 0 0 100 100")
        vb = (0.0, 0.0, 100.0, 100.0)

    # 1. drop non-SVG namespaced elements (sodipodi:namedview, metadata, ...)
    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            if el.getparent() is not None:
                el.getparent().remove(el)
            continue
        if el is not root and ns_of(el.tag) not in ("", SVG_NS) and el.getparent() is not None:
            el.getparent().remove(el)

    # 2. CSS: presentation attribute < stylesheet rule (by specificity) < inline style
    css = "\n".join((el.text or "") for el in root.iter(q("style")))
    rules = parse_stylesheet(css, warnings) if css.strip() else []
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        decls: Dict[str, str] = {}
        for compounds, _spec, _order, d in rules:
            if selector_matches(el, compounds):
                decls.update(d)
        decls.update(parse_decls(el.get("style", "")))
        if "style" in el.attrib:
            del el.attrib["style"]
        for k, v in decls.items():
            if k in PRESENTATION:
                el.set(k, v)
            elif k in ("mix-blend-mode", "isolation") and v not in ("normal", "auto"):
                warnings.append(f"CSS {k}: {v} is not supported (ignored)")
    for el in list(root.iter(q("style"))):
        el.getparent().remove(el)
    _sanitize_transforms(root, warnings)

    # 3. hidden subtrees
    for el in list(root.iter()):
        if isinstance(el.tag, str) and el.get("display", "").strip() == "none":
            par = el.getparent()
            if par is not None and local(par.tag) not in ("defs",):
                par.remove(el)

    # 4. <a>/<switch> behave like <g>
    for el in root.iter():
        if local(el.tag) in ("a", "switch"):
            el.tag = q("g")

    ctx = _Ctx(root, vb, opts, warnings)
    ctx.by_id = {el.get("id"): el for el in root.iter() if isinstance(el.tag, str) and el.get("id")}

    # 5. expand <use> (incl. <symbol> / nested <svg> targets)
    _expand_uses(ctx)
    for el in list(root.iter(q("symbol"))):
        el.getparent().remove(el)

    # 6. nested <svg> in the render tree -> clipped, transformed <g>
    _flatten_nested_svgs(ctx)

    # 7. unsupported elements / attributes
    for el in list(root.iter()):
        if not isinstance(el.tag, str) or el.getparent() is None:
            continue
        name = local(el.tag)
        if name in DROP_SILENTLY:
            el.getparent().remove(el)
        elif name in UNSUPPORTED_ELEMENTS:
            warnings.append(f"<{name}> is not supported and was dropped" +
                            (" (convert text to outlines before import)" if name == "text" else ""))
            el.getparent().remove(el)
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for attr in ("mask", "marker-start", "marker-mid", "marker-end"):
            if el.get(attr) and el.get(attr) != "none":
                warnings.append(f"{attr} on <{local(el.tag)}> is not supported (ignored)")
                del el.attrib[attr]
        f = el.get("filter")
        if f is not None:
            del el.attrib["filter"]
            m = re.match(r"url\(\s*['\"]?#([^)'\"]+)['\"]?\s*\)", f.strip())
            if m:
                el.set("data-bis-filter", m.group(1))
            elif f.strip() != "none":
                warnings.append(f"filter '{f}' is not supported (ignored)")
        if el.get("vector-effect") == "non-scaling-stroke":
            warnings.append("vector-effect=non-scaling-stroke treated as a normal stroke")

    # 8. computed-style walk: paint, gradients, strokes, images, shadows, provenance ids
    _walk(ctx, root, {
        "fill": "#000000", "fill-opacity": "1", "fill-rule": "nonzero", "stroke": "none",
        "stroke-width": "1", "stroke-linecap": "butt", "stroke-linejoin": "miter",
        "stroke-miterlimit": "4", "stroke-dasharray": "none", "stroke-dashoffset": "0",
        "stroke-opacity": "1", "color": opts.current_color, "paint-order": "normal",
        "visibility": "visible", "clip-rule": "nonzero",
    }, [], top=True, ctm=Affine2D.identity(), shadow=None)

    # clipPath children: only geometry, clip-rule and transform matter
    for cp in root.iter(q("clipPath")):
        if cp.get("clipPathUnits") == "objectBoundingBox":
            warnings.append("clipPathUnits=objectBoundingBox is not supported (clip may be wrong)")
        for el in list(cp.iter()):
            n = local(el.tag)
            if n in SHAPE_TAGS:
                _normalize_geometry_attrs(el, vb)
                for k in list(el.attrib):
                    if k not in GEOM_ATTRS[n] | {"transform", "clip-rule", "clip-path"}:
                        del el.attrib[k]
            elif n in ("use", "text", "image") and el.getparent() is not None:
                el.getparent().remove(el)

    # 9. strip everything picosvg does not understand
    _strip_attributes(root)

    # 10. rebuild root with a clean nsmap
    new_root = etree.Element(q("svg"), nsmap={None: SVG_NS, "xlink": XLINK_NS})
    new_root.set("viewBox", " ".join(fmt(v, 6) for v in vb))
    new_root.set("width", fmt(vb[2], 6))
    new_root.set("height", fmt(vb[3], 6))
    for child in list(root):
        new_root.append(child)
    out = etree.tostring(new_root, encoding="unicode")
    return Prepass(out, vb, ctx.meta, warnings, ctx.images)


# ----------------------------------------------------------------------------------------------
# tree rewrites
# ----------------------------------------------------------------------------------------------
MAX_USE_EXPANSION = 100_000   # elements <use> expansion may add (guards exponential self-reference)


def _expand_uses(ctx: _Ctx) -> None:
    root, warnings = ctx.root, ctx.warnings
    budget = MAX_USE_EXPANSION
    for _ in range(16):
        uses = list(root.iter(q("use")))
        if not uses:
            return
        for use in uses:
            ref = _href(use) or ""
            rid = ref[1:] if ref.startswith("#") else None
            target = ctx.by_id.get(rid) if rid else None
            parent = use.getparent()
            if parent is None:
                continue
            # recursive: the <use> sits inside its own target, or inside an expansion of it
            # (A -> B -> A). Either would grow the tree exponentially on every pass.
            recursive = target is not None and (
                target is use or any(a is target or a.get("data-bis-use") == rid for a in use.iterancestors()))
            if target is None or recursive:
                warnings.append(f"<use> of missing/external/recursive target '{ref}' dropped")
                parent.remove(use)
                continue
            cost = sum(1 for _ in target.iter())
            if cost > budget:
                warnings.append("<use> expansion exceeds the element budget: remaining instances dropped")
                parent.remove(use)
                continue
            budget -= cost
            g = etree.Element(q("g"))
            for k, v in use.attrib.items():
                if k not in ("x", "y", "width", "height", "href", XLINK_HREF, "transform", "id"):
                    g.set(k, v)
            if use.get("id"):
                g.set("id", use.get("id"))
            tf = use.get("transform", "")
            x, y = num(use.get("x")), num(use.get("y"))
            if x or y:
                tf += f" translate({fmt(x, 6)} {fmt(y, 6)})"
            clone = copy.deepcopy(target)
            for sub in clone.iter():
                if isinstance(sub.tag, str) and "id" in sub.attrib:
                    del sub.attrib["id"]
            if local(clone.tag) in ("symbol", "svg"):
                svb = None
                if clone.get("viewBox"):
                    vals = num_list(clone.get("viewBox"))
                    svb = tuple(vals) if len(vals) == 4 else None
                w = num(use.get("width") or clone.get("width"), svb[2] if svb else 0)
                h = num(use.get("height") or clone.get("height"), svb[3] if svb else 0)
                if svb and w > 0 and h > 0:
                    tf += " " + _viewbox_transform(svb, w, h, clone.get("preserveAspectRatio"))
                for k in ("viewBox", "preserveAspectRatio", "x", "y", "width", "height"):
                    clone.attrib.pop(k, None)
                clone.tag = q("g")
            if tf.strip():
                g.set("transform", tf.strip())
            g.set("data-bis-use", ref[1:])
            g.append(clone)
            parent.replace(use, g)
    warnings.append("<use> nesting deeper than 16 levels: remaining instances dropped")
    for use in list(root.iter(q("use"))):
        use.getparent().remove(use)


def _flatten_nested_svgs(ctx: _Ctx) -> None:
    root = ctx.root
    nested = [el for el in root.iter(q("svg")) if el is not root
              and not any(local(a.tag) in ("defs", "clipPath", "mask", "pattern") for a in el.iterancestors())]
    for k, el in enumerate(reversed(nested)):  # innermost first
        parent = el.getparent()
        if parent is None:
            continue
        pw = ctx.vb[2]
        ph = ctx.vb[3]
        x, y = num(el.get("x"), 0.0, pw), num(el.get("y"), 0.0, ph)
        w, h = num(el.get("width"), pw, pw), num(el.get("height"), ph, ph)
        vals = num_list(el.get("viewBox"))
        inner_tf = f"translate({fmt(x, 6)} {fmt(y, 6)})"
        if len(vals) == 4 and vals[2] > 0 and vals[3] > 0 and w > 0 and h > 0:
            inner_tf += " " + _viewbox_transform(tuple(vals), w, h, el.get("preserveAspectRatio"))
        cp_id = f"bis-nested-clip-{k}"
        cp = etree.SubElement(ctx.defs_el(), q("clipPath"))
        cp.set("id", cp_id)
        r = etree.SubElement(cp, q("rect"))
        for a, v in (("x", x), ("y", y), ("width", w), ("height", h)):
            r.set(a, fmt(v, 6))
        outer = etree.Element(q("g"))
        outer.set("clip-path", f"url(#{cp_id})")
        for a, v in el.attrib.items():
            if a in PRESENTATION and a not in ("transform", "clip-path"):
                outer.set(a, v)
        if el.get("id"):
            outer.set("id", el.get("id"))
        inner = etree.SubElement(outer, q("g"))
        inner.set("transform", inner_tf)
        for child in list(el):
            inner.append(child)
        parent.replace(el, outer)
        ctx.by_id[cp_id] = cp


def _strip_attributes(root) -> None:
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        name = local(el.tag)
        if name in SHAPE_TAGS:
            keep = GEOM_ATTRS[name] | {"id", "fill", "fill-opacity", "fill-rule", "stroke", "opacity",
                                       "transform", "clip-path", "clip-rule"}
        elif name == "g":
            keep = {"id", "opacity", "transform", "clip-path"}
        elif name in GRADIENT_TAGS:
            keep = GRADIENT_ATTRS[name] | COMMON_GRADIENT_ATTRS | {"id", XLINK_HREF}
            if el.get("href") and not el.get(XLINK_HREF):
                el.set(XLINK_HREF, el.get("href"))
        elif name == "stop":
            keep = {"offset", "stop-color", "stop-opacity"}
        elif name == "clipPath":
            keep = {"id", "transform", "clip-path", "clipPathUnits"}
        elif name in ("defs",):
            keep = set()
        elif name == "svg":
            keep = {"viewBox", "width", "height", "x", "y", "preserveAspectRatio", "id"}
        else:
            keep = None
        if keep is not None:
            for k in list(el.attrib):
                if k not in keep:
                    del el.attrib[k]


def _group_info(el, index_path) -> dict:
    label = el.get(f"{{{INKSCAPE_NS}}}label") or el.get("data-name") or el.get("aria-label")
    gid = el.get("id")
    name = label or gid
    return {
        "id": gid, "label": label, "name": name,
        "auto_name": auto_name(name) and not label,
        "inkscape_layer": el.get(f"{{{INKSCAPE_NS}}}groupmode") == "layer",
        "use_of": el.get("data-bis-use"),
        "opacity": num(el.get("opacity"), 1.0),
        "index_path": list(index_path),
    }


# ----------------------------------------------------------------------------------------------
# filters -> drop shadows
# ----------------------------------------------------------------------------------------------
def parse_filter_shadow(filt) -> Tuple[Optional[dict], Optional[str]]:
    """Parse a <filter> into a drop shadow spec in the filter's user units.

    Returns (shadow, problem). shadow = {dx, dy, blur, opacity, color} or None."""
    prims = [c for c in filt if isinstance(c.tag, str)]
    names = [local(c.tag) for c in prims]
    if filt.get("primitiveUnits") == "objectBoundingBox":
        return None, "uses primitiveUnits=objectBoundingBox"
    for c in prims:
        if local(c.tag) == "feDropShadow":
            col = parse_color(c.get("flood-color", "#000000")) or (0, 0, 0, 1)
            std = num_list(c.get("stdDeviation", "2")) or [2.0]
            return {"dx": num(c.get("dx"), 2.0), "dy": num(c.get("dy"), 2.0), "blur": sum(std) / len(std),
                    "opacity": num(c.get("flood-opacity"), 1.0) * col[3], "color": to_hex(col)}, None
    off = next((c for c in prims if local(c.tag) == "feOffset"), None)
    blur = next((c for c in prims if local(c.tag) == "feGaussianBlur"), None)
    flood = next((c for c in prims if local(c.tag) == "feFlood"), None)
    # Figma: <feColorMatrix values="0 0 0 0 r  0 0 0 0 g  0 0 0 0 b  0 0 0 a 0"/>
    cm_shadow = None
    for c in prims:
        if local(c.tag) == "feColorMatrix" and c.get("type", "matrix") == "matrix":
            v = num_list(c.get("values"))
            if len(v) == 20 and all(abs(x) < 1e-9 for x in v[0:4] + v[5:9] + v[10:14]) and v[18] > 0:
                cm_shadow = ((v[4], v[9], v[14]), v[18])
    if off is None and blur is None:
        return None, f"not a drop shadow ({', '.join(names) or 'empty'})"
    if flood is None and cm_shadow is None and not any(c.get("in") == "SourceAlpha" for c in prims):
        return None, f"not a drop shadow ({', '.join(names)})"
    std = num_list(blur.get("stdDeviation", "0")) if blur is not None else [0.0]
    std = std or [0.0]
    if cm_shadow is not None:
        color, opacity = to_hex(cm_shadow[0]), cm_shadow[1]
    elif flood is not None:
        col = parse_color(flood.get("flood-color", "#000000")) or (0, 0, 0, 1)
        color, opacity = to_hex(col), num(flood.get("flood-opacity"), 1.0) * col[3]
    else:
        color, opacity = "#000000", 1.0
    return {"dx": num(off.get("dx")) if off is not None else 0.0,
            "dy": num(off.get("dy")) if off is not None else 0.0,
            "blur": sum(std) / len(std), "opacity": max(0.0, min(1.0, opacity)), "color": color}, None


def _resolve_shadow(ctx: _Ctx, fid: str, ctm: Affine2D) -> Optional[dict]:
    if fid not in ctx.filters:
        filt = ctx.by_id.get(fid)
        if filt is None or local(filt.tag) != "filter":
            ctx.warnings.append(f"filter #{fid} not found (ignored)")
            ctx.filters[fid] = None
        else:
            spec, problem = parse_filter_shadow(filt)
            if problem:
                ctx.warnings.append(f"filter #{fid} {problem} - ignored")
            ctx.filters[fid] = spec
    spec = ctx.filters[fid]
    if spec is None:
        return None
    vx, vy = ctm.map_vector((spec["dx"], spec["dy"]))
    return {"dx": vx, "dy": vy, "blur": spec["blur"] * matrix_scale(ctm),
            "opacity": spec["opacity"], "color": spec["color"], "filter": fid}


# ----------------------------------------------------------------------------------------------
# computed-style walk
# ----------------------------------------------------------------------------------------------
def _walk(ctx: _Ctx, el, inherited: dict, ancestors: list, top=False, index_path=(),
          ctm: Affine2D = Affine2D.identity(), shadow: Optional[dict] = None):
    name = local(el.tag)
    if name in ("defs", "clipPath", "mask", "pattern", "linearGradient", "radialGradient", "filter"):
        _normalise_paint_server_colors(ctx, el)
        return
    own = {k: el.get(k).strip() for k in INHERITED if el.get(k) is not None and el.get(k).strip() != "inherit"}
    comp = {**inherited, **own}
    el_ctm = ctm if top else compose(_parse_transform(el.get("transform"), ctx.warnings), ctm)
    fid = el.get("data-bis-filter")
    if fid:
        shadow = _resolve_shadow(ctx, fid, el_ctm) or shadow
    if name in ("svg", "g"):
        groups = ancestors if top else ancestors + [_group_info(el, index_path)]
        for i, child in enumerate([c for c in el if isinstance(c.tag, str)]):
            _walk(ctx, child, comp, groups, index_path=index_path + (i,), ctm=el_ctm, shadow=shadow)
        for k in INHERITED:
            el.attrib.pop(k, None)
        return
    if name in SHAPE_TAGS:
        _process_shape(ctx, el, comp, ancestors, index_path, shadow)
    elif name == "image":
        _process_image(ctx, el, comp, ancestors, index_path, ctm, shadow)


def _normalise_paint_server_colors(ctx: _Ctx, el) -> None:
    for stop in el.iter(q("stop")):
        col_s = stop.get("stop-color", "#000000").strip()
        if col_s == "currentColor":
            col_s = ctx.opts.current_color
        col = parse_color(col_s) or (0, 0, 0, 1)
        stop.set("stop-color", to_hex(col))
        stop.set("stop-opacity", fmt(num(stop.get("stop-opacity"), 1.0) * col[3]))


def _parse_paint(ctx: _Ctx, value: str, comp: dict):
    """-> ('none',) | ('color', rgba) | ('url', id, fallback_rgba|None)"""
    v = (value or "none").strip()
    if v == "none":
        return ("none",)
    if v == "currentColor":
        v = comp.get("color", ctx.opts.current_color)
        if v == "currentColor":
            v = ctx.opts.current_color
    m = re.match(r"url\(\s*['\"]?#([^)'\"]+)['\"]?\s*\)\s*(.*)$", v)
    if m:
        fb = parse_color(m.group(2)) if m.group(2) and m.group(2) != "none" else None
        return ("url", m.group(1), fb)
    c = parse_color(v)
    if c is None:
        ctx.warnings.append(f"unparseable colour '{v}' rendered as black")
        c = (0, 0, 0, 1)
    return ("color", c)


def _geometry_only(el):
    g = etree.Element(el.tag)
    for k in GEOM_ATTRS[local(el.tag)]:
        if el.get(k) is not None:
            g.set(k, el.get(k))
    return g


def _local_bbox(el) -> Optional[Tuple[float, float, float, float]]:
    try:
        shape = pico_from_element(_geometry_only(el))
        cmds = list(shape.as_cmd_seq())
        if not cmds:
            return None
        return svg_pathops.skia_path(cmds, "nonzero").bounds
    except Exception:  # noqa: BLE001
        return None


def _resolve_gradient_chain(ctx: _Ctx, gid: str) -> list:
    chain, seen = [], set()
    el = ctx.by_id.get(gid)
    while el is not None and local(el.tag) in GRADIENT_TAGS and gid not in seen:
        seen.add(gid)
        chain.append(el)
        ref = _href(el) or ""
        gid = ref[1:] if ref.startswith("#") else None
        el = ctx.by_id.get(gid) if gid else None
    return chain


def _materialize_gradient(ctx: _Ctx, gid: str, el_shape, uid: str) -> Optional[str]:
    """Return the id of a self-contained userSpaceOnUse gradient (None -> caller falls back).

    picosvg leaves objectBoundingBox gradients untouched on untransformed shapes; after our
    stroke->fill conversion or a clip the bbox changes, which would silently move the gradient.
    Baking the bbox of the *original* geometry fixes that."""
    chain = _resolve_gradient_chain(ctx, gid)
    if not chain:
        return None
    tag = local(chain[0].tag)
    attrs = {}
    for g in reversed(chain):
        for k, v in g.attrib.items():
            if k in COMMON_GRADIENT_ATTRS or (local(g.tag) == tag and k in GRADIENT_ATTRS[tag]):
                attrs[k] = v
    stops = next((list(g.iter(q("stop"))) for g in chain if len(list(g.iter(q("stop"))))), [])
    if not stops:
        return None
    units = attrs.get("gradientUnits", "objectBoundingBox")
    gt = _parse_transform(attrs.get("gradientTransform"), ctx.warnings)
    vx, vy, vw, vh = ctx.vb
    diag = math.sqrt((vw * vw + vh * vh) / 2)
    defaults = {"x1": "0%", "y1": "0%", "x2": "100%", "y2": "0%", "cx": "50%", "cy": "50%", "r": "50%", "fr": "0%"}
    coords = {}
    for k in GRADIENT_ATTRS[tag]:
        raw = attrs.get(k, defaults.get(k))
        if raw is None:
            continue
        if units == "objectBoundingBox":
            coords[k] = num(raw, 0.0, pct_of=1.0)
        else:
            ref = vw if k in ("x1", "x2", "cx", "fx") else vh if k in ("y1", "y2", "cy", "fy") else diag
            coords[k] = num(raw, 0.0, pct_of=ref)
    if tag == "radialGradient":
        coords.setdefault("fx", coords["cx"])
        coords.setdefault("fy", coords["cy"])
    if units == "objectBoundingBox":
        bb = _local_bbox(el_shape)
        if bb is None or bb[2] - bb[0] <= 1e-9 or bb[3] - bb[1] <= 1e-9:
            return None  # spec: zero-size bbox -> paint server ignored
        bbox_m = Affine2D(bb[2] - bb[0], 0, 0, bb[3] - bb[1], bb[0], bb[1])
        gt = Affine2D.compose_ltr((gt, bbox_m))
    new = etree.SubElement(ctx.defs_el(), q(tag))
    new_id = f"{gid}__{uid}"
    new.set("id", new_id)
    for k, v in coords.items():
        new.set(k, fmt(v, 6))
    new.set("gradientUnits", "userSpaceOnUse")
    if gt != Affine2D.identity():
        new.set("gradientTransform", "matrix(" + " ".join(fmt(v, 6) for v in gt) + ")")
    if attrs.get("spreadMethod", "pad") != "pad":
        new.set("spreadMethod", attrs["spreadMethod"])
        ctx.warnings.append(f"gradient #{gid} uses spreadMethod={attrs['spreadMethod']} "
                            "(3D materials render it as 'pad')")
    last = 0.0
    for s in stops:
        col_s = s.get("stop-color", "#000000").strip()
        col = parse_color(ctx.opts.current_color if col_s == "currentColor" else col_s) or (0, 0, 0, 1)
        off = min(1.0, max(last, num(s.get("offset"), 0.0, pct_of=1.0)))
        last = off
        ns = etree.SubElement(new, q("stop"))
        ns.set("offset", fmt(off, 6))
        ns.set("stop-color", to_hex(col))
        op = num(s.get("stop-opacity"), 1.0) * col[3]
        if op < 1:
            ns.set("stop-opacity", fmt(op))
    ctx.by_id[new_id] = new
    return new_id


def _paint_attr(ctx: _Ctx, paint, el_shape, uid: str) -> Tuple[str, float]:
    """-> (svg paint attribute value, alpha multiplier)."""
    if paint[0] == "none":
        return "none", 0.0
    if paint[0] == "color":
        return to_hex(paint[1]), paint[1][3]
    target = ctx.by_id.get(paint[1])
    if target is not None and local(target.tag) in GRADIENT_TAGS:
        new_id = _materialize_gradient(ctx, paint[1], el_shape, uid)
        if new_id:
            return f"url(#{new_id})", 1.0
    fb = paint[2]
    if fb is None and target is not None:
        cols = [parse_color(c.get("fill") or c.get("stop-color") or "") for c in target.iter()
                if isinstance(c.tag, str) and (c.get("fill") or c.get("stop-color"))]
        cols = [c for c in cols if c]
        if cols:
            fb = tuple(sum(c[i] for c in cols) / len(cols) for i in range(4))
    kind = local(target.tag) if target is not None else "missing"
    ctx.warnings.append(f"paint url(#{paint[1]}) ({kind}) is not supported - solid fallback colour used")
    fb = fb or (0.5, 0.5, 0.5, 1.0)
    return to_hex(fb), fb[3]


_CAPS = {"butt": pathops.LineCap.BUTT_CAP, "round": pathops.LineCap.ROUND_CAP,
         "square": pathops.LineCap.SQUARE_CAP}
_JOINS = {"miter": pathops.LineJoin.MITER_JOIN, "miter-clip": pathops.LineJoin.MITER_JOIN,
          "arcs": pathops.LineJoin.MITER_JOIN, "round": pathops.LineJoin.ROUND_JOIN,
          "bevel": pathops.LineJoin.BEVEL_JOIN}


def stroke_outline_d(el, comp: dict, vb, scale_target: float) -> Optional[str]:
    """Accurate stroke -> fill.

    skia's stroker approximates offset curves with a fixed absolute tolerance (~0.1-0.25 units),
    ~6% radius error on a 24-unit Lucide icon. Stroking a copy scaled so width ~= scale_target and
    scaling back brings the error to < 0.001 units."""
    shape = pico_from_element(_geometry_only(el))
    cmds = list(shape.as_cmd_seq())
    if not cmds:
        return None
    diag = math.hypot(vb[2], vb[3]) / math.sqrt(2)
    width = num(comp.get("stroke-width"), 1.0, pct_of=diag)
    if width <= 0:
        return None
    F = max(1.0, scale_target / width)
    sk = svg_pathops.skia_path(cmds, "nonzero").transform(F, 0, 0, F, 0, 0)
    dash: List[float] = []
    if comp.get("stroke-dasharray", "none") not in ("none", ""):
        dash = [num(v, 0.0, pct_of=diag) * F for v in re.split(r"[\s,]+", comp["stroke-dasharray"].strip()) if v]
        if len(dash) % 2:
            dash *= 2
        if not any(dash) or any(d < 0 for d in dash):
            dash = []
    sk.stroke(width * F, _CAPS.get(comp.get("stroke-linecap", "butt"), pathops.LineCap.BUTT_CAP),
              _JOINS.get(comp.get("stroke-linejoin", "miter"), pathops.LineJoin.MITER_JOIN),
              num(comp.get("stroke-miterlimit"), 4.0), dash or None,
              num(comp.get("stroke-dashoffset"), 0.0) * F)
    sk.convertConicsToQuads(0.05)
    try:
        sk.simplify(fix_winding=True)
    except pathops.PathOpsError:
        pass
    sk = sk.transform(1 / F, 0, 0, 1 / F, 0, 0)
    d = SVGPath.from_commands(svg_pathops.svg_commands(sk)).d
    return d or None


_GEOM_AXIS = {"x": "w", "cx": "w", "x1": "w", "x2": "w", "width": "w", "rx": "w",
              "y": "h", "cy": "h", "y1": "h", "y2": "h", "height": "h", "ry": "h", "r": "d"}


def _normalize_geometry_attrs(el, vb) -> None:
    """picosvg does float(attr): resolve %, px/pt/mm units and rx/ry="auto" first."""
    vw, vh = vb[2], vb[3]
    ref = {"w": vw, "h": vh, "d": math.sqrt((vw * vw + vh * vh) / 2)}
    for k, axis in _GEOM_AXIS.items():
        v = el.get(k)
        if v is None or k not in GEOM_ATTRS.get(local(el.tag), ()):
            continue
        if v.strip() == "auto":
            del el.attrib[k]
            continue
        el.set(k, fmt(num(v, 0.0, pct_of=ref[axis]), 6))


def _base_meta(ctx: _Ctx, el, uid: str, comp: dict, ancestors: list, index_path, shadow) -> dict:
    use_of = next((a["use_of"] for a in reversed(ancestors) if a.get("use_of")), None)
    return {
        "uid": uid, "orig_id": el.get("id"), "tag": local(el.tag), "ancestors": ancestors,
        "index_path": list(index_path), "use_of": use_of, "doc_order": ctx.n + ctx.n_img,
        "classes": (el.get("class") or "").split(),
        "current_color": "currentColor" in (comp.get("fill", ""), comp.get("stroke", "")),
        "shadow": shadow,
    }


def _process_shape(ctx: _Ctx, el, comp: dict, ancestors: list, index_path, shadow) -> None:
    parent = el.getparent()
    _normalize_geometry_attrs(el, ctx.vb)
    if comp.get("visibility") in ("hidden", "collapse"):
        parent.remove(el)
        return
    ctx.n += 1
    uid = f"e{ctx.n}"
    base_meta = _base_meta(ctx, el, uid, comp, ancestors, index_path, shadow)
    fill = _parse_paint(ctx, comp.get("fill"), comp)
    stroke = _parse_paint(ctx, comp.get("stroke"), comp)
    fill_attr, fill_a = _paint_attr(ctx, fill, el, uid)
    fill_op = num(comp.get("fill-opacity"), 1.0) * fill_a
    stroke_attr, stroke_a = ("none", 0.0)
    if stroke[0] != "none":
        stroke_attr, stroke_a = _paint_attr(ctx, stroke, el, uid + "s")
    stroke_op = num(comp.get("stroke-opacity"), 1.0) * stroke_a
    paints_fill = fill_attr != "none" and fill_op > 0 and local(el.tag) != "line"
    paints_stroke = stroke_attr != "none" and stroke_op > 0 and num(comp.get("stroke-width"), 1.0) > 0

    el.set("id", uid)
    el.set("fill", fill_attr if paints_fill else "none")
    if paints_fill and fill_op < 1:
        el.set("fill-opacity", fmt(fill_op))
    else:
        el.attrib.pop("fill-opacity", None)
    el.set("fill-rule", comp.get("fill-rule", "nonzero"))
    el.set("stroke", "none")
    ctx.meta[uid] = {**base_meta, "role": "fill", "base": uid, "fill_source": comp.get("fill")}

    outline = None
    if paints_stroke:
        try:
            d = stroke_outline_d(el, comp, ctx.vb, ctx.opts.stroke_scale_target)
        except Exception as ex:  # noqa: BLE001
            ctx.warnings.append(f"stroke of {base_meta['orig_id'] or uid} could not be outlined: {ex}")
            d = None
        if d:
            outline = etree.Element(q("path"))
            outline.set("id", uid + "s")
            outline.set("d", d)
            outline.set("fill", stroke_attr)
            if stroke_op < 1:
                outline.set("fill-opacity", fmt(stroke_op))
            outline.set("fill-rule", "nonzero")
            for k in ("transform", "clip-path"):
                if el.get(k):
                    outline.set(k, el.get(k))
            ctx.meta[uid + "s"] = {**base_meta, "uid": uid + "s", "role": "stroke", "base": uid,
                                   "stroke_width": num(comp.get("stroke-width"), 1.0),
                                   "fill_source": comp.get("stroke")}
    if outline is None:
        if not paints_fill:
            parent.remove(el)
            ctx.meta.pop(uid, None)
        return
    stroke_first = comp.get("paint-order", "normal").strip().startswith("stroke")
    if not paints_fill:
        if el.get("opacity"):
            outline.set("opacity", el.get("opacity"))
        parent.replace(el, outline)
        ctx.meta.pop(uid, None)
        return
    op = el.get("opacity")
    if op is not None and num(op, 1.0) < 1:
        # element opacity applies to fill+stroke composited together -> keep them in one group
        wrapper = etree.Element(q("g"))
        wrapper.set("opacity", op)
        del el.attrib["opacity"]
        parent.replace(el, wrapper)
        for child in ((outline, el) if stroke_first else (el, outline)):
            wrapper.append(child)
    else:
        idx = parent.index(el)
        parent.insert(idx if stroke_first else idx + 1, outline)


# ----------------------------------------------------------------------------------------------
# raster images
# ----------------------------------------------------------------------------------------------
def decode_data_uri(href: str) -> Optional[Tuple[bytes, str]]:
    m = re.match(r"\s*data:([^;,]*)((?:;[^;,]*)*),(.*)$", href, re.S)
    if not m:
        return None
    mime = (m.group(1) or "text/plain").strip().lower()
    payload = m.group(3)
    try:
        if ";base64" in m.group(2).lower():
            data = base64.b64decode(re.sub(r"\s+", "", payload) + "===", validate=False)
        else:
            data = urllib.parse.unquote_to_bytes(payload)
    except Exception:  # noqa: BLE001
        return None
    return data, mime


def _load_raster(data: bytes, mime: str) -> Optional[Tuple[bytes, str, int, int]]:
    """-> (encoded PNG/JPEG bytes, mime, width, height)."""
    from PIL import Image

    if "svg" in mime:
        import resvg_py
        try:
            png = bytes(resvg_py.svg_to_bytes(svg_string=data.decode("utf-8", "replace"), width=1024,
                                              skip_system_fonts=True))
        except Exception:  # noqa: BLE001
            return None
        data, mime = png, "image/png"
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:  # noqa: BLE001
        return None
    fmt_name = (im.format or "").upper()
    if fmt_name == "PNG":
        return data, "image/png", im.width, im.height
    if fmt_name == "JPEG":
        return data, "image/jpeg", im.width, im.height
    buf = io.BytesIO()
    im.convert("RGBA").save(buf, "PNG")
    return buf.getvalue(), "image/png", im.width, im.height


def _process_image(ctx: _Ctx, el, comp: dict, ancestors: list, index_path, parent_ctm: Affine2D,
                   shadow) -> None:
    parent = el.getparent()
    href = _href(el) or ""
    if comp.get("visibility") in ("hidden", "collapse") or num(el.get("opacity"), 1.0) <= 0:
        parent.remove(el)
        return
    decoded = decode_data_uri(href) if href.strip().startswith("data:") else None
    loaded = _load_raster(*decoded) if decoded else None
    if loaded is None:
        what = "external image reference" if not href.strip().startswith("data:") else "undecodable image data"
        ctx.warnings.append(f"<image> with {what} dropped (embed images as base64 PNG/JPEG)")
        parent.remove(el)
        return
    data, mime, W, H = loaded
    vw, vh = ctx.vb[2], ctx.vb[3]
    x, y = num(el.get("x"), 0.0, vw), num(el.get("y"), 0.0, vh)
    w = num(el.get("width"), float(W), vw) if el.get("width") not in (None, "auto") else float(W)
    h = num(el.get("height"), float(H), vh) if el.get("height") not in (None, "auto") else float(H)
    if w <= 0 or h <= 0:
        parent.remove(el)
        return
    sx, sy, tx, ty = _par_mapping((0.0, 0.0, float(W), float(H)), w, h, el.get("preserveAspectRatio"))
    # content rect (local coords) and visible part of it (content ∩ viewport)
    cx0, cy0 = x + tx, y + ty
    cx1, cy1 = cx0 + W * sx, cy0 + H * sy
    vx0, vy0, vx1, vy1 = max(cx0, x), max(cy0, y), min(cx1, x + w), min(cy1, y + h)
    if vx1 - vx0 <= 1e-9 or vy1 - vy0 <= 1e-9:
        parent.remove(el)
        return
    own = _parse_transform(el.get("transform"), ctx.warnings)
    pixel_map = Affine2D(sx, 0.0, 0.0, sy, cx0, cy0)
    matrix = compose(pixel_map, own, parent_ctm)

    ctx.n_img += 1
    uid = f"i{ctx.n_img}"
    rect = etree.Element(q("rect"))
    rect.set("id", uid)
    for k, v in (("x", vx0), ("y", vy0), ("width", vx1 - vx0), ("height", vy1 - vy0)):
        rect.set(k, fmt(v, 6))
    rect.set("fill", IMAGE_MARKER)
    for k in ("transform", "clip-path", "opacity"):
        if el.get(k):
            rect.set(k, el.get(k))
    parent.replace(el, rect)
    ctx.images[uid] = ImageRef(uid, data, mime, W, H, affine_tuple(matrix))
    meta = _base_meta(ctx, el, uid, comp, ancestors, index_path, shadow)
    ctx.meta[uid] = {**meta, "role": "image", "base": uid, "tag": "image"}
