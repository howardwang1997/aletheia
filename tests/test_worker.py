"""Phase 2 step 2: isolated Worker — dry-run returns its dry_value, emits a
label-tagged event, and independent workers fan out in parallel."""

from __future__ import annotations

import asyncio

from aletheia.events.bus import get_bus
from aletheia.orchestrator.reasoner import reason_stage
from aletheia.orchestrator.worker import run_worker


async def _collect(run_id, n, coro):
    """Run coro while capturing up to ~n bus events for run_id."""
    seen = []

    async def sub():
        async for evt in get_bus().subscribe():
            if evt.get("run_id") == run_id:
                seen.append(evt)
                if len(seen) >= n:
                    return

    task = asyncio.create_task(sub())
    await asyncio.sleep(0)
    result = await coro
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.TimeoutError:
        task.cancel()
    return result, seen


def test_worker_dry_run_returns_value_and_tags_label():
    async def go():
        return await _collect(
            "run-w1", 1,
            run_worker("run-w1", "analyst", "do x", dry_run=True, dry_value="ok-analyst"),
        )

    result, seen = asyncio.run(go())
    assert result == "ok-analyst"
    tagged = [e for e in seen if e["type"] == "assistant_text"]
    assert tagged and tagged[0]["agent"] == "analyst"


def test_parallel_workers_isolated():
    async def go():
        labels = ["leakage", "overfit", "baseline", "stats"]
        results = await asyncio.gather(
            *[run_worker("run-w2", lb, "check", dry_run=True, dry_value=f"{lb}-done") for lb in labels]
        )
        return results

    results = asyncio.run(go())
    assert results == ["leakage-done", "overfit-done", "baseline-done", "stats-done"]


def test_reason_stage_shim():
    out = asyncio.run(
        reason_stage("run-w3", "analysis", "interpret", dry_run=True, dry_text="shimmed")
    )
    assert out == "shimmed"
