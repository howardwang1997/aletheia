"""Server-Sent Events backed by the durable Postgres event cursor."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from aletheia.events.store import latest_event_id, list_events_after

router = APIRouter(tags=["events"])


def _requested_cursor(after_id: int | None, last_event_id: str | None) -> int | None:
    if after_id is not None:
        if after_id < 0:
            raise HTTPException(status_code=422, detail="after_id cannot be negative")
        return after_id
    if last_event_id is None or not last_event_id.strip():
        return None
    try:
        cursor = int(last_event_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be an integer") from exc
    if cursor < 0:
        raise HTTPException(status_code=400, detail="Last-Event-ID cannot be negative")
    return cursor


@router.get("/events")
async def events(
    request: Request,
    run_id: str | None = None,
    after_id: int | None = None,
):
    """Tail or resume the canonical stream across API processes and restarts.

    A fresh connection starts at the current tail, preserving the dashboard's live-only default.
    ``after_id`` or the standard ``Last-Event-ID`` header requests lossless replay after a known
    cursor.  Event ids come from the database rather than process-local pub/sub state.
    """

    cursor = _requested_cursor(after_id, request.headers.get("last-event-id"))
    if cursor is None:
        cursor = await asyncio.to_thread(latest_event_id, run_id)

    async def gen():
        nonlocal cursor
        while True:
            if await request.is_disconnected():
                break
            batch = await asyncio.to_thread(
                list_events_after,
                cursor,
                run_id=run_id,
                limit=500,
            )
            if not batch:
                await asyncio.sleep(0.25)
                continue
            for evt in batch:
                if await request.is_disconnected():
                    return
                cursor = int(evt["id"])
                # Keep frames unnamed so EventSource.onmessage continues to fire.  The typed
                # event name remains in JSON for the existing frontend contract.
                yield {
                    "id": str(cursor),
                    "data": json.dumps(evt, default=str),
                }

    return EventSourceResponse(gen(), ping=15)
