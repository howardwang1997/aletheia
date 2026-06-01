"""Phase L: the RAG-evaluation domain — the first non-regression domain. The
deterministic lexical retriever + extractive answerer + gold scorers run fully
offline; run_experiment returns a quality/cost/latency metric family."""

from __future__ import annotations

from aletheia.domains.rag.dataset import load_qa
from aletheia.domains.rag.plugin import RagEvalPlugin
from aletheia.domains.rag.retriever import gold_in_topk, retrieve, token_f1


# --- retriever + scorers (pure, offline) ----------------------------------
def test_retriever_ranks_gold_passage_into_topk():
    corpus, cases = load_qa()
    case = next(c for c in cases if c["gold_doc_ids"] == ["d1"])
    top = retrieve(corpus, case["question"], k=3)
    assert gold_in_topk(top, case["gold_doc_ids"])  # the Eiffel-Tower passage is retrieved


def test_token_f1_is_deterministic_and_bounded():
    assert token_f1("Paris France", "Paris France") == 1.0
    assert token_f1("", "Paris") == 0.0
    f1 = token_f1("The Eiffel Tower is located in Paris France", "Paris France")
    assert 0.0 < f1 < 1.0


# --- run_experiment returns the RAG metric family -------------------------
def test_run_experiment_returns_quality_metrics(tmp_path):
    plugin = RagEvalPlugin()
    result = plugin.run_experiment({"model": "lexical_k5", "k": 5}, {}, tmp_path)

    m = result.metrics
    assert {"answer_f1", "recall_at_k", "exact_match", "latency_ms", "cost_usd"} <= set(m)
    assert 0.0 <= m["answer_f1"] <= 1.0
    assert m["recall_at_k"] == 1.0  # every gold passage is lexically retrievable on the mini set
    assert m["cost_usd"] == 0.0  # no paid API in the lexical/extractive baseline

    assert (tmp_path / "eval.json").exists()
    kinds = {a["kind"] for a in result.artifacts}
    assert "eval" in kinds
    assert result.info["model_impl"] == "LexicalExtractiveRAG"
    assert result.info["protocol_status"] == "eval_set"
    # the per-case faithfulness inputs are returned for the driver's cross-vendor pass
    cases = result.info["faithfulness_cases"]
    assert cases and all({"question", "context", "answer"} <= set(c) for c in cases)


def test_profile_maximizes_f1_and_uses_critic_quality():
    prof = RagEvalPlugin().profile()
    assert prof.headline_metric == "answer_f1"
    assert prof.headline_goal == "max"
    assert prof.quality_via_critics is True


def test_registry_dispatches_rag():
    from aletheia.domains.registry import get_domain_plugin

    assert isinstance(get_domain_plugin("rag"), RagEvalPlugin)
