"""Guarded-loader entry point for the complete step-specific controller worker."""

from __future__ import annotations


def build_worker_runtime(*, deployment, controller_manifest, configuration_bytes):
    """Build one keyless worker from its exact controller, adapter, RPC, and reader pins."""

    from aletheia.jobs.queue import DurableTaskQueue
    from aletheia.research_controller.worker_composition import (
        compose_research_controller_worker_service,
        load_research_controller_worker_runtime_config,
        validate_worker_deployment_binding,
    )
    from aletheia.research_controller_runtime import ResearchControllerRuntimeDependencies

    config = load_research_controller_worker_runtime_config(configuration_bytes)
    validate_worker_deployment_binding(
        config=config,
        role=deployment.role.value,
        process_principal_id=deployment.process_principal_id,
        controller_manifest=controller_manifest,
        prepared_at=deployment.prepared_at,
    )
    service = compose_research_controller_worker_service(
        config=config,
        controller_manifest=controller_manifest,
        reviewed_code_root=deployment.reviewed_code_root,
    )
    return ResearchControllerRuntimeDependencies(
        queue=DurableTaskQueue(principal=deployment.process_principal_id),
        service=service,
    )


__all__ = ["build_worker_runtime"]
