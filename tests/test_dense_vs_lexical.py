"""Phase O: demonstrate (offline + deterministically) that DENSE retrieval beats
LEXICAL on the paraphrase-gap QA set. Uses a fixture 'concept' embedder so the win
is reproducible in CI without downloading a real model; the real all-MiniLM-L6-v2
numbers (recall@3 0.60→1.00, F1 0.067→0.370) are produced by
scripts/demo_dense_vs_lexical.py."""

from __future__ import annotations

from aletheia.domains.rag.compare import compare_retrievers
from aletheia.domains.rag.dataset import load_qa

# Each text is mapped to a CONCEPT id; question + its gold share a concept, distractors
# get their own. Cosine over these one-hot vectors = a deterministic 'semantic' signal
# (a stand-in for a real sentence encoder) that bridges the paraphrase gap.
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
_DIM = 10


def _concept_embed(texts: list[str]) -> list[list[float]]:
    out = []
    for t in texts:
        vec = [0.0] * _DIM
        for markers, cid in _MARKERS:
            if any(m.lower() in t.lower() for m in markers):
                vec[cid] = 1.0
                break
        out.append(vec)
    return out


def test_dense_beats_lexical_on_paraphrase_gap():
    corpus, cases = load_qa({"ref": "paraphrase-qa"})
    result = compare_retrievers(corpus, cases, k=3, embed=_concept_embed)

    # the harness scores both; only the retriever varies
    assert result["dense"]["recall_at_k"] > result["lexical"]["recall_at_k"]
    assert result["dense"]["answer_f1"] >= result["lexical"]["answer_f1"]
    # the semantic retriever finds every paraphrased gold passage
    assert result["dense"]["recall_at_k"] == 1.0
    # lexical is pulled toward surface-word distractors -> misses some gold
    assert result["lexical"]["recall_at_k"] < 1.0
    assert result["delta"]["recall_at_k"] > 0
