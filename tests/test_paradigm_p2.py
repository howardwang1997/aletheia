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


# --- robust paradigm framing: string demos + plan-declared intent ---------------
def test_paradigm_demonstration_accepts_string():
    d = ExperimentDriver("rid", dry_run=True)
    d.hypothesis = {"contribution_type": "paradigm",
                    "demonstration": "the incumbent metric is blind to the scaffold gap"}
    demo = d._paradigm_demonstration()
    assert demo and demo["form"] == "discriminating_instance"
    assert d._contribution_type() == "paradigm"


def test_ideate_inherits_paradigm_framing_from_plan(monkeypatch):
    """An operator can pin a paradigm study in the PLAN; ideation that omits the
    structured fields must not silently downgrade it to performance."""
    create_all()
    run_id = create_run("plan paradigm", domain="materials", status="planned")
    exp_id = finalize_plan(run_id, {"objective": "x", "domain": "materials"})

    async def fake_reason(run_id, stage, prompt, **kw):  # ideation omits the paradigm fields
        return '{"statement": "s", "rationale": "r", "prediction": "p", "novelty_note": "n"}'

    monkeypatch.setattr(drv, "reason_stage", fake_reason)
    d = ExperimentDriver(run_id, dry_run=True)
    plan = {"objective": "x", "contribution_type": "paradigm",
            "demonstration": "random-split RMSE is blind to scaffold generalization"}
    asyncio.run(d._ideate(plan, exp_id))
    assert d._contribution_type() == "paradigm"  # inherited from the plan
    assert any(c["claim_type"] == "formulation" for c in list_claims(run_id))


def test_ideation_debate_refines_and_preserves_framing(monkeypatch):
    """The ideation debate: a skeptic objects -> the proposer revises into a stronger
    hypothesis, and the paradigm framing survives the revision. Stops when 'solid'."""
    calls = []

    async def fake_reason(run_id, stage, prompt, **kw):
        calls.append(prompt)
        if "SKEPTICAL" in prompt:  # 1st skeptic objects, 2nd finds it solid -> stop
            n = sum(1 for p in calls if "SKEPTICAL" in p)
            return ('{"objections":["this is standard scaffold splitting"],"verdict":"revise"}'
                    if n == 1 else '{"objections":[],"verdict":"solid"}')
        # the revision drops the paradigm fields -> _carry_framing must restore them
        return '{"statement":"a stronger, more novel reframing","rationale":"r","prediction":"p","novelty_note":"n"}'

    monkeypatch.setattr(drv, "reason_stage", fake_reason)
    d = ExperimentDriver("rid", dry_run=False)
    d.survey_brief = "LITERATURE: scaffold splits are standard."
    d.hypothesis = {"statement": "weak idea", "contribution_type": "paradigm",
                    "demonstration": {"form": "impossibility", "claim": "incumbent is blind"}}
    asyncio.run(d._debate_hypothesis(None))
    assert d.hypothesis["statement"] == "a stronger, more novel reframing"  # revised
    assert d._contribution_type() == "paradigm"  # framing survived the revision
    assert sum(1 for p in calls if "SKEPTICAL" in p) == 2  # objected once, then solid


def test_ideation_debate_handles_degraded_skeptic(monkeypatch):
    """If the skeptic reasoning call degrades (orchestrator unavailable), the debate must
    NOT silently treat the idea as 'solid' — it breaks without touching the hypothesis."""
    async def fake_reason(run_id, stage, prompt, **kw):
        return "[worker-unavailable: orchestrator]"  # is_degraded -> True

    monkeypatch.setattr(drv, "reason_stage", fake_reason)
    d = ExperimentDriver("rid", dry_run=False)
    d.hypothesis = {"statement": "x", "contribution_type": "paradigm",
                    "demonstration": {"form": "f", "claim": "c"}}
    asyncio.run(d._debate_hypothesis(None))
    assert d.hypothesis["statement"] == "x"  # unchanged; no spurious revision


def test_ideation_debate_noops_in_dry_run(monkeypatch):
    d = ExperimentDriver("rid", dry_run=True)
    d.hypothesis = {"statement": "orig"}
    asyncio.run(d._debate_hypothesis(None))  # dry skeptic returns 'solid' -> no change
    assert d.hypothesis["statement"] == "orig"


def test_carry_framing_preserves_paradigm_unless_changed():
    """Shared guard used by BOTH re-ideation paths (direction gate + scorecard): a
    revision that omits contribution_type/demonstration keeps them; one that changes
    them is respected."""
    d = ExperimentDriver("rid", dry_run=True)
    d.hypothesis = {"contribution_type": "paradigm", "demonstration": {"form": "x", "claim": "c"}}
    kept = d._carry_framing({"statement": "new"})  # revision dropped the framing
    assert kept["contribution_type"] == "paradigm" and kept["demonstration"]["claim"] == "c"
    changed = d._carry_framing({"statement": "n", "contribution_type": "performance"})
    assert changed["contribution_type"] == "performance"  # deliberate change respected


# --- re-ideation must NOT drop the paradigm framing (real-run bug) --------------
def test_reideation_preserves_contribution_type_and_demonstration(monkeypatch):
    """A direction-gate rejection re-ideates; if the revision omits contribution_type /
    demonstration, a paradigm contribution must NOT silently revert to performance (which
    would skip its discriminating demonstration entirely)."""
    from aletheia.critics.schemas import Critique, CritiquePanel
    from aletheia.scheduler.statemachine import LoopGuard

    create_all()
    run_id = create_run("reideate paradigm", domain="materials", status="planned")
    d = ExperimentDriver(run_id, dry_run=True)
    d.guard = LoopGuard(5)
    d.survey_gaps, d.survey_brief = [], ""
    d.hypothesis = {"statement": "orig", "contribution_type": "paradigm",
                    "demonstration": {"form": "impossibility", "claim": "incumbent is blind"}}

    calls = {"n": 0}

    async def fake_review(target, content, ref, **kw):
        calls["n"] += 1
        passed = calls["n"] >= 2  # reject once, then approve
        return CritiquePanel(
            target=target, target_ref=ref,
            critiques=[Critique(critic_id="x", stance="adversarial",
                                verdict="approve" if passed else "reject",
                                confidence=1.0, summary="", findings=[])],
            consensus_verdict="approve" if passed else "reject", gate_passed=passed,
        )

    async def fake_reason(run_id, stage, prompt, **kw):  # revision DROPS the paradigm fields
        return '{"statement": "revised", "rationale": "r", "prediction": "p", "novelty_note": "n"}'

    monkeypatch.setattr(d.gateway, "review", fake_review)
    monkeypatch.setattr(drv, "reason_stage", fake_reason)
    assert asyncio.run(d._direction_gate({"objective": "x"}, None)) is True
    assert d._contribution_type() == "paradigm"  # framing survived the re-ideation
    assert d._paradigm_demonstration()["form"] == "impossibility"


# --- the demonstration + mode reach the results-review payload ------------------
def test_payload_carries_contribution_type_and_demonstration():
    demo = {"form": "enablement", "claim": "measures calibration the incumbent cannot"}
    payload = ExperimentDriver._results_review_payload(
        {"model": "x"}, {"metrics": {}, "info": {}}, analysis={}, claims=[],
        contribution_type="paradigm", demonstration=demo,
    )
    assert payload["contribution_type"] == "paradigm"
    assert payload["demonstration"] == demo
