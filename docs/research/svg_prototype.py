"""
Blender Icon Studio - SVG layer-split pipeline (research prototype).

    SVG text
      -> prepass()        lxml: CSS inlining, <use>/<symbol> expansion, currentColor + color
                          normalisation, objectBoundingBox gradients -> userSpaceOnUse, accurate
                          stroke->fill (pre-scaled skia stroker), provenance ids, unsupported
                          feature stripping (with warnings)
      -> picosvg.topicosvg()  transforms applied, shapes->paths, clip-paths applied (boolean),
                          evenodd->nonzero, groups flattened (only <g opacity> kept)
      -> extract_elements()  one Element per painted path, provenance metadata re-attached by id,
                          skia-pathops simplified geometry (+ shapely polygon for analysis)
      -> split_layers()    strategies: "group" | "element" | "color" | "smart"
      -> build_layers()    per layer: standalone SVG (same viewBox), silhouette + occlusion-cut
                          colour regions as cubic bezier splines in normalised icon space

Dependencies (verified on Python 3.13 / Windows, see svg-pipeline.md):
    pip install picosvg skia-pathops shapely        (lxml comes with picosvg)
Optional: resvg-py (thumbnails / regression diffs), pillow, numpy.

CLI:  python svg_prototype.py icon.svg [--strategy smart] [--out outdir]
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pathops
import shapely
from lxml import etree
from picosvg import svg_pathops
from picosvg.svg import SVG
from picosvg.svg import from_element as pico_from_element
from picosvg.svg_transform import Affine2D
from picosvg.svg_types import SVGPath
from shapely.geometry import Point, Polygon

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
XLINK_HREF = f"{{{XLINK_NS}}}href"

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
                                 "marker-mid", "marker-end", "vector-effect"}
UNSUPPORTED_ELEMENTS = {"text", "image", "foreignObject", "marker", "script", "audio", "video",
                        "canvas", "iframe", "animate", "animateTransform", "animateMotion", "set"}
DROP_SILENTLY = {"title", "desc", "metadata", "style"}

# ----------------------------------------------------------------------------------------------
# small utils
# ----------------------------------------------------------------------------------------------


def local(tag) -> str:
    return tag.split("}", 1)[-1] if isinstance(tag, str) else ""


def ns_of(tag) -> str:
    return tag[1:].split("}", 1)[0] if isinstance(tag, str) and tag.startswith("{") else ""


def q(tag: str) -> str:
    return f"{{{SVG_NS}}}{tag}"


def _num(s, default=0.0, pct_of=None) -> float:
    if s is None:
        return default
    s = str(s).strip()
    m = re.match(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*(%|px|pt|mm|cm|in|em)?$", s)
    if not m:
        return default
    v = float(m.group(1))
    unit = m.group(2)
    if unit == "%":
        return v / 100.0 * (pct_of if pct_of is not None else 1.0)
    return v * {"pt": 1.25, "mm": 3.7795, "cm": 37.795, "in": 96.0, "em": 16.0}.get(unit, 1.0)


def _fmt(v: float, nd: int = 4) -> str:
    s = f"{v:.{nd}f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


# ----------------------------------------------------------------------------------------------
# colours (CSS Color 3/4 subset). svgelements.Color silently returns black for
# "rgb(255 0 0 / 50%)" style input, so we parse ourselves.
# ----------------------------------------------------------------------------------------------
_NAMED = dict(
    (k, v) for k, v in (x.split(":") for x in (
        "aliceblue:f0f8ff antiquewhite:faebd7 aqua:00ffff aquamarine:7fffd4 azure:f0ffff beige:f5f5dc "
        "bisque:ffe4c4 black:000000 blanchedalmond:ffebcd blue:0000ff blueviolet:8a2be2 brown:a52a2a "
        "burlywood:deb887 cadetblue:5f9ea0 chartreuse:7fff00 chocolate:d2691e coral:ff7f50 "
        "cornflowerblue:6495ed cornsilk:fff8dc crimson:dc143c cyan:00ffff darkblue:00008b darkcyan:008b8b "
        "darkgoldenrod:b8860b darkgray:a9a9a9 darkgreen:006400 darkgrey:a9a9a9 darkkhaki:bdb76b "
        "darkmagenta:8b008b darkolivegreen:556b2f darkorange:ff8c00 darkorchid:9932cc darkred:8b0000 "
        "darksalmon:e9967a darkseagreen:8fbc8f darkslateblue:483d8b darkslategray:2f4f4f "
        "darkslategrey:2f4f4f darkturquoise:00ced1 darkviolet:9400d3 deeppink:ff1493 deepskyblue:00bfff "
        "dimgray:696969 dimgrey:696969 dodgerblue:1e90ff firebrick:b22222 floralwhite:fffaf0 "
        "forestgreen:228b22 fuchsia:ff00ff gainsboro:dcdcdc ghostwhite:f8f8ff gold:ffd700 "
        "goldenrod:daa520 gray:808080 green:008000 greenyellow:adff2f grey:808080 honeydew:f0fff0 "
        "hotpink:ff69b4 indianred:cd5c5c indigo:4b0082 ivory:fffff0 khaki:f0e68c lavender:e6e6fa "
        "lavenderblush:fff0f5 lawngreen:7cfc00 lemonchiffon:fffacd lightblue:add8e6 lightcoral:f08080 "
        "lightcyan:e0ffff lightgoldenrodyellow:fafad2 lightgray:d3d3d3 lightgreen:90ee90 "
        "lightgrey:d3d3d3 lightpink:ffb6c1 lightsalmon:ffa07a lightseagreen:20b2aa lightskyblue:87cefa "
        "lightslategray:778899 lightslategrey:778899 lightsteelblue:b0c4de lightyellow:ffffe0 lime:00ff00 "
        "limegreen:32cd32 linen:faf0e6 magenta:ff00ff maroon:800000 mediumaquamarine:66cdaa "
        "mediumblue:0000cd mediumorchid:ba55d3 mediumpurple:9370db mediumseagreen:3cb371 "
        "mediumslateblue:7b68ee mediumspringgreen:00fa9a mediumturquoise:48d1cc mediumvioletred:c71585 "
        "midnightblue:191970 mintcream:f5fffa mistyrose:ffe4e1 moccasin:ffe4b5 navajowhite:ffdead "
        "navy:000080 oldlace:fdf5e6 olive:808000 olivedrab:6b8e23 orange:ffa500 orangered:ff4500 "
        "orchid:da70d6 palegoldenrod:eee8aa palegreen:98fb98 paleturquoise:afeeee palevioletred:db7093 "
        "papayawhip:ffefd5 peachpuff:ffdab9 peru:cd853f pink:ffc0cb plum:dda0dd powderblue:b0e0e6 "
        "purple:800080 rebeccapurple:663399 red:ff0000 rosybrown:bc8f8f royalblue:4169e1 "
        "saddlebrown:8b4513 salmon:fa8072 sandybrown:f4a460 seagreen:2e8b57 seashell:fff5ee "
        "sienna:a0522d silver:c0c0c0 skyblue:87ceeb slateblue:6a5acd slategray:708090 slategrey:708090 "
        "snow:fffafa springgreen:00ff7f steelblue:4682b4 tan:d2b48c teal:008080 thistle:d8bfd8 "
        "tomato:ff6347 turquoise:40e0d0 violet:ee82ee wheat:f5deb3 white:ffffff whitesmoke:f5f5f5 "
        "yellow:ffff00 yellowgreen:9acd32").split())
)


def parse_color(s: str) -> Optional[Tuple[float, float, float, float]]:
    """CSS colour -> (r, g, b, a) floats in 0..1 (sRGB). None if unparseable."""
    if s is None:
        return None
    s = s.strip().lower()
    if s == "transparent":
        return (0.0, 0.0, 0.0, 0.0)
    if s in _NAMED:
        s = "#" + _NAMED[s]
    if s.startswith("#"):
        h = s[1:]
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        if len(h) in (6, 8) and re.fullmatch(r"[0-9a-f]+", h):
            vals = [int(h[i:i + 2], 16) / 255.0 for i in range(0, len(h), 2)]
            return (vals[0], vals[1], vals[2], vals[3] if len(vals) == 4 else 1.0)
        return None
    m = re.fullmatch(r"(rgba?|hsla?)\((.*)\)", s)
    if not m:
        return None
    fn, body = m.group(1), m.group(2)
    parts = [p for p in re.split(r"[\s,/]+", body.strip()) if p]
    if len(parts) not in (3, 4):
        return None

    def comp(p, scale):
        return float(p[:-1]) / 100.0 if p.endswith("%") else float(p) / scale

    try:
        a = comp(parts[3], 1.0) if len(parts) == 4 else 1.0
        if fn.startswith("rgb"):
            r, g, b = (comp(p, 255.0) for p in parts[:3])
        else:
            hue = float(re.sub(r"deg$", "", parts[0])) % 360 / 360.0
            sat, lig = comp(parts[1], 100.0), comp(parts[2], 100.0)
            import colorsys
            r, g, b = colorsys.hls_to_rgb(hue, lig, sat)
    except ValueError:
        return None
    clamp = lambda v: min(1.0, max(0.0, v))  # noqa: E731
    return (clamp(r), clamp(g), clamp(b), clamp(a))


def to_hex(rgb) -> str:
    return "#" + "".join(f"{int(round(c * 255)):02X}" for c in rgb[:3])


def srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rgb_to_lab(rgb) -> Tuple[float, float, float]:
    r, g, b = (srgb_to_linear(c) for c in rgb[:3])
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
    f = lambda t: t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116  # noqa: E731
    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e(c1, c2) -> float:
    a, b = rgb_to_lab(c1), rgb_to_lab(c2)
    return math.sqrt(sum((p - q_) ** 2 for p, q_ in zip(a, b)))


# ----------------------------------------------------------------------------------------------
# minimal CSS (<style>) support: type / .class / #id compounds + descendant combinator
# ----------------------------------------------------------------------------------------------


def parse_decls(text: str) -> Dict[str, str]:
    out = {}
    for part in (text or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            v = v.replace("!important", "").strip()
            if k.strip() and v:
                out[k.strip().lower()] = v
    return out


def _strip_at_rules(css: str) -> str:
    out, i = [], 0
    while i < len(css):
        if css[i] == "@":
            j = i
            while j < len(css) and css[j] not in "{;":
                j += 1
            if j < len(css) and css[j] == "{":
                depth, j = 1, j + 1
                while j < len(css) and depth:
                    depth += {"{": 1, "}": -1}.get(css[j], 0)
                    j += 1
            i = j + 1
            continue
        out.append(css[i])
        i += 1
    return "".join(out)


_COMPOUND = re.compile(r"^(\*|[A-Za-z][\w-]*)?((?:[.#][\w-]+)*)$")


def parse_stylesheet(css: str, warnings: List[str]):
    css = _strip_at_rules(re.sub(r"/\*.*?\*/", "", css, flags=re.S))
    rules = []
    for order, m in enumerate(re.finditer(r"([^{}]+)\{([^{}]*)\}", css)):
        decls = parse_decls(m.group(2))
        for sel in m.group(1).split(","):
            sel = sel.strip()
            if not sel:
                continue
            compounds = []
            ok = True
            for tok in sel.replace(">", " ").split():
                mm = _COMPOUND.match(tok)
                if not mm:
                    ok = False
                    break
                tag = mm.group(1) if mm.group(1) not in (None, "*") else None
                classes = re.findall(r"\.([\w-]+)", mm.group(2))
                ids = re.findall(r"#([\w-]+)", mm.group(2))
                compounds.append((tag, classes, ids[0] if ids else None))
            if not ok:
                warnings.append(f"css: unsupported selector '{sel}' ignored")
                continue
            spec = (sum(1 for c in compounds if c[2]), sum(len(c[1]) for c in compounds),
                    sum(1 for c in compounds if c[0]))
            rules.append((compounds, spec, order, decls))
    rules.sort(key=lambda r: (r[1], r[2]))
    return rules


def _compound_matches(el, comp) -> bool:
    tag, classes, el_id = comp
    if tag and local(el.tag) != tag:
        return False
    if el_id and el.get("id") != el_id:
        return False
    if classes:
        have = set((el.get("class") or "").split())
        if not set(classes) <= have:
            return False
    return True


def _selector_matches(el, compounds) -> bool:
    if not _compound_matches(el, compounds[-1]):
        return False
    anc = el.getparent()
    for comp in reversed(compounds[:-1]):
        while anc is not None and not _compound_matches(anc, comp):
            anc = anc.getparent()
        if anc is None:
            return False
        anc = anc.getparent()
    return True


# ----------------------------------------------------------------------------------------------
# prepass
# ----------------------------------------------------------------------------------------------


@dataclass
class Options:
    current_color: str = "#000000"     # what `currentColor` resolves to (line icons)
    stroke_scale_target: float = 256.0  # stroke geometry is scaled so width ~ this before stroking
    ndigits: int = 4                    # picosvg float rounding
    curve_tolerance_pct: float = 0.05   # flattening tolerance for analysis polygons, % of vb diag


@dataclass
class Prepass:
    svg_text: str
    view_box: Tuple[float, float, float, float]
    meta: Dict[str, dict]
    warnings: List[str]


def _secure_parser():
    return etree.XMLParser(resolve_entities=False, no_network=True, remove_comments=True,
                           remove_pis=True, huge_tree=False, recover=False)


def _href(el) -> Optional[str]:
    return el.get(XLINK_HREF) or el.get("href")


def _viewbox_transform(vb, w, h, par="xMidYMid meet") -> str:
    vx, vy, vw, vh = vb
    if vw <= 0 or vh <= 0:
        return ""
    sx, sy = w / vw, h / vh
    par = (par or "xMidYMid meet").split()
    align = par[0]
    if align != "none":
        s = min(sx, sy) if (len(par) < 2 or par[1] == "meet") else max(sx, sy)
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
    return f"matrix({_fmt(sx, 6)} 0 0 {_fmt(sy, 6)} {_fmt(tx, 6)} {_fmt(ty, 6)})"


def _parse_viewbox(root) -> Optional[Tuple[float, float, float, float]]:
    vb = root.get("viewBox")
    if vb:
        vals = [float(v) for v in re.split(r"[\s,]+", vb.strip()) if v]
        if len(vals) == 4 and vals[2] > 0 and vals[3] > 0:
            return tuple(vals)
    w, h = _num(root.get("width"), 0), _num(root.get("height"), 0)
    if w > 0 and h > 0:
        return (0.0, 0.0, w, h)
    return None


def _auto_name(name: Optional[str]) -> bool:
    """True for generator noise: g12, path3, Layer_1, clip0_12_34, Group 5, Vector ..."""
    if not name:
        return True
    return bool(re.fullmatch(
        r"(g|path|rect|circle|ellipse|layer|group|svg|clip|clippath|mask|vector|shape|frame|union|"
        r"subtract|use|defs)[\s_-]*[\d_]*", name.strip().lower()))


class _Ctx:
    def __init__(self, root, vb, opts, warnings):
        self.root, self.vb, self.opts, self.warnings = root, vb, opts, warnings
        self.meta: Dict[str, dict] = {}
        self.n = 0
        self.defs = None
        self.by_id = {}
        self.grad_cache = {}

    def defs_el(self):
        if self.defs is None:
            self.defs = etree.Element(q("defs"))
            self.root.insert(0, self.defs)
        return self.defs


def prepass(svg_text: str, opts: Optional[Options] = None) -> Prepass:
    opts = opts or Options()
    warnings: List[str] = []
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", svg_text.lstrip("﻿"))
    text = re.sub(r"<!DOCTYPE[^>\[]*(\[[^\]]*\])?\s*>", "", text, flags=re.S)
    root = etree.fromstring(text.encode("utf-8"), _secure_parser())
    if local(root.tag) != "svg":
        raise ValueError("root element is not <svg>")
    vb = _parse_viewbox(root)
    if vb is None:
        warnings.append("no viewBox/width/height: assuming 0 0 100 100")
        vb = (0.0, 0.0, 100.0, 100.0)

    # 1. drop non-SVG namespaced elements (sodipodi:namedview, metadata, ...), title/desc
    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue
        if el is not root and (ns_of(el.tag) not in ("", SVG_NS)):
            if el.getparent() is not None:
                el.getparent().remove(el)

    # 2. CSS: presentation attr < stylesheet rule (by specificity) < inline style
    css = "\n".join((el.text or "") for el in root.iter(q("style")))
    rules = parse_stylesheet(css, warnings) if css.strip() else []
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        decls = {}
        for compounds, _spec, _order, d in rules:
            if _selector_matches(el, compounds):
                decls.update(d)
        decls.update(parse_decls(el.get("style", "")))
        if "style" in el.attrib:
            del el.attrib["style"]
        for k, v in decls.items():
            if k in PRESENTATION or k in ("display",):
                el.set(k, v)
            elif k in ("mix-blend-mode", "isolation") and v not in ("normal", "auto"):
                warnings.append(f"css: {k}:{v} ignored")
    for el in list(root.iter(q("style"))):
        el.getparent().remove(el)

    # 3. remove hidden subtrees
    for el in list(root.iter()):
        if isinstance(el.tag, str) and el.get("display", "").strip() == "none":
            if el.getparent() is not None and local(el.getparent().tag) not in ("defs",):
                el.getparent().remove(el)

    # 4. <a>/<switch> behave like <g>
    for el in root.iter():
        if local(el.tag) in ("a", "switch"):
            el.tag = q("g")

    ctx = _Ctx(root, vb, opts, warnings)
    ctx.by_id = {el.get("id"): el for el in root.iter() if isinstance(el.tag, str) and el.get("id")}

    # 5. expand <use> (incl. <symbol> and nested <svg> targets) - picosvg rejects SVG2 `href`
    #    and drops ids of instantiated content, so we do it here and record provenance.
    for _ in range(16):
        uses = [el for el in root.iter(q("use"))]
        if not uses:
            break
        for use in uses:
            ref = _href(use) or ""
            target = ctx.by_id.get(ref[1:]) if ref.startswith("#") else None
            parent = use.getparent()
            if target is None or parent is None:
                warnings.append(f"<use> to missing/external target '{ref}' dropped")
                if parent is not None:
                    parent.remove(use)
                continue
            g = etree.Element(q("g"))
            for k, v in use.attrib.items():
                if k not in ("x", "y", "width", "height", "href", XLINK_HREF, "transform"):
                    g.set(k, v)
            tf = use.get("transform", "")
            x, y = _num(use.get("x")), _num(use.get("y"))
            if x or y:
                tf += f" translate({_fmt(x, 6)} {_fmt(y, 6)})"
            clone = copy.deepcopy(target)
            for sub in clone.iter():
                if isinstance(sub.tag, str) and "id" in sub.attrib:
                    del sub.attrib["id"]
            if local(clone.tag) in ("symbol", "svg"):
                svb = None
                if clone.get("viewBox"):
                    svb = tuple(float(v) for v in re.split(r"[\s,]+", clone.get("viewBox").strip()) if v)
                w = _num(use.get("width") or clone.get("width"), svb[2] if svb else 0)
                h = _num(use.get("height") or clone.get("height"), svb[3] if svb else 0)
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
    for el in list(root.iter(q("symbol"))):
        el.getparent().remove(el)

    # 6. unsupported elements / attributes
    for el in list(root.iter()):
        if not isinstance(el.tag, str) or el.getparent() is None:
            continue
        name = local(el.tag)
        if name in DROP_SILENTLY:
            el.getparent().remove(el)
        elif name in UNSUPPORTED_ELEMENTS:
            warnings.append(f"<{name}> not supported - dropped" +
                            (" (convert text to outlines before import)" if name == "text" else ""))
            el.getparent().remove(el)
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for attr in ("mask", "filter", "marker-start", "marker-mid", "marker-end"):
            if el.get(attr) and el.get(attr) != "none":
                warnings.append(f"{attr}={el.get(attr)} on <{local(el.tag)} id={el.get('id')}> ignored")
                del el.attrib[attr]
        if el.get("vector-effect") == "non-scaling-stroke":
            warnings.append("vector-effect=non-scaling-stroke treated as normal stroke")

    # 7. walk render tree: compute inherited style, resolve currentColor/colours/gradients,
    #    split fill+stroke, assign provenance ids
    _walk(ctx, root, {
        "fill": "#000000", "fill-opacity": "1", "fill-rule": "nonzero", "stroke": "none",
        "stroke-width": "1", "stroke-linecap": "butt", "stroke-linejoin": "miter",
        "stroke-miterlimit": "4", "stroke-dasharray": "none", "stroke-dashoffset": "0",
        "stroke-opacity": "1", "color": opts.current_color, "paint-order": "normal",
        "visibility": "visible", "clip-rule": "nonzero",
    }, [], top=True)

    # clipPath children: only geometry, clip-rule and transform matter
    for cp in root.iter(q("clipPath")):
        if cp.get("clipPathUnits") == "objectBoundingBox":
            warnings.append("clipPathUnits=objectBoundingBox not supported by picosvg")
        for el in cp.iter():
            if local(el.tag) in SHAPE_TAGS:
                _normalize_geometry_attrs(el, vb)
                for k in list(el.attrib):
                    if k not in GEOM_ATTRS[local(el.tag)] | {"transform", "clip-rule", "clip-path"}:
                        del el.attrib[k]

    # 8. strip everything picosvg does not understand
    _strip_attributes(root)

    # 9. rebuild root with a clean nsmap (svg default + xlink for gradient templates)
    new_root = etree.Element(q("svg"), nsmap={None: SVG_NS, "xlink": XLINK_NS})
    new_root.set("viewBox", " ".join(_fmt(v, 6) for v in vb))
    new_root.set("width", _fmt(vb[2], 6))
    new_root.set("height", _fmt(vb[3], 6))
    for child in list(root):
        new_root.append(child)
    out = etree.tostring(new_root, encoding="unicode")
    return Prepass(out, vb, ctx.meta, warnings)


def _strip_attributes(root):
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
        "auto_name": _auto_name(name) and not label,
        "inkscape_layer": el.get(f"{{{INKSCAPE_NS}}}groupmode") == "layer",
        "use_of": el.get("data-bis-use"),
        "opacity": _num(el.get("opacity"), 1.0),
        "index_path": list(index_path),
    }


def _walk(ctx: _Ctx, el, inherited: dict, ancestors: list, top=False, index_path=()):
    name = local(el.tag)
    if name in ("defs", "clipPath", "mask", "pattern", "linearGradient", "radialGradient"):
        _normalise_paint_server_colors(ctx, el)
        return
    own = {k: el.get(k).strip() for k in INHERITED if el.get(k) is not None and el.get(k).strip() != "inherit"}
    comp = {**inherited, **own}
    if name in ("svg", "g"):
        groups = ancestors if top else ancestors + [_group_info(el, index_path)]
        for i, child in enumerate([c for c in el if isinstance(c.tag, str)]):
            _walk(ctx, child, comp, groups, index_path=index_path + (i,))
        # inherited paint props are now explicit on every shape -> drop from container
        for k in INHERITED:
            el.attrib.pop(k, None)
        return
    if name in SHAPE_TAGS:
        _process_shape(ctx, el, comp, ancestors, index_path)


def _normalise_paint_server_colors(ctx, el):
    for stop in el.iter(q("stop")):
        col_s = stop.get("stop-color", "#000000").strip()
        if col_s == "currentColor":
            col_s = ctx.opts.current_color
        col = parse_color(col_s) or (0, 0, 0, 1)
        stop.set("stop-color", to_hex(col))
        stop.set("stop-opacity", _fmt(_num(stop.get("stop-opacity"), 1.0) * col[3]))


def _parse_paint(ctx, value: str, comp: dict):
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
        ctx.warnings.append(f"unparseable colour '{v}' -> black")
        c = (0, 0, 0, 1)
    return ("color", c)


def _local_bbox(el) -> Optional[Tuple[float, float, float, float]]:
    try:
        shape = pico_from_element(_geometry_only(el))
        cmds = list(shape.as_cmd_seq())
        if not cmds:
            return None
        sk = svg_pathops.skia_path(cmds, "nonzero")
        return sk.bounds  # (xMin, yMin, xMax, yMax)
    except Exception:  # noqa: BLE001
        return None


def _geometry_only(el):
    g = etree.Element(el.tag)
    for k in GEOM_ATTRS[local(el.tag)]:
        if el.get(k) is not None:
            g.set(k, el.get(k))
    return g


def _resolve_gradient_chain(ctx, gid):
    chain, seen = [], set()
    el = ctx.by_id.get(gid)
    while el is not None and local(el.tag) in GRADIENT_TAGS and gid not in seen:
        seen.add(gid)
        chain.append(el)
        ref = _href(el) or ""
        gid = ref[1:] if ref.startswith("#") else None
        el = ctx.by_id.get(gid) if gid else None
    return chain


def _materialize_gradient(ctx, gid: str, el_shape, uid: str):
    """Return id of a self-contained userSpaceOnUse gradient (or None -> caller falls back).

    picosvg leaves objectBoundingBox gradients untouched on untransformed shapes; after our
    stroke->fill conversion or after clip-path application the bbox changes, which would
    silently move the gradient. Baking the bbox of the *original* geometry fixes that."""
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
    units = attrs.get("gradientUnits", "objectBoundingBox")
    gt = Affine2D.fromstring(attrs.get("gradientTransform", "")) if attrs.get("gradientTransform") else Affine2D.identity()
    vx, vy, vw, vh = ctx.vb
    diag = math.sqrt((vw * vw + vh * vh) / 2)
    defaults = {"x1": "0%", "y1": "0%", "x2": "100%", "y2": "0%", "cx": "50%", "cy": "50%", "r": "50%", "fr": "0%"}
    coords = {}
    for k in GRADIENT_ATTRS[tag]:
        raw = attrs.get(k, defaults.get(k))
        if raw is None:
            continue
        if units == "objectBoundingBox":
            coords[k] = _num(raw, 0.0, pct_of=1.0)
        else:
            ref = vw if k in ("x1", "x2", "cx", "fx") else vh if k in ("y1", "y2", "cy", "fy") else diag
            coords[k] = _num(raw, 0.0, pct_of=ref)
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
        new.set(k, _fmt(v, 6))
    new.set("gradientUnits", "userSpaceOnUse")
    if gt != Affine2D.identity():
        new.set("gradientTransform", "matrix(" + " ".join(_fmt(v, 6) for v in gt) + ")")
    if attrs.get("spreadMethod", "pad") != "pad":
        new.set("spreadMethod", attrs["spreadMethod"])
        ctx.warnings.append(f"gradient {gid}: spreadMethod={attrs['spreadMethod']} (renderer support varies)")
    last = 0.0
    for s in stops:
        col_s = s.get("stop-color", "#000000").strip()
        col = parse_color(ctx.opts.current_color if col_s == "currentColor" else col_s) or (0, 0, 0, 1)
        off = min(1.0, max(last, _num(s.get("offset"), 0.0, pct_of=1.0)))
        last = off
        ns = etree.SubElement(new, q("stop"))
        ns.set("offset", _fmt(off, 6))
        ns.set("stop-color", to_hex(col))
        op = _num(s.get("stop-opacity"), 1.0) * col[3]
        if op < 1:
            ns.set("stop-opacity", _fmt(op))
    ctx.by_id[new_id] = new
    return new_id


def _paint_attr(ctx, paint, el_shape, uid, comp) -> Tuple[str, float]:
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
    # pattern / missing / degenerate -> fallback colour or average of stops
    fb = paint[2]
    if fb is None and target is not None:
        cols = [parse_color(c.get("fill") or c.get("stop-color") or "") for c in target.iter()
                if isinstance(c.tag, str) and (c.get("fill") or c.get("stop-color"))]
        cols = [c for c in cols if c]
        if cols:
            fb = tuple(sum(c[i] for c in cols) / len(cols) for i in range(4))
    ctx.warnings.append(f"paint url(#{paint[1]}) unsupported/degenerate -> solid fallback")
    fb = fb or (0.5, 0.5, 0.5, 1.0)
    return to_hex(fb), fb[3]


_CAPS = {"butt": pathops.LineCap.BUTT_CAP, "round": pathops.LineCap.ROUND_CAP,
         "square": pathops.LineCap.SQUARE_CAP}
_JOINS = {"miter": pathops.LineJoin.MITER_JOIN, "miter-clip": pathops.LineJoin.MITER_JOIN,
          "arcs": pathops.LineJoin.MITER_JOIN, "round": pathops.LineJoin.ROUND_JOIN,
          "bevel": pathops.LineJoin.BEVEL_JOIN}


def stroke_outline_d(el, comp: dict, vb, scale_target: float) -> Optional[str]:
    """Accurate stroke->fill. skia's stroker approximates offset curves with a fixed absolute
    tolerance (~0.1-0.25 units), which is ~6% radius error on a 24-unit Lucide icon
    (r=1 circle, width 2). Stroking a copy scaled so width ~= scale_target and scaling back
    brings the error to <0.001 units."""
    shape = pico_from_element(_geometry_only(el))
    cmds = list(shape.as_cmd_seq())  # absolute, arcs -> cubics
    if not cmds:
        return None
    diag = math.hypot(vb[2], vb[3]) / math.sqrt(2)
    width = _num(comp.get("stroke-width"), 1.0, pct_of=diag)
    if width <= 0:
        return None
    F = max(1.0, scale_target / width)
    sk = svg_pathops.skia_path(cmds, "nonzero").transform(F, 0, 0, F, 0, 0)
    dash = []
    if comp.get("stroke-dasharray", "none") not in ("none", ""):
        dash = [_num(v, 0.0, pct_of=diag) * F for v in re.split(r"[\s,]+", comp["stroke-dasharray"].strip()) if v]
        if len(dash) % 2:
            dash *= 2
        if not any(dash):
            dash = []
    sk.stroke(width * F, _CAPS.get(comp.get("stroke-linecap", "butt"), pathops.LineCap.BUTT_CAP),
              _JOINS.get(comp.get("stroke-linejoin", "miter"), pathops.LineJoin.MITER_JOIN),
              _num(comp.get("stroke-miterlimit"), 4.0), dash or None,
              _num(comp.get("stroke-dashoffset"), 0.0) * F)
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


def _normalize_geometry_attrs(el, vb):
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
        el.set(k, _fmt(_num(v, 0.0, pct_of=ref[axis]), 6))


def _process_shape(ctx: _Ctx, el, comp: dict, ancestors: list, index_path):
    parent = el.getparent()
    _normalize_geometry_attrs(el, ctx.vb)
    if comp.get("visibility") in ("hidden", "collapse"):
        parent.remove(el)
        return
    ctx.n += 1
    uid = f"e{ctx.n}"
    orig_id = el.get("id")
    use_of = next((a["use_of"] for a in reversed(ancestors) if a.get("use_of")), None)
    base_meta = {
        "uid": uid, "orig_id": orig_id, "tag": local(el.tag), "ancestors": ancestors,
        "index_path": list(index_path), "use_of": use_of, "doc_order": ctx.n,
        "classes": (el.get("class") or "").split(),
        "current_color": "currentColor" in (comp.get("fill", ""), comp.get("stroke", "")),
    }
    fill = _parse_paint(ctx, comp.get("fill"), comp)
    stroke = _parse_paint(ctx, comp.get("stroke"), comp)
    fill_attr, fill_a = _paint_attr(ctx, fill, el, uid, comp)
    fill_op = _num(comp.get("fill-opacity"), 1.0) * fill_a
    stroke_attr, stroke_a = ("none", 0.0)
    if stroke[0] != "none":
        stroke_attr, stroke_a = _paint_attr(ctx, stroke, el, uid + "s", comp)
    stroke_op = _num(comp.get("stroke-opacity"), 1.0) * stroke_a
    paints_fill = fill_attr != "none" and fill_op > 0 and local(el.tag) != "line"
    paints_stroke = stroke_attr != "none" and stroke_op > 0 and _num(comp.get("stroke-width"), 1.0) > 0

    el.set("id", uid)
    el.set("fill", fill_attr if paints_fill else "none")
    if paints_fill and fill_op < 1:
        el.set("fill-opacity", _fmt(fill_op))
    else:
        el.attrib.pop("fill-opacity", None)
    el.set("fill-rule", comp.get("fill-rule", "nonzero"))
    el.set("stroke", "none")
    ctx.meta[uid] = {**base_meta, "role": "fill", "base": uid,
                     "fill_source": comp.get("fill")}

    outline = None
    if paints_stroke:
        try:
            d = stroke_outline_d(el, comp, ctx.vb, ctx.opts.stroke_scale_target)
        except Exception as ex:  # noqa: BLE001
            ctx.warnings.append(f"stroke of {orig_id or uid} failed: {ex}")
            d = None
        if d:
            outline = etree.Element(q("path"))
            outline.set("id", uid + "s")
            outline.set("d", d)
            outline.set("fill", stroke_attr)
            if stroke_op < 1:
                outline.set("fill-opacity", _fmt(stroke_op))
            outline.set("fill-rule", "nonzero")
            for k in ("transform", "clip-path"):
                if el.get(k):
                    outline.set(k, el.get(k))
            ctx.meta[uid + "s"] = {**base_meta, "uid": uid + "s", "role": "stroke", "base": uid,
                                   "stroke_width": _num(comp.get("stroke-width"), 1.0),
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
    if op is not None and _num(op, 1.0) < 1:
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
# normalisation (picosvg) + element extraction
# ----------------------------------------------------------------------------------------------


@dataclass
class Element:
    uid: str
    order: int
    d: str
    paint: dict
    opacity: float           # element opacity incl. fill-opacity (excl. retained group opacity)
    group_opacity: float     # product of retained <g opacity> ancestors (picosvg keeps these)
    opacity_group: Optional[str]
    meta: dict
    path: Any = None         # pathops.Path, simplified, nonzero, non-overlapping contours
    geom: Any = None         # shapely geometry (flattened) for analysis
    bbox: Tuple[float, float, float, float] = (0, 0, 0, 0)
    area: float = 0.0

    @property
    def opaque(self) -> bool:
        return self.opacity >= 0.999 and self.group_opacity >= 0.999 and self.paint.get("opaque", True)

    @property
    def opaque_in_group(self) -> bool:
        return self.opacity >= 0.999 and self.paint.get("opaque", True)

    @property
    def paint_key(self) -> str:
        return self.paint["key"] + f"@{round(self.opacity * self.group_opacity, 2)}"


def normalize(pre: Prepass, opts: Options) -> SVG:
    pico = SVG.fromstring(pre.svg_text)
    try:
        return pico.topicosvg(ndigits=opts.ndigits)
    except ValueError as ex:
        pre.warnings.append(f"picosvg strict failed ({ex}); retrying with drop_unsupported")
        return SVG.fromstring(pre.svg_text).topicosvg(ndigits=opts.ndigits, drop_unsupported=True)


def _gradient_paint(grad_el) -> dict:
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
        stops.append({"offset": float(s.get("offset", 0)), "hex": to_hex(col), "rgb": list(col[:3]),
                      "opacity": float(s.get("stop-opacity", 1))})
    avg = [sum(s["rgb"][i] for s in stops) / max(1, len(stops)) for i in range(3)]
    key = "grad:" + hashlib.md5(json.dumps([[s["hex"], s["opacity"]] for s in stops]).encode()).hexdigest()[:8]
    return {
        "type": "linear" if tag == "linearGradient" else "radial",
        "units": grad_el.get("gradientUnits", "objectBoundingBox"),
        "coords": g, "transform": list(tf), "spread": grad_el.get("spreadMethod", "pad"),
        "stops": stops, "avg_rgb": avg, "hex": to_hex(avg), "key": key,
        "opaque": all(s["opacity"] >= 0.999 for s in stops), "id": grad_el.get("id"),
    }


def _flatten_contours(sk: pathops.Path, tol: float) -> List[List[Tuple[float, float]]]:
    rings, cur, start, ring = [], None, None, []
    for verb, pts in sk:
        if verb == pathops.PathVerb.MOVE:
            if len(ring) > 2:
                rings.append(ring)
            start = cur = pts[0]
            ring = [cur]
        elif verb == pathops.PathVerb.LINE:
            cur = pts[0]
            ring.append(cur)
        elif verb in (pathops.PathVerb.QUAD, pathops.PathVerb.CUBIC):
            ctrl = (cur,) + tuple(pts)
            length = sum(math.dist(ctrl[i], ctrl[i + 1]) for i in range(len(ctrl) - 1))
            n = max(2, min(64, int(math.ceil(math.sqrt(length / max(tol, 1e-9))))))
            for i in range(1, n + 1):
                t = i / n
                if len(ctrl) == 3:
                    p0, c, p1 = ctrl
                    ring.append(((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * c[0] + t * t * p1[0],
                                 (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * c[1] + t * t * p1[1]))
                else:
                    p0, c1, c2, p1 = ctrl
                    mt = 1 - t
                    ring.append((mt ** 3 * p0[0] + 3 * mt * mt * t * c1[0] + 3 * mt * t * t * c2[0] + t ** 3 * p1[0],
                                 mt ** 3 * p0[1] + 3 * mt * mt * t * c1[1] + 3 * mt * t * t * c2[1] + t ** 3 * p1[1]))
            cur = pts[-1]
        elif verb == pathops.PathVerb.CLOSE:
            if len(ring) > 2:
                rings.append(ring)
            ring = []
            cur = start
    if len(ring) > 2:
        rings.append(ring)
    return rings


def _shapely_from_path(sk: pathops.Path, tol: float):
    geom = None
    for ring in _flatten_contours(sk, tol):
        poly = shapely.make_valid(Polygon(ring))
        if poly.is_empty:
            continue
        # simplified skia paths: even-odd composition == nonzero result
        geom = poly if geom is None else geom.symmetric_difference(poly)
    return geom if geom is not None else Polygon()


def clean_d(sk: pathops.Path, ndigits: int = 4) -> str:
    """pathops.Path -> compact absolute SVG path data (M/L/Q/C/Z only)."""
    return SVGPath.from_commands(svg_pathops.svg_commands(sk)).round_floats(ndigits).d


def _skia(d: str, fill_rule: str = "nonzero") -> pathops.Path:
    sk = svg_pathops.skia_path(SVGPath(d=d).as_cmd_seq(), fill_rule)
    sk.convertConicsToQuads(0.001)
    try:
        sk.simplify(fix_winding=True)
    except pathops.PathOpsError:
        pass
    return sk


def extract_elements(pico: SVG, pre: Prepass, opts: Options) -> List[Element]:
    vb = pre.view_box
    tol = math.hypot(vb[2], vb[3]) * opts.curve_tolerance_pct / 100.0
    grads = {g.get("id"): g for g in pico.svg_root.iter() if local(g.tag) in GRADIENT_TAGS}
    out: List[Element] = []
    seen = {}

    def walk(node, gop, gid, depth):
        for child in node:
            if not isinstance(child.tag, str):
                continue
            name = local(child.tag)
            if name == "g":
                op = float(child.get("opacity", 1))
                walk(child, gop * op, f"og{len(out)}_{depth}" if op < 1 else gid, depth + 1)
            elif name == "path":
                uid = child.get("id") or f"anon{len(out)}"
                if uid in seen:
                    seen[uid] += 1
                    uid = f"{uid}.{seen[uid]}"
                else:
                    seen[uid] = 0
                fill = child.get("fill", "#000000")
                m = re.match(r"url\(#(.+)\)", fill)
                if m and m.group(1) in grads:
                    paint = _gradient_paint(grads[m.group(1)])
                else:
                    col = parse_color(fill) or (0, 0, 0, 1)
                    paint = {"type": "solid", "hex": to_hex(col), "rgb": list(col[:3]), "avg_rgb": list(col[:3]),
                             "key": to_hex(col), "opaque": True}
                op = float(child.get("opacity", 1)) * float(child.get("fill-opacity", 1))
                sk = _skia(child.get("d", ""), child.get("fill-rule", "nonzero"))
                b = sk.bounds if list(sk) else (0, 0, 0, 0)
                geom = _shapely_from_path(sk, tol)
                base = uid.split(".")[0]
                meta = pre.meta.get(base, {"uid": base, "role": "fill", "base": base, "ancestors": [],
                                           "index_path": [], "orig_id": None})
                # emit the *simplified* geometry: no self-intersections, nesting == holes, so
                # three.js ShapePath.toShapes()/earcut and any fill rule give the same result
                d_clean = clean_d(sk, opts.ndigits)
                out.append(Element(uid, len(out), d_clean, paint, op, gop, gid, meta,
                                   sk, geom, tuple(b), float(geom.area)))

    walk(pico.svg_root, 1.0, None, 0)
    return [e for e in out if e.area > 0 or list(e.path)]


# ----------------------------------------------------------------------------------------------
# layer splitting
# ----------------------------------------------------------------------------------------------


@dataclass
class SplitParams:
    max_layers: int = 6                  # hard cap incl. background
    min_layers: int = 2
    adjacency_pct: float = 0.75          # "touching" if gap <= this % of viewBox diagonal
    same_color_de: float = 6.0           # CIE76 dE under which two solid paints count as "same"
    background_cover: float = 0.80       # bottom element covering >= this fraction of the viewBox
    keep_fill_stroke_together: bool = True
    w_color: float = 0.8                 # agglomeration cost weights
    w_gap: float = 2.0
    w_group: float = 1.0
    w_z: float = 0.3
    auto_merge_cost: float = 0.25        # merge even under budget when cost is this low
    explode_single_shape: bool = True    # single compound path -> one element per contour island


class Analysis:
    """Pairwise overlap/gap facts + z-order constraint graph for a list of elements."""

    def __init__(self, els: List[Element], vb, params: SplitParams):
        self.els, self.vb, self.p = els, vb, params
        n = len(els)
        self.diag = math.hypot(vb[2], vb[3])
        self.eps_area = (vb[2] * vb[3]) * 1e-6
        import numpy as np
        geoms = np.array([e.geom for e in els], dtype=object)
        shapely.prepare(geoms)
        self.gap = [[0.0] * n for _ in range(n)]
        self.overlap = [[False] * n for _ in range(n)]
        empty = shapely.is_empty(geoms)
        for i in range(n - 1):  # vectorised row-wise (shapely 2 ufuncs)
            rest = geoms[i + 1:]
            d = shapely.distance(geoms[i], rest)
            d = np.where(empty[i] | empty[i + 1:] | np.isnan(d), np.inf, d)
            touch = np.nonzero(d == 0)[0]
            ov = np.zeros(len(rest), dtype=bool)
            if len(touch):
                ov[touch] = shapely.area(shapely.intersection(geoms[i], rest[touch])) > self.eps_area
            for k in range(len(rest)):
                j = i + 1 + k
                self.gap[i][j] = self.gap[j][i] = float(d[k])
                self.overlap[i][j] = self.overlap[j][i] = bool(ov[k])
        # i painted below j and they overlap -> layer(i) must be below layer(j)
        self.edges = [(i, j) for i in range(n) for j in range(i + 1, n) if self.overlap[i][j]]

    def consistent(self, assign: List[int]) -> bool:
        """True iff the contracted 'below' graph between clusters is acyclic."""
        succ: Dict[int, set] = {}
        indeg: Dict[int, int] = {c: 0 for c in set(assign)}
        for i, j in self.edges:
            a, b = assign[i], assign[j]
            if a != b and b not in succ.setdefault(a, set()):
                succ[a].add(b)
                indeg[b] += 1
        stack = [c for c, d in indeg.items() if d == 0]
        seen = 0
        while stack:
            c = stack.pop()
            seen += 1
            for nb in succ.get(c, ()):
                indeg[nb] -= 1
                if indeg[nb] == 0:
                    stack.append(nb)
        return seen == len(indeg)

    def order_clusters(self, assign: List[int]) -> List[int]:
        """Topological order of clusters, ties broken by mean paint order."""
        clusters = sorted(set(assign))
        succ = {c: set() for c in clusters}
        indeg = {c: 0 for c in clusters}
        for i, j in self.edges:
            a, b = assign[i], assign[j]
            if a != b and b not in succ[a]:
                succ[a].add(b)
                indeg[b] += 1
        mean = {c: sum(i for i, a in enumerate(assign) if a == c) / max(1, assign.count(c)) for c in clusters}
        ready = sorted([c for c in clusters if indeg[c] == 0], key=lambda c: mean[c])
        out = []
        while ready:
            c = ready.pop(0)
            out.append(c)
            for nb in succ[c]:
                indeg[nb] -= 1
                if indeg[nb] == 0:
                    ready.append(nb)
            ready.sort(key=lambda c: mean[c])
        if len(out) != len(clusters):  # should not happen; fall back to mean order
            out = sorted(clusters, key=lambda c: mean[c])
        return out


def _relabel(assign, a, b):
    return [a if x == b else x for x in assign]


def _common_group_depth(m1: dict, m2: dict) -> Tuple[int, int]:
    a1, a2 = m1.get("index_path", []), m2.get("index_path", [])
    n = 0
    for x, y in zip(a1[:-1], a2[:-1]):
        if x != y:
            break
        n += 1
    return n, max(len(a1), len(a2)) - 1


def _forced_units(els: List[Element], params: SplitParams) -> List[int]:
    """Initial clusters: things that must stay together to composite correctly."""
    parent = list(range(len(els)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    first_by = {}
    for i, e in enumerate(els):
        keys = []
        if e.opacity_group:
            keys.append(("og", e.opacity_group))          # retained <g opacity>: composite together
        if params.keep_fill_stroke_together:
            keys.append(("base", e.meta.get("base", e.uid)))  # fill + its stroke outline
        if e.meta.get("use_of"):
            keys.append(("use", tuple(_use_prefix(e.meta))))   # one <use> instance = one unit
        for k in keys:
            if k in first_by:
                union(first_by[k], i)
            else:
                first_by[k] = i
    return [find(i) for i in range(len(els))]


def _use_prefix(meta) -> list:
    anc = meta.get("ancestors", [])
    for depth, a in enumerate(anc):
        if a.get("use_of"):
            return meta.get("index_path", [])[:depth + 1]
    return []


def detect_background(els: List[Element], vb, params: SplitParams) -> List[int]:
    """Bottom-most elements that act as the icon 'plate': big (>= ~45% of the viewBox) and
    containing (>= 90% of) everything painted above them."""
    vb_area = vb[2] * vb[3]
    bg = []
    for i, e in enumerate(els):
        if i != len(bg) or e.area < params.background_cover * 0.56 * vb_area or i == len(els) - 1:
            break
        rest = shapely.unary_union([x.geom for x in els[i + 1:] if not x.geom.is_empty])
        if rest.is_empty or rest.difference(e.geom).area > 0.10 * rest.area:
            break
        bg.append(i)
    return bg


def split_layers(els: List[Element], vb, strategy: str = "smart",
                 params: Optional[SplitParams] = None) -> Tuple[List[List[int]], dict]:
    """Returns (layers as lists of element indices bottom->top, debug info)."""
    params = params or SplitParams()
    if not els:
        return [], {}
    an = Analysis(els, vb, params)
    n = len(els)
    info: dict = {"strategy": strategy}
    adj = an.diag * params.adjacency_pct / 100.0
    assign = _forced_units(els, params)

    def try_merge(a, b) -> bool:
        nonlocal assign
        cand = _relabel(assign, a, b)
        if an.consistent(cand):
            assign = cand
            return True
        return False

    if strategy == "element":
        pass

    elif strategy == "group":
        # unwrap wrappers shared by everything (Figma clip group, Illustrator Layer_1, <svg> in <g>)
        paths = [e.meta.get("index_path", []) for e in els]
        depth = 0
        while all(len(p) > depth + 1 for p in paths) and len({p[depth] for p in paths}) == 1:
            depth += 1
        key_first = {}
        for i, p in enumerate(paths):
            key = ("g", p[depth]) if len(p) > depth + 1 else ("bare", i)
            if key in key_first:
                assign = _relabel(assign, assign[key_first[key]], assign[i])
            else:
                key_first[key] = i
        info["unwrap_depth"] = depth

    elif strategy == "color":
        st = _ClusterState(an, els, assign, params)
        for i in range(n):
            for j in range(i):
                if st.assign[i] != st.assign[j] and _same_paint(els[i], els[j], params):
                    if st.try_merge(st.assign[j], st.assign[i]):
                        break
        assign = st.assign

    elif strategy == "smart":
        bg = detect_background(els, vb, params)
        for i in bg[1:]:
            assign = _relabel(assign, assign[bg[0]], assign[i])
        st = _ClusterState(an, els, assign, params)
        bg_cluster = st.assign[bg[0]] if bg else None
        info["background"] = [els[i].uid for i in bg]
        # phase A: same paint & touching/overlapping, closest pairs first
        pairs = sorted(((an.gap[i][j], i, j) for i in range(n) for j in range(i + 1, n)
                        if an.gap[i][j] <= adj and _same_paint(els[i], els[j], params)))
        # phase A2: same paint & same (non-root) parent group, any distance (eyes, sun rays, ...)
        pairs += sorted(((an.gap[i][j], i, j) for i in range(n) for j in range(i + 1, n)
                         if _same_parent_group(els[i], els[j]) and _same_paint(els[i], els[j], params)))
        for _, i, j in pairs:
            a, b = st.assign[i], st.assign[j]
            if a != b and bg_cluster not in (a, b):
                st.try_merge(min(a, b), max(a, b))
        n_paints = len({e.paint_key for k, e in enumerate(els) if st.assign[k] != bg_cluster})
        max_fg = params.max_layers - (1 if bg else 0)
        if n_paints <= 1:
            max_fg = min(max_fg, 3)  # monochrome glyph/line icon: 2-3 depth layers read best
        info["max_fg"] = max_fg
        # phase B: agglomerate cheapest valid pairs while over budget, or while "obviously cheap"
        # (lazy min-heap over cluster pairs; stale entries are skipped via per-cluster versions)
        import heapq
        heap = []
        alive = [c for c in st.members if c != bg_cluster]
        for x in range(len(alive)):
            for y in range(x + 1, len(alive)):
                a, b = alive[x], alive[y]
                heap.append((st.cost(a, b), a, b, st.ver[a], st.ver[b]))
        heapq.heapify(heap)
        while heap:
            fg_count = sum(1 for c in st.members if c != bg_cluster)
            if fg_count <= 1:
                break
            over_budget = fg_count > max_fg
            cost, a, b, va, vb_ = heapq.heappop(heap)
            if a not in st.members or b not in st.members or st.ver[a] != va or st.ver[b] != vb_:
                continue
            if not over_budget and cost > params.auto_merge_cost:
                break
            if st.try_merge(a, b):
                for c in st.members:
                    if c not in (a, bg_cluster):
                        heapq.heappush(heap, (st.cost(a, c), min(a, c), max(a, c),
                                              st.ver[min(a, c)], st.ver[max(a, c)]))
        if sum(1 for c in st.members if c != bg_cluster) > max_fg:
            info["note"] = "budget not reachable without breaking z-order"
        assign = st.assign
        # phase C: too few layers (single glyph): split by connected components of its parts
        fg = sorted(set(a for a in assign if a != bg_cluster))
        if len(fg) + (1 if bg else 0) < params.min_layers and len(fg) == 1:
            members = [i for i in range(n) if assign[i] == fg[0]]
            comps = _components(an, members, adj)
            while len(comps) > max_fg:  # glue the two closest components
                best = min(((min(an.gap[i][j] for i in comps[x] for j in comps[y]), x, y)
                            for x in range(len(comps)) for y in range(x + 1, len(comps))))
                _, x, y = best
                comps[x] = sorted(comps[x] + comps.pop(y))
            if len(comps) > 1:
                comps.sort(key=lambda c: min(c))
                for c in comps[1:]:
                    newlabel = max(assign) + 1
                    trial = list(assign)
                    for i in c:
                        trial[i] = newlabel
                    if an.consistent(trial):
                        assign = trial
                info["note"] = "single-colour icon split by connected components"
            else:
                info["note"] = "single shape: use contour-island split or manual split in UI"
    else:
        raise ValueError(strategy)

    order = an.order_clusters(assign)
    layers = [[i for i in range(n) if assign[i] == c] for c in order]
    info["consistent"] = an.consistent(assign)
    info["n_layers"] = len(layers)
    return layers, info


def _same_parent_group(a: Element, b: Element) -> bool:
    pa, pb = a.meta.get("index_path", []), b.meta.get("index_path", [])
    return len(pa) > 1 and len(pb) > 1 and pa[:-1] == pb[:-1]


def _same_paint(a: Element, b: Element, p: SplitParams) -> bool:
    if abs(a.opacity * a.group_opacity - b.opacity * b.group_opacity) > 0.05:
        return False
    if a.paint["type"] != "solid" or b.paint["type"] != "solid":
        return a.paint["key"] == b.paint["key"]
    return delta_e(a.paint["rgb"], b.paint["rgb"]) <= p.same_color_de


class _ClusterState:
    """Clusters of elements with (1) an incrementally maintained 'is painted below' DAG between
    clusters - a merge is legal iff it creates no cycle, i.e. there is no path a->..->b or
    b->..->a through a third cluster - and (2) cached linkage stats so merge costs are O(1):
    single-linkage gap, max group affinity, area-weighted mean Lab, mean paint index."""

    def __init__(self, an: Analysis, els: List[Element], assign: List[int], p: SplitParams):
        self.an, self.els, self.p = an, els, p
        self.assign = list(assign)
        self.members: Dict[int, List[int]] = {}
        for i, c in enumerate(self.assign):
            self.members.setdefault(c, []).append(i)
        self.ver = {c: 0 for c in self.members}
        self.succ = {c: set() for c in self.members}
        self.pred = {c: set() for c in self.members}
        for i, j in an.edges:
            a, b = self.assign[i], self.assign[j]
            if a != b:
                self.succ[a].add(b)
                self.pred[b].add(a)
        labs = [rgb_to_lab(e.paint["avg_rgb"]) for e in els]
        wts = [max(e.area, 1e-9) for e in els]
        self.lab = {c: [sum(labs[i][k] * wts[i] for i in m) for k in range(3)] for c, m in self.members.items()}
        self.w = {c: sum(wts[i] for i in m) for c, m in self.members.items()}
        self.zsum = {c: float(sum(m)) for c, m in self.members.items()}
        n = len(els)
        aff_e = [[0.0] * n for _ in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                c, mx = _common_group_depth(els[i].meta, els[j].meta)
                aff_e[i][j] = aff_e[j][i] = c / mx if mx > 0 else 0.0
        self.gap: Dict[Tuple[int, int], float] = {}
        self.aff: Dict[Tuple[int, int], float] = {}
        cl = list(self.members)
        for x in range(len(cl)):
            for y in range(x + 1, len(cl)):
                a, b = cl[x], cl[y]
                k = (min(a, b), max(a, b))
                self.gap[k] = min(an.gap[i][j] for i in self.members[a] for j in self.members[b])
                self.aff[k] = max(aff_e[i][j] for i in self.members[a] for j in self.members[b])

    def cost(self, a: int, b: int) -> float:
        k = (min(a, b), max(a, b))
        la = [v / self.w[a] for v in self.lab[a]]
        lb = [v / self.w[b] for v in self.lab[b]]
        de = math.sqrt(sum((x - y) ** 2 for x, y in zip(la, lb)))
        zdist = abs(self.zsum[a] / len(self.members[a]) - self.zsum[b] / len(self.members[b])) / max(1, len(self.els))
        diag = self.an.diag
        return (self.p.w_color * min(de, 100.0) / 100.0 + self.p.w_gap * min(self.gap[k], diag) / diag
                + self.p.w_group * (1 - self.aff[k]) + self.p.w_z * zdist)

    def _reach(self, src: int, dst: int) -> bool:
        stack = [c for c in self.succ[src] if c != dst]
        seen = set(stack)
        while stack:
            c = stack.pop()
            for nb in self.succ[c]:
                if nb == dst:
                    return True
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        return False

    def try_merge(self, a: int, b: int) -> bool:
        if a == b or self._reach(a, b) or self._reach(b, a):
            return False
        for i in self.members[b]:
            self.assign[i] = a
        self.members[a] += self.members.pop(b)
        self.lab[a] = [x + y for x, y in zip(self.lab[a], self.lab.pop(b))]
        self.w[a] += self.w.pop(b)
        self.zsum[a] += self.zsum.pop(b)
        for c in self.succ[b]:
            self.pred[c].discard(b)
            if c != a:
                self.pred[c].add(a)
        for c in self.pred[b]:
            self.succ[c].discard(b)
            if c != a:
                self.succ[c].add(a)
        self.succ[a] = (self.succ[a] | self.succ.pop(b)) - {a, b}
        self.pred[a] = (self.pred[a] | self.pred.pop(b)) - {a, b}
        self.gap.pop((min(a, b), max(a, b)), None)
        self.aff.pop((min(a, b), max(a, b)), None)
        for c in self.members:
            if c == a:
                continue
            ka, kb = (min(a, c), max(a, c)), (min(b, c), max(b, c))
            self.gap[ka] = min(self.gap[ka], self.gap.pop(kb))
            self.aff[ka] = max(self.aff[ka], self.aff.pop(kb))
        self.ver[a] += 1
        del self.ver[b]
        return True


def _components(an: Analysis, members: List[int], adj: float) -> List[List[int]]:
    comps, todo = [], set(members)
    while todo:
        stack = [todo.pop()]
        comp = []
        while stack:
            i = stack.pop()
            comp.append(i)
            for j in list(todo):
                if an.gap[i][j] <= adj:
                    todo.remove(j)
                    stack.append(j)
        comps.append(sorted(comp))
    return comps


# ----------------------------------------------------------------------------------------------
# geometry export: pathops -> cubic bezier splines (Blender / three.js), layer SVGs
# ----------------------------------------------------------------------------------------------


def icon_space(vb) -> Callable[[Tuple[float, float]], Tuple[float, float]]:
    """SVG user units -> centred, y-up, unit icon space: the viewBox's longer side spans 1.0."""
    cx, cy = vb[0] + vb[2] / 2, vb[1] + vb[3] / 2
    s = max(vb[2], vb[3])
    return lambda p: ((p[0] - cx) / s, (cy - p[1]) / s)


def path_to_splines(sk: pathops.Path, xf: Callable = lambda p: p, nd: int = 6) -> List[dict]:
    """pathops.Path -> [{closed, points:[{co, hl, hr}]}]: one cubic Bezier spline per contour.

    Lines become cubics with handles at 1/3 and 2/3 (exact, Blender handle type FREE);
    quads are degree-elevated exactly. Use after simplify(): contours are then disjoint,
    and nesting depth alone decides holes (even-odd == nonzero)."""
    splines = []
    segs: list = []
    start = cur = None
    closed = False

    def flush():
        nonlocal segs
        segs = [s for s in segs if not (math.dist(s[0], s[3]) < 1e-9 and math.dist(s[0], s[1]) < 1e-9
                                         and math.dist(s[2], s[3]) < 1e-9)]
        if not segs:
            return
        pts = []
        if closed:
            for k, (p0, c1, c2, p1) in enumerate(segs):
                prev = segs[k - 1]
                pts.append({"co": p0, "hl": prev[2], "hr": c1})
        else:
            for k, (p0, c1, c2, p1) in enumerate(segs):
                pts.append({"co": p0, "hl": segs[k - 1][2] if k else p0, "hr": c1})
            pts.append({"co": segs[-1][3], "hl": segs[-1][2], "hr": segs[-1][3]})
        r = lambda p: [round(v, nd) for v in xf(p)]  # noqa: E731
        splines.append({"closed": closed,
                        "points": [{"co": r(p["co"]), "hl": r(p["hl"]), "hr": r(p["hr"])} for p in pts]})
        segs = []

    for verb, pts in sk:
        if verb == pathops.PathVerb.MOVE:
            closed = False
            flush()
            start = cur = pts[0]
        elif verb == pathops.PathVerb.LINE:
            p = pts[0]
            segs.append((cur, _lerp(cur, p, 1 / 3), _lerp(cur, p, 2 / 3), p))
            cur = p
        elif verb == pathops.PathVerb.QUAD:
            c, p = pts
            segs.append((cur, _lerp(cur, c, 2 / 3), _lerp(p, c, 2 / 3), p))
            cur = p
        elif verb == pathops.PathVerb.CUBIC:
            segs.append((cur, pts[0], pts[1], pts[2]))
            cur = pts[2]
        elif verb == pathops.PathVerb.CONIC:
            raise ValueError("convertConicsToQuads() first")
        elif verb == pathops.PathVerb.CLOSE:
            if cur is not None and start is not None and math.dist(cur, start) > 1e-9:
                segs.append((cur, _lerp(cur, start, 1 / 3), _lerp(cur, start, 2 / 3), start))
            closed = True
            flush()
            closed = False
            cur = start
    flush()
    return splines


def _lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def annotate_holes(splines: List[dict]) -> None:
    """Adds depth / hole / parent to each closed spline (for three.js Shape+holes)."""
    polys = []
    for s in splines:
        ring = [tuple(p["co"]) for p in s["points"]]
        poly = shapely.make_valid(Polygon(ring)) if len(ring) >= 3 else Polygon()
        polys.append(poly)
    for i, s in enumerate(splines):
        pt = _probe_point(s)
        containers = [j for j, pj in enumerate(polys) if j != i and not pj.is_empty and pj.contains(Point(pt))]
        s["depth"] = len(containers)
        s["hole"] = len(containers) % 2 == 1
        s["parent"] = min(containers, key=lambda j: polys[j].area) if containers else None


def _probe_point(spline):
    """A point on the curve itself (segment midpoint) - contours never cross after simplify."""
    p = spline["points"]
    a, b = p[0], p[1 % len(p)]
    t = 0.5
    mt = 1 - t
    return tuple(mt ** 3 * a["co"][k] + 3 * mt * mt * t * a["hr"][k] + 3 * mt * t * t * b["hl"][k] + t ** 3 * b["co"][k]
                 for k in range(2))


def splines_to_d(splines: List[dict]) -> str:
    """Inverse of path_to_splines (for validation and for three.js SVGLoader)."""
    out = []
    for s in splines:
        pts = s["points"]
        out.append(f"M{_fmt(pts[0]['co'][0], 6)} {_fmt(pts[0]['co'][1], 6)}")
        seq = list(range(1, len(pts))) + ([0] if s["closed"] else [])
        prev = pts[0]
        for k in seq:
            p = pts[k]
            out.append("C" + " ".join(_fmt(v, 6) for v in (*prev["hr"], *p["hl"], *p["co"])))
            prev = p
        if s["closed"]:
            out.append("Z")
    return " ".join(out)


def gradient_for_shader(paint: dict, vb) -> Optional[dict]:
    """Gradient -> icon-space evaluation recipe that maps 1:1 onto Blender nodes.

    linear: t = dot(P.xy, dir) + offset     (Vector Math DOT -> Math ADD -> Color Ramp)
    radial: t = length(M @ P.xy + m)         (two DOTs -> Combine XYZ -> LENGTH -> Color Ramp)
    P is the object-space position in icon space (see icon_space)."""
    if paint["type"] not in ("linear", "radial"):
        return None
    c = paint["coords"]
    a, b, cc, d, e, f = paint["transform"]
    G = Affine2D(a, b, cc, d, e, f)                     # gradient space -> svg user space
    cx, cy = vb[0] + vb[2] / 2, vb[1] + vb[3] / 2
    s = max(vb[2], vb[3])
    N_inv = Affine2D(s, 0, 0, -s, cx, cy)               # icon space -> svg user space
    M = G.inverse() @ N_inv                             # icon space -> gradient space
    if paint["type"] == "linear":
        dx, dy = c["x2"] - c["x1"], c["y2"] - c["y1"]
        L2 = dx * dx + dy * dy or 1e-12
        # t = ((M p) - p1) . (dx,dy) / L2
        ux = (M.a * dx + M.b * dy) / L2
        uy = (M.c * dx + M.d * dy) / L2
        off = ((M.e - c["x1"]) * dx + (M.f - c["y1"]) * dy) / L2
        return {"type": "linear", "dir": [ux, uy], "offset": off, "spread": paint["spread"]}
    r = c["r"] or 1e-12
    S = Affine2D(1 / r, 0, 0, 1 / r, -c["cx"] / r, -c["cy"] / r) @ M
    # focal point in the same normalised space (centre = origin, radius = 1). With f != 0 the exact
    # SVG rule is: t solves |f + (q - f)/t| = 1  ->  t = (|d|^2) / (-(f.d) + sqrt((f.d)^2 - (|f|^2-1)|d|^2))
    # with d = q - f  (a handful of Math nodes); f == 0 reduces to t = |q|.
    f = [(c.get("fx", c["cx"]) - c["cx"]) / r, (c.get("fy", c["cy"]) - c["cy"]) / r]
    return {"type": "radial", "matrix": [[S.a, S.c], [S.b, S.d]], "offset": [S.e, S.f],
            "focal": f, "has_focal": abs(f[0]) + abs(f[1]) > 1e-6, "spread": paint["spread"]}


@dataclass
class Layer:
    index: int
    name: str
    members: List[Element]
    is_background: bool = False


def _layer_name(members: List[Element], idx: int, is_bg: bool) -> str:
    if is_bg:
        return "background"
    # deepest named group shared by all members
    chains = [m.meta.get("ancestors", []) for m in members]
    shared = None
    for depth in range(min((len(c) for c in chains), default=0)):
        names = {(c[depth].get("name"), c[depth].get("auto_name")) for c in chains}
        if len(names) == 1:
            nm, auto = next(iter(names))
            if nm and not auto:
                shared = nm
        else:
            break
    if shared:
        return shared
    ids = [m.meta.get("orig_id") for m in members if m.meta.get("orig_id") and not _auto_name(m.meta.get("orig_id"))]
    if len(ids) == 1:
        return ids[0]
    if ids:
        return ids[0] + f" +{len(ids) - 1}"
    return f"layer {idx + 1} ({members[0].paint['hex']})"


def build_layers(els: List[Element], groups: List[List[int]], vb, pico: SVG, info: dict) -> List[dict]:
    xf = icon_space(vb)
    defs = {g.get("id"): g for g in pico.svg_root.iter() if local(g.tag) in GRADIENT_TAGS}
    bg_uids = set(info.get("background", []))
    out = []
    for li, idxs in enumerate(groups):
        members = [els[i] for i in sorted(idxs)]
        is_bg = bool(bg_uids) and all(m.uid in bg_uids for m in members)
        name = _layer_name(members, li, is_bg)
        # occlusion-cut colour regions (disjoint where the upper element is opaque)
        regions = []
        for k, m in enumerate(members):
            region = pathops.Path(m.path)
            for up in members[k + 1:]:
                # inside one opacity group children composite first, so cut by element-level opacity
                if up.opaque_in_group and up.opacity_group == m.opacity_group:
                    region = pathops.op(region, up.path, pathops.PathOp.DIFFERENCE, fix_winding=True)
            region.convertConicsToQuads(0.001)
            if not list(region):
                continue
            spl = path_to_splines(region, xf)
            annotate_holes(spl)
            regions.append({"uid": m.uid, "orig_id": m.meta.get("orig_id"), "role": m.meta.get("role"),
                            "paint": _paint_public(m.paint, vb), "opacity": round(m.opacity * m.group_opacity, 4),
                            "z_sub": k, "splines": spl})
        sil = pathops.Path()
        for m in members:
            sil = pathops.op(sil, m.path, pathops.PathOp.UNION, fix_winding=True)
        sil_spl = path_to_splines(sil, xf)
        annotate_holes(sil_spl)
        b = sil.bounds if list(sil) else (0, 0, 0, 0)
        group_ops = {m.opacity_group: m.group_opacity for m in members if m.opacity_group}
        out.append({
            "index": li, "name": name, "is_background": is_bg,
            "members": [m.uid for m in members],
            "orig_ids": [m.meta.get("orig_id") for m in members if m.meta.get("orig_id")],
            "paints": sorted({m.paint["hex"] for m in members}),
            "bbox_svg": [round(v, 4) for v in b],
            "bbox_icon": [round(v, 6) for v in (*xf((b[0], b[3])), *xf((b[2], b[1])))],
            "silhouette": sil_spl,
            "regions": regions,
            "svg": layer_svg(members, vb, defs),
            "opacity_groups": group_ops,
        })
    # disambiguate duplicate names ("Flame", "Flame" -> "Flame / flame-core", "Flame #FDE68A")
    counts: Dict[str, int] = {}
    for L in out:
        counts[L["name"]] = counts.get(L["name"], 0) + 1
    for L in out:
        if counts[L["name"]] > 1:
            ids = [i for i in L["orig_ids"] if not _auto_name(i)]
            L["name"] = f"{L['name']} / {ids[0]}" if ids else f"{L['name']} {L['paints'][0]}"
    return out


def _paint_public(p: dict, vb) -> dict:
    out = {k: v for k, v in p.items() if k not in ("key", "avg_rgb")}
    if p["type"] != "solid":
        out["shader"] = gradient_for_shader(p, vb)
    out["rgb_linear"] = [round(srgb_to_linear(c), 6) for c in p.get("rgb", p["avg_rgb"])]
    return out


def layer_svg(members: List[Element], vb, defs: Dict[str, Any], silhouette_only=False) -> str:
    used = {m.paint.get("id") for m in members if m.paint["type"] != "solid"}
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{" ".join(_fmt(v, 6) for v in vb)}" '
             f'width="{_fmt(vb[2], 6)}" height="{_fmt(vb[3], 6)}">']
    if used and not silhouette_only:
        parts.append("<defs>")
        for gid in sorted(u for u in used if u in defs):
            parts.append(etree.tostring(defs[gid], encoding="unicode").replace(f' xmlns="{SVG_NS}"', ""))
        parts.append("</defs>")
    open_group = None
    for m in members:
        if m.opacity_group != open_group:
            if open_group is not None:
                parts.append("</g>")
            if m.opacity_group is not None:
                parts.append(f'<g opacity="{_fmt(m.group_opacity)}">')
            open_group = m.opacity_group
        fill = "#FFFFFF" if silhouette_only else (f"url(#{m.paint['id']})" if m.paint["type"] != "solid" else m.paint["hex"])
        op = "" if silhouette_only or m.opacity >= 0.999 else f' opacity="{_fmt(m.opacity)}"'
        parts.append(f'<path data-uid="{m.uid}" fill="{fill}"{op} d="{m.d}"/>')
    if open_group is not None:
        parts.append("</g>")
    parts.append("</svg>")
    return "".join(parts)


# ----------------------------------------------------------------------------------------------
# top level
# ----------------------------------------------------------------------------------------------


def _split_contours(sk: pathops.Path) -> List[pathops.Path]:
    out: List[pathops.Path] = []
    cur = None
    for verb, pts in sk:
        if verb == pathops.PathVerb.MOVE:
            cur = pathops.Path()
            out.append(cur)
            cur.moveTo(*pts[0])
        elif verb == pathops.PathVerb.LINE:
            cur.lineTo(*pts[0])
        elif verb == pathops.PathVerb.QUAD:
            cur.quadTo(*pts[0], *pts[1])
        elif verb == pathops.PathVerb.CUBIC:
            cur.cubicTo(*pts[0], *pts[1], *pts[2])
        elif verb == pathops.PathVerb.CLOSE:
            cur.close()
    return out


def explode_islands(e: Element, tol: float) -> List[Element]:
    """Single compound path (icon-font style glyph) -> one Element per island
    (outer contour + its holes; an island inside a hole is its own island)."""
    contours = _split_contours(e.path)
    if len(contours) < 2:
        return [e]
    polys = []
    for c in contours:
        rings = _flatten_contours(c, tol)
        polys.append(shapely.make_valid(Polygon(rings[0])) if rings and len(rings[0]) > 2 else Polygon())
    depth, parent = [], []
    for i, c in enumerate(contours):
        ring = _flatten_contours(c, tol)[0]
        probe = Point(ring[0])
        cont = [j for j, pj in enumerate(polys) if j != i and not pj.is_empty and pj.buffer(tol * 0.01).contains(probe)]
        depth.append(len(cont))
        parent.append(min(cont, key=lambda j: polys[j].area) if cont else None)
    islands: Dict[int, List[int]] = {i: [i] for i in range(len(contours)) if depth[i] % 2 == 0}
    for j in range(len(contours)):
        if depth[j] % 2 == 1 and parent[j] in islands:
            islands[parent[j]].append(j)
    if len(islands) < 2:
        return [e]
    out = []
    for k, (root_i, idxs) in enumerate(sorted(islands.items())):
        p = pathops.Path()
        for j in idxs:
            p.addPath(contours[j])
        try:
            p.simplify(fix_winding=True)
        except pathops.PathOpsError:
            pass
        geom = _shapely_from_path(p, tol)
        d = clean_d(p, 4)
        out.append(Element(f"{e.uid}#i{k}", e.order, d, e.paint, e.opacity, e.group_opacity, e.opacity_group,
                           {**e.meta, "island": k}, p, geom, tuple(p.bounds), float(geom.area)))
    return out


def process(svg_text: str, strategy: str = "smart", opts: Optional[Options] = None,
            params: Optional[SplitParams] = None) -> dict:
    opts = opts or Options()
    params = params or SplitParams()
    pre = prepass(svg_text, opts)
    pico = normalize(pre, opts)
    els = extract_elements(pico, pre, opts)
    if strategy == "smart" and params.explode_single_shape:
        bg = set(detect_background(els, pre.view_box, params))
        fg = [i for i in range(len(els)) if i not in bg]
        if len(fg) == 1:
            tol = math.hypot(pre.view_box[2], pre.view_box[3]) * opts.curve_tolerance_pct / 100.0
            parts = explode_islands(els[fg[0]], tol)
            els = els[:fg[0]] + parts + els[fg[0] + 1:]
            for k, e in enumerate(els):
                e.order = k
    groups, info = split_layers(els, pre.view_box, strategy, params)
    layers = build_layers(els, groups, pre.view_box, pico, info)
    vb = pre.view_box
    return {
        "version": 1,
        "view_box": list(vb),
        "icon_space": {"center": [vb[0] + vb[2] / 2, vb[1] + vb[3] / 2], "scale": max(vb[2], vb[3]),
                       "note": "x' = (x-cx)/scale, y' = (cy-y)/scale  (y-up, longer side = 1)"},
        "strategy": strategy,
        "split_info": info,
        "warnings": pre.warnings,
        "elements": [{"uid": e.uid, "orig_id": e.meta.get("orig_id"), "role": e.meta.get("role"),
                      "tag": e.meta.get("tag"), "paint": e.paint["hex"], "paint_type": e.paint["type"],
                      "opacity": round(e.opacity * e.group_opacity, 4), "area": round(e.area, 3),
                      "groups": [a.get("name") for a in e.meta.get("ancestors", [])]} for e in els],
        "layers": layers,
        "normalized_svg": pico.tostring(),
    }


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("svg")
    ap.add_argument("--strategy", default="smart", choices=["smart", "group", "element", "color"])
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    res = process(open(a.svg, encoding="utf-8").read(), a.strategy)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        stem = os.path.splitext(os.path.basename(a.svg))[0]
        json.dump(res, open(os.path.join(a.out, f"{stem}.{a.strategy}.json"), "w"), indent=1)
        for L in res["layers"]:
            open(os.path.join(a.out, f"{stem}.{a.strategy}.L{L['index']}.svg"), "w", encoding="utf-8").write(L["svg"])
    print(json.dumps({"layers": [(L["index"], L["name"], L["members"]) for L in res["layers"]],
                      "warnings": res["warnings"], "info": res["split_info"]}, indent=1))


if __name__ == "__main__":
    main()
