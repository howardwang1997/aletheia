"""Phase J: the reproduction pass — a metric claim earns `strong` only when an
independent re-run confirms the headline within tolerance; a mismatch downgrades it
and records a `reproducibility` claim. Offline, no spend."""

from __future__ import annotations

import asyncio

from aletheia.db import create_all
from aletheia.memory.service import create_run, finalize_plan, list_claims
from aletheia.scheduler.driver import ExperimentDriver


# --- the tolerance rule ----------------------------------------------------
def test_repro_match_tolerance():
    assert ExperimentDriver._repro_match(0.50, 0.50, 0.05) == (True, 0.0)
    ok, rel = ExperimentDriver._repro_match(0.50, 0.51, 0.05)
    assert ok is True and rel is not None and rel <= 0.05
    ok, _ = ExperimentDriver._repro_match(0.50, 0.70, 0.05)
    assert ok is False
    assert ExperimentDriver._repro_match(None, 0.5, 0.05) == (False, None)


# --- the metric-claim strength rule depends on reproduction ----------------
def test_metric_strength_requires_reproduction():
    cs = ExperimentDriver._claim_strength
    # grouped + clean approve + CONFIRMED reproduction -> strong
    assert cs("metric", protocol_status="grouped", gate_passed=True,
              gate_verdict="approve", reproduced=True) == "strong"
    # same, but reproduction not attempted -> capped at moderate
    assert cs("metric", protocol_status="grouped", gate_passed=True,
              gate_verdict="approve", reproduced=None) == "moderate"
    # reproduction CONTRADICTED -> weak
    assert cs("metric", protocol_status="grouped", gate_passed=True,
              gate_verdict="approve", reproduced=False) == "weak"


# --- a dry run reproduces (deterministic) + records a reproducibility claim --
def test_dry_run_reproduces_and_claims():
    create_all()
    run_id = create_run("reproduction e2e", domain="materials", status="planned")
    finalize_plan(run_id, {"objective": "predict band gap", "domain": "materials"})

    asyncio.run(ExperimentDriver(run_id, dry_run=True).run())

    claims = list_claims(run_id)
    repro = [c for c in claims if c["claim_type"] == "reproducibility"]
    assert repro, "a reproducibility claim should be recorded"
    # dry metrics are deterministic -> the re-run confirms the headline
    assert repro[0]["status"] == "supported"
    assert any(e["evidence_kind"] == "reproduction" for e in repro[0]["evidence"])
