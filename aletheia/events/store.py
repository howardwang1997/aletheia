"""Persist canonical events to the ledger ``events`` table and read them back."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from aletheia.db import session_scope
from aletheia.memory.ledger import Event
from aletheia.reproducibility.manifest import content_sha256


class EventIdentityConflict(RuntimeError):
    """A durable event key is already bound to different canonical content."""


def _event_projection(evt: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": evt.get("run_id"),
        "agent": evt.get("agent"),
        "parent_tool_use_id": evt.get("parent_tool_use_id"),
        "type": evt.get("type", "unknown"),
        "payload": evt.get("payload"),
    }


def persist_event(
    evt: dict[str, Any],
    *,
    event_key: str | None = None,
    session: Session | None = None,
    flush_pending: bool = True,
) -> int:
    """Persist one event, optionally inside the caller's transaction.

    A keyed event is immutable and idempotent: replaying exact content returns the existing id,
    while rebinding the key fails closed.  Passing a session lets queue state and its observable
    transition commit atomically.
    """

    if session is None:
        with session_scope() as owned_session:
            return persist_event(
                evt,
                event_key=event_key,
                session=owned_session,
                flush_pending=flush_pending,
            )

    projection = _event_projection(evt)
    key = event_key if event_key is not None else evt.get("event_key")
    if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 128):
        raise ValueError("event_key must contain 1-128 characters")
    event_sha256 = content_sha256(projection) if key is not None else None
    supplied_sha256 = evt.get("event_sha256")
    if supplied_sha256 is not None and supplied_sha256 != event_sha256:
        raise EventIdentityConflict("supplied durable event SHA-256 does not match its content")

    values = {
        "event_key": key,
        "event_sha256": event_sha256,
        **projection,
    }
    if key is None:
        row = Event(**values)
        session.add(row)
        session.flush()
        return row.id

    inserted_id = session.scalar(
        postgresql_insert(Event)
        .values(**values)
        .on_conflict_do_nothing(constraint="uq_events_event_key")
        .returning(Event.id)
    )
    if flush_pending:
        session.flush()
    if inserted_id is not None:
        return inserted_id

    row = session.scalar(select(Event).where(Event.event_key == key))
    if row is None:
        raise EventIdentityConflict("durable event insert conflicted without a readable row")
    stored = {
        "run_id": row.run_id,
        "agent": row.agent,
        "parent_tool_use_id": row.parent_tool_use_id,
        "type": row.type,
        "payload": row.payload,
    }
    if row.event_sha256 != event_sha256 or stored != projection:
        raise EventIdentityConflict(
            f"durable event key {key!r} is already bound to different content"
        )
    return row.id


def _row_to_dict(r: Event) -> dict[str, Any]:
    if r.event_key is not None:
        projection = {
            "run_id": r.run_id,
            "agent": r.agent,
            "parent_tool_use_id": r.parent_tool_use_id,
            "type": r.type,
            "payload": r.payload,
        }
        if r.event_sha256 != content_sha256(projection):
            raise EventIdentityConflict(
                f"durable event {r.event_key!r} no longer matches its SHA-256"
            )
    return {
        "id": r.id,
        "event_key": r.event_key,
        "event_sha256": r.event_sha256,
        "run_id": r.run_id,
        "agent": r.agent,
        "parent_tool_use_id": r.parent_tool_use_id,
        "type": r.type,
        "payload": r.payload,
        "ts": r.ts.isoformat() if r.ts else None,
    }


def list_events(run_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    with session_scope() as s:
        q = s.query(Event)
        if run_id:
            q = q.filter(Event.run_id == run_id)
        rows = q.order_by(Event.id.desc()).limit(limit).all()
        rows.reverse()
        return [_row_to_dict(r) for r in rows]


def latest_event_id(run_id: str | None = None) -> int:
    """Return the current durable stream cursor, or zero for an empty stream."""

    with session_scope() as session:
        query = select(func.max(Event.id))
        if run_id is not None:
            query = query.where(Event.run_id == run_id)
        return int(session.scalar(query) or 0)


def list_events_after(
    after_id: int,
    *,
    run_id: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Read an ascending, resumable page from the database-backed event stream."""

    if after_id < 0:
        raise ValueError("after_id cannot be negative")
    if not 1 <= limit <= 5_000:
        raise ValueError("event page limit must be between 1 and 5000")
    with session_scope() as session:
        query = select(Event).where(Event.id > after_id)
        if run_id is not None:
            query = query.where(Event.run_id == run_id)
        rows = session.scalars(query.order_by(Event.id.asc()).limit(limit)).all()
        return [_row_to_dict(row) for row in rows]


def list_run_events(run_id: str) -> list[dict[str, Any]]:
    """ALL events for a run in chronological (insertion) order — unbounded.

    Unlike :func:`list_events` (capped at ``limit``, newest-first), this returns the complete
    ordered stream, for faithful conversation-record export."""
    with session_scope() as s:
        rows = s.query(Event).filter(Event.run_id == run_id).order_by(Event.id.asc()).all()
        return [_row_to_dict(r) for r in rows]


def list_run_ids_with_events(limit: int | None = None) -> list[str]:
    """Run ids that have at least one event, newest-activity first (by latest event id)."""
    from sqlalchemy import func

    with session_scope() as s:
        q = (
            s.query(Event.run_id)
            .filter(Event.run_id.isnot(None))
            .group_by(Event.run_id)
            .order_by(func.max(Event.id).desc())
        )
        if limit:
            q = q.limit(limit)
        return [r[0] for r in q.all()]
