"""Guarded factory entrypoint for the qualification node service."""

from __future__ import annotations


def compose_node_service(*, deployment, configuration_bytes):
    """Load the node factory only after this minimal guarded export is admitted."""

    from aletheia.execution.qualification_node_service import (
        compose_node_service as compose_service,
    )

    return compose_service(
        deployment=deployment,
        configuration_bytes=configuration_bytes,
    )


__all__ = ["compose_node_service"]
