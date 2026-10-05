"""Blender Icon Studio: FastAPI application (REST /api/*, WebSocket /ws, static /files, /swatches, SPA).

Run:  .venv/Scripts/python.exe -m uvicorn bis.main:app --app-dir server --port 8420

``create_app()`` builds a fully wired, isolated app (tests pass their own Settings / bridge / svg module);
``bis.main:app`` is created lazily on first access with the environment's settings.
"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import mimetypes
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .batch import BatchError, BatchService
from .blender import BlenderBridge, BlenderUnavailable, Bridge, FakeBridge
from .config import Settings, load_settings
from .events import EventHub
from .jobs import JobManager
from .models import AnimateRequest, BatchRequest, ExportRequest, Project, RenderRequest, StyleRequest
from .pipeline import SvgPipeline, SvgPipelineUnavailable
from .presets import PresetStore
from .projects import ProjectError, ProjectNotFound, ProjectStore
from .rendering import RenderService
from .samples import SampleLibrary, SampleNotFound
from .schemas import (
    BlendBody,
    CreateFromSample,
    MergeBody,
    MoveElementsBody,
    RenditionsBody,
    SplitBody,
    SplitLayerBody,
    SwatchesBody,
)
from .style import LookNotFound, StyleError, looks, project_style, resolve_style_request, restyle_project
from .system import SystemMonitor
from .util import PathOutsideBase, background

log = logging.getLogger("bis")

APP_VERSION = "0.1.0"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
VALID_STRATEGIES = {"smart", "group", "color", "element", "single"}
NO_STORE = {"Cache-Control": "no-store"}

# Starlette guesses Content-Type with `mimetypes`, which on Windows also reads the registry, where `.js` is
# often mapped to text/plain (browsers then refuse the SPA's module scripts). Pin every type we serve.
MIME_TYPES = {
    ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".html": "text/html",
    ".json": "application/json", ".map": "application/json", ".svg": "image/svg+xml",
    ".wasm": "application/wasm", ".webmanifest": "application/manifest+json",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".gif": "image/gif", ".ico": "image/x-icon", ".icns": "image/icns", ".hdr": "image/vnd.radiance",
    ".exr": "image/x-exr", ".ktx2": "image/ktx2", ".glb": "model/gltf-binary", ".mp4": "video/mp4",
    ".zip": "application/zip", ".blend": "application/x-blender", ".txt": "text/plain",
    ".xml": "application/xml",
}


def _register_mime_types() -> None:
    for ext, mime in MIME_TYPES.items():
        mimetypes.add_type(mime, ext)


def _setup_logging(settings: Settings) -> None:
    root = logging.getLogger("bis")
    if getattr(root, "_bis_configured", False):
        return
    root.setLevel(logging.INFO)
    try:
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            settings.logs_dir / "server.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(fh)
    except OSError:  # pragma: no cover - read-only workspace
        pass
    if not logging.getLogger().handlers:
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        sh.setLevel(logging.INFO)
        root.addHandler(sh)
    # Blender's own stdout goes to its own file (the console stays readable); /api/system/logs has the tail.
    wl = logging.getLogger("bis.worker.out")
    wl.propagate = False
    wl.setLevel(logging.INFO)
    try:
        bh = logging.handlers.RotatingFileHandler(
            settings.logs_dir / "blender.log", maxBytes=5_000_000, backupCount=2, encoding="utf-8"
        )
        bh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        wl.addHandler(bh)
    except OSError:  # pragma: no cover
        pass
    root._bis_configured = True  # type: ignore[attr-defined]


def create_app(
    settings: Settings | None = None,
    *,
    bridge: Bridge | None = None,
    svg: Any | None = None,
    opener=None,
) -> FastAPI:
    settings = settings or load_settings()
    settings.ensure_dirs()
    _setup_logging(settings)
    _register_mime_types()

    hub = EventHub()
    pipeline = svg if isinstance(svg, SvgPipeline) else SvgPipeline(svg)
    presets = PresetStore(settings)
    store = ProjectStore(
        settings, pipeline,
        on_event=lambda pid, ev: hub.publish_threadsafe({"type": "project", "projectId": pid, "event": ev}),
        presets=presets,
    )
    if bridge is None:
        bridge = FakeBridge() if settings.fake_blender else BlenderBridge(settings)
    jobs = JobManager(hub.publish, history=settings.job_history)
    renders = RenderService(settings, store, bridge, jobs, presets, opener=opener)
    samples = SampleLibrary(settings, pipeline)
    batches = BatchService(settings, store, samples, renders, jobs, presets)
    monitor = SystemMonitor(settings, bridge, jobs, hub.publish, lambda: hub.subscriber_count > 0)
    bridge.on_status = monitor.push_soon
    jobs.on_queue_change.append(monitor.push_soon)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hub.bind_loop(asyncio.get_running_loop())
        await jobs.start()
        await monitor.start()
        start_task = None
        if settings.start_worker and bridge.available:
            # never block startup on Blender (cold shader caches can take a while)
            start_task = asyncio.create_task(bridge.start(), name="bis-worker-start")
        # import the SVG pipeline (picosvg, skia, shapely, OpenCV: ~1-2 s) now, not on the first request
        warm_svg = asyncio.create_task(asyncio.to_thread(pipeline.available), name="bis-svg-import")
        log.info("Blender Icon Studio %s on http://%s:%d (workspace %s, blender %s)", APP_VERSION,
                 settings.host, settings.port, settings.workspace, settings.blender_exe or "not found")
        try:
            yield
        finally:
            if start_task is not None and not start_task.done():
                start_task.cancel()
            if not warm_svg.done():
                warm_svg.cancel()
            await jobs.stop()
            await monitor.stop()
            try:
                await asyncio.wait_for(bridge.stop(), 15)
            except Exception:  # pragma: no cover - best effort
                log.exception("worker shutdown failed")

    app = FastAPI(title="Blender Icon Studio", version=APP_VERSION, lifespan=lifespan)
    app.state.settings = settings
    app.state.hub = hub
    app.state.store = store
    app.state.bridge = bridge
    app.state.jobs = jobs
    app.state.presets = presets
    app.state.renders = renders
    app.state.samples = samples
    app.state.batches = batches
    app.state.monitor = monitor
    app.state.svg = pipeline

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------------------------------------ errors
    @app.exception_handler(ProjectNotFound)
    async def _nf(_: Request, exc: ProjectNotFound):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(SampleNotFound)
    async def _snf(_: Request, exc: SampleNotFound):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(PathOutsideBase)
    async def _pob(_: Request, exc: PathOutsideBase):
        return JSONResponse({"detail": "Not found"}, status_code=404)

    @app.exception_handler(ProjectError)
    async def _pe(_: Request, exc: ProjectError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(SvgPipelineUnavailable)
    async def _su(_: Request, exc: SvgPipelineUnavailable):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(LookNotFound)
    async def _lnf(_: Request, exc: LookNotFound):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(StyleError)
    async def _se(_: Request, exc: StyleError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(BatchError)
    async def _be(_: Request, exc: BatchError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(BlenderUnavailable)
    async def _bu(_: Request, exc: BlenderUnavailable):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    api = APIRouter(prefix="/api")

    # ------------------------------------------------------------------------------------------ system
    @api.get("/system")
    async def get_system() -> dict:
        return monitor.status().model_dump(mode="json")

    @api.get("/system/logs")
    async def get_logs(n: int = Query(300, ge=1, le=4000)) -> dict:
        return {"lines": bridge.logs(n)}

    @api.post("/system/worker/restart")
    async def restart_worker() -> dict:
        if not bridge.available:
            raise HTTPException(503, f"Blender not found ({settings.blender_exe or 'set BIS_BLENDER'})")
        background(bridge.restart(), "bis-worker-restart")
        await asyncio.sleep(0)
        return monitor.status().model_dump(mode="json")

    @api.post("/system/swatches")
    async def render_swatches(body: SwatchesBody | None = None) -> dict:
        return (await renders.submit_swatches(body or SwatchesBody())).model_dump(mode="json")

    @api.get("/health")
    def health() -> dict:  # sync: the first call may import the SVG pipeline (runs in the thread pool)
        return {"ok": True, "version": APP_VERSION, "svgPipeline": pipeline.available()}

    # ------------------------------------------------------------------------------------------ library
    @api.get("/presets")
    def get_presets() -> dict:
        return presets.with_swatches()

    @api.get("/looks")
    def get_looks() -> dict:
        return looks(presets)

    @api.get("/samples")
    def list_samples() -> list:
        return [s.model_dump(mode="json") for s in samples.list()]

    @api.get("/samples/{name}/thumbnail.png")
    def sample_thumbnail(name: str, size: int = Query(256, ge=32, le=1024)) -> Response:
        png = samples.thumbnail(name, size)
        return Response(png, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})

    @api.get("/samples/{name}/source.svg")
    def sample_source(name: str) -> FileResponse:
        return FileResponse(samples.resolve(name), media_type="image/svg+xml")

    # ------------------------------------------------------------------------------------------ projects
    @api.get("/projects")
    def list_projects() -> list:
        return [s.model_dump(mode="json") for s in store.list_summaries()]

    @api.post("/projects")
    async def create_project(request: Request) -> dict:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                raise HTTPException(400, "multipart field 'file' is required")
            data = await upload.read(MAX_UPLOAD_BYTES + 1)
            if len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "SVG too large (25 MB max)")
            filename = getattr(upload, "filename", None) or "icon.svg"
            strategy = str(form.get("strategy") or "smart")
            name = form.get("name")
            name = str(name) if name else None
        else:
            try:
                body = CreateFromSample.model_validate(await request.json())
            except Exception as e:
                raise HTTPException(422, f"Expected multipart 'file' or JSON {{sample, strategy?}}: {e}")
            path = await run_in_threadpool(samples.resolve, body.sample)
            data = await run_in_threadpool(path.read_bytes)
            filename, strategy, name = path.name, body.strategy, body.name
        if strategy not in VALID_STRATEGIES:
            raise HTTPException(422, f"Unknown split strategy '{strategy}'")
        project = await run_in_threadpool(store.create_from_svg, data, filename, strategy, name)
        return project.model_dump(mode="json")

    @api.get("/projects/{pid}")
    def get_project(pid: str) -> dict:
        return store.load(pid).model_dump(mode="json")

    @api.put("/projects/{pid}")
    def put_project(pid: str, project: Project) -> dict:
        return store.replace(pid, project).model_dump(mode="json")

    @api.delete("/projects/{pid}")
    async def delete_project(pid: str) -> dict:
        if not await run_in_threadpool(store.exists, pid):
            raise ProjectNotFound(pid)
        jobs.cancel_where(lambda j: j.projectId == pid)  # job manager: event-loop thread only
        await run_in_threadpool(store.delete, pid)
        return {"ok": True, "id": pid}

    @api.post("/projects/{pid}/duplicate")
    def duplicate_project(pid: str) -> dict:
        return store.duplicate(pid).model_dump(mode="json")

    @api.get("/projects/{pid}/source.svg")
    def project_source(pid: str) -> FileResponse:
        return FileResponse(store.source_svg_path(pid), media_type="image/svg+xml")

    @api.post("/projects/{pid}/split")
    def split_project(pid: str, body: SplitBody) -> dict:
        return store.split(pid, body.strategy).model_dump(mode="json")

    @api.post("/projects/{pid}/layers/merge")
    def merge_layers(pid: str, body: MergeBody) -> dict:
        return store.merge(pid, body.layerIds).model_dump(mode="json")

    @api.post("/projects/{pid}/layers/{layer_id}/split")
    def split_layer(pid: str, layer_id: str, body: SplitLayerBody | None = None) -> dict:
        return store.split_layer(pid, layer_id, (body or SplitLayerBody()).mode).model_dump(mode="json")

    @api.post("/projects/{pid}/elements/move")
    def move_elements(pid: str, body: MoveElementsBody) -> dict:
        return store.move_elements(pid, body.elementIds, body.toLayerId).model_dump(mode="json")

    # ------------------------------------------------------------------------------------------ styles (PLAN §10)
    @api.get("/projects/{pid}/style")
    def get_style(pid: str, plateFill: bool | None = None, shape: bool = False) -> dict:
        """Copy style. plateFill: None = only deliberate system/none plate fills; shape: include the plate shape."""
        return project_style(store, pid, plate_fill=plateFill, plate_shape=shape).model_dump(mode="json")

    @api.post("/projects/{pid}/style")
    def post_style(pid: str, body: StyleRequest | None = None) -> dict:
        """Apply a look / pasted style / another project's style; saves (broadcasts "saved") and returns it."""
        if not store.exists(pid):
            raise ProjectNotFound(pid)
        style = resolve_style_request(body or StyleRequest(), presets, store.load,
                                      extract=lambda src: project_style(store, src))
        return restyle_project(store, pid, style).model_dump(mode="json")

    @api.get("/projects/{pid}/geometry")
    def get_geometry(pid: str) -> JSONResponse:
        bundle, _ = store.geometry(store.load(pid))
        return JSONResponse(bundle.model_dump(mode="json"), headers=NO_STORE)

    @api.get("/projects/{pid}/layers/{layer_id}/thumbnail.png")
    def layer_thumbnail(pid: str, layer_id: str, size: int = Query(96, ge=16, le=512)) -> Response:
        png = store.layer_thumbnail(pid, layer_id, size)
        return Response(png, media_type="image/png", headers={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------------------------------------ jobs
    # Job endpoints are async: the JobManager lives on the event loop and is not thread-safe.
    @api.post("/projects/{pid}/render")
    async def render(pid: str, body: RenderRequest | None = None) -> dict:
        return (await renders.submit_render(pid, body or RenderRequest())).model_dump(mode="json")

    @api.post("/projects/{pid}/renditions")
    async def renditions(pid: str, body: RenditionsBody | None = None) -> list:
        return [j.model_dump(mode="json") for j in await renders.submit_renditions(pid, body or RenditionsBody())]

    @api.post("/projects/{pid}/animate")
    async def animate(pid: str, body: AnimateRequest | None = None) -> dict:
        return (await renders.submit_animate(pid, body or AnimateRequest())).model_dump(mode="json")

    @api.post("/projects/{pid}/export")
    async def export(pid: str, body: ExportRequest | None = None) -> dict:
        body = body or ExportRequest()
        if not body.targets:
            raise HTTPException(422, "Select at least one export target")
        return (await renders.submit_export(pid, body)).model_dump(mode="json")

    @api.post("/projects/{pid}/blend")
    async def blend(pid: str, body: BlendBody | None = None) -> dict:
        return (await renders.submit_blend(pid, body or BlendBody())).model_dump(mode="json")

    @api.post("/batch")
    async def batch(body: BatchRequest) -> dict:
        return (await batches.submit(body)).model_dump(mode="json")

    @api.get("/jobs")
    async def list_jobs(projectId: str | None = None, limit: int | None = Query(None, ge=1, le=1000)) -> list:
        return [j.model_dump(mode="json") for j in jobs.list(projectId, limit)]

    @api.get("/jobs/{job_id}")
    async def get_job(job_id: str) -> dict:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, f"Job not found: {job_id}")
        return job.model_dump(mode="json")

    @api.delete("/jobs/{job_id}")
    async def cancel_job(job_id: str) -> dict:
        job = jobs.cancel(job_id)
        if job is None:
            raise HTTPException(404, f"Job not found: {job_id}")
        return job.model_dump(mode="json")

    @api.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"], include_in_schema=False)
    def api_404(rest: str) -> JSONResponse:
        return JSONResponse({"detail": f"Unknown API route: /api/{rest}"}, status_code=404)

    app.include_router(api)

    # ------------------------------------------------------------------------------------------ websocket
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        sub = hub.subscribe()
        try:
            await ws.send_json({"type": "system", "status": monitor.status().model_dump(mode="json")})
            for job in jobs.list():
                if job.state in ("queued", "running"):
                    await ws.send_json({"type": "job", "job": job.model_dump(mode="json")})

            async def sender() -> None:
                while True:
                    await ws.send_json(await sub.get())

            async def receiver() -> None:
                while True:
                    msg = await ws.receive_text()
                    if msg.strip() == "ping":
                        await ws.send_text("pong")

            tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc is not None and not isinstance(exc, (WebSocketDisconnect, RuntimeError)):
                    log.debug("websocket closed: %r", exc)
        except WebSocketDisconnect:
            pass
        finally:
            sub.close()

    # ------------------------------------------------------------------------------------------ static
    app.mount("/files/projects", StaticFiles(directory=settings.projects_dir, check_dir=False), name="files")
    app.mount("/files/batches", StaticFiles(directory=settings.batches_dir, check_dir=False), name="batches")
    app.mount("/swatches", StaticFiles(directory=settings.swatches_dir, check_dir=False), name="swatches")

    dist = settings.web_dist

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def spa(full_path: str) -> Response:
        index = dist / "index.html"
        if full_path.startswith(("api/", "files/", "ws")):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        if not index.is_file():
            return JSONResponse(
                {"detail": "Web UI not built. Run `npm run build` in web/ (or use the Vite dev server on :5173).",
                 "api": "/api/system"},
                status_code=404,
            )
        if full_path:
            try:
                candidate = (dist / full_path).resolve()
                if candidate.is_file() and dist.resolve() in candidate.parents:
                    cache = "public, max-age=31536000, immutable" if "/assets/" in candidate.as_posix() else "no-cache"
                    return FileResponse(candidate, headers={"Cache-Control": cache})
            except OSError:
                pass
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app


_app: FastAPI | None = None


def get_app() -> FastAPI:
    global _app
    if _app is None:
        _app = create_app()
    return _app


def __getattr__(name: str):  # PEP 562: `bis.main:app` is built on first access (no import side effects)
    if name == "app":
        return get_app()
    raise AttributeError(name)
