"""Persist canonical events to the ledger ``events`` table and read them back."""

from __future__ import annotations

from typing import Any

from aletheia.db import session_scope
from aletheia.memory.ledger import Event


def persist_event(evt: dict[str, Any]) -> int:
    with session_scope() as s:
        row = Event(
            run_id=evt.get("run_id"),
            agent=evt.get("agent"),
            parent_tool_use_id=evt.get("parent_tool_use_id"),
            type=evt.get("type", "unknown"),
            payload=evt.get("payload"),
        )
        s.add(row)
        s.flush()
        return row.id


def list_events(run_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    with session_scope() as s:
        q = s.query(Event)
        if run_id:
            q = q.filter(Event.run_id == run_id)
        rows = q.order_by(Event.id.desc()).limit(limit).all()
        rows.reverse()
        return [
            {
                "id": r.id,
                "run_id": r.run_id,
                "agent": r.agent,
                "parent_tool_use_id": r.parent_tool_use_id,
                "type": r.type,
                "payload": r.payload,
                "ts": r.ts.isoformat() if r.ts else None,
            }
            for r in rows
        ]
