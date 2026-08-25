"""Explicit labels for compatibility execution surfaces exposed by legacy APIs."""

from typing import Literal

LegacyExecutionSurface = Literal["legacy_protocol_executor"]
LEGACY_PROTOCOL_EXECUTOR: LegacyExecutionSurface = "legacy_protocol_executor"


def mark_legacy_protocol_executor(value: dict) -> dict:
    """Return a copy so persistence rows are never mutated while shaping API output."""

    return {**value, "execution_surface": LEGACY_PROTOCOL_EXECUTOR}


__all__ = [
    "LEGACY_PROTOCOL_EXECUTOR",
    "LegacyExecutionSurface",
    "mark_legacy_protocol_executor",
]
