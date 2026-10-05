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
import base64
import io
import logging
import os
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .blender.base import Bridge, use_oneshot
from .blender.bridge import blender_env
from .config import Settings
from .jobs import PRIORITY_BACKGROUND, JobContext, JobManager
from .materials import normalize_camera
from .models import AnimateRequest, CameraSpec, ExportRequest, Job, Project, RenderRequest
from .presets import ALL_APPEARANCES, PresetStore
from .projects import ProjectStore
from .schemas import BlendBody, RenditionsBody, SwatchesBody
from .util import atomic_write_text, background, files_url, image_size, safe_filename

log = logging.getLogger("bis.rendering")

LIVE_RENDERS_KEPT = 60
MIN_SIZE, MAX_SIZE = 16, 4096
RENDITION_SIZE = 256
#: animation kinds renamed by PLAN §11 (the worker only knows the new names)
LEGACY_ANIMATION_KINDS = {"explode": "iso"}
HEAD_ON_ISO = 1e-3            # camera.iso at or below this is the head-on front view


@dataclass
class LaunchedProcess:
    """A GUI process started by :func:`open_in_blender` (not a child we wait on)."""
    pid: int
    method: str                       # 'wmi' | 'cmd-start' | 'popen'
    popen: subprocess.Popen | None = None

    def alive(self) -> bool:
        if self.popen is not None:
            return self.popen.poll() is None
        return pid_alive(self.pid)


# Win32_Process.Create via CIM: the new process is created by the WMI service host, outside the server's
# process tree AND outside every job object the server runs in (a terminal / IDE / agent harness job with
# KILL_ON_JOB_CLOSE that forbids breakaway would otherwise take Blender down with the server).
_WMI_LAUNCH_PS = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ CreateFlags = [uint32]$env:BIS_LAUNCH_FLAGS }
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = $env:BIS_LAUNCH_CMDLINE; CurrentDirectory = $env:BIS_LAUNCH_CWD; ProcessStartupInformation = $startup }
if ($r.ReturnValue -ne 0) { [Console]::Error.WriteLine("Win32_Process.Create returned $($r.ReturnValue)"); exit 2 }
[Console]::Out.WriteLine($r.ProcessId)
"""
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def pid_alive(pid: int) -> bool:
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def _powershell() -> str:
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    exe = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(exe) if exe.is_file() else "powershell.exe"


def _launch_wmi(cmdline: str, cwd: Path) -> LaunchedProcess:
    env = {**os.environ, "BIS_LAUNCH_CMDLINE": cmdline, "BIS_LAUNCH_CWD": str(cwd),
           "BIS_LAUNCH_FLAGS": str(_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP)}
    script = base64.b64encode(_WMI_LAUNCH_PS.encode("utf-16-le")).decode("ascii")
    r = subprocess.run(
        [_powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", script],
        env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
        creationflags=_CREATE_NO_WINDOW,
    )
    out = (r.stdout or "").strip().splitlines()
    if r.returncode != 0 or not out or not out[-1].strip().isdigit():
        err = (r.stderr or "").strip().splitlines()
        raise OSError(f"WMI launch failed ({r.returncode}): {err[-1] if err else r.stdout.strip()}")
    return LaunchedProcess(int(out[-1].strip()), "wmi")


def _launch_cmd_start(exe: Path, blend: Path) -> LaunchedProcess:
    """Fallback: `cmd /c start`. Blender's parent (cmd) exits at once, so it leaves the server's process tree;
    job breakaway when the enclosing job allows it."""
    cmdline = f'cmd.exe /d /c start "" /D "{blend.parent}" "{exe}" "{blend}"'
    flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW

    def run(f: int) -> subprocess.Popen:
        return subprocess.Popen(cmdline, cwd=str(blend.parent), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=f,
                                close_fds=True, env=blender_env())

    try:
        proc = run(flags | _CREATE_BREAKAWAY_FROM_JOB)
    except OSError:  # the enclosing job forbids breakaway
        proc = run(flags)
    proc.wait(30)
    if proc.returncode:
        raise OSError(f"cmd start exited with {proc.returncode}")
    return LaunchedProcess(proc.pid, "cmd-start")


def open_in_blender(exe: Path, blend: Path) -> LaunchedProcess:
    """Open a .blend in the Blender GUI with the given Blender 5.0 exe (never the .blend file association),
    deliberately WITHOUT -b/--factory-startup so the user's preferences (already OptiX) apply.

    The window must outlive the server: on Windows it is started through WMI (Win32_Process.Create), which
    puts it outside the server's process tree and outside any job object (``taskkill /T`` of the server or a
    closing terminal/harness job does not reach it). Falls back to ``cmd /c start`` (+ job breakaway)."""
    exe, blend = Path(exe), Path(blend)
    if sys.platform != "win32":
        proc = subprocess.Popen([str(exe), str(blend)], cwd=str(blend.parent), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                                close_fds=True, env=blender_env())
        return LaunchedProcess(proc.pid, "popen", proc)
    cmdline = subprocess.list2cmdline([str(exe), str(blend)])
    try:
        launched = _launch_wmi(cmdline, blend.parent)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("WMI launch of Blender failed (%s); falling back to cmd start", e)
        return _launch_cmd_start(exe, blend)
    log.info("opened %s in Blender (pid %d, %s)", blend.name, launched.pid, launched.method)
    return launched


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
            pov: CameraSpec | None = None
            if req.camera is not None:  # an explicit POV follows the project camera's §11 rules (iso 0..1, explode 1)
                pov = req.camera.model_copy(deep=True)
                normalize_camera(pov)
            camera: CameraSpec | dict | None = pov
            if rendition and pov is None:
                # the appearance renditions (+ size waterfall) preview the exported icon: head-on like every
                # platform master, whatever the project's CAD-style POV (camera.iso) is
                camera = {**p.camera.model_dump(mode="json"), "iso": 0.0}
            if req.fullBleed:
                # PLAN §3: full bleed = square plate filling the frame exactly (ortho_scale 2.0). The worker
                # divides the ortho scale by camera.zoom, so pin the zoom instead of inheriting the project's;
                # it is the head-on App Store master, so never the project's CAD-style POV (iso).
                camera = {**(pov or p.camera).model_dump(mode="json"), "view": "front", "zoom": 1.0, "iso": 0.0}
            ctx.progress(0.06, f"{label} · {appearance} · {size}px" + (" (one-shot)" if oneshot else ""))
            result = await self.render_image(
                ctx, p, gpath,
                appearance=appearance, quality=quality, size=size, out=out,
                camera=camera, full_bleed=req.fullBleed, oneshot=oneshot,
                on_progress=ctx.sub(0.06, 0.98),
            )
            if req.live or rendition:
                await asyncio.to_thread(self._record_live_render, pid, out)
            # the library thumbnail is the head-on icon: never a CAD-style POV, whether it was asked for explicitly
            # or is the project's own camera (the live render follows the project's iso slider)
            front = (pov is None or pov.view == "front") and (pov or p.camera).iso <= HEAD_ON_ISO
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
        kind = LEGACY_ANIMATION_KINDS.get(req.kind, req.kind)   # 'explode' → 'iso' (head-on → iso → head-on)
        if kind != req.kind:
            req = req.model_copy(update={"kind": kind})

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
                        launched = await asyncio.to_thread(
                            self.opener, Path(self.settings.blender_exe), path)  # type: ignore[arg-type]
                        result["opened"] = True
                        if isinstance(getattr(launched, "pid", None), int):
                            result["pid"] = launched.pid
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
