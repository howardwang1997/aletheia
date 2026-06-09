"""Conversation-record export (:mod:`aletheia.memory.transcript`).

The ledger already holds every model turn; these tests persist a small conversation (across two
worker lanes plus a bookkeeping event and a usage-bearing result) and assert the export is
lossless (.jsonl) and faithful + readable (.md), preserving order and lane tags.
"""

from __future__ import annotations

import json

from aletheia.db import create_all
from aletheia.events.bus import make_event
from aletheia.events.store import list_run_ids_with_events
from aletheia.memory.service import create_run
from aletheia.memory.transcript import export_transcript


def _ev(run_id, etype, agent, payload):
    from aletheia.events.store import persist_event

    persist_event(make_event(etype, run_id=run_id, agent=agent, payload=payload))


def _seed_conversation(run_id):
    _ev(run_id, "thinking", "coder", {"text": "I should write a regressor."})
    _ev(run_id, "assistant_text", "coder", {"text": "Here is the demonstration code."})
    _ev(run_id, "tool_use", "coder", {"tool": "Bash", "input": {"cmd": "python demo.py"}})
    _ev(run_id, "tool_result", "coder", {"content": "delta_R=0.42", "is_error": False})
    _ev(run_id, "result", "coder", {"cost_usd": 0.04, "num_turns": 3, "usage": {
        "input_tokens": 2, "output_tokens": 900, "cache_read_input_tokens": 12000,
        "cache_creation_input_tokens": 3000}})
    _ev(run_id, "campaign_reason", "orchestrator", {"round": 1, "reason": "generalized"})
    _ev(run_id, "assistant_text", "critic", {"text": "The control is silent; verdict pass."})


def test_export_is_lossless_jsonl_and_preserves_order(tmp_path):
    create_all()
    run_id = create_run("transcript lossless", domain="materials", status="completed")
    _seed_conversation(run_id)

    paths = export_transcript(run_id, out_dir=tmp_path)
    lines = paths["jsonl"].read_text().strip().splitlines()
    assert paths["events"] == 7
    assert len(lines) == 7
    parsed = [json.loads(line) for line in lines]
    # chronological order preserved + full payloads kept (lossless)
    assert [p["type"] for p in parsed] == [
        "thinking", "assistant_text", "tool_use", "tool_result", "result",
        "campaign_reason", "assistant_text",
    ]
    assert parsed[2]["payload"]["input"] == {"cmd": "python demo.py"}


def test_markdown_is_readable_and_lane_tagged(tmp_path):
    create_all()
    run_id = create_run("transcript md", domain="materials", status="completed")
    _seed_conversation(run_id)

    md = export_transcript(run_id, out_dir=tmp_path)["md"].read_text()
    # the actual dialogue content is rendered in full
    assert "I should write a regressor." in md
    assert "Here is the demonstration code." in md
    assert "The control is silent; verdict pass." in md
    # tool call + result rendered
    assert "tool_use → `Bash`" in md
    assert "delta_R=0.42" in md
    # lanes are tagged (two worker sessions interleaved)
    assert "lane: `coder`" in md
    assert "lane: `critic`" in md
    # the token/cost header ties usage into the transcript
    assert "token/cost:" in md
    assert "12,000" in md  # cache_read total surfaced in the header
    # a bookkeeping event is kept as a compact one-liner (nothing hidden)
    assert "campaign_reason" in md


def test_run_is_listed_once_it_has_events(tmp_path):
    create_all()
    run_id = create_run("transcript listed", domain="materials", status="scoping")
    _ev(run_id, "assistant_text", "orchestrator", {"text": "hello"})
    assert run_id in list_run_ids_with_events()
