"""CSS colour parsing (Color 3/4 subset), sRGB/Lab helpers and human colour names.

svgelements' Color silently returns black for CSS4 syntax such as ``rgb(255 0 0 / 50%)``,
so the pipeline parses colours itself."""
from __future__ import annotations

import colorsys
import math
import re
from typing import Optional, Sequence, Tuple

RGBA = Tuple[float, float, float, float]

_NAMED = dict(
    x.split(":") for x in (
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
        "yellow:ffff00 yellowgreen:9acd32").split()
)


def parse_color(s: Optional[str]) -> Optional[RGBA]:
    """CSS colour -> (r, g, b, a) floats in 0..1 (sRGB). None if unparseable."""
    if s is None:
        return None
    s = s.strip().lower()
    if not s:
        return None
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

    def comp(p: str, scale: float) -> float:
        return float(p[:-1]) / 100.0 if p.endswith("%") else float(p) / scale

    try:
        a = comp(parts[3], 1.0) if len(parts) == 4 else 1.0
        if fn.startswith("rgb"):
            r, g, b = (comp(p, 255.0) for p in parts[:3])
        else:
            hue = float(re.sub(r"(deg)$", "", parts[0])) % 360 / 360.0
            sat, lig = comp(parts[1], 100.0), comp(parts[2], 100.0)
            r, g, b = colorsys.hls_to_rgb(hue, lig, sat)
    except ValueError:
        return None

    def clamp(v: float) -> float:
        return min(1.0, max(0.0, v))

    return (clamp(r), clamp(g), clamp(b), clamp(a))


def to_hex(rgb: Sequence[float]) -> str:
    """(r, g, b[, a]) floats -> '#rrggbb' (lower case, alpha dropped)."""
    return "#" + "".join(f"{int(round(min(1.0, max(0.0, c)) * 255)):02x}" for c in rgb[:3])


def hex_to_rgb(h: str) -> Tuple[float, float, float]:
    c = parse_color(h) or (0.0, 0.0, 0.0, 1.0)
    return (c[0], c[1], c[2])


def srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rgb_to_lab(rgb: Sequence[float]) -> Tuple[float, float, float]:
    r, g, b = (srgb_to_linear(c) for c in rgb[:3])
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e(c1: Sequence[float], c2: Sequence[float]) -> float:
    """CIE76 colour difference of two sRGB colours."""
    a, b = rgb_to_lab(c1), rgb_to_lab(c2)
    return math.sqrt(sum((p - q) ** 2 for p, q in zip(a, b)))


def luminance(rgb: Sequence[float]) -> float:
    r, g, b = (srgb_to_linear(c) for c in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


_HUES = [
    (12, "Red"), (40, "Orange"), (66, "Yellow"), (85, "Lime"), (160, "Green"), (190, "Teal"),
    (205, "Cyan"), (250, "Blue"), (275, "Indigo"), (300, "Purple"), (335, "Pink"), (360, "Red"),
]


def color_name(rgb: Sequence[float]) -> str:
    """A short human colour name ('Blue', 'Light Gray', 'Dark Green', ...) for layer names."""
    r, g, b = (min(1.0, max(0.0, c)) for c in rgb[:3])
    h, light, s = colorsys.rgb_to_hls(r, g, b)
    mx, mn = max(r, g, b), min(r, g, b)
    chroma = mx - mn
    if chroma < 0.08 or (s < 0.12 and chroma < 0.16):
        if light > 0.93:
            return "White"
        if light > 0.7:
            return "Light Gray"
        if light > 0.35:
            return "Gray"
        if light > 0.12:
            return "Dark Gray"
        return "Black"
    deg = h * 360.0
    base = next(name for lim, name in _HUES if deg < lim)
    if base == "Orange" and light < 0.35:
        return "Brown"
    if light < 0.25:
        return f"Dark {base}"
    if light > 0.82:
        return f"Light {base}"
    return base
