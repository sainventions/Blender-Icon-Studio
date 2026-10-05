"""FakeBridge: a Blender-free implementation of the worker protocol (PLAN §7).

Used by the API test-suite and by ``BIS_FAKE_BLENDER=1`` (UI development without a GPU). It "renders" a flat
2D approximation with Pillow: the plate in its canvas shape and fill, and every visible layer's regions from
the geometry bundle (bezier splines, holes, solid/gradient paint → first stop colour). Camera framing follows
the real rules (ortho_scale = 2.24/zoom, fullBleed → square plate + ortho 2.0), so export packaging and alpha
masks can be verified without Blender. Materials are ignored (flat paint) and every view is drawn head-on: the
CAD-style POV (``camera.iso``) and the ``iso`` animation are not simulated.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from pathlib import Path
from typing import Any

from .base import (
    BlenderUnavailable,
    Bridge,
    CancelToken,
    JobCancelled,
    LogRing,
    ProgressCallback,
    WorkerError,
    WorkerState,
)

TIER_SIZE = {"draft": 512, "preview": 512, "final": 1024, "ultra": 2048}
SYSTEM_FILLS = {"system-light": ("#ffffff", "#e4e5ea"), "system-dark": ("#3a3a3f", "#111114")}


def _hex(c: str, default=(255, 255, 255)) -> tuple[int, int, int]:
    try:
        c = c.lstrip("#")
        if len(c) == 3:
            c = "".join(ch * 2 for ch in c)
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except Exception:
        return default


def _fill_colors(fill: dict | None) -> tuple[tuple[int, int, int], tuple[int, int, int]] | None:
    """(top, bottom) colours of a Fill/Paint dict; None for none/auto."""
    if not fill:
        return None
    t = fill.get("type")
    if t == "solid":
        c = _hex(fill.get("color", "#ffffff"))
        return c, c
    if t in SYSTEM_FILLS:
        a, b = SYSTEM_FILLS[t]
        return _hex(a), _hex(b)
    if t in ("linear", "radial"):
        stops = fill.get("stops") or []
        if stops:
            return _hex(stops[0].get("color", "#fff")), _hex(stops[-1].get("color", "#fff"))
    return None


def _bezier(p0, p1, p2, p3, n=10):
    for i in range(n):
        t = i / n
        u = 1 - t
        yield (
            u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
            u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1],
        )


def _spline_points(spline: dict) -> list[tuple[float, float]]:
    pts = spline.get("points") or []
    out: list[tuple[float, float]] = []
    n = len(pts)
    segs = n if spline.get("closed", True) else n - 1
    for i in range(max(segs, 0)):
        a, b = pts[i], pts[(i + 1) % n]
        out.extend(_bezier(a["co"], a.get("hr", a["co"]), b.get("hl", b["co"]), b["co"]))
    return out


def fake_render_image(args: dict[str, Any]):
    """Pillow image approximating what the worker would render for these `render` args."""
    from PIL import Image, ImageDraw

    project: dict = args.get("project") or {}
    quality = args.get("quality") or "draft"
    size = int(args.get("size") or TIER_SIZE.get(quality, 512))
    full_bleed = bool(args.get("fullBleed"))
    camera = args.get("camera") or project.get("camera") or {}
    appearance = args.get("appearance") or project.get("appearance") or "light"
    transparent = args.get("transparent", True)
    canvas = project.get("canvas") or {}
    plate = canvas.get("plate") or {}
    art = canvas.get("art") or {}
    shape = "square" if full_bleed else canvas.get("shape", "squircle")
    ortho = 2.0 if full_bleed else 2.24 / max(float(camera.get("zoom") or 1.0), 1e-3)
    ppu = size / ortho  # pixels per canvas unit
    cx = cy = size / 2.0

    def px(x: float, y: float) -> tuple[float, float]:
        return cx + x * ppu, cy - y * ppu

    bg = (0, 0, 0, 0)
    backdrop = (project.get("render") or {}).get("backdrop", "transparent")
    if not transparent and backdrop != "transparent":
        bg = _hex((project.get("render") or {}).get("backdropColor", "#1c1c22")) + (255,)
    img = Image.new("RGBA", (size, size), bg)

    dark = appearance in ("dark", "clear-dark", "tinted-dark")
    plate_fill = plate.get("fill")
    if dark:
        plate_fill = ((project.get("appearances") or {}).get("dark") or {}).get("plateFill") or {"type": "system-dark"}
    if plate.get("visible", True) and shape != "none":
        cols = _fill_colors(plate_fill) or ((240, 240, 244), (220, 220, 228))
        grad = Image.new("RGBA", (size, size))
        gd = ImageDraw.Draw(grad)
        for yy in range(size):
            t = yy / max(size - 1, 1)
            gd.line([(0, yy), (size, yy)], fill=tuple(int(cols[0][k] * (1 - t) + cols[1][k] * t) for k in range(3)) + (255,))
        mask = Image.new("L", (size, size), 0)
        md = ImageDraw.Draw(mask)
        x0, y0 = px(-1, 1)
        x1, y1 = px(1, -1)
        box = [x0, y0, x1 - 1, y1 - 1]
        if shape == "circle":
            md.ellipse(box, fill=255)
        elif shape == "square":
            md.rectangle(box, fill=255)
        else:
            r = (float(canvas.get("cornerRadius", 0.225)) * 2 if shape == "rounded" else 0.45) * ppu
            md.rounded_rectangle(box, radius=r, fill=255)
        img.paste(grad, (0, 0), mask)

    # layers from the geometry bundle (flat paint)
    geo: dict = {}
    gp = args.get("geometryPath")
    if gp and Path(gp).is_file():
        try:
            geo = json.loads(Path(gp).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            geo = {}
    s = float(art.get("scale", 1.0))
    ax, ay = float(art.get("x", 0.0)), float(art.get("y", 0.0))
    for li, layer in enumerate(project.get("layers") or []):
        if not layer.get("visible", True):
            continue
        lg = (geo.get("layers") or {}).get(layer.get("id"))
        t = layer.get("transform") or {}
        ls, lx, ly = float(t.get("scale", 1.0)), float(t.get("x", 0.0)), float(t.get("y", 0.0))

        def to_px(p):
            x, y = p[0] * s + ax, p[1] * s + ay
            return px(x * ls + lx, y * ls + ly)

        opacity = float(layer.get("opacity", 1.0))
        regions = (lg or {}).get("regions") or []
        if not regions:  # no geometry: a centred disc per layer so something is visible
            r = 0.6 * (0.8 ** li)
            d = ImageDraw.Draw(img)
            x0, y0 = to_px((-r, r))
            x1, y1 = to_px((r, -r))
            hue = (li * 67) % 360
            col = tuple(int(127 + 120 * math.cos(math.radians(hue + k * 120))) for k in range(3))
            d.ellipse([x0, y0, x1, y1], fill=col + (int(255 * opacity),))
            continue
        for region in regions:
            mask = Image.new("L", (size, size), 0)
            md = ImageDraw.Draw(mask)
            for sp in sorted(region.get("splines") or [], key=lambda q: q.get("depth", 0)):
                pts = [to_px(p) for p in _spline_points(sp)]
                if len(pts) >= 3:
                    md.polygon(pts, fill=0 if sp.get("hole") else 255)
            fill = layer.get("fill") or {"type": "auto"}
            cols = _fill_colors(fill) if fill.get("type") != "auto" else None
            cols = cols or _fill_colors(region.get("paint")) or ((200, 200, 200), (200, 200, 200))
            a = int(255 * opacity * float(region.get("opacity", 1.0)))
            if appearance.startswith("clear") or appearance.startswith("tinted"):
                lum = int(0.2126 * cols[0][0] + 0.7152 * cols[0][1] + 0.0722 * cols[0][2])
                col = (lum, lum, lum)
            else:
                col = cols[0]
            layer_img = Image.new("RGBA", (size, size), col + (0,))
            layer_img.putalpha(mask.point(lambda v: v * a // 255))
            img.alpha_composite(layer_img)
    return img


class FakeBridge(Bridge):
    def __init__(self, *, delay: float = 0.0, start_delay: float = 0.0, available: bool = True) -> None:
        self.delay = delay
        self.start_delay = start_delay
        self._available = available
        self._state: WorkerState = "stopped"
        self._message = ""
        self._log = LogRing()
        self.calls: list[tuple[str, str, dict[str, Any]]] = []   # (mode, cmd, args)
        self.fail: dict[str, str] = {}                             # cmd -> error message
        self.restarts = 0
        self.pid_value = 4242

    # -- status
    @property
    def state(self) -> WorkerState:
        return self._state

    @property
    def info(self) -> dict[str, Any] | None:
        if self._state in ("ready", "busy"):
            return {"port": 0, "pid": self.pid_value, "version": "5.0.0", "device": "OPTIX",
                    "gpu": "Fake GPU", "warmupSeconds": 0.0, "fake": True}
        return None

    @property
    def available(self) -> bool:
        return self._available

    def status(self) -> dict[str, Any]:
        pid = self.pid_value if self._state in ("ready", "busy", "starting") else None
        return {"state": self._state, "pid": pid, "message": self._message or None}

    def logs(self, n: int = 200) -> list[str]:
        return self._log.tail(n)

    def _set(self, state: WorkerState, msg: str = "") -> None:
        self._state, self._message = state, msg
        self._notify()

    # -- lifecycle
    async def start(self) -> None:
        if not self._available:
            self._set("error", "Blender not found (fake)")
            return
        if self._state in ("ready", "busy"):
            return
        self._set("starting", "Launching fake worker…")
        if self.start_delay:
            await asyncio.sleep(self.start_delay)
        self._set("ready", "Blender 5.0.0 · OPTIX · Fake GPU (fake bridge)")
        self._log.append("BIS_WORKER_READY {\"fake\": true}")

    async def stop(self) -> None:
        self._set("stopped", "")

    async def restart(self) -> None:
        self.restarts += 1
        await self.stop()
        await self.start()

    # -- commands
    async def request(self, cmd, args=None, on_progress=None, timeout=None):
        if not self._available:
            raise BlenderUnavailable("Blender not found (fake)")
        if self._state not in ("ready", "busy"):
            await self.start()
        self.calls.append(("persistent", cmd, args or {}))
        self._set("busy", self._message)
        try:
            return await self._execute(cmd, args or {}, on_progress, None)
        finally:
            self._set("ready", self._message)

    async def run_oneshot(self, cmd, args=None, on_progress=None, cancel: CancelToken | None = None, timeout=None):
        if not self._available:
            raise BlenderUnavailable("Blender not found (fake)")
        self.calls.append(("oneshot", cmd, args or {}))
        return await self._execute(cmd, args or {}, on_progress, cancel)

    async def _sleep(self, seconds: float, cancel: CancelToken | None) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if cancel is not None and cancel.cancelled:
                raise JobCancelled()
            await asyncio.sleep(min(0.01, max(end - time.monotonic(), 0)))
        if cancel is not None and cancel.cancelled:
            raise JobCancelled()

    async def _execute(self, cmd: str, args: dict, on_progress: ProgressCallback | None, cancel):
        self._log.append(f"[fake] {cmd}")
        if cmd in self.fail:
            raise WorkerError(self.fail[cmd], "Traceback (fake)")
        if on_progress:
            on_progress(0.1, f"{cmd}: starting")
        await self._sleep(self.delay / 2, cancel)
        if on_progress:
            on_progress(0.5, f"{cmd}: Sample 8/16")
        await self._sleep(self.delay / 2, cancel)
        t0 = time.perf_counter()
        if cmd == "ping":
            return {"pong": True}
        if cmd == "system_info":
            return {"version": "5.0.0", "device": "OPTIX", "gpu": "Fake GPU", "devices": []}
        if cmd == "shutdown":
            return {}
        if cmd == "render":
            img = await asyncio.to_thread(fake_render_image, args)
            out = Path(args["out"])
            out.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(img.save, out, "PNG")
            q = args.get("quality") or "draft"
            return {"path": str(out), "width": img.width, "height": img.height,
                    "seconds": round(time.perf_counter() - t0, 4),
                    "engine": "eevee" if q == "draft" else "cycles", "device": "OPTIX",
                    "samples": {"draft": 16, "preview": 48, "final": 384, "ultra": 1024}.get(q, 16)}
        if cmd == "animate":
            out_dir = Path(args["outDir"])
            out_dir.mkdir(parents=True, exist_ok=True)
            frames = []
            n = max(1, min(int(args.get("frames") or 8), 240))
            base = await asyncio.to_thread(fake_render_image, {**args, "quality": args.get("quality", "draft")})
            for i in range(n):
                if cancel is not None and cancel.cancelled:
                    raise JobCancelled()
                angle = 360.0 * i / n
                fr = base.rotate(angle if args.get("kind") == "turntable" else 8 * math.sin(math.radians(angle)),
                                 resample=3)
                p = out_dir / f"frame_{i + 1:04d}.png"
                await asyncio.to_thread(fr.save, p, "PNG")
                frames.append(str(p))
                if on_progress:
                    on_progress((i + 1) / n, f"Frame {i + 1}/{n}")
            res: dict[str, Any] = {"frames": frames, "seconds": round(time.perf_counter() - t0, 4)}
            if args.get("format") == "mp4":
                video = out_dir / "animation.mp4"
                video.write_bytes(b"\x00\x00\x00\x18ftypmp42fake")
                res["video"] = str(video)
            return res
        if cmd == "save_blend":
            out = Path(args["out"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"BLENDER-v500 fake .blend\n" + json.dumps(args.get("project", {}).get("name", "")).encode())
            return {"path": str(out)}
        if cmd == "swatches":
            out_dir = Path(args["outDir"])
            out_dir.mkdir(parents=True, exist_ok=True)
            from PIL import Image

            files = []
            for name in args.get("presets") or ["liquid_glass", "satin"]:
                p = out_dir / f"{name}.png"
                Image.new("RGBA", (int(args.get("size", 64)),) * 2, (120, 160, 220, 255)).save(p)
                files.append(str(p))
            return {"files": files}
        raise WorkerError(f"unknown command {cmd!r}")
