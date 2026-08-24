from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from aletheia.jobs.contracts import RetryPolicy, TaskSnapshot, TaskStatus, TerminalCategory
from aletheia.observations.store import (
    ControllerDeliveryAttemptWrite,
    ControllerDeliveryResolutionWrite,
    ControllerDeliveryWrite,
)
from aletheia.reproducibility.manifest import content_sha256
from aletheia.research_controller.contracts import (
    ControllerDeliveryAttempt,
    ControllerDeliveryAttemptKind,
    ControllerDeliveryResolution,
    ControllerDeliveryResolutionDisposition,
    ControllerStep,
    ControllerTickPlan,
    ControllerWakeup,
    ControllerWakeupKind,
    ResearchControllerManifest,
    controller_task_spec,
)
from aletheia.research_controller.redrive import (
    ControllerDeliveryReconciler,
    ControllerDeliveryReconciliationError,
    _AuditedDelivery,
    _assert_completed_successor_eligible,
)
from aletheia.research_controller.service import (
    ControllerStepDisposition,
    ControllerStepReceipt,
    ControllerTickReceipt,
)

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def _manifest(*, max_generation: int = 8) -> ResearchControllerManifest:
    return ResearchControllerManifest(
        controller_key="controller:redrive-v1",
        controller_code_sha256="1" * 64,
        controller_policy_sha256="2" * 64,
        capability_catalog_sha256="3" * 64,
        protocol_registry_policy_sha256="4" * 64,
        scientific_bridge_policy_sha256="5" * 64,
        worker_manifest_sha256="6" * 64,
        retry_policy=RetryPolicy(
            max_attempts=2,
            lease_seconds=60,
            heartbeat_interval_seconds=10,
        ),
        max_delivery_generation=max_generation,
        prepared_at=NOW,
    )


def _wakeup() -> ControllerWakeup:
    return ControllerWakeup(
        registration_id="rcr_" + "7" * 32,
        quest_id="qst_" + "8" * 32,
        source_kind=ControllerWakeupKind.LAUNCH,
        source_key="rcr_" + "7" * 32,
        source_sha256="9" * 64,
    )


def _tick(
    disposition: ControllerStepDisposition,
    *,
    signed_kernel_command_committed: bool = False,
    independent_observation_admission_committed: bool = False,
) -> ControllerTickReceipt:
    plan = ControllerTickPlan(
        projection_sha256="a" * 64,
        step=(
            ControllerStep.COMMIT_ADMISSION
            if independent_observation_admission_committed
            else ControllerStep.COMPILE_PROTOCOL
            if disposition is ControllerStepDisposition.COMPLETED
            else ControllerStep.AWAIT_EXECUTION
        ),
        audited_stream_version=1,
        audited_tail_event_sha256="b" * 64,
        audited_snapshot_sha256="c" * 64,
        blocker_codes=("blocked:test",) if disposition is ControllerStepDisposition.BLOCKED else (),
    )
    step = ControllerStepReceipt(
        wakeup_sha256=_wakeup().wakeup_sha256,
        plan_sha256=plan.plan_sha256,
        disposition=disposition,
        result_artifact_sha256s=(),
        blocker_codes=("blocked:test",) if disposition is ControllerStepDisposition.BLOCKED else (),
        signed_kernel_command_committed=signed_kernel_command_committed,
        independent_observation_admission_committed=(independent_observation_admission_committed),
    )
    return ControllerTickReceipt(
        wakeup_sha256=_wakeup().wakeup_sha256,
        recovery_projection_sha256=plan.projection_sha256,
        plan=plan,
        step_receipt=step,
    )


def _task(
    manifest: ResearchControllerManifest,
    *,
    status: TaskStatus,
    tick: ControllerTickReceipt | None = None,
    invalid_result: bool = False,
) -> TaskSnapshot:
    spec = controller_task_spec(manifest=manifest, wakeup=_wakeup())
    result = (
        {"invalid": True} if invalid_result else (tick.model_dump(mode="json") if tick else None)
    )
    artifact = (
        "research-controller-receipt:" + (tick.receipt_sha256 if tick is not None else "f" * 64)
        if result is not None
        else None
    )
    result_sha256 = (
        content_sha256({"result_artifact_id": artifact, "result": result})
        if result is not None and artifact is not None
        else None
    )
    if status is TaskStatus.SUCCEEDED:
        category = TerminalCategory.SUCCESS
        detail = None
    elif status is TaskStatus.CANCELLED:
        category = TerminalCategory.CANCELLED
        detail = "d" * 64
    else:
        category = TerminalCategory.INFRASTRUCTURE_EXHAUSTED
        detail = "e" * 64
    return TaskSnapshot(
        task_id=spec.task_id,
        task_type=spec.task_type,
        inputs_sha256=spec.inputs_sha256,
        inputs=spec.inputs,
        dependency_ids=spec.dependency_ids,
        owner=spec.owner,
        run_id=spec.run_id,
        idempotency_key=spec.idempotency_key,
        concurrency_key=spec.concurrency_key,
        request_sha256=spec.request_sha256,
        retry_policy=spec.retry_policy,
        priority=spec.priority,
        status=status,
        attempt_count=2,
        state_version=5,
        available_at=NOW,
        active_attempt_id=None,
        lease_owner=None,
        lease_expires_at=None,
        result_artifact_id=artifact,
        result_sha256=result_sha256,
        result=result,
        terminal_category=category,
        terminal_detail_sha256=detail,
        created_at=NOW,
        updated_at=NOW + timedelta(minutes=1),
        completed_at=NOW + timedelta(minutes=1),
    )


def _audited(manifest: ResearchControllerManifest, task: TaskSnapshot) -> _AuditedDelivery:
    spec = controller_task_spec(manifest=manifest, wakeup=_wakeup())
    delivery = ControllerDeliveryWrite.from_contract(
        registration_sha256="0" * 64,
        wakeup=_wakeup(),
        task_id=spec.task_id,
        delivered_at=NOW,
    )
    attempt = ControllerDeliveryAttempt(
        delivery_sha256=delivery.delivery_sha256,
        quest_id=_wakeup().quest_id,
        wakeup_sha256=_wakeup().wakeup_sha256,
        controller_manifest_sha256=manifest.manifest_sha256,
        generation=0,
        kind=ControllerDeliveryAttemptKind.INITIAL,
        task_id=spec.task_id,
        task_request_sha256=spec.request_sha256,
        recorded_at=NOW,
    )
    return _AuditedDelivery(
        delivery=delivery,
        wakeup=_wakeup(),
        attempts=(attempt,),
        tasks=(task,),
    )


class _Harness(ControllerDeliveryReconciler):
    def __init__(self, manifest, audited):
        super().__init__(manifest=manifest, queue=object())
        self.audited = audited
        self.successors = []
        self.resolutions = []

    def _audit_locked_delivery(self, _session, _delivery_sha256):
        return self.audited

    def _append_successor(self, _session, **kwargs):
        self.successors.append(kwargs)
        return "1" * 64

    def _record_resolution(self, _session, **kwargs):
        self.resolutions.append(kwargs)
        return "2" * 64


@pytest.fixture
def transaction(monkeypatch):
    @contextmanager
    def scope():
        yield object()

    monkeypatch.setattr("aletheia.research_controller.redrive.session_scope", scope)
    monkeypatch.setattr(
        "aletheia.research_controller.redrive.get_controller_delivery_resolution",
        lambda *_args, **_kwargs: None,
    )


def test_failed_task_redrives_and_generation_cap_dead_letters(transaction) -> None:
    manifest = _manifest()
    harness = _Harness(
        manifest,
        _audited(manifest, _task(manifest, status=TaskStatus.FAILED)),
    )
    assert harness._process_one(harness.audited.delivery.delivery_sha256) == (
        "redrive",
        "1" * 64,
    )
    assert harness.successors[0]["kind"] is ControllerDeliveryAttemptKind.FAILURE_REDRIVE

    capped = _manifest(max_generation=0)
    harness = _Harness(capped, _audited(capped, _task(capped, status=TaskStatus.FAILED)))
    assert harness._process_one(harness.audited.delivery.delivery_sha256) == (
        "dead_letter",
        "2" * 64,
    )
    assert harness.resolutions[0]["dead_letter_reason"].value == "generation_limit_exhausted"


def test_completed_receipt_only_step_gets_one_successor(transaction) -> None:
    manifest = _manifest()
    tick = _tick(ControllerStepDisposition.COMPLETED)
    harness = _Harness(
        manifest,
        _audited(manifest, _task(manifest, status=TaskStatus.SUCCEEDED, tick=tick)),
    )
    assert harness._process_one(harness.audited.delivery.delivery_sha256) == (
        "successor",
        "1" * 64,
    )
    assert harness.successors[0]["kind"] is ControllerDeliveryAttemptKind.COMPLETED_SUCCESSOR
    assert harness.successors[0]["receipt"] == tick

    capped = _manifest(max_generation=0)
    capped_tick = _tick(ControllerStepDisposition.COMPLETED)
    harness = _Harness(
        capped,
        _audited(
            capped,
            _task(capped, status=TaskStatus.SUCCEEDED, tick=capped_tick),
        ),
    )
    assert harness._process_one(harness.audited.delivery.delivery_sha256) == (
        "dead_letter",
        "2" * 64,
    )
    assert harness.resolutions[0]["dead_letter_reason"].value == "generation_limit_exhausted"
    assert harness.resolutions[0]["receipt"] == capped_tick


@pytest.mark.parametrize(
    ("disposition", "signed", "independent"),
    (
        (ControllerStepDisposition.AWAITING_AUTHORITY, False, False),
        (ControllerStepDisposition.AWAITING_EXTERNAL_RESULT, False, False),
        (ControllerStepDisposition.BLOCKED, False, False),
        (ControllerStepDisposition.COMPLETED, True, False),
        (ControllerStepDisposition.COMPLETED, True, True),
    ),
)
def test_persisted_successor_audit_requires_internal_completed_tick(
    disposition, signed, independent
) -> None:
    _assert_completed_successor_eligible(_tick(ControllerStepDisposition.COMPLETED))

    receipt = _tick(
        disposition,
        signed_kernel_command_committed=signed,
        independent_observation_admission_committed=independent,
    )
    with pytest.raises(
        ControllerDeliveryReconciliationError,
        match="not an internal completed tick",
    ):
        _assert_completed_successor_eligible(receipt)


@pytest.mark.parametrize(
    ("disposition", "signed", "independent", "expected"),
    (
        (ControllerStepDisposition.AWAITING_AUTHORITY, False, False, "awaiting_authority"),
        (
            ControllerStepDisposition.AWAITING_EXTERNAL_RESULT,
            False,
            False,
            "awaiting_external_result",
        ),
        (ControllerStepDisposition.BLOCKED, False, False, "blocked"),
        (ControllerStepDisposition.COMPLETED, True, False, "authoritative_source_committed"),
        (ControllerStepDisposition.COMPLETED, True, True, "authoritative_source_committed"),
    ),
)
def test_terminal_tick_is_durably_resolved_without_self_wakeup(
    transaction, disposition, signed, independent, expected
) -> None:
    manifest = _manifest()
    tick = _tick(
        disposition,
        signed_kernel_command_committed=signed,
        independent_observation_admission_committed=independent,
    )
    harness = _Harness(
        manifest,
        _audited(manifest, _task(manifest, status=TaskStatus.SUCCEEDED, tick=tick)),
    )
    assert harness._process_one(harness.audited.delivery.delivery_sha256) == (
        "terminal",
        "2" * 64,
    )
    assert harness.resolutions[0]["disposition"].value == expected
    assert harness.successors == []


def test_invalid_success_and_cancelled_task_are_typed_dead_letters(transaction) -> None:
    manifest = _manifest()
    invalid = _Harness(
        manifest,
        _audited(
            manifest,
            _task(manifest, status=TaskStatus.SUCCEEDED, invalid_result=True),
        ),
    )
    assert invalid._process_one(invalid.audited.delivery.delivery_sha256)[0] == "dead_letter"
    assert invalid.resolutions[0]["dead_letter_reason"].value == "invalid_succeeded_result"

    cancelled = _Harness(
        manifest,
        _audited(manifest, _task(manifest, status=TaskStatus.CANCELLED)),
    )
    assert cancelled._process_one(cancelled.audited.delivery.delivery_sha256)[0] == "dead_letter"
    assert cancelled.resolutions[0]["dead_letter_reason"].value == "task_cancelled"


def test_resolution_is_rechecked_only_after_delivery_lock(transaction, monkeypatch) -> None:
    manifest = _manifest()
    tick = _tick(ControllerStepDisposition.AWAITING_AUTHORITY)
    harness = _Harness(
        manifest,
        _audited(manifest, _task(manifest, status=TaskStatus.SUCCEEDED, tick=tick)),
    )
    calls = []

    def audit(session, delivery_sha256):
        calls.append("locked")
        return harness.audited

    harness._audit_locked_delivery = audit

    def existing(*_args, **_kwargs):
        calls.append("resolution")
        return None

    monkeypatch.setattr(
        "aletheia.research_controller.redrive.get_controller_delivery_resolution",
        existing,
    )
    harness._process_one(harness.audited.delivery.delivery_sha256)
    assert calls[:2] == ["locked", "resolution"]

    task = harness.audited.tasks[0]
    resolution = ControllerDeliveryResolution(
        delivery_sha256=harness.audited.delivery.delivery_sha256,
        quest_id=harness.audited.delivery.quest_id,
        latest_attempt_sha256=harness.audited.attempts[0].attempt_sha256,
        exhausted_generation=0,
        max_delivery_generation=manifest.max_delivery_generation,
        terminal_task_id=task.task_id,
        terminal_task_status="succeeded",
        terminal_category="success",
        terminal_result_sha256=task.result_sha256,
        tick_receipt_sha256=tick.receipt_sha256,
        step_disposition="awaiting_authority",
        signed_kernel_command_committed=False,
        independent_observation_admission_committed=False,
        controller_manifest_sha256=manifest.manifest_sha256,
        disposition=ControllerDeliveryResolutionDisposition.AWAITING_AUTHORITY,
        resolved_at=NOW,
    )
    calls.clear()
    monkeypatch.setattr(
        "aletheia.research_controller.redrive.get_controller_delivery_resolution",
        lambda *_args, **_kwargs: ControllerDeliveryResolutionWrite.from_contract(resolution),
    )
    assert harness._process_one(harness.audited.delivery.delivery_sha256) == (
        "terminal",
        resolution.resolution_sha256,
    )
    assert calls == ["locked"]

    rebound = resolution.model_copy(
        update={"max_delivery_generation": manifest.max_delivery_generation - 1}
    )
    monkeypatch.setattr(
        "aletheia.research_controller.redrive.get_controller_delivery_resolution",
        lambda *_args, **_kwargs: ControllerDeliveryResolutionWrite.from_contract(rebound),
    )
    with pytest.raises(ControllerDeliveryReconciliationError, match="resolution was rebound"):
        harness._process_one(harness.audited.delivery.delivery_sha256)


def test_attempt_and_resolution_factories_roundtrip_utc_json() -> None:
    manifest = _manifest()
    audited = _audited(manifest, _task(manifest, status=TaskStatus.FAILED))
    attempt_write = ControllerDeliveryAttemptWrite.from_contract(audited.attempts[0])
    assert attempt_write.attempt_json["recorded_at"].endswith("Z")

    resolution = ControllerDeliveryResolution(
        delivery_sha256=audited.delivery.delivery_sha256,
        quest_id=audited.delivery.quest_id,
        latest_attempt_sha256=audited.attempts[0].attempt_sha256,
        exhausted_generation=0,
        max_delivery_generation=manifest.max_delivery_generation,
        terminal_task_id=audited.tasks[0].task_id,
        terminal_task_status="succeeded",
        terminal_category="success",
        terminal_result_sha256="a" * 64,
        tick_receipt_sha256="b" * 64,
        step_disposition="awaiting_authority",
        signed_kernel_command_committed=False,
        independent_observation_admission_committed=False,
        controller_manifest_sha256=manifest.manifest_sha256,
        disposition=ControllerDeliveryResolutionDisposition.AWAITING_AUTHORITY,
        resolved_at=NOW,
    )
    resolution_write = ControllerDeliveryResolutionWrite.from_contract(resolution)
    assert resolution_write.resolution_json["resolved_at"].endswith("Z")

    with pytest.raises(ValueError, match="atomic Kernel command"):
        ControllerDeliveryResolution.model_validate(
            {
                **resolution.model_dump(mode="python"),
                "step_disposition": "completed",
                "independent_observation_admission_committed": True,
                "disposition": "authoritative_source_committed",
            }
        )
