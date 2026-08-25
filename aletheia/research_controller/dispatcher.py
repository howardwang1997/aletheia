"""Transactional Kernel-outbox to durable-controller task dispatcher."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import Field
from sqlalchemy import func, select

from aletheia.db import session_scope
from aletheia.durable_tasks.ports import DurableTaskQueuePort, TaskConcurrencyConflict
from aletheia.execution.allocator import VerifiedQualificationTerminalSource
from aletheia.observations.store import (
    ControllerDeliveryAttemptWrite,
    ControllerDeliveryWrite,
    ObservationIdentityConflict,
    get_controller_delivery_by_source,
    list_controller_registrations,
    list_scientific_execution_authorizations,
    record_controller_delivery,
    record_controller_delivery_attempt,
)
from aletheia.research_controller.contracts import (
    ControllerModel,
    ControllerWakeup,
    ControllerWakeupKind,
    ResearchControllerManifest,
    controller_initial_delivery_attempt,
    controller_task_spec,
)


class ResearchKernelOutboxPort(Protocol):
    """Operational Kernel outbox CAS; it owns no command-signing or replay authority."""

    def list_pending_outbox_in_session(
        self,
        session,
        *,
        registered_quest_ids: tuple[str, ...],
        limit: int = 100,
        lock_for_publish: bool = True,
    ): ...

    def mark_outbox_published_in_session(self, session, expected): ...


class QualificationTerminalOutboxPort(Protocol):
    """Verified PR-4 source plus exact in-transaction outbox re-read."""

    def load_verified_qualification_terminal_source(
        self,
        *,
        execution_id: str,
        attempt_id: str,
    ) -> VerifiedQualificationTerminalSource | None: ...

    def load_qualification_terminal_outbox_in_session(
        self,
        session,
        *,
        execution_id: str,
        attempt_id: str,
    ): ...


def _database_time(session) -> datetime:
    value = session.scalar(select(func.clock_timestamp()))
    if value is None:  # pragma: no cover - PostgreSQL always returns clock_timestamp()
        raise RuntimeError("PostgreSQL did not provide controller delivery time")
    return value


class ControllerDispatchReceipt(ControllerModel):
    schema_name: Literal["aletheia.research_controller_dispatch_receipt"] = (
        "aletheia.research_controller_dispatch_receipt"
    )
    schema_version: Literal[1] = 1
    delivered_outbox_sha256s: tuple[str, ...]
    concurrency_deferred_quest_ids: tuple[str, ...]
    registered_quest_count: int = Field(ge=0)


class ResearchKernelOutboxDispatcher:
    """Deliver at most one pending Kernel event per registered Quest per invocation."""

    def __init__(
        self,
        *,
        kernel_store: ResearchKernelOutboxPort,
        manifest: ResearchControllerManifest,
        queue: DurableTaskQueuePort,
    ) -> None:
        self._kernel_store = kernel_store
        self._manifest = manifest
        self._queue = queue

    def dispatch_once(self) -> ControllerDispatchReceipt:
        with session_scope() as session:
            registrations = list_controller_registrations(session)
        delivered: list[str] = []
        deferred: list[str] = []
        for registration in registrations:
            if (
                registration.controller_id != self._manifest.controller_id
                or registration.controller_manifest_sha256 != self._manifest.manifest_sha256
                or registration.controller_principal_id != self._manifest.controller_key
            ):
                raise ValueError(
                    "controller registration differs from the deployment-pinned manifest"
                )
            try:
                with session_scope() as session:
                    items = self._kernel_store.list_pending_outbox_in_session(
                        session,
                        registered_quest_ids=(registration.quest_id,),
                        limit=1,
                        lock_for_publish=True,
                    )
                    if not items:
                        continue
                    item = items[0]
                    wakeup = ControllerWakeup(
                        registration_id=registration.registration_id,
                        quest_id=registration.quest_id,
                        source_kind=ControllerWakeupKind.KERNEL_OUTBOX,
                        source_key=item.outbox_id,
                        source_sha256=item.event_sha256,
                        source_stream_version=item.sequence,
                    )
                    spec = controller_task_spec(manifest=self._manifest, wakeup=wakeup)
                    enqueue = self._queue.enqueue_in_session(session, spec)
                    existing = get_controller_delivery_by_source(
                        session,
                        source_kind=ControllerWakeupKind.KERNEL_OUTBOX.value,
                        source_key=item.outbox_id,
                    )
                    delivered_at = (
                        existing.delivered_at if existing is not None else _database_time(session)
                    )
                    delivery = ControllerDeliveryWrite.from_contract(
                        registration_sha256=registration.registration_sha256,
                        wakeup=wakeup,
                        task_id=enqueue.task.task_id,
                        delivered_at=delivered_at,
                    )
                    try:
                        record_controller_delivery(session, delivery)
                        record_controller_delivery_attempt(
                            session,
                            ControllerDeliveryAttemptWrite.from_contract(
                                controller_initial_delivery_attempt(
                                    manifest=self._manifest,
                                    wakeup=wakeup,
                                    delivery_sha256=delivery.delivery_sha256,
                                    task_spec=spec,
                                    recorded_at=delivered_at,
                                )
                            ),
                        )
                    except ObservationIdentityConflict as exc:
                        raise ValueError("Kernel outbox delivery identity was rebound") from exc
                    self._kernel_store.mark_outbox_published_in_session(session, item)
                    delivered.append(item.event_sha256)
            except TaskConcurrencyConflict:
                # The existing per-Quest task must finish first.  The outbox row stays pending and
                # the next dispatcher pass retries this exact immutable source.
                deferred.append(registration.quest_id)
        return ControllerDispatchReceipt(
            delivered_outbox_sha256s=tuple(sorted(delivered)),
            concurrency_deferred_quest_ids=tuple(sorted(deferred)),
            registered_quest_count=len(registrations),
        )


class ExecutionTerminalOutboxDispatcher:
    """Wake the owning Quest once for each immutable PR-4 terminal authority."""

    def __init__(
        self,
        *,
        terminal_outbox: QualificationTerminalOutboxPort,
        manifest: ResearchControllerManifest,
        queue: DurableTaskQueuePort,
    ) -> None:
        self._terminal_outbox = terminal_outbox
        self._manifest = manifest
        self._queue = queue

    def dispatch_once(self) -> ControllerDispatchReceipt:
        with session_scope() as session:
            registrations = list_controller_registrations(session)
            authorizations = list_scientific_execution_authorizations(session)
        registrations_by_quest = {item.quest_id: item for item in registrations}
        delivered: list[str] = []
        deferred: list[str] = []
        for authorization in authorizations:
            registration = registrations_by_quest.get(authorization.quest_id)
            if registration is None:
                continue
            if (
                registration.controller_id != self._manifest.controller_id
                or registration.controller_manifest_sha256 != self._manifest.manifest_sha256
                or registration.controller_principal_id != self._manifest.controller_key
            ):
                raise ValueError(
                    "controller registration differs from the deployment-pinned manifest"
                )
            try:
                source = self._terminal_outbox.load_verified_qualification_terminal_source(
                    execution_id=authorization.execution_id,
                    attempt_id=authorization.attempt_id,
                )
                if source is not None:
                    source = VerifiedQualificationTerminalSource.model_validate(
                        source.model_dump(mode="python")
                    )
                    if (
                        source.execution_id != authorization.execution_id
                        or source.attempt_id != authorization.attempt_id
                        or source.qualification_bundle_sha256
                        != authorization.qualification_bundle_sha256
                        or source.qualification_grant_sha256
                        != authorization.qualification_grant_sha256
                        or authorization.registered_at >= source.qualification_admitted_at
                    ):
                        raise ValueError(
                            "verified execution terminal source differs from its preregistered "
                            "scientific authorization"
                        )
                with session_scope() as session:
                    item = self._terminal_outbox.load_qualification_terminal_outbox_in_session(
                        session,
                        execution_id=authorization.execution_id,
                        attempt_id=authorization.attempt_id,
                    )
                    if source is None:
                        if item is not None:
                            raise ValueError(
                                "execution terminal outbox lacks verified PR-4 lineage"
                            )
                        continue
                    if item is None or (
                        item.execution_id != source.execution_id
                        or item.attempt_id != source.attempt_id
                        or item.outbox_id != source.outbox_id
                        or item.terminal_authority_kind != source.terminal_authority_kind
                        or item.terminal_authority_sha256 != source.terminal_authority_sha256
                        or item.payload_sha256 != source.payload_sha256
                        or item.created_at != source.outbox_created_at
                    ):
                        raise ValueError(
                            "verified execution terminal source changed before delivery"
                        )
                    if (
                        item.execution_id != authorization.execution_id
                        or item.attempt_id != authorization.attempt_id
                    ):
                        raise ValueError(
                            "execution terminal outbox differs from its scientific authorization"
                        )
                    wakeup = ControllerWakeup(
                        registration_id=registration.registration_id,
                        quest_id=registration.quest_id,
                        source_kind=ControllerWakeupKind.EXECUTION_TERMINAL_OUTBOX,
                        source_key=item.outbox_id,
                        source_sha256=item.terminal_authority_sha256,
                    )
                    spec = controller_task_spec(manifest=self._manifest, wakeup=wakeup)
                    existing = get_controller_delivery_by_source(
                        session,
                        source_kind=ControllerWakeupKind.EXECUTION_TERMINAL_OUTBOX.value,
                        source_key=item.outbox_id,
                    )
                    if existing is not None:
                        expected = ControllerDeliveryWrite.from_contract(
                            registration_sha256=registration.registration_sha256,
                            wakeup=wakeup,
                            task_id=spec.task_id,
                            delivered_at=existing.delivered_at,
                            execution_id=item.execution_id,
                            attempt_id=item.attempt_id,
                        )
                        if existing != expected:
                            raise ValueError(
                                "execution terminal delivery differs from its immutable source"
                            )
                        continue
                    enqueue = self._queue.enqueue_in_session(session, spec)
                    delivery = ControllerDeliveryWrite.from_contract(
                        registration_sha256=registration.registration_sha256,
                        wakeup=wakeup,
                        task_id=enqueue.task.task_id,
                        # The immutable task is the durable delivery.  Reuse its database-created
                        # timestamp so two dispatchers that both miss the delivery row before one
                        # blocks on the task INSERT build byte-identical delivery receipts after
                        # the winner commits.
                        delivered_at=enqueue.task.created_at,
                        execution_id=item.execution_id,
                        attempt_id=item.attempt_id,
                    )
                    try:
                        record_controller_delivery(session, delivery)
                        record_controller_delivery_attempt(
                            session,
                            ControllerDeliveryAttemptWrite.from_contract(
                                controller_initial_delivery_attempt(
                                    manifest=self._manifest,
                                    wakeup=wakeup,
                                    delivery_sha256=delivery.delivery_sha256,
                                    task_spec=spec,
                                    recorded_at=delivery.delivered_at,
                                )
                            ),
                        )
                    except ObservationIdentityConflict as exc:
                        raise ValueError(
                            "execution terminal delivery identity was rebound"
                        ) from exc
                    delivered.append(item.terminal_authority_sha256)
            except TaskConcurrencyConflict:
                deferred.append(registration.quest_id)
        return ControllerDispatchReceipt(
            delivered_outbox_sha256s=tuple(sorted(delivered)),
            concurrency_deferred_quest_ids=tuple(sorted(set(deferred))),
            registered_quest_count=len(registrations),
        )


__all__ = [
    "ControllerDispatchReceipt",
    "ExecutionTerminalOutboxDispatcher",
    "QualificationTerminalOutboxPort",
    "ResearchKernelOutboxPort",
    "ResearchKernelOutboxDispatcher",
]
