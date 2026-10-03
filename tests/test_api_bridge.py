"""BlenderBridge tests against a stand-in Blender process (bis/blender/fakeproc.py, stdlib Python).

Exercises the real process/TCP plumbing: READY handshake, request ids + progress, stdout draining under heavy
output, worker errors, crash detection + auto-restart, startup watchdog, one-shot BIS_EVENT streaming and
cancel = kill.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from bis.blender import BlenderBridge, CancelToken, JobCancelled, WorkerCrashed, WorkerError  # noqa: E402
from bis.blender import fakeproc  # noqa: E402
from bis.testing import make_test_settings  # noqa: E402

FAKEPROC = Path(fakeproc.__file__)


def make_bridge(tmp_path, **kw) -> BlenderBridge:
    settings = make_test_settings(tmp_path, worker_startup_timeout=kw.pop("startup_timeout", 20.0),
                                  worker_request_timeout=kw.pop("request_timeout", 20.0))
    return BlenderBridge(settings, launcher=[sys.executable, str(FAKEPROC)],
                         worker_script=FAKEPROC, oneshot_script=FAKEPROC, **kw)


async def wait_state(bridge: BlenderBridge, state: str, timeout: float = 20.0) -> None:
    end = time.monotonic() + timeout
    while bridge.state != state:
        if time.monotonic() > end:
            raise AssertionError(f"bridge state {bridge.state!r} != {state!r}; logs: {bridge.logs(20)}")
        await asyncio.sleep(0.02)


def test_persistent_worker_roundtrip(tmp_path):
    async def main():
        b = make_bridge(tmp_path)
        states: list[str] = []
        b.on_status = lambda: states.append(b.state)
        assert b.status()["state"] == "stopped"
        await b.start()
        await wait_state(b, "ready")
        assert b.info["device"] == "OPTIX" and b.pid
        assert "Blender 5.0.0" in b.status()["message"]
        assert await b.request("ping") == {"pong": True}

        progress: list[tuple[float, str]] = []
        out = tmp_path / "r.png"
        res = await b.request("render", {"size": 32, "out": str(out)}, on_progress=lambda p, m: progress.append((p, m)))
        assert res["width"] == 32 and out.is_file() and out.read_bytes()[:4] == b"\x89PNG"
        assert [p for p, _ in progress] == [0.25, 0.5, 1.0] and progress[0][1] == "Sample 4/16"
        assert "starting" in states and "busy" in states and states[-1] == "ready"

        # 20k noisy stdout lines (~3 MB): only completes if the bridge keeps draining the pipe
        t0 = time.monotonic()
        assert await b.request("spam", {"lines": 20000}) == {"spammed": True}
        assert time.monotonic() - t0 < 15
        assert any("Syncing object" in ln for ln in b.logs(50))

        with pytest.raises(WorkerError) as ei:
            await b.request("fail")
        assert "requested failure" in str(ei.value) and "Traceback" in ei.value.traceback
        assert await b.request("ping") == {"pong": True}  # still alive after an error event

        # concurrent requests are serialised and each gets its own answer
        r = await asyncio.gather(*(b.request("ping") for _ in range(5)))
        assert r == [{"pong": True}] * 5

        await b.stop()
        assert b.state == "stopped" and b.pid is None

    asyncio.run(main())


def test_crash_fails_request_and_auto_restarts(tmp_path):
    async def main():
        b = make_bridge(tmp_path)
        await b.start()
        await wait_state(b, "ready")
        pid1 = b.pid
        messages: list[str] = []
        b.on_status = lambda: messages.append(f"{b.state}: {b.status()['message']}")
        with pytest.raises(WorkerCrashed):
            await b.request("crash")
        assert b.state == "error"
        end = time.monotonic() + 20
        while not (b.state == "ready" and b.pid not in (None, pid1)):
            assert time.monotonic() < end, messages
            await asyncio.sleep(0.02)
        assert any("restarting in" in m for m in messages), messages
        assert any(m.startswith("starting") for m in messages)
        assert await b.request("ping") == {"pong": True}
        await b.stop()

    asyncio.run(main())


def test_request_starts_worker_on_demand(tmp_path):
    async def main():
        b = make_bridge(tmp_path)
        assert b.state == "stopped"
        assert await b.request("ping") == {"pong": True}
        assert b.state == "ready"
        await b.restart()
        await wait_state(b, "ready")
        assert await b.request("ping") == {"pong": True}
        await b.stop()

    asyncio.run(main())


def test_startup_watchdog(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKEPROC_NO_READY", "1")

    async def main():
        b = make_bridge(tmp_path, startup_timeout=1.0, max_crashes=0)
        await b.start()
        await asyncio.sleep(0.2)
        assert b.state == "starting"
        await wait_state(b, "error", timeout=10)
        assert "did not report ready" in b.logs(50)[-2] or any("did not report ready" in ln for ln in b.logs(50))
        await b.stop()

    asyncio.run(main())


def test_oneshot_success_error_and_cancel(tmp_path):
    async def main():
        b = make_bridge(tmp_path)
        out = tmp_path / "final.png"
        seen: list[float] = []
        res = await b.run_oneshot("render", {"size": 16, "out": str(out)}, on_progress=lambda p, m: seen.append(p))
        assert res["path"] == str(out) and out.is_file() and seen == [0.25, 0.5, 1.0]
        assert not list((b.settings.tmp_dir / "oneshot").glob("*.json"))  # job file cleaned up
        assert b.state == "stopped"  # one-shots never touch the persistent worker

        with pytest.raises(WorkerError, match="requested failure"):
            await b.run_oneshot("fail", {})
        with pytest.raises(WorkerCrashed, match="code 3"):
            await b.run_oneshot("crash", {})

        token = CancelToken()
        task = asyncio.create_task(b.run_oneshot("sleep", {"seconds": 30}, cancel=token))
        await asyncio.sleep(1.0)
        t0 = time.monotonic()
        token.cancel()
        with pytest.raises(JobCancelled):
            await task
        assert time.monotonic() - t0 < 5
        assert not b._oneshots  # process reaped

    asyncio.run(main())


def test_missing_blender_reports_error(tmp_path):
    async def main():
        settings = make_test_settings(tmp_path, blender_exe=tmp_path / "nope" / "blender.exe")
        b = BlenderBridge(settings)
        assert not b.available
        await b.start()
        assert b.state == "error" and "not found" in b.status()["message"]
        from bis.blender import BlenderUnavailable

        with pytest.raises(BlenderUnavailable):
            await b.request("ping")
        with pytest.raises(BlenderUnavailable):
            await b.run_oneshot("render", {})

    asyncio.run(main())


def test_crash_loop_gives_up_and_fails_fast(tmp_path):
    """After the crash budget is spent the bridge stops restarting and waiting requests fail at once."""

    async def main():
        b = make_bridge(tmp_path, max_crashes=0)
        await b.start()
        await wait_state(b, "ready")
        with pytest.raises(WorkerCrashed):
            await b.request("crash")
        await asyncio.sleep(0.5)
        assert b.state == "error" and "crashed" in b.status()["message"] and b.pid is None
        # an explicit request tries once more (and works: the stand-in only crashes on demand)
        assert await b.request("ping") == {"pong": True}
        await b.stop()

    asyncio.run(main())


def test_oneshot_that_does_not_exit_after_its_result(tmp_path, monkeypatch):
    """A one-shot that reports `done` but hangs (driver teardown) is killed after a grace period and its
    result is still returned: the GPU queue must not stall until the 3 h one-shot timeout."""
    from bis.blender import bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "ONESHOT_EXIT_GRACE", 0.5)

    async def main():
        b = make_bridge(tmp_path)
        t0 = time.monotonic()
        assert await b.run_oneshot("linger", {"seconds": 60}) == {"lingered": True}
        assert time.monotonic() - t0 < 10
        assert not b._oneshots
        assert any("did not exit" in ln for ln in b.logs(50))

    asyncio.run(main())


def test_stale_oneshot_job_files_are_pruned(tmp_path):
    import os

    async def main():
        b = make_bridge(tmp_path)
        jobs_dir = b.settings.tmp_dir / "oneshot"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        old, fresh = jobs_dir / "render-old.json", jobs_dir / "render-fresh.json"
        old.write_text("{}")
        fresh.write_text("{}")
        week_ago = time.time() - 7 * 24 * 3600
        os.utime(old, (week_ago, week_ago))
        assert await b.run_oneshot("render", {"size": 8, "out": str(tmp_path / "x.png")})
        assert not old.exists() and fresh.exists()

    asyncio.run(main())
