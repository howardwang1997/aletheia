"""Post-real-run hardening: plan↔execution method drift → a `not_evaluated` (not
`refuted`) mechanism claim, and the optimize stage is skipped when the results gate
rejected. Offline, no spend."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aletheia.db import create_all
from aletheia.domains.registry import get_domain_plugin
from aletheia.memory.ledger import CLAIM_STATUSES
from aletheia.memory.service import create_claim, create_run, list_claims
from aletheia.scheduler.driver import (
    ExperimentDriver,
    _method_family,
    detect_method_drift,
)


# --- #1 method-family detection + drift (the real-run case: RF hypo, XGB exec) ---
def test_method_family_recognizes_families():
    assert _method_family("a RandomForest variance study") == "random forest"
    assert _method_family("XGBoost on Magpie") == "gradient boosting"
    assert _method_family("predict band gap from composition") is None


def test_detect_method_drift_flags_confident_mismatch():
    drift, msg = detect_method_drift(
        "RandomForest across-tree variance for selective prediction",
        "GradientBoostedTrees", "XGBRegressor",
    )
    assert drift is True and "random forest" in msg and "gradient boosting" in msg


def test_detect_method_drift_quiet_when_aligned_or_unnamed():
    # families agree -> no drift
    assert detect_method_drift("gradient boosting on Magpie", "GBT", "XGBRegressor")[0] is False
    # hypothesis names no family -> no drift (conservative)
    assert detect_method_drift("predict the gap", "GBT", "XGBRegressor")[0] is False


# --- #2 the new claim status exists ----------------------------------------
def test_not_evaluated_is_a_claim_status():
    assert "not_evaluated" in CLAIM_STATUSES
    assert "refuted" in CLAIM_STATUSES  # still distinct


# --- #5 optimize is a no-op when the gate rejected -------------------------
def test_optimize_skipped_on_rejected_gate():
    create_all()
    run_id = create_run("opt skip", domain="materials", status="planned")
    d = ExperimentDriver(run_id, dry_run=True)
    design = {"model": "gbt"}
    result = {"metrics": {"mae": 0.5}}
    # gate_passed=False -> returns the SAME objects without running an alternate eval
    out_design, out_result = asyncio.run(
        d._optimize(design, result, {}, "materials", None, plugin=None, gate_passed=False)
    )
    assert out_design is design and out_result is result


# --- domain-aware artifact contract: RAG (eval-only) vs regression (eval+model) ----
def test_rag_real_run_passes_guard_with_eval_only():
    """A real RAG run emits only an `eval` artifact; the domain-aware guard must NOT
    pause it (the old hard-coded {eval, model} requirement blocked the RAG real path)."""
    create_all()
    run_id = create_run("rag guard", domain="rag", status="planned")
    d = ExperimentDriver(run_id, dry_run=False)
    d.profile = get_domain_plugin("rag").profile()
    result = {"metrics": {"answer_f1": 0.3}, "artifacts": [{"kind": "eval"}], "info": {}}
    assert asyncio.run(d._post_execution_guards(result, "exp1")) is True


def test_regression_real_run_still_blocked_without_model():
    """Regression domains keep the default ("eval","model") contract: a real run that
    produced an eval but no fitted model is still paused (no silent pass)."""
    create_all()
    run_id = create_run("mat guard", domain="materials", status="planned")
    d = ExperimentDriver(run_id, dry_run=False)
    d.profile = get_domain_plugin("materials").profile()
    result = {"metrics": {"mae": 1.0}, "artifacts": [{"kind": "eval"}], "info": {}}
    assert asyncio.run(d._post_execution_guards(result, None)) is False


def test_rag_profile_declares_eval_only_contract():
    assert get_domain_plugin("rag").profile().required_artifacts == ("eval",)
    assert get_domain_plugin("materials").profile().required_artifacts == ("eval", "model")


# --- dense fallback fails closed for the CLAIM (mechanism -> not_evaluated) ---------
def test_method_not_instantiated_marks_mechanism_not_evaluated():
    """When the requested mechanism was never instantiated (e.g. dense retrieval fell
    back to lexical), the mechanism claim must be `not_evaluated`/`weak`, not refuted."""
    create_all()
    run_id = create_run("mech not eval", domain="rag", status="planned")
    d = ExperimentDriver(run_id, dry_run=False)
    d._claim_ids["mechanism"] = create_claim(
        run_id, claim_text="dense retrieval raises answer F1", claim_type="mechanism",
        strength="moderate", status="proposed", experiment_id=None,
    )
    # a CLEAN approve gate — but the mechanism was never tested, so it stays not_evaluated.
    rpanel = SimpleNamespace(consensus_verdict="approve", gate_passed=True)
    asyncio.run(d._finalize_claims(None, rpanel, {}, None, method_not_instantiated=True))
    mech = next(c for c in list_claims(run_id) if c["claim_type"] == "mechanism")
    assert mech["status"] == "not_evaluated" and mech["strength"] == "weak"


# --- method_drift is surfaced to the critic in the results-review payload -----------
def test_results_review_payload_includes_method_drift():
    design = {"model": "RandomForest", "solution_code": "..."}
    result = {
        "metrics": {"answer_f1": 0.3},
        "info": {"method_drift": True, "method_drift_msg": "executed XGB, not RF",
                 "model_impl": "XGBRegressor", "requested_method": "dense (embeddings)"},
    }
    payload = ExperimentDriver._results_review_payload(design, result, analysis={}, claims=[])
    assert payload["method_drift"] is True
    assert payload["method_drift_msg"] == "executed XGB, not RF"
    assert payload["requested_method"] == "dense (embeddings)"
    assert payload["executed_impl"] == "XGBRegressor"
