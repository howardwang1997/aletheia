"""Guarded-loader entry point for the shared workspace one-shot."""

from __future__ import annotations


def build_workspace_service(*, deployment, configuration_bytes):
    """Build only the exact root-owned workspace operation."""

    from aletheia.execution.qualification_root_services import compose_workspace_service

    return compose_workspace_service(
        deployment=deployment,
        configuration_bytes=configuration_bytes,
    )


__all__ = ["build_workspace_service"]
