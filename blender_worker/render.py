"""Render settings (PLAN §6 tiers), stills with ``render_stats`` progress, compositor bloom, animations.

Tiers (OptiX everywhere, OptiX denoiser, persistent data off):

| tier    | engine | size | samples | bounces max/trans/transp/glossy/diffuse |
|---------|--------|------|---------|------------------------------------------|
| draft   | EEVEE  | 512  | 16 TAA  | raytracing SCREEN, trace_max_roughness 0.2 (glass plate over a light wallpaper 0.3), overscan |
| preview | Cycles | 512  | 48      | 16/16/16/6/2, adaptive 0.05             |
| final   | Cycles | 1024 | 384     | 32/32/32/8/4, adaptive 0.01 (min 64)    |
| ultra   | Cycles | 2048 | 1024    | 32/32/32/8/4, adaptive 0.005            |
"""
from __future__ import annotations

import math
import os
import re
import threading
import time
from typing import Callable, Optional

import bpy

from . import presets as P
from .gpu import configure_scene, info as gpu_info
from .util import log

TIERS = {
    "draft": {"engine": "BLENDER_EEVEE", "size": 512, "samples": 16},
    "preview": {"engine": "CYCLES", "size": 512, "samples": 48, "adaptive": 0.05, "min": 0,
                "bounces": (16, 16, 16, 6, 2)},
    "final": {"engine": "CYCLES", "size": 1024, "samples": 384, "adaptive": 0.01, "min": 64,
              "bounces": (32, 32, 32, 8, 4)},
    "ultra": {"engine": "CYCLES", "size": 2048, "samples": 1024, "adaptive": 0.005, "min": 64,
              "bounces": (32, 32, 32, 8, 4)},
}
MAX_SIZE = 4096
TRACE_MAX_ROUGHNESS = 0.2          # drafts: rougher surfaces read the light probes (scene._probe), smoother ones trace
BLOOM_THRESHOLD = 0.35
EEVEE_FILTER = 1.15
ProgressFn = Callable[[float, str], None]


def tier(quality: str) -> dict:
    t = dict(TIERS.get(quality) or TIERS["draft"])
    q = P.quality(quality) if quality in TIERS else {}
    t["size"] = int(q.get("size", t["size"]))
    t["samples"] = int(q.get("samples", t["samples"]))
    if "adaptiveThreshold" in q:
        t["adaptive"] = float(q["adaptiveThreshold"])
    t["name"] = quality if quality in TIERS else "draft"
    return t


def configure(scene: bpy.types.Scene, quality: str, size: Optional[int], *, transparent: bool,
              color_mode: str = "neutral", max_glass_roughness: float = 0.0,
              samples: Optional[int] = None, trace_max_roughness: Optional[float] = None) -> dict:
    """Apply a quality tier + output settings. Returns the effective settings. ``max_glass_roughness``: the
    roughest transmissive surface (a transparent backdrop keeps glass up to it see-through). ``trace_max_roughness``
    (EEVEE; scene info 'traceMaxRoughness'): None = TRACE_MAX_ROUGHNESS."""
    t = tier(quality)
    px = int(size or t["size"])
    px = max(16, min(MAX_SIZE, px))
    r = scene.render
    r.engine = t["engine"]
    r.resolution_x = px
    r.resolution_y = px
    r.resolution_percentage = 100
    r.pixel_aspect_x = r.pixel_aspect_y = 1.0
    r.film_transparent = bool(transparent)
    r.use_persistent_data = False                  # D4
    r.use_border = False
    im = r.image_settings
    im.media_type = "IMAGE"
    im.file_format = "PNG"
    im.color_mode = "RGBA"
    im.color_depth = "8"
    im.compression = 15
    cm = P.color_mode(color_mode)
    vs = scene.view_settings
    scene.display_settings.display_device = "sRGB"
    try:
        vs.view_transform = cm.get("viewTransform", "Khronos PBR Neutral")
        vs.look = cm.get("look", "None")
    except TypeError as ex:
        log("colour mode fallback:", ex)
        vs.view_transform = "Standard"
        vs.look = "None"
    vs.exposure = 0.0
    vs.gamma = 1.0
    spp = int(samples or t["samples"])
    if t["engine"] == "CYCLES":
        configure_scene(scene)                     # GPU + OptiX denoiser (D5)
        cy = scene.cycles
        cy.samples = spp
        cy.use_adaptive_sampling = True
        cy.adaptive_threshold = t.get("adaptive", 0.05)
        cy.adaptive_min_samples = min(spp, t.get("min", 0))
        cy.use_denoising = True
        mb, tb, tpb, gb, db = t["bounces"]
        cy.max_bounces = mb
        cy.transmission_bounces = tb
        cy.transparent_max_bounces = tpb
        cy.glossy_bounces = gb
        cy.diffuse_bounces = db
        cy.volume_bounces = 0                      # no volume shaders (PLAN §11: one Principled BSDF)
        # physical glass (PLAN §11): light reaches what lies beneath / behind glass through it; without caustic paths
        # every glass body cast a black shadow and read as dark smoked glass. Plain path-traced caustics (filter
        # flags, no kernel feature); MNEE (shadow caustics, a 164 s OptiX kernel compile) stays off
        cy.caustics_reflective = True
        cy.caustics_refractive = True
        preview = t["name"] == "preview"
        # preview: filter-glossy + albedo-guided denoise remove the glass "sparkle" at 48 spp (the normal
        # pass made the OptiX denoiser keep it); finals have the samples for full normal guidance
        cy.blur_glossy = 2.0 if preview else 0.5
        cy.sample_clamp_indirect = 5.0 if preview else 10.0
        cy.denoising_input_passes = "RGB_ALBEDO" if preview else "RGB_ALBEDO_NORMAL"
        cy.sample_clamp_direct = 0.0
        cy.film_transparent_glass = bool(transparent)
        cy.film_transparent_roughness = max(0.1, min(1.0, max_glass_roughness + 0.05))
        cy.pixel_filter_type = "BLACKMAN_HARRIS"
        cy.filter_width = 1.5 if t["name"] != "preview" else 1.3
        cy.use_auto_tile = True
        cy.tile_size = 1024 if px >= 2048 else 2048
        cy.seed = 0
        cy.use_light_tree = True
    else:
        # a narrower reconstruction filter (default 1.5 px): drafts blurred thin dark detail lines (Ti84's screen)
        # well past the SVG's own anti-aliasing (Cycles previews use a 1.3 px Blackman-Harris)
        r.filter_size = EEVEE_FILTER
        ee = scene.eevee
        ee.taa_render_samples = spp
        ee.use_raytracing = True
        ee.ray_tracing_method = "SCREEN"
        rto = ee.ray_tracing_options
        rto.resolution_scale = "1" if px <= 512 else "2"
        # rougher surfaces (satin plates 0.45, frosted glass 0.27) read the light probes, the plate probe
        # (scene._probe) / the world, instead of noisy screen traces: smoother plates, draft-vs-preview glyph dE
        # 12.3 -> 11.9 on 16 icons (round 8), and a little faster; clear glass / coats (≤ 0.05) still trace.
        # A glass plate over a light wallpaper (clear-light / tinted-light) traces up to 0.3: the frosted clear glyphs
        # (0.22) refract the plate on screen (glyph and plate within 3 L* of Cycles, round 9; at 0.2 the plate read
        # 9 L* darker)
        rto.trace_max_roughness = float(TRACE_MAX_ROUGHNESS if trace_max_roughness is None else trace_max_roughness)
        rto.screen_trace_quality = 0.25
        rto.screen_trace_thickness = 0.2
        rto.use_denoise = True
        ee.use_fast_gi = True
        ee.fast_gi_method = "GLOBAL_ILLUMINATION"
        ee.use_shadows = True
        ee.shadow_ray_count = 2
        ee.shadow_step_count = 8
        ee.use_overscan = True
        ee.overscan_size = 3.0
        ee.clamp_surface_indirect = 10.0
    return {"engine": t["engine"], "quality": t["name"], "size": px, "samples": spp}


# ------------------------------------------------------------------------------------------------
# compositor: neon bloom + the 'brand' highlight soft clip (5.0 API: compositing_node_group + NodeGroupOutput;
# Glare options are sockets)
# ------------------------------------------------------------------------------------------------
def _soft_clip_nodes(ng, image_socket, knee: float, chain: list):
    """Per-channel highlight roll-off (util.soft_clip) on a scene-linear colour socket -> vector socket:
    min(x, k) + (1 - k)(1 - exp(-max(x - k, 0) / (1 - k)))."""
    w = max(1e-4, 1.0 - knee)

    def vm(op, a, b=None, c=None):
        n = ng.nodes.new("ShaderNodeVectorMath")
        n.operation = op
        for k, v in enumerate((a, b, c)):
            if v is None:
                continue
            if isinstance(v, (tuple, list)):
                n.inputs[k].default_value = v
            else:
                ng.links.new(v, n.inputs[k])
        chain.append(n)
        return n
    d = vm("MAXIMUM", vm("SUBTRACT", image_socket, (knee,) * 3).outputs[0], (0.0, 0.0, 0.0))
    t = vm("SCALE", d.outputs[0])
    t.inputs["Scale"].default_value = -1.0 / w
    ex = vm("POWER", (math.e,) * 3, t.outputs[0])                  # exp(-(x - k) / (1 - k))
    lo = vm("MINIMUM", image_socket, (knee,) * 3)
    hi = vm("MULTIPLY_ADD", ex.outputs[0], (-w,) * 3, (w,) * 3)     # (1 - k)(1 - exp(...))
    return vm("ADD", lo.outputs[0], hi.outputs[0]).outputs[0]


def configure_compositor(scene: bpy.types.Scene, bloom: float, transparent: bool, soft_clip: float = 0.0) -> None:
    """Neon bloom: Glare('Bloom') on the *Emission* pass only (a bright white plate must not glow), added
    onto the image. With a transparent film the glow would fall on alpha = 0 pixels and be lost in the
    PNG, so its luminance is folded into the alpha (premultiplied, so it composites correctly).

    ``soft_clip`` (knee, 'brand' colour mode): the highlight roll-off of util.soft_clip, applied last (after the
    bloom) to the scene-linear image; the Standard view transform then encodes it. Alpha is kept."""
    vl = scene.view_layers[0]
    bloom_on = bloom > 0.0
    clip = float(soft_clip or 0.0)
    clip = clip if 0.0 < clip < 1.0 else 0.0
    if not bloom_on and not clip:
        if scene.render.use_compositing:
            scene.render.use_compositing = False
        if vl.use_pass_emit:
            vl.use_pass_emit = False
        return
    if vl.use_pass_emit != bloom_on:
        vl.use_pass_emit = bloom_on
    name = (("BIS Bloom" if bloom_on else "BIS Comp") + (" Alpha" if bloom_on and transparent else "")
            + (f" Clip {clip:.4f}" if clip else ""))
    ng = bpy.data.node_groups.get(name)
    if ng is None:
        ng = bpy.data.node_groups.new(name, "CompositorNodeTree")
        ng.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
        rl = ng.nodes.new("CompositorNodeRLayers")
        out = ng.nodes.new("NodeGroupOutput")
        chain = [rl]
        image, alpha = rl.outputs["Image"], None
        if bloom_on:
            gl = ng.nodes.new("CompositorNodeGlare")
            gl.name = "BIS Glare"
            gl.inputs["Type"].default_value = "Bloom"
            gl.inputs["Quality"].default_value = "High"
            add = ng.nodes.new("ShaderNodeMix")
            add.data_type = "RGBA"
            add.blend_type = "ADD"
            add.inputs[0].default_value = 1.0
            ng.links.new(rl.outputs["Emission"], gl.inputs["Image"])
            ng.links.new(image, add.inputs[6])
            ng.links.new(gl.outputs["Glare"], add.inputs[7])
            chain += [gl, add]
            image = add.outputs[2]
            if transparent:
                bw = ng.nodes.new("CompositorNodeRGBToBW")
                sub = ng.nodes.new("ShaderNodeMath")        # drop the faint far haze (no veil over the frame)
                sub.name = "BIS Haze"
                sub.operation = "SUBTRACT"
                mul = ng.nodes.new("ShaderNodeMath")
                mul.name = "BIS Haze Gain"
                mul.operation = "MULTIPLY"
                mul.use_clamp = True
                mx = ng.nodes.new("ShaderNodeMath")
                mx.operation = "MAXIMUM"
                ng.links.new(gl.outputs["Glare"], bw.inputs["Image"])
                ng.links.new(bw.outputs[0], sub.inputs[0])
                ng.links.new(sub.outputs[0], mul.inputs[0])
                ng.links.new(rl.outputs["Alpha"], mx.inputs[0])
                ng.links.new(mul.outputs[0], mx.inputs[1])
                chain += [bw, sub, mul, mx]
                alpha = mx.outputs[0]
        if clip:
            image = _soft_clip_nodes(ng, image, clip, chain)
            if alpha is None:
                alpha = rl.outputs["Alpha"]          # the vector math drops alpha: give the film's back
        if alpha is not None:
            sa = ng.nodes.new("CompositorNodeSetAlpha")
            sa.inputs["Type"].default_value = "Replace Alpha"
            ng.links.new(image, sa.inputs["Image"])
            ng.links.new(alpha, sa.inputs["Alpha"])
            chain.append(sa)
            image = sa.outputs["Image"]
        ng.links.new(image, out.inputs[0])
        for i, n in enumerate(chain + [out]):
            n.location = (i * 220, 0)
    if bloom_on:
        # icon framing (QA round 3 #8): the glow hugs the tubes instead of spilling far past the plate: only the
        # bright cores bloom (threshold), over a short reach (size), and the faint far haze is not folded into alpha
        gl = ng.nodes["BIS Glare"]
        gl.inputs["Threshold"].default_value = BLOOM_THRESHOLD
        gl.inputs["Strength"].default_value = 0.3 + 0.9 * bloom
        gl.inputs["Size"].default_value = 0.2 + 0.3 * bloom
        if "BIS Haze" in ng.nodes:
            ng.nodes["BIS Haze"].inputs[1].default_value = 0.1
            ng.nodes["BIS Haze Gain"].inputs[1].default_value = 1.6
    if scene.compositing_node_group != ng:
        scene.compositing_node_group = ng
    if not scene.render.use_compositing:
        scene.render.use_compositing = True


# ------------------------------------------------------------------------------------------------
# progress via render_stats
# ------------------------------------------------------------------------------------------------
_SAMPLE_RE = re.compile(r"Sample\s+(\d+)\s*/\s*(\d+)")
_EEVEE_RE = re.compile(r"Rendering\s+(\d+)\s*/\s*(\d+)\s+samples")


_MEM_RE = re.compile(r"(?:Mem|Peak):\s*([\d.]+)([KMG])")


class _Progress:
    """render_stats handler: progress events (throttled) + peak render memory (Cycles 'Mem'/'Peak')."""

    def __init__(self, fn: Optional[ProgressFn], lo: float = 0.0, hi: float = 1.0):
        self.fn = fn
        self.lo, self.hi = lo, hi
        self.last = 0.0
        self.mem_peak_mb = 0.0
        self.lock = threading.Lock()

    def __call__(self, stats, *_):
        if not isinstance(stats, str):
            return
        for val, unit in _MEM_RE.findall(stats):
            mb = float(val) * {"K": 1 / 1024, "M": 1.0, "G": 1024.0}[unit]
            self.mem_peak_mb = max(self.mem_peak_mb, mb)
        if self.fn is None:
            return
        m = _SAMPLE_RE.search(stats) or _EEVEE_RE.search(stats)
        if not m:
            return
        n, tot = int(m.group(1)), max(1, int(m.group(2)))
        now = time.perf_counter()
        with self.lock:
            if now - self.last < 0.1 and n < tot:
                return
            self.last = now
        try:
            self.fn(self.lo + (self.hi - self.lo) * min(1.0, n / tot), stats.split("|")[-1].strip())
        except Exception:  # progress must never break a render
            pass


class RenderStats(float):
    """Seconds (a float, for backwards compatibility) carrying ``mem_peak_mb``."""
    mem_peak_mb: float = 0.0


def render_still(scene: bpy.types.Scene, out: str, progress: Optional[ProgressFn] = None,
                 lo: float = 0.0, hi: float = 1.0) -> RenderStats:
    """Render the current scene to ``out`` (PNG). Returns seconds (+ ``.mem_peak_mb``)."""
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    scene.render.filepath = out
    scene.render.use_file_extension = False
    handler = _Progress(progress, lo, hi)
    bpy.app.handlers.render_stats.append(handler)
    t0 = time.perf_counter()
    try:
        bpy.ops.render.render(write_still=True)
    finally:
        try:
            bpy.app.handlers.render_stats.remove(handler)
        except ValueError:
            pass
    dt = RenderStats(time.perf_counter() - t0)
    dt.mem_peak_mb = round(handler.mem_peak_mb, 1)
    if not os.path.isfile(out):
        raise RuntimeError(f"render produced no file: {out}")
    return dt


def device_label(engine: str) -> str:
    return gpu_info().get("device", "NONE") if engine == "CYCLES" else "OPENGL"


# ------------------------------------------------------------------------------------------------
# animations: per-frame scene overrides (fast incremental rebuild) -> PNG frames -> optional MP4
# ------------------------------------------------------------------------------------------------
ANIM_KINDS = ("turntable", "tilt", "float", "light-sweep", "iso", "explode")


def frame_overrides(kind: str, t: float, base_camera: dict, base_angle: float, layer_ids: list) -> dict:
    """t in [0, 1); the animation loops seamlessly."""
    s, c = math.sin(2 * math.pi * t), math.cos(2 * math.pi * t)
    persp = {"view": "perspective", "fov": float(base_camera.get("fov", 30.0) or 30.0)}
    if kind == "turntable":
        return {"camera": {**persp, "tiltX": 8.0, "tiltY": 360.0 * t}}
    if kind == "tilt":
        return {"camera": {**persp, "tiltX": 11.0 * s, "tiltY": 15.0 * c}}
    if kind == "float":
        dz = {lid: 0.035 * math.sin(2 * math.pi * t + i * 0.9) for i, lid in enumerate(layer_ids)}
        return {"camera": {**persp, "tiltX": 4.0 * s, "tiltY": 6.0 * c}, "layerZ": dz}
    if kind == "light-sweep":
        return {"lightAngle": base_angle + 80.0 * s}
    if kind in ("iso", "explode"):
        # CAD-style POV (PLAN §11): head-on -> isometric -> head-on, real z distances (camera.iso; 'explode' is the
        # legacy alias; layers are never spread apart)
        return {"camera": {"view": "front", "iso": 0.5 - 0.5 * c}}
    raise ValueError(f"unknown animation kind {kind!r}")


def encode_mp4(frames: list[str], out: str, fps: int, size: int, background: tuple = (0.11, 0.11, 0.13)) -> str:
    """PNG frames -> H.264 MP4 using Blender's sequencer + FFMPEG (frames composited over ``background``,
    display-referred sRGB). Blender expands '#' in movie output paths to frame numbers, so such a target
    is encoded under a temporary name and moved into place."""
    final = os.path.abspath(out)
    if "#" in final:
        import tempfile
        fd, out = tempfile.mkstemp(suffix=".mp4", prefix="bis_anim_")
        os.close(fd)
        os.remove(out)
    old = bpy.context.window.scene if bpy.context.window else bpy.context.scene
    enc = bpy.data.scenes.new("BIS Encode")
    try:
        enc.render.resolution_x = size
        enc.render.resolution_y = size
        enc.render.resolution_percentage = 100
        enc.render.fps = int(fps)
        enc.frame_start = 1
        enc.frame_end = len(frames)
        se = enc.sequence_editor_create()
        strips = se.strips if hasattr(se, "strips") else se.sequences
        col = strips.new_effect("bg", "COLOR", channel=1, frame_start=1, length=len(frames))
        col.color = background[:3]
        img = strips.new_image("frames", frames[0], channel=2, frame_start=1)
        for f in frames[1:]:
            img.elements.append(os.path.basename(f))
        img.blend_type = "ALPHA_OVER"
        img.frame_final_duration = len(frames)
        im = enc.render.image_settings
        im.media_type = "VIDEO"
        im.file_format = "FFMPEG"
        ff = enc.render.ffmpeg
        ff.format = "MPEG4"
        ff.codec = "H264"
        ff.constant_rate_factor = "HIGH"
        ff.ffmpeg_preset = "GOOD"
        ff.gopsize = max(1, int(fps))
        enc.render.use_sequencer = True
        enc.render.use_compositing = False
        enc.render.filepath = out
        enc.render.use_file_extension = False
        enc.view_settings.view_transform = "Standard"
        with bpy.context.temp_override(scene=enc):
            bpy.ops.render.render(animation=True, scene=enc.name)
    finally:
        bpy.data.scenes.remove(enc)
        if bpy.context.window:
            bpy.context.window.scene = old
    if not os.path.isfile(out):
        raise RuntimeError("FFMPEG encode produced no file")
    if out != final:
        import shutil
        shutil.move(out, final)
    return final
