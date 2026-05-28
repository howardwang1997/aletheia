"""Server-Sent Events: live telemetry stream for the dashboard."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from aletheia.events.bus import get_bus

router = APIRouter(tags=["events"])


@router.get("/events")
async def events(request: Request, run_id: str | None = None):
    bus = get_bus()

    async def gen():
        async for evt in bus.subscribe():
            if await request.is_disconnected():
                break
            if run_id and evt.get("run_id") != run_id:
                continue
            yield {"event": evt.get("type", "message"), "data": json.dumps(evt, default=str)}

    return EventSourceResponse(gen())
