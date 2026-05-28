"""Conversational scoping verification (dry-run, no quota).

Drives a ConversationSession through two turns and asserts the chat/activity
events stream + persist, the plan is finalized, and the tool gate denies
non-allowlisted tools.
"""

from __future__ import annotations

import asyncio
import time

from aletheia.db import create_all, session_scope
from aletheia.events.store import list_events
from aletheia.memory.ledger import Experiment, Run
from aletheia.memory.service import create_run
from aletheia.orchestrator.gate import build_tool_gate
from aletheia.orchestrator.session import ALLOWLIST, ConversationSession


async def _wait_for(run_id: str, etype: str, timeout: float = 6.0) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        evs = await asyncio.to_thread(list_events, run_id, 500)
        if any(e["type"] == etype for e in evs):
            return evs
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for event '{etype}' on run {run_id}")


async def test_dryrun_conversation_flow():
    create_all()
    run_id = create_run("study perovskite stability", status="scoping")
    sess = ConversationSession(run_id, dry_run=True)
    sess.start()
    try:
        await sess.send("I want to study perovskite stability.")
        await _wait_for(run_id, "assistant_text")
        types = {e["type"] for e in await asyncio.to_thread(list_events, run_id, 500)}
        assert {"user_message", "assistant_text", "status"} <= types, types

        await sess.send("Target is formation energy; use Matbench. Go ahead.")
        await _wait_for(run_id, "goal_finalized")
    finally:
        await sess.close()

    with session_scope() as s:
        run = s.get(Run, run_id)
        exps = s.query(Experiment).filter(Experiment.run_id == run_id).all()
    assert run.status == "planned"
    assert any(e.design_json for e in exps), "expected an experiment with a design_json plan"


async def test_tool_gate_denies_non_allowlisted():
    create_all()
    gate = build_tool_gate(ALLOWLIST, run_id=None)
    allow = await gate("mcp__aletheia__memory_log", {}, None)
    deny = await gate("Bash", {"command": "ls"}, None)
    assert allow.behavior == "allow"
    assert deny.behavior == "deny"
