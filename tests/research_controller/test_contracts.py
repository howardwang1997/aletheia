from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from aletheia.jobs.contracts import RetryPolicy
from aletheia.research_controller.contracts import (
    CONTROLLER_TASK_TYPE,
    CompilationDisposition,
    ControllerRecoveryProjection,
    ControllerStep,
    ControllerWakeup,
    ControllerWakeupKind,
    ResearchControllerLaunchRequest,
    ResearchControllerManifest,
    controller_task_spec,
    plan_recovery_tick,
)

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)
SHA = "a" * 64


def _manifest() -> ResearchControllerManifest:
    return ResearchControllerManifest(
        controller_key="controller:local-v1",
        controller_code_sha256="1" * 64,
        controller_policy_sha256="2" * 64,
        capability_catalog_sha256="3" * 64,
        protocol_registry_policy_sha256="4" * 64,
        scientific_bridge_policy_sha256="5" * 64,
        worker_manifest_sha256="6" * 64,
        retry_policy=RetryPolicy(
            max_attempts=3,
            lease_seconds=60,
            heartbeat_interval_seconds=10,
        ),
        prepared_at=NOW,
    )


def _projection(**updates: object) -> ControllerRecoveryProjection:
    values: dict[str, object] = {
        "quest_id": "qst_" + "1" * 32,
        "action_sha256": "2" * 64,
        "scientific_slot_id": "sos_" + "3" * 32,
        "audited_stream_version": 7,
        "audited_tail_event_sha256": "4" * 64,
        "audited_snapshot_sha256": "5" * 64,
        "action_authorized": True,
        "compilation_disposition": CompilationDisposition.ACCEPTED,
        "scientific_execution_authorization_registered": False,
        "execution_terminal_observed": False,
        "validation_committed": False,
        "admission_committed": False,
        "observation_incorporated": False,
        "continuation_committed": False,
        "blocker_codes": (),
    }
    values.update(updates)
    return ControllerRecoveryProjection(**values)


def test_launch_and_manifest_identities_are_closed() -> None:
    manifest = _manifest()
    assert manifest.controller_id == f"rctl_{manifest.manifest_sha256[:32]}"
    request = ResearchControllerLaunchRequest(
        program_id="prg_" + "1" * 32,
        quest_id="qst_" + "2" * 32,
        idempotency_key="launch:one",
        expected_stream_version=1,
        expected_tail_event_sha256="3" * 64,
        expected_snapshot_sha256="4" * 64,
    )
    assert request.registration_id.startswith("rcr_")
    assert len(request.request_sha256) == 64
    with pytest.raises(ValidationError, match="policies must differ"):
        ResearchControllerManifest.model_validate(
            {
                **_manifest().model_dump(mode="python", exclude={"controller_id"}),
                "capability_catalog_sha256": _manifest().controller_policy_sha256,
            }
        )


def test_wakeup_shape_and_task_delivery_are_deterministic() -> None:
    request = ResearchControllerLaunchRequest(
        program_id="prg_" + "1" * 32,
        quest_id="qst_" + "2" * 32,
        idempotency_key="launch:one",
        expected_stream_version=1,
        expected_tail_event_sha256="3" * 64,
        expected_snapshot_sha256="4" * 64,
    )
    wakeup = ControllerWakeup(
        registration_id=request.registration_id,
        quest_id=request.quest_id,
        source_kind=ControllerWakeupKind.LAUNCH,
        source_key=request.registration_id,
        source_sha256=request.request_sha256,
    )
    first = controller_task_spec(manifest=_manifest(), wakeup=wakeup)
    second = controller_task_spec(manifest=_manifest(), wakeup=wakeup)
    assert first == second
    assert first.task_type == CONTROLLER_TASK_TYPE
    assert first.run_id is None
    assert first.concurrency_key == f"research-controller:{request.quest_id}"
    with pytest.raises(ValidationError, match="only a Kernel outbox"):
        ControllerWakeup.model_validate(
            {**wakeup.model_dump(mode="python"), "source_stream_version": 2}
        )


@pytest.mark.parametrize(
    ("updates", "expected"),
    (
        (
            {
                "action_authorized": False,
                "compilation_disposition": "missing",
                "scientific_slot_id": None,
            },
            ControllerStep.AWAIT_ACTION_AUTHORIZATION,
        ),
        (
            {
                "action_sha256": None,
                "scientific_slot_id": None,
                "action_authorized": False,
                "compilation_disposition": "missing",
            },
            ControllerStep.PROPOSE_ACTION,
        ),
        (
            {"compilation_disposition": "missing", "scientific_slot_id": None},
            ControllerStep.COMPILE_PROTOCOL,
        ),
        (
            {"compilation_disposition": "blocked", "scientific_slot_id": None},
            ControllerStep.PROPOSE_REDESIGN,
        ),
        ({}, ControllerStep.REGISTER_EXECUTION),
        ({"scientific_execution_authorization_registered": True}, ControllerStep.AWAIT_EXECUTION),
        (
            {
                "scientific_execution_authorization_registered": True,
                "execution_terminal_observed": True,
            },
            ControllerStep.COMMIT_VALIDATION,
        ),
        (
            {
                "scientific_execution_authorization_registered": True,
                "execution_terminal_observed": True,
                "validation_committed": True,
            },
            ControllerStep.COMMIT_ADMISSION,
        ),
        (
            {
                "scientific_execution_authorization_registered": True,
                "execution_terminal_observed": True,
                "validation_committed": True,
                "admission_committed": True,
                "observation_incorporated": True,
            },
            ControllerStep.DERIVE_CONTINUATION,
        ),
        (
            {
                "scientific_execution_authorization_registered": True,
                "execution_terminal_observed": True,
                "validation_committed": True,
                "admission_committed": True,
                "observation_incorporated": True,
                "continuation_committed": True,
            },
            ControllerStep.PROPOSE_FOLLOWUP,
        ),
        ({"blocker_codes": ("custody:unavailable",)}, ControllerStep.BLOCKED),
    ),
)
def test_recovery_planner_selects_one_receipt_derived_step(
    updates: dict[str, object], expected: ControllerStep
) -> None:
    projection = _projection(**updates)
    plan = plan_recovery_tick(projection)
    assert plan.step is expected
    assert plan.projection_sha256 == projection.projection_sha256
    assert plan.direct_scientific_mutation_allowed is False


def test_recovery_projection_rejects_impossible_receipt_chains() -> None:
    with pytest.raises(ValidationError, match="monotonic"):
        _projection(validation_committed=True)
    with pytest.raises(ValidationError, match="blocked compilation"):
        _projection(
            compilation_disposition="blocked",
            scientific_slot_id=None,
            scientific_execution_authorization_registered=True,
        )
    with pytest.raises(ValidationError, match="unique and canonical"):
        _projection(blocker_codes=("z", "a"))
    with pytest.raises(ValidationError, match="exact action"):
        _projection(action_sha256=None, action_authorized=True)
    with pytest.raises(ValidationError, match="authorized action"):
        _projection(action_authorized=False)
    with pytest.raises(ValidationError, match="commit atomically"):
        _projection(
            scientific_execution_authorization_registered=True,
            execution_terminal_observed=True,
            validation_committed=True,
            admission_committed=True,
        )
    with pytest.raises(ValidationError, match="commit atomically"):
        _projection(observation_incorporated=True)
    pre_execution = _projection(scientific_slot_id=None)
    assert pre_execution.compilation_disposition is CompilationDisposition.ACCEPTED
    assert plan_recovery_tick(pre_execution).step is ControllerStep.REGISTER_EXECUTION
