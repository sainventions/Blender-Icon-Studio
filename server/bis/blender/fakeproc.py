"""Stand-in for ``blender.exe`` used by the BlenderBridge tests (stdlib only).

    python fakeproc.py -b --factory-startup --python <script> -- --port 0 --root <repo>      (worker)
    python fakeproc.py -b --factory-startup --python <script> -- --root <repo> --job <json>  (one-shot)

It speaks the real protocol of PLAN §7 (BIS_WORKER_READY + TCP JSON-lines / BIS_EVENT lines) and is chatty on
stdout (like Blender) so tests prove the bridge keeps draining the pipe. Extra test commands: ``sleep``
(``{"seconds"}``), ``crash`` (exit without answering), ``fail`` (error event), ``spam`` (``{"lines"}`` of noise),
``linger`` (one-shot: report the result, then keep running for ``{"seconds"}`` like a hung driver teardown).
Environment: ``FAKEPROC_STARTUP_DELAY`` seconds before READY; ``FAKEPROC_NO_READY=1`` never reports READY.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import sys
import time
import traceback
import zlib


def png_bytes(w: int, h: int, rgba=(80, 140, 255, 255)) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgba) * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def noise(n: int) -> None:
    for i in range(n):
        print(f"Fra:1 Mem:12.3M (Peak 45.6M) | Time:00:00.{i % 100:02d} | Syncing object {i} " + "x" * 120)
    sys.stdout.flush()


def execute(cmd: str, args: dict, emit) -> dict:
    if cmd == "ping":
        return {"pong": True}
    if cmd == "system_info":
        return {"version": "5.0.0", "device": "OPTIX", "gpu": "Fake GPU", "devices": []}
    if cmd == "render":
        size = int(args.get("size") or 64)
        for s in (4, 8, 16):
            emit({"event": "progress", "progress": s / 16, "message": f"Sample {s}/16"})
        noise(400)  # ~70 KB: more than a Windows pipe buffer
        out = args["out"]
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f:
            f.write(png_bytes(size, size))
        print(f"render | Saved: '{out}'")
        return {"path": out, "width": size, "height": size, "seconds": 0.01, "engine": "eevee",
                "device": "OPTIX", "samples": 16}
    if cmd == "save_blend":
        with open(args["out"], "wb") as f:
            f.write(b"BLENDER-v500")
        return {"path": args["out"]}
    if cmd == "sleep":
        end = time.time() + float(args.get("seconds", 1.0))
        while time.time() < end:
            emit({"event": "progress", "progress": 0.5, "message": "sleeping"})
            time.sleep(0.05)
        return {"slept": True}
    if cmd == "spam":
        noise(int(args.get("lines", 5000)))
        return {"spammed": True}
    if cmd == "linger":
        return {"lingered": True}
    if cmd == "crash":
        sys.stdout.flush()
        os._exit(3)
    if cmd == "fail":
        raise RuntimeError("requested failure")
    raise ValueError(f"unknown command {cmd!r}")


def run_worker(rest: list[str]) -> None:
    port = int(rest[rest.index("--port") + 1]) if "--port" in rest else 0
    delay = float(os.environ.get("FAKEPROC_STARTUP_DELAY", "0") or 0)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    noise(50)
    time.sleep(delay)
    if os.environ.get("FAKEPROC_NO_READY"):
        time.sleep(3600)
    info = {"port": srv.getsockname()[1], "pid": os.getpid(), "version": "5.0.0", "device": "OPTIX",
            "gpu": "Fake GPU", "warmupSeconds": delay}
    print("BIS_WORKER_READY " + json.dumps(info), flush=True)
    conn, _ = srv.accept()
    f = conn.makefile("rwb")

    def send(obj: dict) -> None:
        f.write((json.dumps(obj) + "\n").encode())
        f.flush()

    while True:
        line = f.readline()
        if not line:
            break
        msg = json.loads(line)
        rid, cmd = msg.get("id"), msg.get("cmd")
        if cmd == "shutdown":
            send({"id": rid, "event": "done", "result": {}})
            break
        try:
            res = execute(cmd, msg.get("args") or {}, lambda e: send({"id": rid, **e}))
            send({"id": rid, "event": "done", "result": res})
        except Exception as e:
            send({"id": rid, "event": "error", "error": str(e), "traceback": traceback.format_exc()})
    conn.close()


def run_oneshot(rest: list[str]) -> int:
    job_path = rest[rest.index("--job") + 1]
    with open(job_path, "r", encoding="utf-8") as fh:
        job = json.load(fh)

    def emit(obj: dict) -> None:
        print("BIS_EVENT " + json.dumps(obj), flush=True)

    noise(30)
    try:
        res = execute(job["cmd"], job.get("args") or {}, emit)
        emit({"event": "done", "result": res})
        if job["cmd"] == "linger":
            time.sleep(float((job.get("args") or {}).get("seconds", 60)))
        return 0
    except Exception as e:
        emit({"event": "error", "error": str(e), "traceback": traceback.format_exc()})
        return 1


def main() -> int:
    argv = sys.argv[1:]
    rest = argv[argv.index("--") + 1:] if "--" in argv else []
    print("Blender 5.0.0 (fakeproc)", flush=True)
    print("WARNING HIPEW initialization failed", flush=True)
    if "--job" in rest:
        return run_oneshot(rest)
    run_worker(rest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
