"""Guarded-loader entry point for the verified qualification-terminal runtime.

The deployment loader audits every module-level object.  Heavy execution authority types therefore
remain inside :mod:`aletheia.execution.terminal_runtime`; this legacy operational seam exports only
the exact composition callable whose source bytes are pinned by the deployment manifest.
"""

from __future__ import annotations


def build_terminal_runtime(*, deployment, controller_manifest, configuration_bytes):
    """Build the public-key-only terminal dispatcher dependencies."""

    from aletheia.execution.terminal_runtime import (
        compose_verified_qualification_terminal_reader,
    )
    from aletheia.jobs.queue import DurableTaskQueue
    from aletheia.research_controller_runtime import ResearchControllerRuntimeDependencies

    terminal_outbox = compose_verified_qualification_terminal_reader(
        role=deployment.role.value,
        process_principal_id=deployment.process_principal_id,
        controller_manifest_sha256=controller_manifest.manifest_sha256,
        prepared_at=deployment.prepared_at,
        configuration_bytes=configuration_bytes,
    )
    return ResearchControllerRuntimeDependencies(
        queue=DurableTaskQueue(principal=deployment.process_principal_id),
        terminal_outbox=terminal_outbox,
    )


__all__ = ["build_terminal_runtime"]
