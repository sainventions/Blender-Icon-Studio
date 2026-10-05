"""Batch "Icon Pack" jobs (PLAN §10): many icons → one look → renders (+ one zip of exports).

``POST /api/batch`` (:class:`bis.models.BatchRequest`) queues ONE job of kind ``batch`` that, per source:

1. creates a project from a sample (``{sample}`` → new project named after it, split with ``strategy``) or
   loads an existing one (``{projectId}``);
2. applies the style (``look`` | ``style`` | ``fromProject``; none = keep the project's own) and saves it;
3. renders it (``quality``/``size``/``appearance``; draft/preview are clamped to ≤ 512 px) and refreshes the
   project's library thumbnail (head-on projects only: a CAD-style POV, ``camera.iso`` > 0, is not the icon);
4. with ``export``: runs the normal export for the project (one-shot Blender masters, D8).

Per-item failures are recorded on the item and never abort the batch. Cancel stops after the current item
(one-shot renders/exports are killed at once). Queued live drafts run between items (like exports).
Progress messages read ``"Icon 7/24: Maps"``. While the job runs, ``job.result`` is a partial result
``{items, count, failed, partial: true}`` updated after every icon (kept when the batch is cancelled).

Result: ``{items: BatchItemResult[], contactSheet: url, zip?: url, count, failed, seconds}``. Outputs live in
``workspace/batches/<jobId>/`` (served at ``/files/batches/<jobId>/``): ``contact-sheet.png`` (a dark PNG
grid with each icon's name under its tile) and with ``export`` ``icon-pack.zip`` (one folder per icon with
its export package + the contact sheet).
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable, Optional
from urllib.parse import quote

from .blender.base import JobCancelled, use_oneshot
from .jobs import PRIORITY_BACKGROUND, JobContext
from .models import BatchRequest, BatchSource, Job, StyleSpec
from .rendering import HEAD_ON_ISO
from .schemas import BatchItemResult
from .style import project_style, resolve_look, resolve_style_request, restyle_project
from .util import safe_filename

if TYPE_CHECKING:  # pragma: no cover
    from .config import Settings
    from .jobs import JobManager
    from .presets import PresetStore
    from .projects import ProjectStore
    from .rendering import RenderService
    from .samples import SampleLibrary

log = logging.getLogger("bis.batch")

MAX_SOURCES = 500
INTERACTIVE_MAX = 512          # GPU rule: draft/preview batch renders never exceed 512 px
BATCH_FILES_PREFIX = "/files/batches"
CONTACT_SHEET = "contact-sheet.png"
PACK_ZIP = "icon-pack.zip"
PACK_ROOT = "Icon Pack"


class BatchError(ValueError):
    """Invalid batch request (→ HTTP 400)."""


def batch_url(settings: "Settings", path: Path) -> str:
    rel = Path(path).resolve().relative_to(settings.batches_dir.resolve()).as_posix()
    return f"{BATCH_FILES_PREFIX}/{quote(rel)}"


# ---------------------------------------------------------------------------------------------- item context
class ItemContext:
    """A JobContext view for one batch item: maps a child runner's 0..1 progress into ``[start, end]`` of the
    batch job and prefixes messages with ``"Icon i/n: name"``. Cancellation and live-job yielding are the
    batch job's own (``bis.export.run_export`` runs unchanged on top of it)."""

    def __init__(self, parent: JobContext, start: float, end: float, head: str, item_id: str = "") -> None:
        self._parent = parent
        self.start, self.end, self.head = start, end, head
        # export folders/zips are keyed by ``job.id``: one per *item* (``<batchId>-<i>``), so the same project
        # listed twice in one pack never overwrites its own first export
        self.job = SimpleNamespace(id=item_id or parent.job.id)
        self.token = parent.token

    @property
    def cancelled(self) -> bool:
        return self._parent.cancelled

    def check(self) -> None:
        self._parent.check()

    def progress(self, value: float, message: Optional[str] = None) -> None:
        v = self.start + (self.end - self.start) * min(max(float(value), 0.0), 1.0)
        self._parent.progress(v, f"{self.head} · {message}" if message else self.head)

    def message(self, message: str) -> None:
        self._parent.progress(self._parent.job.progress, f"{self.head} · {message}")

    async def yield_to_live(self) -> None:
        await self._parent.yield_to_live()

    def sub(self, start: float, end: float, prefix: str = "") -> Callable[[float, str], None]:
        def cb(p: float, msg: str = "") -> None:
            text = f"{prefix}{msg}" if msg else (prefix.rstrip(": ") or None)
            self.progress(start + (end - start) * min(max(p, 0.0), 1.0), text)

        return cb


# ---------------------------------------------------------------------------------------------- contact sheet
@dataclass
class Tile:
    name: str
    image: Optional[Path] = None
    error: Optional[str] = None


BG = (18, 18, 22)
TILE_BG = (30, 30, 37)
TEXT = (236, 236, 241)
MUTED = (142, 142, 154)
ERROR = (255, 107, 107)


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    names = (["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"] if bold else []) + \
        ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf", "Helvetica.ttc"]
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - Pillow < 10.1
        return ImageFont.load_default()


def _fit_text(draw, text: str, font, width: int) -> str:
    """`text` on ONE line, ellipsised to `width` px (Pillow cannot measure multi-line text, and error messages
    - pydantic validation errors - span several lines: the whole contact sheet used to fail on one)."""
    text = " ".join(str(text).split())
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return (text.rstrip() + "…") if text else "…"


def make_contact_sheet(tiles: list[Tile], target: Path, title: str = "", subtitle: str = "") -> Path:
    """PNG grid of the batch renders on a dark background, each icon's name under its tile."""
    from PIL import Image, ImageDraw

    n = max(len(tiles), 1)
    tile = 256 if n <= 4 else 192
    cols = min(n, 6, max(1, math.ceil(math.sqrt(n * 1.5))))
    rows = math.ceil(n / cols)
    margin, gap, label_h = 40, 24, 40
    header_h = 72 if title else 0
    width = margin * 2 + cols * tile + (cols - 1) * gap
    height = margin * 2 + header_h + rows * (tile + label_h) + (rows - 1) * gap
    sheet = Image.new("RGBA", (width, height), BG + (255,))
    draw = ImageDraw.Draw(sheet)
    f_title, f_sub = _font(26, bold=True), _font(15)
    f_label, f_err = _font(16), _font(13)
    if title:
        draw.text((margin, margin - 4), _fit_text(draw, title, f_title, width - 2 * margin), font=f_title, fill=TEXT)
        if subtitle:
            draw.text((margin, margin + 32), _fit_text(draw, subtitle, f_sub, width - 2 * margin), font=f_sub,
                      fill=MUTED)
    for i, t in enumerate(tiles):
        r, c = divmod(i, cols)
        x = margin + c * (tile + gap)
        y = margin + header_h + r * (tile + label_h + gap)
        draw.rounded_rectangle([x, y, x + tile - 1, y + tile - 1], radius=tile // 10, fill=TILE_BG + (255,))
        placed = False
        if t.image is not None and t.error is None:
            try:
                with Image.open(t.image) as im:
                    im = im.convert("RGBA")
                    k = tile / max(im.width, im.height)  # fit (up- or down-scale) so every tile is the same size
                    im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))),
                                   Image.Resampling.LANCZOS)
                    sheet.alpha_composite(im, (x + (tile - im.width) // 2, y + (tile - im.height) // 2))
                    placed = True
            except Exception as e:  # noqa: BLE001 - a broken render becomes a failed tile
                t.error = t.error or f"unreadable render: {e}"
        if not placed:
            msg = _fit_text(draw, "Failed" if t.error else "No render", f_label, tile - 16)
            tw = draw.textlength(msg, font=f_label)
            draw.text((x + (tile - tw) / 2, y + tile / 2 - 22), msg, font=f_label, fill=ERROR)
            if t.error:
                err = _fit_text(draw, t.error, f_err, tile - 16)
                ew = draw.textlength(err, font=f_err)
                draw.text((x + (tile - ew) / 2, y + tile / 2 + 4), err, font=f_err, fill=MUTED)
        name = _fit_text(draw, t.name, f_label, tile)
        nw = draw.textlength(name, font=f_label)
        draw.text((x + (tile - nw) / 2, y + tile + 10), name, font=f_label, fill=ERROR if t.error else TEXT)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".png.part")
    sheet.convert("RGB").save(tmp, "PNG", optimize=True)
    tmp.replace(target)
    return target


def write_pack_zip(target: Path, folders: list[tuple[str, Path]], extra: list[Path]) -> Path:
    """One zip: ``Icon Pack/<icon name>/…`` per exported icon (+ `extra` files at the pack root)."""
    tmp = target.with_suffix(".zip.part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for folder_name, src in folders:
            if not src.is_dir():  # (its project was deleted meanwhile)
                continue
            for f in sorted(src.rglob("*")):
                rel = f.relative_to(src)
                if f.is_dir() or rel.parts[0] == "_masters":
                    continue
                zf.write(f, f"{PACK_ROOT}/{folder_name}/{rel.as_posix()}")
        for f in extra:
            if f.is_file():
                zf.write(f, f"{PACK_ROOT}/{f.name}")
    tmp.replace(target)
    return target


def _unique(name: str, used: set[str]) -> str:
    base = safe_filename(name, max_len=40, default="Icon")
    out, k = base, 2
    while out.lower() in used:
        out = f"{base} ({k})"
        k += 1
    used.add(out.lower())
    return out


# ---------------------------------------------------------------------------------------------- service
class BatchService:
    def __init__(self, settings: "Settings", store: "ProjectStore", samples: "SampleLibrary",
                 renders: "RenderService", jobs: "JobManager", presets: "PresetStore") -> None:
        self.settings = settings
        self.store = store
        self.samples = samples
        self.renders = renders
        self.jobs = jobs
        self.presets = presets

    # -- validation (event loop; file access in a thread)
    def _validate(self, req: BatchRequest) -> None:
        if not req.sources:
            raise BatchError("Add at least one icon (sources is empty)")
        if len(req.sources) > MAX_SOURCES:
            raise BatchError(f"Too many icons in one batch ({len(req.sources)} > {MAX_SOURCES})")
        for i, s in enumerate(req.sources):
            if (s.sample is None) == (s.projectId is None):
                raise BatchError(f"sources[{i}]: give exactly one of sample or projectId")
        given = [n for n in ("look", "style", "fromProject") if getattr(req, n) is not None]
        if len(given) > 1:
            raise BatchError(f"Give at most one of look, style or fromProject (got {', '.join(given)})")
        if req.look is not None:
            resolve_look(self.presets, req.look)  # LookNotFound → 404
        if req.fromProject is not None:
            self.store.load(req.fromProject)      # ProjectNotFound → 404
        if req.export is not None and not req.export.targets:
            raise BatchError("export: select at least one export target")

    def render_size(self, req: BatchRequest) -> int:
        size = self.renders.resolve_size(req.quality, req.size, None)
        if req.quality in ("draft", "preview"):
            size = min(size, INTERACTIVE_MAX)
        return size

    async def submit(self, req: BatchRequest) -> Job:
        await asyncio.to_thread(self._validate, req)
        n = len(req.sources)

        async def run(ctx: JobContext) -> dict[str, Any]:
            return await self._run(ctx, req)

        return self.jobs.submit("batch", run, request=req.model_dump(mode="json", exclude_none=True),
                                priority=PRIORITY_BACKGROUND, message=f"Icon pack queued · {n} icon{'s' * (n != 1)}")

    # -- job
    def _open_source(self, src: BatchSource, strategy: str) -> tuple[str, str]:
        """(projectId, name) of a source: a new project from a sample, or an existing project."""
        if src.sample is not None:
            path = self.samples.resolve(src.sample)
            project = self.store.create_from_svg(path.read_bytes(), path.name, strategy, path.stem)
            return project.id, project.name
        project = self.store.load(src.projectId or "")
        return project.id, project.name

    async def _run(self, ctx: JobContext, req: BatchRequest) -> dict[str, Any]:
        t0 = time.perf_counter()
        out_dir = self.settings.batches_dir / ctx.job.id
        out_dir.mkdir(parents=True, exist_ok=True)
        style: Optional[StyleSpec] = await asyncio.to_thread(
            resolve_style_request, req, self.presets, self.store.load, allow_none=True,
            extract=lambda src: project_style(self.store, src))
        n = len(req.sources)
        size = self.render_size(req)
        oneshot = use_oneshot("render", req.quality)
        render_share = 0.3 if req.export is not None else 1.0
        items_end = 0.92
        items: list[BatchItemResult] = []
        tiles: list[Tile] = []
        exported: list[tuple[str, Path]] = []
        used_names: set[str] = set()
        ctx.progress(0.0, f"Icon pack: {n} icon{'s' * (n != 1)} · {req.quality} {size}px")

        for i, src in enumerate(req.sources):
            await ctx.yield_to_live()  # queued live drafts/previews run between two icons
            ctx.check()                # cancel → stop after the current item
            start, end = items_end * i / n, items_end * (i + 1) / n
            name = src.sample or src.projectId or "?"
            head = f"Icon {i + 1}/{n}: {name}"
            ctx.progress(start, head)
            item = BatchItemResult(source=src, name=name)
            tile = Tile(name=name)
            t_item = time.perf_counter()
            try:
                pid, name = await asyncio.to_thread(self._open_source, src, req.strategy)
                item.projectId, item.name, tile.name = pid, name, name
                head = f"Icon {i + 1}/{n}: {name}"
                ctx.check()
                if style is not None:
                    ctx.progress(start, f"{head} · applying style")
                    await asyncio.to_thread(restyle_project, self.store, pid, style)
                ctx.check()
                project = await asyncio.to_thread(self.store.load, pid)
                ctx.progress(start, f"{head} · building geometry")
                gpath = await self.renders.geometry(project)
                ctx.check()
                out = self.store.renders_dir(pid) / f"{ctx.job.id}.png"
                r_end = start + (end - start) * render_share
                res = await self.renders.render_image(
                    ctx, project, gpath, appearance=req.appearance, quality=req.quality, size=size, out=out,
                    oneshot=oneshot, on_progress=ItemContext(ctx, start, r_end, head).sub(0.0, 1.0),
                )
                item.renderUrl = res["url"]
                tile.image = Path(res["path"])
                # the library thumbnail is the head-on icon (PLAN §11): never a render from the CAD-style POV
                if req.appearance == project.appearance and project.camera.iso <= HEAD_ON_ISO:
                    await asyncio.to_thread(self.store.set_thumbnail_from_image, pid, tile.image)
                if req.export is not None:
                    from .export import run_export

                    try:
                        item_ctx = ItemContext(ctx, r_end, end, head, item_id=f"{ctx.job.id}-{i + 1}")
                        exp = await run_export(self.renders, item_ctx, pid, req.export)
                    except (JobCancelled, asyncio.CancelledError):
                        raise
                    except Exception as e:  # noqa: BLE001 - the render stays usable
                        if ctx.cancelled:
                            raise JobCancelled() from e
                        log.warning("batch %s: export of %s failed: %s", ctx.job.id, pid, e)
                        item.error = f"Export failed: {e}"
                    else:
                        item.exportUrl = exp.get("zip")
                        exported.append((_unique(name, used_names), Path(exp["folder"])))
            except (JobCancelled, asyncio.CancelledError):
                raise
            except Exception as e:  # noqa: BLE001 - one broken icon never aborts the pack
                if ctx.cancelled:
                    raise JobCancelled() from e
                log.warning("batch %s: %s failed: %s", ctx.job.id, head, e)
                item.error = str(e) or type(e).__name__
                tile.error = item.error
            # deleted mid-item (library delete while the pack runs): the render recreated renders/, so drop the
            # orphan folder and report the icon as failed instead of linking files that no longer exist
            if item.projectId and await asyncio.to_thread(self.store.purge_if_deleted, item.projectId):
                item.error = tile.error = "The project was deleted while the pack was rendering"
                item.renderUrl = item.exportUrl = None
                tile.image = None
            item.seconds = round(time.perf_counter() - t_item, 3)
            items.append(item)
            tiles.append(tile)
            ctx.progress(end, head)
            # the pack UI fills its tiles while the batch runs (and keeps them when it is cancelled)
            ctx.partial_result({"items": [it.model_dump(mode="json", exclude_none=True) for it in items],
                                "count": n, "failed": sum(1 for it in items if it.error)})

        ctx.check()
        failed = sum(1 for it in items if it.error)
        ctx.progress(0.93, "Building the contact sheet…")
        look_label = await asyncio.to_thread(self._style_label, req)
        title = f"Icon Pack · {look_label}" if look_label else "Icon Pack"
        subtitle = f"{n} icon{'s' * (n != 1)} · {req.quality} · {req.appearance}" + (f" · {failed} failed" if failed else "")
        sheet = await asyncio.to_thread(make_contact_sheet, tiles, out_dir / CONTACT_SHEET, title, subtitle)
        result: dict[str, Any] = {
            "items": [it.model_dump(mode="json", exclude_none=True) for it in items],
            "contactSheet": batch_url(self.settings, sheet),
            "count": n,
            "failed": failed,
            "quality": req.quality,
            "size": size,
            "appearance": req.appearance,
        }
        if req.export is not None:
            ctx.check()
            ctx.progress(0.95, f"Packing {len(exported)} export{'s' * (len(exported) != 1)} into one zip…")
            pack = await asyncio.to_thread(write_pack_zip, out_dir / PACK_ZIP, exported, [sheet])
            result["zip"] = batch_url(self.settings, pack)
            result["zipBytes"] = pack.stat().st_size
        result["url"] = result.get("zip") or result["contactSheet"]
        result["seconds"] = round(time.perf_counter() - t0, 3)
        return result

    def _style_label(self, req: BatchRequest) -> str:
        if req.look is not None:
            look = (self.presets.raw().get("looks") or {}).get(req.look) or {}
            return str(look.get("label") or req.look)
        if req.fromProject is not None:
            try:
                return f"style of {self.store.load(req.fromProject).name}"
            except Exception:  # noqa: BLE001
                return "copied style"
        if req.style is not None:
            return "custom style"
        return ""
