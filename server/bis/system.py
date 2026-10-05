"""System status: Blender install/version, worker state, GPU telemetry (nvidia-smi every 2 s), queue counts.

The status is pushed over /ws as ``{"type": "system", "status": SystemStatus}`` on every GPU poll and
whenever the worker state or the queue changes (coalesced per event-loop tick).
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from .blender.base import Bridge
from .config import Settings
from .jobs import JobManager
from .schemas import BlenderInfo, GpuStatus, QueueStatus, SystemStatus, WorkerStatus

log = logging.getLogger("bis.system")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
GPU_QUERY = "name,memory.used,memory.total,utilization.gpu,temperature.gpu"


def find_nvidia_smi() -> str | None:
    found = shutil.which("nvidia-smi")
    if found:
        return found
    for p in ("C:/Windows/System32/nvidia-smi.exe", "C:/Program Files/NVIDIA Corporation/NVSMI/nvidia-smi.exe"):
        if Path(p).is_file():
            return p
    return None


def _num(v: str) -> float | None:
    v = v.strip()
    try:
        return float(v)
    except ValueError:
        return None


def parse_nvidia_smi(output: str, prefer: str | None = None) -> dict[str, Any]:
    """Parse ``--format=csv,noheader,nounits`` output; picks the GPU named `prefer` when several exist."""
    rows = [[c.strip() for c in line.split(",")] for line in output.strip().splitlines() if line.strip()]
    rows = [r for r in rows if len(r) >= 5]
    if not rows:
        return {}
    row = next((r for r in rows if prefer and r[0] == prefer), rows[0])
    return {
        "name": row[0],
        "memoryUsedMB": _num(row[1]),
        "memoryTotalMB": _num(row[2]),
        "utilization": _num(row[3]),
        "temperature": _num(row[4]),
    }


def query_gpu(exe: str, prefer: str | None = None, timeout: float = 5.0) -> dict[str, Any]:
    proc = subprocess.run(
        [exe, f"--query-gpu={GPU_QUERY}", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"nvidia-smi exit {proc.returncode}")
    return parse_nvidia_smi(proc.stdout, prefer)


_VERSION_RE = re.compile(r"Blender\s+(\d+\.\d+(?:\.\d+)?)")


def blender_version(exe: Path, timeout: float = 60.0) -> str | None:
    """`blender --version` (no UI, no GPU) → '5.0.0'."""
    try:
        proc = subprocess.run(
            [str(exe), "--factory-startup", "--version"],
            capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("blender --version failed: %s", e)
        return None
    m = _VERSION_RE.search(proc.stdout or "")
    return m.group(1) if m else None


class SystemMonitor:
    def __init__(
        self,
        settings: Settings,
        bridge: Bridge,
        jobs: JobManager,
        publish: Callable[[dict[str, Any]], None],
        has_subscribers: Callable[[], bool] = lambda: True,
    ) -> None:
        self.settings = settings
        self.bridge = bridge
        self.jobs = jobs
        self._publish = publish
        self._has_subscribers = has_subscribers
        self.gpu: dict[str, Any] = {}
        self.version: str | None = None
        self._smi = find_nvidia_smi()
        self._tasks: list[asyncio.Task] = []
        self._push_scheduled = False
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------------------------------ status
    def status(self) -> SystemStatus:
        info = self.bridge.info or {}
        ws = self.bridge.status()
        version = info.get("version") or self.version
        exe = self.settings.blender_exe
        gpu = dict(self.gpu)
        gpu["device"] = info.get("device") or ("OPTIX" if self.settings.blender_found else None)
        if not gpu.get("name") and info.get("gpu"):
            gpu["name"] = info.get("gpu")
        return SystemStatus(
            blender=BlenderInfo(
                found=bool(self.settings.blender_found or self.settings.fake_blender),
                path=str(exe) if exe else None,
                version=str(version) if version else None,
            ),
            worker=WorkerStatus(state=ws.get("state", "stopped"), pid=ws.get("pid"), message=ws.get("message")),
            gpu=GpuStatus(**gpu),
            queue=QueueStatus(**self.jobs.counts()),
        )

    def push(self) -> None:
        """Publish the status now (from the event-loop thread)."""
        self._push_scheduled = False
        try:
            self._publish({"type": "system", "status": self.status().model_dump(mode="json")})
        except Exception:  # pragma: no cover - defensive
            log.exception("system push failed")

    def push_soon(self) -> None:
        """Coalesce several state changes in one tick into a single push."""
        if self._push_scheduled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = self._loop
            if loop is None or loop.is_closed():
                return
            self._push_scheduled = True
            loop.call_soon_threadsafe(self.push)
            return
        self._push_scheduled = True
        loop.call_soon(self.push)

    # ------------------------------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._tasks.append(asyncio.create_task(self._gpu_loop(), name="bis-gpu-poll"))
        if self.settings.blender_found and not self.settings.fake_blender:
            self._tasks.append(asyncio.create_task(self._version_check(), name="bis-blender-version"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

    async def _version_check(self) -> None:
        exe = self.settings.blender_exe
        if exe is None:
            return
        self.version = await asyncio.to_thread(blender_version, Path(exe))
        if self.version and not self.version.startswith("5.0"):
            log.warning("Blender %s found at %s, but this app targets Blender 5.0", self.version, exe)
        self.push_soon()

    async def poll_once(self) -> None:
        if not self._smi:
            return
        prefer = (self.bridge.info or {}).get("gpu")
        try:
            self.gpu = await asyncio.to_thread(query_gpu, self._smi, prefer)
        except Exception as e:
            log.debug("nvidia-smi failed: %s", e)

    async def _gpu_loop(self) -> None:
        interval = self.settings.gpu_poll_interval
        while True:
            if self._smi:
                await self.poll_once()
            if self._has_subscribers():
                self.push()
            await asyncio.sleep(interval if self._smi else max(interval, 10.0))
