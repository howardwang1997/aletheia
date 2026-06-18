"""Weak-network resilience for the live worker: a CONCURRENCY cap on simultaneous SDK streams +
configurable AUTO-RETRY (each attempt a fresh connection) before degrading.

These fake the Claude SDK client so the retry/concurrency control flow is exercised without any
real network. The point: on a flaky proxy/tunnel, (a) we never open more than N long streams at
once, and (b) a transient failure is retried `worker_max_attempts` times, not given up on at once.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from aletheia.config import get_settings
from aletheia.db import create_all
from aletheia.memory.service import create_run
from aletheia.orchestrator import worker


def _ok_message():
    # a message normalize_message turns into one assistant_text chunk
    return SimpleNamespace(content=[SimpleNamespace(text="FAKE OUTPUT")])


@pytest.mark.asyncio
async def test_auto_retry_then_degrade(monkeypatch):
    create_all()
    run_id = create_run("retry", domain="materials", status="scoping")

    attempts = {"n": 0}

    class FailClient:
        def __init__(self, *a, **k):
            attempts["n"] += 1

        async def __aenter__(self):
            raise ConnectionResetError("ECONNRESET (simulated)")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(worker, "has_credentials", lambda _s: True)
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", FailClient, raising=False)
    monkeypatch.setattr(get_settings(), "worker_max_attempts", 3)
    monkeypatch.setattr(get_settings(), "worker_backoff_s", 0.0)  # no real waiting in the test
    monkeypatch.setattr(get_settings(), "max_concurrent_workers", None)

    out = await worker.run_worker(run_id, "coder", "author it", dry_run=False)
    assert worker.is_degraded(out)        # gave up cleanly after exhausting retries
    assert attempts["n"] == 3             # retried exactly worker_max_attempts times


@pytest.mark.asyncio
async def test_concurrency_cap_limits_simultaneous_streams(monkeypatch):
    create_all()
    run_id = create_run("concurrency", domain="materials", status="scoping")

    state = {"active": 0, "max": 0}

    class CountingClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
            return self

        async def __aexit__(self, *a):
            state["active"] -= 1
            return False

        async def query(self, _prompt):
            pass

        async def receive_response(self):
            await asyncio.sleep(0.05)  # simulate a long-lived stream
            yield _ok_message()

    monkeypatch.setattr(worker, "has_credentials", lambda _s: True)
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", CountingClient, raising=False)
    monkeypatch.setattr(get_settings(), "max_concurrent_workers", 2)
    monkeypatch.setattr(get_settings(), "worker_max_attempts", 1)
    # force a fresh semaphore at the test's limit (module-global is cached by limit)
    worker._worker_sem = None
    worker._worker_sem_limit = None

    outs = await asyncio.gather(*[
        worker.run_worker(run_id, f"w{i}", "go", dry_run=False) for i in range(6)
    ])
    assert all(o == "FAKE OUTPUT" for o in outs)   # all completed
    assert state["max"] <= 2                        # never more than the cap in flight at once
    assert state["max"] >= 2                         # and the cap was actually exercised


@pytest.mark.asyncio
async def test_unlimited_when_cap_unset(monkeypatch):
    # default (None) => no semaphore => the getter returns None
    monkeypatch.setattr(get_settings(), "max_concurrent_workers", None)
    worker._worker_sem = None
    worker._worker_sem_limit = None
    assert worker._get_worker_sem(get_settings().max_concurrent_workers) is None


class _AlwaysReset:
    """A fake SDK client that ECONNRESETs on every attempt, counting constructions."""

    def __init__(self, counter):
        self._counter = counter

    def __call__(self, *a, **k):
        self._counter["n"] += 1
        return self

    async def __aenter__(self):
        raise ConnectionResetError("ECONNRESET (simulated)")

    async def __aexit__(self, *a):
        return False


def _wire_failing_sdk(monkeypatch):
    counter = {"n": 0}
    monkeypatch.setattr(worker, "has_credentials", lambda _s: True)
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", _AlwaysReset(counter), raising=False)
    monkeypatch.setattr(get_settings(), "worker_max_attempts", 2)  # frugal GLOBAL default
    monkeypatch.setattr(get_settings(), "worker_backoff_s", 0.0)  # no real waiting in the test
    monkeypatch.setattr(get_settings(), "max_concurrent_workers", None)
    worker._worker_sem = None
    worker._worker_sem_limit = None
    return counter


@pytest.mark.asyncio
async def test_per_call_max_attempts_overrides_global(monkeypatch):
    # the discriminating-demonstration authoring passes a higher per-call max_attempts; it must beat
    # the frugal global default so the one critical long stream gets patient retries.
    create_all()
    run_id = create_run("retry override", domain="materials", status="scoping")
    counter = _wire_failing_sdk(monkeypatch)
    out = await worker.run_worker(run_id, "coder", "author it", max_attempts=5, dry_run=False)
    assert worker.is_degraded(out)
    assert counter["n"] == 5  # the per-call override, NOT the global 2


@pytest.mark.asyncio
async def test_max_attempts_none_falls_back_to_global(monkeypatch):
    # every OTHER call passes None and must stay frugal at the global default.
    create_all()
    run_id = create_run("retry fallback", domain="materials", status="scoping")
    counter = _wire_failing_sdk(monkeypatch)
    out = await worker.run_worker(run_id, "coder", "author it", max_attempts=None, dry_run=False)
    assert worker.is_degraded(out)
    assert counter["n"] == 2  # None => global worker_max_attempts


def test_headless_worker_disallows_harness_orchestration_tools():
    # a headless single-shot worker must never reach for a harness orchestration/scheduling/UI tool —
    # an agentic model that thinks it's in a loop calls one instead of answering (ScheduleWakeup
    # returned None and degraded the direction gate; AskUserQuestion stalls for a human). Enforced
    # at the tool gate so the worker replies INLINE.
    for tool in ("AskUserQuestion", "ScheduleWakeup", "Monitor", "CronCreate",
                 "EnterPlanMode", "TaskCreate"):
        assert tool in worker._NO_TOOLS, tool
