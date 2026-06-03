"""Cross-encoder reranking for retrieved literature.

A multi-source candidate pool (Semantic Scholar + arXiv + OpenAlex) is reordered by
GENUINE query relevance and off-topic hits are dropped — the fix for keyword retrieval
returning unrelated papers. A cross-encoder (jointly scores the (query, paper) pair) is
the right tool here, far better than bi-encoder cosine; ``ms-marco-MiniLM-L-6-v2`` is the
fast CPU default (swap to ``BAAI/bge-reranker-v2-m3`` etc. via settings for more quality).

Best-effort: if the reranker model can't load (offline / not installed), the input order
is returned unchanged — reranking is an enrichment, never a dependency that breaks the
survey."""

from __future__ import annotations

import math
import sys
from typing import Any

from aletheia.config import get_settings

_cached_model: Any = None
_cached_name: str | None = None


def _sigmoid(x: float) -> float:
    if x < -60:
        return 0.0
    if x > 60:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


def _load(name: str) -> Any:
    """Lazy-load + cache the cross-encoder (so the model is loaded at most once)."""
    global _cached_model, _cached_name
    if _cached_model is not None and _cached_name == name:
        return _cached_model
    from sentence_transformers import CrossEncoder

    model = CrossEncoder(name)
    _cached_model, _cached_name = model, name
    return model


def rerank_papers(query: str, papers: list[Any], *, top_k: int = 8,
                  min_relevance: float | None = None) -> list[Any]:
    """Reorder ``papers`` by cross-encoder relevance to ``query`` and drop off-topic ones
    (sigmoid(score) < ``min_relevance``), returning the top ``top_k``. Falls back to the
    input order (truncated) if reranking is disabled or the model can't load."""
    s = get_settings()
    papers = list(papers)
    cut = top_k if top_k else len(papers)
    if not papers or not (query or "").strip() or not getattr(s, "reranker_enabled", True):
        return papers[:cut]
    mr = s.reranker_min_relevance if min_relevance is None else min_relevance
    try:
        model = _load(s.reranker_model)
        pairs = [(query, f"{p.title}. {(p.abstract or '')[:1000]}") for p in papers]
        scores = [float(x) for x in model.predict(pairs)]
    except Exception as exc:  # noqa: BLE001 - reranking is best-effort
        print(f"[rerank] unavailable ({type(exc).__name__}: {exc}); keeping merge order",
              file=sys.stderr)
        return papers[:cut]
    ranked = sorted(zip(papers, scores), key=lambda t: t[1], reverse=True)
    kept = [p for p, sc in ranked if _sigmoid(sc) >= mr]
    if not kept:  # the threshold dropped everything — don't starve the survey; keep top
        kept = [p for p, _ in ranked]
    return kept[:cut]
