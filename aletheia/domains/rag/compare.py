"""Retriever ablation: run the SAME RAG eval with the lexical vs the dense retriever
(answerer + everything else held fixed) and report the deltas. This isolates the
retriever's effect on recall@k / answer-F1 — the basis for the "dense beats lexical"
demonstration."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from aletheia.domains.rag.plugin import RagEvalPlugin
from aletheia.domains.rag.retriever import dense_retrieve, extractive_answerer, retrieve


def compare_retrievers(
    corpus: list[dict], cases: list[dict], k: int = 3, *, embed: Any, workdir: Path | None = None
) -> dict[str, Any]:
    """Score the lexical and dense retrievers on the same QA set with the SAME
    (extractive) answerer, so only the retriever varies. ``embed(list[str]) ->
    list[unit-vec]`` powers the dense retriever. Returns {lexical, dense, delta}."""
    plugin = RagEvalPlugin()
    base = Path(workdir or tempfile.mkdtemp(prefix="rag_cmp_"))

    def _run(retriever, label):
        res = plugin.evaluate(
            corpus, cases, k,
            answerer=extractive_answerer, retriever=retriever, retriever_label=label,
            workdir=base / label.split()[0], answerer_label="extractive top-sentence",
        )
        return res.metrics

    lexical = _run(retrieve, "lexical token-overlap")
    dense = _run(lambda c, q, kk: dense_retrieve(c, q, kk, embed=embed), "dense (embeddings)")
    delta = {m: round(dense[m] - lexical[m], 4) for m in ("recall_at_k", "answer_f1", "exact_match")}
    return {"k": k, "n": len(cases), "lexical": lexical, "dense": dense, "delta": delta}
