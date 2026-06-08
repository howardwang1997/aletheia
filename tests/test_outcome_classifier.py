"""K2 S2 — deterministic outcome-reason classifier (tests/test_outcome_classifier.py).

The classifier is a pure function over the harness's already-sealed result dicts; each test
pins one branch of the reason taxonomy + asserts the narrowing hint is actionable. It must
NEVER invent a verdict — only explain the one `holds` already recorded.
"""

from aletheia.scheduler.outcome import (
    REASON_AUDIT_REFUTED,
    REASON_CONFOUND,
    REASON_CONTROL_NOT_SILENT,
    REASON_DID_NOT_GENERALIZE,
    REASON_GENERALIZED,
    REASON_INFRA_DEGRADED,
    REASON_NO_DEMONSTRATION,
    REASON_SAMPLE_STARVED,
    REASON_SCOPE_OVERCLAIM,
    REASON_THRESHOLD_TOO_STRONG,
    classify_outcome,
)


def _demo(**kw):
    base = {
        "form": "systematic_signature",
        "holds": False,
        "statistic": 0.5,
        "detail": "",
        "test_statistic": 0.5,
        "control_statistic": 0.1,
        "test_triggers": True,
        "control_silent": True,
        "probes": {"clean": True, "note": ""},
        "preregistration": {"supported_if": {"op": ">=", "threshold": 0.45},
                            "control_silent_if": {"op": "<", "threshold": 0.4}},
        "exploration_applied": True,
        "n_confirm": 2000,
    }
    base.update(kw)
    return base


def test_no_demonstration_when_demo_is_none():
    out = classify_outcome(None, None, verdict="approve")
    assert out["reason"] == REASON_NO_DEMONSTRATION
    assert out["recoverable"] is True


def test_no_demonstration_reject_is_not_recoverable_silently():
    out = classify_outcome(None, None, verdict="reject", gate_passed=False)
    assert out["reason"] == REASON_NO_DEMONSTRATION
    assert out["recoverable"] is False
    assert out["narrowing_hint"]  # tells the next round to address the critique


def test_generalized_held_and_passed():
    demo = _demo(holds=True, test_statistic=0.76, statistic=0.76)
    repro = {"attempted": True, "demonstration_reproduced": True}
    out = classify_outcome(demo, repro, audit_passed=True, gate_passed=True, verdict="approve")
    assert out["reason"] == REASON_GENERALIZED
    assert out["recoverable"] is True


def test_scope_overclaim_held_but_gate_rejected():
    # the milestone case: held on confirm, reproduced, but peer review rejected the broad claim.
    demo = _demo(holds=True, test_statistic=0.76, statistic=0.76)
    repro = {"attempted": True, "demonstration_reproduced": True}
    out = classify_outcome(demo, repro, audit_passed=True, gate_passed=False, verdict="reject")
    assert out["reason"] == REASON_SCOPE_OVERCLAIM
    assert out["recoverable"] is True
    assert "narrow" in out["narrowing_hint"].lower()


def test_held_but_not_reproduced_is_did_not_generalize():
    demo = _demo(holds=True, test_statistic=0.76, statistic=0.76)
    repro = {"attempted": True, "demonstration_reproduced": False}
    out = classify_outcome(demo, repro, audit_passed=True, gate_passed=True, verdict="approve")
    assert out["reason"] == REASON_DID_NOT_GENERALIZE
    assert out["recoverable"] is False


def test_held_but_audit_degraded_is_infra():
    # held on confirm but the audit could not independently verify (vendor floor / infra error).
    demo = _demo(holds=True, test_statistic=0.76, statistic=0.76)
    repro = {"attempted": True, "demonstration_reproduced": True}
    out = classify_outcome(demo, repro, audit_passed=None, audit_error=True,
                           gate_passed=True, verdict="approve")
    assert out["reason"] == REASON_INFRA_DEGRADED
    assert "verif" in out["narrowing_hint"].lower()


def test_audit_refuted_takes_priority():
    demo = _demo(holds=False, audit_refuted=True, detail="deterministic seal #5 refutation: ...")
    out = classify_outcome(demo, None, audit_passed=False, gate_passed=False, verdict="reject")
    assert out["reason"] == REASON_AUDIT_REFUTED
    assert out["recoverable"] is True


def test_control_not_silent():
    demo = _demo(holds=False, control_silent=False, control_statistic=0.6,
                 detail="control fired")
    out = classify_outcome(demo, None, gate_passed=False, verdict="reject")
    assert out["reason"] == REASON_CONTROL_NOT_SILENT


def test_threshold_too_strong_near_miss():
    # effect present (0.40) but just shy of the >=0.45 bar -> threshold too strong.
    demo = _demo(holds=False, test_triggers=False, test_statistic=0.40, statistic=0.40,
                 detail="test did not trigger")
    out = classify_outcome(demo, None, gate_passed=False, verdict="reject")
    assert out["reason"] == REASON_THRESHOLD_TOO_STRONG
    assert out["recoverable"] is True


def test_did_not_generalize_far_miss():
    # effect essentially absent on confirm (0.05 vs the >=0.45 bar) -> did not generalize.
    demo = _demo(holds=False, test_triggers=False, test_statistic=0.05, statistic=0.05,
                 detail="test did not trigger")
    out = classify_outcome(demo, None, gate_passed=False, verdict="reject")
    assert out["reason"] == REASON_DID_NOT_GENERALIZE
    assert out["recoverable"] is False


def test_confound_probe_flagged():
    demo = _demo(holds=False, test_triggers=True, control_silent=True,
                 probes={"clean": False, "note": "leakage suspected"},
                 detail="probe flagged")
    out = classify_outcome(demo, None, gate_passed=False, verdict="reject")
    assert out["reason"] == REASON_CONFOUND


def test_sample_starved_from_detail():
    demo = _demo(holds=None, detail="explore/confirm split too small for a 4-way split")
    out = classify_outcome(demo, None)
    assert out["reason"] == REASON_SAMPLE_STARVED


def test_sample_starved_from_n_confirm_floor():
    demo = _demo(holds=False, test_triggers=False, test_statistic=0.05, n_confirm=10)
    out = classify_outcome(demo, None, min_confirm_n=40, gate_passed=False, verdict="reject")
    assert out["reason"] == REASON_SAMPLE_STARVED


def test_not_evaluated_sandbox_error_is_infra():
    demo = _demo(holds=None, statistic=None,
                 detail="AI-authored demonstration did not run (sandbox timeout/error)")
    out = classify_outcome(demo, None)
    assert out["reason"] == REASON_INFRA_DEGRADED
    assert out["recoverable"] is True


def test_code_gate_rejection_is_infra():
    demo = _demo(holds=False, statistic=None,
                 detail="AI-authored demonstration rejected by code gate: [...]")
    out = classify_outcome(demo, None, gate_passed=False, verdict="reject")
    assert out["reason"] == REASON_INFRA_DEGRADED


def test_lower_is_better_near_miss():
    # a lower-is-better rule (supported_if < 0.4): test 0.42 just over -> threshold too strong.
    demo = _demo(holds=False, test_triggers=False, test_statistic=0.42, statistic=0.42,
                 preregistration={"supported_if": {"op": "<", "threshold": 0.4},
                                  "control_silent_if": {"op": ">", "threshold": 0.6}},
                 detail="test did not trigger")
    out = classify_outcome(demo, None, gate_passed=False, verdict="reject")
    assert out["reason"] == REASON_THRESHOLD_TOO_STRONG
