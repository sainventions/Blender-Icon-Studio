"""Minimal client for the persistent Blender worker (tests + benchmarks). Not the server bridge (that is
workstream C's ``server/bis/blender/``); this only speaks the PLAN §7 protocol."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

REPO = Path(os.environ.get("BIS_WORKER_ROOT") or Path(__file__).resolve().parents[2])   # env: A/B an older worker
BLENDER = Path(os.environ.get("BIS_BLENDER", r"C:/Program Files/Blender Foundation/Blender 5.0/blender.exe"))


def blender_available() -> bool:
    return BLENDER.is_file()


class WorkerError(RuntimeError):
    pass


class Worker:
    """Spawns ``blender_worker/worker.py`` and talks JSON-lines over TCP."""

    def __init__(self, warmup: str = "full", timeout: float = 240.0, extra: Optional[list] = None):
        self.log: deque = deque(maxlen=400)
        t0 = time.perf_counter()
        cmd = [str(BLENDER), "-b", "--factory-startup", "--python", str(REPO / "blender_worker" / "worker.py"),
               "--", "--port", "0", "--root", str(REPO), "--warmup", warmup] + list(extra or [])
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace", cwd=str(REPO))
        self.ready = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            self.log.append(line.rstrip())
            if line.startswith("BIS_WORKER_READY "):
                self.ready = json.loads(line.split(" ", 1)[1])
                break
        if self.ready is None:
            self.proc.kill()
            raise WorkerError("worker did not become ready:\n" + "\n".join(self.log))
        self.startup_seconds = time.perf_counter() - t0
        # keep draining stdout so Blender never blocks on a full pipe
        threading.Thread(target=self._drain, daemon=True).start()
        self.sock = socket.create_connection(("127.0.0.1", self.ready["port"]), timeout=timeout)
        self.file = self.sock.makefile("rwb")
        self._n = 0

    def _drain(self):
        for line in self.proc.stdout:
            self.log.append(line.rstrip())

    def send_raw(self, data: bytes) -> dict:
        self.file.write(data)
        self.file.flush()
        return json.loads(self.file.readline())

    def call(self, cmd: str, args: Optional[dict] = None, check: bool = True):
        """-> (final event, progress events)."""
        self._n += 1
        rid = f"t{self._n}"
        self.file.write((json.dumps({"id": rid, "cmd": cmd, "args": args or {}}) + "\n").encode())
        self.file.flush()
        progress = []
        while True:
            line = self.file.readline()
            if not line:
                raise WorkerError("worker closed the connection:\n" + "\n".join(list(self.log)[-40:]))
            ev = json.loads(line)
            assert ev.get("id") == rid, ev
            if ev["event"] == "progress":
                progress.append(ev)
                continue
            if check and ev["event"] == "error":
                raise WorkerError(f"{cmd} failed: {ev['error']}\n{ev.get('traceback', '')}")
            return ev, progress

    def result(self, cmd: str, args: Optional[dict] = None) -> dict:
        return self.call(cmd, args)[0]["result"]

    def close(self):
        try:
            self.call("shutdown", check=False)
        except Exception:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def run_oneshot(job: dict, job_path: Path, timeout: float = 300.0):
    """-> (exit code, [events], stdout lines)."""
    job_path.write_text(json.dumps(job), encoding="utf-8")
    cmd = [str(BLENDER), "-b", "--factory-startup", "--python", str(REPO / "blender_worker" / "oneshot.py"),
           "--", "--root", str(REPO), "--job", str(job_path)]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                       cwd=str(REPO))
    events = [json.loads(ln.split(" ", 1)[1]) for ln in p.stdout.splitlines() if ln.startswith("BIS_EVENT ")]
    return p.returncode, events, p.stdout.splitlines()


def gpu_memory_used_mib() -> Optional[int]:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
        return int(out.strip().splitlines()[0])
    except Exception:
        return None


def process_gpu_memory_mib(pid: int) -> Optional[int]:
    """Per-process VRAM (often N/A on Windows WDDM)."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
        for ln in out.splitlines():
            parts = [p.strip() for p in ln.split(",")]
            if len(parts) == 2 and parts[0] == str(pid) and parts[1].isdigit():
                return int(parts[1])
    except Exception:
        pass
    return None
