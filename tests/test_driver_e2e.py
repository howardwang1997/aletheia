"""Phase 1 step 6: full dry-run end-to-end loop.

reasoning-dry + compute-dry + critic-dry: launch the driver from a finalized plan
and assert the ledger captured a complete research loop with zero approvals —
metrics, two critique panels (design + results), a report artifact, and an
archived/completed run.
"""

from __future__ import annotations

import pytest

from aletheia.data.registry import register_dataset
from aletheia.db import create_all, session_scope
from aletheia.memory.ledger import CritiquePanel as CritiquePanelRow
from aletheia.memory.ledger import Run
from aletheia.memory.service import create_run, finalize_plan, list_artifacts, list_metrics
from aletheia.scheduler.driver import ExperimentDriver


@pytest.mark.asyncio
async def test_full_dry_run_loop():
    create_all()
    run_id = create_run("dry-run e2e", domain="materials", status="scoping")
    # a ready dataset (benchmark) — satisfies the readiness gate
    register_dataset(run_id, "benchmark", ref="matbench_expt_gap", status="ready")
    exp_id = finalize_plan(
        run_id,
        {
            "objective": "predict experimental band gap from composition",
            "domain": "materials",
            "dataset": "matbench_expt_gap",
            "method": "Magpie features -> RandomForest",
            "metrics": "MAE, R2",
        },
    )

    driver = ExperimentDriver(run_id, dry_run=True)
    await driver.run()

    # metrics persisted for the experiment
    metric_names = {m["name"] for m in list_metrics(exp_id)}
    assert {"mae", "r2", "rmse"} <= metric_names

    # two critic gates fired (design + results) for this experiment
    with session_scope() as s:
        panels = (
            s.query(CritiquePanelRow).filter(CritiquePanelRow.target_ref == exp_id).all()
        )
        targets = {p.target for p in panels}
        assert {"design", "results"} <= targets

        run = s.get(Run, run_id)
        assert run.status == "completed"

    # a report artifact was written
    assert any(a["kind"] == "report" for a in list_artifacts(exp_id))

    # SURVEY ran before design: a survey transition + ≥1 ingested literature chunk
    from aletheia.memory.ledger import Decision, MemoryChunk

    with session_scope() as s:
        assert s.query(Decision).filter(
            Decision.run_id == run_id, Decision.stage_to == "survey"
        ).first() is not None
        lit = s.query(MemoryChunk).filter(
            MemoryChunk.run_id == run_id, MemoryChunk.kind == "literature"
        ).count()
        assert lit >= 1
