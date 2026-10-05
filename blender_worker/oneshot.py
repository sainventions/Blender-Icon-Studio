"""One-shot Blender job (finals, exports, animations, .blend), PLAN §7 / D8.

    blender.exe -b --factory-startup --python blender_worker/oneshot.py -- --root <repo> --job <job.json>

``job.json`` = ``{"cmd": ..., "args": {...}}`` (optionally ``"id"``). Streams ``BIS_EVENT {event}`` lines on
stdout (the same event objects as the persistent worker) and exits 0 on success, 1 on error. Killing the
process cancels the job and frees its VRAM.
"""
from __future__ import annotations

import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)


def _args(argv: list[str]) -> dict:
    a = argv[argv.index("--") + 1:] if "--" in argv else []
    out = {"root": _REPO, "job": None}
    for i, k in enumerate(a):
        if k == "--root" and i + 1 < len(a):
            out["root"] = a[i + 1]
        elif k == "--job" and i + 1 < len(a):
            out["job"] = a[i + 1]
    return out


def _emit(event: dict) -> None:
    sys.stdout.write("BIS_EVENT " + json.dumps(event, default=str) + "\n")
    sys.stdout.flush()


def main() -> int:
    a = _args(sys.argv)
    try:
        if not a["job"]:
            raise ValueError("missing --job <job.json>")
        with open(a["job"], encoding="utf-8") as fh:
            job = json.load(fh)
    except Exception as ex:
        _emit({"id": None, "event": "error", "error": f"cannot read job: {ex}", "traceback": ""})
        return 1
    from blender_worker import presets
    presets.set_root(a["root"])
    import bpy
    from blender_worker import gpu
    from blender_worker.commands import Context, handle

    rid = job.get("id") or "oneshot"
    try:
        bpy.ops.wm.read_homefile(use_empty=True)
        gpu.enable_optix(bpy.context.scene)
    except Exception as ex:
        _emit({"id": rid, "event": "error", "error": f"GPU setup failed: {ex}", "traceback": ""})
        return 1
    ctx = Context(a["root"], "oneshot")
    status = {"ok": False}

    def emit(ev: dict) -> None:
        if ev.get("event") == "done":
            status["ok"] = True
        _emit(ev)

    _emit({"id": rid, "event": "progress", "progress": 0.0, "message": f"blender ready ({gpu.info()['device']})"})
    handle(ctx, {"id": rid, "cmd": job.get("cmd"), "args": job.get("args") or {}}, emit)
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    t0 = time.perf_counter()
    code = 1
    try:
        code = main()
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
