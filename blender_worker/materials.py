"""Material presets (shared/presets.json) as Blender 5.0 node trees — Cycles + EEVEE.

Recipes follow docs/research/glass-materials-blender.md §3 (identifiers verified in 5.0.0). Every
material is described by a plain ``spec`` dict (built by scene.py) and rendered through
:class:`nodes.Graph`, so a value-only change (slider drag, light angle, colours, texture swap) updates the
existing node tree in place; only a change of :func:`topology_key` rebuilds the tree.

Spec keys::

    preset   str                     key of presets.json "materials"
    params   dict                    preset params merged over the preset defaults
    paint    dict                    kind: texture|solid|linear|radial|object (+ image/color/samples/...)
    mono     None | {lo, hi, floor, tint: rgb|None, strength}      appearance luminance transform
    clear    bool                    clear appearance: paint luminance drives milkiness (white frost)
    alpha    bool                    material needs the opacity branch (object alpha / gradient alpha)
    shadow   {kind, opacity}         neutral / chromatic / none (Is-Shadow-Ray transparent wrap)
    role     refract|fake|opaque     EEVEE treatment (only top-most glass refracts; D7)
    thickness float                  EEVEE Thickness socket (object space)
    light    (x, y, z)               unit vector toward the key light (rim mask)
    bbox     (minx, miny, maxx, maxy) object-space extents (translucency falloff, dome)
    inflate  float                   0..1 dome normal on the front cap
    emission float                   extra paint-coloured emission (tinted-dark, plus-lighter blend)
    eevee_backdrop None | dict       role 'backdrop' (glass plate over the wallpaper, clear/tinted renditions):
                                     appearance.wallpaper_linear() — EEVEE shades frosted glass over it
    obj_scale float                  object scale (object coords -> world XY for the backdrop lookup)
    lit      float                   key-light level (1 = studio): scales Liquid Glass' self-lit fill
    edge_dark float                  clear-light: transmission darkened toward the outline (0 = off)
    cm       str                     colour mode (render.colorMode): 'neutral' pre-compensates paints for the
                                     Khronos PBR Neutral view transform (display_paint)
    plate    bool                    the icon plate (not a layer piece): no white-ice body (_white_ice)

Colour fidelity (round 4): paint colours are turned into the *radiance* the view transform displays as the
SVG colour (``display_paint``: inverse Khronos PBR Neutral in the 'neutral' mode, identity otherwise). Self-lit
fills (Liquid Glass body, flat) emit that radiance; diffuse presets get the albedo that the calibrated rig
(lighting.ENGINE_CAL: face-on radiance = DIFFUSE_A · albedo + DIFFUSE_B in both engines) lights to it.
"""
from __future__ import annotations

import os
from typing import Optional

import bpy

from . import presets as P
from .nodes import Graph, TopologyMismatch, auto_layout
from .util import NEUTRAL_CAP, NEUTRAL_CAP_POW, PBR_DESAT, PBR_START, clamp, log

WHITE = (1.0, 1.0, 1.0, 1.0)
SILVER = (0.92, 0.93, 0.95)
PEARL = (0.9, 0.9, 0.92)
CLAY = (0.8, 0.76, 0.72)

# presets that refract (transmission) — EEVEE needs the raytraced-refraction / fake-glass split
GLASS = {"liquid_glass", "clear_glass", "frosted_glass", "dispersive_crystal", "tinted_glass"}
CYCLES_TRANSMISSIVE = GLASS | {"jelly"}
VOLUME_PRESETS = {"jelly"}
UNLIT = {"flat"}
MONO_MIN_RANGE = 0.55
# face-on response of a diffuse (satin) surface under the calibrated studio rig, both engines:
# radiance = DIFFUSE_A · albedo + DIFFUSE_B (coat / specular reflection of the studio world)
DIFFUSE_A = 1.0
DIFFUSE_B = 0.017
ALBEDO_MAX = 1.3            # brightest diffuse albedo used to reach a paint (white plates display ~246/255)
CLEAR_WHITE_MILK = 0.6     # clear glass: frosted share of a white paint's body (readable white glyphs)
WHITE_ICE = 0.6            # frosted glass: the same for white paints (prism uses CLEAR_WHITE_MILK; _white_ice)
# brightest displayed peak a diffuse surface can reach: PBR Neutral's pre-image of that peak = ALBEDO_MAX + B
DIFFUSE_PEAK = 1.0 - (1.0 - PBR_START) ** 2 / (ALBEDO_MAX + DIFFUSE_B - (2 * PBR_START - 1.0))
LG_CAP_CLEAR = 0.16         # Liquid Glass: clear (see-through) share of the flat cap at the default translucency
LG_EDGE_BODY = 0.35         # ... body share left at the silhouette (the bevel lenses what lies beneath)
TRANSLUCENT_CLEAR = 0.3     # ... translucent pieces: how untinted their clear share is (their alpha tints already)
LG_COAT_B = 0.01            # Liquid Glass cap: radiance its coat reflects of the studio world (off the fill)
EEVEE_FILL_GAIN = 1.2       # EEVEE: body share vs Cycles (its slab refraction of the plate reads ~30 % darker)


def is_glass(preset: str) -> bool:
    return preset in GLASS


def max_frost(spec: dict) -> float:
    pr = spec.get("params", {})
    return float(pr.get("frost", 0.0) or 0.0) if spec.get("preset") in CYCLES_TRANSMISSIVE else 0.0


def uses_volume(spec: dict) -> bool:
    return spec["preset"] in VOLUME_PRESETS or (spec["preset"] == "tinted_glass"
                                                 and float(spec["params"].get("absorption", 0) or 0) > 0)


# ================================================================================================
# topology / public API
# ================================================================================================
def topology_key(spec: dict) -> str:
    pr = spec.get("params", {})
    paint = spec["paint"]
    flags = [
        "v6", spec["preset"], paint["kind"], bool(paint.get("has_alpha")), bool(paint.get("uv")),
        spec.get("cm", "neutral") == "neutral",
        paint["kind"] == "radial" and radial_focal(paint) is not None,
        spec.get("mono") is not None, bool(spec.get("mono") and spec["mono"].get("tint") is not None),
        bool(spec.get("clear")), bool(spec.get("alpha")), spec.get("role", "opaque"),
        float(spec.get("inflate") or 0) > 0, float(spec.get("edge_dark") or 0) > 0,
        len((spec.get("eevee_backdrop") or {}).get("blobs") or []) if spec.get("eevee_backdrop") else -1,
        bool(spec.get("plate")),
    ]
    if spec["preset"] == "tinted_glass":
        flags.append(float(pr.get("absorption", 0) or 0) > 0)
    if spec["preset"] == "brushed_metal":
        flags.append(pr.get("brush", "radial"))
    return "|".join(str(f) for f in flags)


def ensure(name: str, spec: dict) -> bpy.types.Material:
    """Create or update material ``name`` for ``spec`` (in place when the topology is unchanged)."""
    mat = bpy.data.materials.get(name)
    key = topology_key(spec)
    if mat is not None and mat.get("bis_key") == key and mat.node_tree is not None:
        try:
            _build(mat, spec, update=True)
            _settings(mat, spec)
            return mat
        except TopologyMismatch as ex:
            log("material topology mismatch, rebuilding", name, ex)
    if mat is None:
        mat = bpy.data.materials.new(name)
    nt = mat.node_tree
    if nt is None:  # pragma: no cover - 5.0 always creates a tree
        mat.use_nodes = True
        nt = mat.node_tree
    nt.nodes.clear()
    _build(mat, spec, update=False)
    mat["bis_key"] = key
    _settings(mat, spec)
    try:
        auto_layout(nt)
    except Exception:  # cosmetic only
        pass
    return mat


def _set_if(obj, attr: str, value) -> None:
    """Assign only on change: every RNA write tags the material for a shading update."""
    cur = getattr(obj, attr)
    try:
        same = tuple(cur) == tuple(value)
    except TypeError:
        same = cur == value
    if not same:
        setattr(obj, attr, value)


def _settings(mat: bpy.types.Material, spec: dict) -> None:
    """EEVEE material settings (D7): dithered; raytraced refraction only for the 'refract' role. Pieces with
    an opacity (element / layer opacity < 1, gradient or raster alpha) are alpha-BLENDED: dithered alpha over
    16 TAA samples left heavy speckle on every translucent overlay (QA #9); both faces of the slab are blended
    (``use_transparency_overlap``), as in Cycles."""
    role = spec.get("role", "opaque")
    blended = bool(spec.get("alpha")) and role != "refract"
    _set_if(mat, "surface_render_method", "BLENDED" if blended else "DITHERED")
    if blended:
        _set_if(mat, "use_transparency_overlap", True)
    _set_if(mat, "use_raytrace_refraction", role == "refract")
    _set_if(mat, "thickness_mode", "SLAB")
    _set_if(mat, "use_transparent_shadow", True)
    _set_if(mat, "use_backface_culling", False)
    # Cycles: self-lit fills / rims are display-calibrated colour, not light sources — no next-event estimation
    # toward their triangles (each glyph was a light in the light tree: slower and noisier previews, and a
    # white glyph lit the plate around it). Neon tubes keep lighting their surroundings.
    try:
        _set_if(mat.cycles, "emission_sampling", "AUTO" if spec["preset"] == "neon" else "NONE")
    except (AttributeError, TypeError):  # pragma: no cover - older builds
        pass
    pc = tuple(spec.get("preview_color", (0.8, 0.8, 0.8)))[:3]
    _set_if(mat, "diffuse_color", (*pc, 1.0))
    if mat.get("bis_preset") != spec["preset"]:
        mat["bis_preset"] = spec["preset"]
    if mat.get("bis_role") != role:
        mat["bis_role"] = role


# ================================================================================================
# images
# ================================================================================================
def load_image(path: str) -> Optional[bpy.types.Image]:
    if not path or not os.path.isfile(path):
        return None
    key = os.path.normcase(os.path.abspath(path))
    mtime = os.path.getmtime(path)
    for img in bpy.data.images:
        if img.get("bis_path") == key:
            if img.get("bis_mtime") != mtime:      # same path rewritten on disk (re-import, regenerated cache)
                try:
                    img.reload()
                except RuntimeError as ex:
                    log("image reload failed", path, ex)
                img["bis_mtime"] = mtime
            return img
    img = bpy.data.images.load(path, check_existing=True)
    img["bis_mtime"] = mtime
    try:
        img.colorspace_settings.name = "sRGB"
    except TypeError:
        pass
    img.alpha_mode = "CHANNEL_PACKED"   # paint colour = RGB regardless of coverage alpha
    img["bis_path"] = key
    return img


def purge_unused_images() -> int:
    n = 0
    for img in list(bpy.data.images):
        if img.get("bis_path") and img.users == 0:
            bpy.data.images.remove(img)
            n += 1
    return n


# ================================================================================================
# shared sub-graphs
# ================================================================================================
class _Ctx:
    """Per-build state: graph + cached shared sockets."""

    def __init__(self, g: Graph, spec: dict):
        self.g = g
        self.spec = spec
        self.params = spec.get("params", {})
        self._tc = None
        self._geo = None
        self._obj = None
        self._edge_s = None

    def prm(self, key: str, default: float = 0.0) -> float:
        v = self.params.get(key, default)
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    @property
    def tc(self):
        if self._tc is None:
            self._tc = self.g.node("ShaderNodeTexCoord")
        return self._tc

    @property
    def geo(self):
        if self._geo is None:
            self._geo = self.g.node("ShaderNodeNewGeometry")
        return self._geo

    @property
    def obj_info(self):
        if self._obj is None:
            self._obj = self.g.node("ShaderNodeObjectInfo")
        return self._obj


def _paint(c: _Ctx):
    """-> (colour socket, alpha socket|None). Object coordinates == art (layers) / canvas (plate)."""
    g, p = c.g, c.spec["paint"]
    kind = p["kind"]
    if kind == "texture":
        pos = c.tc.outputs["Object"]
        if p.get("uv"):
            # raster image: general affine art -> UV (rotated / skewed <image> placements, see scene.image_uv)
            (au, bu, cu), (av, bv, cv) = p["uv"]
            u = g.math("ADD", g.vmath("DOT_PRODUCT", pos, (au, bu, 0.0)), cu)
            v = g.math("ADD", g.vmath("DOT_PRODUCT", pos, (av, bv, 0.0)), cv)
            uvw = g.combine(u, v, 0.0)
        else:
            # layer textures cover the art square −1..1: u = (x+1)/2, v = (y+1)/2 (or an explicit rect)
            x0, y0, x1, y1 = p.get("rect") or (-1.0, -1.0, 1.0, 1.0)
            w, h = max(1e-6, x1 - x0), max(1e-6, y1 - y0)
            mp = g.node("ShaderNodeMapping", vector_type="POINT")
            g.set(mp.inputs["Location"], (-x0 / w, -y0 / h, 0.0))
            g.set(mp.inputs["Scale"], (1.0 / w, 1.0 / h, 1.0))
            g.link(pos, mp.inputs["Vector"])
            uvw = mp.outputs["Vector"]
        tex = g.node("ShaderNodeTexImage", extension="CLIP" if p.get("has_alpha") else "EXTEND",
                     interpolation="Linear")
        img = load_image(p.get("image", ""))
        if tex.image != img:
            tex.image = img
        g.link(uvw, tex.inputs["Vector"])
        return tex.outputs["Color"], (tex.outputs["Alpha"] if p.get("has_alpha") else None)
    if kind == "solid":
        return g.rgb(p.get("color", (1, 1, 1)), "paint"), None
    if kind == "object":
        return c.obj_info.outputs["Color"], None
    if kind in ("linear", "radial"):
        pos = c.tc.outputs["Object"]
        if kind == "linear":
            sx, sy = p.get("start", (0.0, 1.0))
            ex, ey = p.get("end", (0.0, -1.0))
            dx, dy = ex - sx, ey - sy
            l2 = dx * dx + dy * dy or 1e-9
            rel = g.vmath("SUBTRACT", pos, (sx, sy, 0.0))
            t = g.vmath("DOT_PRODUCT", rel, (dx / l2, dy / l2, 0.0))
        else:
            u1, u2, off = _radial_rows(p)
            r1 = g.math("ADD", g.vmath("DOT_PRODUCT", pos, (*u1, 0.0)), off[0])
            r2 = g.math("ADD", g.vmath("DOT_PRODUCT", pos, (*u2, 0.0)), off[1])
            n = g.combine(r1, r2, 0.0)            # position in the normalised gradient circle (centre 0, r 1)
            fn = radial_focal(p)
            if fn is None:
                t = g.vmath("LENGTH", n)
            else:
                # SVG focal gradient: t = |n − f| / s, where s is the distance from f to the unit circle
                # along the ray f -> n:  s = −b + sqrt(b² − |f|² + 1),  b = d·f,  d = (n − f)/|n − f|
                e = g.vmath("SUBTRACT", n, (fn[0], fn[1], 0.0))
                b = g.vmath("DOT_PRODUCT", g.vmath("NORMALIZE", e), (fn[0], fn[1], 0.0))
                disc = g.math("ADD", g.math("MULTIPLY", b, b), 1.0 - (fn[0] ** 2 + fn[1] ** 2))
                s = g.math("SUBTRACT", g.math("SQRT", g.math("MAXIMUM", disc, 0.0)), b)
                t = g.math("DIVIDE", g.vmath("LENGTH", e), g.math("MAXIMUM", s, 1e-6))
        ramp = g.ramp(t, p.get("samples") or [(0.0, (1, 1, 1), 1.0), (1.0, (1, 1, 1), 1.0)])
        return ramp.outputs["Color"], (ramp.outputs["Alpha"] if p.get("has_alpha") else None)
    return g.rgb((1.0, 1.0, 1.0), "paint"), None


def radial_focal(p: dict):
    """Focal point in the normalised gradient circle, or None when it coincides with the centre. Kept
    strictly inside the circle (SVG 1.1 moves an outside focal point onto the circle)."""
    f = p.get("focal")
    if not f:
        return None
    cx, cy = p.get("center", (0.0, 0.0))
    r = float(p.get("radius", 1.0)) or 1e-9
    fx, fy = (float(f[0]) - cx) / r, (float(f[1]) - cy) / r
    d = (fx * fx + fy * fy) ** 0.5
    if d < 1e-4:
        return None
    if d > 0.99:
        fx, fy = fx * 0.99 / d, fy * 0.99 / d
    return fx, fy


def _radial_rows(p: dict):
    """Radial gradient -> rows of the art->normalised-gradient affine: t = |(u1·P + o1, u2·P + o2)|."""
    cx, cy = p.get("center", (0.0, 0.0))
    r = float(p.get("radius", 1.0)) or 1e-9
    m = p.get("matrix")
    if m:   # gradient space -> art: x' = a x + c y + e ; y' = b x + d y + f   (SVG matrix order)
        a, b, cc, d, e, f = m
        det = a * d - b * cc or 1e-12
        ia, ib, ic, id_ = d / det, -b / det, -cc / det, a / det
        ie, if_ = -(ia * e + ic * f), -(ib * e + id_ * f)
    else:
        ia, ib, ic, id_, ie, if_ = 1.0, 0.0, 0.0, 1.0, 0.0, 0.0
    # q = inv(M) P ; t = |(q - c)/r|
    u1 = (ia / r, ic / r)
    u2 = (ib / r, id_ / r)
    off = ((ie - cx) / r, (if_ - cy) / r)
    return u1, u2, off


def _mono(c: _Ctx, col):
    """Appearance luminance transform: perceptual luminance stretched to floor..1, optional tint."""
    m = c.spec.get("mono")
    if not m:
        return col, None
    g = c.g
    bw = g.node("ShaderNodeRGBToBW")
    g.set(bw.inputs[0], col)
    lp = g.math("POWER", g.math("MAXIMUM", bw.outputs[0], 0.0), 1 / 2.2)
    lo, hi = float(m.get("lo", 0.0)), float(m.get("hi", 1.0))
    lo = min(lo, hi - MONO_MIN_RANGE)     # never over-stretch a narrow luminance range into black
    st = g.map_range(lp, lo, hi, float(m.get("floor", 0.25)), 1.0)
    lin = g.math("POWER", st, 2.2)
    if m.get("tint") is not None:
        out = g.mix_rgb(clamp(float(m.get("strength", 0.8))), lin, tuple(m["tint"]), blend="MULTIPLY")
    else:
        out = lin
    return out, st


def _max3(g: Graph, v):
    s = g.separate(v)
    return g.math("MAXIMUM", s.outputs[0], g.math("MAXIMUM", s.outputs[1], s.outputs[2])), s


# Shared node groups: built once per session (and rebuilt when their key changes), so each material carries a
# single group node instead of ~45 math nodes — building / updating materials in Python is the draft's cost.
GROUP_KEY = "g2"


def _node_group(name: str, key: str, inputs: list, build) -> bpy.types.NodeTree:
    ng = bpy.data.node_groups.get(name)
    if ng is not None and ng.get("bis_key") == key and ng.bl_idname == "ShaderNodeTree":
        return ng
    if ng is None or ng.bl_idname != "ShaderNodeTree":
        ng = bpy.data.node_groups.new(name, "ShaderNodeTree")
    ng.nodes.clear()
    ng.interface.clear()
    for nm, typ, default in inputs:
        sock = ng.interface.new_socket(nm, in_out="INPUT", socket_type=typ)
        if default is not None:
            sock.default_value = default
    ng.interface.new_socket("Color", in_out="OUTPUT", socket_type="NodeSocketColor")
    g = Graph(ng, prefix="g")
    gi = g.node("NodeGroupInput")
    go = g.node("NodeGroupOutput")
    g.link(build(g, gi), go.inputs[0])
    ng["bis_key"] = key
    try:
        auto_layout(ng)
    except Exception:  # cosmetic only
        pass
    return ng


def _display_graph(g: Graph, gi) -> "bpy.types.NodeSocket":
    """Inverse Khronos PBR Neutral (util.pbr_neutral_inverse), as a node graph on ``gi.outputs[0]``."""
    y0 = g.vmath("MAXIMUM", gi.outputs[0], (0.0, 0.0, 0.0))
    mx, s0 = _max3(g, y0)
    mn = g.math("MINIMUM", s0.outputs[0], g.math("MINIMUM", s0.outputs[1], s0.outputs[2]))
    mxs = g.math("MAXIMUM", mx, 1e-5)
    sat = g.math("SUBTRACT", 1.0, g.math("DIVIDE", mn, mxs), clamp=True)
    cap = g.math("ADD", NEUTRAL_CAP[0], g.math("MULTIPLY", g.math("POWER", sat, NEUTRAL_CAP_POW), NEUTRAL_CAP[1] - NEUTRAL_CAP[0]))
    npk = g.math("MINIMUM", mx, g.math("MINIMUM", cap, gi.outputs["Max Peak"]))
    y = g.vmath("SCALE", y0, None, scale=g.math("DIVIDE", npk, mxs))
    d = 1.0 - PBR_START
    peak = g.math("ADD", PBR_START - d, g.math("DIVIDE", d * d, g.math("SUBTRACT", 1.0, npk)))
    inv = g.math("ADD", g.math("MULTIPLY", g.math("SUBTRACT", peak, npk), PBR_DESAT), 1.0)
    gnp = g.math("MULTIPLY", g.math("SUBTRACT", 1.0, g.math("DIVIDE", 1.0, inv)), npk)
    x2 = g.vmath("MAXIMUM", g.vmath("SCALE", g.vmath("SUBTRACT", y, g.combine(gnp, gnp, gnp)), None, scale=inv),
                 (0.0, 0.0, 0.0))
    x1h = g.vmath("SCALE", x2, None, scale=g.math("DIVIDE", peak, g.math("MAXIMUM", npk, 1e-5)))
    x1 = g.mix_rgb(g.math("GREATER_THAN", npk, PBR_START), y, x1h)
    s1 = g.separate(x1)
    m1 = g.math("MINIMUM", s1.outputs[0], g.math("MINIMUM", s1.outputs[1], s1.outputs[2]))
    lo = g.math("SUBTRACT", g.math("MULTIPLY", g.math("SQRT", g.math("MAXIMUM", m1, 0.0)), 0.4), m1)
    off = g.mix_float(g.math("GREATER_THAN", m1, 0.04), lo, 0.04)
    return g.vmath("ADD", x1, g.combine(off, off, off))


def _albedo_graph(g: Graph, gi) -> "bpy.types.NodeSocket":
    """max(radiance − B, 0) / DIFFUSE_A, scaled down (hue kept) so its peak stays ≤ Lim."""
    b = gi.outputs["B"]
    x = g.vmath("MAXIMUM", g.vmath("SUBTRACT", gi.outputs["Radiance"], g.combine(b, b, b)), (0.0, 0.0, 0.0))
    if abs(DIFFUSE_A - 1.0) > 1e-6:
        x = g.vmath("SCALE", x, None, scale=1.0 / DIFFUSE_A)
    mx, _ = _max3(g, x)
    k = g.math("MINIMUM", g.math("DIVIDE", gi.outputs["Lim"], g.math("MAXIMUM", mx, 1e-5)), 1.0)
    return g.vmath("SCALE", x, None, scale=k)


def display_paint(c: _Ctx, col, max_peak: float = NEUTRAL_CAP[0]):
    """Radiance the colour mode's view transform displays as the paint colour ``col`` (linear sRGB).

    'neutral' (Khronos PBR Neutral) subtracts a 0.04 offset (toe: #3d3d3d displayed as 31) and compresses
    and desaturates every peak above 0.76 (white -> 240, #e84a27 -> (229, 64, 10)): its inverse is applied
    here, with the target peak capped by saturation (util.NEUTRAL_CAP) and by ``max_peak`` (diffuse
    surfaces: DIFFUSE_PEAK, so the albedo stays reachable). util.pbr_neutral_inverse is the reference
    implementation. Other colour modes: identity."""
    if c.spec.get("cm", "neutral") != "neutral":
        return col
    key = f"{GROUP_KEY}|{NEUTRAL_CAP}|{NEUTRAL_CAP_POW}|{PBR_START}|{PBR_DESAT}"
    ng = _node_group("BIS Display Paint", key, [("Color", "NodeSocketColor", None),
                                                ("Max Peak", "NodeSocketFloat", NEUTRAL_CAP[0])], _display_graph)
    n = c.g.node("ShaderNodeGroup", node_tree=ng)
    if n.node_tree != ng:
        n.node_tree = ng
    c.g.set(n.inputs[0], col)
    c.g.set(n.inputs["Max Peak"], float(max_peak))
    return n.outputs[0]


def diffuse_paint(c: _Ctx, col, b: float = DIFFUSE_B):
    """Albedo the calibrated rig lights to the displayed paint (peak capped at DIFFUSE_PEAK: a brighter target
    would exceed ALBEDO_MAX and the whole colour would be scaled darker)."""
    return _albedo(c, display_paint(c, col, DIFFUSE_PEAK), b)


def _albedo(c: _Ctx, rad, b: float = DIFFUSE_B, lim: float = ALBEDO_MAX):
    """Diffuse albedo that the calibrated rig lights to radiance ``rad`` (hue kept when capped at lim)."""
    ng = _node_group("BIS Albedo", f"{GROUP_KEY}|{DIFFUSE_A}",
                     [("Radiance", "NodeSocketColor", None), ("B", "NodeSocketFloat", 0.0),
                      ("Lim", "NodeSocketFloat", 1.0)], _albedo_graph)
    n = c.g.node("ShaderNodeGroup", node_tree=ng)
    if n.node_tree != ng:
        n.node_tree = ng
    c.g.set(n.inputs["Radiance"], rad)
    c.g.set(n.inputs["B"], float(b))
    c.g.set(n.inputs["Lim"], float(lim))
    return n.outputs[0]


def paint_radiance(c: _Ctx, col):
    """display_paint for opaque pieces. Translucent pieces (opacity / alpha paint) are blended with what lies
    beneath IN RADIANCE, so they use the transform's mid-tone approximation instead: paint + 0.04 (its
    offset), peak ≤ 1 — a linear blend of such radiances is then the (linear-light) blend of the displayed
    colours that _alpha corrects to SVG's sRGB blending. With the exact inverse a white overlay (2.5x) washed
    out what lay beneath and a black one (R = 0, no offset) darkened it ~20 % too much."""
    if not c.spec.get("alpha") or c.spec.get("cm", "neutral") != "neutral":
        return display_paint(c, col)
    return _albedo(c, col, -0.04, 1.0)


def _no_bounce(c: _Ctx):
    """1 for camera / glossy / transmission rays, 0 for diffuse bounces (Cycles): self-lit fills are display-
    calibrated colour, not light sources — a white glyph emitting 2.5x lit the dark plate around it."""
    lp = c.g.node("ShaderNodeLightPath")
    return c.g.math("SUBTRACT", 1.0, lp.outputs["Is Diffuse Ray"])


def _alpha(c: _Ctx, paint_alpha, col=None):
    """Per-SURFACE alpha of a translucent piece, so the piece composites like the SVG.

    SVG blends opacity in gamma-encoded sRGB: a 33 % black overlay keeps 0.67 of the sRGB value beneath, i.e.
    0.67^2.2 = 0.41 in linear light (a linear-light alpha of 0.33 only darkened to 0.67 — maroon lenses read
    as plate red, QA #8); a white overlay brightens less than a linear blend. With paint perceptual luminance
    L the equivalent linear-light alpha is ≈ 1 − (1 − a)^k, k = 2.2 − 1.5 L (exact for black, ≈ for white
    over mid tones). A camera ray crosses two faces of the extruded slab, so each face gets
    1 − (1 − a)^(k / 2)."""
    if not c.spec.get("alpha"):
        return None
    g = c.g
    a = c.obj_info.outputs["Alpha"]
    if paint_alpha is not None:
        a = g.math("MULTIPLY", a, paint_alpha)
    if col is None:
        return a
    bw = g.node("ShaderNodeRGBToBW")
    g.set(bw.inputs[0], col)
    lum = g.math("POWER", g.math("MAXIMUM", bw.outputs[0], 0.0), 1 / 2.2)
    half_k = g.math("SUBTRACT", 1.1, g.math("MULTIPLY", g.math("MINIMUM", lum, 1.0), 0.75))
    keep = g.math("POWER", g.math("SUBTRACT", 1.0, a, clamp=True), half_k)
    return g.math("SUBTRACT", 1.0, keep, clamp=True)


def _normal(c: _Ctx):
    """Optional dome normal on the front cap (inflate). Returns a world normal socket or None."""
    amt = float(c.spec.get("inflate") or 0.0)
    if amt <= 0:
        return None
    g = c.g
    bx = c.spec.get("bbox") or (-1, -1, 1, 1)
    cx, cy = (bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2
    r = max(1e-3, max(bx[2] - bx[0], bx[3] - bx[1]) / 2)
    nrm = c.geo.outputs["Normal"]
    nz = g.separate(nrm).outputs["Z"]
    mask = g.map_range(nz, 0.92, 0.995, 0.0, 1.0, interp="SMOOTHSTEP")
    rel = g.vmath("SUBTRACT", c.tc.outputs["Object"], (cx, cy, 0.0))
    s = g.math("MULTIPLY", mask, 0.9 * amt / r)
    flat = g.vmath("MULTIPLY", rel, (1.0, 1.0, 0.0))
    tilt = g.vmath("SCALE", flat, None, scale=s)
    return g.vmath("NORMALIZE", g.vmath("ADD", nrm, tilt))


def _rim(c: _Ctx, strength: float, weights=(1.0, 0.0, 0.0)):
    """Light-locked rim mask (glass doc §3.2) × placement band (auto / inside / outside) × strength."""
    g = c.g
    L = c.spec.get("light", (-0.45, 0.45, 0.77))
    nrm = c.geo.outputs["Normal"]
    d = g.vmath("DOT_PRODUCT", nrm, tuple(L))
    key = g.math("POWER", g.math("MAXIMUM", d, 0.0), 3.0)
    back = g.math("MULTIPLY", g.math("POWER", g.math("MAXIMUM", g.math("MULTIPLY", d, -1.0), 0.0), 3.0), 0.45)
    lw = g.node("ShaderNodeLayerWeight")
    g.set(lw.inputs["Blend"], 0.18)
    nz = g.separate(nrm).outputs["Z"]
    outside = g.map_range(nz, 0.0, 0.55, 1.0, 0.0, interp="SMOOTHSTEP")
    inside = g.math("MULTIPLY", g.map_range(nz, 0.35, 0.75, 0.0, 1.0, interp="SMOOTHSTEP"),
                    g.map_range(nz, 0.85, 0.98, 1.0, 0.0, interp="SMOOTHSTEP"))
    band = g.math("ADD", g.math("MULTIPLY", lw.outputs["Fresnel"], weights[0]),
                  g.math("ADD", g.math("MULTIPLY", inside, weights[1]), g.math("MULTIPLY", outside, weights[2])))
    return g.math("MULTIPLY", g.math("MULTIPLY", g.math("ADD", key, back), band), strength)


def _shadow_color(c: _Ctx, paint_col, alpha):
    """Transmittance seen by shadow rays (per surface, sqrt so a closed body gives the target)."""
    g = c.g
    sh = c.spec.get("shadow") or {"kind": "neutral", "opacity": 0.5}
    kind = sh.get("kind", "neutral")
    op = 0.0 if kind == "none" else clamp(float(sh.get("opacity", 0.5)))
    if c.spec["preset"] in UNLIT:
        op *= 0.6
    strength = 0.85 if c.spec["preset"] in CYCLES_TRANSMISSIVE else 0.95
    neutral = (1.0 - strength, 1.0 - strength, 1.0 - strength)
    # chromatic target: hue-normalised paint, slightly darkened
    hsv = g.node("ShaderNodeSeparateColor", mode="HSV")
    g.set(hsv.inputs[0], paint_col)
    comb = g.node("ShaderNodeCombineColor", mode="HSV")
    g.link(hsv.outputs[0], comb.inputs[0])
    g.link(hsv.outputs[1], comb.inputs[1])
    g.set(comb.inputs[2], 0.72)
    target = g.mix_rgb(1.0 if kind == "chromatic" else 0.0, neutral, comb.outputs[0])
    # a translucent piece (SVG opacity) is a film the SVG composites without any shadow: seen through it, its
    # own shadow on the plate darkened it twice (Calculator's 44 % black ÷: preview ΔE 11) — alpha² falloff
    amount = g.math("MULTIPLY", g.math("MULTIPLY", alpha, alpha), op) if alpha is not None else op
    tcol = g.mix_rgb(amount, WHITE, target)
    gam = g.node("ShaderNodeGamma")
    g.set(gam.inputs["Color"], tcol)
    g.set(gam.inputs["Gamma"], 0.5)
    return gam.outputs[0]


def _finish(c: _Ctx, surf_cycles, surf_eevee, paint_col, alpha, volume=None, thickness=True):
    """Alpha branch, extra emission, Is-Shadow-Ray wrap and the two engine outputs."""
    g = c.g
    boost = float(c.spec.get("emission") or 0.0)
    em = g.node("ShaderNodeEmission")
    g.set(em.inputs["Color"], paint_col)
    g.set(em.inputs["Strength"], boost)
    shadow_col = _shadow_color(c, paint_col, alpha)
    lp = g.node("ShaderNodeLightPath")
    tr_alpha = g.node("ShaderNodeBsdfTransparent") if alpha is not None else None
    results = []
    for surf in (surf_cycles, surf_eevee):
        s = g.add_shader(surf, em.outputs[0])
        if alpha is not None:
            s = g.mix_shader(alpha, tr_alpha.outputs[0], s)
        tr = g.node("ShaderNodeBsdfTransparent")
        g.set(tr.inputs["Color"], shadow_col)
        results.append(g.mix_shader(lp.outputs["Is Shadow Ray"], s, tr.outputs[0]))
    out_c = g.node("ShaderNodeOutputMaterial", target="CYCLES")
    out_e = g.node("ShaderNodeOutputMaterial", target="EEVEE")
    out_c.is_active_output = True
    out_e.is_active_output = True
    g.link(results[0], out_c.inputs["Surface"])
    g.link(results[1], out_e.inputs["Surface"])
    if volume is not None:
        g.link(volume, out_c.inputs["Volume"])
    if thickness:
        th = g.value(max(1e-4, float(c.spec.get("thickness", 0.1))), "thickness")
        g.link(th, out_e.inputs["Thickness"])


def _principled(c: _Ctx, values: dict, normal=None):
    g = c.g
    p = g.node("ShaderNodeBsdfPrincipled")
    if normal is not None:
        g.link(normal, p.inputs["Normal"])
        g.link(normal, p.inputs["Coat Normal"])
    g.inputs(p, values)
    return p


def _fake_glass(c: _Ctx, base, rim, normal, frost: float):
    """EEVEE stand-in for glass under other glass (visible through the top glass's raytraced refraction)."""
    return _principled(c, {
        "Base Color": base, "Roughness": clamp(0.18 + frost * 0.5, 0, 0.8), "Alpha": 1.0,
        "Coat Weight": 1.0, "Coat Roughness": 0.03, "Specular IOR Level": 0.6,
        "Emission Color": WHITE, "Emission Strength": rim,
    }, normal).outputs[0]


def _white_ice(c: _Ctx, col, normal, cyc, ev, amount: float, frost: float):
    """White paints in the colourless glass presets (frosted, prism) tint nothing: such a glyph took the colour
    of the plate it refracts and vanished (frosted / prism looks: Discord, Spotify, Settings, Brave — the
    crystal look's QA round 3 #8, which _glass_common's white_milk fixes for clear glass). A frosted-white
    body share (whiteness^1.5 × amount, a touch denser at the top) keeps it a white glyph; coloured paints
    (whiteness ≈ 0), the plate and the clear appearances are unchanged. -> (cycles, eevee) surfaces."""
    if amount <= 0 or c.spec.get("plate") or c.spec.get("clear"):
        return cyc, ev
    g = c.g
    bx = c.spec.get("bbox") or (-1, -1, 1, 1)
    y = g.separate(c.tc.outputs["Object"]).outputs["Y"]
    wm = g.math("MULTIPLY", g.math("MULTIPLY", g.math("POWER", _whiteness(c, col), 1.5), amount),
                g.map_range(y, bx[1], bx[3], 0.8, 1.0))
    ice = _principled(c, {"Base Color": col, "Roughness": max(0.3, frost), "Coat Weight": 1.0,
                          "Coat Roughness": 0.03, "Specular IOR Level": 0.5}, normal)
    cyc = g.mix_shader(wm, cyc, ice.outputs[0])
    # EEVEE: a diffuse + refraction mix renders grainy - the milky share is a soft paint-coloured emission
    # (as _glass_common's milk_em)
    em = g.node("ShaderNodeEmission")
    g.set(em.inputs["Color"], col)
    g.set(em.inputs["Strength"], 1.3)
    ev = g.mix_shader(g.math("MULTIPLY", wm, 1.3, clamp=True), ev, em.outputs[0])
    return cyc, ev


def _spec_weights(mode: str):
    return {"off": (0.0, 0.0, 0.0), "auto": (1.0, 0.0, 0.0), "inside": (0.25, 1.0, 0.0),
            "outside": (0.25, 0.0, 1.0)}.get(mode, (1.0, 0.0, 0.0))


# ================================================================================================
# preset builders: (ctx, paint colour, normal) -> (cycles surface, eevee surface, volume|None)
# ================================================================================================
def tint_curve(t: float) -> float:
    """UI tint (0..1) -> colour mix factor. Eases out so the default (0.45) keeps brand colours vivid."""
    t = clamp(float(t))
    return 1.0 - (1.0 - t) ** 4.0


def tinted(c: "_Ctx", col, amount: float):
    """mix(white, paint, amount) in perceptual (gamma 2.2) space -> linear colour socket."""
    g = c.g
    enc = g.node("ShaderNodeGamma")
    g.set(enc.inputs["Color"], col)
    g.set(enc.inputs["Gamma"], 1 / 2.2)
    mixed = g.mix_rgb(amount, WHITE, enc.outputs[0])
    dec = g.node("ShaderNodeGamma")
    g.set(dec.inputs["Color"], mixed)
    g.set(dec.inputs["Gamma"], 2.2)
    return dec.outputs[0]


def glass_colors(c: "_Ctx", col, tint: float):
    """-> (transmission colour, body colour). The transmission colour is sqrt(body) because light
    crosses two interfaces of a slab, so glass over white reads as the body (paint) colour."""
    base_m = tinted(c, col, tint_curve(tint))
    sq = c.g.node("ShaderNodeGamma")
    c.g.set(sq.inputs["Color"], base_m)
    c.g.set(sq.inputs["Gamma"], 0.5)
    return sq.outputs[0], base_m


def _glass_common(c: _Ctx, col, normal, tint: float, frost: float, ior: float, transl: float,
                  rim_strength: float, spec_mode: str, glow: float, coat: float = 1.0, coat_rough: float = 0.02,
                  milk_gain: float = 1.6, white_milk: float = 0.0):
    """Glass body = clear transmission lobe (tinted with sqrt(paint) so the two interfaces give back the
    paint colour) mixed with a milky, paint-coloured diffuse lobe. The milk fraction follows Icon
    Composer's translucency: transparent at the bottom, retains colour at the top."""
    g = c.g
    base_t, base_m = glass_colors(c, col, tint)
    rim = _rim(c, rim_strength if spec_mode != "off" else 0.0, _spec_weights(spec_mode))
    coat_w = 0.0 if spec_mode == "off" else coat
    bx = c.spec.get("bbox") or (-1, -1, 1, 1)
    y = g.separate(c.tc.outputs["Object"]).outputs["Y"]
    fall = g.map_range(y, bx[1], bx[3], 0.45, 1.0)
    milk = g.math("MULTIPLY", fall, (1.0 - transl) * milk_gain, clamp=True)
    if c.spec.get("clear") and c.spec.get("_lum") is not None:
        milk = g.math("MULTIPLY", g.math("MULTIPLY", c.spec["_lum"], 1.3), milk, clamp=True)
    elif white_milk > 0 and not c.spec.get("plate"):
        # white paint tints nothing: as water-clear glass a white glyph vanished into the plate it lenses
        # (crystal look: Discord, Calculator, Spotify). White paints become frosted crystal ("ice") instead.
        # Not on the plate: a clear-glass plate over a white fill stays clear glass
        wm = g.math("MULTIPLY", g.math("POWER", _whiteness(c, col), 1.5), white_milk)
        milk = g.math("MAXIMUM", milk, g.math("MULTIPLY", wm, g.map_range(fall, 0.45, 1.0, 0.8, 1.0)))
    common = {"Roughness": frost, "IOR": ior, "Coat Weight": coat_w, "Coat Roughness": coat_rough,
              "Coat IOR": 1.5, "Specular IOR Level": 0.6, "Emission Color": WHITE, "Emission Strength": rim}
    pt = _principled(c, {**common, "Base Color": base_t, "Transmission Weight": 1.0}, normal)
    pm = _principled(c, {**common, "Base Color": base_m, "Roughness": max(0.3, frost),
                         "Transmission Weight": 0.0}, normal)
    body = g.mix_shader(milk, pt.outputs[0], pm.outputs[0])
    # inner glow (lit from within): paint-tinted emission at the edges
    lw = g.node("ShaderNodeLayerWeight")
    g.set(lw.inputs["Blend"], 0.35)
    glow_em = g.node("ShaderNodeEmission")
    g.set(glow_em.inputs["Color"], g.mix_rgb(0.5, WHITE, col))
    g.set(glow_em.inputs["Strength"], g.math("MULTIPLY", lw.outputs["Facing"], glow))
    cyc = g.add_shader(body, glow_em.outputs[0])
    role = c.spec.get("role", "refract")
    if role == "fake":
        ev = g.add_shader(_fake_glass(c, base_m, rim, normal, frost), glow_em.outputs[0])
    else:
        # EEVEE: one refraction closure (a diffuse+refraction mix renders grainy); the milky lobe is
        # approximated by a soft paint-coloured emission
        milk_em = g.node("ShaderNodeEmission")
        g.set(milk_em.inputs["Color"], base_m)
        g.set(milk_em.inputs["Strength"], 1.3)
        ev = g.add_shader(g.mix_shader(g.math("MULTIPLY", milk, 1.3, clamp=True), pt.outputs[0], milk_em.outputs[0]),
                          glow_em.outputs[0])
    return cyc, ev, None


# Liquid Glass bands on the round bevel, by the normalised SCREEN distance from the silhouette
# e = 1 − |N.xy| (round profile seen along the view axis: 0 at the outline, 1 where the flat cap begins).
RIM_BANDS = {"auto": (0.015, 0.06, 0.24, 0.4), "inside": (0.3, 0.42, 0.6, 0.78),
             "outside": (0.0, 0.005, 0.06, 0.14)}


def _edge(c: _Ctx):
    """-> (e = 1 − |N.xy| (0 = silhouette, 1 = cap), normal socket); built once per material."""
    if c._edge_s is None:
        g = c.g
        nrm = c.geo.outputs["Normal"]
        c._edge_s = (g.math("SUBTRACT", 1.0, g.vmath("LENGTH", g.vmath("MULTIPLY", nrm, (1.0, 1.0, 0.0))),
                            clamp=True), nrm)
    return c._edge_s


def _band(c: _Ctx, e, a0, a1, b0, b1):
    g = c.g
    return g.math("MULTIPLY", g.map_range(e, a0, a1, 0.0, 1.0, interp="SMOOTHSTEP"),
                  g.map_range(e, b0, b1, 1.0, 0.0, interp="SMOOTHSTEP"))


def _light_side(c: _Ctx, nrm, sign: float = 1.0):
    """max(0, ±dot(normalize(N.xy), normalize(L.xy))): 1 on the edges facing (+1) / opposite (−1) the light."""
    g = c.g
    L = c.spec.get("light", (-0.45, 0.45, 0.77))
    lx, ly = float(L[0]), float(L[1])
    ln = (lx * lx + ly * ly) ** 0.5
    lx, ly = (lx / ln, ly / ln) if ln > 1e-3 else (0.0, 1.0)
    n2 = g.vmath("NORMALIZE", g.vmath("MULTIPLY", nrm, (1.0, 1.0, 0.0)))
    return g.math("MAXIMUM", g.math("MULTIPLY", g.vmath("DOT_PRODUCT", n2, (lx, ly, 0.0)), sign), 0.0)


def _rim_lg(c: _Ctx, strength: float, mode: str):
    """Liquid Glass specular rim: a crisp band just inside the outline, bright (clipping to white) on the
    edges facing the light (its direction projected into the icon plane), a faint counter-rim on the
    opposite side and only a hint elsewhere (Icon Composer 1.x look at the default −45°)."""
    g = c.g
    e, nrm = _edge(c)
    key = g.math("POWER", _light_side(c, nrm, 1.0), 2.0)
    back = g.math("POWER", _light_side(c, nrm, -1.0), 2.0)
    w = g.math("ADD", key, g.math("MULTIPLY", back, 0.16))
    band = _band(c, e, *RIM_BANDS.get(mode, RIM_BANDS["auto"]))
    return g.math("MULTIPLY", g.math("MULTIPLY", w, band), strength if mode != "off" else 0.0)


def _whiteness(c: _Ctx, col):
    """0..1: how white the paint is (perceptual value × (1 − saturation)). White glyphs on coloured plates
    keep a frosted-white body; saturated brand colours stay clear, vivid glass."""
    g = c.g
    hsv = g.node("ShaderNodeSeparateColor", mode="HSV")
    g.set(hsv.inputs[0], col)
    v = g.math("POWER", g.math("MAXIMUM", hsv.outputs[2], 0.0), 1 / 2.2)
    return g.math("MULTIPLY", v, g.math("SUBTRACT", 1.0, hsv.outputs[1]), clamp=True)


def _lightness(c: _Ctx, col):
    """Perceptual lightness (0..1) of a paint colour socket: luminance^(1/2.2)."""
    g = c.g
    bw = g.node("ShaderNodeRGBToBW")
    g.set(bw.inputs[0], col)
    return g.math("MINIMUM", g.math("POWER", g.math("MAXIMUM", bw.outputs[0], 0.0), 1 / 2.2), 1.0)


def _coat_only(c: _Ctx, normal, rim, coat_w):
    """Glossy clear coat + rim emission over a self-lit fill (a black, non-transmissive Principled adds
    only its dielectric reflections)."""
    p = _principled(c, {"Base Color": (0.0, 0.0, 0.0), "Roughness": 0.25, "Specular IOR Level": 0.0,
                        "Coat Weight": coat_w, "Coat Roughness": 0.02, "Coat IOR": 1.5,
                        "Emission Color": WHITE, "Emission Strength": rim}, normal)
    return p.outputs[0]


def b_liquid_glass(c: _Ctx, col, normal):
    """Apple iOS 26-style Liquid Glass (art-directed; both engines share the structure).

    * Body: the layer colour carried IN the glass, for light, mid and dark paints alike — a self-lit fill
      emitting exactly the radiance the view transform displays as the paint (display_paint), with a little
      diffuse response under a glossy coat. Translucency only *reveals* what lies beneath: a small clear
      share on the flat cap (16 % at the default 0.75, less for white glyphs) whose transmission is tinted so
      that over a white plate it, too, shows the paint. Interior colour = SVG paint within a few ΔE.
    * Pill edge: the body thins toward the outline (35 % at the silhouette) so the round bevel lenses what is
      beneath with a deeper colour (longer paths) — thin rings, bars and wedges keep their colour instead of
      reading as empty clear capsules.
    * A crisp light-locked specular rim + faint counter-rim just inside the outline, a glossy coat, and a
      soft paint-tinted inner glow where the light exits (opposite the key).
    EEVEE: the same mix with a single refraction closure (diffuse + refraction mixes render grainy)."""
    g = c.g
    tint, frost, ior = c.prm("tint", 1.0), c.prm("frost", 0.06), c.prm("ior", 1.5)
    transl, rim_amt, glow = c.prm("translucency", 0.75), c.prm("rim", 1.0), c.prm("glow", 0.35)
    mode = str(c.params.get("specular", "auto"))
    lit = float(c.spec.get("lit", 1.0))
    # body colour: paint (mixed toward white below tint 1, perceptually) -> displayed radiance
    _, base_m = glass_colors(c, col, tint)
    rad = paint_radiance(c, base_m)
    # paint lightness (perceptual): dark paints read as dark smoked glass — the light-locked rim is subdued
    # and the inner glow carries no white (black axes / dark blades rendered as pale, bright-edged glass)
    lum = _lightness(c, col)
    # (Notion's #3d3d3d faces read as a wireframe of white rims: dark paints keep only a faint sheen)
    rim = g.math("MULTIPLY", _rim_lg(c, 5.0 * rim_amt, mode),
                 g.map_range(lum, 0.1, 0.8, 0.06, 1.0, interp="SMOOTHSTEP"))
    coat_w = 0.0 if mode == "off" else 1.0
    e, nrm = _edge(c)
    bx = c.spec.get("bbox") or (-1, -1, 1, 1)
    y = g.separate(c.tc.outputs["Object"]).outputs["Y"]
    v01 = g.map_range(y, bx[1], bx[3], 0.0, 1.0)
    white = g.math("POWER", _whiteness(c, col), 1.5)
    # clear share of the flat cap: 16 % at the default translucency (0.75), ~50 % at 1; white glyphs
    # stay a dense frosted white (a third of it)
    t_cap = min(0.6, LG_CAP_CLEAR * (max(0.0, transl) / 0.75) ** 4)
    fill = g.math("SUBTRACT", 1.0, g.map_range(white, 0.0, 1.0, t_cap, 0.35 * t_cap))
    # Icon Composer's vertical falloff (a touch clearer at the bottom) and the clear pill edge
    fill = g.math("MULTIPLY", fill, g.map_range(v01, 0.0, 1.0, 0.94, 1.0))
    if c.spec.get("clear") and c.spec.get("_lum") is not None:
        fill = g.math("MULTIPLY", fill, g.math("MULTIPLY", c.spec["_lum"], 1.15), clamp=True)
    fill = g.math("MULTIPLY", fill, g.map_range(e, 0.0, 0.45, LG_EDGE_BODY, 1.0, interp="SMOOTHSTEP"), clamp=True)
    # dark smoked glass: the grazing coat / specular sheen of the bright studio world outlined every dark
    # piece in white (DJI's near-black blades, Ti84's body) - subdued for dark paints
    dk = g.map_range(lum, 0.0, 0.5, 0.35, 1.0)
    coat_b = g.math("MULTIPLY", g.math("MULTIPLY", g.map_range(e, 0.08, 0.6, 0.0, 1.0), coat_w), dk)
    spec_b = g.math("MULTIPLY", g.map_range(e, 0.08, 0.6, 0.0, 0.4), dk)
    common = {"IOR": ior, "Coat Weight": coat_b, "Coat Roughness": 0.02, "Coat IOR": 1.5,
              "Specular IOR Level": spec_b, "Emission Color": WHITE, "Emission Strength": rim}
    # clear share: per-interface tint sqrt(radiance / white plate), so over a white plate the two interfaces
    # give back the paint; deeper toward the outline (longer paths); white stays clear
    t2_body = t2 = g.vmath("MINIMUM", g.vmath("SCALE", rad, None, scale=1.0 / ALBEDO_MAX), (1.0, 1.0, 1.0))
    if c.spec.get("alpha"):
        # translucent piece (SVG opacity): its alpha already lets what lies beneath through; a fully tinted
        # clear share darkened it a second time in Cycles (Calculator's ÷, Notes' curl: preview ΔE 9-11) —
        # partly untinted. EEVEE's fake glass (alpha pieces never refract there) keeps the body colour.
        t2 = g.mix_rgb(TRANSLUCENT_CLEAR, t2, WHITE)
    deep = g.node("ShaderNodeGamma")
    g.set(deep.inputs["Color"], t2)
    g.set(deep.inputs["Gamma"], g.map_range(e, 0.0, 0.6, 0.9, 0.5))
    deep_col = deep.outputs[0]
    edge_dark = float(c.spec.get("edge_dark") or 0.0)
    if edge_dark > 0:
        # clear-light: the clear rim lenses the darker surroundings (a white frosted glyph on a pale frosted
        # plate otherwise disappears) — transmission darkened toward the outline
        k = g.map_range(e, 0.0, 0.6, 1.0 - edge_dark, 1.0, interp="SMOOTHSTEP")
        deep_col = g.mix_rgb(1.0, deep_col, g.combine(k, k, k), blend="MULTIPLY")
    pt = _principled(c, {**common, "Base Color": deep_col, "Roughness": frost, "Transmission Weight": 1.0},
                     normal)
    # self-lit fill: emission (a touch brighter at the top) + a little diffuse response, under the coat whose
    # reflection of the studio world (LG_COAT_B) is taken off the emitted radiance
    face = 1.0 + 0.45 * edge_dark       # clear-light: the frosted-white glyph face is lifted above the plate
    body = g.vmath("MAXIMUM", g.vmath("SUBTRACT", rad, (LG_COAT_B,) * 3), (0.0, 0.0, 0.0))
    fill_em = g.node("ShaderNodeEmission")
    g.set(fill_em.inputs["Color"], body)
    g.set(fill_em.inputs["Strength"], g.math("MULTIPLY", _no_bounce(c),
                                             g.map_range(v01, 0.0, 1.0, 0.95 * lit * face, 1.04 * lit * face)))
    pc = _principled(c, {"Base Color": _albedo(c, body, 0.0), "Roughness": 0.35, "Specular IOR Level": 0.0},
                     normal)
    coat = _coat_only(c, normal, rim, coat_b)
    filled = g.add_shader(g.mix_shader(0.85, pc.outputs[0], fill_em.outputs[0]), coat)
    # inner glow where the light leaves the glass (opposite the key), paint-tinted
    gband = _band(c, e, 0.3, 0.45, 0.8, 0.95)
    gdir = g.math("ADD", 0.08, g.math("MULTIPLY", _light_side(c, nrm, -1.0), 0.92))
    glow_em = g.node("ShaderNodeEmission")
    g.set(glow_em.inputs["Color"], g.mix_rgb(g.math("MULTIPLY", lum, 0.3), base_m, WHITE))
    g.set(glow_em.inputs["Strength"], g.math("MULTIPLY", g.math("MULTIPLY", gband, gdir), 1.4 * glow * lit))
    cyc = g.add_shader(g.mix_shader(fill, pt.outputs[0], filled), glow_em.outputs[0])
    role = c.spec.get("role", "refract")
    # under other glass (role 'fake') the clear part becomes EEVEE's non-refractive fake glass
    if role == "fake":
        clear = _fake_glass(c, _albedo(c, g.vmath("SCALE", t2_body, None, scale=ALBEDO_MAX)), rim, normal, frost)
    else:
        # EEVEE's slab refraction tints once (Cycles: once per interface)
        pte = _principled(c, {**common, "Base Color": deep_col,
                              "Roughness": frost, "Transmission Weight": 1.0}, normal)
        clear = pte.outputs[0]
    fill_ev = g.math("MINIMUM", g.math("MULTIPLY", fill, EEVEE_FILL_GAIN), 1.0)
    ev = g.add_shader(g.mix_shader(fill_ev, clear, g.add_shader(fill_em.outputs[0], coat)), glow_em.outputs[0])
    return cyc, ev, None


def b_clear_glass(c: _Ctx, col, normal):
    return _glass_common(c, col, normal, c.prm("tint", 0.1), c.prm("frost", 0.0), c.prm("ior", 1.5), 1.0,
                         1.2, "auto", 0.0, coat=1.0, coat_rough=0.01, white_milk=CLEAR_WHITE_MILK)


def b_frosted_glass(c: _Ctx, col, normal):
    g = c.g
    noise = g.node("ShaderNodeTexNoise")
    g.link(c.tc.outputs["Object"], noise.inputs["Vector"])
    g.inputs(noise, {"Scale": 400.0, "Detail": 2.0})
    bump = g.node("ShaderNodeBump")
    g.inputs(bump, {"Strength": c.prm("grain", 0.08), "Distance": 0.002})
    g.link(noise.outputs["Fac"], bump.inputs["Height"])
    if normal is not None:
        g.link(normal, bump.inputs["Normal"])
    tint, frost, ior = c.prm("tint", 0.25), c.prm("frost", 0.35), c.prm("ior", 1.5)
    base, base_m = glass_colors(c, col, tint)
    rim = _rim(c, 1.2)
    vals = {"Base Color": base, "Roughness": frost, "IOR": ior, "Transmission Weight": 1.0,
            "Coat Weight": c.prm("coat", 1.0), "Coat Roughness": 0.03, "Specular IOR Level": 0.5,
            "Emission Color": WHITE, "Emission Strength": rim}
    p = g.node("ShaderNodeBsdfPrincipled")
    g.link(bump.outputs["Normal"], p.inputs["Normal"])
    if normal is not None:
        g.link(normal, p.inputs["Coat Normal"])
    g.inputs(p, vals)
    if c.spec.get("role") == "fake":
        ev = _fake_glass(c, base_m, rim, normal, frost)
    else:
        ev = p.outputs[0]
    cyc, ev = _white_ice(c, col, normal, p.outputs[0], ev, WHITE_ICE, frost)
    return cyc, ev, None


def b_dispersive_crystal(c: _Ctx, col, normal):
    g = c.g
    tint, ior, disp, frost = c.prm("tint", 0.05), c.prm("ior", 1.6), c.prm("dispersion", 0.06), c.prm("frost", 0.0)
    base, base_m = glass_colors(c, col, tint)
    lobes = []
    for k, (rgb, d) in enumerate((((1, 0, 0), -disp), ((0, 1, 0), 0.0), ((0, 0, 1), disp))):
        gl = g.node("ShaderNodeBsdfGlass", distribution="MULTI_GGX")
        g.set(gl.inputs["Color"], g.mix_rgb(1.0, base, rgb, blend="MULTIPLY"))
        g.inputs(gl, {"Roughness": frost, "IOR": max(1.0, ior + d)})
        if normal is not None:
            g.link(normal, gl.inputs["Normal"])
        lobes.append(gl.outputs[0])
    split = g.add_shader(g.add_shader(lobes[0], lobes[1]), lobes[2])
    plain = g.node("ShaderNodeBsdfGlass", distribution="MULTI_GGX")
    g.set(plain.inputs["Color"], base)
    g.inputs(plain, {"Roughness": frost, "IOR": ior})
    lp = g.node("ShaderNodeLightPath")
    first = g.math("GREATER_THAN", lp.outputs["Transmission Depth"], 0.5)
    cyc_glass = g.mix_shader(first, split, plain.outputs[0])
    # crisp coat sparkle on top
    rim = _rim(c, 1.5)
    em = g.node("ShaderNodeEmission")
    g.set(em.inputs["Strength"], rim)
    cyc = g.add_shader(cyc_glass, em.outputs[0])
    # EEVEE: 3 refraction closures render as noise -> plain clear glass
    if c.spec.get("role") == "fake":
        ev = _fake_glass(c, base_m, rim, normal, frost)
    else:
        ev = _principled(c, {"Base Color": base, "Roughness": frost, "IOR": ior, "Transmission Weight": 1.0,
                             "Coat Weight": 1.0, "Coat Roughness": 0.01, "Emission Color": WHITE,
                             "Emission Strength": rim}, normal).outputs[0]
    cyc, ev = _white_ice(c, col, normal, cyc, ev, CLEAR_WHITE_MILK, frost)
    return cyc, ev, None


def b_tinted_glass(c: _Ctx, col, normal):
    g = c.g
    tint, frost, ior, absorb = c.prm("tint", 0.95), c.prm("frost", 0.04), c.prm("ior", 1.5), c.prm("absorption", 0.0)
    cyc, ev, _ = _glass_common(c, col, normal, tint, frost, ior, 1.0, 1.4, "auto", 0.0, coat=1.0, coat_rough=0.02)
    vol = None
    if absorb > 0:
        va = g.node("ShaderNodeVolumeAbsorption")
        g.set(va.inputs["Color"], col)
        g.set(va.inputs["Density"], absorb)
        vol = va.outputs[0]
    return cyc, ev, vol


def b_glossy_plastic(c: _Ctx, col, normal):
    base = diffuse_paint(c, col, 0.03)
    s = _principled(c, {"Base Color": base, "Roughness": c.prm("roughness", 0.35), "Coat Weight": c.prm("coat", 1.0),
                        "Coat Roughness": c.prm("coatRoughness", 0.03), "Specular IOR Level": 0.5,
                        "Emission Color": WHITE, "Emission Strength": _rim(c, 0.35)}, normal).outputs[0]
    return s, s, None


def b_satin(c: _Ctx, col, normal):
    # specular / sheen kept low: every face-on surface mirrors the bright studio world, and that white wash
    # desaturated saturated plates (QA #12); the coat (presets.json default 0.15) carries the gloss. The
    # albedo is the one the calibrated rig lights to the displayed paint (white, mid and dark plates alike)
    # the face-on specular takes the paint's hue: an untinted one mirrored ~0.02 of the grey studio into every
    # channel, a white floor that desaturated zero-channel brand colours (Files #e89000: blue 0 -> 20); the
    # reflection taken off the albedo (DIFFUSE_B) is tinted alike
    g = c.g
    rad = display_paint(c, col, DIFFUSE_PEAK)
    mx, _ = _max3(g, rad)
    tint = g.vmath("SCALE", rad, None, scale=g.math("DIVIDE", 1.0, g.math("MAXIMUM", mx, 1e-4)))
    base = _albedo(c, g.vmath("SUBTRACT", rad, g.vmath("SCALE", tint, None, scale=DIFFUSE_B)), 0.0)
    s = _principled(c, {"Base Color": base, "Roughness": c.prm("roughness", 0.45), "Coat Weight": c.prm("coat", 0.15),
                        "Coat Roughness": 0.06, "Specular IOR Level": 0.35, "Specular Tint": tint,
                        "Sheen Weight": 0.03, "Sheen Roughness": 0.5}, normal).outputs[0]
    return s, s, None


def b_candy(c: _Ctx, col, normal):
    s = _principled(c, {"Base Color": diffuse_paint(c, col, 0.03), "Roughness": c.prm("roughness", 0.15),
                        "Subsurface Weight": c.prm("subsurface", 1.0), "Subsurface Radius": col,
                        "Subsurface Scale": 0.08, "Transmission Weight": c.prm("transmission", 0.0),
                        "Coat Weight": c.prm("coat", 1.0), "Coat Roughness": 0.02, "IOR": 1.5,
                        "Emission Color": WHITE, "Emission Strength": _rim(c, 0.5)}, normal).outputs[0]
    return s, s, None


def _gummy(c: _Ctx, col, normal, rough: float, softness: float, coat: float):
    return _principled(c, {"Base Color": diffuse_paint(c, col, 0.02), "Roughness": rough,
                           "Subsurface Weight": 1.0,
                           "Subsurface Radius": col, "Subsurface Scale": 0.02 + softness * 0.25,
                           "Coat Weight": coat, "Coat Roughness": 0.05, "Emission Color": WHITE,
                           "Emission Strength": _rim(c, 0.4)}, normal).outputs[0]


def b_gummy(c: _Ctx, col, normal):
    s = _gummy(c, col, normal, c.prm("roughness", 0.2), c.prm("subsurfaceScale", 0.3), c.prm("coat", 0.6))
    return s, s, None


def b_jelly(c: _Ctx, col, normal):
    g = c.g
    frost, ior = c.prm("frost", 0.08), c.prm("ior", 1.35)
    surf = _principled(c, {"Base Color": WHITE, "Roughness": frost, "IOR": ior, "Transmission Weight": 1.0,
                           "Coat Weight": 1.0, "Coat Roughness": 0.03, "Emission Color": WHITE,
                           "Emission Strength": _rim(c, 1.2)}, normal).outputs[0]
    va = g.node("ShaderNodeVolumeAbsorption")
    g.set(va.inputs["Color"], col)
    g.set(va.inputs["Density"], 0.4 * c.prm("density", 8.0))
    vs = g.node("ShaderNodeVolumeScatter")
    g.set(vs.inputs["Color"], col)
    g.inputs(vs, {"Density": c.prm("scatter", 4.0), "Anisotropy": 0.3})
    vol = g.add_shader(va.outputs[0], vs.outputs[0])
    ev = _gummy(c, col, normal, 0.15, 0.6, 1.0)   # EEVEE: volumes are invisible through refraction
    return surf, ev, vol


def b_chrome(c: _Ctx, col, normal):
    g = c.g
    base = g.mix_rgb(c.prm("tint", 0.0), SILVER, col)
    s = _principled(c, {"Base Color": base, "Metallic": 1.0, "Roughness": c.prm("roughness", 0.05)},
                    normal).outputs[0]
    return s, s, None


def b_brushed_metal(c: _Ctx, col, normal):
    g = c.g
    base = g.mix_rgb(c.prm("tint", 1.0), SILVER, col)
    radial = str(c.params.get("brush", "radial")) != "linear"
    pos = c.tc.outputs["Object"]
    if radial:
        streak_coord = g.math("MULTIPLY", g.vmath("LENGTH", g.vmath("MULTIPLY", pos, (1.0, 1.0, 0.0))), 300.0)
        tan_node = g.node("ShaderNodeTangent", direction_type="RADIAL", axis="Z")
        tangent = tan_node.outputs["Tangent"]
    else:
        streak_coord = g.math("MULTIPLY", g.separate(pos).outputs["Y"], 300.0)
        tangent = g.combine(1.0, 0.0, 0.0)
    noise = g.node("ShaderNodeTexNoise", noise_dimensions="1D")
    g.set(noise.inputs["W"], streak_coord)
    g.inputs(noise, {"Scale": 1.0, "Detail": 8.0, "Roughness": 0.6})
    bump = g.node("ShaderNodeBump")
    g.inputs(bump, {"Strength": 0.12, "Distance": 0.001})
    g.link(noise.outputs["Fac"], bump.inputs["Height"])
    if normal is not None:
        g.link(normal, bump.inputs["Normal"])
    film = c.prm("film", 0.0)
    p = g.node("ShaderNodeBsdfPrincipled")
    g.link(bump.outputs["Normal"], p.inputs["Normal"])
    g.link(tangent, p.inputs["Tangent"])
    g.inputs(p, {"Base Color": base, "Metallic": 1.0, "Roughness": c.prm("roughness", 0.3),
                 "Anisotropic": c.prm("anisotropy", 0.8), "Anisotropic Rotation": 0.0,
                 "Thin Film Thickness": film, "Thin Film IOR": 2.2})
    return p.outputs[0], p.outputs[0], None


def b_matte_clay(c: _Ctx, col, normal):
    g = c.g
    base = diffuse_paint(c, g.mix_rgb(c.prm("tint", 1.0), CLAY, col), 0.01)
    s = _principled(c, {"Base Color": base, "Roughness": c.prm("roughness", 0.9), "Specular IOR Level": 0.3,
                        "Diffuse Roughness": 0.5, "Sheen Weight": c.prm("sheen", 0.15), "Sheen Roughness": 0.5},
                    normal).outputs[0]
    return s, s, None


def b_iridescent(c: _Ctx, col, normal):
    g = c.g
    base = g.mix_rgb(c.prm("tint", 0.2), PEARL, col)
    noise = g.node("ShaderNodeTexNoise")
    g.link(c.tc.outputs["Object"], noise.inputs["Vector"])
    g.inputs(noise, {"Scale": c.prm("bands", 3.0), "Detail": 2.0, "Distortion": 0.6})
    film = g.map_range(noise.outputs["Fac"], 0.25, 0.75, c.prm("filmMin", 250), c.prm("filmMax", 900), clamp=False)
    vals = {"Base Color": base, "Metallic": c.prm("metallic", 1.0), "Roughness": c.prm("roughness", 0.15)}
    pc = _principled(c, {**vals, "Thin Film Thickness": film, "Thin Film IOR": c.prm("filmIor", 1.6)}, normal)
    # EEVEE ignores thin film: rainbow coat tint driven by facing + the same noise bands
    lw = g.node("ShaderNodeLayerWeight")
    g.set(lw.inputs["Blend"], 0.5)
    hue = g.math("FRACT", g.math("ADD", g.math("MULTIPLY", lw.outputs["Facing"], 1.4), noise.outputs["Fac"]))
    rainbow = g.node("ShaderNodeCombineColor", mode="HSV")
    g.set(rainbow.inputs[0], hue)
    g.set(rainbow.inputs[1], 0.65)
    g.set(rainbow.inputs[2], 1.0)
    pe = _principled(c, {**vals, "Coat Weight": 1.0, "Coat Roughness": 0.05, "Coat Tint": rainbow.outputs[0],
                         "Specular Tint": rainbow.outputs[0]}, normal)
    return pc.outputs[0], pe.outputs[0], None


def b_neon(c: _Ctx, col, normal):
    g = c.g
    lw = g.node("ShaderNodeLayerWeight")
    g.set(lw.inputs["Blend"], 0.5)
    centre = g.math("POWER", g.math("SUBTRACT", 1.0, lw.outputs["Facing"]), 2.0)
    ecol = g.mix_rgb(g.math("MULTIPLY", centre, 0.6 * c.prm("core", 0.25)), col, WHITE)
    # strength 4 (preset default) -> emission 1.4: saturated under Khronos/AgX; the bloom carries the glow
    s = _principled(c, {"Base Color": (0.03, 0.03, 0.035), "Roughness": 0.25, "Coat Weight": 0.6,
                        "Coat Roughness": 0.05, "Emission Color": ecol,
                        "Emission Strength": 0.3 * c.prm("strength", 4.0)},
                    normal).outputs[0]
    return s, s, None


def b_flat(c: _Ctx, col, normal):
    """Unlit, colour-exact: emits the radiance the view transform displays as the paint."""
    em = c.g.node("ShaderNodeEmission")
    c.g.set(em.inputs["Color"], paint_radiance(c, col))
    c.g.set(em.inputs["Strength"], _no_bounce(c))
    return em.outputs[0], em.outputs[0], None


def _backdrop_glass(c: _Ctx, col, normal):
    """EEVEE stand-in for a frosted glass PLATE over the known wallpaper (role 'backdrop'): the wallpaper
    straight below each point (orthographic; parallax ignored), softened by the frost, filtered by the
    glass colour and slightly whitened by the frost's forward scatter, as emission; plus the pane's own
    dielectric reflection + glossy coat. No raytraced refraction, so the plate lands in the depth buffer
    and the (raytraced) glass layers above refract it — as in Cycles."""
    from . import appearance as A
    g = c.g
    wp = c.spec["eevee_backdrop"]
    frost = c.prm("frost", 0.3)
    _, base_m = glass_colors(c, col, c.prm("tint", 0.0))
    pos = c.tc.outputs["Object"]
    s = float(c.spec.get("obj_scale") or 1.0)
    pos = g.vmath("MULTIPLY", pos, (s, s, s))
    y = g.separate(pos).outputs["Y"]
    wcol = g.mix_rgb(g.map_range(y, A.WP_Y, -A.WP_Y, 0.0, 1.0), wp["top"], wp["bottom"])
    blur = 1.0 + 1.2 * frost          # rough transmission over a gap: blobs spread out
    for bx, by, br, bc in wp["blobs"]:
        d = g.vmath("DISTANCE", pos, (bx * A.WP_POS, by * A.WP_POS, 0.0))
        f = g.map_range(d, 0.0, br * A.WP_R * blur, 1.0, 0.0, interp="SMOOTHSTEP")
        wcol = g.mix_rgb(g.math("MULTIPLY", f, A.WP_MIX / (1.0 + 0.5 * frost)), wcol, bc)
    seen = g.mix_rgb(1.0, wcol, base_m, blend="MULTIPLY")
    bw = g.node("ShaderNodeRGBToBW")       # dark: the frost mixes in the grey studio light (less saturated)
    g.set(bw.inputs[0], seen)
    seen = g.mix_rgb(0.45 if wp.get("kind") == "dark" else 0.0, seen, bw.outputs[0])
    # forward scatter of the (studio-lit) frost: a whitening that scales with the scene light (dark: env 0.6)
    scatter = (0.1 + 0.35 * frost) * (0.32 if wp.get("kind") == "dark" else 1.0)
    seen = g.mix_rgb(clamp(scatter), seen, WHITE, blend="SCREEN")
    em = g.node("ShaderNodeEmission")
    g.set(em.inputs["Color"], seen)
    g.set(em.inputs["Strength"], 1.0)
    rim = _rim(c, 1.2)
    p = _principled(c, {"Base Color": (0.0, 0.0, 0.0), "Roughness": clamp(frost, 0.05, 0.8),
                        "Specular IOR Level": 0.5, "Coat Weight": 1.0, "Coat Roughness": 0.03,
                        "Emission Color": WHITE, "Emission Strength": rim}, normal)
    return g.add_shader(p.outputs[0], em.outputs[0])


BUILDERS = {
    "liquid_glass": b_liquid_glass, "clear_glass": b_clear_glass, "frosted_glass": b_frosted_glass,
    "dispersive_crystal": b_dispersive_crystal, "tinted_glass": b_tinted_glass,
    "glossy_plastic": b_glossy_plastic, "satin": b_satin, "candy": b_candy, "gummy": b_gummy,
    "jelly": b_jelly, "chrome": b_chrome, "brushed_metal": b_brushed_metal, "matte_clay": b_matte_clay,
    "iridescent": b_iridescent, "neon": b_neon, "flat": b_flat,
}


def _build(mat: bpy.types.Material, spec: dict, update: bool) -> None:
    g = Graph(mat.node_tree, update=update)
    c = _Ctx(g, spec)
    col, paint_alpha = _paint(c)
    col, lum = _mono(c, col)
    spec["_lum"] = lum
    try:
        normal = _normal(c)
        builder = BUILDERS.get(spec["preset"], b_liquid_glass)
        cyc, ev, vol = builder(c, col, normal)
        if spec.get("role") == "backdrop":
            ev = _backdrop_glass(c, col, normal)
        alpha = _alpha(c, paint_alpha, col)
        _finish(c, cyc, ev, col, alpha, vol)
    finally:
        spec.pop("_lum", None)


# ================================================================================================
# spec helpers used by scene.py / swatches.py
# ================================================================================================
def make_spec(preset: str, params: Optional[dict], paint: dict, **kw) -> dict:
    spec = {
        "preset": preset if preset in BUILDERS else "liquid_glass",
        "params": P.material_params(preset if preset in BUILDERS else "liquid_glass", params),
        "paint": paint,
        "mono": None, "clear": False, "alpha": False,
        "shadow": {"kind": "neutral", "opacity": 0.5},
        "role": "opaque", "thickness": 0.1, "light": (-0.45, 0.45, 0.77), "bbox": (-1, -1, 1, 1),
        "inflate": 0.0, "emission": 0.0, "eevee_backdrop": None, "obj_scale": 1.0, "lit": 1.0,
        "edge_dark": 0.0, "cm": "neutral",
    }
    spec.update(kw)
    if spec["role"] != "opaque" and spec["preset"] not in GLASS:
        spec["role"] = "opaque"
    if spec["role"] == "refract" and spec.get("alpha"):
        spec["role"] = "fake"          # alpha-blended in EEVEE (no raytraced refraction on blended surfaces)
    if spec["role"] == "backdrop" and not spec.get("eevee_backdrop"):
        spec["role"] = "refract"
    return spec
