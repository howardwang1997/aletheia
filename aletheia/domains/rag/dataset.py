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


def load_qa(data_spec: dict[str, Any] | None = None) -> tuple[list[dict], list[dict]]:
    """Return (corpus, cases). If the data spec points at a JSON file with
    ``{"corpus": [...], "cases": [...]}`` use it; otherwise the built-in mini set."""
    ref = (data_spec or {}).get("ref") or (data_spec or {}).get("uri")
    if ref:
        p = Path(str(ref)).expanduser()
        if p.exists():
            data = json.loads(p.read_text())
            return data.get("corpus", []), data.get("cases", [])
    return list(_MINI_CORPUS), list(_MINI_CASES)
