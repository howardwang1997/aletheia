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
            # Emit UNNAMED frames (default "message" event) so the browser's
            # EventSource.onmessage fires. The event type lives inside the JSON
            # payload (read as e.type on the client). Naming the SSE event here
            # would route frames to addEventListener(type) and bypass onmessage.
            yield {"data": json.dumps(evt, default=str)}

    return EventSourceResponse(gen())
