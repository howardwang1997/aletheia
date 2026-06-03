"""Paradigm-mode P3 — a `formulation` claim is grounded by a REPRODUCIBLE discriminating
demonstration (never an LLM assertion, never SOTA-delta): no demonstration → speculative /
not_evaluated; held + approved → moderate; held + approved + reproduced → strong. Offline,
no spend. See docs/PARADIGM_MODE_DESIGN.md."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aletheia.db import create_all
from aletheia.memory.service import create_claim, create_run, list_claims
from aletheia.scheduler.driver import ExperimentDriver

CS = ExperimentDriver._claim_strength


# --- the deterministic grounding rule -------------------------------------------
def test_formulation_strength_without_demonstration_is_speculative():
    assert CS("formulation", demonstration_holds=None) == "speculative"
    assert CS("formulation", demonstration_holds=None, gate_passed=True, gate_verdict="approve") == "speculative"


def test_formulation_strength_weak_when_demo_fails_or_gate_fails():
    assert CS("formulation", demonstration_holds=False, gate_passed=True) == "weak"
    assert CS("formulation", demonstration_holds=True, gate_passed=False) == "weak"


def test_formulation_strength_moderate_when_held_and_approved():
    # held + approved but not reproduced -> moderate
    assert CS("formulation", demonstration_holds=True, gate_passed=True, gate_verdict="approve") == "moderate"
    # held + approve_with_changes (even if reproduced) -> moderate, not strong
    assert CS("formulation", demonstration_holds=True, gate_passed=True,
              gate_verdict="approve_with_changes", reproduced=True) == "moderate"


def test_formulation_strength_strong_only_when_held_approved_and_reproduced():
    assert CS("formulation", demonstration_holds=True, gate_passed=True,
              gate_verdict="approve", reproduced=True) == "strong"


# --- finalize wires demonstration + reproduction into the claim -----------------
def _seed_formulation(run_id):
    return create_claim(
        run_id, claim_text="new formulation proposed", claim_type="formulation",
        strength="speculative", status="proposed", experiment_id=None,
    )


def _finalize(run_id, *, verdict, passed, demonstration, demo_reproduced=None):
    d = ExperimentDriver(run_id, dry_run=True)
    d._claim_ids["formulation"] = _seed_formulation(run_id)
    rpanel = SimpleNamespace(consensus_verdict=verdict, gate_passed=passed)
    repro = {"attempted": True, "reproduced": True, "demonstration_reproduced": demo_reproduced} \
        if demo_reproduced is not None else {}
    asyncio.run(d._finalize_claims(None, rpanel, repro, None, demonstration=demonstration))
    return next(c for c in list_claims(run_id) if c["claim_type"] == "formulation")


def test_finalize_not_evaluated_without_demonstration():
    create_all()
    run_id = create_run("p3 no demo", domain="materials", status="planned")
    f = _finalize(run_id, verdict="approve", passed=True, demonstration=None)
    assert f["status"] == "not_evaluated" and f["strength"] == "speculative"


def test_finalize_supported_strong_when_demo_holds_and_reproduced():
    create_all()
    run_id = create_run("p3 strong", domain="materials", status="planned")
    f = _finalize(
        run_id, verdict="approve", passed=True,
        demonstration={"form": "discriminating_instance", "holds": True, "statistic": 0.3},
        demo_reproduced=True,
    )
    assert f["status"] == "supported" and f["strength"] == "strong"


def test_finalize_unverified_when_demo_holds_but_gate_rejects():
    # the demonstration HELD (objectively true), but peer review didn't endorse the
    # broader contribution -> unverified, NOT refuted (refuted == demonstration failed).
    create_all()
    run_id = create_run("p3 reject", domain="materials", status="planned")
    f = _finalize(
        run_id, verdict="reject", passed=False,
        demonstration={"form": "enablement", "holds": True, "statistic": 0.1},
    )
    assert f["status"] == "unverified" and f["strength"] == "weak"


def test_finalize_refuted_only_when_demo_did_not_hold():
    create_all()
    run_id = create_run("p3 refuted", domain="materials", status="planned")
    f = _finalize(
        run_id, verdict="reject", passed=False,
        demonstration={"form": "enablement", "holds": False, "statistic": 1.0},
    )
    assert f["status"] == "refuted" and f["strength"] == "weak"


# --- reproduction confirms the DEMONSTRATION (not a metric) ----------------------
def test_reproduce_marks_demonstration_reproduced(monkeypatch):
    create_all()
    run_id = create_run("p3 repro", domain="materials", status="planned")
    d = ExperimentDriver(run_id, dry_run=True)
    d.budget = None

    async def fake_eval(design, data_spec, domain, exp_id):
        return {"metrics": {"mae": 1.0},
                "info": {"demonstration": {"holds": True, "statistic": 0.30}}}

    monkeypatch.setattr(d, "_run_eval", fake_eval)
    original = {"metrics": {"mae": 1.0},
                "info": {"demonstration": {"holds": True, "statistic": 0.30}}}
    payload = asyncio.run(d._reproduce({"model": "x"}, {}, "materials", original, None))
    assert payload["demonstration_reproduced"] is True
