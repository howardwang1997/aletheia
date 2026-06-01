"""Phase D: multi-experiment research campaigns. One Run drives several linked
Experiments — each round poses a hypothesis, runs the full experiment, then a
go/no-go step decides (and re-ideates) the next. All dry-run (no network, no
spend)."""

from __future__ import annotations

import pytest

from aletheia.config import get_settings
from aletheia.data.registry import register_dataset
from aletheia.db import create_all, session_scope
from aletheia.memory.ledger import Experiment, Run
from aletheia.memory.service import create_run, finalize_plan, list_artifacts, list_metrics
from aletheia.scheduler.driver import ExperimentDriver


def _launch(run_id):
    register_dataset(run_id, "benchmark", ref="matbench_expt_gap", status="ready")
    return finalize_plan(
        run_id,
        {
            "objective": "predict experimental band gap from composition",
            "domain": "materials",
            "dataset": "matbench_expt_gap",
            "method": "Magpie features -> RandomForest",
        },
    )


@pytest.mark.asyncio
async def test_campaign_runs_linked_experiments(monkeypatch):
    monkeypatch.setattr(get_settings(), "max_experiments_per_campaign", 2)
    create_all()
    run_id = create_run("campaign e2e", domain="materials", status="scoping")
    first_exp = _launch(run_id)

    driver = ExperimentDriver(run_id, dry_run=True)
    await driver.run()

    with session_scope() as s:
        exps = (
            s.query(Experiment)
            .filter(Experiment.run_id == run_id)
            .order_by(Experiment.created_at.asc())
            .all()
        )
        # two linked experiments: the second's parent is the first
        assert len(exps) == 2
        child = [e for e in exps if e.parent_experiment_id is not None]
        assert len(child) == 1 and child[0].parent_experiment_id == first_exp
        # each experiment posed its own hypothesis
        assert all(e.hypothesis for e in exps)

        assert s.get(Run, run_id).status == "completed"

    # each experiment wrote a report; the campaign produced a synthesis artifact
    all_arts = [a for e in (first_exp, child[0].id) for a in list_artifacts(e)]
    assert sum(1 for a in all_arts if a["kind"] == "report") >= 1
    assert any(a["kind"] == "campaign" for a in list_artifacts(first_exp))


@pytest.mark.asyncio
async def test_molecules_domain_runs_end_to_end(monkeypatch):
    """The whole loop is domain-agnostic: a dry-run on domain='molecules' reaches
    ARCHIVE with a cited paper whose headline metric + wording come from the molecules
    profile (RMSE / scaffold-split, no materials 'LCSO'/'eV')."""
    monkeypatch.setattr(get_settings(), "max_experiments_per_campaign", 1)
    create_all()
    run_id = create_run("molecules e2e", domain="molecules", status="scoping")
    register_dataset(run_id, "benchmark", ref="esol", status="ready")
    exp_id = finalize_plan(
        run_id,
        {"objective": "predict aqueous solubility from structure", "domain": "molecules", "dataset": "esol"},
    )

    driver = ExperimentDriver(run_id, dry_run=True)
    await driver.run()

    with session_scope() as s:
        assert s.get(Run, run_id).status == "completed"

    from aletheia.paths import run_artifacts_dir

    report = (run_artifacts_dir(run_id) / "report.md").read_text()
    assert "scaffold" in report.lower()  # molecules-profile protocol wording
    assert "LCSO" not in report and " eV" not in report  # no materials leakage
    # the molecules dry metrics (RMSE headline) flowed through to the ledger
    names = {m["name"] for m in list_metrics(exp_id)}
    assert "rmse_scaffold" in names


@pytest.mark.asyncio
async def test_single_experiment_has_no_campaign_artifact(monkeypatch):
    """cap=1 preserves today's behavior: one experiment, no campaign synthesis."""
    monkeypatch.setattr(get_settings(), "max_experiments_per_campaign", 1)
    create_all()
    run_id = create_run("single e2e", domain="materials", status="scoping")
    exp_id = _launch(run_id)

    driver = ExperimentDriver(run_id, dry_run=True)
    await driver.run()

    with session_scope() as s:
        n = s.query(Experiment).filter(Experiment.run_id == run_id).count()
        assert n == 1
        assert s.get(Run, run_id).status == "completed"
    assert not any(a["kind"] == "campaign" for a in list_artifacts(exp_id))
