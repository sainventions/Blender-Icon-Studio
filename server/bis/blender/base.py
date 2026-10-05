"""Shared types for Blender bridges (real and fake): errors, cancel tokens, the bridge interface and the
D8 routing policy (which commands run in the persistent worker vs. a one-shot Blender process)."""
from __future__ import annotations

import abc
import logging
import threading
from collections import deque
from typing import Any, Awaitable, Callable, Literal, Optional

log = logging.getLogger("bis.blender")

WorkerState = Literal["stopped", "starting", "ready", "busy", "error"]
ProgressCallback = Callable[[float, str], None]
StatusCallback = Callable[[], None]


# ---------------------------------------------------------------------------------------------- errors
class BridgeError(RuntimeError):
    """Base class for everything that can go wrong talking to Blender."""


class BlenderUnavailable(BridgeError):
    """blender.exe (or the worker scripts) are missing."""


class WorkerCrashed(BridgeError):
    """The Blender process died or the connection dropped while a request was in flight."""


class WorkerTimeout(BridgeError):
    pass


class WorkerError(BridgeError):
    """The worker answered with {"event": "error"}."""

    def __init__(self, message: str, traceback: str | None = None) -> None:
        super().__init__(message)
        self.traceback = traceback or ""


class JobCancelled(Exception):
    """Raised inside a job runner when the job was cancelled."""


# ---------------------------------------------------------------------------------------------- cancel
class CancelToken:
    """Thread-safe cancellation flag with callbacks (one-shot runners register `kill process`)."""

    def __init__(self) -> None:
        self._cancelled = False
        self._callbacks: list[Callable[[], None]] = []
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            callbacks, self._callbacks = self._callbacks, []
        for cb in callbacks:
            try:
                cb()
            except Exception:  # pragma: no cover - defensive
                log.exception("cancel callback failed")

    def add_callback(self, cb: Callable[[], None]) -> Callable[[], None]:
        """Register `cb`; runs immediately when already cancelled. Returns an unregister function."""
        with self._lock:
            if not self._cancelled:
                self._callbacks.append(cb)
                return lambda: self._remove(cb)
        cb()
        return lambda: None

    def _remove(self, cb: Callable[[], None]) -> None:
        with self._lock:
            try:
                self._callbacks.remove(cb)
            except ValueError:
                pass

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise JobCancelled()


# ---------------------------------------------------------------------------------------------- policy (D8)
PERSISTENT_QUALITIES = ("draft", "preview")


def use_oneshot(kind: str, quality: str | None = None) -> bool:
    """PLAN D8: drafts/previews run in the persistent worker; final/ultra renders, exports, animations,
    .blend saves and swatch batches run in a short-lived Blender process (kill = cancel, frees VRAM)."""
    if kind in ("animate", "export", "blend", "swatches"):
        return True
    return (quality or "draft") not in PERSISTENT_QUALITIES


# ---------------------------------------------------------------------------------------------- interface
class LogRing:
    """Bounded, thread-safe ring buffer of Blender stdout/stderr lines."""

    def __init__(self, maxlen: int = 4000) -> None:
        self._lines: deque[str] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def append(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)

    def tail(self, n: int = 200) -> list[str]:
        with self._lock:
            if n <= 0:
                return []
            return list(self._lines)[-n:]


class Bridge(abc.ABC):
    """Interface used by the job runners. Implemented by BlenderBridge (real) and FakeBridge (tests)."""

    #: invoked (on the event loop) whenever state/pid/message change
    on_status: Optional[StatusCallback] = None

    @property
    @abc.abstractmethod
    def state(self) -> WorkerState: ...

    @abc.abstractmethod
    def status(self) -> dict[str, Any]:
        """{"state", "pid", "message"}: the SystemStatus.worker shape."""

    @property
    @abc.abstractmethod
    def info(self) -> dict[str, Any] | None:
        """BIS_WORKER_READY payload of the running worker (version, device, gpu, ...)."""

    @property
    @abc.abstractmethod
    def available(self) -> bool:
        """True when Blender (and the worker scripts) exist."""

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    async def restart(self) -> None: ...

    @abc.abstractmethod
    async def request(
        self,
        cmd: str,
        args: dict[str, Any] | None = None,
        on_progress: ProgressCallback | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Run `cmd` in the persistent worker (started on demand) and return its result."""

    @abc.abstractmethod
    async def run_oneshot(
        self,
        cmd: str,
        args: dict[str, Any] | None = None,
        on_progress: ProgressCallback | None = None,
        cancel: CancelToken | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Run `cmd` in a fresh Blender process; cancel kills it (raises JobCancelled)."""

    @abc.abstractmethod
    def logs(self, n: int = 200) -> list[str]: ...

    async def run(
        self,
        cmd: str,
        args: dict[str, Any],
        *,
        oneshot: bool,
        on_progress: ProgressCallback | None = None,
        cancel: CancelToken | None = None,
    ) -> dict[str, Any]:
        if oneshot:
            return await self.run_oneshot(cmd, args, on_progress=on_progress, cancel=cancel)
        return await self.request(cmd, args, on_progress=on_progress)

    def _notify(self) -> None:
        cb = self.on_status
        if cb is not None:
            try:
                cb()
            except Exception:  # pragma: no cover - defensive
                log.exception("status callback failed")


Runner = Callable[..., Awaitable[dict[str, Any]]]
