"""ChatGPT-subscription orchestrator through the non-interactive Codex CLI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aletheia.config import Settings, get_settings
from aletheia.orchestrator import codex_cli
from aletheia.orchestrator.codex_runtime import (
    CodexTurn,
    _CodexInvocation,
    _exec_codex,
    codex_control_schema,
    run_codex_turn,
)
from aletheia.orchestrator.tools import ToolSpec


class _CaptureBus:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish(self, event: dict) -> dict:
        self.events.append(event)
        return event


def _settings() -> Settings:
    settings = Settings(_env_file=None)
    settings.orchestrator_provider = "openai"
    settings.openai_auth_mode = "subscription"
    settings.openai_model = "gpt-5.6-sol"
    settings.openai_reasoning_effort = "high"
    settings.codex_command = "codex-test"
    settings.codex_timeout_s = 10
    return settings


def test_codex_control_schema_is_strict_and_tool_bounded():
    schema = codex_control_schema(["lookup"])
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["action", "text", "tool_name", "tool_arguments_json"]
    assert schema["properties"]["action"]["enum"] == ["final", "tool_call"]
    assert schema["properties"]["tool_name"]["enum"] == ["none", "lookup"]
    assert codex_control_schema([])["properties"]["action"]["enum"] == ["final"]


def test_codex_subscription_status_rejects_api_key_login(monkeypatch):
    captured: dict = {}

    def fake_run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return SimpleNamespace(returncode=0, stdout="Logged in using ChatGPT", stderr="")

    monkeypatch.setattr(codex_cli.subprocess, "run", fake_run)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    assert codex_cli.machine_has_codex_subscription("codex-test")
    assert captured["command"] == ["codex-test", "login", "status"]
    assert "OPENAI_API_KEY" not in captured["kwargs"]["env"]

    monkeypatch.setattr(
        codex_cli.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="Logged in using an API key", stderr=""
        ),
    )
    assert not codex_cli.machine_has_codex_subscription("codex-test")


def test_codex_exec_is_ephemeral_read_only_and_forces_subscription(monkeypatch):
    captured: dict = {}

    def fake_run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        output_path = command[command.index("-o") + 1]
        Path(output_path).write_text(
            json.dumps(
                {
                    "action": "final",
                    "text": "answer",
                    "tool_name": "none",
                    "tool_arguments_json": "{}",
                }
            ),
            encoding="utf-8",
        )
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 80,
                            "output_tokens": 7,
                            "reasoning_output_tokens": 3,
                        },
                    }
                ),
            ]
        )
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr("aletheia.orchestrator.codex_runtime.subprocess.run", fake_run)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-leak")
    result = _exec_codex(_settings(), "full prompt", codex_control_schema([]), "gpt-test")

    command = captured["command"]
    assert command[:2] == ["codex-test", "exec"]
    assert {"--ignore-user-config", "--ignore-rules", "--ephemeral"} <= set(command)
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert 'forced_login_method="chatgpt"' in command
    assert command[-1] == "-"
    disabled = {command[index + 1] for index, value in enumerate(command) if value == "--disable"}
    assert {"shell_tool", "apps", "plugins", "browser_use", "computer_use"} <= disabled
    assert captured["kwargs"]["input"] == "full prompt"
    assert "OPENAI_API_KEY" not in captured["kwargs"]["env"]
    assert "CODEX_API_KEY" not in captured["kwargs"]["env"]
    assert result.control["text"] == "answer"
    assert result.thread_id == "thread-1"
    assert result.usage == {
        "input_tokens": 20,
        "output_tokens": 7,
        "cache_read_input_tokens": 80,
        "cache_creation_input_tokens": 0,
    }


@pytest.mark.asyncio
async def test_codex_tool_loop_executes_only_allowlisted_local_tool(monkeypatch):
    bus = _CaptureBus()
    calls: list[str] = []
    seen: list[dict] = []
    gated: list[str] = []

    async def handler(args):
        seen.append(args)
        return {"content": [{"type": "text", "text": "1.2 eV"}]}

    async def gate(name, _args, _context):
        gated.append(name)
        return SimpleNamespace(behavior="allow")

    responses = iter(
        [
            _CodexInvocation(
                {
                    "action": "tool_call",
                    "text": "",
                    "tool_name": "lookup",
                    "tool_arguments_json": '{"query":"band gap"}',
                },
                {"input_tokens": 10, "output_tokens": 2},
                "thread-a",
            ),
            _CodexInvocation(
                {
                    "action": "final",
                    "text": "grounded answer",
                    "tool_name": "none",
                    "tool_arguments_json": "{}",
                },
                {"input_tokens": 20, "output_tokens": 4},
                "thread-b",
            ),
        ]
    )

    def fake_exec(_settings, rendered, _schema, _model):
        calls.append(rendered)
        return next(responses)

    monkeypatch.setattr("aletheia.orchestrator.codex_runtime._exec_codex", fake_exec)
    monkeypatch.setattr("aletheia.orchestrator.codex_runtime.get_bus", lambda: bus)
    tool = ToolSpec("lookup", "Look up a measurement.", {"query": str}, handler)
    prior = [{"role": "user", "content": "earlier"}]
    turn = await run_codex_turn(
        "run-codex-1",
        "librarian",
        "find it",
        system="Use evidence.",
        settings=_settings(),
        tools=[tool],
        allowed_tools=["mcp__research__lookup"],
        can_use_tool=gate,
        max_turns=3,
        history=prior,
    )

    assert turn.text == "grounded answer"
    assert turn.num_turns == 2
    assert turn.usage == {"input_tokens": 30, "output_tokens": 6}
    assert prior == [{"role": "user", "content": "earlier"}]
    assert seen == [{"query": "band gap"}]
    assert gated == ["mcp__research__lookup"]
    assert "1.2 eV" in calls[1]
    assert turn.history[-1] == {"role": "assistant", "content": "grounded answer"}
    assert [event["type"] for event in bus.events] == [
        "tool_use",
        "tool_result",
        "assistant_text",
        "result",
    ]
    assert bus.events[-1]["payload"]["auth_mode"] == "subscription"
    assert bus.events[-1]["payload"]["transport"] == "codex_cli"


@pytest.mark.asyncio
async def test_codex_tool_gate_denial_never_executes_handler(monkeypatch):
    bus = _CaptureBus()
    called = False

    async def handler(_args):
        nonlocal called
        called = True
        return {}

    responses = iter(
        [
            _CodexInvocation(
                {
                    "action": "tool_call",
                    "text": "",
                    "tool_name": "write_note",
                    "tool_arguments_json": '{"note":"unsafe"}',
                },
                {},
            ),
            _CodexInvocation(
                {
                    "action": "final",
                    "text": "denied",
                    "tool_name": "none",
                    "tool_arguments_json": "{}",
                },
                {},
            ),
        ]
    )
    monkeypatch.setattr(
        "aletheia.orchestrator.codex_runtime._exec_codex",
        lambda *_args: next(responses),
    )
    monkeypatch.setattr("aletheia.orchestrator.codex_runtime.get_bus", lambda: bus)
    tool = ToolSpec("write_note", "Write a note.", {"note": str}, handler)
    turn = await run_codex_turn(
        "run-codex-deny",
        "agent",
        "try it",
        system="Test.",
        settings=_settings(),
        tools=[tool],
        allowed_tools=["mcp__safe__write_note"],
        can_use_tool=lambda *_args: False,
        max_turns=2,
    )

    assert turn.text == "denied"
    assert not called
    result_event = next(event for event in bus.events if event["type"] == "tool_result")
    assert result_event["payload"]["is_error"] is True
    assert "PermissionError" in str(result_event["payload"]["content"])


def test_subscription_credentials_require_chatgpt_login(monkeypatch):
    from aletheia.orchestrator import auth

    settings = _settings()
    settings.openai_api_key = "an-api-key-is-not-a-subscription"
    monkeypatch.setattr(auth, "machine_has_codex_subscription", lambda _command: False)
    assert not auth.has_credentials(settings)
    monkeypatch.setattr(auth, "machine_has_codex_subscription", lambda _command: True)
    assert auth.has_credentials(settings)
    settings.openai_auth_mode = "api_key"
    assert auth.has_credentials(settings)


@pytest.mark.asyncio
async def test_worker_dispatches_subscription_to_codex_runtime(monkeypatch):
    from aletheia.orchestrator import codex_runtime, worker

    settings = get_settings()
    monkeypatch.setattr(settings, "orchestrator_provider", "openai")
    monkeypatch.setattr(settings, "openai_auth_mode", "subscription")
    monkeypatch.setattr(settings, "resume_cache_enabled", False)
    monkeypatch.setattr(settings, "worker_max_attempts", 1)
    monkeypatch.setattr(worker, "has_credentials", lambda _settings: True)
    captured: dict = {}

    async def fake_turn(run_id, agent, prompt, **kwargs):
        captured.update(run_id=run_id, agent=agent, prompt=prompt, kwargs=kwargs)
        return CodexTurn("subscription result", [], num_turns=1)

    monkeypatch.setattr(codex_runtime, "run_codex_turn", fake_turn)
    result = await worker.run_worker("run-codex-2", "coder", "author", dry_run=False)

    assert result == "subscription result"
    assert captured["agent"] == "coder"
    assert captured["kwargs"]["settings"] is settings


@pytest.mark.asyncio
async def test_subscription_scoping_session_replays_history(monkeypatch):
    from aletheia.orchestrator import codex_runtime, session as session_module

    bus = _CaptureBus()
    calls: list[dict] = []
    two_turns = asyncio.Event()

    async def fake_turn(_run_id, _agent, prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        history = [*kwargs["history"], {"role": "assistant", "content": f"reply:{prompt}"}]
        if len(calls) == 2:
            two_turns.set()
        return CodexTurn(f"reply:{prompt}", history, num_turns=1)

    monkeypatch.setattr(codex_runtime, "run_codex_turn", fake_turn)
    monkeypatch.setattr(session_module, "get_bus", lambda: bus)
    conversation = session_module.ConversationSession("run-codex-session", dry_run=False)
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
    assert "mcp__aletheia__finalize_goal" in calls[0]["allowed_tools"]
