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
from aletheia.config import get_settings
from aletheia.memory.service import create_run, finalize_plan, list_artifacts, list_metrics
from aletheia.scheduler.driver import ExperimentDriver


@pytest.mark.asyncio
async def test_full_dry_run_loop(monkeypatch):
    # pin to a single experiment so the single-loop assertions stay precise;
    # the multi-experiment campaign path is proven in test_campaign.py
    monkeypatch.setattr(get_settings(), "max_experiments_per_campaign", 1)
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

    # a report artifact was written — now a structured, cited paper (Phase C):
    # it carries a References section + an inline citation, and a references.bib
    # bibliography artifact sits alongside it.
    arts = list_artifacts(exp_id)
    assert any(a["kind"] == "report" for a in arts)
    assert any(a["kind"] == "bibliography" for a in arts)
    # the SURVEY recorded its result each run: a survey.md artifact + frontier methods
    assert any(a["kind"] == "survey" for a in arts)
    from aletheia.paths import run_artifacts_dir

    report = (run_artifacts_dir(run_id) / "report.md").read_text()
    assert "## References" in report and "[1]" in report

    # research front-end ran before design: survey → ideate transitions, a chosen
    # hypothesis persisted, a direction (novelty) gate, and ≥1 ingested literature chunk
    from aletheia.memory.ledger import Decision, Experiment, MemoryChunk

    with session_scope() as s:
        stages = {
            d.stage_to
            for d in s.query(Decision).filter(Decision.run_id == run_id).all()
        }
        assert {"survey", "ideate", "experiment_design"} <= stages
        methods = s.query(MemoryChunk).filter(
            MemoryChunk.run_id == run_id, MemoryChunk.kind == "method"
        ).count()
        assert methods >= 1  # frontier methods discovered + indexed by the SURVEY
        lit = s.query(MemoryChunk).filter(
            MemoryChunk.run_id == run_id, MemoryChunk.kind == "literature"
        ).count()
        assert lit >= 1
        assert s.get(Experiment, exp_id).hypothesis  # IDEATE set it
        assert s.query(CritiquePanelRow).filter(
            CritiquePanelRow.target_ref == exp_id, CritiquePanelRow.target == "direction"
        ).first() is not None
