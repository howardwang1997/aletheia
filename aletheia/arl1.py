"""Frozen system qualification for ``ARL-1 Protocol Executor``.

ARL-1 is an engineering capability claim: on a bounded, explicitly named scope the deployed
system can execute a caller-supplied question and protocol, apply a validator frozen before the
run, retain every attempt, reproduce the result, and render a deterministic report.  It is not a
scientific-validity claim and it grants no Research Kernel mutation authority.

The qualification signer is deliberately not allowed to accept hashes at face value.  Issuance
requires a source-verification port, native replay of the protocol compiler, and native offline
verification of the destructive PR-8h target campaign.  The signed receipt binds the complete
evidence bundle while retaining explicit claim ceilings.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Literal, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import AwareDatetime, Field, model_validator

from aletheia.protocols.compiler import (
    ProtocolCompilationRequest,
    verify_compilation,
)
from aletheia.protocols.schemas import ProtocolCompilationResult
from aletheia.observations.execution_registration import (
    AtomicScientificExecutionCampaignRegistrationReceipt,
    AtomicScientificExecutionRegistrationReceipt,
)
from aletheia.observations.scientific_bridge import (
    BridgeValidationDisposition,
    CommittedObservationAdmission,
    CommittedObservationValidationReceipt,
    RawRunEnvelope,
    ScientificExecutionAuthorization,
    ScientificObservationOutcome,
    VerifiedRawRunCustodyProjection,
)
from aletheia.qualification_campaign import (
    QualificationCampaignError,
    QualificationTargetCampaignReceiptV1,
    QualificationTargetCampaignRequestV1,
    verify_qualification_target_campaign_receipt,
)
from aletheia.research_kernel.policy import ed25519_key_id, ed25519_public_key_hex
from aletheia.research_kernel.schemas import (
    KernelModel,
    KernelObjectKind,
    KernelObjectRef,
    EventType,
    ObservationIncorporatedPayload,
    ResearchEvent,
    canonical_json_bytes,
    canonical_sha256,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SIGNATURE_PATTERN = r"^[0-9a-f]{128}$"
_SYMBOLIC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$"
_CAMPAIGN_ID_PATTERN = r"^arl1c_[0-9a-f]{32}$"
_POLICY_ID_PATTERN = r"^arl1p_[0-9a-f]{32}$"
_VERIFICATION_ID_PATTERN = r"^arl1v_[0-9a-f]{32}$"
_RECEIPT_ID_PATTERN = r"^arl1r_[0-9a-f]{32}$"
_TRUST_ANCHOR_ID_PATTERN = r"^arl1t_[0-9a-f]{32}$"
_SIGNATURE_CONTEXT = b"aletheia.arl1.qualification.v1\0"
_SOURCE_VERIFICATION_SIGNATURE_CONTEXT = b"aletheia.arl1.source-verification.v1\0"

_Sha256 = Annotated[str, Field(pattern=_SHA256_PATTERN)]
_SymbolicId = Annotated[str, Field(pattern=_SYMBOLIC_ID_PATTERN)]


class ARL1QualificationError(ValueError):
    """An ARL-1 contract, evidence source, policy, or signature failed closed."""


class ARL0GateKind(str, Enum):
    LEDGER_REPLAY = "ledger_replay"
    SANDBOX_ISOLATION = "sandbox_isolation"
    HIDDEN_BOUNDARY = "hidden_boundary"
    ALL_ATTEMPT_RETENTION = "all_attempt_retention"
    CLAIM_CEILING = "claim_ceiling"
    SCHEMA_STRUCTURE = "schema_structure"
    DEPENDENCY_AUDIT = "dependency_audit"


_REQUIRED_ARL0_GATES = tuple(ARL0GateKind)
_REPORT_LIMITATIONS = (
    "given_question_and_protocol_only",
    "not_autonomous_research_design",
    "not_scientific_validity_proof",
    "not_independent_replication",
)


class ARL1Outcome(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    INCONCLUSIVE = "inconclusive"


class ARL1VerificationSubjectKind(str, Enum):
    ARL0_INTEGRITY = "arl0_integrity"
    EVIDENCE_ARCHIVE = "evidence_archive"
    PROTOCOL_CAMPAIGN = "protocol_campaign"


class ARL1ArchiveManifestKind(str, Enum):
    ARL0_INTEGRITY = "arl0_integrity"
    PROTOCOL_CAMPAIGN = "protocol_campaign"
    EVIDENCE_BUNDLE = "evidence_bundle"


class ARL1EvidenceArchiveEntryV1(KernelModel):
    """One immutable source object retained in the ARL-1 content-addressed archive."""

    schema_name: Literal["aletheia.arl1_evidence_archive_entry"] = (
        "aletheia.arl1_evidence_archive_entry"
    )
    schema_version: Literal[1] = 1
    object_kind: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    object_sha256: str = Field(pattern=_SHA256_PATTERN)
    byte_length: int = Field(ge=1, le=64 * 1024**2)
    canonical_json: bool


class ARL1EvidenceArchiveManifestV1(KernelModel):
    """Closed, append-only inventory whose own canonical bytes are content addressed."""

    schema_name: Literal["aletheia.arl1_evidence_archive_manifest"] = (
        "aletheia.arl1_evidence_archive_manifest"
    )
    schema_version: Literal[1] = 1
    manifest_kind: ARL1ArchiveManifestKind
    scope_sha256: str = Field(pattern=_SHA256_PATTERN)
    entries: tuple[ARL1EvidenceArchiveEntryV1, ...] = Field(min_length=1, max_length=10_000)
    retained_at: AwareDatetime
    content_addressed: Literal[True] = True
    append_only: Literal[True] = True

    @model_validator(mode="after")
    def _manifest_is_canonical(self) -> "ARL1EvidenceArchiveManifestV1":
        keys = tuple((item.object_kind, item.object_sha256) for item in self.entries)
        if keys != tuple(sorted(keys)) or len(set(keys)) != len(keys):
            raise ValueError("ARL-1 archive entries must be unique and canonical")
        if len({item.object_kind for item in self.entries}) != len(self.entries):
            raise ValueError("ARL-1 archive object kinds must identify exactly one source")
        return self

    @property
    def manifest_sha256(self) -> str:
        return canonical_sha256(self)


class ARL1EvidenceVerifierPinV1(KernelModel):
    """Deployment-owned public authority for one independent evidence verifier."""

    schema_name: Literal["aletheia.arl1_evidence_verifier_pin"] = (
        "aletheia.arl1_evidence_verifier_pin"
    )
    schema_version: Literal[1] = 1
    verification_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    key_id: str = Field(pattern=_SHA256_PATTERN)
    public_key_ed25519_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _pin_is_exact(self) -> "ARL1EvidenceVerifierPinV1":
        if self.key_id != ed25519_key_id(self.public_key_ed25519_hex):
            raise ValueError("ARL-1 verifier key id differs from its public key")
        if self.expires_at <= self.valid_from or (
            self.revoked_at is not None
            and not self.valid_from <= self.revoked_at <= self.expires_at
        ):
            raise ValueError("ARL-1 verifier key interval is invalid")
        return self

    @property
    def active_until(self) -> datetime:
        return min(self.expires_at, self.revoked_at or self.expires_at)

    def active_at(self, observed_at: datetime) -> bool:
        return self.valid_from <= observed_at < self.active_until

    @property
    def pin_sha256(self) -> str:
        return canonical_sha256(self)


class ARL0GateEvidenceV1(KernelModel):
    schema_name: Literal["aletheia.arl0_gate_evidence"] = "aletheia.arl0_gate_evidence"
    schema_version: Literal[1] = 1
    gate_kind: ARL0GateKind
    evaluated_scope_sha256: str = Field(pattern=_SHA256_PATTERN)
    evidence_artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    verification_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    verified_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    verified_at: AwareDatetime
    passed: Literal[True] = True
    synthetic_evidence: Literal[False] = False

    @property
    def evidence_sha256(self) -> str:
        return canonical_sha256(self)


class ARL0IntegrityEvidenceV1(KernelModel):
    schema_name: Literal["aletheia.arl0_integrity_evidence"] = "aletheia.arl0_integrity_evidence"
    schema_version: Literal[1] = 1
    source_tree_sha256: str = Field(pattern=_SHA256_PATTERN)
    environment_lock_sha256: str = Field(pattern=_SHA256_PATTERN)
    schema_revision: str = Field(pattern=r"^[0-9]{8}_[0-9]{4}$")
    database_schema_verification_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    gates: tuple[ARL0GateEvidenceV1, ...] = Field(
        min_length=len(_REQUIRED_ARL0_GATES),
        max_length=len(_REQUIRED_ARL0_GATES),
    )
    evidence_archive_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    completed_at: AwareDatetime
    all_lower_level_gates_passed: Literal[True] = True
    integrity_qualified: Literal[True] = True
    synthetic_evidence: Literal[False] = False

    @model_validator(mode="after")
    def _integrity_is_complete(self) -> "ARL0IntegrityEvidenceV1":
        if tuple(item.gate_kind for item in self.gates) != _REQUIRED_ARL0_GATES:
            raise ValueError("ARL-0 gates must be exhaustive and canonically ordered")
        if len({item.evidence_sha256 for item in self.gates}) != len(self.gates):
            raise ValueError("ARL-0 gate evidence must be unique")
        if len({item.evidence_artifact_sha256 for item in self.gates}) != len(self.gates) or len(
            {item.verification_receipt_sha256 for item in self.gates}
        ) != len(self.gates):
            raise ValueError("ARL-0 gates must retain distinct evidence and verification receipts")
        if any(item.verified_at > self.completed_at for item in self.gates):
            raise ValueError("ARL-0 completion predates one of its gate receipts")
        return self

    @property
    def integrity_sha256(self) -> str:
        return canonical_sha256(self)


class ARL1ReplicateExecutionEvidenceV1(KernelModel):
    """Full typed authority chain for one preregistered scientific replicate slot."""

    schema_name: Literal["aletheia.arl1_replicate_execution_evidence"] = (
        "aletheia.arl1_replicate_execution_evidence"
    )
    schema_version: Literal[1] = 1
    authorization: ScientificExecutionAuthorization
    registration_receipt: AtomicScientificExecutionRegistrationReceipt
    raw_run: RawRunEnvelope
    raw_run_custody: VerifiedRawRunCustodyProjection
    committed_validation: CommittedObservationValidationReceipt
    outcome: ARL1Outcome
    synthetic_evidence: Literal[False] = False

    @model_validator(mode="after")
    def _replicate_is_exact(self) -> "ARL1ReplicateExecutionEvidenceV1":
        authorization = self.authorization
        message = authorization.message
        binding = message.action_protocol_binding
        intent = message.qualification_bundle.intent
        registration = self.registration_receipt
        raw_run = self.raw_run
        custody = self.raw_run_custody
        validation_message = self.committed_validation.message
        validation = validation_message.receipt.message
        terminal = raw_run.accepted_terminal_submission
        expected_outcome = ScientificObservationOutcome(self.outcome.value)
        if (
            registration.authorization_sha256 != authorization.authorization_sha256
            or registration.quest_id != binding.action.quest_id
            or registration.scientific_slot_id != message.scientific_slot_id
            or registration.action_sha256 != binding.action.object_sha256
            or registration.execution_id != intent.execution_id
            or registration.attempt_id != intent.infrastructure_attempt.infrastructure_attempt_id
            or registration.qualification_bundle_sha256
            != message.qualification_bundle.bundle_sha256
            or registration.qualification_grant_sha256 != message.qualification_grant.grant_sha256
        ):
            raise ValueError("ARL-1 replicate registration rebound its signed authorization")
        if (
            raw_run.scientific_authorization != authorization
            or raw_run.qualification_admission_sha256 != registration.qualification_admission_sha256
            or terminal.disposition != "process_succeeded"
            or validation.raw_run != raw_run
            or validation.disposition is not BridgeValidationDisposition.VALIDATED_CONFIRMATION
            or validation.outcome is not expected_outcome
            or validation.scientific_observation_sha256 is None
        ):
            raise ValueError("ARL-1 replicate lacks exact successful independent validation")
        if (
            custody.raw_run_sha256 != raw_run.raw_run_sha256
            or custody.scientific_execution_authorization_sha256
            != authorization.authorization_sha256
            or custody.scientific_slot_id != message.scientific_slot_id
            or custody.qualification_admission_sha256 != registration.qualification_admission_sha256
            or custody.resource_reservation_sha256 != registration.resource_reservation_sha256
            or custody.artifact_manifest_sha256 != raw_run.artifact_manifest.manifest_sha256
            or custody.terminal_acceptance_sha256 != terminal.accepted_terminal_submission_sha256
            or custody.sea_registered_at != registration.registered_at
            or custody.resource_reserved_at != registration.reserved_at
        ):
            raise ValueError(
                "ARL-1 replicate raw-run custody differs from registration or terminal"
            )
        if not (
            registration.registered_at
            < registration.reserved_at
            <= custody.runtime_launched_at
            <= raw_run.accepted_runtime_termination.runtime_ended_at
            <= validation.validated_at
            <= validation_message.committed_at
        ):
            raise ValueError("ARL-1 replicate execution and validation chronology differs")
        return self

    @property
    def slot_index(self) -> int:
        return self.authorization.message.action_protocol_binding.replicate_slot.slot_index

    @property
    def scientific_slot_id(self) -> str:
        return self.authorization.message.scientific_slot_id

    @property
    def scientific_observation_sha256(self) -> str:
        value = self.committed_validation.message.receipt.message.scientific_observation_sha256
        if value is None:  # pragma: no cover - model validator proves this
            raise ARL1QualificationError("validated ARL-1 replicate lost its observation")
        return value

    @property
    def evidence_sha256(self) -> str:
        return canonical_sha256(self)


class ARL1AttemptEvidenceRefV1(KernelModel):
    schema_name: Literal["aletheia.arl1_attempt_evidence_ref"] = (
        "aletheia.arl1_attempt_evidence_ref"
    )
    schema_version: Literal[1] = 1
    slot_index: int = Field(ge=1, le=100)
    scientific_slot_id: str = Field(pattern=r"^sos_[0-9a-f]{32}$")
    execution_id: str = Field(pattern=r"^exe_[0-9a-f]{32}$")
    infrastructure_attempt_id: str = Field(pattern=r"^iat_[0-9a-f]{32}$")
    authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    registration_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    raw_run_sha256: str = Field(pattern=_SHA256_PATTERN)
    terminal_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    committed_validation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)

    @classmethod
    def from_replicate(
        cls,
        replicate: ARL1ReplicateExecutionEvidenceV1,
    ) -> "ARL1AttemptEvidenceRefV1":
        authorization = replicate.authorization
        intent = authorization.message.qualification_bundle.intent
        return cls(
            slot_index=replicate.slot_index,
            scientific_slot_id=replicate.scientific_slot_id,
            execution_id=intent.execution_id,
            infrastructure_attempt_id=(intent.infrastructure_attempt.infrastructure_attempt_id),
            authorization_sha256=authorization.authorization_sha256,
            registration_receipt_sha256=replicate.registration_receipt.receipt_sha256,
            raw_run_sha256=replicate.raw_run.raw_run_sha256,
            terminal_receipt_sha256=(
                replicate.raw_run.accepted_terminal_submission.accepted_terminal_submission_sha256
            ),
            artifact_manifest_sha256=replicate.raw_run.artifact_manifest.manifest_sha256,
            committed_validation_receipt_sha256=(
                replicate.committed_validation.committed_receipt_sha256
            ),
        )


class ARL1AllAttemptsManifestV1(KernelModel):
    schema_name: Literal["aletheia.arl1_all_attempts_manifest"] = (
        "aletheia.arl1_all_attempts_manifest"
    )
    schema_version: Literal[1] = 1
    quest_id: str = Field(pattern=r"^qst_[0-9a-f]{32}$")
    action_sha256: str = Field(pattern=_SHA256_PATTERN)
    protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    work_order_node_sha256: str = Field(pattern=_SHA256_PATTERN)
    attempts: tuple[ARL1AttemptEvidenceRefV1, ...] = Field(min_length=2, max_length=100)
    retained_at: AwareDatetime
    append_only_archive: Literal[True] = True
    all_attempts_retained: Literal[True] = True

    @model_validator(mode="after")
    def _manifest_is_exhaustive(self) -> "ARL1AllAttemptsManifestV1":
        if tuple(item.slot_index for item in self.attempts) != tuple(
            range(1, len(self.attempts) + 1)
        ):
            raise ValueError("ARL-1 attempt manifest slots must be exhaustive and canonical")
        identities = (
            tuple(item.scientific_slot_id for item in self.attempts),
            tuple(item.execution_id for item in self.attempts),
            tuple(item.infrastructure_attempt_id for item in self.attempts),
            tuple(item.authorization_sha256 for item in self.attempts),
            tuple(item.raw_run_sha256 for item in self.attempts),
        )
        if any(len(set(values)) != len(self.attempts) for values in identities):
            raise ValueError("ARL-1 attempt manifest repeats an authority identity")
        return self

    @property
    def manifest_sha256(self) -> str:
        return canonical_sha256(self)


class ARL1ReproductionReceiptV1(KernelModel):
    schema_name: Literal["aletheia.arl1_reproduction_receipt"] = (
        "aletheia.arl1_reproduction_receipt"
    )
    schema_version: Literal[1] = 1
    campaign_registration_sha256: str = Field(pattern=_SHA256_PATTERN)
    replicate_evidence_sha256s: tuple[_Sha256, ...] = Field(min_length=2, max_length=100)
    scientific_slot_ids: tuple[str, ...] = Field(min_length=2, max_length=100)
    committed_validation_receipt_sha256s: tuple[_Sha256, ...] = Field(
        min_length=2,
        max_length=100,
    )
    scientific_observation_sha256s: tuple[_Sha256, ...] = Field(
        min_length=2,
        max_length=100,
    )
    outcome: ARL1Outcome
    reproduced_at: AwareDatetime
    every_preregistered_slot_executed: Literal[True] = True
    every_execution_independently_validated: Literal[True] = True
    common_outcome_reproduced: Literal[True] = True
    synthetic_evidence: Literal[False] = False

    @model_validator(mode="after")
    def _reproduction_is_nonvacuous(self) -> "ARL1ReproductionReceiptV1":
        count = len(self.replicate_evidence_sha256s)
        if (
            len(self.scientific_slot_ids) != count
            or len(self.committed_validation_receipt_sha256s) != count
            or len(self.scientific_observation_sha256s) != count
        ):
            raise ValueError("ARL-1 reproduction receipt coverage is incomplete")
        for values in (
            self.replicate_evidence_sha256s,
            self.scientific_slot_ids,
            self.committed_validation_receipt_sha256s,
            self.scientific_observation_sha256s,
        ):
            if len(set(values)) != count:
                raise ValueError("ARL-1 reproduction receipt reuses one replicate authority")
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


class ARL1ProtocolExecutorReportV1(KernelModel):
    """Deterministic, deliberately narrow report derived from one protocol campaign."""

    schema_name: Literal["aletheia.arl1_protocol_executor_report"] = (
        "aletheia.arl1_protocol_executor_report"
    )
    schema_version: Literal[1] = 1
    report_id: str | None = Field(default=None, pattern=r"^arl1o_[0-9a-f]{32}$")
    quest_id: str = Field(pattern=r"^qst_[0-9a-f]{32}$")
    question_ref: KernelObjectRef
    protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    compilation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    work_order_sha256: str = Field(pattern=_SHA256_PATTERN)
    work_order_node_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    exact_reexecution_evidence_sha256s: tuple[_Sha256, ...] = Field(
        min_length=2,
        max_length=100,
    )
    all_attempts_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    committed_validation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    committed_admission_sha256: str = Field(pattern=_SHA256_PATTERN)
    incorporation_event_sha256: str = Field(pattern=_SHA256_PATTERN)
    outcome: ARL1Outcome
    reproduction_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_evidence_archive_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    reported_at: AwareDatetime
    claim_ceiling: Literal["bounded_protocol_execution_engineering"] = (
        "bounded_protocol_execution_engineering"
    )
    limitations: tuple[str, ...] = _REPORT_LIMITATIONS
    autonomous_research_design_claimed: Literal[False] = False
    scientific_validity_claimed: Literal[False] = False
    independent_replication_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _report_is_canonical(self) -> "ARL1ProtocolExecutorReportV1":
        if self.question_ref.object_kind is not KernelObjectKind.QUESTION:
            raise ValueError("ARL-1 report must bind an authoritative question")
        if self.question_ref.quest_id != self.quest_id:
            raise ValueError("ARL-1 report question escaped its Quest")
        if self.exact_reexecution_evidence_sha256s != tuple(
            sorted(set(self.exact_reexecution_evidence_sha256s))
        ):
            raise ValueError("ARL-1 report reexecution evidence must be unique and canonical")
        if self.limitations != _REPORT_LIMITATIONS:
            raise ValueError("ARL-1 report limitations cannot be weakened or reordered")
        expected = f"arl1o_{self.identity_sha256[:32]}"
        if self.report_id is not None and self.report_id != expected:
            raise ValueError("ARL-1 report id differs from its contents")
        object.__setattr__(self, "report_id", expected)
        return self

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"report_id"}))

    @property
    def report_sha256(self) -> str:
        return canonical_sha256(self)


class ARL1ProtocolCampaignEvidenceV1(KernelModel):
    """One given-protocol execution, validation, exact reexecution, admission, and report."""

    schema_name: Literal["aletheia.arl1_protocol_campaign_evidence"] = (
        "aletheia.arl1_protocol_campaign_evidence"
    )
    schema_version: Literal[1] = 1
    campaign_id: str | None = Field(default=None, pattern=_CAMPAIGN_ID_PATTERN)
    domain_scope: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    modality_scope: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    compilation_request: ProtocolCompilationRequest
    compilation_result: ProtocolCompilationResult
    work_order_node_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    campaign_registration: AtomicScientificExecutionCampaignRegistrationReceipt
    replicate_executions: tuple[ARL1ReplicateExecutionEvidenceV1, ...] = Field(
        min_length=2,
        max_length=100,
    )
    all_attempts_manifest: ARL1AllAttemptsManifestV1
    reproduction_receipt: ARL1ReproductionReceiptV1
    committed_admission: CommittedObservationAdmission
    incorporation_event: ResearchEvent
    scientific_slot_id: str = Field(pattern=r"^sos_[0-9a-f]{32}$")
    execution_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    scientific_execution_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    qualification_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    qualification_grant_sha256: str = Field(pattern=_SHA256_PATTERN)
    terminal_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    validator_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    validation_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    committed_validation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    committed_admission_sha256: str = Field(pattern=_SHA256_PATTERN)
    scientific_observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    incorporation_event_sha256: str = Field(pattern=_SHA256_PATTERN)
    outcome: ARL1Outcome
    exact_reexecution_evidence_sha256s: tuple[_Sha256, ...] = Field(
        min_length=2,
        max_length=100,
    )
    reproduction_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    all_attempts_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    all_attempt_count: int = Field(ge=2, le=100)
    source_evidence_archive_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    scientific_execution_authorized_at: AwareDatetime
    validator_frozen_at: AwareDatetime
    execution_started_at: AwareDatetime
    execution_completed_at: AwareDatetime
    validated_at: AwareDatetime
    admitted_at: AwareDatetime
    incorporated_at: AwareDatetime
    report: ARL1ProtocolExecutorReportV1
    engineering_terminal_disposition: Literal["process_succeeded"] = "process_succeeded"
    validation_disposition: Literal["validated_confirmation"] = "validated_confirmation"
    predefined_validator_frozen_before_execution: Literal[True] = True
    all_attempts_retained: Literal[True] = True
    exact_reproduction_passed: Literal[True] = True
    observation_admitted: Literal[True] = True
    observation_incorporated: Literal[True] = True
    synthetic_evidence: Literal[False] = False

    @model_validator(mode="after")
    def _campaign_is_exact(self) -> "ARL1ProtocolCampaignEvidenceV1":
        try:
            verify_compilation(self.compilation_request, self.compilation_result)
        except (TypeError, ValueError) as exc:
            raise ValueError("ARL-1 protocol compilation is not canonical") from exc
        protocol = self.compilation_request.protocol
        result = self.compilation_result
        work_order = result.work_order
        if work_order is None or not result.report.accepted:
            raise ValueError("ARL-1 requires an accepted protocol and exact work order")
        nodes = tuple(item for item in work_order.nodes if item.node_id == self.work_order_node_id)
        if len(nodes) != 1:
            raise ValueError("ARL-1 work-order node does not resolve exactly once")
        node = nodes[0]
        if node.scientific_replicate_count < 2:
            raise ValueError("ARL-1 protocol must preregister at least two exact reexecutions")
        question = protocol.graph_scope.question_ref
        if (
            question.object_kind is not KernelObjectKind.QUESTION
            or work_order.quest_id != question.quest_id
        ):
            raise ValueError("ARL-1 question differs from the compiled work order")
        if self.all_attempt_count != node.scientific_replicate_count:
            raise ValueError("ARL-1 evidence must cover every preregistered exact reexecution")
        replicates = self.replicate_executions
        registration = self.campaign_registration
        if (
            len(replicates) != node.scientific_replicate_count
            or tuple(item.slot_index for item in replicates) != tuple(range(1, len(replicates) + 1))
            or registration.authorizations != tuple(item.authorization for item in replicates)
            or registration.registration_receipts
            != tuple(item.registration_receipt for item in replicates)
        ):
            raise ValueError("ARL-1 replicate registration or execution coverage is incomplete")
        first = replicates[0]
        first_message = first.authorization.message
        first_binding = first_message.action_protocol_binding
        if any(
            item.authorization.message.action_protocol_binding.compilation_request
            != self.compilation_request
            or item.authorization.message.action_protocol_binding.compilation_result
            != self.compilation_result
            or item.authorization.message.action_protocol_binding.work_order_node != node
            or item.authorization.message.action_protocol_binding.action != first_binding.action
            or item.outcome is not self.outcome
            for item in replicates
        ):
            raise ValueError("ARL-1 replicate escaped the authorized protocol campaign")
        primary_matches = tuple(
            item for item in replicates if item.scientific_slot_id == self.scientific_slot_id
        )
        if len(primary_matches) != 1:
            raise ValueError("ARL-1 campaign requires one exact primary admitted slot")
        primary = primary_matches[0]
        primary_message = primary.authorization.message
        primary_intent = primary_message.qualification_bundle.intent
        primary_validation = primary.committed_validation
        admission = self.committed_admission
        decision = admission.message.decision.message
        if (
            decision.committed_validation_receipt != primary_validation
            or decision.scientific_slot_id != self.scientific_slot_id
            or decision.admitted_observation_sha256 != primary.scientific_observation_sha256
            or self.scientific_execution_authorization_sha256
            != primary.authorization.authorization_sha256
            or self.execution_id != primary_intent.execution_id
            or self.infrastructure_attempt_id
            != primary_intent.infrastructure_attempt.infrastructure_attempt_id
            or self.qualification_bundle_sha256
            != primary_message.qualification_bundle.bundle_sha256
            or self.qualification_grant_sha256 != primary_message.qualification_grant.grant_sha256
            or self.terminal_receipt_sha256
            != primary.raw_run.accepted_terminal_submission.accepted_terminal_submission_sha256
            or self.artifact_manifest_sha256 != primary.raw_run.artifact_manifest.manifest_sha256
            or self.validator_manifest_sha256 != primary_message.validator_manifest_sha256
            or self.validation_policy_sha256 != primary_message.observation_validation_policy_sha256
            or self.committed_validation_receipt_sha256
            != primary_validation.committed_receipt_sha256
            or self.committed_admission_sha256 != admission.committed_admission_sha256
            or self.scientific_observation_sha256 != primary.scientific_observation_sha256
        ):
            raise ValueError("ARL-1 primary observation authority was rebound")
        event = self.incorporation_event
        payload = event.payload
        if (
            event.event_type is not EventType.OBSERVATION_INCORPORATED
            or not isinstance(payload, ObservationIncorporatedPayload)
            or event.quest_id != first_binding.action.quest_id
            or payload.action_id != first_binding.action.action_id
            or payload.branch_id != first_binding.action_proposed_event.payload.branch_id
            or payload.scientific_slot_id != self.scientific_slot_id
            or payload.committed_admission_sha256 != admission.committed_admission_sha256
            or payload.scientific_observation_sha256 != primary.scientific_observation_sha256
            or payload.outcome != self.outcome.value
            or self.incorporation_event_sha256 != event.event_sha256
        ):
            raise ValueError("ARL-1 incorporation event differs from the admitted observation")
        expected_attempts = tuple(
            ARL1AttemptEvidenceRefV1.from_replicate(item) for item in replicates
        )
        expected_manifest = ARL1AllAttemptsManifestV1(
            quest_id=first_binding.action.quest_id,
            action_sha256=first_binding.action.object_sha256,
            protocol_sha256=protocol.protocol_sha256,
            work_order_node_sha256=node.node_sha256,
            attempts=expected_attempts,
            retained_at=self.all_attempts_manifest.retained_at,
        )
        if (
            self.all_attempts_manifest != expected_manifest
            or self.all_attempts_manifest_sha256 != expected_manifest.manifest_sha256
        ):
            raise ValueError("ARL-1 all-attempt manifest is not the exact replicate projection")
        expected_reproduction = ARL1ReproductionReceiptV1(
            campaign_registration_sha256=registration.campaign_registration_sha256,
            replicate_evidence_sha256s=tuple(item.evidence_sha256 for item in replicates),
            scientific_slot_ids=tuple(item.scientific_slot_id for item in replicates),
            committed_validation_receipt_sha256s=tuple(
                item.committed_validation.committed_receipt_sha256 for item in replicates
            ),
            scientific_observation_sha256s=tuple(
                item.scientific_observation_sha256 for item in replicates
            ),
            outcome=self.outcome,
            reproduced_at=self.reproduction_receipt.reproduced_at,
        )
        if (
            self.reproduction_receipt != expected_reproduction
            or self.reproduction_receipt_sha256 != expected_reproduction.receipt_sha256
        ):
            raise ValueError("ARL-1 reproduction receipt is not derived from every replicate")
        expected_reexecution_hashes = tuple(sorted(item.evidence_sha256 for item in replicates))
        if (
            self.all_attempt_count != len(self.exact_reexecution_evidence_sha256s)
            or self.exact_reexecution_evidence_sha256s
            != tuple(sorted(set(self.exact_reexecution_evidence_sha256s)))
            or self.exact_reexecution_evidence_sha256s != expected_reexecution_hashes
        ):
            raise ValueError("ARL-1 attempt count or reexecution evidence differs")
        if (
            len(
                {
                    *self.exact_reexecution_evidence_sha256s,
                    self.terminal_receipt_sha256,
                    self.artifact_manifest_sha256,
                    self.committed_validation_receipt_sha256,
                    self.committed_admission_sha256,
                    self.incorporation_event_sha256,
                }
            )
            != len(self.exact_reexecution_evidence_sha256s) + 5
        ):
            raise ValueError("ARL-1 evidence identities must be non-vacuous and distinct")
        if not (
            self.scientific_execution_authorized_at <= self.execution_started_at
            and self.validator_frozen_at <= self.execution_started_at
            and self.execution_started_at
            <= self.execution_completed_at
            <= self.validated_at
            <= self.admitted_at
            <= self.incorporated_at
            <= self.report.reported_at
        ):
            raise ValueError(
                "ARL-1 execution, validation, admission, and reporting are out of order"
            )
        if (
            self.scientific_execution_authorized_at != first_message.authorized_at
            or self.validator_frozen_at != first_message.admission_policy.frozen_at
            or self.execution_started_at
            != min(item.raw_run_custody.runtime_launched_at for item in replicates)
            or self.execution_completed_at
            != max(
                item.raw_run.accepted_runtime_termination.runtime_ended_at for item in replicates
            )
            or self.validated_at
            != max(item.committed_validation.message.committed_at for item in replicates)
            or self.admitted_at != admission.message.committed_at
            or self.incorporated_at != event.committed_at
            or self.all_attempts_manifest.retained_at < self.validated_at
            or self.reproduction_receipt.reproduced_at < self.validated_at
        ):
            raise ValueError("ARL-1 retained campaign chronology is not source-derived")
        expected_report = build_arl1_protocol_executor_report(
            quest_id=question.quest_id,
            question_ref=question,
            protocol_sha256=protocol.protocol_sha256,
            compilation_receipt_sha256=result.receipt.receipt_sha256,
            work_order_sha256=work_order.work_order_sha256,
            work_order_node_id=self.work_order_node_id,
            exact_reexecution_evidence_sha256s=self.exact_reexecution_evidence_sha256s,
            all_attempts_manifest_sha256=self.all_attempts_manifest_sha256,
            committed_validation_receipt_sha256=self.committed_validation_receipt_sha256,
            committed_admission_sha256=self.committed_admission_sha256,
            incorporation_event_sha256=self.incorporation_event_sha256,
            outcome=self.outcome,
            reproduction_receipt_sha256=self.reproduction_receipt_sha256,
            source_evidence_archive_manifest_sha256=(self.source_evidence_archive_manifest_sha256),
            reported_at=self.report.reported_at,
        )
        if self.report != expected_report:
            raise ValueError("ARL-1 report is not the deterministic view of campaign evidence")
        expected_id = f"arl1c_{self.identity_sha256[:32]}"
        if self.campaign_id is not None and self.campaign_id != expected_id:
            raise ValueError("ARL-1 campaign id differs from its evidence")
        object.__setattr__(self, "campaign_id", expected_id)
        return self

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"campaign_id"}))

    @property
    def campaign_sha256(self) -> str:
        return canonical_sha256(self)


def build_arl1_protocol_executor_report(
    *,
    quest_id: str,
    question_ref: KernelObjectRef,
    protocol_sha256: str,
    compilation_receipt_sha256: str,
    work_order_sha256: str,
    work_order_node_id: str,
    exact_reexecution_evidence_sha256s: tuple[str, ...],
    all_attempts_manifest_sha256: str,
    committed_validation_receipt_sha256: str,
    committed_admission_sha256: str,
    incorporation_event_sha256: str,
    outcome: ARL1Outcome,
    reproduction_receipt_sha256: str,
    source_evidence_archive_manifest_sha256: str,
    reported_at: datetime,
) -> ARL1ProtocolExecutorReportV1:
    return ARL1ProtocolExecutorReportV1(
        quest_id=quest_id,
        question_ref=question_ref,
        protocol_sha256=protocol_sha256,
        compilation_receipt_sha256=compilation_receipt_sha256,
        work_order_sha256=work_order_sha256,
        work_order_node_id=work_order_node_id,
        exact_reexecution_evidence_sha256s=exact_reexecution_evidence_sha256s,
        all_attempts_manifest_sha256=all_attempts_manifest_sha256,
        committed_validation_receipt_sha256=committed_validation_receipt_sha256,
        committed_admission_sha256=committed_admission_sha256,
        incorporation_event_sha256=incorporation_event_sha256,
        outcome=outcome,
        reproduction_receipt_sha256=reproduction_receipt_sha256,
        source_evidence_archive_manifest_sha256=source_evidence_archive_manifest_sha256,
        reported_at=reported_at,
    )


class ARL1QualificationPolicyV1(KernelModel):
    schema_name: Literal["aletheia.arl1_qualification_policy"] = (
        "aletheia.arl1_qualification_policy"
    )
    schema_version: Literal[1] = 1
    policy_id: str | None = Field(default=None, pattern=_POLICY_ID_PATTERN)
    target_deployment_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    target_observer_pin_sha256: str = Field(pattern=_SHA256_PATTERN)
    allowed_domain_scopes: tuple[_SymbolicId, ...] = Field(min_length=1, max_length=100)
    allowed_modality_scopes: tuple[_SymbolicId, ...] = Field(min_length=1, max_length=100)
    minimum_distinct_protocol_campaigns: int = Field(ge=1, le=100)
    required_arl0_gates: tuple[ARL0GateKind, ...] = _REQUIRED_ARL0_GATES
    evidence_verifier_principal_ids: tuple[_SymbolicId, ...] = Field(min_length=1, max_length=32)
    evidence_verifier_policy_sha256s: tuple[_Sha256, ...] = Field(min_length=1, max_length=32)
    evidence_verifier_pins: tuple[ARL1EvidenceVerifierPinV1, ...] = Field(
        min_length=1,
        max_length=32,
    )
    qualification_authority_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    qualification_authority_key_id: str = Field(pattern=_SHA256_PATTERN)
    qualification_authority_public_key_ed25519_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    frozen_at: AwareDatetime
    valid_from: AwareDatetime
    valid_until: AwareDatetime
    maximum_receipt_validity_seconds: int = Field(ge=60, le=31_536_000)
    claim_ceiling: Literal["bounded_protocol_execution_engineering"] = (
        "bounded_protocol_execution_engineering"
    )

    @model_validator(mode="after")
    def _policy_is_closed(self) -> "ARL1QualificationPolicyV1":
        for values, label in (
            (self.allowed_domain_scopes, "domain scopes"),
            (self.allowed_modality_scopes, "modality scopes"),
            (self.evidence_verifier_principal_ids, "evidence verifier principals"),
            (self.evidence_verifier_policy_sha256s, "evidence verifier policies"),
        ):
            if values != tuple(sorted(set(values))):
                raise ValueError(f"ARL-1 {label} must be unique and canonical")
        if self.evidence_verifier_pins != tuple(
            sorted(self.evidence_verifier_pins, key=lambda item: item.pin_sha256)
        ) or len({item.pin_sha256 for item in self.evidence_verifier_pins}) != len(
            self.evidence_verifier_pins
        ):
            raise ValueError("ARL-1 evidence verifier pins must be unique and canonical")
        if (
            tuple(sorted(item.principal_id for item in self.evidence_verifier_pins))
            != self.evidence_verifier_principal_ids
            or tuple(
                sorted(item.verification_policy_sha256 for item in self.evidence_verifier_pins)
            )
            != self.evidence_verifier_policy_sha256s
        ):
            raise ValueError("ARL-1 verifier principal/policy allowlists differ from public pins")
        if self.required_arl0_gates != _REQUIRED_ARL0_GATES:
            raise ValueError("ARL-1 policy cannot weaken lower-level integrity gates")
        if (
            self.qualification_authority_key_id
            != ed25519_key_id(self.qualification_authority_public_key_ed25519_hex)
            or self.qualification_authority_principal_id in self.evidence_verifier_principal_ids
            or self.qualification_authority_key_id
            in {item.key_id for item in self.evidence_verifier_pins}
            or not self.frozen_at <= self.valid_from < self.valid_until
        ):
            raise ValueError("ARL-1 authority identity, separation, or validity differs")
        expected = f"arl1p_{self.identity_sha256[:32]}"
        if self.policy_id is not None and self.policy_id != expected:
            raise ValueError("ARL-1 policy id differs from its contents")
        object.__setattr__(self, "policy_id", expected)
        return self

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"policy_id"}))

    @property
    def policy_sha256(self) -> str:
        return canonical_sha256(self)


class ARL1QualificationTrustAnchorV1(KernelModel):
    """Out-of-band deployment trust; never derived from an untrusted receipt at verification."""

    schema_name: Literal["aletheia.arl1_qualification_trust_anchor"] = (
        "aletheia.arl1_qualification_trust_anchor"
    )
    schema_version: Literal[1] = 1
    trust_anchor_id: str | None = Field(default=None, pattern=_TRUST_ANCHOR_ID_PATTERN)
    policy_id: str = Field(pattern=_POLICY_ID_PATTERN)
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    qualification_authority_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    qualification_authority_key_id: str = Field(pattern=_SHA256_PATTERN)
    qualification_authority_public_key_ed25519_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_verifier_pin_sha256s: tuple[_Sha256, ...] = Field(min_length=1, max_length=32)
    frozen_at: AwareDatetime

    @model_validator(mode="after")
    def _anchor_is_canonical(self) -> "ARL1QualificationTrustAnchorV1":
        if self.qualification_authority_key_id != ed25519_key_id(
            self.qualification_authority_public_key_ed25519_hex
        ) or self.evidence_verifier_pin_sha256s != tuple(
            sorted(set(self.evidence_verifier_pin_sha256s))
        ):
            raise ValueError("ARL-1 trust anchor key or verifier pins are noncanonical")
        expected = f"arl1t_{self.identity_sha256[:32]}"
        if self.trust_anchor_id is not None and self.trust_anchor_id != expected:
            raise ValueError("ARL-1 trust anchor id differs from its contents")
        object.__setattr__(self, "trust_anchor_id", expected)
        return self

    @classmethod
    def from_policy(
        cls,
        policy: ARL1QualificationPolicyV1,
    ) -> "ARL1QualificationTrustAnchorV1":
        return cls(
            policy_id=policy.policy_id,
            policy_sha256=policy.policy_sha256,
            qualification_authority_principal_id=(policy.qualification_authority_principal_id),
            qualification_authority_key_id=policy.qualification_authority_key_id,
            qualification_authority_public_key_ed25519_hex=(
                policy.qualification_authority_public_key_ed25519_hex
            ),
            evidence_verifier_pin_sha256s=tuple(
                sorted(item.pin_sha256 for item in policy.evidence_verifier_pins)
            ),
            frozen_at=policy.frozen_at,
        )

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"trust_anchor_id"}))

    @property
    def anchor_sha256(self) -> str:
        return canonical_sha256(self)


def verify_arl1_policy_trust_anchor(
    policy: ARL1QualificationPolicyV1,
    trust_anchor: ARL1QualificationTrustAnchorV1,
) -> ARL1QualificationPolicyV1:
    """Require exact equality with deployment-owned policy and signing authorities."""

    try:
        policy = ARL1QualificationPolicyV1.model_validate(policy.model_dump(mode="python"))
        anchor = ARL1QualificationTrustAnchorV1.model_validate(
            trust_anchor.model_dump(mode="python")
        )
        if (
            policy.policy_id != anchor.policy_id
            or policy.policy_sha256 != anchor.policy_sha256
            or policy.qualification_authority_principal_id
            != anchor.qualification_authority_principal_id
            or policy.qualification_authority_key_id != anchor.qualification_authority_key_id
            or policy.qualification_authority_public_key_ed25519_hex
            != anchor.qualification_authority_public_key_ed25519_hex
            or tuple(sorted(item.pin_sha256 for item in policy.evidence_verifier_pins))
            != anchor.evidence_verifier_pin_sha256s
            or policy.frozen_at != anchor.frozen_at
        ):
            raise ARL1QualificationError(
                "ARL-1 policy differs from the out-of-band qualification trust anchor"
            )
        return policy
    except ARL1QualificationError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise ARL1QualificationError("ARL-1 policy trust anchor is invalid") from exc


class ARL1SourceVerificationReceiptV1(KernelModel):
    schema_name: Literal["aletheia.arl1_source_verification_receipt"] = (
        "aletheia.arl1_source_verification_receipt"
    )
    schema_version: Literal[1] = 1
    receipt_id: str | None = Field(default=None, pattern=_VERIFICATION_ID_PATTERN)
    subject_kind: ARL1VerificationSubjectKind
    subject_sha256: str = Field(pattern=_SHA256_PATTERN)
    verification_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    verified_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    verification_key_id: str = Field(pattern=_SHA256_PATTERN)
    verified_at: AwareDatetime
    source_bytes_freshly_rehashed: Literal[True] = True
    source_authorities_replayed: Literal[True] = True
    blockers: tuple[str, ...] = ()
    passed: Literal[True] = True
    synthetic_verifier: Literal[False] = False
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)

    @model_validator(mode="after")
    def _verification_is_canonical(self) -> "ARL1SourceVerificationReceiptV1":
        if self.blockers:
            raise ValueError("a passing ARL-1 source verification cannot retain blockers")
        expected = f"arl1v_{self.identity_sha256[:32]}"
        if self.receipt_id is not None and self.receipt_id != expected:
            raise ValueError("ARL-1 source verification id differs from its contents")
        object.__setattr__(self, "receipt_id", expected)
        return self

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"receipt_id"}))

    @property
    def signature_message(self) -> bytes:
        return _SOURCE_VERIFICATION_SIGNATURE_CONTEXT + canonical_json_bytes(
            self.model_dump(
                mode="json",
                exclude={"receipt_id", "signature_ed25519_hex"},
            )
        )

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


def issue_arl1_source_verification_receipt(
    *,
    subject_kind: ARL1VerificationSubjectKind,
    subject_sha256: str,
    verifier_pin: ARL1EvidenceVerifierPinV1,
    verifier_private_key: bytes,
    verified_at: datetime,
) -> ARL1SourceVerificationReceiptV1:
    """Sign one fresh-byte, full-authority replay result under an independent verifier pin."""

    _require_utc(verified_at, "ARL-1 source verified_at")
    try:
        public_hex = ed25519_public_key_hex(verifier_private_key)
    except (TypeError, ValueError) as exc:
        raise ARL1QualificationError("ARL-1 verifier private key is invalid") from exc
    if (
        public_hex != verifier_pin.public_key_ed25519_hex
        or ed25519_key_id(public_hex) != verifier_pin.key_id
        or not verifier_pin.active_at(verified_at)
    ):
        raise ARL1QualificationError("ARL-1 verifier signer or validity differs from its pin")
    unsigned = ARL1SourceVerificationReceiptV1(
        subject_kind=subject_kind,
        subject_sha256=subject_sha256,
        verification_policy_sha256=verifier_pin.verification_policy_sha256,
        verified_by_principal_id=verifier_pin.principal_id,
        verification_key_id=verifier_pin.key_id,
        verified_at=verified_at,
        signature_ed25519_hex="0" * 128,
    )
    signature = (
        Ed25519PrivateKey.from_private_bytes(verifier_private_key)
        .sign(unsigned.signature_message)
        .hex()
    )
    return ARL1SourceVerificationReceiptV1.model_validate(
        unsigned.model_copy(
            update={"receipt_id": None, "signature_ed25519_hex": signature}
        ).model_dump(mode="python")
    )


def verify_arl1_source_verification_receipt(
    receipt: ARL1SourceVerificationReceiptV1,
    *,
    verifier_pin: ARL1EvidenceVerifierPinV1,
    observed_at: datetime,
) -> ARL1SourceVerificationReceiptV1:
    """Historically verify the exact independent source-replay signature."""

    try:
        _require_utc(observed_at, "ARL-1 source receipt observed_at")
        receipt = ARL1SourceVerificationReceiptV1.model_validate(receipt.model_dump(mode="python"))
        if (
            receipt.verification_policy_sha256 != verifier_pin.verification_policy_sha256
            or receipt.verified_by_principal_id != verifier_pin.principal_id
            or receipt.verification_key_id != verifier_pin.key_id
            or receipt.verified_at > observed_at
            or not verifier_pin.active_at(receipt.verified_at)
        ):
            raise ARL1QualificationError(
                "ARL-1 source receipt authority or chronology differs from its pin"
            )
        Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(verifier_pin.public_key_ed25519_hex)
        ).verify(bytes.fromhex(receipt.signature_ed25519_hex), receipt.signature_message)
        return receipt
    except ARL1QualificationError:
        raise
    except InvalidSignature as exc:
        raise ARL1QualificationError("ARL-1 source verification signature is invalid") from exc
    except (AttributeError, TypeError, ValueError) as exc:
        raise ARL1QualificationError(
            "ARL-1 source verification receipt failed closed verification"
        ) from exc


class ARL1EvidenceBundleV1(KernelModel):
    schema_name: Literal["aletheia.arl1_evidence_bundle"] = "aletheia.arl1_evidence_bundle"
    schema_version: Literal[1] = 1
    policy: ARL1QualificationPolicyV1
    arl0_integrity: ARL0IntegrityEvidenceV1
    target_campaign_request: QualificationTargetCampaignRequestV1
    target_campaign_receipt: QualificationTargetCampaignReceiptV1
    protocol_campaigns: tuple[ARL1ProtocolCampaignEvidenceV1, ...] = Field(
        min_length=1,
        max_length=100,
    )
    source_verification_receipts: tuple[ARL1SourceVerificationReceiptV1, ...] = Field(
        min_length=3,
        max_length=102,
    )
    evidence_archive_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    prepared_at: AwareDatetime
    qualification_candidate_only: Literal[True] = True
    scientific_authority_conferred: Literal[False] = False
    synthetic_evidence_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _bundle_is_closed(self) -> "ARL1EvidenceBundleV1":
        policy = self.policy
        request = self.target_campaign_request
        spec = request.observer_config.commissioning_request.installation_request.deployment_spec
        observer_pin = request.observer_config.observer_pin
        campaigns = self.protocol_campaigns
        if (
            spec.deployment_id != policy.target_deployment_id
            or observer_pin.pin_sha256 != policy.target_observer_pin_sha256
            or policy.frozen_at > request.requested_at
            or len(campaigns) < policy.minimum_distinct_protocol_campaigns
            or len({item.compilation_request.protocol.protocol_sha256 for item in campaigns})
            != len(campaigns)
            or tuple(item.campaign_id for item in campaigns)
            != tuple(sorted(item.campaign_id for item in campaigns))
        ):
            raise ValueError("ARL-1 target or protocol campaign scope differs from policy")
        if any(
            item.domain_scope not in policy.allowed_domain_scopes
            or item.modality_scope not in policy.allowed_modality_scopes
            or policy.frozen_at > item.execution_started_at
            or self.target_campaign_receipt.completed_at > item.execution_started_at
            for item in campaigns
        ):
            raise ValueError("ARL-1 protocol campaign is outside the qualified scope")
        operational_principals = {observer_pin.principal_id}
        operational_keys = {observer_pin.key_id}
        operational_policies = {observer_pin.policy_sha256}
        for campaign in campaigns:
            for replicate in campaign.replicate_executions:
                authorization = replicate.authorization.message
                binding = authorization.action_protocol_binding
                operational_principals.update(
                    (
                        authorization.authorized_by_principal_id,
                        authorization.validator_principal_id,
                        authorization.admission_principal_id,
                        binding.action_proposed_event.principal_id,
                        binding.action_authorized_event.principal_id,
                    )
                )
                operational_keys.update(
                    (
                        authorization.authorization_key_id,
                        authorization.validator_key_id,
                        authorization.admission_key_id,
                    )
                )
                operational_policies.update(
                    (
                        authorization.execution_authority_policy_sha256,
                        authorization.validator_authority_policy_sha256,
                        authorization.admission_authority_policy_sha256,
                    )
                )
                custody = replicate.raw_run_custody
                for authority in (
                    custody.allocator_authority,
                    custody.qualification_authority,
                    custody.node_enrollment_authority,
                    custody.node_execution_authority,
                    custody.runtime_control_authority,
                    custody.terminal_submission_authority,
                    custody.terminal_acceptance_authority,
                ):
                    operational_principals.add(authority.principal_id)
                    operational_keys.add(authority.key_id)
                    operational_policies.add(authority.policy_sha256)
                validation = replicate.committed_validation.message
                operational_principals.add(validation.committed_by_principal_id)
                operational_keys.add(validation.commit_key_id)
                operational_policies.add(validation.database_authority_policy_sha256)
            decision = campaign.committed_admission.message.decision.message
            operational_principals.add(decision.decided_by_principal_id)
            operational_keys.add(decision.decision_key_id)
            operational_policies.add(decision.admission_authority_policy_sha256)
            operational_principals.add(campaign.incorporation_event.principal_id)
        verifier_principals = {item.principal_id for item in policy.evidence_verifier_pins}
        verifier_keys = {item.key_id for item in policy.evidence_verifier_pins}
        verifier_policies = {
            item.verification_policy_sha256 for item in policy.evidence_verifier_pins
        }
        if (
            verifier_principals & operational_principals
            or verifier_keys & operational_keys
            or verifier_policies & operational_policies
            or policy.qualification_authority_principal_id in operational_principals
            or policy.qualification_authority_key_id in operational_keys
        ):
            raise ValueError(
                "ARL-1 source verifier or qualification signer overlaps an evaluated authority"
            )
        expected_subjects = {
            (ARL1VerificationSubjectKind.ARL0_INTEGRITY, self.arl0_integrity.integrity_sha256),
            (
                ARL1VerificationSubjectKind.EVIDENCE_ARCHIVE,
                self.evidence_archive_manifest_sha256,
            ),
            *(
                (ARL1VerificationSubjectKind.PROTOCOL_CAMPAIGN, item.campaign_sha256)
                for item in campaigns
            ),
        }
        actual_subjects = {
            (item.subject_kind, item.subject_sha256) for item in self.source_verification_receipts
        }
        if (
            actual_subjects != expected_subjects
            or len(actual_subjects) != len(self.source_verification_receipts)
            or tuple(
                (item.subject_kind.value, item.subject_sha256)
                for item in self.source_verification_receipts
            )
            != tuple(
                sorted(
                    (item.subject_kind.value, item.subject_sha256)
                    for item in self.source_verification_receipts
                )
            )
        ):
            raise ValueError("ARL-1 source verification coverage is incomplete or noncanonical")
        if any(
            item.verified_by_principal_id not in policy.evidence_verifier_principal_ids
            or item.verification_policy_sha256 not in policy.evidence_verifier_policy_sha256s
            or item.verified_at > self.prepared_at
            for item in self.source_verification_receipts
        ):
            raise ValueError("ARL-1 source verification authority or chronology differs")
        pins = {
            (item.principal_id, item.key_id, item.verification_policy_sha256): item
            for item in policy.evidence_verifier_pins
        }
        for receipt in self.source_verification_receipts:
            pin = pins.get(
                (
                    receipt.verified_by_principal_id,
                    receipt.verification_key_id,
                    receipt.verification_policy_sha256,
                )
            )
            if pin is None:
                raise ValueError("ARL-1 source verification has no exact public authority pin")
            try:
                verify_arl1_source_verification_receipt(
                    receipt,
                    verifier_pin=pin,
                    observed_at=self.prepared_at,
                )
            except ARL1QualificationError as exc:
                raise ValueError("ARL-1 source verification signature is invalid") from exc
        campaign_by_sha256 = {item.campaign_sha256: item for item in campaigns}
        if any(
            (
                item.subject_kind is ARL1VerificationSubjectKind.ARL0_INTEGRITY
                and item.verified_at < self.arl0_integrity.completed_at
            )
            or (
                item.subject_kind is ARL1VerificationSubjectKind.PROTOCOL_CAMPAIGN
                and item.verified_at < campaign_by_sha256[item.subject_sha256].report.reported_at
            )
            for item in self.source_verification_receipts
        ):
            raise ValueError("ARL-1 source verification predates its completed evidence")
        if (
            self.arl0_integrity.completed_at > self.prepared_at
            or self.target_campaign_receipt.completed_at > self.prepared_at
            or any(item.report.reported_at > self.prepared_at for item in campaigns)
        ):
            raise ValueError("ARL-1 evidence bundle was prepared before its evidence completed")
        if any(
            item.verified_by_principal_id not in policy.evidence_verifier_principal_ids
            for item in self.arl0_integrity.gates
        ):
            raise ValueError("ARL-0 gate verifier is outside the ARL-1 policy")
        return self

    @property
    def bundle_sha256(self) -> str:
        return canonical_sha256(self)


class ARL1EvidenceVerificationPort(Protocol):
    """Independent fresh-byte and authority replay required before qualification signing."""

    def verify_arl0_integrity(
        self,
        *,
        evidence: ARL0IntegrityEvidenceV1,
        policy: ARL1QualificationPolicyV1,
        retained_receipt: ARL1SourceVerificationReceiptV1,
    ) -> ARL1SourceVerificationReceiptV1: ...

    def verify_protocol_campaign(
        self,
        *,
        evidence: ARL1ProtocolCampaignEvidenceV1,
        policy: ARL1QualificationPolicyV1,
        retained_receipt: ARL1SourceVerificationReceiptV1,
    ) -> ARL1SourceVerificationReceiptV1: ...

    def verify_evidence_archive(
        self,
        *,
        bundle: ARL1EvidenceBundleV1,
        retained_receipt: ARL1SourceVerificationReceiptV1,
    ) -> ARL1SourceVerificationReceiptV1: ...


def verify_arl1_evidence_bundle(
    bundle: ARL1EvidenceBundleV1,
    *,
    source_verifier: ARL1EvidenceVerificationPort,
    trust_anchor: ARL1QualificationTrustAnchorV1,
) -> ARL1EvidenceBundleV1:
    """Revalidate every native contract and compare fresh independent verification receipts."""

    try:
        bundle = ARL1EvidenceBundleV1.model_validate(bundle.model_dump(mode="python"))
        verify_arl1_policy_trust_anchor(bundle.policy, trust_anchor)
        verify_qualification_target_campaign_receipt(
            bundle.target_campaign_request,
            bundle.target_campaign_receipt,
        )
        expected = {
            (item.subject_kind, item.subject_sha256): item
            for item in bundle.source_verification_receipts
        }
        expected_key = (
            ARL1VerificationSubjectKind.ARL0_INTEGRITY,
            bundle.arl0_integrity.integrity_sha256,
        )
        observed = source_verifier.verify_arl0_integrity(
            evidence=bundle.arl0_integrity,
            policy=bundle.policy,
            retained_receipt=expected[expected_key],
        )
        if observed != expected.get(expected_key):
            raise ARL1QualificationError("fresh ARL-0 verification differs from retained receipt")
        archive_key = (
            ARL1VerificationSubjectKind.EVIDENCE_ARCHIVE,
            bundle.evidence_archive_manifest_sha256,
        )
        observed = source_verifier.verify_evidence_archive(
            bundle=bundle,
            retained_receipt=expected[archive_key],
        )
        if observed != expected.get(archive_key):
            raise ARL1QualificationError(
                "fresh evidence-archive verification differs from retained receipt"
            )
        for campaign in bundle.protocol_campaigns:
            expected_key = (
                ARL1VerificationSubjectKind.PROTOCOL_CAMPAIGN,
                campaign.campaign_sha256,
            )
            observed = source_verifier.verify_protocol_campaign(
                evidence=campaign,
                policy=bundle.policy,
                retained_receipt=expected[expected_key],
            )
            if observed != expected.get(expected_key):
                raise ARL1QualificationError(
                    "fresh protocol-campaign verification differs from retained receipt"
                )
        return bundle
    except ARL1QualificationError:
        raise
    except QualificationCampaignError as exc:
        raise ARL1QualificationError("target-host qualification campaign failed replay") from exc
    except (AttributeError, OSError, TypeError, ValueError, RuntimeError) as exc:
        raise ARL1QualificationError("ARL-1 evidence bundle failed closed verification") from exc


def _require_utc(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ARL1QualificationError(f"{label} must be timezone-aware UTC")


class ARL1QualificationReceiptMessageV1(KernelModel):
    schema_name: Literal["aletheia.arl1_qualification_receipt_message"] = (
        "aletheia.arl1_qualification_receipt_message"
    )
    schema_version: Literal[1] = 1
    policy_id: str = Field(pattern=_POLICY_ID_PATTERN)
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    evidence_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    arl0_integrity_sha256: str = Field(pattern=_SHA256_PATTERN)
    target_campaign_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    protocol_campaign_sha256s: tuple[_Sha256, ...] = Field(min_length=1, max_length=100)
    qualified_scopes: tuple[_SymbolicId, ...] = Field(min_length=1, max_length=100)
    evidence_archive_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    qualified_at: AwareDatetime
    expires_at: AwareDatetime
    qualified_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    qualification_key_id: str = Field(pattern=_SHA256_PATTERN)
    autonomy_level: Literal["ARL-1 Protocol Executor"] = "ARL-1 Protocol Executor"
    cumulative_arl0_satisfied: Literal[True] = True
    reliable_given_protocol_execution: Literal[True] = True
    predefined_validation_satisfied: Literal[True] = True
    exact_reproduction_satisfied: Literal[True] = True
    deterministic_reporting_satisfied: Literal[True] = True
    claim_ceiling: Literal["bounded_protocol_execution_engineering"] = (
        "bounded_protocol_execution_engineering"
    )
    autonomous_research_design_claimed: Literal[False] = False
    scientific_validity_claimed: Literal[False] = False
    independent_replication_claimed: Literal[False] = False
    scientific_authority_conferred: Literal[False] = False


class ARL1QualificationReceiptV1(KernelModel):
    schema_name: Literal["aletheia.arl1_qualification_receipt"] = (
        "aletheia.arl1_qualification_receipt"
    )
    schema_version: Literal[1] = 1
    receipt_id: str | None = Field(default=None, pattern=_RECEIPT_ID_PATTERN)
    message: ARL1QualificationReceiptMessageV1
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)

    @model_validator(mode="after")
    def _receipt_identity_is_exact(self) -> "ARL1QualificationReceiptV1":
        expected = f"arl1r_{self.identity_sha256[:32]}"
        if self.receipt_id is not None and self.receipt_id != expected:
            raise ValueError("ARL-1 receipt id differs from its signed contents")
        object.__setattr__(self, "receipt_id", expected)
        return self

    @property
    def signature_message(self) -> bytes:
        return _SIGNATURE_CONTEXT + canonical_json_bytes(self.message)

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(
            {
                "message": self.message.model_dump(mode="json"),
                "signature_ed25519_hex": self.signature_ed25519_hex,
            }
        )

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


def _receipt_message(
    bundle: ARL1EvidenceBundleV1,
    *,
    qualified_at: datetime,
    expires_at: datetime,
) -> ARL1QualificationReceiptMessageV1:
    policy = bundle.policy
    scopes = tuple(
        sorted({f"{item.domain_scope}:{item.modality_scope}" for item in bundle.protocol_campaigns})
    )
    return ARL1QualificationReceiptMessageV1(
        policy_id=policy.policy_id,
        policy_sha256=policy.policy_sha256,
        evidence_bundle_sha256=bundle.bundle_sha256,
        arl0_integrity_sha256=bundle.arl0_integrity.integrity_sha256,
        target_campaign_receipt_sha256=bundle.target_campaign_receipt.receipt_sha256,
        protocol_campaign_sha256s=tuple(item.campaign_sha256 for item in bundle.protocol_campaigns),
        qualified_scopes=scopes,
        evidence_archive_manifest_sha256=bundle.evidence_archive_manifest_sha256,
        qualified_at=qualified_at,
        expires_at=expires_at,
        qualified_by_principal_id=policy.qualification_authority_principal_id,
        qualification_key_id=policy.qualification_authority_key_id,
    )


def issue_arl1_qualification(
    bundle: ARL1EvidenceBundleV1,
    *,
    source_verifier: ARL1EvidenceVerificationPort,
    trust_anchor: ARL1QualificationTrustAnchorV1,
    qualification_private_key: bytes,
    receipt_validity_seconds: int,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> ARL1QualificationReceiptV1:
    """Issue ARL-1 only after native replay plus independent fresh-source verification."""

    bundle = verify_arl1_evidence_bundle(
        bundle,
        source_verifier=source_verifier,
        trust_anchor=trust_anchor,
    )
    policy = bundle.policy
    try:
        qualified_at = clock()
    except Exception as exc:  # noqa: BLE001 - qualification clock is an authority boundary
        raise ARL1QualificationError("ARL-1 qualification clock failed closed") from exc
    _require_utc(qualified_at, "ARL-1 qualified_at")
    if (
        isinstance(receipt_validity_seconds, bool)
        or not isinstance(receipt_validity_seconds, int)
        or receipt_validity_seconds < 60
    ):
        raise ARL1QualificationError("ARL-1 receipt validity is invalid")
    expires_at = qualified_at + timedelta(seconds=receipt_validity_seconds)
    try:
        public_hex = ed25519_public_key_hex(qualification_private_key)
    except (TypeError, ValueError) as exc:
        raise ARL1QualificationError("ARL-1 qualification private key is invalid") from exc
    if (
        public_hex != policy.qualification_authority_public_key_ed25519_hex
        or ed25519_key_id(public_hex) != policy.qualification_authority_key_id
        or not policy.valid_from <= qualified_at < expires_at <= policy.valid_until
        or qualified_at < bundle.prepared_at
        or receipt_validity_seconds > policy.maximum_receipt_validity_seconds
    ):
        raise ARL1QualificationError("ARL-1 signer, chronology, or validity differs from policy")
    message = _receipt_message(bundle, qualified_at=qualified_at, expires_at=expires_at)
    unsigned = ARL1QualificationReceiptV1(
        message=message,
        signature_ed25519_hex="0" * 128,
    )
    signature = (
        Ed25519PrivateKey.from_private_bytes(qualification_private_key)
        .sign(unsigned.signature_message)
        .hex()
    )
    receipt = unsigned.model_copy(update={"signature_ed25519_hex": signature, "receipt_id": None})
    return ARL1QualificationReceiptV1.model_validate(receipt.model_dump(mode="python"))


def verify_arl1_qualification(
    bundle: ARL1EvidenceBundleV1,
    receipt: ARL1QualificationReceiptV1,
    *,
    source_verifier: ARL1EvidenceVerificationPort,
    trust_anchor: ARL1QualificationTrustAnchorV1,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> ARL1QualificationReceiptV1:
    """Freshly reverify evidence, receipt derivation, validity, and qualification signature."""

    try:
        bundle = verify_arl1_evidence_bundle(
            bundle,
            source_verifier=source_verifier,
            trust_anchor=trust_anchor,
        )
        try:
            observed_at = clock()
        except Exception as exc:  # noqa: BLE001 - audit clock is an authority boundary
            raise ARL1QualificationError("ARL-1 verification clock failed closed") from exc
        _require_utc(observed_at, "ARL-1 observed_at")
        receipt = ARL1QualificationReceiptV1.model_validate(receipt.model_dump(mode="python"))
        policy = bundle.policy
        expected = _receipt_message(
            bundle,
            qualified_at=receipt.message.qualified_at,
            expires_at=receipt.message.expires_at,
        )
        if receipt.message != expected:
            raise ARL1QualificationError("ARL-1 receipt rebound its evidence or policy")
        if (
            not policy.valid_from
            <= receipt.message.qualified_at
            < receipt.message.expires_at
            <= policy.valid_until
            or receipt.message.qualified_at < bundle.prepared_at
            or receipt.message.expires_at - receipt.message.qualified_at
            > timedelta(seconds=policy.maximum_receipt_validity_seconds)
        ):
            raise ARL1QualificationError("ARL-1 receipt chronology or validity differs from policy")
        if not receipt.message.qualified_at <= observed_at < receipt.message.expires_at:
            raise ARL1QualificationError("ARL-1 receipt is not active at observation time")
        Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(policy.qualification_authority_public_key_ed25519_hex)
        ).verify(bytes.fromhex(receipt.signature_ed25519_hex), receipt.signature_message)
        return receipt
    except ARL1QualificationError:
        raise
    except InvalidSignature as exc:
        raise ARL1QualificationError("ARL-1 qualification signature is invalid") from exc
    except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
        raise ARL1QualificationError("ARL-1 receipt failed closed verification") from exc


__all__ = [
    "ARL0GateEvidenceV1",
    "ARL0GateKind",
    "ARL0IntegrityEvidenceV1",
    "ARL1AllAttemptsManifestV1",
    "ARL1ArchiveManifestKind",
    "ARL1AttemptEvidenceRefV1",
    "ARL1EvidenceArchiveEntryV1",
    "ARL1EvidenceArchiveManifestV1",
    "ARL1EvidenceBundleV1",
    "ARL1EvidenceVerificationPort",
    "ARL1EvidenceVerifierPinV1",
    "ARL1Outcome",
    "ARL1ProtocolCampaignEvidenceV1",
    "ARL1ProtocolExecutorReportV1",
    "ARL1QualificationError",
    "ARL1QualificationPolicyV1",
    "ARL1QualificationReceiptMessageV1",
    "ARL1QualificationReceiptV1",
    "ARL1QualificationTrustAnchorV1",
    "ARL1ReplicateExecutionEvidenceV1",
    "ARL1ReproductionReceiptV1",
    "ARL1SourceVerificationReceiptV1",
    "ARL1VerificationSubjectKind",
    "build_arl1_protocol_executor_report",
    "issue_arl1_qualification",
    "issue_arl1_source_verification_receipt",
    "verify_arl1_evidence_bundle",
    "verify_arl1_qualification",
    "verify_arl1_policy_trust_anchor",
    "verify_arl1_source_verification_receipt",
]
