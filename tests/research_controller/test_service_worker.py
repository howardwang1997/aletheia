from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from aletheia.jobs.contracts import RetryPolicy, TaskSnapshot, TaskStatus
from aletheia.research_controller.contracts import (
    CompilationDisposition,
    ControllerRecoveryProjection,
    ControllerStep,
    ControllerTickPlan,
    ControllerWakeup,
    ControllerWakeupKind,
    ResearchControllerManifest,
    controller_task_spec,
)
from aletheia.research_controller.service import (
    ControllerStepDisposition,
    ControllerStepReceipt,
    ControllerTickReceipt,
    ResearchControllerService,
)
from aletheia.research_controller.worker import research_controller_task_handler
from aletheia.research_controller_runtime import research_controller_durable_worker

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def _manifest() -> ResearchControllerManifest:
    return ResearchControllerManifest(
        controller_key="controller:local-v1",
        controller_code_sha256="1" * 64,
        controller_policy_sha256="2" * 64,
        capability_catalog_sha256="3" * 64,
        protocol_registry_policy_sha256="4" * 64,
        scientific_bridge_policy_sha256="5" * 64,
        worker_manifest_sha256="6" * 64,
        retry_policy=RetryPolicy(max_attempts=3, lease_seconds=60, heartbeat_interval_seconds=10),
        prepared_at=NOW,
    )


def _wakeup() -> ControllerWakeup:
    return ControllerWakeup(
        registration_id="rcr_" + "1" * 32,
        quest_id="qst_" + "2" * 32,
        source_kind=ControllerWakeupKind.LAUNCH,
        source_key="launch:one",
        source_sha256="3" * 64,
    )


def _projection() -> ControllerRecoveryProjection:
    return ControllerRecoveryProjection(
        quest_id=_wakeup().quest_id,
        action_sha256="4" * 64,
        scientific_slot_id="sos_" + "5" * 32,
        audited_stream_version=3,
        audited_tail_event_sha256="6" * 64,
        audited_snapshot_sha256="7" * 64,
        action_authorized=True,
        compilation_disposition=CompilationDisposition.ACCEPTED,
        scientific_execution_authorization_registered=False,
        execution_terminal_observed=False,
        validation_committed=False,
        admission_committed=False,
        observation_incorporated=False,
        continuation_committed=False,
        blocker_codes=(),
    )


class _Recovery:
    calls = 0

    def load(self, wakeup):
        self.calls += 1
        return _projection()


class _Executor:
    calls = 0

    def execute(self, *, wakeup, plan):
        self.calls += 1
        return ControllerStepReceipt(
            wakeup_sha256=wakeup.wakeup_sha256,
            plan_sha256=plan.plan_sha256,
            disposition=ControllerStepDisposition.AWAITING_AUTHORITY,
            result_artifact_sha256s=(),
            blocker_codes=(),
        )


def _snapshot(task_spec) -> TaskSnapshot:
    return TaskSnapshot(
        task_id=task_spec.task_id,
        task_type=task_spec.task_type,
        inputs_sha256=task_spec.inputs_sha256,
        inputs=task_spec.inputs,
        dependency_ids=(),
        owner=task_spec.owner,
        run_id=task_spec.run_id,
        idempotency_key=task_spec.idempotency_key,
        concurrency_key=task_spec.concurrency_key,
        request_sha256=task_spec.request_sha256,
        retry_policy=task_spec.retry_policy,
        priority=task_spec.priority,
        status=TaskStatus.LEASED,
        attempt_count=1,
        state_version=2,
        available_at=NOW,
        active_attempt_id="attempt-controller-one",
        lease_owner="worker:controller-one",
        lease_expires_at=NOW + timedelta(minutes=1),
        result_artifact_id=None,
        result_sha256=None,
        result=None,
        terminal_category=None,
        terminal_detail_sha256=None,
        created_at=NOW,
        updated_at=NOW,
        completed_at=None,
    )


def test_handler_rebuilds_every_tick_without_memory_or_legacy_run() -> None:
    recovery = _Recovery()
    executor = _Executor()
    service = ResearchControllerService(recovery=recovery, executor=executor)
    handler = research_controller_task_handler(manifest=_manifest(), service=service)
    spec = controller_task_spec(manifest=_manifest(), wakeup=_wakeup())

    first = handler(_snapshot(spec))
    second = handler(_snapshot(spec))

    assert first == second
    assert recovery.calls == 2
    assert executor.calls == 2
    assert first.result["step_receipt"]["legacy_optimize_used"] is False
    assert spec.run_id is None


def test_step_receipt_rejects_impossible_authority_claims() -> None:
    plan = ControllerTickPlan(
        projection_sha256="8" * 64,
        step=ControllerStep.COMPILE_PROTOCOL,
        audited_stream_version=3,
        audited_tail_event_sha256="9" * 64,
        audited_snapshot_sha256="a" * 64,
        blocker_codes=(),
    )
    common = {
        "wakeup_sha256": _wakeup().wakeup_sha256,
        "plan_sha256": plan.plan_sha256,
        "result_artifact_sha256s": (),
        "blocker_codes": (),
    }
    with pytest.raises(ValidationError, match="completed controller step"):
        ControllerStepReceipt(
            **common,
            disposition=ControllerStepDisposition.AWAITING_AUTHORITY,
            signed_kernel_command_committed=True,
        )
    with pytest.raises(ValidationError, match="atomic Kernel command"):
        ControllerStepReceipt(
            **common,
            disposition=ControllerStepDisposition.COMPLETED,
            independent_observation_admission_committed=True,
        )


def test_completed_admission_receipt_requires_both_atomic_commits() -> None:
    plan = ControllerTickPlan(
        projection_sha256="8" * 64,
        step=ControllerStep.COMMIT_ADMISSION,
        audited_stream_version=3,
        audited_tail_event_sha256="9" * 64,
        audited_snapshot_sha256="a" * 64,
        blocker_codes=(),
    )
    common = {
        "wakeup_sha256": _wakeup().wakeup_sha256,
        "recovery_projection_sha256": plan.projection_sha256,
        "plan": plan,
    }
    incomplete = ControllerStepReceipt(
        wakeup_sha256=_wakeup().wakeup_sha256,
        plan_sha256=plan.plan_sha256,
        disposition=ControllerStepDisposition.COMPLETED,
        result_artifact_sha256s=(),
        blocker_codes=(),
    )
    with pytest.raises(ValidationError, match="atomically commit"):
        ControllerTickReceipt(**common, step_receipt=incomplete)

    complete = incomplete.model_copy(
        update={
            "signed_kernel_command_committed": True,
            "independent_observation_admission_committed": True,
        }
    )
    receipt = ControllerTickReceipt(**common, step_receipt=complete)
    assert receipt.step_receipt.signed_kernel_command_committed is True
    assert receipt.step_receipt.independent_observation_admission_committed is True


def test_only_admission_plan_may_claim_independent_admission() -> None:
    plan = ControllerTickPlan(
        projection_sha256="8" * 64,
        step=ControllerStep.COMPILE_PROTOCOL,
        audited_stream_version=3,
        audited_tail_event_sha256="9" * 64,
        audited_snapshot_sha256="a" * 64,
        blocker_codes=(),
    )
    step = ControllerStepReceipt(
        wakeup_sha256=_wakeup().wakeup_sha256,
        plan_sha256=plan.plan_sha256,
        disposition=ControllerStepDisposition.COMPLETED,
        result_artifact_sha256s=(),
        blocker_codes=(),
        signed_kernel_command_committed=True,
        independent_observation_admission_committed=True,
    )
    with pytest.raises(ValidationError, match="only the admission step"):
        ControllerTickReceipt(
            wakeup_sha256=_wakeup().wakeup_sha256,
            recovery_projection_sha256=plan.projection_sha256,
            plan=plan,
            step_receipt=step,
        )


def test_handler_accepts_exact_redrive_generation_and_rejects_rebound_chain() -> None:
    service = ResearchControllerService(recovery=_Recovery(), executor=_Executor())
    manifest = _manifest()
    spec = controller_task_spec(
        manifest=manifest,
        wakeup=_wakeup(),
        delivery_sha256="d" * 64,
        delivery_generation=1,
        supersedes_task_id="task-rctl-" + "e" * 32,
    )
    handler = research_controller_task_handler(manifest=manifest, service=service)

    result = handler(_snapshot(spec))
    assert result.result["wakeup_sha256"] == _wakeup().wakeup_sha256

    rebound_inputs = {
        **spec.inputs,
        "supersedes_task_id": "task-rctl-" + "f" * 32,
    }
    with pytest.raises(ValueError, match="frozen deterministic envelope"):
        handler(_snapshot(spec).model_copy(update={"inputs": rebound_inputs}))


def test_handler_rejects_task_from_another_deployment_manifest() -> None:
    service = ResearchControllerService(recovery=_Recovery(), executor=_Executor())
    spec = controller_task_spec(manifest=_manifest(), wakeup=_wakeup())
    changed = _manifest().model_copy(update={"controller_code_sha256": "f" * 64})
    foreign_handler = research_controller_task_handler(manifest=changed, service=service)
    with pytest.raises(ValueError, match="deployment-pinned manifest"):
        foreign_handler(_snapshot(spec))


def test_handler_rejects_legacy_run_binding() -> None:
    service = ResearchControllerService(recovery=_Recovery(), executor=_Executor())
    handler = research_controller_task_handler(manifest=_manifest(), service=service)
    spec = controller_task_spec(manifest=_manifest(), wakeup=_wakeup())
    snapshot = _snapshot(spec).model_copy(update={"run_id": "0" * 32})
    with pytest.raises(ValueError, match="cannot synthesize a legacy Run"):
        handler(snapshot)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"request_sha256": "f" * 64}, "frozen deterministic envelope"),
        (
            {
                "retry_policy": RetryPolicy(
                    max_attempts=4,
                    lease_seconds=60,
                    heartbeat_interval_seconds=10,
                )
            },
            "frozen deterministic envelope",
        ),
        ({"status": TaskStatus.QUEUED, "active_attempt_id": None}, "active durable-task lease"),
    ],
)
def test_handler_rejects_rebound_envelope_or_absent_lease(changes, message) -> None:
    service = ResearchControllerService(recovery=_Recovery(), executor=_Executor())
    handler = research_controller_task_handler(manifest=_manifest(), service=service)
    spec = controller_task_spec(manifest=_manifest(), wakeup=_wakeup())

    with pytest.raises(ValueError, match=message):
        handler(_snapshot(spec).model_copy(update=changes))


def test_durable_worker_composition_pins_worker_and_task_manifests() -> None:
    manifest = _manifest()
    service = ResearchControllerService(recovery=_Recovery(), executor=_Executor())

    worker = research_controller_durable_worker(manifest=manifest, service=service)

    assert worker.worker_id == f"research-controller:{manifest.controller_id}"
    assert worker.worker_manifest_sha256 == manifest.worker_manifest_sha256
    assert tuple(worker.handlers) == ("research.controller.v1",)
