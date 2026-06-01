"""Phase 2 increment 2: pgvector semantic recall — embedder determinism, the
index/recall round-trip + similarity ordering against live pgvector (HashEmbedder,
zero spend), filters, the memory_recall MCP tool, and recall-before-design
briefing injection into the driver's design prompt.
"""

from __future__ import annotations

import asyncio

import numpy as np

from aletheia.config import get_settings
from aletheia.db import create_all
from aletheia.memory.embedder import HashEmbedder, get_embedder
from aletheia.memory.vector import format_briefing, index_chunk, recall


# --- embedder -------------------------------------------------------------
def test_hash_embedder_deterministic_unit_norm():
    emb = HashEmbedder(384)
    a1 = emb.embed(["GBM on Magpie features"])[0]
    a2 = emb.embed(["GBM on Magpie features"])[0]
    b = emb.embed(["a totally different sentence"])[0]
    assert a1 == a2  # deterministic
    assert len(a1) == 384
    assert abs(np.linalg.norm(a1) - 1.0) < 1e-6  # unit norm
    assert a1 != b
    assert emb.embed([]) == []


def test_get_embedder_dry_run_is_hash():
    emb = get_embedder(dry_run=True)
    assert isinstance(emb, HashEmbedder)
    assert emb.dim == get_settings().embedding_dim


# --- index + recall round-trip / ordering ---------------------------------
def test_index_and_recall_orders_by_similarity():
    create_all()
    run = "recall-order"
    index_chunk("hypothesis", "GBM on Magpie features predicts band gap", run_id=run, dry_run=True)
    index_chunk("conclusion", "random forest underperforms on metallic systems", run_id=run, dry_run=True)
    index_chunk("note", "dataset has many zero-gap entries", run_id=run, dry_run=True)

    hits = recall("GBM on Magpie features predicts band gap", run_id=run, dry_run=True)
    assert hits  # found something
    # the exact-text chunk is the nearest neighbour (hash embedder => sim ~1.0)
    assert hits[0]["kind"] == "hypothesis"
    assert hits[0]["similarity"] > 0.99
    # ranked by descending similarity
    sims = [h["similarity"] for h in hits]
    assert sims == sorted(sims, reverse=True)


def test_recall_filters_by_kind_and_run_scope():
    create_all()
    index_chunk("hypothesis", "scope-A hypothesis text", run_id="scope-A", dry_run=True)
    index_chunk("conclusion", "scope-A conclusion text", run_id="scope-A", dry_run=True)
    index_chunk("hypothesis", "scope-B hypothesis text", run_id="scope-B", dry_run=True)

    # kind filter
    only_concl = recall("scope-A conclusion text", run_id="scope-A", kinds=["conclusion"], dry_run=True)
    assert only_concl and all(h["kind"] == "conclusion" for h in only_concl)

    # run scoping: scope-A only
    scoped = recall("hypothesis text", run_id="scope-A", dry_run=True)
    assert scoped and all(h["run_id"] == "scope-A" for h in scoped)

    # exclude_run_id drops the current run (cross-run recall)
    cross = recall("scope-A hypothesis text", exclude_run_id="scope-A", dry_run=True)
    assert all(h["run_id"] != "scope-A" for h in cross)


def test_recall_empty_query_returns_empty():
    assert recall("   ", dry_run=True) == []


def test_format_briefing():
    assert format_briefing([]) == ""
    out = format_briefing([{"kind": "design_rationale", "text": "RF on Magpie", "similarity": 0.91}])
    assert "PRIOR WORK" in out and "design rationale" in out and "RF on Magpie" in out


# --- memory_recall MCP tool -----------------------------------------------
def test_memory_recall_tool_returns_hits():
    from aletheia.orchestrator.tools import build_recall_tool

    create_all()
    # index in the SAME embedding space the tool recalls in (the tool uses the real
    # backend, dry_run=False) so the exact-text query matches its own chunk (~1.0)
    # regardless of what else is in the store.
    index_chunk("hypothesis", "tool-test unique recall phrase alpha", run_id="tool-run", dry_run=False)
    tool = build_recall_tool("tool-run")
    out = asyncio.run(tool.handler({"query": "tool-test unique recall phrase alpha"}))
    text = out["content"][0]["text"]
    assert "Relevant prior research" in text and "tool-test unique recall phrase" in text


def test_memory_recall_tool_handles_no_hits(monkeypatch):
    from aletheia.orchestrator.tools import build_recall_tool

    # the builder does `from aletheia.memory.vector import recall` at call time,
    # so patch the source before building the tool.
    import aletheia.memory.vector as vmod

    monkeypatch.setattr(vmod, "recall", lambda *a, **k: [])
    tool = build_recall_tool("empty-run")
    out = asyncio.run(tool.handler({"query": "nothing matches this"}))
    assert "No relevant prior research" in out["content"][0]["text"]


# --- recall-before-design briefing injection ------------------------------
def test_design_prompt_includes_prior_work(monkeypatch):
    import aletheia.scheduler.driver as drv

    captured = {}

    async def fake_reason(run_id, stage, prompt, **kw):
        captured["prompt"] = prompt
        return '{"model": "gradient_boosting", "model_params": {}}'

    async def fake_transition(*a, **k):
        return None

    monkeypatch.setattr(
        drv, "recall",
        lambda *a, **k: [
            {"kind": "conclusion", "text": "RF underperformed on metals last time", "similarity": 0.88}
        ],
    )
    monkeypatch.setattr(drv, "reason_stage", fake_reason)
    monkeypatch.setattr(drv, "record_transition", fake_transition)

    class _Plugin:
        def baselines(self):
            return [{"model": "random_forest", "model_params": {}}]

    driver = drv.ExperimentDriver("design-brief", dry_run=True)
    plan = {"objective": "predict band gap", "direction": "composition ML", "hypothesis": "GBM helps"}
    design = asyncio.run(driver._design(plan, {}, _Plugin(), None))

    assert design["model"] == "gradient_boosting"
    assert "PRIOR WORK" in captured["prompt"]
    assert "RF underperformed on metals" in captured["prompt"]
