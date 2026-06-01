"""The QA evaluation set for the RAG domain: a small corpus + questions with gold
answers + gold passage ids. A tiny in-memory set ships for dry-run + tests (offline);
a real run can load a larger JSON set via the data spec (``ref`` = a file path)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# A tiny, self-contained QA set. Gold answers are spans present in the gold passage,
# so the deterministic extractive answerer yields non-trivial (but imperfect) F1.
_MINI_CORPUS: list[dict[str, str]] = [
    {"doc_id": "d1", "text": "The Eiffel Tower is a wrought-iron lattice tower located in Paris, France."},
    {"doc_id": "d2", "text": "Mount Everest is the tallest mountain on Earth, measured from sea level."},
    {"doc_id": "d3", "text": "Water boils at 100 degrees Celsius at standard sea-level pressure."},
    {"doc_id": "d4", "text": "The speed of light in vacuum is about 299792 kilometers per second."},
    {"doc_id": "d5", "text": "Photosynthesis converts carbon dioxide and water into glucose and oxygen."},
]

_MINI_CASES: list[dict[str, Any]] = [
    {"question": "Where is the Eiffel Tower located?", "gold_answer": "Paris France", "gold_doc_ids": ["d1"]},
    {"question": "What is the tallest mountain on Earth?", "gold_answer": "Mount Everest", "gold_doc_ids": ["d2"]},
    {"question": "At what temperature does water boil?", "gold_answer": "100 degrees Celsius", "gold_doc_ids": ["d3"]},
    {"question": "What does photosynthesis produce?", "gold_answer": "glucose and oxygen", "gold_doc_ids": ["d5"]},
]


# A PARAPHRASE-GAP set: each question paraphrases its gold passage (different words),
# while a DISTRACTOR passage shares a surface word with the question but is about a
# different aspect. Lexical token-overlap is pulled toward the distractor; a semantic
# (dense) retriever recognizes the paraphrase and retrieves the gold. Used to
# demonstrate "dense beats lexical".
_PARAPHRASE_CORPUS: list[dict[str, str]] = [
    {"doc_id": "g1", "text": "To stop a recurring membership, open settings and choose End Plan."},
    {"doc_id": "x1", "text": "A subscription unlocks unlimited access to all premium features."},
    {"doc_id": "g2", "text": "If you can no longer sign in, reset access from the login page via the emailed link."},
    {"doc_id": "x2", "text": "Strong passwords use at least twelve characters and a symbol."},
    {"doc_id": "g3", "text": "Money is returned to your original card within ten business days."},
    {"doc_id": "x3", "text": "We accept major credit cards and PayPal for payment."},
    {"doc_id": "g4", "text": "Turn on two-step verification under the security tab for extra protection."},
    {"doc_id": "x4", "text": "Report any suspicious security incident to our support team promptly."},
    {"doc_id": "g5", "text": "Adjust how often we email you from the notifications preferences screen."},
    {"doc_id": "x5", "text": "Our newsletter shares product news and email tips every week."},
]
_PARAPHRASE_CASES: list[dict[str, Any]] = [
    {"question": "How do I cancel my subscription?", "gold_answer": "End Plan", "gold_doc_ids": ["g1"]},
    {"question": "I forgot my login credentials, how do I get back in?", "gold_answer": "reset access", "gold_doc_ids": ["g2"]},
    {"question": "How can I get a refund?", "gold_answer": "returned to your original card", "gold_doc_ids": ["g3"]},
    {"question": "How do I make my account more secure?", "gold_answer": "two-step verification", "gold_doc_ids": ["g4"]},
    {"question": "How do I stop getting so many emails?", "gold_answer": "notifications preferences", "gold_doc_ids": ["g5"]},
]


def load_qa(data_spec: dict[str, Any] | None = None) -> tuple[list[dict], list[dict]]:
    """Return (corpus, cases). A data spec may point at a JSON file
    (``{"corpus": [...], "cases": [...]}``) or name a built-in set via ``ref``:
    ``paraphrase-qa`` = the paraphrase-gap set; otherwise the mini set."""
    ref = (data_spec or {}).get("ref") or (data_spec or {}).get("uri")
    if ref:
        if str(ref).strip().lower() in ("paraphrase-qa", "paraphrase", "support-faq"):
            return list(_PARAPHRASE_CORPUS), list(_PARAPHRASE_CASES)
        p = Path(str(ref)).expanduser()
        if p.exists():
            data = json.loads(p.read_text())
            return data.get("corpus", []), data.get("cases", [])
    return list(_MINI_CORPUS), list(_MINI_CASES)
