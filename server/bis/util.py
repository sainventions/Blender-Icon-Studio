"""Small, dependency-free helpers shared by the server modules."""
from __future__ import annotations

import asyncio
import dataclasses
import functools
import gzip
import io
import json
import logging
import os
import re
import sys
import tempfile
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Coroutine
from urllib.parse import quote, unquote

from pydantic import BaseModel

FILES_PREFIX = "/files"
WIN_MAX_PATH = 259  # longest usable path on Windows without long-path support

_log = logging.getLogger("bis.util")


# ---------------------------------------------------------------------------------------------- ids / time
def now_iso() -> str:
    """UTC timestamp, ISO 8601 with milliseconds and a 'Z' suffix (sortable as a string)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(n: int = 12) -> str:
    return uuid.uuid4().hex[:n]


_slug_re = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 40, default: str = "icon") -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    slug = _slug_re.sub("-", text).strip("-")[:max_len].strip("-")
    return slug or default


def safe_filename(text: str, max_len: int = 60, default: str = "file") -> str:
    """Filesystem-safe name that keeps case and spaces readable (for files inside exports)."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text).strip(" .")
    return text[:max_len].strip(" .") or default


# ---------------------------------------------------------------------------------------------- json
def to_jsonable(obj: Any) -> Any:
    """Convert pydantic models, dataclasses, paths, tuples and sets into plain JSON values."""
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, Path):
        return obj.as_posix()
    return obj


def dumps(obj: Any, indent: int | None = None) -> str:
    return json.dumps(to_jsonable(obj), indent=indent, ensure_ascii=False)


def read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------------------------- atomic io
def _replace_with_retry(src: str, dst: Path, attempts: int = 20) -> None:
    # On Windows os.replace fails while another handle (e.g. a static-file response or an antivirus
    # scanner) has the destination open. Retry briefly instead of failing the save.
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.025 * (i + 1))


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, obj: Any, indent: int | None = 2) -> None:
    atomic_write_text(path, dumps(obj, indent=indent) + "\n")


# ---------------------------------------------------------------------------------------------- paths / urls
class PathOutsideBase(ValueError):
    pass


def safe_child(base: Path, *parts: str) -> Path:
    """Join `parts` onto `base`, refusing anything that escapes `base` (.., absolute paths, drives)."""
    base = Path(base).resolve()
    p = base.joinpath(*parts).resolve()
    if p != base and base not in p.parents:
        raise PathOutsideBase(f"path escapes {base}: {'/'.join(parts)}")
    return p


def files_url(workspace: Path, path: Path, bust: bool = False) -> str:
    """Public URL of a file under <workspace>/projects (served by the /files mount)."""
    rel = Path(path).resolve().relative_to((Path(workspace) / "projects").resolve()).as_posix()
    url = f"{FILES_PREFIX}/projects/{quote(rel)}"
    if bust:
        try:
            url += f"?v={Path(path).stat().st_mtime_ns // 1_000_000}"
        except OSError:
            pass
    return url


def files_path(workspace: Path, url: str) -> Path:
    """Inverse of :func:`files_url` (accepts absolute http URLs and query strings)."""
    u = url.split("?", 1)[0].split("#", 1)[0]
    if "://" in u:
        u = "/" + u.split("://", 1)[1].split("/", 1)[1]
    prefix = f"{FILES_PREFIX}/projects/"
    if not u.startswith(prefix):
        raise PathOutsideBase(f"not a project file URL: {url}")
    return safe_child(Path(workspace) / "projects", *unquote(u[len(prefix):]).split("/"))


def project_url_prefix(project_id: str) -> str:
    return f"{FILES_PREFIX}/projects/{quote(project_id)}"


def image_size(path: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image

        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


@functools.lru_cache(maxsize=1)
def long_paths_enabled() -> bool:
    """True when paths longer than MAX_PATH work (non-Windows, or Win32 long paths enabled)."""
    if sys.platform != "win32":
        return True
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
            return int(winreg.QueryValueEx(k, "LongPathsEnabled")[0]) == 1
    except OSError:
        return False


# ---------------------------------------------------------------------------------------------- svg sniffing
def looks_like_svg(data: bytes) -> bool:
    """Cheap pre-check before the SVG pipeline: plain/BOM-prefixed UTF-8, UTF-16 and gzip (.svgz) SVGs."""
    if data[:2] == b"\x1f\x8b":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
                data = gz.read(256 * 1024)
        except (OSError, EOFError):
            return False
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = data[: 256 * 1024].decode("utf-16", "ignore")
    else:
        text = data[: 128 * 1024].decode("utf-8", "ignore").lstrip("﻿")
    low = text.lower()
    return "<svg" in low or low.lstrip().startswith("<?xml")


# ---------------------------------------------------------------------------------------------- asyncio
_BACKGROUND: set[asyncio.Task] = set()


def _background_done(task: asyncio.Task) -> None:
    _BACKGROUND.discard(task)
    if not task.cancelled() and task.exception() is not None:
        _log.error("background task %s failed", task.get_name(), exc_info=task.exception())


def background(coro: Coroutine[Any, Any, Any], name: str | None = None) -> asyncio.Task:
    """create_task() that keeps a strong reference until the task finishes (the event loop only holds weak
    references; an unreferenced task can be garbage-collected mid-flight) and logs its failure."""
    task = asyncio.get_running_loop().create_task(coro, name=name)
    _BACKGROUND.add(task)
    task.add_done_callback(_background_done)
    return task
