"""Guarded-loader entry point for the qualification terminal-outbox service."""

from __future__ import annotations


def build_outbox_service(*, deployment, configuration_bytes):
    """Build only the exact non-root outbox operation."""

    from aletheia.execution.qualification_outbox_service import compose_outbox_service

    return compose_outbox_service(
        deployment=deployment,
        configuration_bytes=configuration_bytes,
    )


__all__ = ["build_outbox_service"]
