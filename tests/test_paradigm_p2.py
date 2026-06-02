"""Paradigm-mode P2 — the gate now JUDGES paradigm work on the right axes (not SOTA-delta),
and the discriminating-demonstration requirement is enforced deterministically (the
fakeability guardrail). Offline, no spend. See docs/PARADIGM_MODE_DESIGN.md."""

from __future__ import annotations

import asyncio

import aletheia.scheduler.driver as drv
from aletheia.critics.gateway import _instruction
from aletheia.db import create_all
from aletheia.memory.service import create_run, finalize_plan, list_claims
from aletheia.scheduler.driver import ExperimentDriver


# --- the results-gate instruction branches on mode ------------------------------
def test_results_instruction_performance_is_sota_oriented():
    txt = _instruction("results", "adversarial")  # default mode = performance
    assert "baseline comparisons" in txt
    assert "DISCRIMINATING DEMONSTRATION" not in txt


def test_results_instruction_paradigm_ignores_sota_and_demands_demonstration():
    txt = _instruction("results", "adversarial", mode="paradigm")
    assert "DISCRIMINATING DEMONSTRATION" in txt
    assert "do NOT reward OR penalize" in txt  # SOTA-delta is explicitly not the bar
    assert "well-posed" in txt


def test_non_results_target_unaffected_by_mode():
    # paradigm mode only changes the RESULTS standard; design/direction are unchanged
    assert _instruction("design", "adversarial", mode="paradigm") == _instruction("design", "adversarial")


# --- discriminating-demonstration requirement (the guardrail) -------------------
def _ideate(monkeypatch, hypo_json: str):
    create_all()
    run_id = create_run("paradigm p2", domain="materials", status="planned")
    exp_id = finalize_plan(run_id, {"objective": "x", "domain": "materials"})

    async def fake_reason(run_id, stage, prompt, **kw):
        return hypo_json

    monkeypatch.setattr(drv, "reason_stage", fake_reason)
    d = ExperimentDriver(run_id, dry_run=True)
    asyncio.run(d._ideate({"objective": "x"}, exp_id))
    return run_id, d


def test_paradigm_without_demonstration_falls_back_to_performance(monkeypatch):
    """Claiming 'paradigm' with no concrete demonstration must NOT be honored — it falls
    back to performance and records no formulation claim (no grandiosity license)."""
    run_id, d = _ideate(
        monkeypatch,
        '{"statement": "a grand new theory of everything", "rationale": "r", "prediction": "p", '
        '"novelty_note": "n", "contribution_type": "paradigm"}',  # no demonstration
    )
    assert d._contribution_type() == "performance"
    assert d._paradigm_demonstration() is None
    assert not any(c["claim_type"] == "formulation" for c in list_claims(run_id))


def test_paradigm_with_demonstration_is_honored(monkeypatch):
    run_id, d = _ideate(
        monkeypatch,
        '{"statement": "reframe as ranking", "rationale": "r", "prediction": "p", "novelty_note": "n", '
        '"contribution_type": "paradigm", "demonstration": {"form": "discriminating_instance", '
        '"claim": "RMSE ties two molecules a chemist ranks differently"}}',
    )
    assert d._contribution_type() == "paradigm"
    assert d._paradigm_demonstration()["form"] == "discriminating_instance"


# --- the demonstration + mode reach the results-review payload ------------------
def test_payload_carries_contribution_type_and_demonstration():
    demo = {"form": "enablement", "claim": "measures calibration the incumbent cannot"}
    payload = ExperimentDriver._results_review_payload(
        {"model": "x"}, {"metrics": {}, "info": {}}, analysis={}, claims=[],
        contribution_type="paradigm", demonstration=demo,
    )
    assert payload["contribution_type"] == "paradigm"
    assert payload["demonstration"] == demo
