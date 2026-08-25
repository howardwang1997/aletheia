"""In-process low-latency event fan-out layered over the durable event store.

Single source of observability: the orchestrator and all services publish
canonical events here. The bus persists each event to the ledger and fans it out
to local subscribers.  The FastAPI SSE endpoint reads the database cursor directly, so replay and
cross-process delivery do not depend on this process-local optimization.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from aletheia.events.store import persist_event


class EventBus:
    def __init__(self, max_queue: int = 1000) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._max_queue = max_queue

    async def publish(self, event: dict[str, Any]) -> dict[str, Any]:
        event.setdefault("ts", datetime.now(timezone.utc).isoformat())
        # Persist off the event loop (sync SQLAlchemy).
        try:
            event["id"] = await asyncio.to_thread(persist_event, event)
        except Exception as exc:  # never let persistence break the live stream
            event.setdefault("id", None)
            event["_persist_error"] = str(exc)
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
        return event

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subscribers.discard(q)


_BUS: EventBus | None = None


def get_bus() -> EventBus:
    global _BUS
    if _BUS is None:
        _BUS = EventBus()
    return _BUS


def make_event(
    type: str,
    run_id: str | None = None,
    agent: str = "orchestrator",
    parent_tool_use_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "agent": agent,
        "parent_tool_use_id": parent_tool_use_id,
        "type": type,
        "payload": payload or {},
    }
