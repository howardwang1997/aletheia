"""Conversational pre-research: discover_gaps surveys + ranks GENUINE open gaps for a
human to pick. Offline (dry-run LLM + tolerant JSON parsing), no spend."""

from __future__ import annotations

import asyncio

from aletheia.db import create_all
from aletheia.memory.service import create_run
from aletheia.research.gaps import _parse_list, discover_gaps


def test_parse_list_tolerates_fences_and_prose():
    assert _parse_list('```json\n[{"rank":1}]\n```') == [{"rank": 1}]
    assert _parse_list('here are gaps: [{"a":1},{"b":2}] (done)') == [{"a": 1}, {"b": 2}]
    assert _parse_list("not json at all") == []
    assert _parse_list("") == []


def test_discover_gaps_dry_run_returns_ranked_structured():
    create_all()
    run_id = create_run("gap test", domain="molecules", status="scoping")
    gaps = asyncio.run(discover_gaps(run_id, "molecules", "molecular property prediction", dry_run=True))
    assert gaps, "dry-run should return the canned example gap"
    g = gaps[0]
    assert "statement" in g and "why_open" in g
    assert g["candidate_framing"]["contribution_type"] in ("paradigm", "performance")
    # ranked: every returned gap carries a numeric rank and we keep <= k_gaps
    assert all(isinstance(x.get("rank"), (int, float)) for x in gaps)


def test_discover_gaps_ranks_by_rank_field(monkeypatch):
    import aletheia.research.gaps as mod

    async def fake_worker(run_id, label, prompt, **kw):
        # returned out of order -> discover_gaps must sort by rank
        return '[{"rank":3,"statement":"c"},{"rank":1,"statement":"a"},{"rank":2,"statement":"b"}]'

    monkeypatch.setattr(mod, "run_worker", fake_worker)
    monkeypatch.setattr(mod.literature, "search", lambda *a, **k: [])  # no network
    create_all()
    run_id = create_run("gap rank", domain="molecules", status="scoping")
    gaps = asyncio.run(discover_gaps(run_id, "molecules", "x", dry_run=False, k_gaps=2))
    assert [g["statement"] for g in gaps] == ["a", "b"]  # sorted by rank, capped at k_gaps
