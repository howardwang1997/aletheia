"""OpenAI Responses runtime for Aletheia's provider-neutral orchestrator.

The runtime keeps conversation state locally (``store=False``) and replays every response output
item, including reasoning items, as recommended for multi-turn Responses workflows. Local tools use
the same :class:`ToolSpec` handlers as the Claude MCP path; only the transport/schema adapter differs.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from typing import Any

from aletheia.config import Settings
from aletheia.events.bus import get_bus, make_event
from aletheia.orchestrator.tools import ToolSpec


@dataclass
class ResponsesTurn:
    text: str
    history: list[dict[str, Any]]
    usage: dict[str, int] = field(default_factory=dict)
    model: str | None = None
    response_id: str | None = None
    num_turns: int = 0


def _json_type(value_type: Any) -> dict[str, Any]:
    mapping = {str: "string", int: "integer", float: "number", bool: "boolean"}
    if value_type in mapping:
        return {"type": mapping[value_type]}
    if value_type is list:
        return {"type": "array", "items": {}}
    if value_type is dict:
        return {"type": "object", "additionalProperties": True}
    return {"type": "string"}


def responses_tool_schema(spec: ToolSpec) -> dict[str, Any]:
    """Convert a local tool to an OpenAI strict function schema."""
    properties = {name: _json_type(kind) for name, kind in spec.input_schema.items()}
    return {
        "type": "function",
        "name": spec.name,
        "description": spec.description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
        "strict": True,
    }


def _dump_item(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    if hasattr(item, "model_dump"):
        return item.model_dump(exclude_none=True)
    data = getattr(item, "__dict__", None)
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if not k.startswith("_") and v is not None}
    raise TypeError(f"unsupported Responses output item: {type(item).__name__}")


def _response_text(response: Any) -> str:
    direct = getattr(response, "output_text", None)
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    chunks: list[str] = []
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "message":
            continue
        for block in getattr(item, "content", []) or []:
            text = getattr(block, "text", None)
            if isinstance(text, str) and text:
                chunks.append(text)
    return "\n".join(chunks).strip()


def _usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    input_details = getattr(usage, "input_tokens_details", None)
    cached = getattr(input_details, "cached_tokens", 0) if input_details is not None else 0
    cache_write = getattr(input_details, "cache_write_tokens", 0) if input_details is not None else 0
    input_total = int(getattr(usage, "input_tokens", 0) or 0)
    return {
        # Aletheia's canonical usage shape stores uncached, cache-read, and cache-created
        # classes separately. Responses ``input_tokens`` includes its cached-token subset.
        "input_tokens": max(0, input_total - int(cached or 0) - int(cache_write or 0)),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_read_input_tokens": int(cached or 0),
        "cache_creation_input_tokens": int(cache_write or 0),
    }


def _add_usage(total: dict[str, int], current: dict[str, int]) -> None:
    for key, value in current.items():
        total[key] = total.get(key, 0) + int(value)


def _tool_result_text(result: Any) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)


def make_client(settings: Settings):
    """Construct lazily so GPT support does not affect Claude-only installations/tests."""
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url or None)


async def run_responses_turn(
    run_id: str,
    agent: str,
    prompt: str,
    *,
    system: str,
    settings: Settings,
    model: str | None = None,
    tools: list[ToolSpec] | None = None,
    allowed_tools: list[str] | None = None,
    can_use_tool: Any = None,
    max_turns: int = 1,
    history: list[dict[str, Any]] | None = None,
) -> ResponsesTurn:
    """Run one user turn, including a bounded local function-calling loop.

    ``history`` is copied, then extended with the new user input, every model output item, and every
    function result. The returned history can be reused by the multi-turn scoping session.
    """
    client = make_client(settings)
    items = [dict(item) for item in (history or [])]
    items.append({"role": "user", "content": prompt})

    allowed_names = {
        name.split("__")[-1] for name in (allowed_tools or [])
    } if allowed_tools is not None else None
    tool_map = {
        spec.name: spec for spec in (tools or [])
        if allowed_names is None or spec.name in allowed_names
    }
    gate_names = {
        spec_name: next(
            (name for name in (allowed_tools or []) if name.split("__")[-1] == spec_name),
            spec_name,
        )
        for spec_name in tool_map
    }
    schemas = [responses_tool_schema(spec) for spec in tool_map.values()]
    usage_total: dict[str, int] = {}
    final_text = ""
    actual_model: str | None = None
    response_id: str | None = None

    for turn_idx in range(1, max(1, int(max_turns)) + 1):
        request: dict[str, Any] = {
            "model": model or settings.openai_model,
            "instructions": system,
            # Snapshot the request. ``items`` is extended after the response returns; keeping a
            # separate list also makes tracing/mocking reflect exactly what was sent on this call.
            "input": list(items),
            "reasoning": {"effort": settings.openai_reasoning_effort},
            "max_output_tokens": settings.openai_max_output_tokens,
            "store": False,
        }
        if schemas:
            request.update(tools=schemas, parallel_tool_calls=False)
        response = await client.responses.create(**request)
        if getattr(response, "error", None):
            raise RuntimeError(str(response.error))

        response_id = getattr(response, "id", response_id)
        actual_model = getattr(response, "model", actual_model)
        _add_usage(usage_total, _usage(response))
        output = list(getattr(response, "output", []) or [])
        items.extend(_dump_item(item) for item in output)

        text = _response_text(response)
        if text:
            final_text = text
            await get_bus().publish(
                make_event("assistant_text", run_id=run_id, agent=agent, payload={"text": text})
            )

        calls = [item for item in output if getattr(item, "type", None) == "function_call"]
        if not calls:
            await get_bus().publish(make_event(
                "result", run_id=run_id, agent=agent,
                payload={
                    "result": final_text,
                    "cost_usd": None,
                    "usage": usage_total,
                    "is_error": False,
                    "num_turns": turn_idx,
                    "provider": "openai",
                    "model": actual_model or model or settings.openai_model,
                    "response_id": response_id,
                },
            ))
            return ResponsesTurn(
                final_text, items, usage_total, actual_model, response_id, turn_idx,
            )

        if turn_idx >= max(1, int(max_turns)):
            raise RuntimeError(f"OpenAI tool loop exceeded max_turns={max_turns}")

        for call in calls:
            name = str(getattr(call, "name", ""))
            call_id = str(getattr(call, "call_id", ""))
            raw_args = getattr(call, "arguments", "{}") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                if not isinstance(args, dict):
                    raise TypeError("function arguments must decode to an object")
                await get_bus().publish(make_event(
                    "tool_use", run_id=run_id, agent=agent,
                    payload={"id": call_id, "tool": name, "input": args},
                ))
                spec = tool_map.get(name)
                if spec is None:
                    raise PermissionError(f"tool '{name}' is not allowed in this phase")
                if can_use_tool is not None:
                    decision = can_use_tool(gate_names[name], args, None)
                    if inspect.isawaitable(decision):
                        decision = await decision
                    if decision is not True and getattr(decision, "behavior", None) != "allow":
                        message = getattr(decision, "message", None) or "tool gate denied the call"
                        raise PermissionError(str(message))
                result = await spec.handler(args)
                is_error = False
            except Exception as exc:  # return a visible tool error so the model may recover
                result = {"error": f"{type(exc).__name__}: {exc}"}
                is_error = True
            output_text = _tool_result_text(result)
            await get_bus().publish(make_event(
                "tool_result", run_id=run_id, agent=agent,
                payload={"tool_use_id": call_id, "content": result, "is_error": is_error},
            ))
            items.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": output_text,
            })

    raise RuntimeError("OpenAI Responses loop ended without a final response")  # pragma: no cover
