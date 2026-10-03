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
"""
from __future__ import annotations

import os
from typing import Optional

import bpy

from . import presets as P
from .nodes import Graph, TopologyMismatch, auto_layout
from .util import clamp, log

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
        "v4", spec["preset"], paint["kind"], bool(paint.get("has_alpha")), bool(paint.get("uv")),
        paint["kind"] == "radial" and radial_focal(paint) is not None,
        spec.get("mono") is not None, bool(spec.get("mono") and spec["mono"].get("tint") is not None),
        bool(spec.get("clear")), bool(spec.get("alpha")), spec.get("role", "opaque"),
        float(spec.get("inflate") or 0) > 0,
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
    """EEVEE material settings (D7): dithered; raytraced refraction only for the 'refract' role."""
    role = spec.get("role", "opaque")
    _set_if(mat, "surface_render_method", "DITHERED")
    _set_if(mat, "use_raytrace_refraction", role == "refract")
    _set_if(mat, "thickness_mode", "SLAB")
    _set_if(mat, "use_transparent_shadow", True)
    _set_if(mat, "use_backface_culling", False)
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


def _alpha(c: _Ctx, paint_alpha):
    if not c.spec.get("alpha"):
        return None
    a = c.obj_info.outputs["Alpha"]
    if paint_alpha is not None:
        a = c.g.math("MULTIPLY", a, paint_alpha)
    return a


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
    amount = g.math("MULTIPLY", alpha, op) if alpha is not None else op
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
                  milk_gain: float = 1.6):
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


def b_liquid_glass(c: _Ctx, col, normal):
    return _glass_common(c, col, normal, c.prm("tint", 0.45), c.prm("frost", 0.12), c.prm("ior", 1.45),
                         c.prm("translucency", 0.6), c.prm("rim", 0.8) * 4.5,
                         str(c.params.get("specular", "auto")), c.prm("glow", 0.0))


def b_clear_glass(c: _Ctx, col, normal):
    return _glass_common(c, col, normal, c.prm("tint", 0.1), c.prm("frost", 0.0), c.prm("ior", 1.5), 1.0,
                         1.2, "auto", 0.0, coat=1.0, coat_rough=0.01)


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
    return p.outputs[0], ev, None


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
    s = _principled(c, {"Base Color": col, "Roughness": c.prm("roughness", 0.35), "Coat Weight": c.prm("coat", 1.0),
                        "Coat Roughness": c.prm("coatRoughness", 0.03), "Specular IOR Level": 0.5,
                        "Emission Color": WHITE, "Emission Strength": _rim(c, 0.35)}, normal).outputs[0]
    return s, s, None


def b_satin(c: _Ctx, col, normal):
    s = _principled(c, {"Base Color": col, "Roughness": c.prm("roughness", 0.45), "Coat Weight": c.prm("coat", 0.25),
                        "Coat Roughness": 0.06, "Specular IOR Level": 0.45, "Sheen Weight": 0.08,
                        "Sheen Roughness": 0.5}, normal).outputs[0]
    return s, s, None


def b_candy(c: _Ctx, col, normal):
    s = _principled(c, {"Base Color": col, "Roughness": c.prm("roughness", 0.15),
                        "Subsurface Weight": c.prm("subsurface", 1.0), "Subsurface Radius": col,
                        "Subsurface Scale": 0.08, "Transmission Weight": c.prm("transmission", 0.0),
                        "Coat Weight": c.prm("coat", 1.0), "Coat Roughness": 0.02, "IOR": 1.5,
                        "Emission Color": WHITE, "Emission Strength": _rim(c, 0.5)}, normal).outputs[0]
    return s, s, None


def _gummy(c: _Ctx, col, normal, rough: float, softness: float, coat: float):
    return _principled(c, {"Base Color": col, "Roughness": rough, "Subsurface Weight": 1.0,
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
    base = g.mix_rgb(c.prm("tint", 1.0), CLAY, col)
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
    em = c.g.node("ShaderNodeEmission")
    c.g.set(em.inputs["Color"], col)
    c.g.set(em.inputs["Strength"], 1.0)
    return em.outputs[0], em.outputs[0], None


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
        alpha = _alpha(c, paint_alpha)
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
        "inflate": 0.0, "emission": 0.0,
    }
    spec.update(kw)
    if spec["role"] != "opaque" and spec["preset"] not in GLASS:
        spec["role"] = "opaque"
    return spec
