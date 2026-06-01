"""Phase M: the RAG-aware coder + host-side LLM generator. The answerer is injected
(host-side, real LLM in prod; offline in tests); the FIXED harness still scores
deterministically against gold, and the coder authors the generation strategy."""

from __future__ import annotations

import asyncio

import aletheia.scheduler.driver as drv
from aletheia.db import create_all
from aletheia.domains.rag.plugin import RagEvalPlugin
from aletheia.memory.service import create_run, finalize_plan
from aletheia.scheduler.driver import ExperimentDriver


# --- the harness scores an INJECTED answerer (gold vs wrong) deterministically ----
def test_evaluate_scores_injected_answerer(tmp_path):
    plugin = RagEvalPlugin()
    data = plugin.load_data({})
    corpus, cases = data["corpus"], data["cases"]

    def gold_answerer(question, contexts):
        return next(c["gold_answer"] for c in cases if c["question"] == question)

    def wrong_answerer(question, contexts):
        return "completely unrelated nonsense tokens"

    good = plugin.evaluate(corpus, cases, 5, answerer=gold_answerer, workdir=tmp_path / "g")
    bad = plugin.evaluate(corpus, cases, 5, answerer=wrong_answerer, workdir=tmp_path / "b")

    assert good.metrics["answer_f1"] == 1.0  # perfect answers -> perfect F1 (harness, not the answerer)
    assert good.metrics["exact_match"] == 1.0
    assert bad.metrics["answer_f1"] == 0.0  # wrong answers -> 0 F1 (the answerer cannot grade itself)


# --- host-side generation calls the LLM worker; the harness still scores -----------
def test_hostside_eval_uses_llm_worker(monkeypatch):
    create_all()
    run_id = create_run("rag hostside", domain="rag", status="planned")
    finalize_plan(run_id, {"objective": "evaluate RAG", "domain": "rag"})

    calls = {"n": 0}

    async def fake_worker(run_id, label, prompt, **kw):
        calls["n"] += 1
        # a 'perfect' generator that returns the gold span embedded in the context
        if "Eiffel" in prompt:
            return "Paris France"
        return "unknown"

    monkeypatch.setattr(drv, "run_worker", fake_worker)
    d = ExperimentDriver(run_id, dry_run=False)  # real path, but the worker is faked
    from aletheia.domains.registry import get_domain_plugin

    d.profile = get_domain_plugin("rag").profile()
    d.plugin = get_domain_plugin("rag")
    d.budget = None  # skip budget charging in the unit test

    result = asyncio.run(d._rag_eval_hostside({"k": 5, "answer_prompt": d._DEFAULT_RAG_PROMPT}, {}, "rag", None))

    assert calls["n"] >= 1  # the host-side LLM was actually called per case
    assert "answer_f1" in result["metrics"]
    assert result["metrics"]["cost_usd"] > 0.0  # real generation has an estimated cost
    assert "host-side LLM" in result["info"]["model_impl"]


# --- dry-run host-side eval falls back to the offline answerer (no LLM, no spend) --
def test_hostside_dry_run_is_offline(monkeypatch):
    create_all()
    run_id = create_run("rag hostside dry", domain="rag", status="planned")
    finalize_plan(run_id, {"objective": "evaluate RAG", "domain": "rag"})

    async def boom(*a, **k):  # if the worker is called in dry-run, fail loudly
        raise AssertionError("dry-run must not call the LLM worker")

    monkeypatch.setattr(drv, "run_worker", boom)
    d = ExperimentDriver(run_id, dry_run=True)
    from aletheia.domains.registry import get_domain_plugin

    d.profile = get_domain_plugin("rag").profile()
    d.plugin = get_domain_plugin("rag")

    result = asyncio.run(d._rag_eval_hostside({"k": 5}, {}, "rag", None))
    assert result["metrics"]["cost_usd"] == 0.0  # offline extractive answerer
    assert 0.0 <= result["metrics"]["answer_f1"] <= 1.0


# --- the RAG-aware coder authors a generation strategy (prompt + k) ---------------
def test_code_rag_sets_strategy(monkeypatch):
    import json

    create_all()
    run_id = create_run("rag coder", domain="rag", status="planned")
    exp_id = finalize_plan(run_id, {"objective": "evaluate RAG", "domain": "rag"})

    async def fake_worker(run_id, label, prompt, **kw):
        return json.dumps({"answer_prompt": "Use {context} to answer {question} briefly.", "k": 4})

    monkeypatch.setattr(drv, "run_worker", fake_worker)
    d = ExperimentDriver(run_id, dry_run=False)
    from aletheia.domains.registry import get_domain_plugin

    d.profile = get_domain_plugin("rag").profile()
    design: dict = {"model": "rag", "k": 5}
    asyncio.run(d._code_rag(design, exp_id))
    assert design["answer_prompt"] == "Use {context} to answer {question} briefly."
    assert design["k"] == 4


def test_code_rag_rejects_bad_prompt_and_defaults(monkeypatch):
    import json

    create_all()
    run_id = create_run("rag coder bad", domain="rag", status="planned")
    exp_id = finalize_plan(run_id, {"objective": "evaluate RAG", "domain": "rag"})

    async def fake_worker(run_id, label, prompt, **kw):
        return json.dumps({"answer_prompt": "no placeholders here", "k": 99})  # invalid

    monkeypatch.setattr(drv, "run_worker", fake_worker)
    d = ExperimentDriver(run_id, dry_run=False)
    from aletheia.domains.registry import get_domain_plugin

    d.profile = get_domain_plugin("rag").profile()
    design: dict = {"model": "rag", "k": 5}
    asyncio.run(d._code_rag(design, exp_id))
    assert "answer_prompt" not in design  # invalid prompt rejected -> default used downstream
    assert design["k"] == 10  # out-of-range k (99) clamped into [1, 10]
