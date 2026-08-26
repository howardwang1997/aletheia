from __future__ import annotations

import hashlib
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from aletheia.observations.coordinator import AtomicObservationAdmissionReceipt
from aletheia.observations.scientific_bridge import (
    ObservationAdmissionDisposition,
)
from aletheia.observations.service import (
    AdmissionChallengeRegistrationReceipt,
    ValidationChallengeRegistrationReceipt,
    ValidationCommitReceipt,
)
from aletheia.research_controller.contracts import (
    CompilationDisposition,
    ControllerRecoveryProjection,
    ControllerStep,
    ControllerWakeup,
    ControllerWakeupKind,
    plan_recovery_tick,
)
from aletheia.research_controller.observation_steps import (
    AtomicObservationAdmissionStepAdapter,
    IndependentObservationValidationStepAdapter,
)
from aletheia.research_controller.service import ControllerStepDisposition
from aletheia.research_controller.step_executor import (
    ControllerStepAdapterManifest,
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
    ControllerStepExecutionError,
)
from aletheia.research_kernel.schemas import ObservationIncorporatedPayload
from aletheia.research_store.store import ResearchCommandReceipt

_OBSERVATION_TESTS = Path(__file__).resolve().parents[1] / "observations"
_CONTROLLER_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_OBSERVATION_TESTS))
sys.path.insert(0, str(_CONTROLLER_TESTS))
from test_scientific_bridge import (  # noqa: E402
    _bridge_case,
    _commit_admission,
    _commit_validation,
    _issue_admission_decision,
    _validated_receipt,
)
from test_vertical_cut import (  # noqa: E402
    _f9_enriched_grouped_fixture,
    runtime_fixture_support,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _case(monkeypatch: pytest.MonkeyPatch):
    enriched = _f9_enriched_grouped_fixture()
    original = runtime_fixture_support.fixture_by_name

    def fixture_by_name(name: str):
        return enriched if name == "grouped_regression" else original(name)

    monkeypatch.setattr(runtime_fixture_support, "fixture_by_name", fixture_by_name)
    return _bridge_case()


def _binding(role, pin, service_manifest_sha256):
    return ControllerStepAuthorityBinding(
        role=role,
        principal_id=pin.principal_id,
        key_id=pin.key_id,
        policy_sha256=pin.policy_sha256,
        service_manifest_sha256=service_manifest_sha256,
        externally_deployed=True,
    )


def _kernel_binding():
    return ControllerStepAuthorityBinding(
        role=ControllerStepAuthorityRole.KERNEL_COMMAND,
        principal_id="principal:observation-kernel-authority",
        key_id=_digest("observation-kernel-key"),
        policy_sha256=_digest("observation-kernel-policy"),
        service_manifest_sha256=_digest("observation-kernel-service"),
        externally_deployed=True,
    )


def _manifest(case, step):
    database = _binding(
        ControllerStepAuthorityRole.DATABASE_ATTESTATION,
        case.database_pin,
        _digest("observation-database-service"),
    )
    if step is ControllerStep.COMMIT_VALIDATION:
        authorities = (
            database,
            _binding(
                ControllerStepAuthorityRole.INDEPENDENT_VALIDATION,
                case.validator_pin,
                case.authorization.message.validator_manifest_sha256,
            ),
        )
    else:
        authorities = (
            database,
            _binding(
                ControllerStepAuthorityRole.INDEPENDENT_ADMISSION,
                case.admission_pin,
                _digest("observation-admission-service"),
            ),
            _kernel_binding(),
        )
    return ControllerStepAdapterManifest(
        step=step,
        adapter_code_sha256=_digest(f"observation-adapter:{step.value}"),
        adapter_config_sha256=_digest(f"observation-config:{step.value}"),
        authorities=authorities,
        prepared_at=case.authorization.message.authorized_at,
    )


def _controller_state(case, *, admission: bool):
    binding = case.authorization.message.action_protocol_binding
    wakeup = ControllerWakeup(
        registration_id="rcr_" + "6" * 32,
        quest_id=binding.action.quest_id,
        source_kind=ControllerWakeupKind.EXECUTION_TERMINAL_OUTBOX,
        source_key="qto_" + "7" * 64,
        source_sha256="7" * 64,
        source_stream_version=None,
    )
    projection = ControllerRecoveryProjection(
        quest_id=wakeup.quest_id,
        action_sha256=binding.action.object_sha256,
        scientific_slot_id=case.authorization.message.scientific_slot_id,
        audited_stream_version=binding.action_authorized_event.sequence,
        audited_tail_event_sha256=binding.action_authorized_event.event_sha256,
        audited_snapshot_sha256=binding.authorized_graph_snapshot_sha256,
        action_authorized=True,
        compilation_disposition=CompilationDisposition.ACCEPTED,
        scientific_execution_authorization_registered=True,
        execution_terminal_observed=True,
        validation_committed=admission,
        admission_committed=False,
        observation_incorporated=False,
        continuation_committed=False,
        blocker_codes=(),
    )
    return wakeup, projection, plan_recovery_tick(projection)


class _RawRuns:
    def __init__(self, raw_run):
        self.raw_run = raw_run
        self.calls = []

    def load_raw_run(self, **scope):
        self.calls.append(scope)
        return self.raw_run


class _Database:
    def __init__(self, binding, *, validation, committed_validation, admission_challenge=None):
        self.authority_binding = binding
        self.validation = validation
        self.committed_validation = committed_validation
        self.admission_challenge = admission_challenge
        self.calls = []

    def issue_validation_challenge(self, *, raw_run, validation_campaign_sha256):
        self.calls.append(("validation_challenge", raw_run, validation_campaign_sha256))
        challenge = self.validation.message.issuance_challenge
        return ValidationChallengeRegistrationReceipt(
            challenge=challenge,
            recorded_at=challenge.message.issued_at,
        )

    def commit_validation(self, receipt):
        self.calls.append(("commit_validation", receipt))
        return ValidationCommitReceipt(
            committed_validation=self.committed_validation,
        )

    def issue_admission_challenge(self, committed_validation):
        self.calls.append(("admission_challenge", committed_validation))
        assert self.admission_challenge is not None
        return AdmissionChallengeRegistrationReceipt(
            challenge=self.admission_challenge,
            recorded_at=self.admission_challenge.message.issued_at,
        )


class _Validator:
    def __init__(self, binding, validation):
        self.authority_binding = binding
        self.validation = validation
        self.calls = []

    def prepare_validation_campaign(self, *, raw_run):
        self.calls.append(("prepare", raw_run))
        projection = self.validation.message.validation_campaign_projection
        return None if projection is None else projection.campaign_sha256

    def issue_validation_receipt(
        self,
        *,
        raw_run,
        validation_campaign_sha256,
        issuance_challenge,
    ):
        self.calls.append(("issue", raw_run, validation_campaign_sha256, issuance_challenge))
        return self.validation


class _Validations:
    def __init__(self, committed):
        self.committed = committed
        self.calls = []

    def load_committed_validation(self, **scope):
        self.calls.append(scope)
        return self.committed


class _Admission:
    def __init__(self, binding, decision):
        self.authority_binding = binding
        self.decision = decision
        self.calls = []

    def issue_admission_decision(self, *, committed_validation, issuance_challenge):
        self.calls.append((committed_validation, issuance_challenge))
        return self.decision


class _Coordinator:
    def __init__(self, database_binding, kernel_binding, receipt):
        self.database_authority_binding = database_binding
        self.kernel_authority_binding = kernel_binding
        self.receipt = receipt
        self.calls = []

    def commit_and_incorporate(self, decision):
        self.calls.append(decision)
        return self.receipt


def _atomic_receipt(case, decision, *, kernel_binding):
    committed = _commit_admission(case, decision)
    validation = decision.message.committed_validation_receipt.message.receipt.message
    authorization = validation.raw_run.scientific_authorization.message
    action_binding = authorization.action_protocol_binding
    protocol = action_binding.compilation_request.protocol
    payload = ObservationIncorporatedPayload(
        branch_id=protocol.graph_scope.branch_id,
        action_id=action_binding.action.action_id,
        scientific_slot_id=decision.message.scientific_slot_id,
        committed_admission_sha256=committed.committed_admission_sha256,
        scientific_observation_sha256=decision.message.admitted_observation_sha256,
        outcome=validation.outcome.value,
        source_world_model_sha256=protocol.world_model.world_model_sha256,
    )
    event_sha256 = _digest("observation-incorporated-event")
    kernel = ResearchCommandReceipt(
        command_id="rcm_" + "8" * 32,
        quest_id=action_binding.action.quest_id,
        scope_binding=protocol.graph_scope.scope_binding,
        idempotency_key=f"observation-admission:{decision.decision_sha256}",
        source_event_key=f"scientific-slot:{decision.message.scientific_slot_id}",
        command_sha256=_digest("observation-incorporated-command"),
        expected_stream_version=action_binding.action_authorized_event.sequence,
        expected_tail_event_sha256=action_binding.action_authorized_event.event_sha256,
        result_stream_version=action_binding.action_authorized_event.sequence + 1,
        result_event_sha256=event_sha256,
        result_event_id=f"evt_{event_sha256[:32]}",
        result_snapshot_sha256=_digest("observation-incorporated-snapshot"),
        outbox_id=f"rko_{event_sha256[:32]}",
        principal_id=kernel_binding.principal_id,
        authorization_trust_root_sha256=_digest("observation-kernel-trust-root"),
        authorization_policy_sha256=kernel_binding.policy_sha256,
        authorization_receipt_sha256=_digest("observation-kernel-auth-receipt"),
        committed_at=committed.message.committed_at + timedelta(seconds=1),
        created=True,
    )
    return AtomicObservationAdmissionReceipt(
        committed_admission=committed,
        incorporation_payload=payload,
        kernel_receipt=kernel,
        created=True,
    )


def test_validation_step_binds_raw_campaign_signer_challenge_and_db_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(monkeypatch)
    validation = _validated_receipt(case)
    committed = _commit_validation(case, validation)
    wakeup, projection, plan = _controller_state(case, admission=False)
    manifest = _manifest(case, ControllerStep.COMMIT_VALIDATION)
    database = _Database(
        manifest.authorities[0],
        validation=validation,
        committed_validation=committed,
    )
    validator = _Validator(manifest.authorities[1], validation)
    raw_runs = _RawRuns(validation.message.raw_run)
    adapter = IndependentObservationValidationStepAdapter(
        manifest=manifest,
        raw_runs=raw_runs,
        database=database,
        validator=validator,
    )

    receipt = adapter.execute(wakeup=wakeup, projection=projection, plan=plan)

    assert receipt.disposition is ControllerStepDisposition.COMPLETED
    assert receipt.result_artifact_sha256s == (committed.committed_receipt_sha256,)
    assert receipt.signed_kernel_command_committed is False
    assert receipt.independent_observation_admission_committed is False
    assert len(raw_runs.calls) == 1
    assert [item[0] for item in database.calls] == [
        "validation_challenge",
        "commit_validation",
    ]


def test_validation_step_rejects_rebound_raw_action_and_service_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(monkeypatch)
    validation = _validated_receipt(case)
    committed = _commit_validation(case, validation)
    wakeup, projection, plan = _controller_state(case, admission=False)
    manifest = _manifest(case, ControllerStep.COMMIT_VALIDATION)
    database = _Database(
        manifest.authorities[0],
        validation=validation,
        committed_validation=committed,
    )
    validator = _Validator(manifest.authorities[1], validation)
    rebound_projection = projection.model_copy(update={"action_sha256": "f" * 64})
    rebound_plan = plan_recovery_tick(rebound_projection)

    with pytest.raises(ControllerStepExecutionError, match="graph-scoped audited"):
        IndependentObservationValidationStepAdapter(
            manifest=manifest,
            raw_runs=_RawRuns(validation.message.raw_run),
            database=database,
            validator=validator,
        ).execute(wakeup=wakeup, projection=rebound_projection, plan=rebound_plan)

    validator.authority_binding = validator.authority_binding.model_copy(
        update={"service_manifest_sha256": _digest("rebound-validator-service")}
    )
    with pytest.raises(ValueError, match="deployment-pinned"):
        IndependentObservationValidationStepAdapter(
            manifest=manifest,
            raw_runs=_RawRuns(validation.message.raw_run),
            database=database,
            validator=validator,
        )


def test_admission_step_commits_independent_decision_and_kernel_event_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(monkeypatch)
    validation = _validated_receipt(case)
    decision, committed = _issue_admission_decision(
        case,
        receipt=validation,
        disposition=ObservationAdmissionDisposition.ADMITTED,
        reason_codes=(),
    )
    wakeup, projection, plan = _controller_state(case, admission=True)
    manifest = _manifest(case, ControllerStep.COMMIT_ADMISSION)
    database_binding, admission_binding, kernel_binding = manifest.authorities
    database = _Database(
        database_binding,
        validation=validation,
        committed_validation=committed,
        admission_challenge=decision.message.issuance_challenge,
    )
    atomic = _atomic_receipt(case, decision, kernel_binding=kernel_binding)
    coordinator = _Coordinator(database_binding, kernel_binding, atomic)
    adapter = AtomicObservationAdmissionStepAdapter(
        manifest=manifest,
        validations=_Validations(committed),
        database=database,
        admission=_Admission(admission_binding, decision),
        coordinator=coordinator,
    )

    receipt = adapter.execute(wakeup=wakeup, projection=projection, plan=plan)

    assert receipt.disposition is ControllerStepDisposition.COMPLETED
    assert receipt.signed_kernel_command_committed is True
    assert receipt.independent_observation_admission_committed is True
    assert receipt.result_artifact_sha256s == tuple(
        sorted(
            (
                atomic.committed_admission.committed_admission_sha256,
                atomic.kernel_receipt.result_event_sha256,
            )
        )
    )
    assert coordinator.calls == [decision]


def test_admission_step_preserves_signed_rejection_without_kernel_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(monkeypatch)
    validation = _validated_receipt(case)
    decision, committed = _issue_admission_decision(
        case,
        receipt=validation,
        disposition=ObservationAdmissionDisposition.REJECTED,
        reason_codes=("policy_requires_external_replication",),
    )
    wakeup, projection, plan = _controller_state(case, admission=True)
    manifest = _manifest(case, ControllerStep.COMMIT_ADMISSION)
    database_binding, admission_binding, kernel_binding = manifest.authorities
    database = _Database(
        database_binding,
        validation=validation,
        committed_validation=committed,
        admission_challenge=decision.message.issuance_challenge,
    )
    coordinator = _Coordinator(
        database_binding,
        kernel_binding,
        receipt=object(),
    )
    adapter = AtomicObservationAdmissionStepAdapter(
        manifest=manifest,
        validations=_Validations(committed),
        database=database,
        admission=_Admission(admission_binding, decision),
        coordinator=coordinator,
    )

    receipt = adapter.execute(wakeup=wakeup, projection=projection, plan=plan)

    assert receipt.disposition is ControllerStepDisposition.BLOCKED
    assert receipt.blocker_codes == ("observation_admission:policy_requires_external_replication",)
    assert receipt.result_artifact_sha256s == (decision.decision_sha256,)
    assert coordinator.calls == []


def test_admission_step_rejects_rebound_kernel_authority_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(monkeypatch)
    validation = _validated_receipt(case)
    decision, committed = _issue_admission_decision(
        case,
        receipt=validation,
        disposition=ObservationAdmissionDisposition.ADMITTED,
        reason_codes=(),
    )
    wakeup, projection, plan = _controller_state(case, admission=True)
    manifest = _manifest(case, ControllerStep.COMMIT_ADMISSION)
    database_binding, admission_binding, kernel_binding = manifest.authorities
    database = _Database(
        database_binding,
        validation=validation,
        committed_validation=committed,
        admission_challenge=decision.message.issuance_challenge,
    )
    atomic = _atomic_receipt(case, decision, kernel_binding=kernel_binding)
    rebound = atomic.model_copy(
        update={
            "kernel_receipt": atomic.kernel_receipt.model_copy(
                update={"principal_id": "principal:rebound-kernel-authority"}
            )
        }
    )
    adapter = AtomicObservationAdmissionStepAdapter(
        manifest=manifest,
        validations=_Validations(committed),
        database=database,
        admission=_Admission(admission_binding, decision),
        coordinator=_Coordinator(database_binding, kernel_binding, rebound),
    )

    with pytest.raises(ControllerStepExecutionError, match="Kernel authority"):
        adapter.execute(wakeup=wakeup, projection=projection, plan=plan)
