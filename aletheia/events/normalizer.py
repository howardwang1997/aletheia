"""Translate Claude Agent SDK messages into canonical events.

Written defensively (attribute-sniffing rather than hard imports) so it tolerates
SDK version drift in block/message shapes. Subagent activity is attributed via
``parent_tool_use_id`` so the dashboard can show per-agent lanes.
"""

from __future__ import annotations

from typing import Any

from aletheia.events.bus import make_event


def _block_event(block: Any, run_id: str, agent: str, parent: str | None) -> dict | None:
    # Order matters: tool_use and tool_result share some attrs with others.
    if hasattr(block, "name") and hasattr(block, "input"):
        return make_event(
            "tool_use",
            run_id=run_id,
            agent=agent,
            parent_tool_use_id=parent,
            payload={
                "id": getattr(block, "id", None),
                "tool": getattr(block, "name", None),
                "input": getattr(block, "input", None),
            },
        )
    if hasattr(block, "tool_use_id"):
        content = getattr(block, "content", None)
        return make_event(
            "tool_result",
            run_id=run_id,
            agent=agent,
            parent_tool_use_id=parent,
            payload={
                "tool_use_id": getattr(block, "tool_use_id", None),
                "content": _stringify(content),
                "is_error": getattr(block, "is_error", None),
            },
        )
    if hasattr(block, "thinking"):
        return make_event(
            "thinking",
            run_id=run_id,
            agent=agent,
            parent_tool_use_id=parent,
            payload={"text": getattr(block, "thinking", "")},
        )
    if hasattr(block, "text"):
        return make_event(
            "assistant_text",
            run_id=run_id,
            agent=agent,
            parent_tool_use_id=parent,
            payload={"text": getattr(block, "text", "")},
        )
    return None


def _stringify(content: Any) -> Any:
    if content is None or isinstance(content, (str, int, float, bool, dict, list)):
        return content
    return str(content)


def normalize_message(msg: Any, run_id: str, agent: str = "orchestrator") -> list[dict]:
    """Return zero or more canonical events for one SDK message."""
    events: list[dict] = []
    cls = type(msg).__name__
    parent = getattr(msg, "parent_tool_use_id", None)
    lane = agent if parent is None else (getattr(msg, "subagent", None) or "subagent")

    content = getattr(msg, "content", None)
    if isinstance(content, list):  # AssistantMessage / UserMessage with blocks
        for block in content:
            evt = _block_event(block, run_id, lane, parent)
            if evt:
                events.append(evt)

    if cls == "ResultMessage" or hasattr(msg, "total_cost_usd"):
        events.append(
            make_event(
                "result",
                run_id=run_id,
                agent=lane,
                parent_tool_use_id=parent,
                payload={
                    "result": _stringify(getattr(msg, "result", None)),
                    "cost_usd": getattr(msg, "total_cost_usd", None),
                    "usage": _stringify(getattr(msg, "usage", None)),
                    "is_error": getattr(msg, "is_error", None),
                    "num_turns": getattr(msg, "num_turns", None),
                },
            )
        )
    elif cls == "SystemMessage":
        events.append(
            make_event(
                "system",
                run_id=run_id,
                agent=lane,
                parent_tool_use_id=parent,
                payload={
                    "subtype": getattr(msg, "subtype", None),
                    "data": _stringify(getattr(msg, "data", None)),
                },
            )
        )

    if not events and not isinstance(content, list):
        # Unknown message shape — capture it rather than dropping it.
        events.append(
            make_event(
                "system",
                run_id=run_id,
                agent=lane,
                parent_tool_use_id=parent,
                payload={"class": cls, "repr": _stringify(msg)[:2000]},
            )
        )
    return events
