"""Phase 0 walking-skeleton verification.

Proves the backbone end-to-end without spending model quota: a dry-run
orchestrator turn flows through the event bus, persists to the ledger, and writes
a work-log (decision) entry. Requires Postgres up (docker compose up -d).
"""

from __future__ import annotations

from aletheia.db import create_all, session_scope
from aletheia.memory.ledger import Decision, Event
from aletheia.memory.service import create_run
from aletheia.orchestrator.client import run_dryrun


def test_tables_create():
    create_all()  # idempotent


async def test_dryrun_persists_events_and_worklog():
    create_all()
    run_id = create_run("phase-0 skeleton test", domain="meta")

    await run_dryrun(run_id, "verify the skeleton")

    with session_scope() as s:
        events = s.query(Event).filter(Event.run_id == run_id).all()
        decisions = s.query(Decision).filter(Decision.run_id == run_id).all()

    types = {e.type for e in events}
    expected = {"run_started", "assistant_text", "tool_use", "memory_log", "result", "run_finished"}
    assert expected <= types, f"missing event types: {expected - types}"
    assert len(decisions) >= 1, "expected at least one work-log (decision) entry"
