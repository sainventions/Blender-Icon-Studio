"""Runtime configuration for the Blender Icon Studio server.

Everything is derived from the repository root and can be overridden with environment variables:

    BIS_ROOT            repository root (default: detected from this file's location)
    BIS_WORKSPACE       runtime data directory (default: <root>/workspace)
    BIS_BLENDER         path to blender.exe (default: Blender 5.0 under Program Files, then PATH)
    BIS_PORT            HTTP port (default: 8420)
    BIS_NO_WORKER=1     do not start the persistent Blender worker at startup (started lazily on demand)
    BIS_FAKE_BLENDER=1  use the in-process FakeBridge instead of Blender (UI development without a GPU)
    BIS_EXPORT_MAX_SIZE cap (px) for export master renders (debugging)

Use :func:`load_settings` (or ``Settings.from_env(**overrides)``), never module-level globals, so tests can
create isolated apps with temporary workspaces.
"""
from __future__ import annotations

import glob
import os
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path

DEFAULT_BLENDER = Path("C:/Program Files/Blender Foundation/Blender 5.0/blender.exe")
DEFAULT_PORT = 8420
SAMPLE_DIR_NAMES = ("samle icons", "docs/research/svgtests")  # sic: the corpus folder name is intentional


def detect_root() -> Path:
    """Repository root: env BIS_ROOT, else three levels up from server/bis/config.py."""
    env = os.environ.get("BIS_ROOT")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "shared" / "presets.json").is_file() and (parent / "server").is_dir():
            return parent
    return here.parents[2]


def detect_blender(explicit: str | os.PathLike | None = None) -> Path | None:
    """Locate blender.exe. Order: explicit/env BIS_BLENDER, the Blender 5.0 default install, any
    'Blender 5.0*' folder under Program Files, then `blender` on PATH. Returns None when not found."""
    candidates: list[Path] = []
    explicit = explicit or os.environ.get("BIS_BLENDER")
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(DEFAULT_BLENDER)
    for pattern in (
        "C:/Program Files/Blender Foundation/Blender 5.0*/blender.exe",
        "C:/Program Files/Blender Foundation/Blender 5.*/blender.exe",
    ):
        candidates.extend(Path(p) for p in sorted(glob.glob(pattern), reverse=True))
    which = shutil.which("blender")
    if which:
        candidates.append(Path(which))
    for c in candidates:
        if c.is_file():
            return c
    return None


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str) -> int | None:
    v = os.environ.get(name, "").strip()
    return int(v) if v.isdigit() else None


@dataclass(frozen=True)
class Settings:
    root: Path
    workspace: Path
    blender_exe: Path | None
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    sample_dirs: tuple[Path, ...] = field(default_factory=tuple)
    #: start the persistent worker in the background at app startup (it is started lazily otherwise)
    start_worker: bool = True
    #: use the FakeBridge (no Blender) for UI development / demos without a GPU
    fake_blender: bool = False
    #: seconds to wait for BIS_WORKER_READY (cold NVIDIA shader cache: first EEVEE RT compile ~12 s+)
    worker_startup_timeout: float = 300.0
    #: per-request timeout for persistent-worker commands
    worker_request_timeout: float = 600.0
    #: one-shot process timeout (finals, exports, animations)
    oneshot_timeout: float = 3 * 3600.0
    #: auto Cycles preview after live drafts settle (PLAN D7); seconds of quiet before it is queued
    auto_preview: bool = True
    auto_preview_delay: float = 1.2
    gpu_poll_interval: float = 2.0
    job_history: int = 200
    #: GPU rule: draft/preview tiers never exceed this size (px)
    persistent_max_size: int = 512
    #: optional cap for export master renders (px); None = tier rules
    export_max_size: int | None = None
    #: where material swatch PNGs live (tests point this at a temp dir so they never clobber the real ones)
    swatches_override: Path | None = None
    cors_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )

    # ------------------------------------------------------------------ derived paths
    @property
    def projects_dir(self) -> Path:
        return self.workspace / "projects"

    @property
    def batches_dir(self) -> Path:
        """Batch ("Icon Pack") outputs: contact sheets + pack zips, served at /files/batches/<jobId>/."""
        return self.workspace / "batches"

    @property
    def tmp_dir(self) -> Path:
        return self.workspace / "tmp"

    @property
    def cache_dir(self) -> Path:
        return self.workspace / "cache"

    @property
    def logs_dir(self) -> Path:
        return self.workspace / "logs"

    @property
    def presets_path(self) -> Path:
        return self.root / "shared" / "presets.json"

    @property
    def web_dist(self) -> Path:
        return self.root / "web" / "dist"

    @property
    def swatches_dir(self) -> Path:
        return self.swatches_override or (self.root / "web" / "public" / "swatches")

    @property
    def worker_dir(self) -> Path:
        return self.root / "blender_worker"

    @property
    def worker_script(self) -> Path:
        return self.worker_dir / "worker.py"

    @property
    def oneshot_script(self) -> Path:
        return self.worker_dir / "oneshot.py"

    @property
    def blender_found(self) -> bool:
        return self.blender_exe is not None and Path(self.blender_exe).is_file()

    def ensure_dirs(self) -> None:
        for d in (self.workspace, self.projects_dir, self.batches_dir, self.tmp_dir, self.cache_dir,
                  self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

    def with_overrides(self, **kw) -> "Settings":
        return replace(self, **kw)

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        root = Path(overrides.pop("root", None) or detect_root()).resolve()
        workspace = overrides.pop("workspace", None) or os.environ.get("BIS_WORKSPACE") or (root / "workspace")
        blender = overrides.pop("blender_exe", "__auto__")
        if blender == "__auto__":
            blender = detect_blender()
        elif blender is not None:
            blender = Path(blender)
        port = _env_int("BIS_PORT") or DEFAULT_PORT
        kw = dict(
            root=root,
            workspace=Path(workspace).resolve(),
            blender_exe=blender,
            port=port,
            sample_dirs=tuple(root / d for d in SAMPLE_DIR_NAMES),
            start_worker=not _env_flag("BIS_NO_WORKER"),
            fake_blender=_env_flag("BIS_FAKE_BLENDER"),
            export_max_size=_env_int("BIS_EXPORT_MAX_SIZE"),
        )
        kw.update(overrides)
        return cls(**kw)


def load_settings(**overrides) -> Settings:
    return Settings.from_env(**overrides)
