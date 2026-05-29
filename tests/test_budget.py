"""Phase 1 step 7: a tiny budget cap auto-pauses the run (lights-out but bounded)."""

from __future__ import annotations

import pytest

from aletheia.data.registry import register_dataset
from aletheia.db import create_all, session_scope
from aletheia.events.store import list_events
from aletheia.memory.ledger import Run
from aletheia.memory.service import budget_spent, create_run, finalize_plan
from aletheia.scheduler.driver import ExperimentDriver


@pytest.mark.asyncio
async def test_tiny_cap_auto_pauses():
    create_all()
    # cap well below one stage's estimated cost (0.10) -> pause on the first charge
    run_id = create_run("budget test", domain="materials", status="scoping", budget_cap_usd=0.01)
    register_dataset(run_id, "benchmark", ref="matbench_expt_gap", status="ready")
    finalize_plan(run_id, {"objective": "bandgap", "domain": "materials"})

    driver = ExperimentDriver(run_id, dry_run=True)
    await driver.run()

    with session_scope() as s:
        run = s.get(Run, run_id)
        assert run.status == "paused"
    # a breach event was emitted and spend was recorded
    types = {e["type"] for e in list_events(run_id, 500)}
    assert "budget_breach" in types
    assert budget_spent(run_id, "usd") > 0.0


@pytest.mark.asyncio
async def test_generous_cap_completes():
    create_all()
    run_id = create_run("budget ok", domain="materials", status="scoping", budget_cap_usd=100.0)
    register_dataset(run_id, "benchmark", ref="matbench_expt_gap", status="ready")
    finalize_plan(run_id, {"objective": "bandgap", "domain": "materials"})

    driver = ExperimentDriver(run_id, dry_run=True)
    await driver.run()

    with session_scope() as s:
        assert s.get(Run, run_id).status == "completed"
