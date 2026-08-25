"""Production controller adapters for independent validation and atomic admission.

The worker only coordinates externally deployed services.  It never receives a database,
validator, admission, or Research Kernel signing key.  Every returned contract is rebound to the
exact recovery projection before it can become a controller step receipt.
"""

from __future__ import annotations

import re
from typing import Protocol

from aletheia.observations.coordinator import AtomicObservationAdmissionReceipt
from aletheia.observations.scientific_bridge import (
    AdmissionIssuanceChallenge,
    BridgeValidationDisposition,
    CommittedObservationValidationReceipt,
    ObservationAdmissionDecision,
    ObservationAdmissionDisposition,
    ObservationValidationReceipt,
    RawRunEnvelope,
    ValidationIssuanceChallenge,
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
    ControllerTickPlan,
    ControllerWakeup,
    plan_recovery_tick,
)
from aletheia.research_controller.service import (
    ControllerStepDisposition,
    ControllerStepReceipt,
)
from aletheia.research_controller.step_executor import (
    ControllerStepAdapterManifest,
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
    ControllerStepExecutionError,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RawRunEnvelopeSourcePort(Protocol):
    """Load one deterministic raw envelope from durable SEA and PR-4 terminal custody."""

    def load_raw_run(
        self,
        *,
        quest_id: str,
        action_sha256: str,
        scientific_slot_id: str,
    ) -> RawRunEnvelope: ...


class CommittedValidationSourcePort(Protocol):
    """Load the exact durable validation selected by one scientific slot."""

    def load_committed_validation(
        self,
        *,
        quest_id: str,
        action_sha256: str,
        scientific_slot_id: str,
    ) -> CommittedObservationValidationReceipt: ...


class DatabaseObservationBridgePort(Protocol):
    """External database-attestation service; its private key is outside the worker."""

    authority_binding: ControllerStepAuthorityBinding

    def issue_validation_challenge(
        self,
        *,
        raw_run: RawRunEnvelope,
        validation_campaign_sha256: str | None,
    ) -> ValidationChallengeRegistrationReceipt: ...

    def commit_validation(
        self,
        receipt: ObservationValidationReceipt,
    ) -> ValidationCommitReceipt: ...

    def issue_admission_challenge(
        self,
        committed_validation: CommittedObservationValidationReceipt,
    ) -> AdmissionChallengeRegistrationReceipt: ...


class IndependentObservationValidatorPort(Protocol):
    """External F9-v2 campaign/receipt authority; no signing material enters the worker."""

    authority_binding: ControllerStepAuthorityBinding

    def prepare_validation_campaign(
        self,
        *,
        raw_run: RawRunEnvelope,
    ) -> str | None: ...

    def issue_validation_receipt(
        self,
        *,
        raw_run: RawRunEnvelope,
        validation_campaign_sha256: str | None,
        issuance_challenge: ValidationIssuanceChallenge,
    ) -> ObservationValidationReceipt: ...


class IndependentObservationAdmissionPort(Protocol):
    """External admission-decision authority; a decision alone never fills a slot."""

    authority_binding: ControllerStepAuthorityBinding

    def issue_admission_decision(
        self,
        *,
        committed_validation: CommittedObservationValidationReceipt,
        issuance_challenge: AdmissionIssuanceChallenge,
    ) -> ObservationAdmissionDecision: ...


class AtomicObservationAdmissionPort(Protocol):
    """External coordinator for the admission row and signed Kernel transaction."""

    database_authority_binding: ControllerStepAuthorityBinding
    kernel_authority_binding: ControllerStepAuthorityBinding

    def commit_and_incorporate(
        self,
        decision: ObservationAdmissionDecision,
    ) -> AtomicObservationAdmissionReceipt: ...


def _freeze_manifest(
    manifest: ControllerStepAdapterManifest,
    *,
    step: ControllerStep,
) -> ControllerStepAdapterManifest:
    try:
        frozen = ControllerStepAdapterManifest.model_validate(manifest.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise TypeError("observation step adapter manifest is invalid") from exc
    if frozen.step is not step:
        raise ValueError("observation step adapter requires its exact controller step")
    return frozen


def _authority(
    manifest: ControllerStepAdapterManifest,
    role: ControllerStepAuthorityRole,
) -> ControllerStepAuthorityBinding:
    matches = tuple(item for item in manifest.authorities if item.role is role)
    if len(matches) != 1:
        raise ValueError("observation step manifest lacks one exact authority role")
    return matches[0]


def _require_port_binding(
    port: object,
    *,
    attribute: str,
    expected: ControllerStepAuthorityBinding,
    label: str,
) -> None:
    try:
        candidate = getattr(port, attribute)
        binding = ControllerStepAuthorityBinding.model_validate(candidate.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise TypeError(f"{label} lacks a frozen authority binding") from exc
    if candidate != binding or binding != expected:
        raise ValueError(f"{label} differs from the deployment-pinned authority")


def _validated_step_inputs(
    *,
    wakeup: ControllerWakeup,
    projection: ControllerRecoveryProjection,
    plan: ControllerTickPlan,
    step: ControllerStep,
) -> tuple[ControllerWakeup, ControllerRecoveryProjection, ControllerTickPlan]:
    try:
        wakeup = ControllerWakeup.model_validate(wakeup.model_dump(mode="python"))
        projection = ControllerRecoveryProjection.model_validate(
            projection.model_dump(mode="python")
        )
        plan = ControllerTickPlan.model_validate(plan.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ControllerStepExecutionError("observation step inputs are invalid") from exc
    if (
        projection.quest_id != wakeup.quest_id
        or plan_recovery_tick(projection) != plan
        or plan.step is not step
        or projection.action_sha256 is None
        or projection.scientific_slot_id is None
        or not projection.action_authorized
        or projection.compilation_disposition is not CompilationDisposition.ACCEPTED
        or not projection.scientific_execution_authorization_registered
        or not projection.execution_terminal_observed
        or projection.blocker_codes
    ):
        raise ControllerStepExecutionError("observation step received another controller state")
    return wakeup, projection, plan


def _raw_run_matches_projection(
    raw_run: RawRunEnvelope,
    projection: ControllerRecoveryProjection,
) -> None:
    message = raw_run.scientific_authorization.message
    binding = message.action_protocol_binding
    if (
        binding.action.quest_id != projection.quest_id
        or binding.action.object_sha256 != projection.action_sha256
        or message.scientific_slot_id != projection.scientific_slot_id
        or binding.compilation_request.protocol.world_model is None
    ):
        raise ControllerStepExecutionError(
            "raw run differs from the graph-scoped audited controller action"
        )


class IndependentObservationValidationStepAdapter:
    """Commit one externally validated receipt through a separate DB attestation service."""

    def __init__(
        self,
        *,
        manifest: ControllerStepAdapterManifest,
        raw_runs: RawRunEnvelopeSourcePort,
        database: DatabaseObservationBridgePort,
        validator: IndependentObservationValidatorPort,
    ) -> None:
        frozen = _freeze_manifest(manifest, step=ControllerStep.COMMIT_VALIDATION)
        database_binding = _authority(frozen, ControllerStepAuthorityRole.DATABASE_ATTESTATION)
        validator_binding = _authority(frozen, ControllerStepAuthorityRole.INDEPENDENT_VALIDATION)
        _require_port_binding(
            database,
            attribute="authority_binding",
            expected=database_binding,
            label="database observation service",
        )
        _require_port_binding(
            validator,
            attribute="authority_binding",
            expected=validator_binding,
            label="independent validation service",
        )
        for port, method, label in (
            (raw_runs, "load_raw_run", "raw-run source"),
            (database, "issue_validation_challenge", "database observation service"),
            (database, "commit_validation", "database observation service"),
            (validator, "prepare_validation_campaign", "independent validation service"),
            (validator, "issue_validation_receipt", "independent validation service"),
        ):
            if not callable(getattr(port, method, None)):
                raise TypeError(f"{label} does not implement {method}")
        self.manifest = frozen
        self._raw_runs = raw_runs
        self._database = database
        self._validator = validator
        self._database_binding = database_binding
        self._validator_binding = validator_binding

    def execute(
        self,
        *,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan: ControllerTickPlan,
    ) -> ControllerStepReceipt:
        try:
            wakeup, projection, plan = _validated_step_inputs(
                wakeup=wakeup,
                projection=projection,
                plan=plan,
                step=ControllerStep.COMMIT_VALIDATION,
            )
            if projection.validation_committed:
                raise ControllerStepExecutionError("validation is already durably committed")
            raw_run = RawRunEnvelope.model_validate(
                self._raw_runs.load_raw_run(
                    quest_id=projection.quest_id,
                    action_sha256=projection.action_sha256,
                    scientific_slot_id=projection.scientific_slot_id,
                ).model_dump(mode="python")
            )
            _raw_run_matches_projection(raw_run, projection)
            authorization = raw_run.scientific_authorization.message
            if (
                authorization.validator_principal_id != self._validator_binding.principal_id
                or authorization.validator_key_id != self._validator_binding.key_id
                or authorization.validator_authority_policy_sha256
                != self._validator_binding.policy_sha256
                or authorization.validator_manifest_sha256
                != self._validator_binding.service_manifest_sha256
            ):
                raise ControllerStepExecutionError(
                    "raw run changed the deployment-pinned validation authority"
                )
            campaign_sha256 = self._validator.prepare_validation_campaign(raw_run=raw_run)
            succeeded = raw_run.accepted_terminal_submission.disposition == "process_succeeded"
            if succeeded != (campaign_sha256 is not None) or (
                campaign_sha256 is not None and _SHA256.fullmatch(campaign_sha256) is None
            ):
                raise ControllerStepExecutionError(
                    "validation campaign selection differs from terminal engineering state"
                )
            challenge_receipt = self._database.issue_validation_challenge(
                raw_run=raw_run,
                validation_campaign_sha256=campaign_sha256,
            )
            challenge = ValidationIssuanceChallenge.model_validate(
                challenge_receipt.challenge.model_dump(mode="python")
            )
            challenge_message = challenge.message
            if (
                challenge_message.raw_run_sha256 != raw_run.raw_run_sha256
                or challenge_message.scientific_slot_id != projection.scientific_slot_id
                or challenge_message.validation_campaign_sha256 != campaign_sha256
                or challenge_message.issued_by_principal_id != self._database_binding.principal_id
                or challenge_message.issuance_key_id != self._database_binding.key_id
                or challenge_message.database_authority_policy_sha256
                != self._database_binding.policy_sha256
            ):
                raise ControllerStepExecutionError(
                    "database validation challenge rebound the controller source"
                )
            validation = ObservationValidationReceipt.model_validate(
                self._validator.issue_validation_receipt(
                    raw_run=raw_run,
                    validation_campaign_sha256=campaign_sha256,
                    issuance_challenge=challenge,
                ).model_dump(mode="python")
            )
            message = validation.message
            if (
                message.raw_run != raw_run
                or message.issuance_challenge != challenge
                or message.validated_by_principal_id != self._validator_binding.principal_id
                or message.validation_key_id != self._validator_binding.key_id
                or message.validator_authority_policy_sha256
                != self._validator_binding.policy_sha256
                or (message.validation_campaign_projection is None) != (campaign_sha256 is None)
                or (
                    message.validation_campaign_projection is not None
                    and (
                        message.validation_campaign_projection.campaign_sha256 != campaign_sha256
                        or message.validation_campaign_projection.validator_manifest_sha256
                        != self._validator_binding.service_manifest_sha256
                    )
                )
            ):
                raise ControllerStepExecutionError(
                    "independent validation receipt rebound its challenge or authority"
                )
            committed = self._database.commit_validation(validation).committed_validation
            committed = CommittedObservationValidationReceipt.model_validate(
                committed.model_dump(mode="python")
            )
            commit_message = committed.message
            if (
                commit_message.receipt != validation
                or commit_message.committed_by_principal_id != self._database_binding.principal_id
                or commit_message.commit_key_id != self._database_binding.key_id
                or commit_message.database_authority_policy_sha256
                != self._database_binding.policy_sha256
            ):
                raise ControllerStepExecutionError(
                    "committed validation rebound its exact receipt or DB authority"
                )
            return ControllerStepReceipt(
                wakeup_sha256=wakeup.wakeup_sha256,
                plan_sha256=plan.plan_sha256,
                disposition=ControllerStepDisposition.COMPLETED,
                result_artifact_sha256s=(committed.committed_receipt_sha256,),
                blocker_codes=(),
            )
        except ControllerStepExecutionError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed across external authority ports
            raise ControllerStepExecutionError(
                "independent observation validation failed closed"
            ) from exc


class AtomicObservationAdmissionStepAdapter:
    """Obtain an external admission decision and atomically incorporate only an admitted one."""

    def __init__(
        self,
        *,
        manifest: ControllerStepAdapterManifest,
        validations: CommittedValidationSourcePort,
        database: DatabaseObservationBridgePort,
        admission: IndependentObservationAdmissionPort,
        coordinator: AtomicObservationAdmissionPort,
    ) -> None:
        frozen = _freeze_manifest(manifest, step=ControllerStep.COMMIT_ADMISSION)
        database_binding = _authority(frozen, ControllerStepAuthorityRole.DATABASE_ATTESTATION)
        admission_binding = _authority(frozen, ControllerStepAuthorityRole.INDEPENDENT_ADMISSION)
        kernel_binding = _authority(frozen, ControllerStepAuthorityRole.KERNEL_COMMAND)
        _require_port_binding(
            database,
            attribute="authority_binding",
            expected=database_binding,
            label="database observation service",
        )
        _require_port_binding(
            admission,
            attribute="authority_binding",
            expected=admission_binding,
            label="independent admission service",
        )
        _require_port_binding(
            coordinator,
            attribute="database_authority_binding",
            expected=database_binding,
            label="atomic admission coordinator database authority",
        )
        _require_port_binding(
            coordinator,
            attribute="kernel_authority_binding",
            expected=kernel_binding,
            label="atomic admission coordinator Kernel authority",
        )
        for port, method, label in (
            (validations, "load_committed_validation", "validation source"),
            (database, "issue_admission_challenge", "database observation service"),
            (admission, "issue_admission_decision", "independent admission service"),
            (coordinator, "commit_and_incorporate", "atomic admission coordinator"),
        ):
            if not callable(getattr(port, method, None)):
                raise TypeError(f"{label} does not implement {method}")
        self.manifest = frozen
        self._validations = validations
        self._database = database
        self._admission = admission
        self._coordinator = coordinator
        self._database_binding = database_binding
        self._admission_binding = admission_binding
        self._kernel_binding = kernel_binding

    def execute(
        self,
        *,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan: ControllerTickPlan,
    ) -> ControllerStepReceipt:
        try:
            wakeup, projection, plan = _validated_step_inputs(
                wakeup=wakeup,
                projection=projection,
                plan=plan,
                step=ControllerStep.COMMIT_ADMISSION,
            )
            if not projection.validation_committed or projection.admission_committed:
                raise ControllerStepExecutionError(
                    "admission requires one committed validation and an empty slot"
                )
            committed_validation = CommittedObservationValidationReceipt.model_validate(
                self._validations.load_committed_validation(
                    quest_id=projection.quest_id,
                    action_sha256=projection.action_sha256,
                    scientific_slot_id=projection.scientific_slot_id,
                ).model_dump(mode="python")
            )
            validation = committed_validation.message.receipt.message
            _raw_run_matches_projection(validation.raw_run, projection)
            if (
                validation.disposition is not BridgeValidationDisposition.VALIDATED_CONFIRMATION
                or validation.scientific_observation_sha256 is None
                or validation.outcome is None
                or committed_validation.message.committed_by_principal_id
                != self._database_binding.principal_id
                or committed_validation.message.commit_key_id != self._database_binding.key_id
                or committed_validation.message.database_authority_policy_sha256
                != self._database_binding.policy_sha256
            ):
                raise ControllerStepExecutionError(
                    "admission source is not one exact independently confirmed validation"
                )
            challenge_receipt = self._database.issue_admission_challenge(committed_validation)
            challenge = AdmissionIssuanceChallenge.model_validate(
                challenge_receipt.challenge.model_dump(mode="python")
            )
            challenge_message = challenge.message
            if (
                challenge_message.scientific_slot_id != projection.scientific_slot_id
                or challenge_message.committed_validation_receipt_sha256
                != committed_validation.committed_receipt_sha256
                or challenge_message.validation_receipt_sha256
                != committed_validation.message.validation_receipt_sha256
                or challenge_message.issued_by_principal_id != self._database_binding.principal_id
                or challenge_message.issuance_key_id != self._database_binding.key_id
                or challenge_message.database_authority_policy_sha256
                != self._database_binding.policy_sha256
            ):
                raise ControllerStepExecutionError(
                    "database admission challenge rebound the committed validation"
                )
            decision = ObservationAdmissionDecision.model_validate(
                self._admission.issue_admission_decision(
                    committed_validation=committed_validation,
                    issuance_challenge=challenge,
                ).model_dump(mode="python")
            )
            decision_message = decision.message
            if (
                decision_message.committed_validation_receipt != committed_validation
                or decision_message.issuance_challenge != challenge
                or decision_message.decided_by_principal_id != self._admission_binding.principal_id
                or decision_message.decision_key_id != self._admission_binding.key_id
                or decision_message.admission_authority_policy_sha256
                != self._admission_binding.policy_sha256
            ):
                raise ControllerStepExecutionError(
                    "independent admission decision rebound its challenge or authority"
                )
            if decision_message.disposition is ObservationAdmissionDisposition.REJECTED:
                blockers = tuple(
                    sorted(
                        f"observation_admission:{item}" for item in decision_message.reason_codes
                    )
                )
                return ControllerStepReceipt(
                    wakeup_sha256=wakeup.wakeup_sha256,
                    plan_sha256=plan.plan_sha256,
                    disposition=ControllerStepDisposition.BLOCKED,
                    result_artifact_sha256s=(decision.decision_sha256,),
                    blocker_codes=blockers,
                )
            atomic = AtomicObservationAdmissionReceipt.model_validate(
                self._coordinator.commit_and_incorporate(decision).model_dump(mode="python")
            )
            if (
                atomic.committed_admission.message.decision != decision
                or atomic.incorporation_payload.scientific_slot_id != projection.scientific_slot_id
                or atomic.incorporation_payload.action_id
                != decision_message.committed_validation_receipt.message.receipt.message.raw_run.scientific_authorization.message.action_protocol_binding.action.action_id
                or atomic.kernel_receipt.quest_id != projection.quest_id
                or atomic.kernel_receipt.principal_id != self._kernel_binding.principal_id
                or atomic.kernel_receipt.authorization_policy_sha256
                != self._kernel_binding.policy_sha256
            ):
                raise ControllerStepExecutionError(
                    "atomic admission receipt rebound observation or Kernel authority"
                )
            return ControllerStepReceipt(
                wakeup_sha256=wakeup.wakeup_sha256,
                plan_sha256=plan.plan_sha256,
                disposition=ControllerStepDisposition.COMPLETED,
                result_artifact_sha256s=tuple(
                    sorted(
                        (
                            atomic.committed_admission.committed_admission_sha256,
                            atomic.kernel_receipt.result_event_sha256,
                        )
                    )
                ),
                blocker_codes=(),
                signed_kernel_command_committed=True,
                independent_observation_admission_committed=True,
            )
        except ControllerStepExecutionError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed across external authority ports
            raise ControllerStepExecutionError(
                "atomic observation admission failed closed"
            ) from exc


__all__ = [
    "AtomicObservationAdmissionPort",
    "AtomicObservationAdmissionStepAdapter",
    "CommittedValidationSourcePort",
    "DatabaseObservationBridgePort",
    "IndependentObservationAdmissionPort",
    "IndependentObservationValidationStepAdapter",
    "IndependentObservationValidatorPort",
    "RawRunEnvelopeSourcePort",
]
