"""Command dispatcher shared by the persistent worker (worker.py) and one-shot jobs (oneshot.py).

Protocol (PLAN §7): ``{"id", "cmd", "args"}`` → progress events, then exactly one ``done`` / ``error``.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback
from typing import Callable, Optional

import bpy

from . import VERSION, gpu, heightfield, materials
from . import presets as P
from . import render as R
from . import swatches as SW
from .defaults import APPEARANCE_IDS, QUALITIES, norm_project
from .scene import SceneBuilder
from .util import hex_to_srgb, log

Emit = Callable[[dict], None]


class CommandError(Exception):
    """A user-facing error (bad arguments etc.)."""


class Context:
    def __init__(self, root: str, mode: str = "worker"):
        self.root = root
        self.mode = mode
        self.builder = SceneBuilder()
        self.started = time.time()
        self.warmup_seconds = 0.0
        self.renders = 0
        self._bundles: dict[str, tuple[float, dict]] = {}
        self.shutdown = False

    # ---------------------------------------------------------------------------------------------
    def bundle(self, args: dict) -> dict:
        if isinstance(args.get("geometry"), dict):
            return args["geometry"]
        path = args.get("geometryPath")
        if not path:
            raise CommandError("missing 'geometryPath' (or inline 'geometry')")
        if not os.path.isfile(path):
            raise CommandError(f"geometry bundle not found: {path}")
        mt = os.path.getmtime(path)
        hit = self._bundles.get(path)
        if hit and hit[0] == mt:
            return hit[1]
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if len(self._bundles) > 16:
            self._bundles.clear()
        self._bundles[path] = (mt, data)
        return data


def _project(args: dict) -> dict:
    p = args.get("project")
    if isinstance(p, str):           # path to project.json
        if not os.path.isfile(p):
            raise CommandError(f"project file not found: {p}")
        with open(p, encoding="utf-8") as fh:
            p = json.load(fh)
    if not isinstance(p, dict):
        raise CommandError("missing 'project'")
    rs = p.get("render") if isinstance(p.get("render"), dict) else {}
    proj = norm_project(p)
    # a project without a colour mode renders in the worker's default (P.DEFAULT_COLOR_MODE, 'brand'); unknown
    # modes fall back to it too (models.RenderSettings' own default is still 'neutral')
    proj["render"]["colorMode"] = P.color_mode_id(rs.get("colorMode"))
    return proj


def _color_mode(project: dict) -> str:
    return P.color_mode_id((project.get("render") or {}).get("colorMode"))


def _appearance(args: dict, project: dict) -> str:
    appearance = args.get("appearance") or project.get("appearance", "light")
    if appearance not in APPEARANCE_IDS:
        raise CommandError(f"unknown appearance {appearance!r}")
    return appearance


def _quality(args: dict, project: dict, default: Optional[str] = None) -> str:
    quality = args.get("quality") or default or project["render"].get("quality", "draft")
    if quality not in QUALITIES:
        raise CommandError(f"unknown quality {quality!r}")
    return quality


def _backdrop(project: dict, args: dict) -> str:
    backdrop = project["render"].get("backdrop", "transparent")
    tr = args.get("transparent")
    if tr is True:
        return "transparent"
    if tr is False and backdrop == "transparent":
        return "color"
    return backdrop


# =================================================================================================
# commands
# =================================================================================================
def cmd_ping(ctx: Context, args: dict, progress) -> dict:
    return {"pong": True, "pid": os.getpid(), "uptime": round(time.time() - ctx.started, 2)}


def cmd_system_info(ctx: Context, args: dict, progress) -> dict:
    info = gpu.info()
    info.update({"pid": os.getpid(), "workerVersion": VERSION, "python": sys.version.split()[0],
                 "warmupSeconds": round(ctx.warmup_seconds, 2), "renders": ctx.renders,
                 "deviceOk": gpu.device_ok(), "mode": ctx.mode,
                 "datablocks": {"objects": len(bpy.data.objects), "curves": len(bpy.data.curves),
                                "materials": len(bpy.data.materials), "images": len(bpy.data.images),
                                "meshes": len(bpy.data.meshes)}})
    return info


def cmd_shutdown(ctx: Context, args: dict, progress) -> dict:
    ctx.shutdown = True
    return {"bye": True}


def cmd_reset(ctx: Context, args: dict, progress) -> dict:
    ctx.builder.reset()
    gpu.enable_optix(bpy.context.scene)
    return {"reset": True}


def _render_one(ctx: Context, project: dict, bundle: dict, args: dict, out: str, progress,
                lo: float = 0.0, hi: float = 1.0, overrides: Optional[dict] = None) -> dict:
    t0 = time.perf_counter()
    quality = _quality(args, project)
    appearance = _appearance(args, project)
    backdrop = _backdrop(project, args)
    full_bleed = bool(args.get("fullBleed", False))
    info = ctx.builder.build(project, bundle, appearance, camera=args.get("camera"), full_bleed=full_bleed,
                             backdrop=backdrop, overrides=overrides, engine=R.tier(quality)["engine"])
    scene = bpy.context.scene
    transparent = info["backdrop"] == "transparent"
    cm = _color_mode(project)
    settings = R.configure(scene, quality, args.get("size") or project["render"].get("size"),
                           transparent=transparent, color_mode=cm,
                           max_glass_roughness=info["maxGlassRoughness"], samples=args.get("samples"))
    R.configure_compositor(scene, info["bloom"], transparent, P.soft_clip_knee(cm))
    build_s = time.perf_counter() - t0
    if progress:
        progress(lo + (hi - lo) * 0.05, "scene built")
    secs = R.render_still(scene, out, progress, lo + (hi - lo) * 0.05, hi)
    ctx.renders += 1
    gi = gpu.info()
    return {
        "path": os.path.abspath(out), "width": settings["size"], "height": settings["size"],
        "seconds": round(time.perf_counter() - t0, 4), "renderSeconds": round(secs, 4),
        "buildSeconds": round(build_s, 4),
        "engine": "cycles" if settings["engine"] == "CYCLES" else "eevee",
        "device": gi["device"], "gpu": gi["gpu"], "samples": settings["samples"], "quality": settings["quality"],
        "appearance": info["appearance"], "backdrop": info["backdrop"], "fullBleed": full_bleed,
        "colorMode": cm, "wallpaper": info["wallpaper"],
        "light": info["light"], "stats": info["stats"], "warnings": info["warnings"],
        "memPeakMB": getattr(secs, "mem_peak_mb", 0.0),
    }


def cmd_render(ctx: Context, args: dict, progress) -> dict:
    project = _project(args)
    bundle = ctx.bundle(args)
    out = args.get("out")
    if not out:
        raise CommandError("missing 'out'")
    return _render_one(ctx, project, bundle, args, out, progress)


def cmd_animate(ctx: Context, args: dict, progress) -> dict:
    project = _project(args)
    bundle = ctx.bundle(args)
    kind = args.get("kind", "tilt")
    if kind not in R.ANIM_KINDS:
        raise CommandError(f"unknown animation kind {kind!r}")
    n = max(1, min(2400, int(args.get("frames", 48))))
    fps = max(1, int(args.get("fps", 24)))
    fmt = args.get("format", "mp4")
    out_dir = args.get("outDir")
    if not out_dir:
        raise CommandError("missing 'outDir'")
    _quality(args, project)            # fail fast: before any frame is rendered
    _appearance(args, project)
    os.makedirs(out_dir, exist_ok=True)
    size = max(16, min(R.MAX_SIZE, int(args.get("size") or 512)))
    if fmt == "mp4":
        size += size % 2                # H.264 (yuv420p) needs even frame dimensions
    base_cam = dict(project["camera"])
    if isinstance(args.get("camera"), dict):
        base_cam.update(args["camera"])
    base_angle = float(project["lighting"].get("angle", -45.0))
    layer_ids = [L["id"] for L in project["layers"]]
    t0 = time.perf_counter()
    frames = []
    margs = {**args, "size": size, "camera": base_cam}
    span = 0.9 if fmt == "mp4" else 1.0
    # one framing for the whole clip: union of the subject over sampled frames (no jitter, never cropped)
    samples = n if n <= 96 else 96
    plan = ctx.builder.plan_animation(
        project, bundle, _appearance(args, project),
        [R.frame_overrides(kind, k / samples, base_cam, base_angle, layer_ids) for k in range(samples)], base_cam)
    for i in range(n):
        ov = R.frame_overrides(kind, i / n, base_cam, base_angle, layer_ids)
        if plan is not None:
            ov["framing"] = plan
        out = os.path.join(out_dir, f"frame_{i + 1:04d}.png")
        _render_one(ctx, project, bundle, margs, out, None, overrides=ov)
        frames.append(os.path.abspath(out))
        if progress:
            progress(span * (i + 1) / n, f"frame {i + 1}/{n}")
    result = {"frames": frames, "seconds": 0.0, "fps": fps, "size": size, "kind": kind, "format": fmt,
              "device": gpu.info()["device"]}
    if fmt == "mp4":
        if progress:
            progress(0.92, "encoding mp4")
        # sequencer colour strips are display-referred (COLOR_GAMMA): pass the sRGB backdrop as is
        bg = hex_to_srgb(project["render"].get("backdropColor", "#1c1c22"))
        video = os.path.join(out_dir, args.get("videoName", "animation.mp4"))
        result["video"] = os.path.abspath(R.encode_mp4(frames, video, fps, size, bg))
    result["seconds"] = round(time.perf_counter() - t0, 3)
    return result


def cmd_save_blend(ctx: Context, args: dict, progress) -> dict:
    project = _project(args)
    bundle = ctx.bundle(args)
    out = args.get("out")
    if not out:
        raise CommandError("missing 'out'")
    appearance = _appearance(args, project)
    quality = _quality(args, project, "final")
    backdrop = _backdrop(project, args)
    # pieces are the same height-field body meshes the renders use (plain meshes, editable in Blender)
    # lights calibrated for the engine the .blend is saved with (a draft-quality .blend opens in EEVEE)
    info = ctx.builder.build(project, bundle, appearance, camera=args.get("camera"),
                             full_bleed=bool(args.get("fullBleed", False)), backdrop=backdrop, editable=True,
                             engine=R.tier(quality)["engine"])
    scene = bpy.context.scene
    cm = _color_mode(project)
    R.configure(scene, quality, args.get("size"), transparent=info["backdrop"] == "transparent",
                color_mode=cm, max_glass_roughness=info["maxGlassRoughness"])
    R.configure_compositor(scene, info["bloom"], info["backdrop"] == "transparent", P.soft_clip_knee(cm))
    scene.render.filepath = "//render.png"
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    packed = []
    if args.get("pack", True):
        for img in bpy.data.images:
            if img.source == "FILE" and img.packed_file is None and img.filepath:
                try:
                    img.pack()
                    packed.append(img)
                except RuntimeError as ex:
                    log("pack failed", img.name, ex)
    bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(out), copy=True, compress=True)
    for img in packed:     # keep the worker session light: drop the in-memory packed copies again
        try:
            img.unpack(method="REMOVE")
        except Exception:
            pass
    pieces = [ob for ob in bpy.data.objects if ob.name.startswith("BIS ") and ob.type in ("CURVE", "MESH")
              and ob.name != "BIS Wallpaper"]
    return {"path": os.path.abspath(out), "packed": len(packed), "appearance": info["appearance"],
            "quality": quality,
            "editable": {"curves": sum(ob.type == "CURVE" for ob in pieces),
                         "meshes": sum(ob.type == "MESH" for ob in pieces),
                         "bodies": sum(ob.type == "MESH" and ob.data.get("bis_route") == "heightfield" for ob in pieces)}}


def cmd_swatches(ctx: Context, args: dict, progress) -> dict:
    out_dir = args.get("outDir") or os.path.join(ctx.root, "web", "public", "swatches")
    t0 = time.perf_counter()
    files = SW.render_swatches(ctx.builder, out_dir, int(args.get("size", 192)), args.get("quality", "preview"),
                               progress, args.get("presets"))
    return {"files": [os.path.abspath(f) for f in files], "seconds": round(time.perf_counter() - t0, 3)}


def cmd_scene_info(ctx: Context, args: dict, progress) -> dict:
    """Introspection (debug UI / tests): BIS objects, shape materials (topology key, node identity, node types,
    Principled input values), curves."""
    mats = {}
    for m in bpy.data.materials:
        if m.get(materials.SHAPE_PROP) and m.node_tree is not None:
            nodes = m.node_tree.nodes
            bsdf = [n for n in nodes if n.bl_idname == "ShaderNodeBsdfPrincipled"]
            values = {}
            if bsdf:
                for s in bsdf[0].inputs:
                    if s.enabled and not s.is_linked and s.type == "VALUE":
                        values[s.name] = round(float(s.default_value), 5)
            mats[m.name] = {"key": m.get("bis_key", ""), "preset": m.get("bis_preset", ""),
                            "shape": m.get(materials.SHAPE_PROP, ""),
                            "nodes": len(nodes), "nodeIds": sum(n.as_pointer() % 1000003 for n in nodes),
                            "types": sorted(n.bl_idname for n in nodes), "principled": values,
                            "linked": sorted(s.name for s in (bsdf[0].inputs if bsdf else []) if s.is_linked),
                            "raytraceRefraction": bool(m.use_raytrace_refraction), "users": m.users,
                            "renderMethod": m.surface_render_method}
    objs = []
    for ob in bpy.data.objects:
        if ob.name.startswith("BIS"):
            objs.append({"name": ob.name, "type": ob.type,
                         "route": ob.data.get("bis_route") if ob.type in ("CURVE", "MESH") and ob.data else None,
                         "material": ob.material_slots[0].material.name if ob.material_slots and
                         ob.material_slots[0].material else None,
                         "location": [round(v, 5) for v in ob.matrix_world.translation],
                         "visibleTransmission": bool(ob.visible_transmission),
                         "visibleShadow": bool(ob.visible_shadow)})
            if args.get("check") and ob.type == "MESH" and ob.data and ob.data.get("bis_route") == "heightfield":
                # the body mesh: non-manifold edges, self-intersections, normals facing away from their faces
                objs[-1]["check"] = heightfield.check_mesh(ob.data)
    lights = {ob.name: round(float(ob.data.energy), 4) for ob in bpy.data.objects
              if ob.name.startswith("BIS") and ob.type == "LIGHT" and ob.data is not None}
    sc = bpy.context.scene
    comp = {"useCompositing": bool(sc.render.use_compositing), "viewTransform": sc.view_settings.view_transform,
            "group": sc.compositing_node_group.name if sc.compositing_node_group else None}
    return {"materials": mats, "objects": objs, "curves": len(bpy.data.curves), "images": len(bpy.data.images),
            "lights": lights, "compositor": comp,
            "engine": bpy.context.scene.render.engine, "info": {k: v for k, v in ctx.builder.info.items()
                                                                if k in ("appearance", "backdrop", "stats")}}


def cmd_warmup(ctx: Context, args: dict, progress) -> dict:
    return warmup(ctx, full=bool(args.get("full", True)), cycles=bool(args.get("cycles", True)))


COMMANDS = {
    "ping": cmd_ping, "system_info": cmd_system_info, "shutdown": cmd_shutdown, "reset": cmd_reset,
    "render": cmd_render, "animate": cmd_animate, "save_blend": cmd_save_blend, "swatches": cmd_swatches,
    "warmup": cmd_warmup, "scene_info": cmd_scene_info,
}


def warmup(ctx: Context, full: bool = True, cycles: bool = True) -> dict:
    """Compile the EEVEE shader variants (cold NVIDIA cache: ~12 s once) and initialise OptiX."""
    t0 = time.perf_counter()
    project, bundle = SW.warmup_scene(full)
    tmp = os.path.join(tempfile.gettempdir(), f"bis_warmup_{os.getpid()}.png")
    timings = {}
    try:
        for q, size in (("draft", 64),) + ((("preview", 48),) if cycles else ()):
            t = time.perf_counter()
            _render_one(ctx, project, bundle, {"quality": q, "size": size, "samples": 4 if q == "preview" else 8},
                        tmp, None)
            timings[q] = round(time.perf_counter() - t, 3)
        if full:
            # the tinted renditions add the mono (luminance x tint) paint variant; clear ones the frosted plate
            t = time.perf_counter()
            mproj, mbundle = SW.warmup_scene(full, mono=True)
            for ap in ("clear-dark", "tinted-dark"):
                _render_one(ctx, mproj, mbundle, {"quality": "draft", "size": 64, "samples": 4, "appearance": ap},
                            tmp, None)
            timings["mono"] = round(time.perf_counter() - t, 3)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    ctx.builder.reset()
    gpu.enable_optix(bpy.context.scene)
    ctx.warmup_seconds = time.perf_counter() - t0
    ctx.renders = 0
    return {"warmupSeconds": round(ctx.warmup_seconds, 3), "timings": timings}


def handle(ctx: Context, req: dict, emit: Emit) -> None:
    """Run one request; emits progress events and exactly one done/error event. Never raises."""
    rid = req.get("id") if isinstance(req, dict) else None
    try:
        if not isinstance(req, dict):
            raise CommandError("request must be a JSON object")
        cmd = req.get("cmd")
        args = req.get("args") or {}
        if not isinstance(args, dict):
            raise CommandError("'args' must be an object")
        fn = COMMANDS.get(cmd)
        if fn is None:
            raise CommandError(f"unknown command {cmd!r}")

        def progress(p: float, message: str = "") -> None:
            emit({"id": rid, "event": "progress", "progress": round(max(0.0, min(1.0, float(p))), 4),
                  "message": str(message)})

        result = fn(ctx, args, progress)
        emit({"id": rid, "event": "done", "result": result})
    except BaseException as ex:  # noqa: BLE001 - the worker must survive anything
        if isinstance(ex, (SystemExit, KeyboardInterrupt)):
            ctx.shutdown = True
        tb = traceback.format_exc()
        log("command failed:", repr(ex))
        emit({"id": rid, "event": "error", "error": f"{type(ex).__name__}: {ex}", "traceback": tb})
        if not isinstance(ex, CommandError):
            # a failed build can leave half-made datablocks; start the next request from a clean file
            try:
                ctx.builder.reset()
                gpu.enable_optix(bpy.context.scene)
            except Exception:
                pass
