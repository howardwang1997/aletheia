"""Phase 2 step 2: isolated Worker — dry-run returns its dry_value, emits a
label-tagged event, and independent workers fan out in parallel."""

from __future__ import annotations

import asyncio

from aletheia.events.bus import get_bus
from aletheia.orchestrator.reasoner import reason_stage
from aletheia.orchestrator.worker import (
    _looks_like_api_error,
    degraded_marker,
    is_degraded,
    run_worker,
)


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


def test_degradation_helpers():
    assert _looks_like_api_error("")
    assert _looks_like_api_error("API Error: 529 Overloaded")
    assert _looks_like_api_error("the service is Overloaded right now")
    assert not _looks_like_api_error("LCSO MAE 0.466; no leakage.")
    m = degraded_marker("analysis:leakage")
    assert is_degraded(m) and not is_degraded("a real finding")


def test_analysis_excludes_degraded_subchecks(monkeypatch):
    """A degraded sub-check is filtered out of the synthesis input (no error text)."""
    import aletheia.scheduler.driver as drv

    captured = {}

    async def fake_worker(run_id, label, prompt, **kw):
        if label == "analysis":  # the synthesis call
            captured["prompt"] = prompt
            return "synthesis"
        if label == "analysis:leakage":
            return degraded_marker(label)  # this one failed
        return f"{label}: fine"

    monkeypatch.setattr(drv, "run_worker", fake_worker)
    # a real run/experiment so the analysis-stage claim rows satisfy their FK
    from aletheia.db import create_all
    from aletheia.memory.service import create_run, finalize_plan

    create_all()
    run_id = create_run("worker analysis test", domain="materials", status="planned")
    exp_id = finalize_plan(run_id, {"objective": "predict band gap", "domain": "materials"})
    driver = drv.ExperimentDriver(run_id, dry_run=False)
    result = {"metrics": {"mae_lcso": 0.4, "mae_holdout": 0.4}, "info": {"eval_summary": "s"}}
    out = asyncio.run(driver._analyze({"model": "rf"}, result, exp_id))
    assert out == "synthesis"
    # synthesis prompt saw the healthy sub-checks but NOT the degraded leakage text
    assert "worker-unavailable" not in captured["prompt"]
    assert "unavailable sub-checks, excluded: leakage" in captured["prompt"]
    assert "overfit: fine" in captured["prompt"]
