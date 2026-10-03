"""In-process pub/sub for the /ws stream (WsEvent: job | system | project).

Publishing never blocks: each subscriber has a bounded queue and the oldest events are dropped when a slow
client falls behind (job/system events are state snapshots, so the newest one always wins).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .util import to_jsonable

log = logging.getLogger("bis.events")


class Subscription:
    def __init__(self, hub: "EventHub", maxsize: int) -> None:
        self._hub = hub
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=maxsize)

    def put(self, event: dict) -> None:
        while True:
            try:
                self.queue.put_nowait(event)
                return
            except asyncio.QueueFull:
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover
                    return

    async def get(self) -> dict:
        return await self.queue.get()

    def close(self) -> None:
        self._hub.unsubscribe(self)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class EventHub:
    def __init__(self, queue_size: int = 512) -> None:
        self._subs: set[Subscription] = set()
        self._queue_size = queue_size
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> Subscription:
        sub = Subscription(self, self._queue_size)
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subs.discard(sub)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def publish(self, event: dict[str, Any]) -> None:
        """Publish from the event loop thread."""
        payload = to_jsonable(event)
        for sub in list(self._subs):
            sub.put(payload)

    def publish_threadsafe(self, event: dict[str, Any]) -> None:
        """Publish from any thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self.publish(event)
        else:
            loop.call_soon_threadsafe(self.publish, event)
