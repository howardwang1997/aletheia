"""Phase N: the dense (semantic) retriever. The ranking math is tested offline with
an injected STUB embedder (controlled vectors); the driver selects dense only on real
runs and falls back to the lexical baseline in dry-run (the hash stub is random)."""

from __future__ import annotations

import asyncio

import aletheia.scheduler.driver as drv
from aletheia.db import create_all
from aletheia.domains.rag.retriever import dense_retrieve
from aletheia.domains.registry import get_domain_plugin
from aletheia.memory.service import create_run
from aletheia.scheduler.driver import ExperimentDriver

_CORPUS = [
    {"doc_id": "d1", "text": "alpha"},
    {"doc_id": "d2", "text": "beta"},
    {"doc_id": "d3", "text": "gamma"},
]


def _stub_embed(vectors: dict[str, list[float]]):
    """An embed(list[str]) -> list[unit-vec] stub: looks up controlled vectors by text."""
    def embed(texts):
        return [vectors[t] for t in texts]
    return embed


# --- the cosine ranking is correct + respects k ---------------------------
def test_dense_retrieve_ranks_by_cosine():
    # query aligns with d2's vector; d1/d3 are orthogonal
    embed = _stub_embed({
        "q": [0.0, 1.0],
        "alpha": [1.0, 0.0],
        "beta": [0.0, 1.0],
        "gamma": [1.0, 0.0],
    })
    top = dense_retrieve(_CORPUS, "q", k=1, embed=embed)
    assert [r["doc_id"] for r in top] == ["d2"]  # most similar by cosine

    top2 = dense_retrieve(_CORPUS, "q", k=2, embed=embed)
    assert len(top2) == 2 and top2[0]["doc_id"] == "d2"


def test_dense_retrieve_empty_corpus():
    assert dense_retrieve([], "q", k=3, embed=_stub_embed({})) == []


# --- the driver selects dense only on real runs; dry-run falls back to lexical ----
def test_make_retriever_dry_run_falls_back_to_lexical():
    create_all()
    run_id = create_run("dense select dry", domain="rag", status="planned")
    d = ExperimentDriver(run_id, dry_run=True)
    d.profile = get_domain_plugin("rag").profile()
    _fn, label = d._make_retriever({"retriever": "dense"})
    assert label.startswith("lexical")  # dry-run cannot use the (random) hash embedder


def test_make_retriever_real_run_uses_dense(monkeypatch):
    create_all()
    run_id = create_run("dense select real", domain="rag", status="planned")
    # stub the embedder so no model is downloaded
    import aletheia.memory.embedder as emb

    class _E:
        def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(emb, "get_embedder", lambda **kw: _E())
    d = ExperimentDriver(run_id, dry_run=False)
    d.profile = get_domain_plugin("rag").profile()
    fn, label = d._make_retriever({"retriever": "dense"})
    assert label.startswith("dense")
    assert fn(_CORPUS, "q", 1)  # the dense closure runs without a real model


# --- the RAG coder may select the retriever -------------------------------
def test_code_rag_selects_retriever(monkeypatch):
    import json

    from aletheia.memory.service import finalize_plan

    create_all()
    run_id = create_run("rag coder retriever", domain="rag", status="planned")
    exp_id = finalize_plan(run_id, {"objective": "evaluate RAG", "domain": "rag"})

    async def fake_worker(run_id, label, prompt, **kw):
        return json.dumps({"retriever": "dense", "k": 6,
                           "answer_prompt": "From {context} answer {question}."})

    monkeypatch.setattr(drv, "run_worker", fake_worker)
    d = ExperimentDriver(run_id, dry_run=False)
    d.profile = get_domain_plugin("rag").profile()
    design: dict = {"model": "rag"}
    asyncio.run(d._code_rag(design, exp_id))
    assert design["retriever"] == "dense"
    assert design["k"] == 6
