"""Programmatic tool gate (the seed of the planned PreToolUse guardrail).

Used with ``permission_mode="default"`` so the SDK consults this callback for
every tool call and we decide allow/deny with no human prompt — autonomous but
gated. During scoping the allowlist is just the two MCP tools, so the agent can
converse and record a plan but cannot run Bash/Write/web/etc.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aletheia.events.bus import get_bus, make_event


def build_tool_gate(
    allowlist: set[str], run_id: str | None = None
) -> Callable[[str, dict[str, Any], Any], Awaitable[Any]]:
    """Return a ``can_use_tool`` callback that allows only ``allowlist`` tools."""
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context: Any):
        if tool_name in allowlist:
            return PermissionResultAllow()
        # Record the denial so it's visible/auditable, then deny.
        await get_bus().publish(
            make_event(
                "tool_denied",
                run_id=run_id,
                payload={"tool": tool_name, "reason": "not in allowlist"},
            )
        )
        available = ", ".join(sorted(t.split("__")[-1] for t in allowlist)) or "none"
        return PermissionResultDeny(
            message=(
                f"Tool '{tool_name}' is not available in this phase. To ask the user "
                "something, just write it as your normal reply — the conversation is the "
                f"channel, no tool is needed. Available tools: {available}."
            )
        )

    return can_use_tool
