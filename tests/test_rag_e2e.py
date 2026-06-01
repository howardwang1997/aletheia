"""Phase L: a full dry-run loop on the RAG domain — proving the (regression-free)
loop generalizes. The headline is answer-F1 (maximized) and a cross-vendor
faithfulness metric is recorded alongside the deterministic metrics."""

from __future__ import annotations

import pytest

from aletheia.config import get_settings
from aletheia.data.registry import register_dataset
from aletheia.db import create_all, session_scope
from aletheia.memory.ledger import Run
from aletheia.memory.service import create_run, finalize_plan, list_claims
from aletheia.scheduler.driver import ExperimentDriver


@pytest.mark.asyncio
async def test_rag_dry_run_reaches_archive(monkeypatch):
    monkeypatch.setattr(get_settings(), "max_experiments_per_campaign", 1)
    create_all()
    run_id = create_run("rag dry e2e", domain="rag", status="scoping")
    register_dataset(run_id, "benchmark", ref="mini-qa", status="ready")
    finalize_plan(
        run_id,
        {
            "objective": "evaluate a retrieval-augmented QA configuration",
            "domain": "rag",
            "method": "lexical retrieval + extractive answer",
            "metrics": "answer F1, recall@k, faithfulness",
        },
    )

    await ExperimentDriver(run_id, dry_run=True).run()

    with session_scope() as s:
        assert s.get(Run, run_id).status == "completed"

    # the run finished on the RAG metric family (headline = answer_f1, maximized) and
    # recorded a cross-vendor faithfulness metric alongside the deterministic ones.
    from aletheia.events.store import list_events

    events = list_events(run_id, 1000)
    finished = [e for e in events if e["type"] == "run_finished"]
    assert finished
    metrics = (finished[-1]["payload"] or {}).get("metrics") or {}
    assert "answer_f1" in metrics
    assert metrics.get("faithfulness") == 0.8  # canned cross-vendor score in dry-run

    # the evidence ledger still works for a non-regression domain (metric + sota claims)
    claims = list_claims(run_id)
    assert any(c["claim_type"] == "metric" for c in claims)
    assert any(c["claim_type"] == "sota" for c in claims)
