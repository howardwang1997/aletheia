"""Demonstrate that DENSE (semantic) retrieval beats LEXICAL (token-overlap) on a
paraphrase-gap QA set, using the REAL sentence-transformers embedder + the lab's own
RAG harness. The answerer is held fixed (extractive), so the only variable is the
retriever — the scoring is the fixed deterministic harness (recall@k / answer-F1).

    conda run -n aletheia python scripts/demo_dense_vs_lexical.py

Needs the `local` embedding backend (sentence-transformers; downloads all-MiniLM-L6-v2
on first use). With the `hash` stub it is meaningless (random) — use the real backend.
"""

from __future__ import annotations

from aletheia.domains.rag.compare import compare_retrievers
from aletheia.domains.rag.dataset import load_qa
from aletheia.memory.embedder import get_embedder


def main() -> None:
    embedder = get_embedder(dry_run=False)
    backend = type(embedder).__name__
    if "Hash" in backend:
        print(f"WARNING: embedder is {backend} (random stub). Set ALETHEIA_EMBEDDING_BACKEND=local "
              "and install sentence-transformers for a real demonstration.\n")

    corpus, cases = load_qa({"ref": "paraphrase-qa"})
    result = compare_retrievers(corpus, cases, k=3, embed=embedder.embed)

    print(f"Paraphrase-gap QA: {result['n']} cases, k={result['k']}, embedder={backend}\n")
    hdr = f"{'metric':<14}{'lexical':>10}{'dense':>10}{'delta':>10}"
    print(hdr)
    print("-" * len(hdr))
    for m in ("recall_at_k", "answer_f1", "exact_match"):
        print(f"{m:<14}{result['lexical'][m]:>10.3f}{result['dense'][m]:>10.3f}{result['delta'][m]:>+10.3f}")

    win = result["delta"]["recall_at_k"] > 0 or result["delta"]["answer_f1"] > 0
    print(f"\n=> dense {'BEATS' if win else 'does NOT beat'} lexical on this paraphrase-gap set.")


if __name__ == "__main__":
    main()
