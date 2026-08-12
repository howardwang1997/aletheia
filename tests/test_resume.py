"""Checkpoint / resume: idempotent ledger writes + the worker result cache.

These lock down the two halves of resume:
  1. Idempotent writes — replaying a stage must UPDATE the same rows, not duplicate them, so a
     resumed run's ledger (claims / experiments / metrics) stays clean.
  2. Worker cache — a resumed run (read mode) returns a stored result without calling Claude (0
     tokens); a fresh run never reads (no within-run collisions) but does write.
"""

from __future__ import annotations

import hashlib

import pytest

from aletheia.config import get_settings
from aletheia.data.registry import register_dataset
from aletheia.db import create_all, session_scope
from aletheia.memory.ledger import Claim, Experiment, Metric
from aletheia.memory.service import (
    create_claim,
    create_experiment,
    create_run,
    finalize_plan,
    get_cached_worker,
    list_claims,
    put_cached_worker,
    record_metrics,
)


# ---- idempotent writes -----------------------------------------------------------------------

def test_create_claim_idempotent_on_replay():
    create_all()
    run_id = create_run("claim idem", domain="materials", status="scoping")
    exp_id = finalize_plan(run_id, {"objective": "x", "domain": "materials"})

    id1 = create_claim(run_id, claim_text="effect holds", claim_type="novelty",
                       strength="weak", experiment_id=exp_id)
    # SAME content (a replay) -> same row, fields updated, NO duplicate
    id2 = create_claim(run_id, claim_text="effect holds", claim_type="novelty",
                       strength="moderate", status="supported", experiment_id=exp_id)
    assert id1 == id2
    # DIFFERENT content -> a distinct claim
    id3 = create_claim(run_id, claim_text="a different claim", claim_type="formulation",
                       strength="speculative", experiment_id=exp_id)
    assert id3 != id1

    with session_scope() as s:
        rows = s.query(Claim).filter(Claim.run_id == run_id).all()
        assert len(rows) == 2  # not 3 — the novelty claim was deduped
        novelty = next(r for r in rows if r.id == id1)
        assert novelty.strength == "moderate" and novelty.status == "supported"


def test_create_experiment_idempotent_on_replay():
    create_all()
    run_id = create_run("exp idem", domain="materials", status="scoping")
    root = finalize_plan(run_id, {"objective": "root", "domain": "materials"})
    plan = {"objective": "round 2", "hypothesis": "h2"}

    id1 = create_experiment(run_id, plan, parent_experiment_id=root)
    id2 = create_experiment(run_id, plan, parent_experiment_id=root)  # replay -> reuse
    assert id1 == id2
    id3 = create_experiment(run_id, {"objective": "round 3"}, parent_experiment_id=id1)
    assert id3 != id1

    with session_scope() as s:
        # root + round2 + round3 == 3 experiments (round2 created once despite two calls)
        n = s.query(Experiment).filter(Experiment.run_id == run_id).count()
        assert n == 3


def test_record_metrics_idempotent_on_replay():
    create_all()
    run_id = create_run("metric idem", domain="materials", status="scoping")
    exp_id = finalize_plan(run_id, {"objective": "x", "domain": "materials"})

    record_metrics(exp_id, {"r2": 0.5, "mae": 1.2}, split="test")
    record_metrics(exp_id, {"r2": 0.7, "mae": 1.2}, split="test")  # replay: update r2 in place

    with session_scope() as s:
        rows = s.query(Metric).filter(Metric.experiment_id == exp_id).all()
        assert len(rows) == 2  # not 4
        by_name = {r.name: r.value for r in rows}
        assert by_name["r2"] == 0.7  # updated, not duplicated


# ---- worker cache ----------------------------------------------------------------------------

def test_worker_cache_roundtrip():
    create_all()
    run_id = create_run("cache rt", domain="materials", status="scoping")
    assert get_cached_worker(run_id, "k1") is None
    put_cached_worker(run_id, "k1", "coder", "RESULT-A")
    assert get_cached_worker(run_id, "k1") == "RESULT-A"
    put_cached_worker(run_id, "k1", "coder", "RESULT-B")  # idempotent update
    assert get_cached_worker(run_id, "k1") == "RESULT-B"


@pytest.mark.asyncio
async def test_run_worker_returns_cached_on_resume(monkeypatch):
    from aletheia.orchestrator import worker

    create_all()
    run_id = create_run("worker cache", domain="materials", status="scoping")
    label, system, prompt = "coder", worker.STAGE_SYSTEM, "author the demonstration"
    settings = get_settings()
    key = hashlib.sha256(
        "\0".join([
            settings.orchestrator_provider,
            settings.orchestrator_transport,
            label,
            system,
            settings.orchestrator_model,
            prompt,
            "",
        ]).encode("utf-8")
    ).hexdigest()
    put_cached_worker(run_id, key, label, "CACHED-DEMO-CODE")

    # pretend we have creds (so run_worker doesn't take the dry-run early return) + enable resume read
    monkeypatch.setattr(worker, "has_credentials", lambda _s: True)
    monkeypatch.setattr(get_settings(), "resume_cache_read", True)

    # cache HIT must return the stored result WITHOUT importing/calling the Claude SDK
    out = await worker.run_worker(run_id, label, prompt, system=system, dry_run=False)
    assert out == "CACHED-DEMO-CODE"

    # with resume_cache_read False (a fresh run), the cache is NOT read — a different key stays a miss
    monkeypatch.setattr(get_settings(), "resume_cache_read", False)
    assert get_cached_worker(run_id, "never-written") is None


# ---- integration: replaying the driver does not duplicate the ledger -------------------------

@pytest.mark.asyncio
async def test_driver_replay_does_not_duplicate_ledger(monkeypatch):
    from aletheia.scheduler.driver import ExperimentDriver

    monkeypatch.setattr(get_settings(), "max_experiments_per_campaign", 1)
    create_all()
    run_id = create_run("replay idem", domain="materials", status="scoping")
    register_dataset(run_id, "benchmark", ref="matbench_expt_gap", status="ready")
    finalize_plan(run_id, {"objective": "bandgap", "domain": "materials"})

    await ExperimentDriver(run_id, dry_run=True).run()
    claims_1 = len(list_claims(run_id))
    with session_scope() as s:
        exps_1 = s.query(Experiment).filter(Experiment.run_id == run_id).count()

    # replay the SAME run_id (what resume does) — idempotent writes must not grow the ledger
    await ExperimentDriver(run_id, dry_run=True).run()
    claims_2 = len(list_claims(run_id))
    with session_scope() as s:
        exps_2 = s.query(Experiment).filter(Experiment.run_id == run_id).count()

    assert claims_1 > 0  # the dry run actually produced claims
    assert claims_2 == claims_1  # no duplicates on replay
    assert exps_2 == exps_1
