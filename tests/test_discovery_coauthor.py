"""Co-author discovery path: grok proposes the ANGLE, Claude writes the CODE (aletheia/research/discovery).

Offline + deterministic: a FAKE angle-ideator yields angles, a FAKE author stands in for Claude (no
live SDK), a FAKE gateway/search stand in for the novelty + grounding gates. Verifies the wiring —
angles get code merged in, failed authoring drops only that candidate, the Claude-author runs the
worker + extracts code, and a good angle survives the FULL filter end-to-end carrying its authored code.
"""

from __future__ import annotations

import numpy as np

from aletheia.domains.materials.matbench_task import MaterialsBandGapPlugin
from aletheia.research.discovery import (
    discover, make_claude_code_author, make_coauthor_ideator,
)

_PREREG = {"statistic_name": "s", "supported_if": {"op": ">=", "threshold": 0.5},
           "control_silent_if": {"op": "<=", "threshold": 0.1}}


def _const_demo(test, control):
    return ("def compute_demonstration(X, y, groups, meta):\n"
            f"    return {{'test_statistic': {test}, 'control_statistic': {control}, "
            "'n_test': 50, 'n_control': 50, 'components': {}, 'detail': 'd'}\n")


class _FakePanel:
    def __init__(self, gate_passed):
        self.gate_passed = gate_passed
        self.consensus_verdict = "approve" if gate_passed else "reject"
        self.critiques = []


class _FakeGateway:
    async def review(self, target, content, target_ref=None, run_id=None, dry_run=False, exclude_vendors=None):
        statement = (content.get("hypothesis") or {}).get("statement", "")
        return _FakePanel("NOTNOVEL" not in statement)


def _fake_search(query, k):
    return [] if "UNGROUNDED" in query else [object()] * 6


def test_coauthor_ideator_merges_angle_and_code_and_drops_failures():
    angles = [{"title": "A", "insight": "i", "claim": "c", "prereg": _PREREG},
              {"title": "B", "insight": "i", "claim": "c", "prereg": _PREREG},
              {"title": "C", "insight": "i", "claim": "c", "prereg": _PREREG}]

    def author(angle):
        if angle["title"] == "B":
            return ""                       # authoring produced nothing -> drop B
        if angle["title"] == "C":
            raise RuntimeError("author crashed")  # one angle crashing must not kill the round
        return _const_demo(1.0, 0.0)

    cands = make_coauthor_ideator(lambda avoid, lessons: angles, author)([], [])
    assert [c["title"] for c in cands] == ["A"]          # only the successfully-authored angle remains
    assert cands[0]["claim"] == "c" and cands[0]["code"]  # angle fields + authored code merged


def test_claude_code_author_runs_worker_and_extracts():
    seen = {}

    async def fake_worker(run_id, label, prompt, *, system, model, dry_run):
        seen.update(run_id=run_id, label=label, system=system, dry_run=dry_run,
                    contract=("compute_demonstration" in prompt and "PRE-REGISTERED" in prompt))
        return "```python\ndef compute_demonstration(X, y, groups, meta):\n    return {}\n```"

    def fake_extract(text):
        return text.split("```python", 1)[1].rsplit("```", 1)[0].strip()

    author = make_claude_code_author("rid", worker=fake_worker, extract=fake_extract)
    code = author({"title": "t", "insight": "i", "claim": "c", "prereg": _PREREG})

    assert "def compute_demonstration" in code
    assert seen["run_id"] == "rid" and seen["label"] == "discovery-coder"
    assert seen["system"] and seen["dry_run"] is False and seen["contract"] is True


def test_coauthor_end_to_end_banks_the_good_angle():
    rng = np.random.default_rng(0)
    X, y = rng.random((120, 8)), rng.random(120)
    groups = np.array(["A-B"] * 60 + ["C-D"] * 60, dtype=object)
    angle = {"title": "GOOD", "insight": "i", "claim": "a clean discriminating effect", "prereg": _PREREG}

    ideate = make_coauthor_ideator(lambda avoid, lessons: [angle], lambda ang: _const_demo(1.0, 0.0))
    survivors, rows = discover(
        ideate_fn=ideate, plugin=MaterialsBandGapPlugin(), X=X, y=y, groups=groups,
        gateway=_FakeGateway(), run_id="t", k_survivors=1, max_rounds=1,
        search_fn=_fake_search, briefing_fn=lambda p: "",
        novelty_exclude={"anthropic", "grok"}, log=lambda *a: None,
    )
    assert [s["title"] for s in survivors] == ["GOOD"]
    assert survivors[0]["candidate"]["code"]  # the (Claude-authored) code carried for the campaign


def test_claude_code_author_routes_to_provided_loop():
    """The deadlock fix: when a `loop` is given (the driver runs `discover` in a worker thread), the
    Claude SDK call must be submitted to THAT loop (the main one) via run_coroutine_threadsafe, not a
    fresh in-thread loop. Here `author` is called from the main thread with the loop spun in another
    thread — the inverse arrangement, but it asserts the call actually executes ON the provided loop."""
    import asyncio
    import threading

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    ran_on = {}

    async def fake_worker(run_id, label, prompt, *, system, model, dry_run):
        ran_on["loop"] = asyncio.get_running_loop()
        return "```python\ndef compute_demonstration(X, y, groups, meta):\n    return {}\n```"

    author = make_claude_code_author(
        "rid", loop=loop, worker=fake_worker,
        extract=lambda txt: txt.split("```python", 1)[1].rsplit("```", 1)[0].strip())
    try:
        code = author({"title": "t", "prereg": _PREREG})
    finally:
        loop.call_soon_threadsafe(loop.stop)

    assert "def compute_demonstration" in code
    assert ran_on["loop"] is loop  # executed on the provided loop, not a fresh worker-thread loop
