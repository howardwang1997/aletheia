"""A deterministic, offline lexical retriever + extractive answerer + the gold-based
scorers. No network, no LLM, no randomness — so the RAG metrics are honest and the
domain is fully unit-testable. A real LLM/dense-retriever generator is a later lift;
the harness scores whatever the config produces."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

_TOKEN = re.compile(r"[a-z0-9]+")
_SENT = re.compile(r"(?<=[.!?])\s+")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


def _overlap(q_tokens: set[str], text: str) -> int:
    """How many distinct query tokens appear in ``text`` (lexical relevance)."""
    return len(q_tokens & set(tokenize(text)))


def retrieve(corpus: list[dict], question: str, k: int) -> list[dict]:
    """Rank corpus docs by query-token overlap (ties broken by original order, so it
    is deterministic) and return the top-k as {doc_id, text, score}."""
    q = set(tokenize(question))
    scored = [
        {"doc_id": d.get("doc_id"), "text": d.get("text", ""), "score": _overlap(q, d.get("text", ""))}
        for d in corpus
    ]
    # stable sort by score desc (Python sort is stable -> ties keep corpus order)
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[: max(1, k)]


def extractive_answer(question: str, top_doc_text: str) -> str:
    """Pick the sentence in the top retrieved doc with the most query-token overlap
    (a deterministic extractive 'generator' — no LLM)."""
    q = set(tokenize(question))
    sentences = [s.strip() for s in _SENT.split(top_doc_text or "") if s.strip()] or [top_doc_text or ""]
    return max(sentences, key=lambda s: _overlap(q, s))


def dense_retrieve(corpus: list[dict], question: str, k: int, *, embed: Any) -> list[dict]:
    """Semantic (embedding-based) retrieval: embed the question + every doc via the
    injected ``embed(list[str]) -> list[unit-vec]`` and rank by cosine similarity (a
    dot product on unit-norm vectors). Captures meaning beyond literal token overlap —
    the surveyed frontier retrieval method. ``embed`` is injected so the ranking is
    testable offline with a stub (the real backend is sentence-transformers)."""
    import numpy as np

    if not corpus:
        return []
    texts = [d.get("text", "") for d in corpus]
    vecs = embed([question] + texts)
    q = np.asarray(vecs[0], dtype=float)
    docs = np.asarray(vecs[1:], dtype=float)
    sims = docs @ q  # unit vectors -> cosine similarity
    order = np.argsort(-sims)[: max(1, k)]
    return [
        {"doc_id": corpus[i].get("doc_id"), "text": corpus[i].get("text", ""), "score": float(sims[i])}
        for i in order
    ]


def extractive_answerer(question: str, contexts: list[str]) -> str:
    """An ``answerer(question, contexts)`` adapter (the eval contract) over
    ``extractive_answer`` — answers from the top retrieved passage. Offline, no LLM;
    the default + dry-run/fallback answerer."""
    return extractive_answer(question, contexts[0] if contexts else "")


def token_f1(pred: str, gold: str) -> float:
    """Token-level F1 between a predicted and a gold answer (SQuAD-style)."""
    p, g = tokenize(pred), tokenize(gold)
    if not p or not g:
        return 0.0
    common = Counter(p) & Counter(g)
    n = sum(common.values())
    if n == 0:
        return 0.0
    precision, recall = n / len(p), n / len(g)
    return 2 * precision * recall / (precision + recall)


def normalized_exact_match(pred: str, gold: str) -> float:
    """1.0 if the gold answer's tokens are exactly the predicted tokens (order-free,
    normalized), else 0.0 — a strict deterministic check."""
    return 1.0 if tokenize(pred) == tokenize(gold) else 0.0


def gold_in_topk(retrieved: list[dict], gold_doc_ids: list[str]) -> bool:
    got = {r["doc_id"] for r in retrieved}
    return any(g in got for g in (gold_doc_ids or []))
