"""Render orchestration: turns API requests into GPU-queue jobs (render, renditions, animate, export, blend,
swatches) and runs them through the Blender bridge.

Flow of a render job (PLAN §4 C): load the *saved* project.json when the job starts (so a queued live job
always renders the newest state) → ``bis.svg.build_geometry`` (cached by hash) → bridge ``render`` with
``out = workspace/projects/<id>/renders/<jobId>.png`` → result ``{url, path, width, height, seconds, engine,
device, …}``. Drafts/previews go to the persistent worker, everything heavier to a one-shot process (D8).

Auto preview (D7): when a live draft finishes and no newer live draft is pending, a live Cycles *preview* of
the same view is queued after ``auto_preview_delay`` seconds of quiet (if ``project.render.autoPreview``).
Those jobs carry ``request.auto = true``.
"""
from __future__ import annotations

import asyncio
import io
import logging
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, Callable

from .blender.base import Bridge, use_oneshot
from .blender.bridge import blender_env
from .config import Settings
from .jobs import PRIORITY_BACKGROUND, JobContext, JobManager
from .models import AnimateRequest, CameraSpec, ExportRequest, Job, Project, RenderRequest
from .presets import ALL_APPEARANCES, PresetStore
from .projects import ProjectStore
from .schemas import BlendBody, RenditionsBody, SwatchesBody
from .util import atomic_write_text, background, files_url, image_size, safe_filename

log = logging.getLogger("bis.rendering")

LIVE_RENDERS_KEPT = 60
MIN_SIZE, MAX_SIZE = 16, 4096
RENDITION_SIZE = 256


def open_in_blender(exe: Path, blend: Path) -> subprocess.Popen:
    """Open a .blend in the Blender GUI — deliberately WITHOUT -b/--factory-startup so the user's
    preferences (already OptiX) apply. Detached from the server process."""
    def launch(flags: int) -> subprocess.Popen:
        return subprocess.Popen(
            [str(exe), str(blend)],
            cwd=str(blend.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
            env=blender_env(),
        )

    if sys.platform != "win32":
        return launch(0)
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        # the user's Blender session must survive the server (and a terminal job object the server runs in)
        return launch(flags | subprocess.CREATE_BREAKAWAY_FROM_JOB)
    except OSError:  # the enclosing job forbids breakaway
        return launch(flags)


class RenderService:
    def __init__(
        self,
        settings: Settings,
        store: ProjectStore,
        bridge: Bridge,
        jobs: JobManager,
        presets: PresetStore,
        opener: Callable[[Path, Path], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.bridge = bridge
        self.jobs = jobs
        self.presets = presets
        self.opener = opener or open_in_blender
        self._auto_timers: dict[str, asyncio.TimerHandle] = {}
        jobs.on_finished.append(self._on_job_finished)

    # ------------------------------------------------------------------------------------------ sizing
    def resolve_size(self, quality: str, requested: int | None, project: Project | None = None) -> int:
        size = requested or (project.render.size if project is not None else None) or self.presets.tier_size(quality)
        if quality in ("draft", "preview"):  # GPU rule: interactive tiers stay small
            size = min(size, self.settings.persistent_max_size)
        return int(min(max(size, MIN_SIZE), MAX_SIZE))

    # ------------------------------------------------------------------------------------------ primitives
    async def geometry(self, project: Project) -> Path:
        _, path = await asyncio.to_thread(self.store.geometry, project)
        return path

    async def render_image(
        self,
        ctx: JobContext,
        project: Project,
        geometry_path: Path,
        *,
        appearance: str,
        quality: str,
        size: int,
        out: Path,
        camera: CameraSpec | dict | None = None,
        full_bleed: bool = False,
        transparent: bool | None = None,
        oneshot: bool | None = None,
        on_progress: Callable[[float, str], None] | None = None,
    ) -> dict[str, Any]:
        """One render through the bridge; returns the normalised render result."""
        out.parent.mkdir(parents=True, exist_ok=True)
        if transparent is None:
            transparent = project.render.backdrop == "transparent"
        cam = camera.model_dump(mode="json") if isinstance(camera, CameraSpec) else camera
        args: dict[str, Any] = {
            "project": project.model_dump(mode="json"),
            "geometryPath": str(geometry_path),
            "appearance": appearance,
            "quality": quality,
            "size": int(size),
            "fullBleed": bool(full_bleed),
            "out": str(out),
            "transparent": bool(transparent),
        }
        if cam is not None:
            args["camera"] = cam
        if oneshot is None:
            oneshot = use_oneshot("render", quality)
        res = await self.bridge.run("render", args, oneshot=oneshot, on_progress=on_progress, cancel=ctx.token)
        ctx.check()
        path = Path(res.get("path") or out)
        if not path.is_file():
            if out.is_file():
                path = out
            else:
                raise RuntimeError(f"Blender reported success but wrote no image ({path})")
        w, h = res.get("width"), res.get("height")
        if not w or not h:
            w, h = image_size(path) or (size, size)
        result = dict(res)  # keep the worker's extras (samples, renderSeconds, warnings, stats, …)
        result.update(
            {
                "url": files_url(self.settings.workspace, path),
                "path": str(path),
                "width": int(w),
                "height": int(h),
                "seconds": float(res.get("seconds") or 0.0),
                "engine": res.get("engine") or self.presets.tier_engine(quality),
                "device": res.get("device") or "OPTIX",
            }
        )
        result.setdefault("appearance", appearance)
        result.setdefault("quality", quality)
        return result

    def _record_live_render(self, pid: str, path: Path) -> None:
        """Keep only the newest LIVE_RENDERS_KEPT live renders of a project (drafts pile up quickly)."""
        try:
            d = self.store.renders_dir(pid)
            manifest = d / ".live"
            names = manifest.read_text(encoding="utf-8").split() if manifest.is_file() else []
            names.append(path.name)
            stale, keep = names[:-LIVE_RENDERS_KEPT], names[-LIVE_RENDERS_KEPT:]
            for n in stale:
                try:
                    (d / n).unlink()
                except OSError:
                    pass
            atomic_write_text(manifest, "\n".join(keep) + "\n")
        except Exception as e:  # pragma: no cover - housekeeping must never fail a job
            log.debug("live-render pruning failed: %s", e)

    # ------------------------------------------------------------------------------------------ render
    # NOTE: every submit_* coroutine runs on the event loop (JobManager is not thread-safe); project
    # files are read in a worker thread. Jobs re-read project.json when they start running.
    async def _require(self, pid: str) -> Project:
        return await asyncio.to_thread(self.store.load, pid)  # ProjectNotFound -> 404

    async def submit_render(self, pid: str, req: RenderRequest, *, auto: bool = False,
                            rendition: bool = False) -> Job:
        await self._require(pid)
        return self._enqueue_render(pid, req, auto=auto, rendition=rendition)

    def _enqueue_render(self, pid: str, req: RenderRequest, *, auto: bool = False, rendition: bool = False) -> Job:
        quality = req.quality
        request = req.model_dump(mode="json", exclude_none=True)
        if auto:
            request["auto"] = True
        if rendition:
            request["rendition"] = True
        meta = {"live": req.live, "quality": quality, "auto": auto, "rendition": rendition,
                "appearance": req.appearance, "request": req.model_dump(mode="json")}
        if req.live and not auto:  # a fresh edit (or an explicit preview) supersedes a pending auto preview
            self._disarm_auto_preview(pid)
            if quality == "draft":  # …and a queued auto preview of the old state must not delay the draft
                self.jobs.cancel_where(
                    lambda j: j.projectId == pid and j.state == "queued" and bool(j.request.get("auto"))
                )
        if rendition:
            key = ("rendition", pid, req.appearance, quality)
        elif req.live:
            key = ("live", pid, quality)
        else:
            key = None
        oneshot = use_oneshot("render", quality)
        label = f"{quality.capitalize()} render"

        async def run(ctx: JobContext) -> dict[str, Any]:
            p = await asyncio.to_thread(self.store.load, pid)
            appearance = req.appearance or p.appearance
            ctx.progress(0.02, "Building geometry…")
            gpath = await self.geometry(p)
            ctx.check()
            size = self.resolve_size(quality, req.size, p)
            out = self.store.renders_dir(pid) / f"{ctx.job.id}.png"
            camera: CameraSpec | dict | None = req.camera
            if req.fullBleed:
                # PLAN §3: full bleed = square plate filling the frame exactly (ortho_scale 2.0). The worker
                # divides the ortho scale by camera.zoom, so pin the zoom instead of inheriting the project's.
                camera = {**(req.camera or p.camera).model_dump(mode="json"), "view": "front", "zoom": 1.0}
            ctx.progress(0.06, f"{label} · {appearance} · {size}px" + (" (one-shot)" if oneshot else ""))
            result = await self.render_image(
                ctx, p, gpath,
                appearance=appearance, quality=quality, size=size, out=out,
                camera=camera, full_bleed=req.fullBleed, oneshot=oneshot,
                on_progress=ctx.sub(0.06, 0.98),
            )
            if req.live or rendition:
                await asyncio.to_thread(self._record_live_render, pid, out)
            front = req.camera is None or req.camera.view == "front"
            if (quality in ("draft", "preview") and not rendition and front and not req.fullBleed
                    and appearance == p.appearance):
                await asyncio.to_thread(self.store.set_thumbnail_from_image, pid, out)
            return result

        return self.jobs.submit(
            "render", run, project_id=pid, request=request, live=req.live, key=key, meta=meta,
            message="Queued" if not auto else "Auto preview queued",
        )

    async def submit_renditions(self, pid: str, body: RenditionsBody) -> list[Job]:
        project = await self._require(pid)
        apps = body.appearances or self.presets.platform_appearances(project.canvas.platform)
        apps = [a for a in apps if a in ALL_APPEARANCES]
        size = body.size or RENDITION_SIZE
        return [
            self._enqueue_render(
                pid,
                RenderRequest(quality=body.quality, appearance=a, size=size, camera=body.camera),
                rendition=True,
            )
            for a in apps
        ]

    # ------------------------------------------------------------------------------------------ auto preview (D7)
    def _disarm_auto_preview(self, pid: str) -> None:
        h = self._auto_timers.pop(pid, None)
        if h is not None:
            h.cancel()

    def _on_job_finished(self, job: Job, meta: dict[str, Any]) -> None:
        if job.projectId is not None and job.state == "cancelled":
            # the project may have been deleted while this job was still writing into its folder
            background(asyncio.to_thread(self.store.purge_if_deleted, job.projectId), "bis-purge-deleted")
        if (job.kind != "render" or not meta.get("live") or meta.get("quality") != "draft"
                or job.state != "done" or not self.settings.auto_preview or job.projectId is None):
            return
        pid = job.projectId
        newer = self.jobs.pending(lambda j, m: j.projectId == pid and m.get("live") and m.get("quality") == "draft")
        if newer:
            return  # the newest draft will arm the timer when it finishes
        self._disarm_auto_preview(pid)
        loop = asyncio.get_running_loop()
        req = dict(meta.get("request") or {})
        self._auto_timers[pid] = loop.call_later(
            self.settings.auto_preview_delay,
            lambda: background(self._fire_auto_preview(pid, req), "bis-auto-preview"),
        )

    async def _fire_auto_preview(self, pid: str, req: dict[str, Any]) -> None:
        self._auto_timers.pop(pid, None)
        try:
            project = await asyncio.to_thread(self.store.load, pid)
        except Exception:
            return
        if not project.render.autoPreview:
            return
        if self.jobs.pending(lambda j, m: j.projectId == pid and m.get("live") and m.get("quality") == "draft"):
            return
        preview = RenderRequest.model_validate({**req, "quality": "preview", "live": True})
        try:
            self._enqueue_render(pid, preview, auto=True)
        except Exception as e:  # pragma: no cover - project deleted meanwhile
            log.debug("auto preview skipped: %s", e)

    # ------------------------------------------------------------------------------------------ animate
    async def submit_animate(self, pid: str, req: AnimateRequest) -> Job:
        await self._require(pid)

        async def run(ctx: JobContext) -> dict[str, Any]:
            p = await asyncio.to_thread(self.store.load, pid)
            ctx.progress(0.02, "Building geometry…")
            gpath = await self.geometry(p)
            size = self.resolve_size(req.quality, req.size, None)
            frames_n = max(1, min(int(req.frames), 1200))
            out_dir = self.store.renders_dir(pid) / ctx.job.id
            out_dir.mkdir(parents=True, exist_ok=True)
            args = {
                "project": p.model_dump(mode="json"),
                "geometryPath": str(gpath),
                "appearance": p.appearance,
                "quality": req.quality,
                "size": size,
                "kind": req.kind,
                "frames": frames_n,
                "fps": int(req.fps),
                "format": "mp4" if req.format == "mp4" else "png",
                "outDir": str(out_dir),
                "transparent": p.render.backdrop == "transparent" and req.format != "mp4",
            }
            ctx.progress(0.05, f"Animating {req.kind} · {frames_n} frames · {size}px")
            res = await self.bridge.run_oneshot("animate", args, on_progress=ctx.sub(0.05, 0.9, "Animate: "),
                                                cancel=ctx.token)
            ctx.check()
            frames = [Path(f) for f in res.get("frames") or []]
            frames = [f for f in frames if f.is_file()] or sorted(out_dir.glob("*.png"))
            ws = self.settings.workspace
            result: dict[str, Any] = {
                "kind": req.kind, "format": req.format, "fps": req.fps, "frameCount": len(frames),
                "frames": [files_url(ws, f) for f in frames],
                "seconds": float(res.get("seconds") or 0.0),
            }
            if frames:
                wh = image_size(frames[0])
                if wh:
                    result["width"], result["height"] = wh
            if req.format == "mp4":
                video = Path(res.get("video") or "")
                if not video.is_file():
                    raise RuntimeError("Blender did not produce the MP4")
                result["video"] = result["url"] = files_url(ws, video)
            elif req.format in ("gif", "webp"):
                ctx.progress(0.92, f"Encoding {req.format.upper()}…")
                target = self.store.renders_dir(pid) / f"{ctx.job.id}.{req.format}"
                await asyncio.to_thread(encode_animation, frames, target, req.fps, req.format,
                                        p.render.backdrop, p.render.backdropColor)
                result["url"] = files_url(ws, target)
            else:  # png sequence → zip
                target = self.store.renders_dir(pid) / f"{ctx.job.id}-frames.zip"
                await asyncio.to_thread(zip_files, frames, target)
                result["zip"] = result["url"] = files_url(ws, target)
            if frames and "url" not in result:
                result["url"] = result["frames"][0]
            return result

        return self.jobs.submit("animate", run, project_id=pid, request=req.model_dump(mode="json"),
                                priority=PRIORITY_BACKGROUND)

    # ------------------------------------------------------------------------------------------ export
    async def submit_export(self, pid: str, req: ExportRequest) -> Job:
        await self._require(pid)
        from .export import run_export

        async def run(ctx: JobContext) -> dict[str, Any]:
            return await run_export(self, ctx, pid, req)

        return self.jobs.submit("export", run, project_id=pid, request=req.model_dump(mode="json"),
                                priority=PRIORITY_BACKGROUND)

    # ------------------------------------------------------------------------------------------ blend
    async def save_blend(self, ctx: JobContext, project: Project, gpath: Path, out: Path,
                         appearance: str | None = None, on_progress=None) -> Path:
        args = {
            "project": project.model_dump(mode="json"),
            "geometryPath": str(gpath),
            "appearance": appearance or project.appearance,
            "out": str(out),
            "pack": True,
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        res = await self.bridge.run_oneshot("save_blend", args, on_progress=on_progress, cancel=ctx.token)
        ctx.check()
        path = Path(res.get("path") or out)
        if not path.is_file():
            raise RuntimeError("Blender did not write the .blend file")
        return path

    async def submit_blend(self, pid: str, body: BlendBody) -> Job:
        await self._require(pid)

        async def run(ctx: JobContext) -> dict[str, Any]:
            p = await asyncio.to_thread(self.store.load, pid)
            ctx.progress(0.05, "Building geometry…")
            gpath = await self.geometry(p)
            out = self.store.blend_dir(pid) / f"{safe_filename(p.name, max_len=40, default='icon')}.blend"
            ctx.progress(0.15, "Building the Blender scene…")
            path = await self.save_blend(ctx, p, gpath, out, body.appearance, ctx.sub(0.15, 0.95))
            result: dict[str, Any] = {"url": files_url(self.settings.workspace, path), "path": str(path),
                                      "opened": False}
            if body.open:
                if self.settings.blender_found and not self.settings.fake_blender:
                    ctx.progress(0.97, "Opening Blender…")
                    try:  # the .blend is saved either way; a GUI launch failure must not fail the job
                        self.opener(Path(self.settings.blender_exe), path)  # type: ignore[arg-type]
                        result["opened"] = True
                    except Exception as e:  # noqa: BLE001
                        log.warning("could not open Blender: %s", e)
                        result["openError"] = str(e)
                else:
                    log.info("not opening Blender (fake bridge or Blender missing)")
            return result

        return self.jobs.submit("blend", run, project_id=pid, request=body.model_dump(mode="json"))

    # ------------------------------------------------------------------------------------------ swatches
    async def submit_swatches(self, body: SwatchesBody) -> Job:
        async def run(ctx: JobContext) -> dict[str, Any]:
            out_dir = self.settings.swatches_dir
            out_dir.mkdir(parents=True, exist_ok=True)
            size = min(max(int(body.size), 32), 512)
            quality = body.quality if body.quality in ("draft", "preview") else "preview"
            res = await self.bridge.run_oneshot(
                "swatches",
                {"outDir": str(out_dir), "size": size, "quality": quality, "presets": self.presets.material_keys()},
                on_progress=ctx.sub(0.0, 1.0, "Swatches: "),
                cancel=ctx.token,
            )
            files = [Path(f) for f in res.get("files") or []]
            return {"files": [{"name": f.name, "url": f"/swatches/{f.name}"} for f in files if f.is_file()]}

        return self.jobs.submit("swatches", run, request=body.model_dump(mode="json"), priority=PRIORITY_BACKGROUND)


# ---------------------------------------------------------------------------------------------- encoders
def encode_animation(frames: list[Path], target: Path, fps: int, fmt: str, backdrop: str, backdrop_color: str) -> None:
    """PNG frames → animated GIF/WebP with Pillow (Blender has no GIF/animated-WebP writer)."""
    from PIL import Image

    if not frames:
        raise RuntimeError("no frames to encode")
    duration = max(int(round(1000 / max(fps, 1))), 10)
    images = []
    for f in frames:
        with Image.open(f) as im:
            images.append(im.convert("RGBA"))
    if fmt == "gif":
        if backdrop != "transparent":
            bg = Image.new("RGBA", images[0].size, backdrop_color)
            images = [Image.alpha_composite(bg, im).convert("RGB") for im in images]
            pal = [im.quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG)
                   for im in images]
            pal[0].save(target, save_all=True, append_images=pal[1:], duration=duration, loop=0, optimize=False)
        else:
            images[0].save(target, save_all=True, append_images=images[1:], duration=duration, loop=0,
                           disposal=2, optimize=False)
    else:
        images[0].save(target, format="WEBP", save_all=True, append_images=images[1:], duration=duration,
                       loop=0, quality=90, method=4)


def zip_files(files: list[Path], target: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.name)
    target.write_bytes(buf.getvalue())
