"""Isolated worker: one focused model task in its own context.

A *worker* runs a single, relatively-independent unit of reasoning in a **fresh,
isolated** provider session and returns only its text. Because each call
is isolated, the FSM driver's main context never accumulates a worker's internal
turns — it keeps only the structured result. Independent workers are plain
coroutines, so callers fan them out with ``asyncio.gather`` for parallel,
context-isolated sub-tasks (e.g. the decomposed ANALYSIS sub-checks).

This generalizes the old single-shot ``reason_stage`` (now a thin shim): same
streaming-to-the-event-bus behavior and dry-run path, plus an optional model
override, tool wiring, and multi-turn allowance.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from aletheia.config import get_settings
from aletheia.events.bus import get_bus, make_event
from aletheia.events.normalizer import normalize_message
from aletheia.orchestrator.auth import configure_auth, has_credentials
from aletheia.orchestrator.tools import ToolSpec, to_claude_tool
from aletheia.memory.service import get_cached_worker, put_cached_worker

STAGE_SYSTEM = (
    "You are Aletheia, an autonomous AI scientist executing one stage of a research "
    "loop in a lights-out lab. Be rigorous, concrete, and concise. When asked for "
    "JSON, return ONLY a single JSON object."
)

_DEGRADED_PREFIX = "[worker-unavailable"
# default outer attempts on a transient API error (the SDK already retries internally).
# Overridable via settings.worker_max_attempts / worker_backoff_s for weak-network resilience.
_OUTER_ATTEMPTS = 2
_BACKOFF_S = 8.0

# Process-wide cap on CONCURRENT live SDK streams (settings.max_concurrent_workers). Created lazily
# at the configured size; on a fragile proxy/tunnel this keeps many long-lived streams from being
# opened at once (the main ECONNRESET trigger). None/0 => unlimited.
_worker_sem: asyncio.Semaphore | None = None
_worker_sem_limit: int | None = None


def _get_worker_sem(limit: int | None) -> asyncio.Semaphore | None:
    global _worker_sem, _worker_sem_limit
    if not limit or limit <= 0:
        return None
    if _worker_sem is None or _worker_sem_limit != limit:
        _worker_sem = asyncio.Semaphore(limit)
        _worker_sem_limit = limit
    return _worker_sem


# Built-in tools a text-only worker must NOT have. Without this, a worker running under
# ``bypassPermissions`` keeps the full default Claude Code toolset, so the model may "help"
# by WRITING its answer to a file (e.g. the coder doing ``Write('/tmp/demo.py', ...)``) and
# returning only prose — then ``extract_code`` gets prose, not a fenced block, and the gate
# rejects it. Text-only workers must return their answer INLINE. (mcp-tool workers opt back
# in via ``allowed_tools``.)
# The HARNESS ORCHESTRATION family (AskUserQuestion, ScheduleWakeup, Cron*, Monitor, plan/worktree
# mode, Task*, push/remote signals) is disallowed because these workers run HEADLESS and SINGLE-SHOT:
# their answer IS the return value. An agentic model that thinks it's in a loop "helpfully" reaches
# for one of these instead of answering — e.g. ScheduleWakeup returned result=None and the direction
# gate degraded across all retries; AskUserQuestion stalls waiting for a human. Forcing them off makes
# the worker reply INLINE. The system prompts already say not to — this enforces it at the gate.
_NO_TOOLS: list[str] = [
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Read",
    "Bash",
    "BashOutput",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "Task",
    "TodoWrite",
    "KillBash",
    "AskUserQuestion",
    "ScheduleWakeup",
    "Monitor",
    "PushNotification",
    "RemoteTrigger",
    "CronCreate",
    "CronDelete",
    "CronList",
    "EnterPlanMode",
    "ExitPlanMode",
    "EnterWorktree",
    "ExitWorktree",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskOutput",
    "TaskStop",
    "TaskUpdate",
]


def degraded_marker(label: str) -> str:
    """Sentinel a worker returns when it could not produce real output (e.g. a
    transient API overload that survived the SDK's own retries). Downstream code
    should ignore degraded results rather than treat the text as a real answer."""
    return f"{_DEGRADED_PREFIX}: {label}]"


def is_degraded(text: str | None) -> bool:
    return (text or "").startswith(_DEGRADED_PREFIX)


async def _cache_get(run_id: str, key: str) -> str | None:
    """Best-effort cache read — the resume cache is an optimization; a DB hiccup must never
    break the worker, so any failure is swallowed and treated as a miss."""
    try:
        return await asyncio.to_thread(get_cached_worker, run_id, key)
    except Exception:  # noqa: BLE001
        return None


async def _cache_put(run_id: str, key: str, label: str, result: str) -> None:
    """Best-effort cache write (e.g. an ad-hoc run_id with no run row simply isn't cached)."""
    try:
        await asyncio.to_thread(put_cached_worker, run_id, key, label, result)
    except Exception:  # noqa: BLE001
        pass


def _looks_like_api_error(text: str) -> bool:
    """The SDK surfaces an exhausted-retry API failure as its final assistant text
    (e.g. 'API Error: 529 Overloaded ...'); treat empty output the same way."""
    t = (text or "").strip()
    return (not t) or t.startswith("API Error:") or ("Overloaded" in t[:120])


async def run_worker(
    run_id: str,
    label: str,
    prompt: str,
    *,
    system: str = STAGE_SYSTEM,
    model: str | None = None,
    mcp_servers: dict[str, Any] | None = None,
    tools: list[ToolSpec] | None = None,
    tool_namespace: str = "aletheia",
    allowed_tools: list[str] | None = None,
    can_use_tool: Any = None,
    max_turns: int = 1,
    max_attempts: int | None = None,
    dry_run: bool,
    dry_value: str | None = None,
    memory_context_receipt_id: str | None = None,
) -> str:
    """Run one isolated worker task and return its text. Streams events tagged with
    ``label`` (the dashboard lane / ledger ``agent``)."""
    settings = get_settings()
    provider = settings.orchestrator_provider
    model_resolved = model or settings.orchestrator_model
    if memory_context_receipt_id is not None:
        from aletheia.programs.memory import ResearchMemoryStore

        memory_receipt = await asyncio.to_thread(
            ResearchMemoryStore().load_task_context,
            memory_context_receipt_id,
        )
        accepted_provider_names = {"claude", "anthropic"} if provider == "claude" else {"openai"}
        if memory_receipt.consumer_provider not in accepted_provider_names:
            raise ValueError("research memory context receipt targets another provider")
        if memory_receipt.consumer_model != model_resolved:
            raise ValueError("research memory context receipt targets another model")
        prompt = (
            memory_receipt.context.prompt_text
            + "\n\nCURRENT TASK (use only the verified memory above):\n"
            + prompt
        )
        await get_bus().publish(
            make_event(
                "research_memory_context_loaded",
                run_id=run_id,
                agent=label,
                payload={
                    "context_receipt_id": memory_context_receipt_id,
                    "context_sha256": memory_receipt.context.context_sha256,
                    "scope_node_id": memory_receipt.context.scope_node_id,
                    "task_key": memory_receipt.context.task_key,
                    "source_fact_count": len(memory_receipt.context.source_fact_ids),
                },
            )
        )
    if dry_run or not has_credentials(settings):
        text = dry_value or f"[dry-run] {label} complete."
        await get_bus().publish(
            make_event("assistant_text", run_id=run_id, agent=label, payload={"text": text})
        )
        return text

    # Resume cache: a stable fingerprint of this exact call. On a RESUMED run (read mode) a prior
    # successful result short-circuits the provider call entirely (0 tokens). A fresh run never reads
    # (no within-run collisions) but still WRITES, so a later resume can fast-forward.
    cache_key = hashlib.sha256(
        "\0".join(
            [
                provider,
                settings.orchestrator_transport,
                label,
                system,
                model_resolved,
                prompt,
                ",".join(sorted(allowed_tools or [])),
            ]
        ).encode("utf-8")
    ).hexdigest()
    if settings.resume_cache_enabled and settings.resume_cache_read:
        cached = await _cache_get(run_id, cache_key)
        if cached is not None:
            await get_bus().publish(
                make_event(
                    "worker_cache_hit",
                    run_id=run_id,
                    agent=label,
                    payload={"label": label, "chars": len(cached)},
                )
            )
            return cached

    options = None
    claude_client = None
    if provider == "claude":
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        configure_auth(settings)
        if tools:
            from claude_agent_sdk import create_sdk_mcp_server

            server = create_sdk_mcp_server(
                name=tool_namespace,
                version="0.0.1",
                tools=[to_claude_tool(spec) for spec in tools],
            )
            mcp_servers = {tool_namespace: server}
            if allowed_tools is None:
                allowed_tools = [f"mcp__{tool_namespace}__{spec.name}" for spec in tools]
        opts: dict[str, Any] = dict(
            model=model or settings.claude_model,
            system_prompt=system,
            setting_sources=[],
            max_turns=max_turns,
            max_budget_usd=settings.budget_usd,
        )
        if mcp_servers:
            opts["mcp_servers"] = mcp_servers
            opts["allowed_tools"] = allowed_tools or []
            # gated (no human prompt) when a tool gate is supplied; else autonomous
            opts["permission_mode"] = "default" if can_use_tool else "bypassPermissions"
            if can_use_tool:
                opts["can_use_tool"] = can_use_tool
        else:
            # text-only worker: no tools. Force an INLINE reply so ``extract_code`` /
            # ``_parse_json`` see the actual answer instead of prose left after a Write tool call.
            opts["permission_mode"] = "bypassPermissions"
            opts["allowed_tools"] = []
            opts["disallowed_tools"] = list(_NO_TOOLS)
        options = ClaudeAgentOptions(**opts)
        claude_client = ClaudeSDKClient
    elif provider == "openai" and mcp_servers and not tools:
        raise ValueError("OpenAI workers require provider-neutral tools=, not Claude MCP servers")

    sem = _get_worker_sem(settings.max_concurrent_workers)
    # per-call override: the discriminating-demonstration authoring is the longest, most critical
    # stream — give just THAT call more patient attempts to land one clean stream, without inflating
    # retries (and token burn) on every other call. None => the global weak-network default.
    max_attempts = max(
        1, int(max_attempts if max_attempts is not None else settings.worker_max_attempts)
    )
    backoff = float(settings.worker_backoff_s)

    last = ""
    for attempt in range(1, max_attempts + 1):
        chunks: list[str] = []
        # Hold a concurrency slot ONLY around the live stream — not the backoff sleep — so a
        # retrying worker frees its slot for others while it waits.
        if sem is not None:
            await sem.acquire()
        try:
            if provider == "openai":
                if settings.openai_auth_mode == "subscription":
                    from aletheia.orchestrator.codex_runtime import run_codex_turn

                    result = await run_codex_turn(
                        run_id,
                        label,
                        prompt,
                        system=system,
                        settings=settings,
                        model=model,
                        tools=tools,
                        allowed_tools=allowed_tools,
                        can_use_tool=can_use_tool,
                        max_turns=max_turns,
                    )
                else:
                    from aletheia.orchestrator.openai_runtime import run_responses_turn

                    result = await run_responses_turn(
                        run_id,
                        label,
                        prompt,
                        system=system,
                        settings=settings,
                        model=model,
                        tools=tools,
                        allowed_tools=allowed_tools,
                        can_use_tool=can_use_tool,
                        max_turns=max_turns,
                    )
                last = result.text
            else:
                assert claude_client is not None and options is not None
                async with claude_client(options=options) as client:
                    await client.query(prompt)
                    async for msg in client.receive_response():
                        for evt in normalize_message(msg, run_id, agent=label):
                            await get_bus().publish(evt)
                            if evt["type"] == "assistant_text":
                                chunks.append((evt.get("payload") or {}).get("text", ""))
                last = "\n".join(chunks).strip()
        except Exception as exc:  # noqa: BLE001 - transient client/transport failures
            last = f"API Error: {exc}"
        finally:
            if sem is not None:
                sem.release()
        if not _looks_like_api_error(last):
            if (
                settings.resume_cache_enabled
            ):  # cache only SUCCESSES — a failed call re-runs on resume
                await _cache_put(run_id, cache_key, label, last)
            return last
        if attempt < max_attempts:
            await get_bus().publish(
                make_event(
                    "worker_retry",
                    run_id=run_id,
                    agent=label,
                    payload={
                        "label": label,
                        "attempt": attempt,
                        "of": max_attempts,
                        "reason": last[:160],
                    },
                )
            )
            await asyncio.sleep(backoff * attempt)
    # transient failure survived the SDK's retries + our outer attempts: degrade
    # cleanly so downstream ignores it instead of ingesting an error string.
    await get_bus().publish(
        make_event(
            "worker_degraded",
            run_id=run_id,
            agent=label,
            payload={"label": label, "reason": last[:200], "attempts": max_attempts},
        )
    )
    return degraded_marker(label)
