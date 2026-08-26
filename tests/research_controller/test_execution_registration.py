from __future__ import annotations

import hashlib
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from aletheia.observations.execution_registration import (
    AtomicScientificExecutionRegistrationReceipt,
)
from aletheia.research_controller.contracts import (
    CompilationDisposition,
    ControllerRecoveryProjection,
    ControllerStep,
    ControllerWakeup,
    ControllerWakeupKind,
    plan_recovery_tick,
)
from aletheia.research_controller.execution_registration import (
    QualifiedExecutionRegistrationStepAdapter,
)
from aletheia.research_controller.service import ControllerStepDisposition
from aletheia.research_controller.step_executor import (
    ControllerStepAdapterManifest,
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
    ControllerStepExecutionError,
)

_OBSERVATION_TESTS = Path(__file__).resolve().parents[1] / "observations"
sys.path.insert(0, str(_OBSERVATION_TESTS))
from test_scientific_bridge import _bridge_case  # noqa: E402


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _case():
    bridge = _bridge_case()
    binding = bridge.authorization.message.action_protocol_binding
    wakeup = ControllerWakeup(
        registration_id="rcr_" + "1" * 32,
        quest_id=binding.action.quest_id,
        source_kind=ControllerWakeupKind.KERNEL_OUTBOX,
        source_key=f"rko_{binding.action_authorized_event.event_sha256[:32]}",
        source_sha256=binding.action_authorized_event.event_sha256,
        source_stream_version=binding.action_authorized_event.sequence,
    )
    projection = ControllerRecoveryProjection(
        quest_id=wakeup.quest_id,
        action_sha256=binding.action.object_sha256,
        scientific_slot_id=None,
        audited_stream_version=binding.action_authorized_event.sequence,
        audited_tail_event_sha256=binding.action_authorized_event.event_sha256,
        audited_snapshot_sha256=binding.authorized_graph_snapshot_sha256,
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
    plan = plan_recovery_tick(projection)
    assert plan.step is ControllerStep.REGISTER_EXECUTION
    pin = bridge.execution_pin
    manifest = ControllerStepAdapterManifest(
        step=ControllerStep.REGISTER_EXECUTION,
        adapter_code_sha256=_digest("qualified-execution-registration-adapter"),
        adapter_config_sha256=_digest("qualified-execution-registration-config"),
        authorities=(
            ControllerStepAuthorityBinding(
                role=ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION,
                principal_id=pin.principal_id,
                key_id=pin.key_id,
                policy_sha256=pin.policy_sha256,
                service_manifest_sha256=_digest("external-sea-issuer-service"),
                externally_deployed=True,
            ),
        ),
        prepared_at=bridge.authorization.message.authorized_at,
    )
    return bridge, wakeup, projection, plan, manifest


class _Issuer:
    def __init__(self, authorization) -> None:
        self.authorization = authorization
        self.calls = []

    def issue_scientific_execution_authorization(self, **scope):
        self.calls.append(scope)
        return self.authorization


class _Registrar:
    def __init__(self, authorization) -> None:
        message = authorization.message
        binding = message.action_protocol_binding
        intent = message.qualification_bundle.intent
        registered_at = message.authorized_at + timedelta(seconds=1)
        self.receipt = AtomicScientificExecutionRegistrationReceipt(
            authorization_sha256=authorization.authorization_sha256,
            quest_id=binding.action.quest_id,
            scientific_slot_id=message.scientific_slot_id,
            action_sha256=binding.action.object_sha256,
            execution_id=intent.execution_id,
            attempt_id=intent.infrastructure_attempt.infrastructure_attempt_id,
            qualification_bundle_sha256=message.qualification_bundle.bundle_sha256,
            qualification_grant_sha256=message.qualification_grant.grant_sha256,
            registered_at=registered_at,
            qualification_admission_sha256="a" * 64,
            resource_reservation_sha256="b" * 64,
            reserved_at=registered_at + timedelta(seconds=1),
        )
        self.calls = []

    def register_and_reserve(self, authorization):
        self.calls.append(authorization)
        return self.receipt


def test_register_execution_step_uses_external_sea_and_atomic_registrar() -> None:
    bridge, wakeup, projection, plan, manifest = _case()
    issuer = _Issuer(bridge.authorization)
    registrar = _Registrar(bridge.authorization)
    adapter = QualifiedExecutionRegistrationStepAdapter(
        manifest=manifest,
        issuer=issuer,
        registrar=registrar,
    )

    receipt = adapter.execute(wakeup=wakeup, projection=projection, plan=plan)

    assert receipt.disposition is ControllerStepDisposition.COMPLETED
    assert receipt.result_artifact_sha256s == (registrar.receipt.receipt_sha256,)
    assert receipt.signed_kernel_command_committed is False
    assert receipt.independent_observation_admission_committed is False
    assert len(issuer.calls) == len(registrar.calls) == 1


def test_register_execution_step_rejects_rebound_audited_action() -> None:
    bridge, wakeup, projection, _plan, manifest = _case()
    projection = projection.model_copy(update={"action_sha256": "f" * 64})
    plan = plan_recovery_tick(projection)
    adapter = QualifiedExecutionRegistrationStepAdapter(
        manifest=manifest,
        issuer=_Issuer(bridge.authorization),
        registrar=_Registrar(bridge.authorization),
    )

    with pytest.raises(ControllerStepExecutionError, match="audited controller state"):
        adapter.execute(wakeup=wakeup, projection=projection, plan=plan)
