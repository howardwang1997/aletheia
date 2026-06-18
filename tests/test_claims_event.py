"""Pin the backend↔frontend ``claims`` event contract.

After the results gate, ``_finalize_claims`` publishes a ``claims`` event carrying the final
claim ledger (the frontend renders a claim-status card from it — ``frontend/lib/useSession.ts``
keeps the latest such event, ``frontend/components/Activity.tsx`` renders the rows). This test
fails if a row ever drops one of the fields the UI reads, so a backend schema change cannot
silently break the dashboard."""

from __future__ import annotations

import asyncio

from aletheia.db import create_all
from aletheia.events.bus import get_bus
from aletheia.memory.service import create_run, finalize_plan
from aletheia.scheduler.driver import ExperimentDriver

_REQUIRED_KEYS = {"claim_type", "status", "strength", "evidence_kinds", "claim_text"}
# the statuses the frontend must render without crashing
_KNOWN_STATUSES = {"proposed", "supported", "refuted", "unverified", "not_evaluated"}


async def _run_and_capture_claims_event(run_id: str) -> dict | None:
    """Run a dry-run loop while subscribing to the bus; return the last ``claims`` event payload."""
    captured: list[dict] = []

    async def sub():
        async for evt in get_bus().subscribe():
            if evt.get("run_id") == run_id and evt.get("type") == "claims":
                captured.append(evt)

    task = asyncio.create_task(sub())
    await asyncio.sleep(0)
    await ExperimentDriver(run_id, dry_run=True).run()
    await asyncio.sleep(0.1)  # let the final publish drain to the subscriber
    task.cancel()
    return captured[-1] if captured else None


def test_claims_event_has_stable_row_shape():
    create_all()
    run_id = create_run("claims-event shape", domain="materials", status="planned")
    finalize_plan(run_id, {"objective": "predict band gap", "domain": "materials"})

    evt = asyncio.run(_run_and_capture_claims_event(run_id))
    assert evt is not None, "a `claims` event must be published after claim finalization"

    rows = (evt.get("payload") or {}).get("claims")
    assert isinstance(rows, list) and rows, "the claims event carries a non-empty list of rows"
    for row in rows:
        assert _REQUIRED_KEYS <= set(row), f"row missing keys: {_REQUIRED_KEYS - set(row)}"
        assert isinstance(row["evidence_kinds"], list)  # sorted set of evidence kinds
        assert isinstance(row["claim_text"], str)
        assert row["status"] in _KNOWN_STATUSES, f"unexpected status the UI can't render: {row['status']}"
