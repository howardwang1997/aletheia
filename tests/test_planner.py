"""Phase K: the experiment-search planner — the campaign go/no-go proposes TYPED
candidate next experiments (each with an open question + EIG) and deterministically
picks the highest-gain one that clears the floor, or stops. Offline, no spend."""

from __future__ import annotations

import asyncio
import json

from aletheia.db import create_all, session_scope
from aletheia.memory.ledger import Experiment
from aletheia.memory.service import create_run, finalize_plan
from aletheia.scheduler.driver import ExperimentDriver


def _setup():
    create_all()
    run_id = create_run("planner test", domain="materials", status="planned")
    exp_id = finalize_plan(run_id, {"objective": "predict band gap", "domain": "materials"})
    return run_id, exp_id


async def _run_step(d, candidates):
    async def fake_reason(run_id, stage, prompt, **kw):
        return json.dumps({"candidates": candidates})

    # patch the reasoner used inside _campaign_step
    import aletheia.scheduler.driver as mod

    orig = mod.reason_stage
    mod.reason_stage = fake_reason
    try:
        d._last_scores = {"expected_information_gain": 0.8}  # last round not the blocker
        return await d._campaign_step(
            {"objective": "x"},
            [{"round": 1, "exp_id": "e1", "hypothesis": "h", "headline": 0.5,
              "headline_metric": "mae_lcso", "model": "rf", "verdict": "approve",
              "experiment_type": "baseline"}],
            1, 3,
        )
    finally:
        mod.reason_stage = orig


# --- the planner picks the highest-EIG viable candidate --------------------
def test_planner_selects_highest_eig():
    run_id, _ = _setup()
    d = ExperimentDriver(run_id, dry_run=True)
    cands = [
        {"experiment_type": "ablation", "open_question": "which features?",
         "expected_information_gain": 0.8, "hypothesis": {"statement": "ablate features"}},
        {"experiment_type": "data_scaling", "open_question": "more data?",
         "expected_information_gain": 0.5, "hypothesis": {"statement": "scale data"}},
    ]
    decision = asyncio.run(_run_step(d, cands))
    assert decision["continue"] is True
    assert decision["next_hypothesis"]["experiment_type"] == "ablation"  # higher EIG won
    assert decision["next_hypothesis"]["open_question"] == "which features?"


# --- the planner stops when no candidate clears the EIG floor --------------
def test_planner_stops_when_no_candidate_clears_floor():
    run_id, _ = _setup()
    d = ExperimentDriver(run_id, dry_run=True)
    cands = [
        {"experiment_type": "ablation", "expected_information_gain": 0.1,
         "hypothesis": {"statement": "low-gain ablation"}},
        {"experiment_type": "robustness", "expected_information_gain": 0.05,
         "hypothesis": {"statement": "low-gain robustness"}},
    ]
    decision = asyncio.run(_run_step(d, cands))
    assert decision["continue"] is False
    assert "floor" in decision["rationale"]


# --- the dry-run planner proposes a typed candidate (offline, canned) ------
def test_planner_dry_path_proposes_typed_candidate():
    from aletheia.domains.registry import get_domain_plugin

    run_id, _ = _setup()
    d = ExperimentDriver(run_id, dry_run=True)
    d.profile = get_domain_plugin("materials").profile()  # _run sets this before the loop
    d._last_scores = {"expected_information_gain": 0.8}
    decision = asyncio.run(d._campaign_step(
        {"objective": "x"},
        [{"round": 1, "exp_id": "e1", "hypothesis": "h", "headline": 0.5,
          "headline_metric": "mae_lcso", "model": "rf", "verdict": "approve",
          "experiment_type": "baseline"}],
        1, 3,
    ))
    assert decision["continue"] is True
    assert decision["next_hypothesis"]["experiment_type"] in ExperimentDriver.EXPERIMENT_TYPES


# --- a dry-run campaign runs multiple typed rounds end-to-end --------------
def test_dry_campaign_runs_planned_rounds():
    run_id, _ = _setup()
    asyncio.run(ExperimentDriver(run_id, dry_run=True).run())
    with session_scope() as s:
        n = s.query(Experiment).filter(Experiment.run_id == run_id).count()
    assert n >= 2  # the planner kept proposing viable (EIG >= floor) experiments
