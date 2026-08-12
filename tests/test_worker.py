"""Phase 2 step 2: isolated Worker — dry-run returns its dry_value, emits a
label-tagged event, and independent workers fan out in parallel."""

from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

from aletheia.events.bus import get_bus
from aletheia.orchestrator.reasoner import reason_stage
from aletheia.orchestrator.tools import ToolSpec
from aletheia.orchestrator.worker import (
    _NO_TOOLS,
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


# --- non-dry-run ClaudeAgentOptions wiring (real path, faked SDK) --------------------------
# The dry-run tests above never construct ClaudeAgentOptions, so an SDK incompatibility with
# the option combo we set for text-only workers (empty allowed_tools + a disallow list) would
# only ever surface in a live run. These tests drive the non-dry-run branch with a faked SDK
# that captures the constructor kwargs, pinning the tool-permission contract.
def _install_fake_sdk(monkeypatch):
    """Replace ``claude_agent_sdk`` with a fake that records ClaudeAgentOptions kwargs and
    yields one inline assistant-text message (so the worker returns without retry/backoff)."""
    captured: dict = {}

    class FakeOptions:
        def __init__(self, **kwargs):
            captured.clear()
            captured.update(kwargs)

    class FakeClient:
        def __init__(self, options=None):
            self.options = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def query(self, prompt):
            return None

        async def receive_response(self):
            # one AssistantMessage-shaped object: content blocks with a .text attr
            yield SimpleNamespace(content=[SimpleNamespace(text="inline answer")])

    fake_mod = types.ModuleType("claude_agent_sdk")
    fake_mod.ClaudeAgentOptions = FakeOptions
    fake_mod.ClaudeSDKClient = FakeClient
    fake_mod.tool = lambda name, description, schema: (
        lambda handler: SimpleNamespace(
            name=name, description=description, input_schema=schema, handler=handler,
        )
    )
    fake_mod.create_sdk_mcp_server = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_mod)

    import aletheia.orchestrator.worker as w
    monkeypatch.setattr(w, "has_credentials", lambda *a, **k: True)
    monkeypatch.setattr(w, "configure_auth", lambda *a, **k: None)
    return captured


def test_text_only_worker_disables_all_tools(monkeypatch):
    captured = _install_fake_sdk(monkeypatch)
    out = asyncio.run(run_worker("run-opts1", "coder", "author code", dry_run=False))
    assert out == "inline answer"
    # text-only: autonomous, NO tools allowed, and the full built-in toolset disallowed so the
    # model cannot quietly Write its answer to a file and return only prose.
    assert captured["permission_mode"] == "bypassPermissions"
    assert captured["allowed_tools"] == []
    disallowed = set(captured["disallowed_tools"])
    assert {"Write", "Bash", "Read", "Edit"} <= disallowed
    assert disallowed == set(_NO_TOOLS)


def test_mcp_worker_passes_allowed_tools_and_no_disallow_list(monkeypatch):
    captured = _install_fake_sdk(monkeypatch)
    out = asyncio.run(run_worker(
        "run-opts2", "retriever", "search",
        mcp_servers={"lit": object()}, allowed_tools=["mcp__lit__search"], dry_run=False,
    ))
    assert out == "inline answer"
    # mcp/tool worker: allowed_tools pass through and the text-only disallow list is NOT applied.
    assert captured["allowed_tools"] == ["mcp__lit__search"]
    assert "disallowed_tools" not in captured
    assert captured["permission_mode"] == "bypassPermissions"  # no can_use_tool gate -> autonomous


def test_mcp_worker_with_gate_is_default_permission(monkeypatch):
    captured = _install_fake_sdk(monkeypatch)

    async def gate(*a, **k):
        return True

    out = asyncio.run(run_worker(
        "run-opts3", "retriever", "search",
        mcp_servers={"lit": object()}, allowed_tools=["mcp__lit__search"],
        can_use_tool=gate, dry_run=False,
    ))
    assert out == "inline answer"
    # a tool gate is supplied -> the SDK asks the gate per call instead of bypassing.
    assert captured["permission_mode"] == "default"
    assert captured["can_use_tool"] is gate


def test_provider_neutral_tool_is_adapted_for_claude(monkeypatch):
    captured = _install_fake_sdk(monkeypatch)

    async def lookup(args):
        return {"content": [{"type": "text", "text": args["query"]}]}

    spec = ToolSpec("lookup", "Look up a value.", {"query": str}, lookup)
    out = asyncio.run(run_worker(
        "run-opts4", "retriever", "search",
        tools=[spec], tool_namespace="research", dry_run=False,
    ))
    assert out == "inline answer"
    assert captured["allowed_tools"] == ["mcp__research__lookup"]
    server = captured["mcp_servers"]["research"]
    assert server["tools"][0].name == "lookup"


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
