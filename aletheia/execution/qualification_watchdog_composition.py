"""Guarded-loader entry point for the independent deadline watchdog."""

from __future__ import annotations


def build_watchdog_service(*, deployment, configuration_bytes):
    """Build only the exact root-owned watchdog operation."""

    from aletheia.execution.qualification_root_services import compose_watchdog_service

    return compose_watchdog_service(
        deployment=deployment,
        configuration_bytes=configuration_bytes,
    )


__all__ = ["build_watchdog_service"]
