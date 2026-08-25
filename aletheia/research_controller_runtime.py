"""Outer composition of the protected controller handler with the legacy engineering queue.

The controller authority graph imports neither ``aletheia.jobs`` nor its legacy event emitter.
Deployments that still use the v1 durable worker opt into that engineering compatibility here.
Task results remain non-authoritative until the independent Kernel/admission seams commit them.
"""

from __future__ import annotations

from aletheia.jobs.queue import DurableTaskQueue
from aletheia.jobs.worker import DurableWorker
from aletheia.research_controller.contracts import (
    CONTROLLER_TASK_TYPE,
    ResearchControllerManifest,
)
from aletheia.research_controller.service import ResearchControllerService
from aletheia.research_controller.worker import research_controller_task_handler


def research_controller_durable_worker(
    *,
    manifest: ResearchControllerManifest,
    service: ResearchControllerService,
    queue: DurableTaskQueue | None = None,
) -> DurableWorker:
    """Pin the legacy queue worker to one deployment-owned controller manifest."""

    worker_id = f"research-controller:{manifest.controller_id}"
    return DurableWorker(
        worker_id=worker_id,
        worker_manifest_sha256=manifest.worker_manifest_sha256,
        handlers={
            CONTROLLER_TASK_TYPE: research_controller_task_handler(
                manifest=manifest,
                service=service,
            )
        },
        queue=queue,
    )


__all__ = ["research_controller_durable_worker"]
