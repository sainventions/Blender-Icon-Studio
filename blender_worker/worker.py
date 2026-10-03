"""Persistent Blender worker: TCP 127.0.0.1, newline-delimited JSON (PLAN §7).

    blender.exe -b --factory-startup --python blender_worker/worker.py -- --port 0 --root <repo>
                [--warmup full|basic|none] [--no-cycles-warmup]

Prints ``BIS_WORKER_READY {"port", "pid", "version", "device", "gpu", "warmupSeconds"}`` on stdout once
the warm-up render is done and the socket listens (or ``BIS_WORKER_FAILED {"error", "pid"}`` and exit
code 3 when no OptiX device can be set up). Requests are handled serially; a bad request yields an
``error`` event and the worker keeps running. Clients may reconnect at any time (one at a time).
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)


def _args(argv: list[str]) -> dict:
    a = argv[argv.index("--") + 1:] if "--" in argv else []
    out = {"port": 0, "root": _REPO, "warmup": "full", "cycles_warmup": True, "host": "127.0.0.1"}
    i = 0
    while i < len(a):
        k = a[i]
        if k == "--port":
            out["port"] = int(a[i + 1]); i += 1
        elif k == "--root":
            out["root"] = a[i + 1]; i += 1
        elif k == "--warmup":
            out["warmup"] = a[i + 1]; i += 1
        elif k == "--host":
            out["host"] = a[i + 1]; i += 1
        elif k == "--no-cycles-warmup":
            out["cycles_warmup"] = False
        i += 1
    return out


class Connection:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.lock = threading.Lock()
        self.alive = True

    def send(self, event: dict) -> None:
        if not self.alive:
            return
        data = (json.dumps(event, default=str) + "\n").encode("utf-8")
        with self.lock:
            try:
                self.sock.sendall(data)
            except OSError:
                self.alive = False


def serve(ctx, host: str, port: int, ready_extra: dict) -> None:
    from blender_worker import gpu
    from blender_worker.util import log

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(4)
    port = srv.getsockname()[1]
    info = gpu.info()
    ready = {"port": port, "pid": os.getpid(), "version": info["version"], "device": info["device"],
             "gpu": info["gpu"], **ready_extra}
    sys.stdout.write("BIS_WORKER_READY " + json.dumps(ready) + "\n")
    sys.stdout.flush()
    from blender_worker.commands import handle

    while not ctx.shutdown:
        try:
            conn, addr = srv.accept()
        except OSError as ex:
            log("accept failed:", ex)
            time.sleep(0.2)
            continue
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        c = Connection(conn)
        log("client connected", addr)
        buf = b""
        try:
            while not ctx.shutdown and c.alive:
                try:
                    chunk = conn.recv(1 << 16)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf and not ctx.shutdown:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        req = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as ex:
                        c.send({"id": None, "event": "error", "error": f"bad JSON: {ex}", "traceback": ""})
                        continue
                    handle(ctx, req, c.send)
        finally:
            try:
                conn.close()
            except OSError:
                pass
            log("client disconnected")
    srv.close()


def main() -> None:
    a = _args(sys.argv)
    from blender_worker import presets
    presets.set_root(a["root"])
    import bpy
    from blender_worker import gpu
    from blender_worker.commands import Context, warmup
    from blender_worker.util import log

    t0 = time.perf_counter()
    try:
        bpy.ops.wm.read_homefile(use_empty=True)  # keeps preferences (OptiX); read_factory_settings would not
        gpu.enable_optix(bpy.context.scene)
    except Exception as ex:  # no OptiX device / driver: say so on stdout (the bridge logs it) and exit non-zero
        import traceback
        log("startup failed:", traceback.format_exc())
        sys.stdout.write("BIS_WORKER_FAILED " + json.dumps({"error": f"{type(ex).__name__}: {ex}",
                                                            "pid": os.getpid()}) + "\n")
        sys.stdout.flush()
        os._exit(3)
    ctx = Context(a["root"], "worker")
    extra = {"warmupSeconds": 0.0}
    if a["warmup"] != "none":
        try:
            w = warmup(ctx, full=a["warmup"] == "full", cycles=a["cycles_warmup"])
            extra.update(warmupSeconds=w["warmupSeconds"], warmupTimings=w["timings"])
        except Exception as ex:  # never fail startup because of warm-up
            import traceback
            log("warm-up failed:", ex, traceback.format_exc())
            ctx.builder.reset()
            gpu.enable_optix(bpy.context.scene)
    extra["startupSeconds"] = round(time.perf_counter() - t0, 3)
    serve(ctx, a["host"], a["port"], extra)
    log("worker exiting")
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
