"""BlenderBridge: the server's connection to Blender 5.0 (PLAN §7, D8).

Persistent worker (drafts / previews)
    ``blender -b --factory-startup --python blender_worker/worker.py -- --port 0 --root <repo>``
    * stdout+stderr are drained continuously by a daemon thread into a log ring (an undrained pipe blocks
      Blender), and scanned for ``BIS_WORKER_READY {json}``;
    * then a TCP JSON-lines connection to 127.0.0.1:<port>; requests ``{"id","cmd","args"}``, responses
      ``{"id","event":"progress"|"done"|"error",...}``; requests are serialised (the worker is single-threaded);
    * crash / connection loss fails in-flight requests and restarts the worker with back-off.

One-shot processes (final/ultra renders, exports, animations, .blend saves, swatches)
    ``blender -b --factory-startup --python blender_worker/oneshot.py -- --root <repo> --job <job.json>``
    streaming ``BIS_EVENT {json}`` lines; cancel = kill the process (frees VRAM immediately).

Process I/O uses threads + ``subprocess.Popen`` (works under any asyncio loop policy, including the selector
loop uvicorn uses with --reload on Windows); the TCP side uses asyncio streams.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings
from ..util import atomic_write_json, background, new_id, to_jsonable
from . import procgroup
from .base import (
    BlenderUnavailable,
    Bridge,
    CancelToken,
    JobCancelled,
    LogRing,
    ProgressCallback,
    WorkerCrashed,
    WorkerError,
    WorkerState,
    WorkerTimeout,
)

log = logging.getLogger("bis.blender.bridge")
out_log = logging.getLogger("bis.worker.out")

READY_PREFIX = "BIS_WORKER_READY"
EVENT_PREFIX = "BIS_EVENT"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
_STREAM_LIMIT = 64 * 1024 * 1024
#: seconds a one-shot process may linger after reporting its result before it is killed
ONESHOT_EXIT_GRACE = 20.0
#: job files of failed / cancelled one-shots are kept for debugging, then pruned after this many seconds
ONESHOT_JOB_FILE_TTL = 3 * 24 * 3600.0


def blender_env() -> dict[str, str]:
    """Environment for Blender children: drop variables of the server's Python that could leak into
    Blender's bundled interpreter."""
    env = dict(os.environ)
    for k in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"):
        env.pop(k, None)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _kill(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is None:
            proc.kill()
    except OSError:
        pass


def _json_default(o: Any) -> Any:
    return to_jsonable(o)


def _prune_old_files(folder: Path, max_age: float) -> None:
    now = time.time()
    try:
        for f in folder.iterdir():
            try:
                if f.is_file() and now - f.stat().st_mtime > max_age:
                    f.unlink()
            except OSError:
                pass
    except OSError:
        pass


@dataclass
class _Pending:
    future: asyncio.Future
    on_progress: ProgressCallback | None


class BlenderBridge(Bridge):
    RESTART_BACKOFF = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0)

    def __init__(
        self,
        settings: Settings,
        *,
        max_crashes: int = 5,
        crash_window: float = 180.0,
        launcher: list[str] | None = None,
        worker_script: Path | None = None,
        oneshot_script: Path | None = None,
    ) -> None:
        """`launcher` replaces ``[blender.exe]`` as the argv prefix (tests run a Python stand-in);
        `worker_script` / `oneshot_script` override ``blender_worker/worker.py`` / ``oneshot.py``."""
        self.settings = settings
        self.exe: Path | None = Path(settings.blender_exe) if settings.blender_exe else None
        self._launcher = list(launcher) if launcher else None
        self.worker_script = Path(worker_script) if worker_script else settings.worker_script
        self.oneshot_script = Path(oneshot_script) if oneshot_script else settings.oneshot_script
        self._state: WorkerState = "stopped"
        self._message = ""
        self._info: dict[str, Any] | None = None
        self._proc: subprocess.Popen | None = None
        self._gen = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self._watchdog: asyncio.Task | None = None
        self._pending: dict[str, _Pending] = {}
        self._req_lock: asyncio.Lock | None = None
        self._start_lock: asyncio.Lock | None = None
        self._ready: asyncio.Event | None = None
        self._stopping = False
        self._restart_handle: asyncio.TimerHandle | None = None
        self._crashes: deque[float] = deque()
        self._gave_up = False  # crash budget exhausted: no automatic restarts until asked
        self._max_crashes = max_crashes
        self._crash_window = crash_window
        self._busy = 0
        self._oneshots: set[subprocess.Popen] = set()
        self._log = LogRing()
        self._spawned_at = 0.0
        self._pruned_job_files = False

    # ------------------------------------------------------------------------------------------ status
    @property
    def state(self) -> WorkerState:
        return self._state

    @property
    def info(self) -> dict[str, Any] | None:
        return self._info

    @property
    def available(self) -> bool:
        if self._launcher:
            return True
        return self.exe is not None and self.exe.is_file()

    def _argv(self) -> list[str]:
        return list(self._launcher) if self._launcher else [str(self.exe)]

    @property
    def pid(self) -> int | None:
        p = self._proc
        return p.pid if p is not None and p.poll() is None else None

    def status(self) -> dict[str, Any]:
        return {"state": self._state, "pid": self.pid, "message": self._message or None}

    def logs(self, n: int = 200) -> list[str]:
        return self._log.tail(n)

    def _set_state(self, state: WorkerState, message: str | None = None) -> None:
        changed = state != self._state or (message is not None and message != self._message)
        self._state = state
        if message is not None:
            self._message = message
        if changed:
            self._notify()

    def _ready_message(self) -> str:
        i = self._info or {}
        parts = [f"Blender {i.get('version', '?')}", str(i.get("device") or "?")]
        if i.get("gpu"):
            parts.append(str(i["gpu"]))
        if i.get("warmupSeconds") is not None:
            try:
                parts.append(f"warm-up {float(i['warmupSeconds']):.1f}s")
            except (TypeError, ValueError):
                pass
        return " · ".join(parts)

    # ------------------------------------------------------------------------------------------ loop plumbing
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if loop is not self._loop:
            self._loop = loop
            self._req_lock = asyncio.Lock()
            self._start_lock = asyncio.Lock()
            self._ready = asyncio.Event()
            if self._state == "ready":
                self._ready.set()
        return loop

    def _call_soon(self, fn, *args) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:  # loop closed between the check and the call
            pass

    # ------------------------------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._ensure_loop()
        assert self._start_lock is not None
        async with self._start_lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            if not self.available:
                self._set_state("error", f"Blender not found ({self.exe or 'set BIS_BLENDER'})")
                return
            if not self.worker_script.is_file():
                self._set_state("error", f"Worker script missing: {self.worker_script}")
                return
            self._stopping = False
            self._gave_up = False
            self._spawn()

    def _spawn(self) -> None:
        assert self._ready is not None
        self._gen += 1
        gen = self._gen
        self._ready.clear()
        self._info = None
        self._busy = 0
        cmd = [
            *self._argv(), "-b", "--factory-startup",
            "--python", str(self.worker_script),
            "--", "--port", "0", "--root", str(self.settings.root),
        ]
        self._log.append(f"[bridge] spawning worker (gen {gen}): {' '.join(cmd)}")
        self._set_state("starting", "Launching Blender worker…")
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(self.settings.root),
                env=blender_env(),
                creationflags=_NO_WINDOW,
            )
        except OSError as e:
            self._set_state("error", f"Could not launch Blender: {e}")
            return
        procgroup.attach(proc)  # dies with the server, even on a hard kill
        self._proc = proc
        self._spawned_at = time.monotonic()
        threading.Thread(target=self._pump, args=(proc, gen), daemon=True, name=f"bis-worker-{gen}").start()
        self._watchdog = asyncio.get_running_loop().create_task(self._startup_watchdog(gen))
        self._notify()

    def _pump(self, proc: subprocess.Popen, gen: int) -> None:
        """Daemon thread: drain the worker's stdout forever; detect READY; report the exit."""
        assert proc.stdout is not None
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                self._log.append(line)
                out_log.info("%s", line)
                if line.startswith(READY_PREFIX):
                    try:
                        info = json.loads(line[len(READY_PREFIX):].strip() or "{}")
                    except json.JSONDecodeError:
                        info = {}
                    self._call_soon(self._on_ready, gen, info)
        except (OSError, ValueError):
            pass
        rc = proc.wait()
        self._log.append(f"[bridge] worker (gen {gen}, pid {proc.pid}) exited with code {rc}")
        self._call_soon(self._on_exit, gen, rc)

    async def _startup_watchdog(self, gen: int) -> None:
        assert self._ready is not None
        try:
            await asyncio.wait_for(self._ready.wait(), self.settings.worker_startup_timeout)
        except asyncio.TimeoutError:
            if gen == self._gen and self._proc is not None:
                msg = f"Worker did not report ready within {self.settings.worker_startup_timeout:.0f}s"
                self._log.append(f"[bridge] {msg}; killing it")
                self._message = msg
                _kill(self._proc)
        except asyncio.CancelledError:
            pass

    def _on_ready(self, gen: int, info: dict[str, Any]) -> None:
        if gen != self._gen:
            return
        port = info.get("port")
        if not isinstance(port, int):
            self._log.append(f"[bridge] READY line without a port: {info}")
            if self._proc is not None:
                _kill(self._proc)
            return
        background(self._connect(gen, port, info), f"bis-worker-connect-{gen}")

    async def _connect(self, gen: int, port: int, info: dict[str, Any]) -> None:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=_STREAM_LIMIT)
        except OSError as e:
            self._log.append(f"[bridge] connect to worker port {port} failed: {e}")
            if gen == self._gen and self._proc is not None:
                _kill(self._proc)
            return
        if gen != self._gen:
            writer.close()
            return
        self._writer = writer
        self._info = info
        self._read_task = asyncio.get_running_loop().create_task(self._read_loop(gen, reader))
        self._set_state("ready", self._ready_message())
        assert self._ready is not None
        self._ready.set()
        log.info("Blender worker ready: %s", self._message.replace("·", "|"))

    async def _read_loop(self, gen: int, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                try:
                    line = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError) as e:
                    self._log.append(f"[bridge] oversized worker message dropped: {e}")
                    continue
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    self._log.append(f"[bridge] non-JSON worker message: {line[:200]!r}")
                    continue
                self._dispatch(msg)
        except (ConnectionError, OSError) as e:
            self._log.append(f"[bridge] worker connection error: {e}")
        except asyncio.CancelledError:
            return
        if gen == self._gen and not self._stopping:
            if self._ready is not None:
                self._ready.clear()
            self._writer = None
            self._set_state("error", "Connection to the Blender worker was lost")
            self._fail_pending(WorkerCrashed("Connection to the Blender worker was lost"))
            if self._proc is not None and self._proc.poll() is None:
                self._log.append("[bridge] worker closed the socket but is still running; killing it")
                _kill(self._proc)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        rid = msg.get("id")
        pending = self._pending.get(rid) if rid is not None else None
        if pending is None:
            return  # late answer of a timed-out / abandoned request
        event = msg.get("event")
        if event == "progress":
            if pending.on_progress is not None:
                try:
                    pending.on_progress(float(msg.get("progress") or 0.0), str(msg.get("message") or ""))
                except Exception:  # pragma: no cover - defensive
                    log.exception("progress callback failed")
        elif event == "done":
            if not pending.future.done():
                pending.future.set_result(msg.get("result") or {})
        elif event == "error":
            if not pending.future.done():
                pending.future.set_exception(
                    WorkerError(str(msg.get("error") or "worker error"), msg.get("traceback"))
                )

    def _fail_pending(self, exc: Exception) -> None:
        for p in list(self._pending.values()):
            if not p.future.done():
                p.future.set_exception(exc)

    def _on_exit(self, gen: int, rc: int) -> None:
        if gen != self._gen:
            return
        self._proc = None
        if self._ready is not None:
            self._ready.clear()
        self._close_conn()
        self._fail_pending(WorkerCrashed(f"Blender worker exited (code {rc})"))
        if self._stopping:
            self._set_state("stopped", "")
            return
        now = time.monotonic()
        self._crashes.append(now)
        while self._crashes and now - self._crashes[0] > self._crash_window:
            self._crashes.popleft()
        tail = next((ln for ln in reversed(self._log.tail(40)) if ln.strip() and not ln.startswith("[bridge]")), "")
        if len(self._crashes) > self._max_crashes:
            self._gave_up = True
            self._set_state(
                "error",
                f"Worker crashed {len(self._crashes)}× in {self._crash_window:.0f}s (last exit code {rc}). "
                f"{tail[:160]}; restart it from the status bar.",
            )
            return
        delay = self.RESTART_BACKOFF[min(len(self._crashes) - 1, len(self.RESTART_BACKOFF) - 1)]
        self._set_state("error", f"Worker exited (code {rc}); restarting in {delay:.0f}s. {tail[:160]}".strip())
        loop = asyncio.get_running_loop()
        self._restart_handle = loop.call_later(delay, lambda: background(self._auto_restart(), "bis-worker-restart"))

    async def _auto_restart(self) -> None:
        self._restart_handle = None
        if self._stopping or (self._proc is not None and self._proc.poll() is None):
            return
        await self.start()

    def _close_conn(self) -> None:
        if self._read_task is not None and not self._read_task.done():
            self._read_task.cancel()
        self._read_task = None
        if self._watchdog is not None and not self._watchdog.done():
            self._watchdog.cancel()
        self._watchdog = None
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        self._writer = None

    async def stop(self) -> None:
        self._ensure_loop()
        self._stopping = True
        if self._restart_handle is not None:
            self._restart_handle.cancel()
            self._restart_handle = None
        proc = self._proc
        if proc is not None and proc.poll() is None:
            if self._writer is not None and self._state in ("ready", "busy"):
                try:
                    self._writer.write(b'{"id": "shutdown", "cmd": "shutdown", "args": {}}\n')
                    await asyncio.wait_for(self._writer.drain(), 2.0)
                    await asyncio.to_thread(self._wait_quietly, proc, 4.0)
                except Exception:
                    pass
            _kill(proc)
            await asyncio.to_thread(self._wait_quietly, proc, 5.0)
        for p in list(self._oneshots):
            _kill(p)
        self._gen += 1  # ignore late events of the old process
        self._proc = None
        self._close_conn()
        self._fail_pending(WorkerCrashed("Blender worker stopped"))
        if self._ready is not None:
            self._ready.clear()
        self._info = None
        self._set_state("stopped", "")

    @staticmethod
    def _wait_quietly(proc: subprocess.Popen, timeout: float) -> None:
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            pass

    async def restart(self) -> None:
        await self.stop()
        self._crashes.clear()
        self._gave_up = False
        self._stopping = False
        await self.start()

    # ------------------------------------------------------------------------------------------ requests
    async def _wait_ready(self) -> None:
        assert self._ready is not None
        if self._proc is None or self._proc.poll() is not None:
            if self._restart_handle is not None:  # crash back-off pending: start right away
                self._restart_handle.cancel()
                self._restart_handle = None
            if self._gave_up:  # explicit work after a crash loop: one more attempt
                self._crashes.clear()
            await self.start()
            if self._proc is None:
                raise BlenderUnavailable(self._message or "Blender worker could not be started")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.worker_startup_timeout
        while not self._ready.is_set():
            if self._gave_up:
                raise WorkerCrashed(self._message or "Blender worker keeps crashing")
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise WorkerTimeout("Blender worker did not become ready in time")
            try:
                await asyncio.wait_for(self._ready.wait(), min(0.25, remaining))
            except asyncio.TimeoutError:
                pass

    async def request(
        self,
        cmd: str,
        args: dict[str, Any] | None = None,
        on_progress: ProgressCallback | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if not self.available:
            raise BlenderUnavailable(f"Blender not found ({self.exe or 'set BIS_BLENDER'})")
        loop = self._ensure_loop()
        assert self._req_lock is not None
        async with self._req_lock:
            await self._wait_ready()
            if self._writer is None:  # died between READY and now; one more attempt
                await self._wait_ready()
            if self._writer is None:
                raise WorkerCrashed("Blender worker is not connected")
            rid = new_id()
            fut: asyncio.Future = loop.create_future()
            self._pending[rid] = _Pending(fut, on_progress)
            self._busy += 1
            self._set_state("busy")
            try:
                line = json.dumps({"id": rid, "cmd": cmd, "args": args or {}}, default=_json_default) + "\n"
                try:
                    self._writer.write(line.encode("utf-8"))
                    await self._writer.drain()
                except (ConnectionError, OSError) as e:
                    raise WorkerCrashed(f"Could not send to the Blender worker: {e}") from e
                try:
                    return await asyncio.wait_for(fut, timeout or self.settings.worker_request_timeout)
                except asyncio.TimeoutError as e:
                    self._log.append(f"[bridge] request {cmd} ({rid}) timed out; killing the worker")
                    if self._proc is not None:
                        _kill(self._proc)
                    raise WorkerTimeout(f"Blender worker did not answer '{cmd}' in time") from e
            finally:
                self._pending.pop(rid, None)
                self._busy = max(0, self._busy - 1)
                if self._state == "busy" and self._busy == 0:
                    alive = self._writer is not None and self._ready is not None and self._ready.is_set()
                    self._set_state("ready" if alive else "error")

    # ------------------------------------------------------------------------------------------ one-shot
    async def run_oneshot(
        self,
        cmd: str,
        args: dict[str, Any] | None = None,
        on_progress: ProgressCallback | None = None,
        cancel: CancelToken | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if not self.available:
            raise BlenderUnavailable(f"Blender not found ({self.exe or 'set BIS_BLENDER'})")
        script = self.oneshot_script
        if not script.is_file():
            raise BlenderUnavailable(f"One-shot script missing: {script}")
        if cancel is not None:
            cancel.raise_if_cancelled()
        loop = self._ensure_loop()
        jobs_dir = self.settings.tmp_dir / "oneshot"
        if not self._pruned_job_files:
            self._pruned_job_files = True
            await asyncio.to_thread(_prune_old_files, jobs_dir, ONESHOT_JOB_FILE_TTL)
        job_file = jobs_dir / f"{cmd}-{new_id()}.json"
        atomic_write_json(job_file, {"cmd": cmd, "args": args or {}})
        argv = [
            *self._argv(), "-b", "--factory-startup", "--python", str(script),
            "--", "--root", str(self.settings.root), "--job", str(job_file),
        ]
        self._log.append(f"[oneshot] {cmd}: {' '.join(argv)}")
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(self.settings.root),
            env=blender_env(),
            creationflags=_NO_WINDOW,
        )
        procgroup.attach(proc)
        self._oneshots.add(proc)
        queue: asyncio.Queue = asyncio.Queue()
        tag = f"[oneshot {proc.pid}]"

        def pump() -> None:
            assert proc.stdout is not None
            try:
                for raw in iter(proc.stdout.readline, b""):
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line.startswith(EVENT_PREFIX):
                        try:
                            evt = json.loads(line[len(EVENT_PREFIX):].strip())
                        except json.JSONDecodeError:
                            self._log.append(f"{tag} {line}")
                            continue
                        loop.call_soon_threadsafe(queue.put_nowait, ("event", evt))
                    else:
                        self._log.append(f"{tag} {line}")
                        out_log.info("%s %s", tag, line)
            except (OSError, ValueError, RuntimeError):
                pass
            rc = proc.wait()
            try:
                loop.call_soon_threadsafe(queue.put_nowait, ("exit", rc))
            except RuntimeError:
                pass

        threading.Thread(target=pump, daemon=True, name=f"bis-oneshot-{proc.pid}").start()
        unregister = cancel.add_callback(lambda: _kill(proc)) if cancel is not None else (lambda: None)
        deadline = loop.time() + (timeout or self.settings.oneshot_timeout)
        result: dict[str, Any] | None = None
        error: WorkerError | None = None
        rc: int | None = None
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    _kill(proc)
                    if result is not None or error is not None:
                        # the job reported its outcome but Blender did not exit (driver teardown hang)
                        self._log.append(f"{tag} did not exit {ONESHOT_EXIT_GRACE:.0f}s after its result; killed")
                        break
                    raise WorkerTimeout(f"One-shot '{cmd}' timed out")
                try:
                    kind, payload = await asyncio.wait_for(queue.get(), remaining)
                except asyncio.TimeoutError:
                    continue
                if kind == "exit":
                    rc = payload
                    break
                event = payload.get("event")
                if event == "progress" and on_progress is not None:
                    try:
                        on_progress(float(payload.get("progress") or 0.0), str(payload.get("message") or ""))
                    except Exception:  # pragma: no cover - defensive
                        log.exception("progress callback failed")
                elif event == "done":
                    result = payload.get("result") or {}
                    deadline = min(deadline, loop.time() + ONESHOT_EXIT_GRACE)
                elif event == "error":
                    error = WorkerError(str(payload.get("error") or "one-shot error"), payload.get("traceback"))
                    deadline = min(deadline, loop.time() + ONESHOT_EXIT_GRACE)
        except asyncio.CancelledError:
            _kill(proc)
            raise
        finally:
            unregister()
            _kill(proc)
            self._oneshots.discard(proc)
            if error is None and (cancel is None or not cancel.cancelled):
                try:
                    job_file.unlink()
                except OSError:
                    pass
        if cancel is not None and cancel.cancelled:
            raise JobCancelled()
        if error is not None:
            raise error
        if result is None:
            tail = " | ".join(ln for ln in self._log.tail(8) if ln.startswith(tag))[-400:]
            raise WorkerCrashed(f"Blender exited (code {rc}) before reporting a result. {tail}".strip())
        return result
