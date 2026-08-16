"""Durable SSE cursor semantics independent of any process-local EventBus subscriber."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from fastapi import HTTPException

from aletheia.api.events_sse import _requested_cursor, events
from aletheia.db import create_all
from aletheia.events.bus import make_event
from aletheia.events.store import latest_event_id, persist_event


class _ConnectedRequest:
    headers: dict[str, str] = {}

    async def is_disconnected(self) -> bool:
        return False


def test_cursor_prefers_query_then_standard_resume_header():
    assert _requested_cursor(12, "9") == 12
    assert _requested_cursor(None, "9") == 9
    assert _requested_cursor(None, None) is None
    with pytest.raises(HTTPException) as malformed:
        _requested_cursor(None, "not-an-integer")
    assert malformed.value.status_code == 400


def test_sse_replays_database_event_with_id_frame():
    create_all()
    run_id = uuid.uuid4().hex
    cursor = latest_event_id(run_id)
    event_id = persist_event(
        make_event("durable_sse_test", run_id=run_id, payload={"cross_process": True}),
        event_key=f"sse:{uuid.uuid4().hex}",
    )

    async def receive_one():
        response = await events(_ConnectedRequest(), run_id=run_id, after_id=cursor)
        frame = await anext(response.body_iterator)
        await response.body_iterator.aclose()
        return frame

    frame = asyncio.run(receive_one())
    assert frame["id"] == str(event_id)
    payload = json.loads(frame["data"])
    assert payload["id"] == event_id
    assert payload["payload"] == {"cross_process": True}
