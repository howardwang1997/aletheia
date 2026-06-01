"""Phase I: the hypothesis scorecard — score every hypothesis before spending
compute; a deterministic rule blocks low-novelty / unclear-evaluation ones, and the
campaign stops on low expected information gain. All offline, no spend."""

from __future__ import annotations

import asyncio

import pytest

from aletheia.db import create_all, session_scope
from aletheia.memory.ledger import Run
from aletheia.memory.service import create_run, finalize_plan, list_claims, list_scorecards
from aletheia.scheduler.driver import ExperimentDriver
from aletheia.scheduler.statemachine import LoopGuard


# --- the deterministic gate rule ------------------------------------------
def test_scorecard_decision_truth_table():
    d = ExperimentDriver("sc-rule", dry_run=True)
    good = {"novelty": 0.7, "evaluation_clarity": 0.8}
    proceed, _ = d._scorecard_decision(good)
    assert proceed is True

    proceed, reason = d._scorecard_decision({"novelty": 0.1, "evaluation_clarity": 0.8})
    assert proceed is False and "novelty" in reason

    proceed, reason = d._scorecard_decision({"novelty": 0.7, "evaluation_clarity": 0.1})
    assert proceed is False and "evaluation clarity" in reason


# --- a dry run scores, persists, and proceeds -----------------------------
def test_dry_run_persists_scorecard_and_upgrades_novelty():
    create_all()
    run_id = create_run("scorecard e2e", domain="materials", status="planned")
    finalize_plan(run_id, {"objective": "predict band gap", "domain": "materials"})

    asyncio.run(ExperimentDriver(run_id, dry_run=True).run())

    cards = list_scorecards(run_id)
    assert len(cards) >= 1
    assert cards[0]["decision"] == "proceed"
    assert 0.0 <= cards[0]["scores"]["novelty"] <= 1.0

    # the novelty claim was scored + grounded in structured findings -> upgraded to weak
    novelty = next(c for c in list_claims(run_id) if c["claim_type"] == "novelty")
    assert novelty["strength"] == "weak"


# --- a low-novelty hypothesis is blocked past the loop limit ---------------
def test_scorecard_gate_blocks_low_novelty(monkeypatch):
    create_all()
    run_id = create_run("scorecard block", domain="materials", status="scoping")
    exp_id = finalize_plan(run_id, {"objective": "predict band gap", "domain": "materials"})

    async def low_scores(self, plan):
        return {"novelty": 0.1, "evaluation_clarity": 0.8, "expected_information_gain": 0.5,
                "rationale": "already done by prior work"}

    monkeypatch.setattr(ExperimentDriver, "_score_hypothesis", low_scores)
    d = ExperimentDriver(run_id, dry_run=True)
    d.guard = LoopGuard(2)  # block fast
    d.hypothesis = {"statement": "a non-novel rehash"}

    ok = asyncio.run(d._scorecard_gate({"objective": "predict band gap"}, exp_id))
    assert ok is False
    with session_scope() as s:
        assert s.get(Run, run_id).status == "paused"
    # several scorecards recorded (re-ideation attempts), all 'block'
    cards = list_scorecards(run_id)
    assert cards and all(c["decision"] == "block" for c in cards)


# --- the campaign stops when expected information gain is below the floor ---
def test_campaign_stops_on_low_eig(monkeypatch):
    create_all()
    run_id = create_run("low eig campaign", domain="materials", status="planned")
    finalize_plan(run_id, {"objective": "predict band gap", "domain": "materials"})

    async def low_eig(self, plan):
        # clears the novelty/eval floors (proceeds) but EIG is below the campaign floor
        return {"novelty": 0.7, "evaluation_clarity": 0.8, "expected_information_gain": 0.05,
                "feasibility": 0.8, "sota_relevance": 0.7, "dataset_fit": 0.8,
                "cost_risk": 0.2, "failure_interpretability": 0.7, "rationale": "ok but low gain"}

    monkeypatch.setattr(ExperimentDriver, "_score_hypothesis", low_eig)
    # default campaign cap is 3; the low-EIG floor should stop it after round 1
    asyncio.run(ExperimentDriver(run_id, dry_run=True).run())

    # only ONE experiment ran despite a cap of 3 — the low-EIG floor stopped the campaign
    from aletheia.memory.ledger import Experiment

    with session_scope() as s:
        n = s.query(Experiment).filter(Experiment.run_id == run_id).count()
    assert n == 1


@pytest.mark.asyncio
async def test_scorecard_dims_clamped(monkeypatch):
    """Out-of-range / missing scores are clamped to [0,1] (never crash the gate)."""
    d = ExperimentDriver("sc-clamp", dry_run=True)
    scores = await d._score_hypothesis({"objective": "x"})
    for k in d._SCORE_DIMS:
        assert 0.0 <= scores[k] <= 1.0
