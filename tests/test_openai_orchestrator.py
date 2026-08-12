"""Provider-neutral OpenAI orchestrator contract (no live API calls)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from aletheia.config import Settings, get_settings
from aletheia.orchestrator.auth import has_credentials
from aletheia.orchestrator.openai_runtime import (
    ResponsesTurn,
    responses_tool_schema,
    run_responses_turn,
)
from aletheia.orchestrator.tools import ToolSpec


class _OutputItem(SimpleNamespace):
    def model_dump(self, *, exclude_none: bool = False):
        data = dict(vars(self))
        if exclude_none:
            data = {key: value for key, value in data.items() if value is not None}
        return data


class _CaptureBus:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish(self, event: dict) -> dict:
        self.events.append(event)
        return event


class _FakeResponses:
    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = iter(responses)
        self.requests: list[dict] = []

    async def create(self, **request):
        self.requests.append(request)
        return next(self._responses)


def _fake_client(responses: list[SimpleNamespace]):
    api = _FakeResponses(responses)
    return SimpleNamespace(responses=api), api


def _settings() -> Settings:
    settings = Settings(_env_file=None)
    settings.orchestrator_provider = "openai"
    settings.openai_auth_mode = "api_key"
    settings.openai_api_key = "test-key"
    settings.openai_model = "gpt-5.6-sol"
    settings.openai_reasoning_effort = "high"
    settings.openai_max_output_tokens = 4096
    return settings


def _usage(input_tokens: int, output_tokens: int, cached_tokens: int = 0):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
    )


def test_strict_tool_schema_requires_every_field():
    async def handler(_args):
        return {}

    schema = responses_tool_schema(
        ToolSpec("search", "Search records.", {"query": str, "limit": int}, handler)
    )
    assert schema["type"] == "function"
    assert schema["strict"] is True
    assert schema["parameters"] == {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": ["query", "limit"],
        "additionalProperties": False,
    }


@pytest.mark.asyncio
async def test_responses_text_turn_uses_local_history_and_canonical_events(monkeypatch):
    message = _OutputItem(
        type="message",
        role="assistant",
        content=[{"type": "output_text", "text": "grounded answer"}],
    )
    response = SimpleNamespace(
        id="resp_1",
        model="gpt-5.6-sol",
        output=[message],
        output_text="grounded answer",
        usage=_usage(12, 4, 3),
        error=None,
    )
    client, api = _fake_client([response])
    bus = _CaptureBus()
    monkeypatch.setattr("aletheia.orchestrator.openai_runtime.make_client", lambda _s: client)
    monkeypatch.setattr("aletheia.orchestrator.openai_runtime.get_bus", lambda: bus)

    prior = [{"role": "user", "content": "earlier question"}]
    turn = await run_responses_turn(
        "run-gpt-1", "analyst", "new question",
        system="Use evidence.", settings=_settings(), history=prior,
    )

    assert turn.text == "grounded answer"
    assert turn.usage == {
        "input_tokens": 9,
        "output_tokens": 4,
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 0,
    }
    assert prior == [{"role": "user", "content": "earlier question"}], "history is copied"
    assert turn.history[-2] == {"role": "user", "content": "new question"}
    assert turn.history[-1]["type"] == "message"
    request = api.requests[0]
    assert request["store"] is False
    assert request["reasoning"] == {"effort": "high"}
    assert request["input"][0] == prior[0]
    assert [event["type"] for event in bus.events] == ["assistant_text", "result"]
    assert bus.events[-1]["payload"]["provider"] == "openai"


@pytest.mark.asyncio
async def test_responses_function_call_executes_allowlisted_local_tool(monkeypatch):
    call = _OutputItem(
        type="function_call",
        name="lookup",
        call_id="call_1",
        arguments='{"query":"band gap"}',
    )
    answer = _OutputItem(
        type="message",
        role="assistant",
        content=[{"type": "output_text", "text": "done"}],
    )
    first = SimpleNamespace(
        id="resp_tools_1", model="gpt-5.6-sol", output=[call], output_text="",
        usage=_usage(10, 2), error=None,
    )
    second = SimpleNamespace(
        id="resp_tools_2", model="gpt-5.6-sol", output=[answer], output_text="done",
        usage=_usage(15, 3, 5), error=None,
    )
    client, api = _fake_client([first, second])
    bus = _CaptureBus()
    seen: list[dict] = []
    gated: list[str] = []

    async def handler(args):
        seen.append(args)
        return {"content": [{"type": "text", "text": "1.2 eV"}]}

    async def gate(name, _args, _context):
        gated.append(name)
        return SimpleNamespace(behavior="allow")

    tool = ToolSpec("lookup", "Look up a measurement.", {"query": str}, handler)
    monkeypatch.setattr("aletheia.orchestrator.openai_runtime.make_client", lambda _s: client)
    monkeypatch.setattr("aletheia.orchestrator.openai_runtime.get_bus", lambda: bus)

    turn = await run_responses_turn(
        "run-gpt-2", "librarian", "find it",
        system="Use the lookup tool.", settings=_settings(), tools=[tool],
        allowed_tools=["mcp__research__lookup"], can_use_tool=gate, max_turns=3,
    )

    assert turn.text == "done"
    assert turn.num_turns == 2
    assert turn.usage["input_tokens"] == 20
    assert turn.usage["output_tokens"] == 5
    assert turn.usage["cache_read_input_tokens"] == 5
    assert seen == [{"query": "band gap"}]
    assert gated == ["mcp__research__lookup"]
    assert len(api.requests) == 2
    assert api.requests[0]["tools"][0]["name"] == "lookup"
    tool_output = api.requests[1]["input"][-1]
    assert tool_output["type"] == "function_call_output"
    assert tool_output["call_id"] == "call_1"
    assert "1.2 eV" in tool_output["output"]
    assert [event["type"] for event in bus.events] == [
        "tool_use", "tool_result", "assistant_text", "result",
    ]


@pytest.mark.asyncio
async def test_worker_dispatches_to_openai_without_claude_sdk(monkeypatch):
    from aletheia.orchestrator import openai_runtime, worker

    settings = get_settings()
    monkeypatch.setattr(settings, "orchestrator_provider", "openai")
    monkeypatch.setattr(settings, "openai_auth_mode", "api_key")
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "resume_cache_enabled", False)
    monkeypatch.setattr(settings, "worker_max_attempts", 1)
    captured: dict = {}

    async def fake_turn(run_id, agent, prompt, **kwargs):
        captured.update(run_id=run_id, agent=agent, prompt=prompt, kwargs=kwargs)
        return ResponsesTurn("GPT result", [], num_turns=1)

    monkeypatch.setattr(openai_runtime, "run_responses_turn", fake_turn)
    out = await worker.run_worker("run-gpt-3", "coder", "author", dry_run=False)

    assert out == "GPT result"
    assert captured["agent"] == "coder"
    assert captured["kwargs"]["settings"] is settings


def test_provider_specific_credentials():
    settings = Settings(_env_file=None)
    settings.orchestrator_provider = "openai"
    settings.openai_auth_mode = "api_key"
    settings.openai_api_key = None
    assert not has_credentials(settings)
    settings.openai_api_key = "test-key"
    assert has_credentials(settings)


@pytest.mark.asyncio
async def test_openai_scoping_session_replays_history_and_exposes_scoped_tools(monkeypatch):
    from aletheia.orchestrator import openai_runtime, session as session_module

    bus = _CaptureBus()
    calls: list[dict] = []
    two_turns = asyncio.Event()

    async def fake_turn(_run_id, _agent, prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        history = [*kwargs["history"], {"role": "assistant", "content": f"reply:{prompt}"}]
        if len(calls) == 2:
            two_turns.set()
        return ResponsesTurn(f"reply:{prompt}", history, num_turns=1)

    monkeypatch.setattr(openai_runtime, "run_responses_turn", fake_turn)
    monkeypatch.setattr(session_module, "get_bus", lambda: bus)
    conversation = session_module.ConversationSession("run-gpt-session", dry_run=False)
    task = asyncio.create_task(conversation._run_openai_loop(_settings()))
    try:
        await conversation._queue.put("first")
        await conversation._queue.put("second")
        await asyncio.wait_for(two_turns.wait(), timeout=1.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert calls[0]["history"] == []
    assert calls[1]["history"] == [{"role": "assistant", "content": "reply:first"}]
    assert {tool.name for tool in calls[0]["tools"]} == {
        "memory_log", "memory_recall", "finalize_goal", "request_data", "inspect_dataset",
    }
    assert "mcp__aletheia__finalize_goal" in calls[0]["allowed_tools"]
    assert callable(calls[0]["can_use_tool"])
