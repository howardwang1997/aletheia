"""Restart-safe, given-protocol ARL-1 replicate campaign composition.

This service is deliberately narrower than the autonomous research controller.  A caller supplies
one already-authorized, already-compiled protocol and an exhaustive set of signed replicate
authorizations.  The service atomically preregisters every slot, consumes durable execution and
validation sources, admits one primary observation only after every replicate agrees, and derives
the exact report and archive from authoritative timestamps.  It owns no signing key.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, model_validator

from aletheia.arl1 import (
    ARL1AllAttemptsManifestV1,
    ARL1AttemptEvidenceRefV1,
    ARL1Outcome,
    ARL1ProtocolCampaignEvidenceV1,
    ARL1ReplicateExecutionEvidenceV1,
    ARL1ReproductionReceiptV1,
    build_arl1_protocol_executor_report,
)
from aletheia.arl1_verifier import (
    LocalARL1EvidenceArchive,
    build_protocol_campaign_archive_manifest,
    retain_protocol_campaign_archive,
)
from aletheia.observations.coordinator import AtomicObservationAdmissionReceipt
from aletheia.observations.execution_registration import (
    AtomicScientificExecutionCampaignRegistrationReceipt,
)
from aletheia.observations.scientific_bridge import (
    AdmissionIssuanceChallenge,
    BridgeValidationDisposition,
    CommittedObservationValidationReceipt,
    ObservationAdmissionDecision,
    ObservationAdmissionDisposition,
    ObservationValidationReceipt,
    RawRunEnvelope,
    ScientificExecutionAuthorization,
    ScientificObservationOutcome,
    ValidationIssuanceChallenge,
    VerifiedRawRunCustodyProjection,
)
from aletheia.observations.service import (
    AdmissionChallengeRegistrationReceipt,
    ValidationChallengeRegistrationReceipt,
    ValidationCommitReceipt,
)
from aletheia.protocols.compiler import ProtocolCompilationRequest, verify_compilation
from aletheia.protocols.schemas import ProtocolCompilationResult
from aletheia.research_controller.step_executor import (
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
)
from aletheia.research_kernel.schemas import (
    EventType,
    KernelModel,
    ObservationIncorporatedPayload,
    ResearchEvent,
    canonical_sha256,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SYMBOLIC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$"
_REQUEST_ID_PATTERN = r"^arl1q_[0-9a-f]{32}$"


class ARL1ProtocolCampaignError(RuntimeError):
    """The frozen campaign could not reach one exact reproducible result."""


class ARL1ProtocolCampaignPending(ARL1ProtocolCampaignError):
    """One preregistered replicate is still awaiting exact terminal material."""

    def __init__(
        self,
        *,
        scientific_slot_id: str,
        pending_code: str,
        retry_after_milliseconds: int,
    ) -> None:
        self.scientific_slot_id = scientific_slot_id
        self.pending_code = pending_code
        self.retry_after_milliseconds = retry_after_milliseconds
        super().__init__(f"{scientific_slot_id}:{pending_code}")


class ARL1ProtocolCampaignRequestV1(KernelModel):
    """Caller-supplied protocol and exhaustive signed replicate authority set."""

    schema_name: Literal["aletheia.arl1_protocol_campaign_request"] = (
        "aletheia.arl1_protocol_campaign_request"
    )
    schema_version: Literal[1] = 1
    request_id: str | None = Field(default=None, pattern=_REQUEST_ID_PATTERN)
    domain_scope: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    modality_scope: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    compilation_request: ProtocolCompilationRequest
    compilation_result: ProtocolCompilationResult
    work_order_node_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    authorizations: tuple[ScientificExecutionAuthorization, ...] = Field(
        min_length=2,
        max_length=100,
    )
    primary_scientific_slot_id: str = Field(pattern=r"^sos_[0-9a-f]{32}$")
    requested_at: AwareDatetime
    given_question_and_protocol_only: Literal[True] = True
    autonomous_research_design_allowed: Literal[False] = False
    synthetic_evidence_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _request_is_exact(self) -> "ARL1ProtocolCampaignRequestV1":
        try:
            verify_compilation(self.compilation_request, self.compilation_result)
        except (TypeError, ValueError) as exc:
            raise ValueError("ARL-1 campaign request compilation is invalid") from exc
        result = self.compilation_result
        work_order = result.work_order
        if work_order is None or not result.report.accepted:
            raise ValueError("ARL-1 campaign requires one accepted WorkOrder")
        nodes = tuple(item for item in work_order.nodes if item.node_id == self.work_order_node_id)
        if len(nodes) != 1:
            raise ValueError("ARL-1 campaign node does not resolve exactly once")
        node = nodes[0]
        if node.scientific_replicate_count != len(self.authorizations):
            raise ValueError("ARL-1 campaign authorization count differs from WorkOrder")
        slots = tuple(
            item.message.action_protocol_binding.replicate_slot for item in self.authorizations
        )
        first_message = self.authorizations[0].message
        first_binding = first_message.action_protocol_binding
        if (
            tuple(item.slot_index for item in slots)
            != tuple(range(1, len(self.authorizations) + 1))
            or any(item.slot_count != len(self.authorizations) for item in slots)
            or self.primary_scientific_slot_id
            not in {item.message.scientific_slot_id for item in self.authorizations}
            or not first_message.authorized_at <= self.requested_at < first_message.expires_at
        ):
            raise ValueError("ARL-1 campaign slot coverage or request window differs")
        for authorization in self.authorizations:
            message = authorization.message
            binding = message.action_protocol_binding
            if (
                binding.compilation_request != self.compilation_request
                or binding.compilation_result != self.compilation_result
                or binding.work_order_node != node
                or binding.action != first_binding.action
                or binding.action_proposed_event != first_binding.action_proposed_event
                or binding.action_authorized_event != first_binding.action_authorized_event
                or binding.authorized_graph_snapshot_sha256
                != first_binding.authorized_graph_snapshot_sha256
                or message.authorized_at != first_message.authorized_at
                or message.expires_at != first_message.expires_at
                or message.observation_admission_deadline
                != first_message.observation_admission_deadline
                or message.validator_manifest_sha256 != first_message.validator_manifest_sha256
                or message.observation_validation_policy_sha256
                != first_message.observation_validation_policy_sha256
                or message.admission_policy != first_message.admission_policy
                or message.execution_authority_policy_sha256
                != first_message.execution_authority_policy_sha256
                or message.validator_authority_policy_sha256
                != first_message.validator_authority_policy_sha256
                or message.admission_authority_policy_sha256
                != first_message.admission_authority_policy_sha256
            ):
                raise ValueError("ARL-1 campaign authorizations do not share one frozen protocol")
        identities = (
            tuple(item.authorization_sha256 for item in self.authorizations),
            tuple(item.message.scientific_slot_id for item in self.authorizations),
            tuple(
                item.message.qualification_bundle.intent.execution_id
                for item in self.authorizations
            ),
            tuple(
                item.message.qualification_bundle.intent.infrastructure_attempt.infrastructure_attempt_id
                for item in self.authorizations
            ),
        )
        if any(len(set(values)) != len(values) for values in identities):
            raise ValueError("ARL-1 campaign repeats an execution authority identity")
        expected = f"arl1q_{self.identity_sha256[:32]}"
        if self.request_id is not None and self.request_id != expected:
            raise ValueError("ARL-1 campaign request id differs from its contents")
        object.__setattr__(self, "request_id", expected)
        return self

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"request_id"}))

    @property
    def request_sha256(self) -> str:
        return canonical_sha256(self)


class ARL1ProtocolCampaignRunReceiptV1(KernelModel):
    schema_name: Literal["aletheia.arl1_protocol_campaign_run_receipt"] = (
        "aletheia.arl1_protocol_campaign_run_receipt"
    )
    schema_version: Literal[1] = 1
    request_id: str = Field(pattern=_REQUEST_ID_PATTERN)
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    campaign: ARL1ProtocolCampaignEvidenceV1
    completed_at: AwareDatetime
    restart_replay_uses_durable_sources_only: Literal[True] = True
    exact_retry_stable: Literal[True] = True
    scientific_validity_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _receipt_is_exact(self) -> "ARL1ProtocolCampaignRunReceiptV1":
        if self.completed_at != self.campaign.report.reported_at:
            raise ValueError("ARL-1 run receipt completion differs from deterministic report")
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


class AtomicCampaignRegistrarPort(Protocol):
    def register_and_reserve_campaign(
        self,
        authorizations: tuple[ScientificExecutionAuthorization, ...],
    ) -> AtomicScientificExecutionCampaignRegistrationReceipt: ...


class RawRunSourcePort(Protocol):
    def load_raw_run(
        self,
        *,
        quest_id: str,
        action_sha256: str,
        scientific_slot_id: str,
    ) -> RawRunEnvelope: ...


class RawRunCustodyPort(Protocol):
    def verify_raw_run_custody(
        self,
        *,
        raw_run: RawRunEnvelope,
        observed_at: datetime,
    ) -> VerifiedRawRunCustodyProjection: ...


class ReplicateValidationPort(Protocol):
    def commit_or_load_validation(
        self,
        *,
        raw_run: RawRunEnvelope,
    ) -> CommittedObservationValidationReceipt: ...


class PrimaryAdmissionPort(Protocol):
    def commit_or_load_admission(
        self,
        *,
        committed_validation: CommittedObservationValidationReceipt,
    ) -> AtomicObservationAdmissionReceipt: ...


class KernelAuditPort(Protocol):
    def audit(self, quest_id: str, *, expected_scope_binding: object | None = None) -> object: ...


class DatabaseObservationBridgePort(Protocol):
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


class IndependentValidatorPort(Protocol):
    authority_binding: ControllerStepAuthorityBinding

    def prepare_validation_campaign(self, *, raw_run: RawRunEnvelope) -> str | None: ...

    def issue_validation_receipt(
        self,
        *,
        raw_run: RawRunEnvelope,
        validation_campaign_sha256: str | None,
        issuance_challenge: ValidationIssuanceChallenge,
    ) -> ObservationValidationReceipt: ...


class IndependentAdmissionPort(Protocol):
    authority_binding: ControllerStepAuthorityBinding

    def issue_admission_decision(
        self,
        *,
        committed_validation: CommittedObservationValidationReceipt,
        issuance_challenge: AdmissionIssuanceChallenge,
    ) -> ObservationAdmissionDecision: ...


class AtomicAdmissionCoordinatorPort(Protocol):
    database_authority_binding: ControllerStepAuthorityBinding
    admission_authority_binding: ControllerStepAuthorityBinding
    kernel_authority_binding: ControllerStepAuthorityBinding

    def commit_and_incorporate(
        self,
        decision: ObservationAdmissionDecision,
    ) -> AtomicObservationAdmissionReceipt: ...


def _port_binding(
    port: object,
    *,
    attribute: str,
    role: ControllerStepAuthorityRole,
    label: str,
) -> ControllerStepAuthorityBinding:
    try:
        candidate = getattr(port, attribute)
        binding = ControllerStepAuthorityBinding.model_validate(candidate.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise TypeError(f"{label} lacks one frozen authority binding") from exc
    if candidate != binding or binding.role is not role or not binding.externally_deployed:
        raise ValueError(f"{label} authority binding differs from its required role")
    return binding


class ARL1IndependentValidationCoordinator:
    """Keyless orchestration over the deployed DB and independent-validator RPC ports."""

    def __init__(
        self,
        *,
        database: DatabaseObservationBridgePort,
        validator: IndependentValidatorPort,
    ) -> None:
        self._database = database
        self._validator = validator
        self._database_binding = _port_binding(
            database,
            attribute="authority_binding",
            role=ControllerStepAuthorityRole.DATABASE_ATTESTATION,
            label="ARL-1 database observation port",
        )
        self._validator_binding = _port_binding(
            validator,
            attribute="authority_binding",
            role=ControllerStepAuthorityRole.INDEPENDENT_VALIDATION,
            label="ARL-1 independent validator port",
        )

    def commit_or_load_validation(
        self,
        *,
        raw_run: RawRunEnvelope,
    ) -> CommittedObservationValidationReceipt:
        try:
            raw_run = RawRunEnvelope.model_validate(raw_run.model_dump(mode="python"))
            authorization = raw_run.scientific_authorization.message
            validator = self._validator_binding
            if (
                authorization.validator_principal_id != validator.principal_id
                or authorization.validator_key_id != validator.key_id
                or authorization.validator_authority_policy_sha256 != validator.policy_sha256
                or authorization.validator_manifest_sha256 != validator.service_manifest_sha256
            ):
                raise ARL1ProtocolCampaignError(
                    "ARL-1 raw run changed the deployment-pinned validator"
                )
            campaign_sha256 = self._validator.prepare_validation_campaign(raw_run=raw_run)
            process_succeeded = (
                raw_run.accepted_terminal_submission.disposition == "process_succeeded"
            )
            if process_succeeded != (campaign_sha256 is not None) or (
                campaign_sha256 is not None
                and (
                    len(campaign_sha256) != 64
                    or any(character not in "0123456789abcdef" for character in campaign_sha256)
                )
            ):
                raise ARL1ProtocolCampaignError(
                    "ARL-1 validation campaign differs from terminal engineering state"
                )
            challenge_receipt = ValidationChallengeRegistrationReceipt.model_validate(
                self._database.issue_validation_challenge(
                    raw_run=raw_run,
                    validation_campaign_sha256=campaign_sha256,
                ).model_dump(mode="python")
            )
            challenge = challenge_receipt.challenge
            challenge_message = challenge.message
            database = self._database_binding
            if (
                challenge_message.raw_run_sha256 != raw_run.raw_run_sha256
                or challenge_message.scientific_slot_id != authorization.scientific_slot_id
                or challenge_message.validation_campaign_sha256 != campaign_sha256
                or challenge_message.issued_by_principal_id != database.principal_id
                or challenge_message.issuance_key_id != database.key_id
                or challenge_message.database_authority_policy_sha256 != database.policy_sha256
            ):
                raise ARL1ProtocolCampaignError(
                    "ARL-1 validation challenge rebound its raw run or database authority"
                )
            receipt = ObservationValidationReceipt.model_validate(
                self._validator.issue_validation_receipt(
                    raw_run=raw_run,
                    validation_campaign_sha256=campaign_sha256,
                    issuance_challenge=challenge,
                ).model_dump(mode="python")
            )
            message = receipt.message
            projection = message.validation_campaign_projection
            if (
                message.raw_run != raw_run
                or message.issuance_challenge != challenge
                or message.validated_by_principal_id != validator.principal_id
                or message.validation_key_id != validator.key_id
                or message.validator_authority_policy_sha256 != validator.policy_sha256
                or (projection is None) != (campaign_sha256 is None)
                or (
                    projection is not None
                    and (
                        projection.campaign_sha256 != campaign_sha256
                        or projection.validator_manifest_sha256 != validator.service_manifest_sha256
                    )
                )
            ):
                raise ARL1ProtocolCampaignError(
                    "ARL-1 independent validation rebound its challenge or authority"
                )
            committed = ValidationCommitReceipt.model_validate(
                self._database.commit_validation(receipt).model_dump(mode="python")
            ).committed_validation
            commit_message = committed.message
            if (
                commit_message.receipt != receipt
                or commit_message.committed_by_principal_id != database.principal_id
                or commit_message.commit_key_id != database.key_id
                or commit_message.database_authority_policy_sha256 != database.policy_sha256
            ):
                raise ARL1ProtocolCampaignError(
                    "ARL-1 committed validation rebound its receipt or database authority"
                )
            return committed
        except ARL1ProtocolCampaignError:
            raise
        except Exception as exc:  # noqa: BLE001 - external authority boundary
            raise ARL1ProtocolCampaignError("ARL-1 independent validation failed closed") from exc


class ARL1PrimaryAdmissionCoordinator:
    """Keyless one-slot admission through separate DB, admission, and Kernel RPC roles."""

    def __init__(
        self,
        *,
        database: DatabaseObservationBridgePort,
        admission: IndependentAdmissionPort,
        coordinator: AtomicAdmissionCoordinatorPort,
    ) -> None:
        self._database = database
        self._admission = admission
        self._coordinator = coordinator
        self._database_binding = _port_binding(
            database,
            attribute="authority_binding",
            role=ControllerStepAuthorityRole.DATABASE_ATTESTATION,
            label="ARL-1 database observation port",
        )
        self._admission_binding = _port_binding(
            admission,
            attribute="authority_binding",
            role=ControllerStepAuthorityRole.INDEPENDENT_ADMISSION,
            label="ARL-1 independent admission port",
        )
        coordinator_database = _port_binding(
            coordinator,
            attribute="database_authority_binding",
            role=ControllerStepAuthorityRole.DATABASE_ATTESTATION,
            label="ARL-1 atomic coordinator database port",
        )
        coordinator_admission = _port_binding(
            coordinator,
            attribute="admission_authority_binding",
            role=ControllerStepAuthorityRole.INDEPENDENT_ADMISSION,
            label="ARL-1 atomic coordinator admission port",
        )
        self._kernel_binding = _port_binding(
            coordinator,
            attribute="kernel_authority_binding",
            role=ControllerStepAuthorityRole.KERNEL_COMMAND,
            label="ARL-1 atomic coordinator Kernel port",
        )
        if (
            coordinator_database != self._database_binding
            or coordinator_admission != self._admission_binding
        ):
            raise ValueError("ARL-1 atomic coordinator authority closure differs")

    def commit_or_load_admission(
        self,
        *,
        committed_validation: CommittedObservationValidationReceipt,
    ) -> AtomicObservationAdmissionReceipt:
        try:
            committed = CommittedObservationValidationReceipt.model_validate(
                committed_validation.model_dump(mode="python")
            )
            validation = committed.message.receipt.message
            authorization = validation.raw_run.scientific_authorization.message
            database = self._database_binding
            admission = self._admission_binding
            if (
                validation.disposition is not BridgeValidationDisposition.VALIDATED_CONFIRMATION
                or validation.outcome is None
                or validation.scientific_observation_sha256 is None
                or committed.message.committed_by_principal_id != database.principal_id
                or committed.message.commit_key_id != database.key_id
                or committed.message.database_authority_policy_sha256 != database.policy_sha256
                or authorization.admission_principal_id != admission.principal_id
                or authorization.admission_key_id != admission.key_id
                or authorization.admission_authority_policy_sha256 != admission.policy_sha256
            ):
                raise ARL1ProtocolCampaignError(
                    "ARL-1 admission source or deployment authority differs"
                )
            challenge_receipt = AdmissionChallengeRegistrationReceipt.model_validate(
                self._database.issue_admission_challenge(committed).model_dump(mode="python")
            )
            challenge = challenge_receipt.challenge
            challenge_message = challenge.message
            if (
                challenge_message.scientific_slot_id != authorization.scientific_slot_id
                or challenge_message.committed_validation_receipt_sha256
                != committed.committed_receipt_sha256
                or challenge_message.validation_receipt_sha256
                != committed.message.validation_receipt_sha256
                or challenge_message.issued_by_principal_id != database.principal_id
                or challenge_message.issuance_key_id != database.key_id
                or challenge_message.database_authority_policy_sha256 != database.policy_sha256
            ):
                raise ARL1ProtocolCampaignError(
                    "ARL-1 admission challenge rebound its validation or database authority"
                )
            decision = ObservationAdmissionDecision.model_validate(
                self._admission.issue_admission_decision(
                    committed_validation=committed,
                    issuance_challenge=challenge,
                ).model_dump(mode="python")
            )
            decision_message = decision.message
            if (
                decision_message.committed_validation_receipt != committed
                or decision_message.issuance_challenge != challenge
                or decision_message.decided_by_principal_id != admission.principal_id
                or decision_message.decision_key_id != admission.key_id
                or decision_message.admission_authority_policy_sha256 != admission.policy_sha256
            ):
                raise ARL1ProtocolCampaignError(
                    "ARL-1 admission decision rebound its challenge or authority"
                )
            if decision_message.disposition is not ObservationAdmissionDisposition.ADMITTED:
                raise ARL1ProtocolCampaignError(
                    "ARL-1 primary observation was independently rejected: "
                    + ",".join(decision_message.reason_codes)
                )
            atomic = AtomicObservationAdmissionReceipt.model_validate(
                self._coordinator.commit_and_incorporate(decision).model_dump(mode="python")
            )
            if (
                atomic.committed_admission.message.decision != decision
                or atomic.incorporation_payload.scientific_slot_id
                != authorization.scientific_slot_id
                or atomic.incorporation_payload.action_id
                != authorization.action_protocol_binding.action.action_id
                or atomic.kernel_receipt.quest_id
                != authorization.action_protocol_binding.action.quest_id
                or atomic.kernel_receipt.principal_id != self._kernel_binding.principal_id
                or atomic.kernel_receipt.authorization_policy_sha256
                != self._kernel_binding.policy_sha256
            ):
                raise ARL1ProtocolCampaignError(
                    "ARL-1 atomic admission rebound observation or Kernel authority"
                )
            return atomic
        except ARL1ProtocolCampaignError:
            raise
        except Exception as exc:  # noqa: BLE001 - external authority boundary
            raise ARL1ProtocolCampaignError("ARL-1 atomic admission failed closed") from exc


class ARL1ProtocolCampaignService:
    """Execute/recover one bounded replicate campaign from durable authority services."""

    def __init__(
        self,
        *,
        registrar: AtomicCampaignRegistrarPort,
        raw_run_source: RawRunSourcePort,
        raw_run_custody: RawRunCustodyPort,
        validation: ReplicateValidationPort,
        admission: PrimaryAdmissionPort,
        kernel_store: KernelAuditPort,
        archive: LocalARL1EvidenceArchive,
    ) -> None:
        self._registrar = registrar
        self._raw_run_source = raw_run_source
        self._raw_run_custody = raw_run_custody
        self._validation = validation
        self._admission = admission
        self._kernel_store = kernel_store
        self._archive = archive

    def execute(
        self,
        request: ARL1ProtocolCampaignRequestV1,
    ) -> ARL1ProtocolCampaignRunReceiptV1:
        try:
            request = ARL1ProtocolCampaignRequestV1.model_validate(
                request.model_dump(mode="python")
            )
            registration = AtomicScientificExecutionCampaignRegistrationReceipt.model_validate(
                self._registrar.register_and_reserve_campaign(request.authorizations).model_dump(
                    mode="python"
                )
            )
            if registration.authorizations != request.authorizations:
                raise ARL1ProtocolCampaignError(
                    "campaign registrar returned another authorization set"
                )
            replicates = tuple(
                self._load_replicate(
                    authorization=authorization,
                    registration=receipt,
                )
                for authorization, receipt in zip(
                    request.authorizations,
                    registration.registration_receipts,
                    strict=True,
                )
            )
            outcomes = {item.outcome for item in replicates}
            if len(outcomes) != 1:
                raise ARL1ProtocolCampaignError(
                    "exact reexecutions did not reproduce one common outcome"
                )
            primary = next(
                item
                for item in replicates
                if item.scientific_slot_id == request.primary_scientific_slot_id
            )
            admission = AtomicObservationAdmissionReceipt.model_validate(
                self._admission.commit_or_load_admission(
                    committed_validation=primary.committed_validation
                ).model_dump(mode="python")
            )
            if (
                admission.committed_admission.message.decision.message.committed_validation_receipt
                != primary.committed_validation
            ):
                raise ARL1ProtocolCampaignError(
                    "primary admission returned another replicate validation"
                )
            event = self._incorporation_event(request=request, admission=admission)
            campaign = self._assemble_campaign(
                request=request,
                registration=registration,
                replicates=replicates,
                admission=admission,
                event=event,
            )
            return ARL1ProtocolCampaignRunReceiptV1(
                request_id=request.request_id,
                request_sha256=request.request_sha256,
                campaign=campaign,
                completed_at=campaign.report.reported_at,
            )
        except ARL1ProtocolCampaignError:
            raise
        except Exception as exc:  # noqa: BLE001 - external authority boundaries fail closed
            raise ARL1ProtocolCampaignError("ARL-1 protocol campaign failed closed") from exc

    def _load_replicate(self, *, authorization, registration):
        message = authorization.message
        binding = message.action_protocol_binding
        raw_run = RawRunEnvelope.model_validate(
            self._raw_run_source.load_raw_run(
                quest_id=binding.action.quest_id,
                action_sha256=binding.action.object_sha256,
                scientific_slot_id=message.scientific_slot_id,
            ).model_dump(mode="python")
        )
        committed = CommittedObservationValidationReceipt.model_validate(
            self._validation.commit_or_load_validation(raw_run=raw_run).model_dump(mode="python")
        )
        validation = committed.message.receipt.message
        if (
            validation.raw_run != raw_run
            or validation.disposition is not BridgeValidationDisposition.VALIDATED_CONFIRMATION
            or validation.outcome is None
            or validation.scientific_observation_sha256 is None
        ):
            raise ARL1ProtocolCampaignError(
                "replicate did not produce a valid independent observation"
            )
        custody = VerifiedRawRunCustodyProjection.model_validate(
            self._raw_run_custody.verify_raw_run_custody(
                raw_run=raw_run,
                observed_at=committed.message.committed_at,
            ).model_dump(mode="python")
        )
        return ARL1ReplicateExecutionEvidenceV1(
            authorization=authorization,
            registration_receipt=registration,
            raw_run=raw_run,
            raw_run_custody=custody,
            committed_validation=committed,
            outcome=ARL1Outcome(ScientificObservationOutcome(validation.outcome).value),
        )

    def _incorporation_event(
        self,
        *,
        request: ARL1ProtocolCampaignRequestV1,
        admission: AtomicObservationAdmissionReceipt,
    ) -> ResearchEvent:
        binding = request.authorizations[0].message.action_protocol_binding
        audit = self._kernel_store.audit(
            binding.action.quest_id,
            expected_scope_binding=binding.compilation_request.protocol.graph_scope.scope_binding,
        )
        event_sha256 = admission.kernel_receipt.result_event_sha256
        matches = tuple(event for event in audit.events if event.event_sha256 == event_sha256)
        if len(matches) != 1:
            raise ARL1ProtocolCampaignError(
                "atomic admission does not resolve one audited Kernel event"
            )
        event = ResearchEvent.model_validate(matches[0].model_dump(mode="python"))
        payload = event.payload
        if (
            event.event_type is not EventType.OBSERVATION_INCORPORATED
            or not isinstance(payload, ObservationIncorporatedPayload)
            or payload.committed_admission_sha256
            != admission.committed_admission.committed_admission_sha256
            or payload.scientific_slot_id != request.primary_scientific_slot_id
        ):
            raise ARL1ProtocolCampaignError("audited Kernel event rebound the primary admission")
        return event

    def _assemble_campaign(
        self,
        *,
        request: ARL1ProtocolCampaignRequestV1,
        registration: AtomicScientificExecutionCampaignRegistrationReceipt,
        replicates: tuple[ARL1ReplicateExecutionEvidenceV1, ...],
        admission: AtomicObservationAdmissionReceipt,
        event: ResearchEvent,
    ) -> ARL1ProtocolCampaignEvidenceV1:
        primary = next(
            item
            for item in replicates
            if item.scientific_slot_id == request.primary_scientific_slot_id
        )
        binding = primary.authorization.message.action_protocol_binding
        protocol = request.compilation_request.protocol
        work_order = request.compilation_result.work_order
        if work_order is None:  # pragma: no cover - request validation proves it
            raise ARL1ProtocolCampaignError("accepted campaign lost its WorkOrder")
        node = next(item for item in work_order.nodes if item.node_id == request.work_order_node_id)
        retained_at = max(item.committed_validation.message.committed_at for item in replicates)
        attempts = ARL1AllAttemptsManifestV1(
            quest_id=binding.action.quest_id,
            action_sha256=binding.action.object_sha256,
            protocol_sha256=protocol.protocol_sha256,
            work_order_node_sha256=node.node_sha256,
            attempts=tuple(ARL1AttemptEvidenceRefV1.from_replicate(item) for item in replicates),
            retained_at=retained_at,
        )
        outcome = primary.outcome
        reproduction = ARL1ReproductionReceiptV1(
            campaign_registration_sha256=registration.campaign_registration_sha256,
            replicate_evidence_sha256s=tuple(item.evidence_sha256 for item in replicates),
            scientific_slot_ids=tuple(item.scientific_slot_id for item in replicates),
            committed_validation_receipt_sha256s=tuple(
                item.committed_validation.committed_receipt_sha256 for item in replicates
            ),
            scientific_observation_sha256s=tuple(
                item.scientific_observation_sha256 for item in replicates
            ),
            outcome=outcome,
            reproduced_at=retained_at,
        )
        placeholder = "0" * 64
        provisional = self._campaign_with_manifest(
            request=request,
            registration=registration,
            replicates=replicates,
            attempts=attempts,
            reproduction=reproduction,
            admission=admission,
            event=event,
            archive_manifest_sha256=placeholder,
        )
        manifest = build_protocol_campaign_archive_manifest(
            provisional,
            retained_at=event.committed_at,
        )
        campaign = self._campaign_with_manifest(
            request=request,
            registration=registration,
            replicates=replicates,
            attempts=attempts,
            reproduction=reproduction,
            admission=admission,
            event=event,
            archive_manifest_sha256=manifest.manifest_sha256,
        )
        retained = retain_protocol_campaign_archive(
            self._archive,
            campaign,
            retained_at=event.committed_at,
        )
        if retained != manifest:
            raise ARL1ProtocolCampaignError("campaign archive changed during publication")
        return campaign

    @staticmethod
    def _campaign_with_manifest(
        *,
        request,
        registration,
        replicates,
        attempts,
        reproduction,
        admission,
        event,
        archive_manifest_sha256,
    ) -> ARL1ProtocolCampaignEvidenceV1:
        primary = next(
            item
            for item in replicates
            if item.scientific_slot_id == request.primary_scientific_slot_id
        )
        message = primary.authorization.message
        intent = message.qualification_bundle.intent
        work_order = request.compilation_result.work_order
        if work_order is None:  # pragma: no cover - request validation proves it
            raise ARL1ProtocolCampaignError("campaign assembly lost its WorkOrder")
        reexecution_hashes = tuple(sorted(item.evidence_sha256 for item in replicates))
        report = build_arl1_protocol_executor_report(
            quest_id=message.action_protocol_binding.action.quest_id,
            question_ref=request.compilation_request.protocol.graph_scope.question_ref,
            protocol_sha256=request.compilation_request.protocol.protocol_sha256,
            compilation_receipt_sha256=request.compilation_result.receipt.receipt_sha256,
            work_order_sha256=work_order.work_order_sha256,
            work_order_node_id=request.work_order_node_id,
            exact_reexecution_evidence_sha256s=reexecution_hashes,
            all_attempts_manifest_sha256=attempts.manifest_sha256,
            committed_validation_receipt_sha256=(
                primary.committed_validation.committed_receipt_sha256
            ),
            committed_admission_sha256=(admission.committed_admission.committed_admission_sha256),
            incorporation_event_sha256=event.event_sha256,
            outcome=primary.outcome,
            reproduction_receipt_sha256=reproduction.receipt_sha256,
            source_evidence_archive_manifest_sha256=archive_manifest_sha256,
            reported_at=event.committed_at,
        )
        return ARL1ProtocolCampaignEvidenceV1(
            domain_scope=request.domain_scope,
            modality_scope=request.modality_scope,
            compilation_request=request.compilation_request,
            compilation_result=request.compilation_result,
            work_order_node_id=request.work_order_node_id,
            campaign_registration=registration,
            replicate_executions=replicates,
            all_attempts_manifest=attempts,
            reproduction_receipt=reproduction,
            committed_admission=admission.committed_admission,
            incorporation_event=event,
            scientific_slot_id=primary.scientific_slot_id,
            execution_id=intent.execution_id,
            infrastructure_attempt_id=intent.infrastructure_attempt.infrastructure_attempt_id,
            scientific_execution_authorization_sha256=primary.authorization.authorization_sha256,
            qualification_bundle_sha256=message.qualification_bundle.bundle_sha256,
            qualification_grant_sha256=message.qualification_grant.grant_sha256,
            terminal_receipt_sha256=(
                primary.raw_run.accepted_terminal_submission.accepted_terminal_submission_sha256
            ),
            artifact_manifest_sha256=primary.raw_run.artifact_manifest.manifest_sha256,
            validator_manifest_sha256=message.validator_manifest_sha256,
            validation_policy_sha256=message.observation_validation_policy_sha256,
            committed_validation_receipt_sha256=(
                primary.committed_validation.committed_receipt_sha256
            ),
            committed_admission_sha256=(admission.committed_admission.committed_admission_sha256),
            scientific_observation_sha256=primary.scientific_observation_sha256,
            incorporation_event_sha256=event.event_sha256,
            outcome=primary.outcome,
            exact_reexecution_evidence_sha256s=reexecution_hashes,
            reproduction_receipt_sha256=reproduction.receipt_sha256,
            all_attempts_manifest_sha256=attempts.manifest_sha256,
            all_attempt_count=len(replicates),
            source_evidence_archive_manifest_sha256=archive_manifest_sha256,
            scientific_execution_authorized_at=message.authorized_at,
            validator_frozen_at=message.admission_policy.frozen_at,
            execution_started_at=min(
                item.raw_run_custody.runtime_launched_at for item in replicates
            ),
            execution_completed_at=max(
                item.raw_run.accepted_runtime_termination.runtime_ended_at for item in replicates
            ),
            validated_at=max(item.committed_validation.message.committed_at for item in replicates),
            admitted_at=admission.committed_admission.message.committed_at,
            incorporated_at=event.committed_at,
            report=report,
        )


__all__ = [
    "ARL1IndependentValidationCoordinator",
    "ARL1PrimaryAdmissionCoordinator",
    "ARL1ProtocolCampaignError",
    "ARL1ProtocolCampaignPending",
    "ARL1ProtocolCampaignRequestV1",
    "ARL1ProtocolCampaignRunReceiptV1",
    "ARL1ProtocolCampaignService",
    "AtomicCampaignRegistrarPort",
    "AtomicAdmissionCoordinatorPort",
    "DatabaseObservationBridgePort",
    "IndependentAdmissionPort",
    "IndependentValidatorPort",
    "KernelAuditPort",
    "PrimaryAdmissionPort",
    "RawRunCustodyPort",
    "RawRunSourcePort",
    "ReplicateValidationPort",
]
