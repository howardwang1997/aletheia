"""Critic independence (Codex review finding #1): a gate-derived claim cannot exceed
`weak` on single-vendor (degraded) review — the cross-vendor adversarial panel is the
load-bearing guarantee for moderate/strong, so a gate that survived on one vendor (e.g.
same-vendor self-review) must not yield a strong claim. Offline, no spend."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aletheia.db import create_all
from aletheia.memory.service import create_claim, create_run, list_claims
from aletheia.scheduler.driver import ExperimentDriver

CS = ExperimentDriver._claim_strength


def test_claim_strength_caps_to_weak_without_cross_vendor():
    # cross-vendor: full strength
    assert CS("metric", gate_passed=True, gate_verdict="approve", reproduced=True, cross_vendor=True) == "strong"
    assert CS("formulation", gate_passed=True, gate_verdict="approve", reproduced=True,
              demonstration_holds=True, cross_vendor=True) == "strong"
    # single-vendor: capped at weak
    assert CS("metric", gate_passed=True, gate_verdict="approve", reproduced=True, cross_vendor=False) == "weak"
    assert CS("formulation", gate_passed=True, gate_verdict="approve", reproduced=True,
              demonstration_holds=True, cross_vendor=False) == "weak"
    assert CS("sota", has_sota=True, cross_vendor=False) == "weak"  # moderate -> weak
    # already <= weak: unaffected
    assert CS("novelty", cross_vendor=False) == "speculative"


def _panel(verdict, gate_passed, vendor_ids):
    return SimpleNamespace(consensus_verdict=verdict, gate_passed=gate_passed,
                           critiques=[SimpleNamespace(critic_id=v) for v in vendor_ids])


def _finalize_metric(run_id, vendor_ids):
    d = ExperimentDriver(run_id, dry_run=True)
    d._claim_ids["metric"] = create_claim(
        run_id, claim_text="m", claim_type="metric", strength="speculative", status="proposed")
    rpanel = _panel("approve", True, vendor_ids)
    asyncio.run(d._finalize_claims("grouped", rpanel, {"attempted": True, "reproduced": True}, None))
    return next(c for c in list_claims(run_id) if c["claim_type"] == "metric")


def test_finalize_caps_metric_on_single_vendor():
    create_all()
    run_id = create_run("xv1", domain="materials", status="planned")
    m = _finalize_metric(run_id, ["anthropic"])  # 1 distinct vendor (self-review)
    assert m["strength"] == "weak"  # would be strong cross-vendor; capped single-vendor


def test_finalize_metric_strong_on_cross_vendor():
    create_all()
    run_id = create_run("xv2", domain="materials", status="planned")
    m = _finalize_metric(run_id, ["anthropic", "grok"])  # 2 distinct vendors
    assert m["strength"] == "strong"
