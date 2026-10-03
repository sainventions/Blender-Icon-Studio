"""JobManager — one global, serial GPU queue (asyncio).

* Jobs (bis.models.Job) run one at a time: the persistent worker and one-shot Blender processes share one
  8 GB GPU with the desktop, so nothing renders concurrently.
* Live jobs jump ahead of normal jobs (priority) and are **coalesced**: submitting a job with the same
  coalesce key as a *queued* job replaces it in place (the old one is cancelled as "superseded").
* Cancel: queued → cancelled immediately; running → the job's CancelToken fires (one-shot runners kill their
  Blender process; persistent-worker renders cannot be interrupted, so the job is marked cancelled at once and
  its eventual result is discarded).
* Every state/progress change is published as ``{"type": "job", "job": Job}`` (progress throttled to 10 Hz).
* Finished jobs stay queryable in a bounded in-memory history (default 200).
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Hashable, Optional

from .blender.base import CancelToken, JobCancelled
from .models import Job
from .util import new_id, now_iso

log = logging.getLogger("bis.jobs")

PRIORITY_LIVE = 0
PRIORITY_NORMAL = 10
PRIORITY_BACKGROUND = 20
_PUBLISH_INTERVAL = 0.1


class JobContext:
    """Handed to a job runner: progress reporting + cancellation."""

    def __init__(self, manager: "JobManager", job: Job) -> None:
        self._manager = manager
        self.job = job
        self.token = CancelToken()
        self._last_publish = 0.0

    @property
    def cancelled(self) -> bool:
        return self.token.cancelled

    def check(self) -> None:
        self.token.raise_if_cancelled()

    def progress(self, value: float, message: str | None = None) -> None:
        if self.token.cancelled or self.job.state != "running":
            return
        value = min(max(float(value), 0.0), 1.0)
        # never go backwards (sub-steps report their own 0..1)
        self.job.progress = max(self.job.progress, round(value, 4))
        if message:
            self.job.message = message
        now = time.monotonic()
        if now - self._last_publish >= _PUBLISH_INTERVAL or value >= 1.0:
            self._last_publish = now
            self._manager._publish(self.job)

    def message(self, message: str) -> None:
        self.progress(self.job.progress, message)

    async def yield_to_live(self) -> None:
        """Let queued *live* jobs (drafts, auto previews) run now, between two GPU steps of a long job
        (exports render a dozen masters). The GPU is idle at that point, so the queue stays serial."""
        await self._manager._run_live_between(self)

    def sub(self, start: float, end: float, prefix: str = "") -> Callable[[float, str], None]:
        """Progress callback mapping a child's 0..1 into [start, end] of this job."""

        def cb(p: float, msg: str = "") -> None:
            text = f"{prefix}{msg}" if msg else (prefix.rstrip(": ") or None)
            self.progress(start + (end - start) * min(max(p, 0.0), 1.0), text)

        return cb


JobRunner = Callable[[JobContext], Awaitable[Optional[dict[str, Any]]]]


@dataclass(eq=False)  # identity semantics: queue membership / removal must never compare jobs
class _Entry:
    job: Job
    runner: JobRunner
    ctx: JobContext
    live: bool
    key: Hashable | None
    priority: int
    seq: int
    meta: dict[str, Any] = field(default_factory=dict)


class JobManager:
    def __init__(self, publish: Callable[[dict[str, Any]], None] | None = None, history: int = 200) -> None:
        self._publish_fn = publish
        self._history_size = history
        self._queue: list[_Entry] = []
        self._active: dict[str, _Entry] = {}
        self._history: OrderedDict[str, Job] = OrderedDict()
        self._running: _Entry | None = None
        self._seq = itertools.count()
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self.on_finished: list[Callable[[Job, dict[str, Any]], None]] = []
        self.on_queue_change: list[Callable[[], None]] = []

    # ------------------------------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._wake = asyncio.Event()
            if self._queue:
                self._wake.set()
            self._task = asyncio.get_running_loop().create_task(self._loop(), name="bis-job-loop")

    async def stop(self) -> None:
        for e in list(self._queue):
            self._finalize_cancel(e, "Server shutting down")
        self._queue.clear()
        if self._running is not None:
            self._running.ctx.token.cancel()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def wait_idle(self, timeout: float = 30.0) -> None:
        """Wait until nothing is queued or running (tests)."""
        deadline = time.monotonic() + timeout
        while self._queue or self._running is not None:
            if time.monotonic() > deadline:
                raise TimeoutError("job queue did not drain")
            await asyncio.sleep(0.01)

    # ------------------------------------------------------------------------------------------ queries
    def get(self, job_id: str) -> Job | None:
        e = self._active.get(job_id)
        if e is not None:
            return e.job
        return self._history.get(job_id)

    def list(self, project_id: str | None = None, limit: int | None = None) -> list[Job]:
        jobs = [e.job for e in self._active.values()] + list(self._history.values())
        if project_id is not None:
            jobs = [j for j in jobs if j.projectId == project_id]
        jobs.sort(key=lambda j: j.createdAt, reverse=True)
        return jobs[:limit] if limit else jobs

    def counts(self) -> dict[str, int]:
        return {"queued": len(self._queue), "running": 1 if self._running is not None else 0}

    def pending(self, predicate: Callable[[Job, dict[str, Any]], bool]) -> list[Job]:
        """Queued or running jobs matching predicate(job, meta)."""
        out = [e.job for e in self._queue if predicate(e.job, e.meta)]
        if self._running is not None and predicate(self._running.job, self._running.meta):
            out.append(self._running.job)
        return out

    # ------------------------------------------------------------------------------------------ submit / cancel
    def submit(
        self,
        kind: str,
        runner: JobRunner,
        *,
        project_id: str | None = None,
        request: dict[str, Any] | None = None,
        live: bool = False,
        key: Hashable | None = None,
        priority: int | None = None,
        message: str = "Queued",
        meta: dict[str, Any] | None = None,
    ) -> Job:
        job = Job(
            id=new_id(),
            projectId=project_id,
            kind=kind,  # type: ignore[arg-type]
            state="queued",
            message=message,
            createdAt=now_iso(),
            request=request or {},
        )
        entry = _Entry(
            job=job,
            runner=runner,
            ctx=JobContext(self, job),
            live=live,
            key=key,
            priority=priority if priority is not None else (PRIORITY_LIVE if live else PRIORITY_NORMAL),
            seq=next(self._seq),
            meta=dict(meta or {}),
        )
        replaced: _Entry | None = None
        if key is not None:
            for i, old in enumerate(self._queue):
                if old.key == key:
                    entry.seq = old.seq  # take over the queue slot
                    self._queue[i] = entry
                    replaced = old
                    break
        if replaced is None:
            self._queue.append(entry)
        self._active[job.id] = entry
        self._publish(job)
        if replaced is not None:
            self._finalize_cancel(replaced, f"Superseded by {job.id}")
        self._queue_changed()
        if self._wake is not None:
            self._wake.set()
        return job

    def cancel(self, job_id: str) -> Job | None:
        entry = self._active.get(job_id)
        if entry is None:
            return self._history.get(job_id)
        if entry in self._queue:
            self._queue.remove(entry)
            self._finalize_cancel(entry, "Cancelled")
            self._queue_changed()
            return entry.job
        # running
        entry.ctx.token.cancel()
        job = entry.job
        if job.state == "running":
            job.state = "cancelled"
            job.message = "Cancelled"
            job.finishedAt = now_iso()
            self._publish(job)
        return job

    def cancel_where(self, predicate: Callable[[Job], bool]) -> int:
        n = 0
        for e in list(self._active.values()):
            if predicate(e.job) and e.job.state in ("queued", "running"):
                self.cancel(e.job.id)
                n += 1
        return n

    # ------------------------------------------------------------------------------------------ internals
    def _publish(self, job: Job) -> None:
        if self._publish_fn is not None:
            try:
                self._publish_fn({"type": "job", "job": job.model_dump(mode="json")})
            except Exception:  # pragma: no cover - defensive
                log.exception("job publish failed")

    def _queue_changed(self) -> None:
        for cb in list(self.on_queue_change):
            try:
                cb()
            except Exception:  # pragma: no cover - defensive
                log.exception("queue-change callback failed")

    def _finalize_cancel(self, entry: _Entry, message: str) -> None:
        entry.ctx.token.cancel()
        job = entry.job
        job.state = "cancelled"
        job.message = message
        job.finishedAt = now_iso()
        self._retire(entry)
        self._publish(job)

    def _retire(self, entry: _Entry) -> None:
        self._active.pop(entry.job.id, None)
        self._history[entry.job.id] = entry.job
        self._history.move_to_end(entry.job.id)
        while len(self._history) > self._history_size:
            self._history.popitem(last=False)

    def _pop_next(self) -> _Entry | None:
        if not self._queue:
            return None
        entry = min(self._queue, key=lambda e: (e.priority, e.seq))
        self._queue.remove(entry)
        return entry

    async def _loop(self) -> None:
        assert self._wake is not None
        while True:
            entry = self._pop_next()
            if entry is None:
                self._wake.clear()
                await self._wake.wait()
                continue
            await self._execute(entry)

    async def _run_live_between(self, ctx: JobContext) -> None:
        """Run queued live jobs inline while the job owning `ctx` is paused between GPU steps."""
        outer = self._running
        if outer is None or outer.ctx is not ctx or outer.live:
            return
        # Only the live jobs queued *now* (one pass): a user who keeps editing must not starve the export —
        # newer drafts wait for its next step boundary.
        batch = sorted((e for e in self._queue if e.live), key=lambda e: (e.priority, e.seq))
        for entry in batch:
            if ctx.cancelled:
                return
            if entry not in self._queue:  # cancelled or superseded meanwhile
                continue
            self._queue.remove(entry)
            outer.job.message, saved = "Paused for a live render…", outer.job.message
            self._publish(outer.job)
            try:
                await self._execute(entry)
            finally:
                self._running = outer
                if outer.job.state == "running":
                    outer.job.message = saved
                    self._publish(outer.job)
                self._queue_changed()

    async def _execute(self, entry: _Entry) -> None:
        job, ctx = entry.job, entry.ctx
        self._running = entry
        job.state = "running"
        job.startedAt = now_iso()
        if job.message in ("Queued", ""):
            job.message = "Starting…"
        self._publish(job)
        self._queue_changed()
        t0 = time.perf_counter()
        try:
            result = await entry.runner(ctx)
            if ctx.cancelled:
                self._mark_cancelled(job)
            else:
                job.state = "done"
                job.progress = 1.0
                job.result = result or {}
                job.message = "Done"
        except JobCancelled:
            self._mark_cancelled(job)
        except asyncio.CancelledError:
            self._mark_cancelled(job)
            raise
        except Exception as e:  # noqa: BLE001 - every failure becomes a job error
            if ctx.cancelled:
                self._mark_cancelled(job)
            else:
                log.exception("job %s (%s) failed", job.id, job.kind)
                job.state = "error"
                job.error = str(e) or type(e).__name__
                job.message = "Failed"
                tb = getattr(e, "traceback", "")
                if tb:
                    job.result = {"traceback": tb[-4000:]}
        finally:
            job.finishedAt = job.finishedAt if job.state == "cancelled" and job.finishedAt else now_iso()
            self._running = None
            self._retire(entry)
            log.info("job %s %s %s in %.2fs", job.id, job.kind, job.state, time.perf_counter() - t0)
            self._publish(job)
            self._queue_changed()
            for cb in list(self.on_finished):
                try:
                    cb(job, entry.meta)
                except Exception:  # pragma: no cover - defensive
                    log.exception("on_finished callback failed")

    @staticmethod
    def _mark_cancelled(job: Job) -> None:
        if job.state != "cancelled":
            job.state = "cancelled"
            job.message = "Cancelled"
        job.result = None
