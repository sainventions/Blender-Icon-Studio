"""Project storage: ``workspace/projects/<id>/``.

Layout of a project directory::

    project.json        the Project document (bis.models.Project), written atomically
    source.svg          original upload            ┐
    normalized.svg      normalised SVG             ├ written by bis.svg.import_svg (workstream A)
    elements.json       A's per-element store      ┘
    cache/              geometry bundles, layer textures + layer SVGs (bis.svg.build_geometry)
    thumbnail.png       256 px library thumbnail (source SVG at import, then the latest draft render)
    renders/<jobId>.png render outputs
    exports/<jobId>.zip export packages (+ the unpacked folder exports/<jobId>/)
    blend/<name>.blend  saved .blend files

All public methods are synchronous (file + CPU work); the API layer runs them in a thread pool. Every
read-modify-write of a project holds that project's lock.

PLAN §11 (round 6): every load and every save passes the project through ``bis.materials.normalize_project`` —
material params outside the shared Principled schema are dropped (a few legacy params are renamed), legacy shadow
kinds ``neutral`` / ``chromatic`` become ``physical``, ``camera.explode`` is reset to 1 and ``camera.iso`` clamped
to 0..1, per-shape overrides of elements a layer no longer holds are dropped. Loading a legacy project.json does
not rewrite it (the cleaned copy is served; the file follows on the next save). Layer edits (split / merge / move)
carry ``Layer.elementMaterials`` along with their elements.
"""
from __future__ import annotations

import io
import logging
import re
import shutil
import threading
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .materials import carry_element_materials, normalize_project
from .models import GeometryBundle, Layer, Project, SourceInfo
from .pipeline import SvgPipeline, SvgPipelineUnavailable
from .presets import PresetStore
from .schemas import ProjectSummary
from .util import (
    atomic_write_bytes,
    atomic_write_json,
    files_url,
    looks_like_svg,
    new_id,
    now_iso,
    project_url_prefix,
    read_json,
    slugify,
    to_jsonable,
)

log = logging.getLogger("bis.projects")

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,120}$")
THUMB_SIZE = 256
DEFAULT_LAYER_GAP = 0.13
NON_COPIED_DIRS = {"cache", "renders", "exports", "blend"}


class ProjectNotFound(KeyError):
    def __str__(self) -> str:
        return f"Project not found: {self.args[0] if self.args else ''}"


class ProjectError(ValueError):
    """A request that is invalid for this project (bad layer ids, illegal merge, broken SVG, …)."""


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class ProjectStore:
    def __init__(
        self,
        settings: Settings,
        svg: SvgPipeline,
        on_event: Callable[[str, str], None] | None = None,
        presets: PresetStore | None = None,
    ) -> None:
        self.settings = settings
        self.svg = svg
        self.presets = presets or PresetStore(settings)
        self.root = settings.projects_dir
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._on_event = on_event
        # folder name -> ((mtime_ns, size) of project.json, (id, name, updatedAt, layerCount))
        self._summaries: dict[str, tuple[tuple[int, int], tuple[str, str, str, int]]] = {}
        self._summaries_lock = threading.Lock()

    # ------------------------------------------------------------------------------------------ paths
    def lock(self, pid: str) -> threading.RLock:
        with self._locks_guard:
            lk = self._locks.get(pid)
            if lk is None:
                lk = self._locks[pid] = threading.RLock()
            return lk

    def dir(self, pid: str) -> Path:
        if not _ID_RE.match(pid or ""):
            raise ProjectNotFound(pid)
        return self.root / pid

    def exists(self, pid: str) -> bool:
        try:
            return (self.dir(pid) / "project.json").is_file()
        except ProjectNotFound:
            return False

    def renders_dir(self, pid: str) -> Path:
        d = self.dir(pid) / "renders"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def exports_dir(self, pid: str) -> Path:
        d = self.dir(pid) / "exports"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def blend_dir(self, pid: str) -> Path:
        d = self.dir(pid) / "blend"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def source_svg_path(self, pid: str) -> Path:
        p = self.dir(pid) / "source.svg"
        if not p.is_file():
            raise ProjectNotFound(pid)
        return p

    def thumbnail_path(self, pid: str) -> Path:
        return self.dir(pid) / "thumbnail.png"

    def url(self, path: Path, bust: bool = False) -> str:
        return files_url(self.settings.workspace, path, bust=bust)

    def _emit(self, pid: str, event: str) -> None:
        if self._on_event is not None:
            try:
                self._on_event(pid, event)
            except Exception:  # pragma: no cover - defensive
                log.exception("project event callback failed")

    # ------------------------------------------------------------------------------------------ CRUD
    def load(self, pid: str) -> Project:
        path = self.dir(pid) / "project.json"
        if not path.is_file():
            raise ProjectNotFound(pid)
        data = read_json(path)
        broken = _drop_null_layer_modes(data)
        project = Project.model_validate(data)
        if broken:
            self._repair_layer_modes(project, broken)
        # PLAN §11: legacy material params / shadow kinds / explode come back on the Principled contract. The
        # cleaned copy is what everyone (UI, renders, styles) sees; the file follows on the next save.
        normalize_project(project, self.presets)
        return project

    def _repair_layer_modes(self, project: Project, layer_ids: set[str]) -> None:
        """Layers saved with ``mode: null`` (a look applied by the round-4 server, whose looks had no mode yet)
        get their art-derived mode back and the project is re-saved (quietly: no updatedAt / event).
        load() reads without the project lock, so the re-save takes it and first checks that the file is still
        the version that was read: a save that landed meanwhile (a PUT, a style apply) is never overwritten by
        this older copy - that newer version is repaired by its own next load if it needs to be."""
        auto = self.auto_modes(project) or {}
        for layer in project.layers:
            if layer.id in layer_ids:
                layer.mode = auto.get(layer.id, "individual")  # type: ignore[assignment]
        log.warning("project %s: repaired layer modes saved as null (%s)", project.id, ", ".join(sorted(layer_ids)))
        try:
            with self.lock(project.id):
                current = read_json(self.dir(project.id) / "project.json")
                if (not isinstance(current, dict) or current.get("updatedAt") != project.updatedAt
                        or _drop_null_layer_modes(current) != layer_ids):
                    return
                self.save(project, touch=False, emit=False)
        except (OSError, ValueError) as e:  # read-only workspace: the repaired copy is still returned
            log.warning("could not save the repaired project %s: %s", project.id, e)

    def save(self, project: Project, touch: bool = True, emit: bool = True) -> Project:
        d = self.dir(project.id)
        normalize_project(project, self.presets)  # every write stays on the §11 contract (see bis.materials)
        with self.lock(project.id):
            if touch:
                project.updatedAt = now_iso()
            atomic_write_json(d / "project.json", project.model_dump(mode="json"))
        if emit:
            self._emit(project.id, "saved")
        return project

    def replace(self, pid: str, data: Project) -> Project:
        """PUT: full replace (id is forced to the URL id, updatedAt set by the server)."""
        with self.lock(pid):
            current = self.load(pid)
            data.id = pid
            data.createdAt = data.createdAt or current.createdAt
            return self.save(data)

    def list_summaries(self) -> list[ProjectSummary]:
        """Library listing. project.json files can be large (hundreds of elements), so the parsed summary
        fields are cached per file version (mtime + size) instead of re-reading every project each time."""
        out: list[ProjectSummary] = []
        if not self.root.is_dir():
            return out
        seen: set[str] = set()
        for d in self.root.iterdir():
            pj = d / "project.json"
            try:
                st = pj.stat()
            except OSError:
                continue
            seen.add(d.name)
            key = (st.st_mtime_ns, st.st_size)
            with self._summaries_lock:
                hit = self._summaries.get(d.name)
            if hit is None or hit[0] != key:
                try:
                    data = read_json(pj)
                except (OSError, ValueError):
                    log.warning("skipping unreadable project %s", d.name)
                    continue
                fields = (str(data.get("id") or d.name), str(data.get("name") or d.name),
                          str(data.get("updatedAt") or ""), len(data.get("layers") or []))
                hit = (key, fields)
                with self._summaries_lock:
                    self._summaries[d.name] = hit
            pid, name, updated, layer_count = hit[1]
            thumb = d / "thumbnail.png"
            out.append(
                ProjectSummary(
                    id=pid,
                    name=name,
                    updatedAt=updated,
                    thumbnail=self.url(thumb, bust=True) if thumb.is_file() else None,
                    layerCount=layer_count,
                )
            )
        with self._summaries_lock:
            for gone in set(self._summaries) - seen:
                self._summaries.pop(gone, None)
        out.sort(key=lambda s: s.updatedAt, reverse=True)
        return out

    def _new_id(self, name: str) -> str:
        base = slugify(name, max_len=32, default="icon")
        while True:
            pid = f"{base}-{new_id(6)}"
            if not (self.root / pid).exists():
                return pid

    def create_from_svg(
        self, svg_bytes: bytes, filename: str, strategy: str = "smart", name: str | None = None
    ) -> Project:
        if not svg_bytes.strip():
            raise ProjectError("The uploaded file is empty")
        if not looks_like_svg(svg_bytes):  # plain, BOM, UTF-16 and gzip (.svgz) files pass
            raise ProjectError("The uploaded file is not an SVG")
        name = (name or Path(filename).stem or "Icon").strip() or "Icon"
        pid = self._new_id(name)
        d = self.root / pid
        d.mkdir(parents=True)
        try:
            res = self.svg.import_svg(svg_bytes, filename, d, strategy)
            source = _get(res, "source")
            source = source if isinstance(source, SourceInfo) else SourceInfo.model_validate(to_jsonable(source))
            warnings = list(dict.fromkeys([*source.warnings, *(_get(res, "warnings") or [])]))
            source.warnings = warnings
            now = now_iso()
            project = Project.model_validate(
                {
                    "id": pid,
                    "name": name,
                    "createdAt": now,
                    "updatedAt": now,
                    "source": to_jsonable(source),
                    "strategy": strategy,
                    "elements": to_jsonable(_get(res, "elements") or []),
                    "layers": to_jsonable(_get(res, "layers") or []),
                    "canvas": to_jsonable(_get(res, "canvas")) or {},
                }
            )
            if not (d / "source.svg").is_file():  # A writes it; make sure it exists either way
                atomic_write_bytes(d / "source.svg", svg_bytes)
            self.save(project, touch=False, emit=False)
        except Exception as e:
            shutil.rmtree(d, ignore_errors=True)
            if isinstance(e, (ProjectError, SvgPipelineUnavailable)):
                raise
            if isinstance(e, ValueError):  # bis.svg: unreadable / invalid SVG — a user error, not a crash
                log.warning("import of %s rejected: %s", filename, e)
            else:
                log.exception("import of %s failed", filename)
            raise ProjectError(f"Could not import {filename}: {e}") from e
        self._write_source_thumbnail(pid, svg_bytes)
        self._emit(pid, "saved")
        return project

    def _write_source_thumbnail(self, pid: str, svg_bytes: bytes) -> None:
        try:
            png = self.svg.thumbnail_png(svg_bytes, THUMB_SIZE)
            atomic_write_bytes(self.thumbnail_path(pid), png)
        except Exception as e:
            log.warning("thumbnail for %s failed: %s", pid, e)

    def delete(self, pid: str) -> None:
        d = self.dir(pid)
        if not d.is_dir():
            raise ProjectNotFound(pid)
        with self.lock(pid):
            shutil.rmtree(d, ignore_errors=True)
            if d.exists():  # a file was still open (Windows); retry once
                import time

                time.sleep(0.2)
                shutil.rmtree(d, ignore_errors=True)
        with self._locks_guard:
            self._locks.pop(pid, None)
        self._emit(pid, "deleted")

    def duplicate(self, pid: str) -> Project:
        with self.lock(pid):
            src = self.load(pid)
            new_pid = self._new_id(src.name)
            src_dir, dst_dir = self.dir(pid), self.root / new_pid
            dst_dir.mkdir(parents=True)
            for item in src_dir.iterdir():
                if item.name in NON_COPIED_DIRS or item.name == "project.json":
                    continue
                if item.is_dir():
                    shutil.copytree(item, dst_dir / item.name)
                else:
                    shutil.copy2(item, dst_dir / item.name)
        now = now_iso()
        dup = src.model_copy(deep=True, update={"id": new_pid, "name": f"{src.name} copy", "createdAt": now, "updatedAt": now})
        self.save(dup, touch=False)
        return dup

    # ------------------------------------------------------------------------------------------ layers
    def _mutate(self, pid: str, fn: Callable[[Project, Path], None]) -> Project:
        with self.lock(pid):
            project = self.load(pid)
            before = [l.model_copy(deep=True) for l in project.layers]
            try:
                fn(project, self.dir(pid))
            except ProjectError:
                raise
            except ValueError as e:  # bis.svg reports invalid edits (incl. ZOrderError) as ValueError
                raise ProjectError(str(e)) from e
            _prune_overrides(project)
            carry_element_materials(before, project.layers)  # per-shape materials follow their elements
            return self.save(project)

    def split(self, pid: str, strategy: str) -> Project:
        def fn(p: Project, d: Path) -> None:
            p.layers = self.svg.split_layers(d, p, strategy)
            p.strategy = strategy  # type: ignore[assignment]

        return self._mutate(pid, fn)

    def merge(self, pid: str, layer_ids: list[str]) -> Project:
        def fn(p: Project, d: Path) -> None:
            known = {l.id for l in p.layers}
            missing = [i for i in layer_ids if i not in known]
            if missing:
                raise ProjectError(f"Unknown layer ids: {', '.join(missing)}")
            if len(set(layer_ids)) < 2:
                raise ProjectError("Select at least two layers to merge")
            p.layers = self.svg.merge_layers(d, p, list(dict.fromkeys(layer_ids)))

        return self._mutate(pid, fn)

    def split_layer(self, pid: str, layer_id: str, mode: str) -> Project:
        def fn(p: Project, d: Path) -> None:
            if not any(l.id == layer_id for l in p.layers):
                raise ProjectError(f"Unknown layer id: {layer_id}")
            p.layers = self.svg.split_layer(d, p, layer_id, mode)

        return self._mutate(pid, fn)

    def move_elements(self, pid: str, element_ids: list[str], to_layer_id: str | None) -> Project:
        def fn(p: Project, d: Path) -> None:
            known = {e.id for e in p.elements}
            unknown = [e for e in element_ids if e not in known]
            if unknown or not element_ids:
                raise ProjectError(f"Unknown element ids: {', '.join(unknown)}" if unknown else "No elements selected")
            if to_layer_id is not None and not any(l.id == to_layer_id for l in p.layers):
                raise ProjectError(f"Unknown layer id: {to_layer_id}")
            layers = self.svg.move_elements(d, p, element_ids, to_layer_id)
            if layers is None:  # pipeline without a move op (tests): local, order-preserving fallback
                move_elements(p, element_ids, to_layer_id)
            else:
                p.layers = layers

        return self._mutate(pid, fn)

    # ------------------------------------------------------------------------------------------ geometry / thumbs
    def auto_modes(self, project: Project) -> dict[str, str] | None:
        """Layer id → the mode the SVG pipeline derives from the layer's art (None when unknown: no pipeline,
        a broken element store). Used to tell a mode the user picked from an art-derived one."""
        try:
            return self.svg.layer_auto_modes(self.dir(project.id), project)
        except Exception as e:  # noqa: BLE001 - a heuristic must never break a style copy
            log.warning("auto layer modes of %s unavailable: %s", project.id, e)
            return None

    def geometry(self, project: Project) -> tuple[GeometryBundle, Path]:
        d = self.dir(project.id)
        bundle = self.svg.build_geometry(d, project, project_url_prefix(project.id))
        return bundle, self.svg.geometry_path(d, bundle)

    def layer_thumbnail(self, pid: str, layer_id: str, size: int = 96) -> bytes:
        project = self.load(pid)
        if not any(l.id == layer_id for l in project.layers):
            raise ProjectError(f"Unknown layer id: {layer_id}")
        return self.svg.layer_thumbnail_png(self.dir(pid), project, layer_id, size)

    def set_thumbnail_from_image(self, pid: str, image_path: Path) -> None:
        """Downscale a render into the project's library thumbnail."""
        from PIL import Image

        try:
            with Image.open(image_path) as im:
                im = im.convert("RGBA")
                im.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.Resampling.LANCZOS)
                canvas = Image.new("RGBA", (THUMB_SIZE, THUMB_SIZE), (0, 0, 0, 0))
                canvas.paste(im, ((THUMB_SIZE - im.width) // 2, (THUMB_SIZE - im.height) // 2))
                buf = io.BytesIO()
                canvas.save(buf, "PNG", optimize=True)
            if self.exists(pid):  # never resurrect the folder of a project deleted mid-render
                atomic_write_bytes(self.thumbnail_path(pid), buf.getvalue())
        except Exception as e:
            log.warning("could not update thumbnail of %s: %s", pid, e)

    def purge_if_deleted(self, pid: str) -> bool:
        """Remove what is left of a deleted project. A persistent-worker render cannot be interrupted, so a
        job that was running when its project was deleted may recreate ``renders/`` afterwards."""
        try:
            d = self.dir(pid)
        except ProjectNotFound:
            return False
        with self.lock(pid):
            if not d.is_dir() or (d / "project.json").exists():
                return False
            shutil.rmtree(d, ignore_errors=True)
        with self._locks_guard:
            self._locks.pop(pid, None)
        return True


# ---------------------------------------------------------------------------------------------- helpers
def _drop_null_layer_modes(data: Any) -> set[str]:
    """Remove ``"mode": null`` from the layers of a raw project.json (in place) → the ids of those layers."""
    out: set[str] = set()
    layers = data.get("layers") if isinstance(data, dict) else None
    for layer in layers if isinstance(layers, list) else ():
        if isinstance(layer, dict) and "mode" in layer and layer["mode"] is None:
            del layer["mode"]
            out.add(str(layer.get("id")))
    return out


def _prune_overrides(project: Project) -> None:
    """Drop appearance overrides that point at layers which no longer exist."""
    ids = {l.id for l in project.layers}
    for ov in (project.appearances.dark, project.appearances.mono):
        for lid in [k for k in ov.layers if k not in ids]:
            del ov.layers[lid]


def _unique_layer_id(project: Project) -> str:
    existing = {l.id for l in project.layers}
    while True:
        lid = f"layer-{new_id(6)}"
        if lid not in existing:
            return lid


def move_elements(project: Project, element_ids: list[str], to_layer_id: str | None) -> None:
    """Move elements between layers (or into a new layer above the top-most source layer).

    Element order inside every layer follows the source paint order. Layers left empty are removed.
    """
    if not element_ids:
        raise ProjectError("No elements selected")
    order = {e.id: i for i, e in enumerate(project.elements)}
    unknown = [e for e in element_ids if e not in order]
    if unknown:
        raise ProjectError(f"Unknown element ids: {', '.join(unknown)}")
    ids = set(element_ids)
    layers = project.layers
    src_idx = [i for i, l in enumerate(layers) if ids & set(l.elementIds)]

    if to_layer_id is not None:
        target = next((l for l in layers if l.id == to_layer_id), None)
        if target is None:
            raise ProjectError(f"Unknown layer id: {to_layer_id}")
        for l in layers:
            if l is not target:
                l.elementIds = [e for e in l.elementIds if e not in ids]
        target.elementIds = sorted(set(target.elementIds) | ids, key=lambda e: order[e])
    else:
        top = max(src_idx) if src_idx else len(layers) - 1
        if layers:
            template = layers[top]
            z = template.depth.z
            above = layers[top + 1].depth.z if top + 1 < len(layers) else None
            new_z = (z + above) / 2 if above is not None and above > z else z + DEFAULT_LAYER_GAP
            new = template.model_copy(deep=True)
            new.depth.z = round(new_z, 4)
        else:
            new = Layer(id="tmp", name="Layer", elementIds=[])
            top = -1
        new.id = _unique_layer_id(project)
        first = min(ids, key=lambda e: order[e])
        el = project.elements[order[first]]
        new.name = el.name if len(ids) == 1 else f"{el.name} +{len(ids) - 1}"
        new.visible, new.locked = True, False
        for l in layers:
            l.elementIds = [e for e in l.elementIds if e not in ids]
        new.elementIds = sorted(ids, key=lambda e: order[e])
        layers.insert(top + 1, new)
        # keep the stack ordered: lift layers above the new one that would now sit at/below it
        for i in range(top + 2, len(layers)):
            if layers[i].depth.z <= layers[i - 1].depth.z:
                layers[i].depth.z = round(layers[i - 1].depth.z + DEFAULT_LAYER_GAP, 4)
    project.layers = [l for l in layers if l.elementIds]
