from __future__ import annotations

import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from aletheia.db import engine, session_scope
from aletheia.jobs.contracts import RetryPolicy, TaskExecutionResult, TerminalCategory
from aletheia.jobs.queue import DurableTaskQueue
from aletheia.observations.store import (
    ControllerDeliveryAttemptWrite,
    ControllerDeliveryResolutionWrite,
    ControllerDeliveryWrite,
    ControllerRegistrationWrite,
    list_controller_delivery_attempts,
    list_controller_delivery_resolutions,
    record_controller_delivery,
    record_controller_delivery_attempt,
    register_controller,
)
from aletheia.observations.persistence import (
    ResearchControllerDeliveryAttemptRecord,
    ResearchControllerDeliveryResolutionRecord,
)
from aletheia.research_controller.contracts import (
    CONTROLLER_TASK_TYPE,
    ControllerDeadLetterReason,
    ControllerDeliveryAttempt,
    ControllerDeliveryAttemptKind,
    ControllerDeliveryResolution,
    ControllerDeliveryResolutionDisposition,
    ControllerStep,
    ControllerTickPlan,
    ControllerWakeup,
    ControllerWakeupKind,
    ResearchControllerLaunchRequest,
    ResearchControllerManifest,
    ResearchControllerRegistration,
    controller_initial_delivery_attempt,
    controller_task_spec,
)
from aletheia.research_controller.redrive import ControllerDeliveryReconciler
from aletheia.research_controller.service import (
    ControllerStepDisposition,
    ControllerStepReceipt,
    ControllerTickReceipt,
)

_RESEARCH_KERNEL_TESTS = Path(__file__).resolve().parents[1] / "research_kernel"
sys.path.insert(0, str(_RESEARCH_KERNEL_TESTS))

from test_store import _quest_fixture, _store  # noqa: E402

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def _manifest() -> ResearchControllerManifest:
    return ResearchControllerManifest(
        controller_key="controller:redrive-postgresql",
        controller_code_sha256="1" * 64,
        controller_policy_sha256="2" * 64,
        capability_catalog_sha256="3" * 64,
        protocol_registry_policy_sha256="4" * 64,
        scientific_bridge_policy_sha256="5" * 64,
        worker_manifest_sha256="6" * 64,
        retry_policy=RetryPolicy(
            max_attempts=1,
            lease_seconds=2,
            heartbeat_interval_seconds=1,
            initial_backoff_seconds=0,
            max_backoff_seconds=0,
        ),
        prepared_at=NOW,
    )


def _tick(
    wakeup: ControllerWakeup, disposition: ControllerStepDisposition
) -> ControllerTickReceipt:
    plan = ControllerTickPlan(
        projection_sha256="a" * 64,
        step=ControllerStep.COMPILE_PROTOCOL,
        audited_stream_version=1,
        audited_tail_event_sha256="b" * 64,
        audited_snapshot_sha256="c" * 64,
        blocker_codes=(),
    )
    step = ControllerStepReceipt(
        wakeup_sha256=wakeup.wakeup_sha256,
        plan_sha256=plan.plan_sha256,
        disposition=disposition,
        result_artifact_sha256s=(),
        blocker_codes=(),
    )
    return ControllerTickReceipt(
        wakeup_sha256=wakeup.wakeup_sha256,
        recovery_projection_sha256=plan.projection_sha256,
        plan=plan,
        step_receipt=step,
    )


def _complete(
    queue: DurableTaskQueue,
    *,
    wakeup: ControllerWakeup,
    disposition: ControllerStepDisposition,
    now: datetime,
) -> None:
    lease = queue.claim(
        worker_id="pytest:redrive-worker",
        worker_manifest_sha256=_manifest().worker_manifest_sha256,
        task_types=[CONTROLLER_TASK_TYPE],
        now=now,
    )
    assert lease is not None
    tick = _tick(wakeup, disposition)
    queue.complete(
        lease,
        TaskExecutionResult(
            result_artifact_id=f"research-controller-receipt:{tick.receipt_sha256}",
            result=tick.model_dump(mode="json"),
        ),
        now=now + timedelta(seconds=1),
    )


def _append_direct_completed_successor(
    queue: DurableTaskQueue,
    *,
    manifest: ResearchControllerManifest,
    wakeup: ControllerWakeup,
    delivery_sha256: str,
    now: datetime,
    claimed_tick_receipt_sha256: str | None = None,
) -> None:
    """Bypass the portable precheck so a fresh schema exercises the deferred trigger."""

    with session_scope() as session:
        predecessor = list_controller_delivery_attempts(
            session,
            delivery_sha256=delivery_sha256,
        )[-1]
        predecessor_task = queue.get_in_session(session, predecessor.task_id)
        receipt = ControllerTickReceipt.model_validate(predecessor_task.result)
        generation = predecessor.generation + 1
        spec = controller_task_spec(
            manifest=manifest,
            wakeup=wakeup,
            delivery_sha256=delivery_sha256,
            delivery_generation=generation,
            supersedes_task_id=predecessor.task_id,
        )
        enqueue = queue.enqueue_in_session(session, spec, now=now)
        attempt = ControllerDeliveryAttempt(
            delivery_sha256=delivery_sha256,
            quest_id=wakeup.quest_id,
            wakeup_sha256=wakeup.wakeup_sha256,
            controller_manifest_sha256=manifest.manifest_sha256,
            generation=generation,
            kind=ControllerDeliveryAttemptKind.COMPLETED_SUCCESSOR,
            task_id=spec.task_id,
            task_request_sha256=spec.request_sha256,
            supersedes_task_id=predecessor.task_id,
            predecessor_status=predecessor_task.status.value,
            predecessor_terminal_category=predecessor_task.terminal_category.value,
            predecessor_result_sha256=predecessor_task.result_sha256,
            predecessor_tick_receipt_sha256=(claimed_tick_receipt_sha256 or receipt.receipt_sha256),
            recorded_at=enqueue.task.created_at,
        )
        write = ControllerDeliveryAttemptWrite.from_contract(attempt)
        session.add(ResearchControllerDeliveryAttemptRecord(**write.model_dump(mode="python")))


def _append_direct_stale_resolution(
    queue: DurableTaskQueue,
    *,
    manifest: ResearchControllerManifest,
    delivery_sha256: str,
    quest_id: str,
    resolved_at: datetime,
) -> None:
    """Insert a valid older-attempt resolution directly for deferred-trigger coverage."""

    with session_scope() as session:
        attempts = list_controller_delivery_attempts(
            session,
            delivery_sha256=delivery_sha256,
        )
        stale = attempts[-2]
        task = queue.get_in_session(session, stale.task_id)
        receipt = ControllerTickReceipt.model_validate(task.result)
        resolution = ControllerDeliveryResolution(
            delivery_sha256=delivery_sha256,
            quest_id=quest_id,
            latest_attempt_sha256=stale.attempt_sha256,
            exhausted_generation=stale.generation,
            max_delivery_generation=stale.generation,
            terminal_task_id=stale.task_id,
            terminal_task_status=task.status.value,
            terminal_category=task.terminal_category.value,
            terminal_result_sha256=task.result_sha256,
            tick_receipt_sha256=receipt.receipt_sha256,
            step_disposition=receipt.step_receipt.disposition.value,
            signed_kernel_command_committed=(receipt.step_receipt.signed_kernel_command_committed),
            independent_observation_admission_committed=(
                receipt.step_receipt.independent_observation_admission_committed
            ),
            controller_manifest_sha256=manifest.manifest_sha256,
            disposition=ControllerDeliveryResolutionDisposition.DEAD_LETTER,
            dead_letter_reason=ControllerDeadLetterReason.GENERATION_LIMIT_EXHAUSTED,
            resolved_at=resolved_at,
        )
        write = ControllerDeliveryResolutionWrite.from_contract(resolution)
        session.add(ResearchControllerDeliveryResolutionRecord(**write.model_dump(mode="python")))


def test_postgresql_redrive_restart_concurrency_successor_and_resolution(
    tmp_path: Path,
) -> None:
    if os.environ.get("ALETHEIA_PR5_FRESH_DB_TEST") != "1":
        pytest.skip("requires an explicitly isolated fresh PostgreSQL database")
    if not inspect(engine()).has_table("research_controller_delivery_resolutions"):
        pytest.skip("requires a fresh 0027 schema with controller delivery generations")

    suffix = uuid.uuid4().hex
    quest_id = "qst_" + suffix[:32]
    archive, scope, _charter, genesis, _branch, trust_root, policy = _quest_fixture(
        tmp_path / "cas",
        label=f"redrive-{suffix}",
        quest_id=quest_id,
    )
    genesis_receipt = _store(trust_root, policy, archive=archive).commit(genesis)
    assert scope.program_id is not None
    manifest = _manifest()
    request = ResearchControllerLaunchRequest(
        program_id=scope.program_id,
        quest_id=quest_id,
        idempotency_key=f"redrive:{suffix}",
        expected_stream_version=genesis_receipt.result_stream_version,
        expected_tail_event_sha256=genesis_receipt.result_event_sha256,
        expected_snapshot_sha256=genesis_receipt.result_snapshot_sha256,
    )
    registration = ResearchControllerRegistration(
        registration_id=request.registration_id,
        launch_request=request,
        controller_id=manifest.controller_id,
        controller_manifest_sha256=manifest.manifest_sha256,
        controller_principal_id=manifest.controller_key,
        registered_by_principal_id="pytest:redrive-launcher",
        registered_at=NOW,
    )
    wakeup = ControllerWakeup(
        registration_id=registration.registration_id,
        quest_id=quest_id,
        source_kind=ControllerWakeupKind.LAUNCH,
        source_key=registration.registration_id,
        source_sha256=request.request_sha256,
    )
    queue = DurableTaskQueue(principal="pytest:redrive-queue")
    initial_spec = controller_task_spec(manifest=manifest, wakeup=wakeup)
    with session_scope() as session:
        register_controller(session, ControllerRegistrationWrite.from_contract(registration))
        enqueue = queue.enqueue_in_session(session, initial_spec, now=NOW)
        delivery = ControllerDeliveryWrite.from_contract(
            registration_sha256=registration.registration_sha256,
            wakeup=wakeup,
            task_id=initial_spec.task_id,
            delivered_at=enqueue.task.created_at,
        )
        record_controller_delivery(session, delivery)
        record_controller_delivery_attempt(
            session,
            ControllerDeliveryAttemptWrite.from_contract(
                controller_initial_delivery_attempt(
                    manifest=manifest,
                    wakeup=wakeup,
                    delivery_sha256=delivery.delivery_sha256,
                    task_spec=initial_spec,
                    recorded_at=delivery.delivered_at,
                )
            ),
        )

    lease = queue.claim(
        worker_id="pytest:crashed-controller",
        worker_manifest_sha256=manifest.worker_manifest_sha256,
        task_types=[CONTROLLER_TASK_TYPE],
        now=NOW,
    )
    assert lease is not None
    queue.recover_expired(now=lease.lease_expires_at + timedelta(microseconds=1))

    first = ControllerDeliveryReconciler(manifest=manifest, queue=queue).reconcile_once()
    assert len(first.redriven_attempt_sha256s) == 1
    assert (
        ControllerDeliveryReconciler(manifest=manifest, queue=queue)
        .reconcile_once()
        .inspected_delivery_count
        == 0
    )
    with session_scope() as session:
        generation_one_attempt = list_controller_delivery_attempts(
            session,
            delivery_sha256=delivery.delivery_sha256,
        )[-1]
    generation_one_available_at = queue.get(generation_one_attempt.task_id).available_at

    generation_one = queue.claim(
        worker_id="pytest:failed-controller",
        worker_manifest_sha256=manifest.worker_manifest_sha256,
        task_types=[CONTROLLER_TASK_TYPE],
        now=generation_one_available_at,
    )
    assert generation_one is not None
    queue.fail(
        generation_one,
        category=TerminalCategory.INFRASTRUCTURE,
        detail_sha256="c" * 64,
        retry=False,
        now=generation_one_available_at + timedelta(seconds=1),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = tuple(
            pool.map(
                lambda _: ControllerDeliveryReconciler(
                    manifest=manifest, queue=queue
                ).reconcile_once(),
                range(2),
            )
        )
    assert sum(len(item.redriven_attempt_sha256s) for item in receipts) == 1
    with session_scope() as session:
        assert tuple(
            item.generation
            for item in list_controller_delivery_attempts(
                session, delivery_sha256=delivery.delivery_sha256
            )
        ) == (0, 1, 2)

    _complete(
        queue,
        wakeup=wakeup,
        disposition=ControllerStepDisposition.COMPLETED,
        now=generation_one_available_at + timedelta(minutes=2),
    )
    with pytest.raises(
        IntegrityError,
        match="completed controller successor lacks its exact internal tick receipt",
    ):
        _append_direct_completed_successor(
            queue,
            manifest=manifest,
            wakeup=wakeup,
            delivery_sha256=delivery.delivery_sha256,
            now=generation_one_available_at + timedelta(minutes=2, seconds=2),
            claimed_tick_receipt_sha256="f" * 64,
        )
    successor = ControllerDeliveryReconciler(manifest=manifest, queue=queue).reconcile_once()
    assert len(successor.successor_attempt_sha256s) == 1

    _complete(
        queue,
        wakeup=wakeup,
        disposition=ControllerStepDisposition.AWAITING_AUTHORITY,
        now=generation_one_available_at + timedelta(minutes=3),
    )
    with pytest.raises(
        IntegrityError,
        match="completed controller successor lacks its exact internal tick receipt",
    ):
        _append_direct_completed_successor(
            queue,
            manifest=manifest,
            wakeup=wakeup,
            delivery_sha256=delivery.delivery_sha256,
            now=generation_one_available_at + timedelta(minutes=3, seconds=2),
        )
    with pytest.raises(
        IntegrityError,
        match="controller resolution does not target the latest delivery attempt",
    ):
        _append_direct_stale_resolution(
            queue,
            manifest=manifest,
            delivery_sha256=delivery.delivery_sha256,
            quest_id=quest_id,
            resolved_at=generation_one_available_at + timedelta(minutes=3, seconds=3),
        )
    with ThreadPoolExecutor(max_workers=2) as pool:
        resolutions = tuple(
            pool.map(
                lambda _: ControllerDeliveryReconciler(
                    manifest=manifest, queue=queue
                ).reconcile_once(),
                range(2),
            )
        )
    assert sum(len(item.terminal_resolution_sha256s) for item in resolutions) >= 1
    with session_scope() as session:
        stored = list_controller_delivery_resolutions(session, quest_id=quest_id)
        assert len(stored) == 1
        assert stored[0].disposition == "awaiting_authority"
    with pytest.raises(
        IntegrityError,
        match="resolved controller delivery cannot append another attempt",
    ):
        _append_direct_completed_successor(
            queue,
            manifest=manifest,
            wakeup=wakeup,
            delivery_sha256=delivery.delivery_sha256,
            now=generation_one_available_at + timedelta(minutes=4),
        )
    assert (
        ControllerDeliveryReconciler(manifest=manifest, queue=queue)
        .reconcile_once()
        .inspected_delivery_count
        == 0
    )
