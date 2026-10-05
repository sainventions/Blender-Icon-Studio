"""Shape materials (PLAN §11): ONE Principled BSDF per shape object, Cycles + EEVEE.

Every shape object (layer piece, combined body, raster card, the plate) gets its own material whose node tree is
exactly ``pre-processing → ONE Principled BSDF → Material Output (target ALL)``. The params are the Principled
schema of ``shared/presets.json`` (presets are only starting values); each maps 1:1 to a Principled input, and
the only pre-processing is the paint and a few classic texture helpers:

    Art        Texture Coordinate (Object) → Mapping → Image Texture (the rasterised layer SVG), or for fill
               overrides / plates / hand-written bundles an RGB node or a gradient (vector math → Color Ramp)
               [tinted appearances: RGB to BW → Map Range → Mix (Multiply) by the tint colour]
    Base Color = Mix(white → art, tint)            paintMode 'emission': Mix(black → art, tint)
    Emission Color = art                           paintMode 'emission' / 'base+emission'
    Specular / Coat / Sheen Tint = Mix(white → art, specularTint / coatTint / sheenTint)      (only when > 0)
    Subsurface Radius = art
    Normal = Bump(Noise(grainScale), grain)                                                   (only when > 0)
    Thin Film Thickness = Map Range(Noise, thickness · (1 ∓ filmVariation))                   (only when > 0)
    Tangent = Tangent (radial about Z)                                                        (only when > 0)
    Alpha = alpha · piece opacity [· art alpha]
    Thickness (Material Output, EEVEE refraction: sphere / slab model) = Value

No Mix/Add Shader, no Emission/Transparent/Glass/Refraction/Volume shader nodes, no Light Path tricks: Cycles
does the physics (refraction, real shadows, subsurface, thin film), EEVEE draws the same graph as a draft
(raytraced refraction where transmissive; glass seen through other glass is beyond it; Cycles is the truth).

A material is described by a plain ``spec`` dict (:func:`make_spec`) and built through :class:`nodes.Graph`, so a
value-only change (slider drag, colours, texture swap, preset switch with the same graph shape) updates the tree in
place; only a change of :func:`topology_key` rebuilds it.
"""
from __future__ import annotations

import os
from typing import Optional

import bpy
import numpy as np

from . import presets as P
from .nodes import Graph, TopologyMismatch, layout
from .util import clamp, log

WHITE = (1.0, 1.0, 1.0, 1.0)
BLACK = (0.0, 0.0, 0.0, 1.0)
SHAPE_PROP = "bis_shape"           # custom property marking materials owned by the scene builder
GRAIN_BUMP_DISTANCE = 0.002        # object units: sandblast depth at grain 1
FILM_NOISE_SCALE = 3.0             # film variation bands across the art square
MAX_NAME = 63                      # Blender ID name limit
SOFT_ALPHA = 0.25                  # a raster whose covered pixels are this often partly transparent has SOFT art alpha

# params that map 1:1 onto a Principled input (the rest is pre-processing: paint, tints, grain, film, alpha)
PRINCIPLED = {
    "metallic": "Metallic", "roughness": "Roughness", "ior": "IOR", "diffuseRoughness": "Diffuse Roughness",
    "subsurfaceWeight": "Subsurface Weight", "subsurfaceScale": "Subsurface Scale",
    "subsurfaceAnisotropy": "Subsurface Anisotropy", "specularIorLevel": "Specular IOR Level",
    "anisotropic": "Anisotropic", "anisotropicRotation": "Anisotropic Rotation", "transmission": "Transmission Weight",
    "coatWeight": "Coat Weight", "coatRoughness": "Coat Roughness", "coatIor": "Coat IOR",
    "sheenWeight": "Sheen Weight", "sheenRoughness": "Sheen Roughness", "emissionStrength": "Emission Strength",
    "thinFilmIor": "Thin Film IOR",
}
TINTS = (("specularTint", "Specular Tint"), ("coatTint", "Coat Tint"), ("sheenTint", "Sheen Tint"))
PAINT_MODES = ("base", "emission", "base+emission")


def _num(params: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(params.get(key, default))
    except (TypeError, ValueError):
        return default


def _on(params: dict, key: str) -> bool:
    return _num(params, key) > 1e-6


def paint_mode(params: dict) -> str:
    m = params.get("paintMode", "base")
    return m if m in PAINT_MODES else "base"


def is_transmissive(params: dict) -> bool:
    return _on(params, "transmission")


def max_glass_roughness(specs) -> float:
    """Roughest transmissive surface of the scene (Cycles film_transparent_roughness for transparent backdrops)."""
    return max([_num(s["params"], "roughness") for s in specs if is_transmissive(s["params"])] + [0.0])


def max_emission(specs) -> float:
    """Strongest emission of the scene (drives the compositor bloom: a render setting, not a material trick)."""
    return max([_num(s["params"], "emissionStrength") for s in specs if paint_mode(s["params"]) != "base"] + [0.0])


def shape_name(layer_name: str, element: str) -> str:
    """'BIS <layer name> / <element id>', trimmed to Blender's ID name limit (the element id is kept)."""
    tail = f" / {element}"
    head = f"BIS {layer_name}"
    if len(head) + len(tail) > MAX_NAME:
        head = head[:max(4, MAX_NAME - len(tail) - 1)] + "~"
    return (head + tail)[:MAX_NAME]


# ================================================================================================
# topology / public API
# ================================================================================================
def topology_key(spec: dict) -> str:
    """Everything that changes the SHAPE of the graph. The preset is not part of it: switching between presets whose
    params share the same zero / non-zero pattern only changes values (no EEVEE recompile)."""
    pr = spec["params"]
    paint = spec["paint"]
    return "|".join(str(f) for f in (
        "p11", paint["kind"], bool(paint.get("has_alpha")), bool(paint.get("uv")),
        paint["kind"] == "radial" and radial_focal(paint) is not None,
        spec.get("mono") is not None, paint_mode(pr),
        _on(pr, "grain"), _on(pr, "filmVariation"), _on(pr, "anisotropic"),
        *(_on(pr, k) for k, _ in TINTS),
    ))


def ensure(name: str, spec: dict) -> bpy.types.Material:
    """Create or update material ``name`` for ``spec`` (in place when the topology is unchanged)."""
    mat = bpy.data.materials.get(name)
    key = topology_key(spec)
    if mat is not None and mat.get("bis_key") == key and mat.node_tree is not None:
        try:
            _build(mat.node_tree, spec, update=True)
            _settings(mat, spec)
            return mat
        except TopologyMismatch as ex:
            log("material topology mismatch, rebuilding", name, ex)
    if mat is None:
        mat = bpy.data.materials.new(name)
    nt = mat.node_tree
    nt.nodes.clear()
    _build(nt, spec, update=False)
    layout(nt)
    mat["bis_key"] = key
    _settings(mat, spec)
    return mat


def purge_unused(keep: set) -> int:
    """Remove scene-builder materials (``SHAPE_PROP``) that are no longer used by any shape."""
    n = 0
    for m in list(bpy.data.materials):
        if m.get(SHAPE_PROP) and m.name not in keep and m.users == 0:
            bpy.data.materials.remove(m)
            n += 1
    return n


def _set_if(obj, attr: str, value) -> None:
    """Assign only on change: every RNA write tags the material for a shading update."""
    cur = getattr(obj, attr)
    try:
        same = tuple(cur) == tuple(value)
    except TypeError:
        same = cur == value
    if not same:
        setattr(obj, attr, value)


def art_alpha_is_soft(paint: dict) -> bool:
    """Is a paint's art alpha real translucency (a glow / shine raster, gradient stops below opacity 1) rather than a
    coverage mask (an opaque raster whose only partial alpha is its anti-aliased edge: iMessage's bubble, Feit's house,
    Outlook)? Rasters: the share of covered pixels (alpha > 0.02) that are partly transparent (< 0.98) is ≥ SOFT_ALPHA
    (Find Device's shine 1.0, Vanced Neon's glow 0.99; iMessage 0.02, Outlook 0.003, Feit 0). Measured once per image
    file and cached on the image (its path + mtime, :func:`load_image`)."""
    if not paint.get("has_alpha"):
        return False
    if paint.get("kind") != "texture":
        return True
    img = load_image(paint.get("image"))
    if img is None:
        return False
    mt = img.get("bis_mtime")
    if img.get("bis_soft") is not None and img.get("bis_soft_mtime") == mt:
        return float(img["bis_soft"]) >= SOFT_ALPHA
    frac = 0.0
    try:
        n = len(img.pixels)
        if int(img.channels) == 4 and n:
            px = np.empty(n, dtype=np.float32)
            img.pixels.foreach_get(px)
            a = px[3::4]
            cov = a > 0.02
            frac = float(np.count_nonzero(cov & (a < 0.98))) / max(1, int(np.count_nonzero(cov)))
    except (RuntimeError, ValueError, TypeError) as ex:
        log("art alpha probe failed", paint.get("image"), ex)
    img["bis_soft"] = frac
    img["bis_soft_mtime"] = mt
    return frac >= SOFT_ALPHA


def _settings(mat: bpy.types.Material, spec: dict) -> None:
    """Material settings (not nodes). EEVEE: transmissive surfaces use raytraced refraction through a SPHERE of the
    piece's thickness (rounded height-field bodies; read closer to Cycles than a slab); translucent ones (opacity or
    alpha < 1, soft art alpha) are alpha-BLENDED; everything else is dithered. Cycles ignores these."""
    pr = spec["params"]
    transmissive = is_transmissive(pr)
    translucent = spec.get("opacity", 1.0) * _num(pr, "alpha", 1.0) < 0.999
    # translucent shapes (piece opacity / alpha < 1, art alpha on opaque materials, SOFT art alpha on glass: shine /
    # glow rasters, gradient stops) are alpha-BLENDED. DITHERED alpha does not converge in EEVEE (a hashed per-pixel
    # pattern: Calculator's 44 % ÷ and Find Device's shine image stayed grainy even at 128 TAA samples; QA r8 #9).
    # Blended surfaces cannot raytrace: their transmission reads the light probes, namely the scene's plate probe
    # (scene.SceneBuilder._probe: the plate beneath, not the dark studio world; a translucent Contacts head rendered
    # near-black without it, QA r9 N1). That probe is a single-sample capture whose plate carries the bodies' noisy
    # transparent shadows: a FLAT blended glass piece magnifies it into blotches (iMessage / Feit / Outlook rasters,
    # round-8 review), so glass whose art alpha is only a coverage mask stays dithered + raytraced (edge-only dither)
    paint = spec["paint"]
    blended = translucent or (bool(paint.get("has_alpha")) and (not transmissive or art_alpha_is_soft(paint)))
    _set_if(mat, "surface_render_method", "BLENDED" if blended else "DITHERED")
    if blended:
        # only the front-most surface of a blended body: with overlap its back faces (the underside) were drawn over
        # the front in object order: blotchy raster glass (iMessage's bubble) without its key highlight
        _set_if(mat, "use_transparency_overlap", False)
    # spec 'raytrace' False (the plate, scene.SceneBuilder._plate): refraction from the light probes only; the surface
    # stays in EEVEE's opaque layer, which the raytraced glass above it traces against
    _set_if(mat, "use_raytrace_refraction", transmissive and not blended and bool(spec.get("raytrace", True)))
    _set_if(mat, "thickness_mode", "SPHERE" if transmissive else "SLAB")
    _set_if(mat, "use_transparent_shadow", True)
    _set_if(mat, "use_backface_culling", False)
    pc = tuple(spec.get("preview_color", (0.8, 0.8, 0.8)))[:3]
    _set_if(mat, "diffuse_color", (*pc, 1.0))
    for k, v in ((SHAPE_PROP, spec.get("shape") or mat.name), ("bis_preset", spec["preset"])):
        if mat.get(k) != v:
            mat[k] = v


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
# paint (art colour)
# ================================================================================================
class _Coords:
    """One Texture Coordinate node per material, created on first use (Object = art / canvas coordinates)."""

    def __init__(self, g: Graph):
        self.g = g
        self._tc = None

    @property
    def obj(self):
        if self._tc is None:
            self._tc = self.g.node("ShaderNodeTexCoord", "Art coordinates")
        return self._tc.outputs["Object"]


def _paint(g: Graph, paint: dict, co: _Coords):
    """-> (art colour socket, art alpha socket | None). Object coordinates == art (layers) / canvas (plate)."""
    kind = paint["kind"]
    if kind == "texture":
        pos = co.obj
        if paint.get("uv"):
            # raster image: general affine art -> UV (rotated / skewed <image> placements, see scene.image_uv)
            (au, bu, cu), (av, bv, cv) = paint["uv"]
            u = g.math("ADD", g.vmath("DOT_PRODUCT", pos, (au, bu, 0.0)), cu)
            v = g.math("ADD", g.vmath("DOT_PRODUCT", pos, (av, bv, 0.0)), cv)
            uvw = g.combine(u, v, 0.0)
        else:
            # layer textures cover the art square −1..1: u = (x+1)/2, v = (y+1)/2 (or an explicit rect)
            x0, y0, x1, y1 = paint.get("rect") or (-1.0, -1.0, 1.0, 1.0)
            w, h = max(1e-6, x1 - x0), max(1e-6, y1 - y0)
            mp = g.node("ShaderNodeMapping", "Art square -> UV", vector_type="POINT")
            g.set(mp.inputs["Location"], (-x0 / w, -y0 / h, 0.0))
            g.set(mp.inputs["Scale"], (1.0 / w, 1.0 / h, 1.0))
            g.link(pos, mp.inputs["Vector"])
            uvw = mp.outputs["Vector"]
        tex = g.node("ShaderNodeTexImage", "Art (layer SVG)", extension="CLIP" if paint.get("has_alpha") else "EXTEND",
                     interpolation="Linear")
        img = load_image(paint.get("image", ""))
        if tex.image != img:
            tex.image = img
        g.link(uvw, tex.inputs["Vector"])
        return tex.outputs["Color"], (tex.outputs["Alpha"] if paint.get("has_alpha") else None)
    if kind in ("linear", "radial"):
        pos = co.obj
        if kind == "linear":
            sx, sy = paint.get("start", (0.0, 1.0))
            ex, ey = paint.get("end", (0.0, -1.0))
            dx, dy = ex - sx, ey - sy
            l2 = dx * dx + dy * dy or 1e-9
            t = g.vmath("DOT_PRODUCT", g.vmath("SUBTRACT", pos, (sx, sy, 0.0)), (dx / l2, dy / l2, 0.0))
        else:
            u1, u2, off = _radial_rows(paint)
            r1 = g.math("ADD", g.vmath("DOT_PRODUCT", pos, (*u1, 0.0)), off[0])
            r2 = g.math("ADD", g.vmath("DOT_PRODUCT", pos, (*u2, 0.0)), off[1])
            n = g.combine(r1, r2, 0.0)            # position in the normalised gradient circle (centre 0, r 1)
            fn = radial_focal(paint)
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
        ramp = g.ramp(t, paint.get("samples") or [(0.0, (1, 1, 1), 1.0), (1.0, (1, 1, 1), 1.0)], label="Art gradient")
        return ramp.outputs["Color"], (ramp.outputs["Alpha"] if paint.get("has_alpha") else None)
    # solid (and anything unknown): one editable colour
    return g.rgb(paint.get("color", (1.0, 1.0, 1.0)), "Art colour"), None


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
    u1 = (ia / r, ic / r)
    u2 = (ib / r, id_ / r)
    off = ((ie - cx) / r, (if_ - cy) / r)
    return u1, u2, off


def _mono(g: Graph, col, mono: Optional[dict]):
    """Tinted appearances: art luminance stretched to floor..1 (linear light), multiplied by the tint colour."""
    if not mono:
        return col
    bw = g.node("ShaderNodeRGBToBW", "Art luminance")
    g.set(bw.inputs[0], col)
    lo, hi = float(mono.get("lo", 0.0)), float(mono.get("hi", 1.0))
    st = g.map_range(bw.outputs[0], lo, max(hi, lo + 1e-3), float(mono.get("floor", 0.08)), 1.0, label="Mono stretch")
    return g.mix_rgb(1.0, st, tuple(mono.get("tint") or (1.0, 1.0, 1.0)), blend="MULTIPLY", label="Mono x tint")


# ================================================================================================
# the graph
# ================================================================================================
def _build(nt: bpy.types.NodeTree, spec: dict, update: bool) -> None:
    g = Graph(nt, update=update)
    pr = spec["params"]
    mode = paint_mode(pr)
    co = _Coords(g)
    art, art_alpha = _paint(g, spec["paint"], co)
    art = _mono(g, art, spec.get("mono"))

    bsdf = g.node("ShaderNodeBsdfPrincipled", "Principled BSDF", distribution="MULTI_GGX",
                  subsurface_method="RANDOM_WALK")
    vals = {sock: _num(pr, key) for key, sock in PRINCIPLED.items()}
    # Paint: Base Color = Mix(white -> art, tint); emission-painted shapes start from black
    base = g.mix_rgb(clamp(_num(pr, "tint", 1.0)), BLACK if mode == "emission" else WHITE, art,
                     label="Art Colour Amount (tint)")
    vals["Base Color"] = base
    vals["Emission Color"] = art if mode != "base" else WHITE
    vals["Subsurface Radius"] = art
    for key, sock in TINTS:
        vals[sock] = (g.mix_rgb(clamp(_num(pr, key)), WHITE, art, label=f"{sock} (art)") if _on(pr, key) else WHITE)
    # Alpha = alpha x piece opacity [x art alpha]
    a = clamp(_num(pr, "alpha", 1.0) * float(spec.get("opacity", 1.0)))
    vals["Alpha"] = g.math("MULTIPLY", art_alpha, a, label="Alpha x art alpha") if art_alpha is not None else a
    # Thin film: thickness, or a noise-driven band of thicknesses (filmVariation)
    film = max(0.0, _num(pr, "thinFilmThickness"))
    if _on(pr, "filmVariation"):
        v = clamp(_num(pr, "filmVariation"))
        nz = g.node("ShaderNodeTexNoise", "Film variation", noise_dimensions="3D")
        g.link(co.obj, nz.inputs["Vector"])
        g.inputs(nz, {"Scale": FILM_NOISE_SCALE, "Detail": 2.0, "Distortion": 0.6})
        vals["Thin Film Thickness"] = g.map_range(nz.outputs["Fac"], 0.25, 0.75, film * (1.0 - v), film * (1.0 + v),
                                                  clamp=False, label="Film thickness range")
    else:
        vals["Thin Film Thickness"] = film
    # Grain: Noise -> Bump -> Normal (sandblasted / brushed micro-surface; the coat stays smooth)
    if _on(pr, "grain"):
        nz = g.node("ShaderNodeTexNoise", "Grain", noise_dimensions="3D")
        g.link(co.obj, nz.inputs["Vector"])
        g.inputs(nz, {"Scale": max(1.0, _num(pr, "grainScale", 400.0)), "Detail": 2.0, "Roughness": 0.5})
        bump = g.node("ShaderNodeBump", "Grain bump")
        g.inputs(bump, {"Strength": clamp(_num(pr, "grain")), "Distance": GRAIN_BUMP_DISTANCE})
        g.link(nz.outputs["Fac"], bump.inputs["Height"])
        vals["Normal"] = bump.outputs["Normal"]
    # Anisotropy: radial tangent about the object's Z axis (concentric brushing)
    if _on(pr, "anisotropic"):
        vals["Tangent"] = g.node("ShaderNodeTangent", "Radial tangent", direction_type="RADIAL", axis="Z").outputs[0]
    g.inputs(bsdf, vals)

    out = g.node("ShaderNodeOutputMaterial", "Material Output", target="ALL")
    out.is_active_output = True
    g.link(bsdf.outputs[0], out.inputs["Surface"])
    # EEVEE refraction thickness (object space; Material.thickness_mode); Cycles traces the real body
    g.link(g.value(max(1e-4, float(spec.get("thickness", 0.1))), "Thickness (EEVEE)"), out.inputs["Thickness"])


# ================================================================================================
# spec helpers used by scene.py / swatches.py
# ================================================================================================
def resolve(layer_material: Optional[dict], element_material: Optional[dict] = None) -> tuple[str, dict]:
    """(preset, full params) of a shape, via presets.resolve_material (layer material merged with the shape's own)."""
    return P.resolve_material(layer_material, element_material)


def make_spec(preset: str, params: Optional[dict], paint: dict, **kw) -> dict:
    """Material spec: ``params`` are merged over the preset's defaults (presets.json). Keys: preset, params, paint
    (kind texture|solid|linear|radial + data), opacity (piece opacity, multiplies Alpha), mono (tinted appearances:
    {lo, hi, floor, tint}), thickness (EEVEE refraction), raytrace (EEVEE raytraced refraction where transmissive;
    False = light probes only), preview_color, shape (owner tag)."""
    if preset not in P.material_ids():
        preset = "liquid_glass"
    spec = {"preset": preset, "params": P.material_params(preset, params), "paint": paint, "opacity": 1.0,
            "mono": None, "thickness": 0.1, "raytrace": True, "preview_color": (0.8, 0.8, 0.8), "shape": None}
    spec.update(kw)
    return spec
