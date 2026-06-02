"""Post-real-run hardening: plan↔execution method drift → a `not_evaluated` (not
`refuted`) mechanism claim, and the optimize stage is skipped when the results gate
rejected. Offline, no spend."""

from __future__ import annotations

import asyncio

from aletheia.db import create_all
from aletheia.memory.ledger import CLAIM_STATUSES
from aletheia.memory.service import create_run
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
