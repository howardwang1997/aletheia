"""Audited launch/status boundary for the durable Research Kernel controller."""

from __future__ import annotations

from typing import Protocol

from aletheia.research_controller.contracts import (
    ResearchControllerLaunchReceipt,
    ResearchControllerLaunchRequest,
    ResearchControllerManifest,
)
from aletheia.research_store.store import ResearchKernelStore, ResearchReplayAudit


class ControllerLaunchConflict(ValueError):
    """The caller's expected authoritative head is stale or differently scoped."""


class ControllerLaunchPersistencePort(Protocol):
    """Atomically register launch + delivery + durable task using database time."""

    def register_launch(
        self,
        *,
        request: ResearchControllerLaunchRequest,
        manifest: ResearchControllerManifest,
        registered_by_principal_id: str,
    ) -> ResearchControllerLaunchReceipt: ...


def verify_launch_audit(
    *, request: ResearchControllerLaunchRequest, audit: ResearchReplayAudit
) -> None:
    """Require the launch request to name the exact audited Kernel head and scope."""

    if audit.quest_id != request.quest_id or audit.scope_binding.quest_id != request.quest_id:
        raise ControllerLaunchConflict("controller launch belongs to another Quest")
    if audit.scope_binding.program_id != request.program_id:
        raise ControllerLaunchConflict("controller launch belongs to another Program")
    if len(audit.events) != request.expected_stream_version:
        raise ControllerLaunchConflict("controller launch expected stream version is stale")
    if not audit.events:
        raise ControllerLaunchConflict("controller launch requires an activated Quest stream")
    if (
        audit.state.quest_id != audit.quest_id
        or audit.state.stream_version != len(audit.events)
        or audit.state.event_ids != tuple(event.event_id for event in audit.events)
        or audit.state.event_sha256s != tuple(event.event_sha256 for event in audit.events)
        or audit.state.tail_event_sha256 != audit.events[-1].event_sha256
    ):
        raise ControllerLaunchConflict("controller launch audit projection is inconsistent")
    if audit.events[-1].event_sha256 != request.expected_tail_event_sha256:
        raise ControllerLaunchConflict("controller launch expected tail event is stale")
    if audit.state.snapshot_sha256 != request.expected_snapshot_sha256:
        raise ControllerLaunchConflict("controller launch expected snapshot is stale")
    if (
        len(audit.verified_snapshot_sha256s) != len(audit.events)
        or audit.verified_snapshot_sha256s[-1] != request.expected_snapshot_sha256
    ):
        raise ControllerLaunchConflict("controller launch snapshot lacks verified custody")


class ResearchControllerLauncher:
    """Read the authoritative ledger first, then atomically subscribe through the launch port."""

    def __init__(
        self,
        *,
        kernel_store: ResearchKernelStore,
        manifest: ResearchControllerManifest,
        persistence: ControllerLaunchPersistencePort,
    ) -> None:
        self._kernel_store = kernel_store
        self._manifest = manifest
        self._persistence = persistence

    def launch(
        self,
        request: ResearchControllerLaunchRequest,
        *,
        registered_by_principal_id: str,
    ) -> ResearchControllerLaunchReceipt:
        audit = self._kernel_store.audit(request.quest_id)
        verify_launch_audit(request=request, audit=audit)
        return self._persistence.register_launch(
            request=request,
            manifest=self._manifest,
            registered_by_principal_id=registered_by_principal_id,
        )


__all__ = [
    "ControllerLaunchConflict",
    "ControllerLaunchPersistencePort",
    "ResearchControllerLauncher",
    "verify_launch_audit",
]
