"""Phase G: the evidence ledger (claims ↔ evidence) — the anti-overclaiming layer.

Claims carry a harness-set strength + status; a dry-run loop leaves a metric claim
(referencing the headline key), a speculative novelty claim, and a capped sota claim.
All offline, no spend."""

from __future__ import annotations

import asyncio

from aletheia.db import create_all
from aletheia.memory.service import (
    attach_claim_evidence,
    create_claim,
    finalize_plan,
    list_claims,
    create_run,
    update_claim,
)
from aletheia.scheduler.driver import ExperimentDriver


# --- service helpers round-trip -------------------------------------------
def test_claim_helpers_round_trip():
    create_all()
    run_id = create_run("evidence test", domain="materials", status="planned")
    cid = create_claim(
        run_id,
        claim_text="model attains mae_lcso=0.4",
        claim_type="metric",
        strength="moderate",
        status="proposed",
        created_by="analysis",
        stage="analysis",
        evidence=[{"evidence_kind": "metric", "evidence_ref": "mae_lcso", "note": "value=0.4"}],
    )
    attach_claim_evidence(cid, "artifact", "/path/eval.json", note="eval")
    update_claim(cid, strength="strong", status="supported")

    claims = list_claims(run_id)
    assert len(claims) == 1
    c = claims[0]
    assert c["claim_type"] == "metric"
    assert c["strength"] == "strong" and c["status"] == "supported"
    assert {e["evidence_kind"] for e in c["evidence"]} == {"metric", "artifact"}


# --- a dry-run loop leaves claims -----------------------------------------
def test_dry_run_leaves_claims():
    create_all()
    run_id = create_run("evidence e2e", domain="materials", status="planned")
    finalize_plan(run_id, {"objective": "predict band gap", "domain": "materials"})

    asyncio.run(ExperimentDriver(run_id, dry_run=True).run())

    claims = list_claims(run_id)
    types = {c["claim_type"] for c in claims}
    assert {"metric", "novelty", "sota"} <= types

    metric = next(c for c in claims if c["claim_type"] == "metric")
    # the metric claim references the headline metric key as evidence
    assert any(e["evidence_kind"] == "metric" for e in metric["evidence"])

    novelty = next(c for c in claims if c["claim_type"] == "novelty")
    assert novelty["strength"] == "speculative"  # no structured novelty check in v1

    sota = next(c for c in claims if c["claim_type"] == "sota")
    assert sota["strength"] in ("weak", "moderate")  # capped — curated string only
