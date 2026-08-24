"""Atomic delivery-generation reconciliation for the durable research controller."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import Field
from sqlalchemy import column, func, select, table
from sqlalchemy.orm import aliased

from aletheia.db import session_scope
from aletheia.durable_tasks.contracts import TaskSnapshot, TaskSpec, TaskStatus
from aletheia.durable_tasks.ports import DurableTaskQueuePort, TaskConcurrencyConflict
from aletheia.observations.persistence import (
    ResearchControllerDeliveryAttemptRecord,
    ResearchControllerDeliveryResolutionRecord,
    ResearchControllerDeliveryRecord,
)
from aletheia.observations.store import (
    ControllerDeliveryAttemptWrite,
    ControllerDeliveryResolutionWrite,
    ControllerDeliveryWrite,
    ControllerRegistrationWrite,
    get_controller_delivery_by_sha256,
    get_controller_delivery_resolution,
    get_controller_registration_by_quest,
    list_controller_delivery_attempts,
    record_controller_delivery_attempt,
    record_controller_delivery_resolution,
)
from aletheia.research_controller.contracts import (
    ControllerDeadLetterReason,
    ControllerDeliveryAttempt,
    ControllerDeliveryAttemptKind,
    ControllerDeliveryResolution,
    ControllerDeliveryResolutionDisposition,
    ControllerModel,
    ControllerWakeup,
    ResearchControllerManifest,
    ResearchControllerRegistration,
    controller_task_spec,
)
from aletheia.research_controller.service import (
    ControllerStepDisposition,
    ControllerTickReceipt,
)
from aletheia.research_kernel.schemas import canonical_sha256

_DURABLE_TASKS = table(
    "durable_tasks",
    column("task_id"),
    column("status"),
    column("completed_at"),
)


class ControllerDeliveryReconciliationError(RuntimeError):
    """A delivery generation, task envelope, or terminal result failed exact audit."""


class ControllerDeliveryReconciliationReceipt(ControllerModel):
    schema_name: Literal["aletheia.research_controller_delivery_reconciliation_receipt"] = (
        "aletheia.research_controller_delivery_reconciliation_receipt"
    )
    schema_version: Literal[1] = 1
    redriven_attempt_sha256s: tuple[str, ...]
    successor_attempt_sha256s: tuple[str, ...]
    dead_letter_resolution_sha256s: tuple[str, ...]
    terminal_resolution_sha256s: tuple[str, ...]
    concurrency_deferred_delivery_sha256s: tuple[str, ...]
    inspected_delivery_count: int = Field(ge=0)


@dataclass(frozen=True)
class _AuditedDelivery:
    delivery: ControllerDeliveryWrite
    wakeup: ControllerWakeup
    attempts: tuple[ControllerDeliveryAttempt, ...]
    tasks: tuple[TaskSnapshot, ...]


def _database_time(session) -> datetime:
    value = session.scalar(select(func.clock_timestamp()))
    if value is None:  # pragma: no cover - PostgreSQL always provides database time
        raise ControllerDeliveryReconciliationError("PostgreSQL did not provide redrive time")
    return value


def _registration_contract(
    write: ControllerRegistrationWrite,
) -> ResearchControllerRegistration:
    try:
        registration = ResearchControllerRegistration.model_validate(write.registration_json)
        expected = ControllerRegistrationWrite.from_contract(registration)
    except (TypeError, ValueError) as exc:
        raise ControllerDeliveryReconciliationError(
            "controller registration is not canonical"
        ) from exc
    if write != expected:
        raise ControllerDeliveryReconciliationError("controller registration was rebound")
    return registration


def _task_matches_spec(task: TaskSnapshot, spec: TaskSpec) -> bool:
    return all(
        (
            task.task_id == spec.task_id,
            task.task_type == spec.task_type,
            task.inputs_sha256 == spec.inputs_sha256,
            task.inputs == spec.inputs,
            task.dependency_ids == spec.dependency_ids,
            task.owner == spec.owner,
            task.run_id == spec.run_id,
            task.idempotency_key == spec.idempotency_key,
            task.concurrency_key == spec.concurrency_key,
            task.request_sha256 == spec.request_sha256,
            task.retry_policy == spec.retry_policy,
            task.priority == spec.priority,
        )
    )


def _verified_tick_receipt(
    task: TaskSnapshot,
    *,
    wakeup: ControllerWakeup,
) -> ControllerTickReceipt:
    if (
        task.status is not TaskStatus.SUCCEEDED
        or task.terminal_category is None
        or task.terminal_category.value != "success"
        or task.terminal_detail_sha256 is not None
        or task.result is None
        or task.result_sha256 is None
        or task.result_artifact_id is None
    ):
        raise ControllerDeliveryReconciliationError(
            "successful controller task lacks its exact result envelope"
        )
    try:
        receipt = ControllerTickReceipt.model_validate(task.result)
    except (TypeError, ValueError) as exc:
        raise ControllerDeliveryReconciliationError(
            "controller task result is not a typed tick receipt"
        ) from exc
    if (
        receipt.wakeup_sha256 != wakeup.wakeup_sha256
        or task.result_artifact_id != f"research-controller-receipt:{receipt.receipt_sha256}"
        or task.result_sha256
        != canonical_sha256(
            {
                "result_artifact_id": task.result_artifact_id,
                "result": task.result,
            }
        )
    ):
        raise ControllerDeliveryReconciliationError(
            "controller tick receipt differs from its wakeup or task result"
        )
    return receipt


def _assert_completed_successor_eligible(receipt: ControllerTickReceipt) -> None:
    """Require the exact non-authoritative completion that may advance one delivery.

    Awaiting, blocked, and authority-committing ticks are terminal for their immutable source.
    Persisted generation chains are audited through this helper so a direct store caller cannot
    make one of those terminal receipts runnable again.
    """

    step = receipt.step_receipt
    if (
        step.disposition is not ControllerStepDisposition.COMPLETED
        or step.signed_kernel_command_committed
        or step.independent_observation_admission_committed
    ):
        raise ControllerDeliveryReconciliationError(
            "controller successor predecessor is not an internal completed tick"
        )


def _candidate_delivery_sha256s(session, *, limit: int) -> tuple[str, ...]:
    latest = aliased(ResearchControllerDeliveryAttemptRecord)
    latest_generation = (
        select(func.max(latest.generation))
        .where(latest.delivery_sha256 == ResearchControllerDeliveryRecord.delivery_sha256)
        .correlate(ResearchControllerDeliveryRecord)
        .scalar_subquery()
    )
    rows = session.scalars(
        select(ResearchControllerDeliveryRecord.delivery_sha256)
        .join(
            ResearchControllerDeliveryAttemptRecord,
            ResearchControllerDeliveryAttemptRecord.delivery_sha256
            == ResearchControllerDeliveryRecord.delivery_sha256,
        )
        .join(
            _DURABLE_TASKS,
            _DURABLE_TASKS.c.task_id == ResearchControllerDeliveryAttemptRecord.task_id,
        )
        .outerjoin(
            ResearchControllerDeliveryResolutionRecord,
            ResearchControllerDeliveryResolutionRecord.delivery_sha256
            == ResearchControllerDeliveryRecord.delivery_sha256,
        )
        .where(
            ResearchControllerDeliveryAttemptRecord.generation == latest_generation,
            ResearchControllerDeliveryResolutionRecord.delivery_sha256.is_(None),
            _DURABLE_TASKS.c.status.in_(
                (
                    TaskStatus.FAILED.value,
                    TaskStatus.SUCCEEDED.value,
                    TaskStatus.CANCELLED.value,
                )
            ),
        )
        .order_by(
            _DURABLE_TASKS.c.completed_at,
            ResearchControllerDeliveryRecord.delivery_sha256,
        )
        .limit(limit)
    ).all()
    return tuple(rows)


class ControllerDeliveryReconciler:
    """Create one exact successor generation, or durably adjudicate a dead-letter."""

    def __init__(
        self,
        *,
        manifest: ResearchControllerManifest,
        queue: DurableTaskQueuePort,
    ) -> None:
        self._manifest = manifest
        self._queue = queue

    def _audit_locked_delivery(self, session, delivery_sha256: str) -> _AuditedDelivery:
        delivery = get_controller_delivery_by_sha256(
            session,
            delivery_sha256,
            lock_for_update=True,
        )
        if delivery is None:
            raise ControllerDeliveryReconciliationError("controller delivery disappeared")
        registration_write = get_controller_registration_by_quest(session, delivery.quest_id)
        if registration_write is None:
            raise ControllerDeliveryReconciliationError(
                "controller delivery lacks its registration"
            )
        registration = _registration_contract(registration_write)
        if (
            registration_write.registration_sha256 != delivery.registration_sha256
            or registration.registration_id != delivery.registration_id
            or registration.controller_id != self._manifest.controller_id
            or registration.controller_manifest_sha256 != self._manifest.manifest_sha256
            or registration.controller_principal_id != self._manifest.controller_key
        ):
            raise ControllerDeliveryReconciliationError(
                "controller delivery differs from its deployment-pinned registration"
            )
        try:
            wakeup = ControllerWakeup.model_validate(delivery.delivery_json["wakeup"])
            expected_delivery = ControllerDeliveryWrite.from_contract(
                registration_sha256=registration.registration_sha256,
                wakeup=wakeup,
                task_id=delivery.task_id,
                delivered_at=delivery.delivered_at,
                execution_id=delivery.execution_id,
                attempt_id=delivery.attempt_id,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ControllerDeliveryReconciliationError(
                "controller delivery is not canonical"
            ) from exc
        if delivery != expected_delivery:
            raise ControllerDeliveryReconciliationError("controller delivery was rebound")

        writes = list_controller_delivery_attempts(
            session, delivery_sha256=delivery.delivery_sha256
        )
        if not writes or tuple(item.generation for item in writes) != tuple(range(len(writes))):
            raise ControllerDeliveryReconciliationError(
                "controller delivery generation chain is incomplete"
            )
        attempts: list[ControllerDeliveryAttempt] = []
        tasks: list[TaskSnapshot] = []
        for index, write in enumerate(writes):
            try:
                attempt = ControllerDeliveryAttempt.model_validate(write.attempt_json)
                expected_write = ControllerDeliveryAttemptWrite.from_contract(attempt)
            except (TypeError, ValueError) as exc:
                raise ControllerDeliveryReconciliationError(
                    "controller delivery attempt is not canonical"
                ) from exc
            if write != expected_write:
                raise ControllerDeliveryReconciliationError(
                    "controller delivery attempt columns were rebound"
                )
            predecessor = attempts[-1] if attempts else None
            if (
                attempt.delivery_sha256 != delivery.delivery_sha256
                or attempt.quest_id != delivery.quest_id
                or attempt.wakeup_sha256 != wakeup.wakeup_sha256
                or attempt.controller_manifest_sha256 != self._manifest.manifest_sha256
                or attempt.generation != index
                or (index == 0 and attempt.task_id != delivery.task_id)
                or (
                    index > 0
                    and (predecessor is None or attempt.supersedes_task_id != predecessor.task_id)
                )
            ):
                raise ControllerDeliveryReconciliationError(
                    "controller delivery attempt chain was rebound"
                )
            spec = controller_task_spec(
                manifest=self._manifest,
                wakeup=wakeup,
                delivery_sha256=(delivery.delivery_sha256 if index > 0 else None),
                delivery_generation=index,
                supersedes_task_id=(predecessor.task_id if predecessor is not None else None),
            )
            task = self._queue.get_in_session(
                session,
                attempt.task_id,
                lock_for_update=index == len(writes) - 1,
            )
            if attempt.task_request_sha256 != spec.request_sha256 or not _task_matches_spec(
                task, spec
            ):
                raise ControllerDeliveryReconciliationError(
                    "controller delivery attempt differs from its deterministic task"
                )
            if predecessor is not None:
                previous_task = tasks[-1]
                if (
                    attempt.predecessor_status != previous_task.status.value
                    or attempt.predecessor_terminal_category
                    != (
                        previous_task.terminal_category.value
                        if previous_task.terminal_category is not None
                        else None
                    )
                    or attempt.predecessor_terminal_detail_sha256
                    != previous_task.terminal_detail_sha256
                    or attempt.predecessor_result_sha256 != previous_task.result_sha256
                ):
                    raise ControllerDeliveryReconciliationError(
                        "controller delivery attempt falsifies predecessor terminal evidence"
                    )
                if attempt.kind is ControllerDeliveryAttemptKind.COMPLETED_SUCCESSOR:
                    predecessor_receipt = _verified_tick_receipt(
                        previous_task,
                        wakeup=wakeup,
                    )
                    _assert_completed_successor_eligible(predecessor_receipt)
                    if (
                        attempt.predecessor_tick_receipt_sha256
                        != predecessor_receipt.receipt_sha256
                    ):
                        raise ControllerDeliveryReconciliationError(
                            "controller successor differs from its predecessor tick receipt"
                        )
            attempts.append(attempt)
            tasks.append(task)
        return _AuditedDelivery(
            delivery=delivery,
            wakeup=wakeup,
            attempts=tuple(attempts),
            tasks=tuple(tasks),
        )

    def _record_resolution(
        self,
        session,
        *,
        audited: _AuditedDelivery,
        disposition: ControllerDeliveryResolutionDisposition,
        receipt: ControllerTickReceipt | None,
        dead_letter_reason: ControllerDeadLetterReason | None = None,
    ) -> str:
        latest_attempt = audited.attempts[-1]
        task = audited.tasks[-1]
        if task.status not in {TaskStatus.FAILED, TaskStatus.SUCCEEDED, TaskStatus.CANCELLED}:
            raise ControllerDeliveryReconciliationError(
                "only a terminal delivery task can be resolved"
            )
        if task.terminal_category is None:
            raise ControllerDeliveryReconciliationError("terminal delivery task lacks its category")
        step_receipt = receipt.step_receipt if receipt is not None else None
        resolution = ControllerDeliveryResolution(
            delivery_sha256=audited.delivery.delivery_sha256,
            quest_id=audited.delivery.quest_id,
            latest_attempt_sha256=latest_attempt.attempt_sha256,
            exhausted_generation=latest_attempt.generation,
            max_delivery_generation=self._manifest.max_delivery_generation,
            terminal_task_id=task.task_id,
            terminal_task_status=task.status.value,
            terminal_category=task.terminal_category.value,
            terminal_detail_sha256=task.terminal_detail_sha256,
            terminal_result_sha256=task.result_sha256,
            tick_receipt_sha256=(receipt.receipt_sha256 if receipt is not None else None),
            step_disposition=(step_receipt.disposition.value if step_receipt is not None else None),
            signed_kernel_command_committed=(
                step_receipt.signed_kernel_command_committed if step_receipt is not None else None
            ),
            independent_observation_admission_committed=(
                step_receipt.independent_observation_admission_committed
                if step_receipt is not None
                else None
            ),
            controller_manifest_sha256=self._manifest.manifest_sha256,
            disposition=disposition,
            dead_letter_reason=dead_letter_reason,
            resolved_at=_database_time(session),
        )
        record_controller_delivery_resolution(
            session,
            ControllerDeliveryResolutionWrite.from_contract(resolution),
        )
        return resolution.resolution_sha256

    def _append_successor(
        self,
        session,
        *,
        audited: _AuditedDelivery,
        kind: ControllerDeliveryAttemptKind,
        receipt: ControllerTickReceipt | None,
    ) -> str:
        predecessor = audited.attempts[-1]
        task = audited.tasks[-1]
        generation = predecessor.generation + 1
        spec = controller_task_spec(
            manifest=self._manifest,
            wakeup=audited.wakeup,
            delivery_sha256=audited.delivery.delivery_sha256,
            delivery_generation=generation,
            supersedes_task_id=task.task_id,
        )
        enqueue = self._queue.enqueue_in_session(session, spec)
        attempt = ControllerDeliveryAttempt(
            delivery_sha256=audited.delivery.delivery_sha256,
            quest_id=audited.delivery.quest_id,
            wakeup_sha256=audited.wakeup.wakeup_sha256,
            controller_manifest_sha256=self._manifest.manifest_sha256,
            generation=generation,
            kind=kind,
            task_id=spec.task_id,
            task_request_sha256=spec.request_sha256,
            supersedes_task_id=task.task_id,
            predecessor_status=task.status.value,
            predecessor_terminal_category=(
                task.terminal_category.value if task.terminal_category is not None else None
            ),
            predecessor_terminal_detail_sha256=task.terminal_detail_sha256,
            predecessor_result_sha256=task.result_sha256,
            predecessor_tick_receipt_sha256=(
                receipt.receipt_sha256 if receipt is not None else None
            ),
            recorded_at=enqueue.task.created_at,
        )
        record_controller_delivery_attempt(
            session,
            ControllerDeliveryAttemptWrite.from_contract(attempt),
        )
        return attempt.attempt_sha256

    def _process_one(self, delivery_sha256: str) -> tuple[str, str | None]:
        with session_scope() as session:
            audited = self._audit_locked_delivery(session, delivery_sha256)
            existing_resolution = get_controller_delivery_resolution(
                session, delivery_sha256=delivery_sha256
            )
            if existing_resolution is not None:
                try:
                    resolution = ControllerDeliveryResolution.model_validate(
                        existing_resolution.resolution_json
                    )
                except (TypeError, ValueError) as exc:
                    raise ControllerDeliveryReconciliationError(
                        "controller delivery resolution is not canonical"
                    ) from exc
                terminal_task = audited.tasks[-1]
                if (
                    existing_resolution
                    != ControllerDeliveryResolutionWrite.from_contract(resolution)
                    or resolution.delivery_sha256 != audited.delivery.delivery_sha256
                    or resolution.quest_id != audited.delivery.quest_id
                    or resolution.latest_attempt_sha256 != audited.attempts[-1].attempt_sha256
                    or resolution.exhausted_generation != audited.attempts[-1].generation
                    or resolution.max_delivery_generation != self._manifest.max_delivery_generation
                    or resolution.terminal_task_id != audited.tasks[-1].task_id
                    or resolution.terminal_task_status != terminal_task.status.value
                    or resolution.terminal_category
                    != (
                        terminal_task.terminal_category.value
                        if terminal_task.terminal_category is not None
                        else None
                    )
                    or resolution.terminal_detail_sha256 != terminal_task.terminal_detail_sha256
                    or resolution.terminal_result_sha256 != terminal_task.result_sha256
                    or resolution.controller_manifest_sha256 != self._manifest.manifest_sha256
                ):
                    raise ControllerDeliveryReconciliationError(
                        "controller delivery resolution was rebound"
                    )
                if resolution.tick_receipt_sha256 is not None:
                    receipt = _verified_tick_receipt(terminal_task, wakeup=audited.wakeup)
                    if (
                        resolution.tick_receipt_sha256 != receipt.receipt_sha256
                        or resolution.step_disposition != receipt.step_receipt.disposition.value
                        or resolution.signed_kernel_command_committed
                        != receipt.step_receipt.signed_kernel_command_committed
                        or resolution.independent_observation_admission_committed
                        != receipt.step_receipt.independent_observation_admission_committed
                    ):
                        raise ControllerDeliveryReconciliationError(
                            "controller resolution differs from its verified tick receipt"
                        )
                return "terminal", resolution.resolution_sha256
            latest = audited.attempts[-1]
            task = audited.tasks[-1]
            if task.status is TaskStatus.FAILED:
                if (
                    task.terminal_category is None
                    or task.terminal_detail_sha256 is None
                    or task.result_sha256 is not None
                ):
                    raise ControllerDeliveryReconciliationError(
                        "failed controller task lacks exact terminal evidence"
                    )
                if latest.generation >= self._manifest.max_delivery_generation:
                    return (
                        "dead_letter",
                        self._record_resolution(
                            session,
                            audited=audited,
                            disposition=ControllerDeliveryResolutionDisposition.DEAD_LETTER,
                            receipt=None,
                            dead_letter_reason=(
                                ControllerDeadLetterReason.GENERATION_LIMIT_EXHAUSTED
                            ),
                        ),
                    )
                return (
                    "redrive",
                    self._append_successor(
                        session,
                        audited=audited,
                        kind=ControllerDeliveryAttemptKind.FAILURE_REDRIVE,
                        receipt=None,
                    ),
                )
            if task.status is TaskStatus.CANCELLED:
                if (
                    task.terminal_category is None
                    or task.terminal_category.value != "cancelled"
                    or task.terminal_detail_sha256 is None
                    or task.result_sha256 is not None
                ):
                    raise ControllerDeliveryReconciliationError(
                        "cancelled controller task lacks exact terminal evidence"
                    )
                return (
                    "dead_letter",
                    self._record_resolution(
                        session,
                        audited=audited,
                        disposition=ControllerDeliveryResolutionDisposition.DEAD_LETTER,
                        receipt=None,
                        dead_letter_reason=ControllerDeadLetterReason.TASK_CANCELLED,
                    ),
                )
            if task.status is not TaskStatus.SUCCEEDED:
                return "inactive", None
            try:
                receipt = _verified_tick_receipt(task, wakeup=audited.wakeup)
            except ControllerDeliveryReconciliationError:
                return (
                    "dead_letter",
                    self._record_resolution(
                        session,
                        audited=audited,
                        disposition=ControllerDeliveryResolutionDisposition.DEAD_LETTER,
                        receipt=None,
                        dead_letter_reason=(ControllerDeadLetterReason.INVALID_SUCCEEDED_RESULT),
                    ),
                )
            step = receipt.step_receipt
            needs_successor = (
                step.disposition is ControllerStepDisposition.COMPLETED
                and not step.signed_kernel_command_committed
                and not step.independent_observation_admission_committed
            )
            if not needs_successor:
                disposition = {
                    ControllerStepDisposition.AWAITING_AUTHORITY: (
                        ControllerDeliveryResolutionDisposition.AWAITING_AUTHORITY
                    ),
                    ControllerStepDisposition.AWAITING_EXTERNAL_RESULT: (
                        ControllerDeliveryResolutionDisposition.AWAITING_EXTERNAL_RESULT
                    ),
                    ControllerStepDisposition.BLOCKED: (
                        ControllerDeliveryResolutionDisposition.BLOCKED
                    ),
                    ControllerStepDisposition.COMPLETED: (
                        ControllerDeliveryResolutionDisposition.AUTHORITATIVE_SOURCE_COMMITTED
                    ),
                }[step.disposition]
                return (
                    "terminal",
                    self._record_resolution(
                        session,
                        audited=audited,
                        disposition=disposition,
                        receipt=receipt,
                    ),
                )
            if latest.generation >= self._manifest.max_delivery_generation:
                return (
                    "dead_letter",
                    self._record_resolution(
                        session,
                        audited=audited,
                        disposition=ControllerDeliveryResolutionDisposition.DEAD_LETTER,
                        receipt=receipt,
                        dead_letter_reason=(ControllerDeadLetterReason.GENERATION_LIMIT_EXHAUSTED),
                    ),
                )
            return (
                "successor",
                self._append_successor(
                    session,
                    audited=audited,
                    kind=ControllerDeliveryAttemptKind.COMPLETED_SUCCESSOR,
                    receipt=receipt,
                ),
            )

    def reconcile_once(self, *, limit: int = 100) -> ControllerDeliveryReconciliationReceipt:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
            raise ValueError("controller reconciliation limit must be between 1 and 1000")
        with session_scope() as session:
            candidates = _candidate_delivery_sha256s(session, limit=limit)
        redriven: list[str] = []
        successors: list[str] = []
        dead_letters: list[str] = []
        terminal: list[str] = []
        deferred: list[str] = []
        for delivery_sha256 in candidates:
            try:
                disposition, identity = self._process_one(delivery_sha256)
            except TaskConcurrencyConflict:
                deferred.append(delivery_sha256)
                continue
            if disposition == "redrive" and identity is not None:
                redriven.append(identity)
            elif disposition == "successor" and identity is not None:
                successors.append(identity)
            elif disposition == "dead_letter" and identity is not None:
                dead_letters.append(identity)
            elif disposition == "terminal" and identity is not None:
                terminal.append(identity)
        return ControllerDeliveryReconciliationReceipt(
            redriven_attempt_sha256s=tuple(sorted(redriven)),
            successor_attempt_sha256s=tuple(sorted(successors)),
            dead_letter_resolution_sha256s=tuple(sorted(dead_letters)),
            terminal_resolution_sha256s=tuple(sorted(terminal)),
            concurrency_deferred_delivery_sha256s=tuple(sorted(deferred)),
            inspected_delivery_count=len(candidates),
        )


__all__ = [
    "ControllerDeliveryReconciler",
    "ControllerDeliveryReconciliationError",
    "ControllerDeliveryReconciliationReceipt",
]
