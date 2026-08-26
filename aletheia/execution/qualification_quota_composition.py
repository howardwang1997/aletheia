"""Guarded-loader entry point for the loopback quota daemon."""

from __future__ import annotations


def build_quota_service(*, deployment, configuration_bytes):
    """Build only the exact root-owned quota operation."""

    from aletheia.execution.qualification_root_services import compose_quota_service

    return compose_quota_service(
        deployment=deployment,
        configuration_bytes=configuration_bytes,
    )


__all__ = ["build_quota_service"]
