"""Cross-encoder literature reranking: reorder by relevance, drop off-topic hits, and
fail-soft to the input order when the model can't load. Offline (CrossEncoder stubbed)."""

from __future__ import annotations

import sys
import types

import aletheia.research.rerank as rr
from aletheia.research.literature import Paper


def _papers() -> list[Paper]:
    return [
        Paper(title="off-topic forensic anthropology", abstract="about bones"),
        Paper(title="dense retrieval for QA", abstract="DPR semantic search"),
        Paper(title="unrelated superconductivity roadmap", abstract="materials"),
    ]


def _stub_crossencoder(monkeypatch, predict):
    class _CE:
        def __init__(self, name):
            pass

        def predict(self, pairs):
            return predict(pairs)

    fake = types.ModuleType("sentence_transformers")
    fake.CrossEncoder = _CE
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    monkeypatch.setattr(rr, "_cached_model", None)
    monkeypatch.setattr(rr, "_cached_name", None)


def test_rerank_reorders_and_drops_offtopic(monkeypatch):
    # relevant iff "retrieval" appears in the doc text; everything else scores very low
    _stub_crossencoder(monkeypatch, lambda pairs: [8.0 if "retrieval" in d.lower() else -8.0
                                                   for _q, d in pairs])
    out = rr.rerank_papers("retrieval", _papers(), top_k=8, min_relevance=0.05)
    assert [p.title for p in out] == ["dense retrieval for QA"]  # the 2 off-topic dropped


def test_rerank_graceful_degrade_keeps_order(monkeypatch):
    def _boom(name):
        raise RuntimeError("model unavailable")

    fake = types.ModuleType("sentence_transformers")
    fake.CrossEncoder = _boom
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    monkeypatch.setattr(rr, "_cached_model", None)
    monkeypatch.setattr(rr, "_cached_name", None)
    papers = _papers()
    out = rr.rerank_papers("retrieval", papers, top_k=2)
    assert [p.title for p in out] == [p.title for p in papers[:2]]  # unchanged, truncated


def test_rerank_keeps_top_when_threshold_drops_all(monkeypatch):
    _stub_crossencoder(monkeypatch, lambda pairs: [-8.0 for _ in pairs])  # all below cutoff
    out = rr.rerank_papers("x", _papers(), top_k=2, min_relevance=0.5)
    assert len(out) == 2  # never starves the survey: keeps the top reordered
