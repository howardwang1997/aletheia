"""Phase P: planner-driven retriever ablation. The lab itself runs the controlled
dense-vs-lexical comparison (answerer fixed) as a planner-proposed `ablation`
experiment and records a `comparison` claim. A fixture concept-embedder keeps it
deterministic + offline (no model download)."""

from __future__ import annotations

import asyncio
import types

import aletheia.memory.embedder as emb
from aletheia.config import get_settings
from aletheia.data.registry import register_dataset
from aletheia.db import create_all, session_scope
from aletheia.domains.registry import get_domain_plugin
from aletheia.memory.ledger import Run
from aletheia.memory.service import create_run, finalize_plan, list_claims
from aletheia.scheduler.driver import ExperimentDriver

# concept markers for the paraphrase-gap set: question + its gold share a concept,
# distractors get their own -> cosine retrieves gold (dense), lexical hits distractor.
_MARKERS = [
    (("cancel my subscription", "End Plan", "recurring membership"), 0),
    (("unlimited access", "premium features"), 5),
    (("forgot my login", "reset access", "no longer sign in"), 1),
    (("Strong passwords", "twelve characters"), 6),
    (("get a refund", "returned to your original card", "Money is returned"), 2),
    (("credit cards and PayPal",), 7),
    (("more secure", "two-step verification"), 3),
    (("suspicious security incident",), 8),
    (("stop getting so many emails", "notifications preferences", "how often we email"), 4),
    (("newsletter",), 9),
]


def _concept_embed(texts):
    out = []
    for t in texts:
        vec = [0.0] * 10
        for markers, cid in _MARKERS:
            if any(m.lower() in t.lower() for m in markers):
                vec[cid] = 1.0
                break
        out.append(vec)
    return out


def _patch_embedder(monkeypatch):
    monkeypatch.setattr(emb, "get_embedder", lambda **kw: types.SimpleNamespace(embed=_concept_embed))


# --- the ablation eval records both retrievers + a positive delta ---------
def test_rag_ablation_metrics(monkeypatch):
    _patch_embedder(monkeypatch)
    create_all()
    run_id = create_run("ablation unit", domain="rag", status="planned")
    exp_id = finalize_plan(run_id, {"objective": "rag", "domain": "rag"})
    d = ExperimentDriver(run_id, dry_run=True)
    d.profile = get_domain_plugin("rag").profile()
    d.plugin = get_domain_plugin("rag")

    result = asyncio.run(d._rag_ablation({"k": 3}, {"ref": "paraphrase-qa"}, "rag", exp_id))
    m = result["metrics"]
    assert m["recall_at_k_dense"] > m["recall_at_k_lexical"]
    assert m["recall_at_k_delta"] > 0
    assert m["answer_f1"] == m["answer_f1_dense"]  # headline = the candidate (dense)
    assert result["info"]["model_impl"] == "retriever ablation (lexical vs dense)"


# --- the comparison claim is recorded from the ablation deltas ------------
def test_comparison_claim_recorded():
    create_all()
    run_id = create_run("cmp claim", domain="rag", status="planned")
    exp_id = finalize_plan(run_id, {"objective": "rag", "domain": "rag"})
    d = ExperimentDriver(run_id, dry_run=True)
    d.profile = get_domain_plugin("rag").profile()
    rpanel = types.SimpleNamespace(consensus_verdict="approve", gate_passed=True)

    ablation = {"delta": {"answer_f1": 0.30, "recall_at_k": 0.40}}
    asyncio.run(d._finalize_claims("eval_set", rpanel, {}, exp_id, ablation=ablation))
    cmp = next(c for c in list_claims(run_id) if c["claim_type"] == "comparison")
    assert cmp["status"] == "supported" and "beats" in cmp["claim_text"]

    # a non-positive delta is honestly refuted
    run2 = create_run("cmp claim refuted", domain="rag", status="planned")
    exp2 = finalize_plan(run2, {"objective": "rag", "domain": "rag"})
    d2 = ExperimentDriver(run2, dry_run=True)
    d2.profile = get_domain_plugin("rag").profile()
    asyncio.run(d2._finalize_claims("eval_set", rpanel, {}, exp2,
                                    ablation={"delta": {"answer_f1": 0.0, "recall_at_k": 0.0}}))
    cmp2 = next(c for c in list_claims(run2) if c["claim_type"] == "comparison")
    assert cmp2["status"] == "refuted" and "does not beat" in cmp2["claim_text"]


# --- the planner drives the ablation end-to-end (round 2) -----------------
def test_dry_campaign_runs_planner_ablation(monkeypatch):
    _patch_embedder(monkeypatch)
    monkeypatch.setattr(get_settings(), "max_experiments_per_campaign", 2)
    create_all()
    run_id = create_run("rag ablation campaign", domain="rag", status="scoping")
    register_dataset(run_id, "benchmark", ref="paraphrase-qa", status="ready")
    finalize_plan(run_id, {"objective": "evaluate a RAG configuration", "domain": "rag"})

    asyncio.run(ExperimentDriver(run_id, dry_run=True).run())

    with session_scope() as s:
        assert s.get(Run, run_id).status == "completed"
    # round 2 was a planner-proposed ablation -> a supported comparison claim
    comparisons = [c for c in list_claims(run_id) if c["claim_type"] == "comparison"]
    assert comparisons and any(c["status"] == "supported" for c in comparisons)
