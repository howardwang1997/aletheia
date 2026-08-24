"""PostgreSQL composition for atomic controller launch and initial wakeup delivery."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select

from aletheia.db import session_scope
from aletheia.durable_tasks.ports import DurableTaskQueuePort
from aletheia.observations.store import (
    ControllerDeliveryAttemptWrite,
    ControllerDeliveryWrite,
    ControllerRegistrationWrite,
    ObservationIdentityConflict,
    get_controller_delivery_by_source,
    get_controller_registration_by_launch_request,
    get_controller_registration_by_quest,
    record_controller_delivery,
    record_controller_delivery_attempt,
    register_controller,
)
from aletheia.research_controller.contracts import (
    ControllerWakeup,
    ControllerWakeupKind,
    ResearchControllerLaunchReceipt,
    ResearchControllerLaunchRequest,
    ResearchControllerManifest,
    ResearchControllerRegistration,
    controller_initial_delivery_attempt,
    controller_task_spec,
)
from aletheia.research_controller.launch import ControllerLaunchConflict
from aletheia.research_controller.launch import verify_launch_audit
from aletheia.research_store.store import ResearchKernelStore


def _database_time(session) -> datetime:
    value = session.scalar(select(func.clock_timestamp()))
    if value is None:  # pragma: no cover - PostgreSQL always returns clock_timestamp()
        raise RuntimeError("PostgreSQL did not provide controller registration time")
    return value


def _registration_contract(write: ControllerRegistrationWrite) -> ResearchControllerRegistration:
    try:
        registration = ResearchControllerRegistration.model_validate(write.registration_json)
    except ValueError as exc:
        raise ControllerLaunchConflict("persisted controller registration is invalid") from exc
    if (
        registration.registration_sha256 != write.registration_sha256
        or registration.registration_id != write.registration_id
        or registration.launch_request.request_sha256 != write.launch_request_sha256
        or registration.controller_id != write.controller_id
        or registration.controller_manifest_sha256 != write.controller_manifest_sha256
        or registration.controller_principal_id != write.controller_principal_id
        or registration.registered_by_principal_id != write.registered_by_principal_id
        or registration.registered_at != write.registered_at
    ):
        raise ControllerLaunchConflict("persisted controller registration was rebound")
    return registration


class PostgreSQLControllerLaunchAdapter:
    """Commit registration, deterministic task, and launch delivery in one transaction."""

    def __init__(
        self,
        *,
        kernel_store: ResearchKernelStore,
        queue: DurableTaskQueuePort,
    ) -> None:
        self._kernel_store = kernel_store
        self._queue = queue

    def register_launch(
        self,
        *,
        request: ResearchControllerLaunchRequest,
        manifest: ResearchControllerManifest,
        registered_by_principal_id: str,
    ) -> ResearchControllerLaunchReceipt:
        with session_scope() as session:
            # Re-audit under the same Quest-head lock used by this write transaction.  The API's
            # earlier audit gives a useful conflict response, but only this check closes the race
            # between that read and durable controller registration.
            verify_launch_audit(
                request=request,
                audit=self._kernel_store.audit_in_session(session, request.quest_id),
            )
            existing_request = get_controller_registration_by_launch_request(
                session, request.request_sha256
            )
            existing_quest = get_controller_registration_by_quest(session, request.quest_id)
            if existing_request is not None:
                registration = _registration_contract(existing_request)
                if existing_quest != existing_request:
                    raise ControllerLaunchConflict(
                        "controller launch request and Quest registrations disagree"
                    )
                if (
                    registration.launch_request != request
                    or registration.controller_id != manifest.controller_id
                    or registration.controller_manifest_sha256 != manifest.manifest_sha256
                    or registration.controller_principal_id != manifest.controller_key
                    or registration.registered_by_principal_id != registered_by_principal_id
                ):
                    raise ControllerLaunchConflict(
                        "controller launch exact retry differs from frozen registration"
                    )
                registration_created = False
            else:
                if existing_quest is not None:
                    raise ControllerLaunchConflict(
                        "Quest is already registered by another controller launch"
                    )
                registration = ResearchControllerRegistration(
                    registration_id=request.registration_id,
                    launch_request=request,
                    controller_id=manifest.controller_id,
                    controller_manifest_sha256=manifest.manifest_sha256,
                    controller_principal_id=manifest.controller_key,
                    registered_by_principal_id=registered_by_principal_id,
                    registered_at=_database_time(session),
                )
                try:
                    append = register_controller(
                        session, ControllerRegistrationWrite.from_contract(registration)
                    )
                except ObservationIdentityConflict as exc:
                    raise ControllerLaunchConflict(
                        "controller registration identity is already bound"
                    ) from exc
                registration_created = append.created

            wakeup = ControllerWakeup(
                registration_id=registration.registration_id,
                quest_id=request.quest_id,
                source_kind=ControllerWakeupKind.LAUNCH,
                source_key=registration.registration_id,
                source_sha256=request.request_sha256,
            )
            task_spec = controller_task_spec(manifest=manifest, wakeup=wakeup)
            enqueue = self._queue.enqueue_in_session(session, task_spec)
            existing_delivery = get_controller_delivery_by_source(
                session,
                source_kind=ControllerWakeupKind.LAUNCH.value,
                source_key=registration.registration_id,
            )
            delivered_at = (
                existing_delivery.delivered_at
                if existing_delivery is not None
                else _database_time(session)
            )
            delivery = ControllerDeliveryWrite.from_contract(
                registration_sha256=registration.registration_sha256,
                wakeup=wakeup,
                task_id=enqueue.task.task_id,
                delivered_at=delivered_at,
            )
            try:
                delivery_append = record_controller_delivery(session, delivery)
                attempt_append = record_controller_delivery_attempt(
                    session,
                    ControllerDeliveryAttemptWrite.from_contract(
                        controller_initial_delivery_attempt(
                            manifest=manifest,
                            wakeup=wakeup,
                            delivery_sha256=delivery.delivery_sha256,
                            task_spec=task_spec,
                            recorded_at=delivered_at,
                        )
                    ),
                )
            except ObservationIdentityConflict as exc:
                raise ControllerLaunchConflict(
                    "controller launch delivery identity is already bound"
                ) from exc
            return ResearchControllerLaunchReceipt(
                registration=registration,
                wakeup=wakeup,
                durable_task_id=enqueue.task.task_id,
                created=(
                    registration_created
                    or enqueue.created
                    or delivery_append.created
                    or attempt_append.created
                ),
            )


__all__ = ["PostgreSQLControllerLaunchAdapter"]
