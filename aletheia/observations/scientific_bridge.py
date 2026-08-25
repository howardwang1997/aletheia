"""Pure signed contracts bridging engineering execution to scientific observation.

PR-4 qualification proves that frozen work is safe to execute.  Its contracts deliberately say
``qualification_only=True`` and ``scientific_admission_allowed=False``.  This module does not
change those facts.  It adds a separately signed, preregistered scientific authorization, binds
one exact Research Kernel action to one exact protocol/WorkOrder slot, and requires an independent
validation signature before an admission authority may sign a decision.

The contracts below verify canonical structure and bridge signatures only.  They do not read a
Research Kernel event store, re-run the complete PR-4 runtime/node proof, inspect CAS bytes, reserve
a database slot, or provide exactly-once delivery.  Explicit adapter ports at the bottom mark those
future authority boundaries.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from enum import Enum
from typing import Literal, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from aletheia.execution.runtime_contracts import (
    EngineeringQualificationBundle,
    EngineeringQualificationGrant,
    QualificationAuthorityVerifier,
    VerifiedEngineeringQualification,
    WorkerNodeEnrollment,
    WorkerNodeManifest,
    artifact_output_tree_sha256,
)
from aletheia.execution.runtime_v2_contracts import (
    AcceptedQualificationTerminalSubmission,
    AcceptedRuntimeTermination,
    QualificationTerminalSubmission,
)
from aletheia.execution.schemas import (
    ArtifactManifest,
    ArtifactRole,
    ArtifactVerifiedReceipt,
    ExecutionIntent,
    ExecutionRetryMode,
    ExpectedArtifact,
    ScientificReplicateSlot,
    canonical_sha256 as execution_canonical_sha256,
)
from aletheia.protocols.compiler import (
    ProtocolCompilationRequest,
    verify_compilation,
    verify_execution_intent_binding,
)
from aletheia.protocols.claim_contracts import ObservableSpec
from aletheia.protocols.schemas import (
    CompilationReceipt,
    ObservableOutputBinding,
    ProtocolDataPort,
    ProtocolCompilationResult,
    ProtocolPortDirection,
    ProtocolStep,
    WorkOrderDAG,
    WorkOrderNode,
)
from aletheia.research_kernel.schemas import (
    ActionAuthorizedPayload,
    ActionProposedPayload,
    EventType,
    ResearchActionProposal,
    ResearchEvent,
    canonical_json_bytes,
    canonical_sha256,
)

SCIENTIFIC_BRIDGE_SCHEMA_VERSION = 1

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SIGNATURE_PATTERN = r"^[0-9a-f]{128}$"
_SYMBOLIC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$"
_LOCAL_ID_PATTERN = r"^[a-z][a-z0-9_.-]{1,79}$"
_BRIDGE_SIGNATURE_CONTEXT = b"aletheia.scientific_bridge.ed25519.v1\0"


class ScientificBridgeModel(BaseModel):
    """Closed, immutable base model for pure bridge contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def _datetimes_are_canonical_utc(self) -> "ScientificBridgeModel":
        for field_name in type(self).model_fields:
            value = getattr(self, field_name)
            if isinstance(value, datetime) and (
                value.tzinfo is None or value.utcoffset() != timedelta(0)
            ):
                raise ValueError(f"{field_name} must be timezone-aware UTC")
        return self


class ScientificBridgeRole(str, Enum):
    EXECUTION_AUTHORIZER = "scientific_execution_authorizer"
    OBSERVATION_VALIDATOR = "observation_validator"
    OBSERVATION_ADMITTER = "observation_admitter"


class ScientificObservationOutcome(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    INCONCLUSIVE = "inconclusive"


class BridgeValidationDisposition(str, Enum):
    """Projection of the existing F9 campaign disposition, not a scientific rule engine."""

    VALIDATED_CONFIRMATION = "validated_confirmation"
    REJECTED_SCIENTIFIC = "rejected_scientific"
    BLOCKED_EXECUTION = "blocked_execution"


class ObservationAdmissionDisposition(str, Enum):
    ADMITTED = "admitted"
    REJECTED = "rejected"


def scientific_observation_row_scope(scientific_slot_id: str) -> str:
    """Return the one canonical database row scope for a scientific slot."""

    return f"observation:{scientific_slot_id}"


def scientific_bridge_key_id(public_key_ed25519_hex: str) -> str:
    """Return the key identity used by externally pinned bridge authorities."""

    try:
        raw = bytes.fromhex(public_key_ed25519_hex)
    except ValueError as exc:
        raise ValueError("bridge public key must be lowercase hexadecimal") from exc
    if len(raw) != 32 or public_key_ed25519_hex != public_key_ed25519_hex.lower():
        raise ValueError("bridge public key must contain exactly 32 lowercase-hex bytes")
    return hashlib.sha256(raw).hexdigest()


class ScientificBridgeAuthorityPin(ScientificBridgeModel):
    """Deployment-owned trust input; signed payloads never select their verification key."""

    schema_name: Literal["aletheia.scientific_bridge_authority_pin"] = (
        "aletheia.scientific_bridge_authority_pin"
    )
    schema_version: Literal[1] = SCIENTIFIC_BRIDGE_SCHEMA_VERSION
    role: ScientificBridgeRole
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    key_id: str = Field(pattern=_SHA256_PATTERN)
    public_key_ed25519_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _pin_is_canonical(self) -> "ScientificBridgeAuthorityPin":
        if self.key_id != scientific_bridge_key_id(self.public_key_ed25519_hex):
            raise ValueError("bridge key id does not match its public key")
        if self.expires_at <= self.valid_from:
            raise ValueError("bridge key expiry must follow validity start")
        if self.revoked_at is not None and not (
            self.valid_from <= self.revoked_at <= self.expires_at
        ):
            raise ValueError("bridge key revocation must fall inside its validity window")
        return self

    @property
    def active_until(self) -> datetime:
        return min(self.expires_at, self.revoked_at or self.expires_at)

    def active_at(self, timestamp: datetime) -> bool:
        return self.valid_from <= timestamp < self.active_until


class ObservationDatabaseAuthorityPin(ScientificBridgeModel):
    """Deployment-owned database/time trust input for challenges and commit proofs."""

    schema_name: Literal["aletheia.observation_database_authority_pin"] = (
        "aletheia.observation_database_authority_pin"
    )
    schema_version: Literal[1] = SCIENTIFIC_BRIDGE_SCHEMA_VERSION
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    key_id: str = Field(pattern=_SHA256_PATTERN)
    public_key_ed25519_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _pin_is_canonical(self) -> "ObservationDatabaseAuthorityPin":
        if self.key_id != scientific_bridge_key_id(self.public_key_ed25519_hex):
            raise ValueError("observation database key id does not match its public key")
        if self.expires_at <= self.valid_from:
            raise ValueError("observation database key expiry must follow validity start")
        if self.revoked_at is not None and not (
            self.valid_from <= self.revoked_at <= self.expires_at
        ):
            raise ValueError("observation database revocation is outside its validity window")
        return self

    @property
    def active_until(self) -> datetime:
        return min(self.expires_at, self.revoked_at or self.expires_at)

    def active_at(self, timestamp: datetime) -> bool:
        return self.valid_from <= timestamp < self.active_until


class VerifiedExecutionAuthorityProjection(ScientificBridgeModel):
    """Typed identity returned by a mandatory custody adapter, never caller authority."""

    principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    key_id: str = Field(pattern=_SHA256_PATTERN)
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)


class VerifiedArtifactCustodyProjection(ScientificBridgeModel):
    artifact_key: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_verified_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    authority: VerifiedExecutionAuthorityProjection


class ScientificOutcomeBinMapping(ScientificBridgeModel):
    outcome_bin_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    outcome: ScientificObservationOutcome


class ObservationAdmissionPolicy(ScientificBridgeModel):
    """Frozen bridge policy mapping existing F9 outcome bins into Kernel evidence classes."""

    schema_name: Literal["aletheia.observation_admission_policy"] = (
        "aletheia.observation_admission_policy"
    )
    schema_version: Literal[1] = SCIENTIFIC_BRIDGE_SCHEMA_VERSION
    policy_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    validator_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation_validation_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    analysis_outcome_space_sha256: str = Field(pattern=_SHA256_PATTERN)
    outcome_bin_mappings: tuple[ScientificOutcomeBinMapping, ...] = Field(
        min_length=1,
        max_length=128,
    )
    maximum_admissions_per_scientific_slot: Literal[1] = 1
    require_independent_validation: Literal[True] = True
    qualification_terminal_failure_is_scientific_outcome: Literal[False] = False
    qualification_artifact_can_self_admit: Literal[False] = False
    frozen_at: AwareDatetime

    @model_validator(mode="after")
    def _mappings_are_canonical(self) -> "ObservationAdmissionPolicy":
        ids = tuple(item.outcome_bin_id for item in self.outcome_bin_mappings)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("outcome-bin mappings must be unique and canonically ordered")
        return self

    @property
    def policy_sha256(self) -> str:
        return canonical_sha256(self)


class VerifiedObservationValidationCampaignProjection(ScientificBridgeModel):
    """Closed output of the mandatory external F9 campaign/archive verifier.

    This projection does not reproduce F9 validation rules.  It carries only the exact identities
    and derived disposition needed for bridge signing and is authoritative only when returned by
    ``ObservationValidationCampaignVerificationPort``.
    """

    schema_name: Literal["aletheia.verified_observation_validation_campaign_projection"] = (
        "aletheia.verified_observation_validation_campaign_projection"
    )
    schema_version: Literal[1] = SCIENTIFIC_BRIDGE_SCHEMA_VERSION
    observation_staging_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    validation_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    committed_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    validation_batch_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    validator_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation_validation_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation_namespace_sha256: str = Field(pattern=_SHA256_PATTERN)
    protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    selection_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_commitment_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_commitment_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    namespace_seal_sha256: str = Field(pattern=_SHA256_PATTERN)
    raw_run_sha256: str = Field(pattern=_SHA256_PATTERN)
    scientific_observation_artifact_binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_verified_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    raw_observation_content_sha256: str = Field(pattern=_SHA256_PATTERN)
    outcome_bin_id: str | None = Field(default=None, pattern=_LOCAL_ID_PATTERN)
    disposition: BridgeValidationDisposition
    blocker_codes: tuple[str, ...] = Field(max_length=256)
    generated_at: AwareDatetime
    committed_at: AwareDatetime

    @model_validator(mode="after")
    def _projection_is_closed(self) -> "VerifiedObservationValidationCampaignProjection":
        if self.blocker_codes != tuple(sorted(set(self.blocker_codes))):
            raise ValueError("validation campaign blockers must be unique and canonical")
        if self.committed_at < self.generated_at:
            raise ValueError("validation campaign commitment predates campaign generation")
        if self.disposition is BridgeValidationDisposition.VALIDATED_CONFIRMATION:
            if (
                self.validation_batch_sha256 is None
                or self.outcome_bin_id is None
                or self.blocker_codes
            ):
                raise ValueError(
                    "validated campaign projection requires batch/outcome and no blockers"
                )
        elif self.disposition is BridgeValidationDisposition.REJECTED_SCIENTIFIC:
            if (
                self.validation_batch_sha256 is None
                or self.outcome_bin_id is None
                or not self.blocker_codes
            ):
                raise ValueError("scientifically rejected campaign requires batch/outcome/blockers")
        elif (
            self.validation_batch_sha256 is not None
            or self.outcome_bin_id is not None
            or not self.blocker_codes
        ):
            raise ValueError("blocked campaign projection requires blockers but no batch/outcome")
        return self

    @property
    def projection_sha256(self) -> str:
        return canonical_sha256(self)


class ScientificObservationArtifactBinding(ScientificBridgeModel):
    """Exact preregistered observable producer and raw artifact relation."""

    schema_name: Literal["aletheia.scientific_observation_artifact_binding"] = (
        "aletheia.scientific_observation_artifact_binding"
    )
    schema_version: Literal[1] = SCIENTIFIC_BRIDGE_SCHEMA_VERSION
    work_order_node: WorkOrderNode
    protocol_step: ProtocolStep
    observable_output_binding: ObservableOutputBinding
    observable: ObservableSpec
    data_port: ProtocolDataPort
    expected_artifact: ExpectedArtifact
    observation_namespace_sha256: str = Field(pattern=_SHA256_PATTERN)
    selection_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_commitment_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _artifact_is_the_exact_observable_output(self) -> "ScientificObservationArtifactBinding":
        node = self.work_order_node
        step = self.protocol_step
        binding = self.observable_output_binding
        observable = self.observable
        port = self.data_port
        expected = self.expected_artifact
        if (
            node.protocol_step_id != step.step_id
            or node.role is not step.role
            or binding.producer_step_id != step.step_id
            or binding.output_port_id != port.port_id
            or binding.observable_spec_sha256 != observable.observable_sha256
            or port.port_id not in step.output_port_ids
            or port.port_id not in node.output_port_ids
            or port.direction is ProtocolPortDirection.INPUT
        ):
            raise ValueError("scientific observation escaped its exact observable producer")
        if (
            expected.artifact_key != port.port_id
            or expected.role is not ArtifactRole.RAW_OUTPUT
            or not expected.required
            or expected.schema_sha256 is None
            or expected.schema_sha256 != port.schema_ref.schema_sha256
            or expected.schema_sha256 != observable.output_schema_sha256
            or expected.data_classification != port.data_classification.value
            or port.identity_schema_sha256 != observable.entity_identity_schema_sha256
            or port.unit_or_ontology_sha256 != observable.unit_or_ontology_sha256
        ):
            raise ValueError(
                "scientific artifact role/schema/unit/identity differs from its observable port"
            )
        return self

    @property
    def artifact_key(self) -> str:
        return self.data_port.port_id

    @property
    def artifact_media_type(self) -> str:
        return self.expected_artifact.media_type

    @property
    def binding_sha256(self) -> str:
        return canonical_sha256(self)


class ScientificActionProtocolBinding(ScientificBridgeModel):
    """Closed action -> protocol -> WorkOrder node -> one preregistered slot binding."""

    schema_name: Literal["aletheia.scientific_action_protocol_binding"] = (
        "aletheia.scientific_action_protocol_binding"
    )
    schema_version: Literal[1] = SCIENTIFIC_BRIDGE_SCHEMA_VERSION
    action: ResearchActionProposal
    action_proposed_event: ResearchEvent
    action_authorized_event: ResearchEvent
    authorized_graph_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    compilation_request: ProtocolCompilationRequest
    compilation_result: ProtocolCompilationResult
    compilation_receipt: CompilationReceipt
    work_order: WorkOrderDAG
    work_order_node: WorkOrderNode
    replicate_slot: ScientificReplicateSlot
    bound_at: AwareDatetime

    @model_validator(mode="after")
    def _binding_is_exact(self) -> "ScientificActionProtocolBinding":
        action = self.action
        proposed = self.action_proposed_event
        authorized = self.action_authorized_event
        if (
            proposed.event_type is not EventType.ACTION_PROPOSED
            or not isinstance(proposed.payload, ActionProposedPayload)
            or proposed.payload.action_ref != action.object_ref
        ):
            raise ValueError("action proposal event does not bind the exact action")
        if (
            authorized.event_type is not EventType.ACTION_AUTHORIZED
            or not isinstance(authorized.payload, ActionAuthorizedPayload)
            or authorized.payload.action_id != action.action_id
        ):
            raise ValueError("action authorization event does not bind the exact action")
        if (
            proposed.quest_id != action.quest_id
            or authorized.quest_id != action.quest_id
            or proposed.principal_id != action.proposed_by_principal_id
            or proposed.payload.branch_id != authorized.payload.branch_id
            or authorized.sequence != proposed.sequence + 1
            or authorized.parent_event_sha256 != proposed.event_sha256
            or action.basis_tail_event_sha256 != proposed.parent_event_sha256
        ):
            raise ValueError("action event lineage, branch, or basis tail is not exact")
        if action.proposed_by_principal_id == authorized.principal_id:
            raise ValueError("action proposer and action authorizer principals must be separate")
        if not (action.proposed_at <= proposed.committed_at <= authorized.committed_at):
            raise ValueError("action proposal and authorization times are out of order")

        request = self.compilation_request
        result = self.compilation_result
        protocol = request.protocol
        scope = protocol.graph_scope
        if (
            scope.scope_binding.quest_id != action.quest_id
            or scope.branch_id != proposed.payload.branch_id
            or scope.question_ref != action.question_ref
            or scope.graph_snapshot_sha256 != self.authorized_graph_snapshot_sha256
        ):
            raise ValueError("protocol graph scope differs from the authorized action scope")
        if (
            result.work_order is None
            or result.work_order != self.work_order
            or result.receipt != self.compilation_receipt
            or self.compilation_receipt.work_order_sha256 != self.work_order.work_order_sha256
        ):
            raise ValueError("binding compilation does not contain the exact accepted WorkOrder")
        if (
            self.work_order.quest_id != action.quest_id
            or self.work_order.graph_scope_sha256 != scope.graph_scope_sha256
            or self.work_order.protocol_sha256 != protocol.protocol_sha256
        ):
            raise ValueError("WorkOrder escaped its action-bound protocol scope")
        nodes = tuple(
            item for item in self.work_order.nodes if item.node_id == self.work_order_node.node_id
        )
        if nodes != (self.work_order_node,):
            raise ValueError("scientific WorkOrder node must resolve exactly once")
        node = self.work_order_node
        slot = self.replicate_slot
        if (
            slot.quest_id != self.work_order.quest_id
            or slot.protocol_sha256 != self.work_order.protocol_sha256
            or slot.work_order_id != self.work_order.work_order_id
            or slot.work_order_node_id != node.node_id
            or slot.work_order_node_sha256 != node.node_sha256
            or slot.slot_count != node.scientific_replicate_count
            or slot.replicate_kind != node.replicate_kind
            or slot.preregistration_sha256 != node.replicate_preregistration_sha256
            or slot.randomization_seed_sha256 != node.replicate_seed_sha256s[0]
            or slot.independent_site_required != node.independent_site_required
        ):
            raise ValueError("scientific replicate slot is not the exact WorkOrder projection")
        if slot.slot_count != 1 or slot.slot_index != 1:
            raise ValueError("scientific bridge Phase 1 supports exactly one preregistered slot")
        if not authorized.committed_at <= protocol.authored_at <= self.bound_at:
            raise ValueError(
                "action binding was not frozen after authorization and protocol authorship"
            )
        try:
            verify_compilation(request, result)
        except ValueError as exc:
            raise ValueError("action binding compilation failed canonical verification") from exc
        return self

    @property
    def binding_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def scientific_slot_id(self) -> str:
        digest = canonical_sha256(
            {
                "schema_name": "aletheia.scientific_observation_slot_identity",
                "schema_version": SCIENTIFIC_BRIDGE_SCHEMA_VERSION,
                "quest_id": self.action.quest_id,
                "action_sha256": self.action.object_sha256,
                "protocol_sha256": self.compilation_request.protocol.protocol_sha256,
                "work_order_id": self.work_order.work_order_id,
                "work_order_node_sha256": self.work_order_node.node_sha256,
                "replicate_slot_id": self.replicate_slot.replicate_slot_id,
            }
        )
        return f"sos_{digest[:32]}"


class ScientificExecutionAuthorizationMessage(ScientificBridgeModel):
    """Scientific eligibility for one exact, already engineering-qualified execution."""

    schema_name: Literal["aletheia.scientific_execution_authorization_message"] = (
        "aletheia.scientific_execution_authorization_message"
    )
    schema_version: Literal[1] = SCIENTIFIC_BRIDGE_SCHEMA_VERSION
    scientific_slot_id: str = Field(pattern=r"^sos_[0-9a-f]{32}$")
    action_protocol_binding: ScientificActionProtocolBinding
    qualification_bundle: EngineeringQualificationBundle
    qualification_grant: EngineeringQualificationGrant
    validator_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation_validation_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    admission_policy: ObservationAdmissionPolicy
    scientific_observation_artifact_binding: ScientificObservationArtifactBinding
    execution_authority_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorized_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    authorization_key_id: str = Field(pattern=_SHA256_PATTERN)
    validator_authority_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    validator_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    validator_key_id: str = Field(pattern=_SHA256_PATTERN)
    admission_authority_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    admission_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    admission_key_id: str = Field(pattern=_SHA256_PATTERN)
    authorized_at: AwareDatetime
    expires_at: AwareDatetime
    observation_admission_deadline: AwareDatetime
    source_qualification_only: Literal[True] = True
    source_scientific_admission_allowed: Literal[False] = False
    independent_validation_required: Literal[True] = True
    direct_artifact_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _authorization_is_closed(self) -> "ScientificExecutionAuthorizationMessage":
        binding = self.action_protocol_binding
        bundle = self.qualification_bundle
        grant = self.qualification_grant
        intent = bundle.intent
        if self.scientific_slot_id != binding.scientific_slot_id:
            raise ValueError("scientific slot identity differs from its closed binding")
        if (
            bundle.compilation_request != binding.compilation_request
            or bundle.compilation_result != binding.compilation_result
            or bundle.work_order != binding.work_order
            or bundle.intent.replicate_slot != binding.replicate_slot
            or bundle.intent.work_order_node_sha256 != binding.work_order_node.node_sha256
        ):
            raise ValueError("qualification bundle differs from the action-protocol binding")
        try:
            verify_execution_intent_binding(bundle.work_order, intent)
        except ValueError as exc:
            raise ValueError("qualification intent differs from its exact WorkOrder node") from exc
        if (
            intent.infrastructure_attempt.attempt_number != 1
            or intent.infrastructure_attempt.previous_attempt_id is not None
            or intent.retry_policy.mode is not ExecutionRetryMode.NEVER
            or intent.retry_policy.maximum_attempts_per_scientific_slot != 1
            or intent.resource_request.max_infrastructure_attempts != 1
        ):
            raise ValueError("scientific bridge Phase 1 forbids infrastructure retry ambiguity")
        _require_grant_matches_bundle(bundle=bundle, grant=grant)
        protocol = binding.compilation_request.protocol
        artifact_binding = self.scientific_observation_artifact_binding
        if (
            artifact_binding.work_order_node != binding.work_order_node
            or tuple(
                item
                for item in protocol.steps
                if item.step_id == artifact_binding.protocol_step.step_id
            )
            != (artifact_binding.protocol_step,)
            or tuple(
                item
                for item in protocol.observable_output_bindings
                if item.observable_spec_sha256 == artifact_binding.observable.observable_sha256
            )
            != (artifact_binding.observable_output_binding,)
            or tuple(
                item
                for item in binding.work_order_node.observable_output_bindings
                if item.observable_spec_sha256 == artifact_binding.observable.observable_sha256
            )
            != (artifact_binding.observable_output_binding,)
            or tuple(
                item
                for item in protocol.observables
                if item.observable_sha256 == artifact_binding.observable.observable_sha256
            )
            != (artifact_binding.observable,)
            or tuple(
                item
                for item in protocol.data_ports
                if item.port_id == artifact_binding.data_port.port_id
            )
            != (artifact_binding.data_port,)
            or tuple(
                item
                for item in binding.work_order_node.expected_artifacts
                if item.artifact_key == artifact_binding.artifact_key
            )
            != (artifact_binding.expected_artifact,)
            or tuple(
                item
                for item in artifact_binding.protocol_step.expected_artifacts
                if item.artifact_key == artifact_binding.artifact_key
            )
            != (artifact_binding.expected_artifact,)
        ):
            raise ValueError("scientific observation artifact binding escaped the frozen protocol")
        if (
            self.validator_manifest_sha256 != self.admission_policy.validator_manifest_sha256
            or self.observation_validation_policy_sha256
            != self.admission_policy.observation_validation_policy_sha256
            or protocol.analysis_plan.outcome_space_sha256
            != self.admission_policy.analysis_outcome_space_sha256
        ):
            raise ValueError("validator, validation policy, and admission policy are not closed")
        if self.validator_principal_id not in protocol.independence.validator_principal_ids:
            raise ValueError(
                "validator principal is outside the preregistered independence contract"
            )
        if self.admission_principal_id not in protocol.independence.claim_approver_principal_ids:
            raise ValueError(
                "admission principal is outside the preregistered independence contract"
            )
        principals = (
            binding.action.proposed_by_principal_id,
            binding.action_authorized_event.principal_id,
            self.authorized_by_principal_id,
            self.validator_principal_id,
            self.admission_principal_id,
            grant.message.authorized_by_principal_id,
        )
        if len(principals) != len(set(principals)):
            raise ValueError(
                "proposal, action, bridge, validation, admission, and PR-4 principals differ"
            )
        bridge_keys = (
            self.authorization_key_id,
            self.validator_key_id,
            self.admission_key_id,
            grant.message.authorization_key_id,
        )
        bridge_policies = (
            self.execution_authority_policy_sha256,
            self.validator_authority_policy_sha256,
            self.admission_authority_policy_sha256,
            grant.message.qualification_authority_policy_sha256,
        )
        if len(bridge_keys) != len(set(bridge_keys)) or len(bridge_policies) != len(
            set(bridge_policies)
        ):
            raise ValueError(
                "bridge and engineering authorities require distinct keys and policies"
            )
        expected_artifact = tuple(
            item
            for item in intent.expected_artifacts
            if item.artifact_key == artifact_binding.artifact_key
        )
        if expected_artifact != (artifact_binding.expected_artifact,):
            raise ValueError("scientific observation must name one required raw-output artifact")
        if not (
            binding.bound_at
            <= grant.message.authorized_at
            <= self.authorized_at
            < self.expires_at
            <= grant.message.expires_at
            <= intent.deadline
            < self.observation_admission_deadline
            <= protocol.resource_budget.deadline
        ):
            raise ValueError(
                "scientific authorization, qualification, and observation times diverge"
            )
        if self.admission_policy.frozen_at > protocol.authored_at:
            raise ValueError(
                "scientific validation material was not frozen before protocol authorship"
            )
        return self

    @property
    def observation_artifact_key(self) -> str:
        return self.scientific_observation_artifact_binding.artifact_key

    @property
    def message_sha256(self) -> str:
        return canonical_sha256(self)


class ScientificExecutionAuthorization(ScientificBridgeModel):
    schema_name: Literal["aletheia.scientific_execution_authorization"] = (
        "aletheia.scientific_execution_authorization"
    )
    schema_version: Literal[1] = SCIENTIFIC_BRIDGE_SCHEMA_VERSION
    message: ScientificExecutionAuthorizationMessage
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)

    @property
    def signature_message(self) -> bytes:
        return _signature_message("scientific_execution_authorization", self.message)

    @property
    def authorization_sha256(self) -> str:
        return canonical_sha256(self)


class RawRunEnvelope(ScientificBridgeModel):
    """Exact PR-4 terminal material eligible for independent scientific validation.

    The envelope validates hashes and typed closure only.  Its ``authorized_at <= runtime_ended_at``
    check is a weak sanity bound, not proof that the SEA was registered before launch.  Historical
    SEA registration, full launch lineage, node/runtime signatures, and CAS byte custody remain the
    responsibility of ``RawRunCustodyVerificationPort``.
    """

    schema_name: Literal["aletheia.raw_run_envelope"] = "aletheia.raw_run_envelope"
    schema_version: Literal[1] = SCIENTIFIC_BRIDGE_SCHEMA_VERSION
    scientific_authorization: ScientificExecutionAuthorization
    qualification_admission_sha256: str = Field(pattern=_SHA256_PATTERN)
    accepted_runtime_termination: AcceptedRuntimeTermination
    terminal_submission: QualificationTerminalSubmission
    accepted_terminal_submission: AcceptedQualificationTerminalSubmission
    artifact_manifest: ArtifactManifest
    artifact_verified_receipts: tuple[ArtifactVerifiedReceipt, ...]
    assembled_at: AwareDatetime
    source_qualification_only: Literal[True] = True
    source_scientific_admission_allowed: Literal[False] = False
    executor_reported_scientific_outcome_trusted: Literal[False] = False

    @model_validator(mode="after")
    def _raw_run_is_closed(self) -> "RawRunEnvelope":
        authorization = self.scientific_authorization.message
        intent = authorization.qualification_bundle.intent
        accepted = self.accepted_runtime_termination
        submission = self.terminal_submission
        terminal = self.accepted_terminal_submission
        manifest = self.artifact_manifest
        receipt_hashes = _artifact_receipt_hashes(
            manifest=manifest,
            receipts=self.artifact_verified_receipts,
        )
        expected_disposition = _terminal_disposition(
            intent=intent,
            accepted=accepted,
            manifest=manifest,
        )
        _validate_artifact_manifest_against_intent(
            intent=intent,
            manifest=manifest,
            success=expected_disposition == "process_succeeded",
        )
        if (
            accepted.attempt_id != intent.infrastructure_attempt.infrastructure_attempt_id
            or manifest.intent_sha256 != intent.intent_sha256
            or manifest.execution_id != intent.execution_id
            or manifest.replicate_slot_id != intent.replicate_slot.replicate_slot_id
            or manifest.infrastructure_attempt_id != accepted.attempt_id
            or manifest.produced_at != accepted.runtime_ended_at
        ):
            raise ValueError("raw run escaped its exact scientific execution attempt")
        if (
            submission.intent_sha256 != intent.intent_sha256
            or submission.execution_id != intent.execution_id
            or submission.attempt_id != accepted.attempt_id
            or submission.fencing_epoch != accepted.fencing_epoch
            or submission.lease_token_sha256 != accepted.lease_token_sha256
            or submission.accepted_runtime_termination_sha256
            != accepted.accepted_termination_sha256
            or submission.artifact_manifest_sha256 != manifest.manifest_sha256
            or submission.output_tree_sha256 != artifact_output_tree_sha256(manifest)
            or submission.artifact_verified_receipt_sha256s != receipt_hashes
        ):
            raise ValueError("terminal submission does not bind the exact raw run")
        if (
            terminal.attempt_id != accepted.attempt_id
            or terminal.node_manifest_sha256 != submission.node_manifest_sha256
            or terminal.terminal_submission_sha256 != submission.terminal_submission_sha256
            or terminal.accepted_runtime_termination_sha256 != accepted.accepted_termination_sha256
            or terminal.artifact_manifest_sha256 != manifest.manifest_sha256
            or terminal.output_tree_sha256 != submission.output_tree_sha256
            or terminal.artifact_verified_receipt_sha256s != receipt_hashes
            or terminal.node_submitted_at != submission.submitted_at
            or terminal.artifact_submission_deadline != accepted.artifact_submission_deadline
        ):
            raise ValueError(
                "runtime-control acceptance does not bind the exact terminal submission"
            )
        if (
            submission.disposition != expected_disposition
            or terminal.disposition != expected_disposition
        ):
            raise ValueError("qualification terminal disposition is not mechanically derived")
        if (
            accepted.runtime_control_policy_sha256 != terminal.runtime_control_policy_sha256
            or accepted.accepted_by_principal_id != terminal.accepted_by_principal_id
            or accepted.acceptance_key_id != terminal.acceptance_key_id
        ):
            raise ValueError("runtime-control terminal acceptances changed authority")
        runtime_authority = (
            terminal.accepted_by_principal_id,
            terminal.acceptance_key_id,
            terminal.runtime_control_policy_sha256,
        )
        validator_authority = (
            authorization.validator_principal_id,
            authorization.validator_key_id,
            authorization.validator_authority_policy_sha256,
        )
        admission_authority = (
            authorization.admission_principal_id,
            authorization.admission_key_id,
            authorization.admission_authority_policy_sha256,
        )
        if any(
            runtime_value in {validator_value, admission_value}
            for runtime_value, validator_value, admission_value in zip(
                runtime_authority,
                validator_authority,
                admission_authority,
                strict=True,
            )
        ):
            raise ValueError("validator/admitter authority reuses runtime-control authority")
        observed_entries = tuple(
            item
            for item in manifest.entries
            if item.artifact_key == authorization.observation_artifact_key
        )
        if expected_disposition == "process_succeeded" and (
            len(observed_entries) != 1 or observed_entries[0].role is not ArtifactRole.RAW_OUTPUT
        ):
            raise ValueError("successful raw run lacks its authorized observation artifact")
        if not (
            authorization.authorized_at <= accepted.runtime_ended_at
            and accepted.accepted_at <= submission.submitted_at
            and submission.submitted_at <= terminal.accepted_at <= self.assembled_at
            and self.assembled_at < authorization.observation_admission_deadline
        ):
            raise ValueError("scientific authorization and raw-run custody times are out of order")
        return self

    @property
    def raw_run_sha256(self) -> str:
        return canonical_sha256(self)


class VerifiedRawRunCustodyProjection(ScientificBridgeModel):
    """Closed output of the mandatory full historical raw-run custody adapter."""

    schema_name: Literal["aletheia.verified_raw_run_custody_projection"] = (
        "aletheia.verified_raw_run_custody_projection"
    )
    schema_version: Literal[1] = SCIENTIFIC_BRIDGE_SCHEMA_VERSION
    raw_run_sha256: str = Field(pattern=_SHA256_PATTERN)
    scientific_execution_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    scientific_slot_id: str = Field(pattern=r"^sos_[0-9a-f]{32}$")
    qualification_admission_sha256: str = Field(pattern=_SHA256_PATTERN)
    sea_registration_sha256: str = Field(pattern=_SHA256_PATTERN)
    sea_registered_at: AwareDatetime
    qualification_admitted_at: AwareDatetime
    resource_reservation_sha256: str = Field(pattern=_SHA256_PATTERN)
    resource_reserved_at: AwareDatetime
    runtime_launch_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_launched_at: AwareDatetime
    terminal_submission_sha256: str = Field(pattern=_SHA256_PATTERN)
    terminal_acceptance_sha256: str = Field(pattern=_SHA256_PATTERN)
    terminal_accepted_at: AwareDatetime
    cost_quote_sha256: str = Field(pattern=_SHA256_PATTERN)
    quoted_worker_node_manifest: WorkerNodeManifest
    terminal_worker_node_manifest: WorkerNodeManifest
    worker_node_enrollment: WorkerNodeEnrollment
    allocator_authority: VerifiedExecutionAuthorityProjection
    qualification_authority: VerifiedExecutionAuthorityProjection
    node_enrollment_authority: VerifiedExecutionAuthorityProjection
    node_execution_authority: VerifiedExecutionAuthorityProjection
    runtime_control_authority: VerifiedExecutionAuthorityProjection
    terminal_submission_authority: VerifiedExecutionAuthorityProjection
    terminal_acceptance_authority: VerifiedExecutionAuthorityProjection
    artifact_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_tree_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_verified_receipt_sha256s: tuple[str, ...]
    fresh_artifacts: tuple[VerifiedArtifactCustodyProjection, ...]
    verified_at: AwareDatetime

    @model_validator(mode="after")
    def _projection_is_closed(self) -> "VerifiedRawRunCustodyProjection":
        manifest = self.terminal_worker_node_manifest
        enrollment = self.worker_node_enrollment.message
        if self.quoted_worker_node_manifest != manifest:
            raise ValueError("quoted and terminal worker manifests differ")
        if (
            enrollment.node_manifest_sha256 != manifest.manifest_sha256
            or enrollment.node_id != manifest.node_id
            or enrollment.site_id != manifest.site_id
            or enrollment.principal_id != manifest.principal_id
            or enrollment.node_signing_key_id != manifest.node_signing_key_id
            or enrollment.node_signing_public_key_ed25519_hex
            != manifest.node_signing_public_key_ed25519_hex
        ):
            raise ValueError("worker enrollment does not bind the quoted terminal node")
        node_identity = (manifest.principal_id, manifest.node_signing_key_id)
        if (
            (
                self.node_execution_authority.principal_id,
                self.node_execution_authority.key_id,
            )
            != node_identity
            or (
                self.terminal_submission_authority.principal_id,
                self.terminal_submission_authority.key_id,
            )
            != node_identity
            or self.node_execution_authority.policy_sha256
            != self.terminal_submission_authority.policy_sha256
        ):
            raise ValueError("node execution and terminal signing authority differ from enrollment")
        if (
            self.node_enrollment_authority.principal_id != enrollment.enrolled_by_principal_id
            or self.node_enrollment_authority.key_id != enrollment.enrollment_authority_key_id
            or self.node_enrollment_authority.policy_sha256
            != enrollment.node_enrollment_policy_sha256
        ):
            raise ValueError("node enrollment authority projection differs from certificate")
        if not (
            enrollment.issued_at <= self.runtime_launched_at < enrollment.expires_at
            and manifest.key_valid_from
            <= self.terminal_accepted_at
            < min(manifest.key_expires_at, manifest.key_revoked_at or manifest.key_expires_at)
            and self.runtime_launched_at < self.terminal_accepted_at <= self.verified_at
        ):
            raise ValueError("node enrollment/key or runtime terminal time is invalid")
        if not (
            self.sea_registered_at
            <= self.qualification_admitted_at
            <= self.resource_reserved_at
            <= self.runtime_launched_at
            < self.terminal_accepted_at
            <= self.verified_at
        ):
            raise ValueError("raw-run custody lineage is not historically ordered")
        artifact_keys = tuple(item.artifact_key for item in self.fresh_artifacts)
        if artifact_keys != tuple(sorted(set(artifact_keys))):
            raise ValueError("fresh artifact custody projections must be unique and canonical")
        hashes = self.artifact_verified_receipt_sha256s
        if hashes != tuple(sorted(set(hashes))) or {
            item.artifact_verified_receipt_sha256 for item in self.fresh_artifacts
        } != set(hashes):
            raise ValueError("fresh artifact custody does not cover exact receipt hashes")
        return self

    @property
    def projection_sha256(self) -> str:
        return canonical_sha256(self)


class ValidationIssuanceChallengeMessage(ScientificBridgeModel):
    schema_name: Literal["aletheia.validation_issuance_challenge_message"] = (
        "aletheia.validation_issuance_challenge_message"
    )
    schema_version: Literal[1] = SCIENTIFIC_BRIDGE_SCHEMA_VERSION
    purpose: Literal["issue_observation_validation_receipt"] = (
        "issue_observation_validation_receipt"
    )
    nonce_sha256: str = Field(pattern=_SHA256_PATTERN)
    row_scope: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    scientific_slot_id: str = Field(pattern=r"^sos_[0-9a-f]{32}$")
    raw_run_sha256: str = Field(pattern=_SHA256_PATTERN)
    validation_campaign_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    observation_admission_deadline: AwareDatetime
    database_authority_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    issued_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    issuance_key_id: str = Field(pattern=_SHA256_PATTERN)
    issued_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def _challenge_is_fresh(self) -> "ValidationIssuanceChallengeMessage":
        if self.row_scope != scientific_observation_row_scope(self.scientific_slot_id):
            raise ValueError("validation issuance challenge changed the scientific-slot row")
        if not self.issued_at < self.expires_at <= self.observation_admission_deadline:
            raise ValueError("validation issuance challenge has no live half-open database window")
        return self


class ValidationIssuanceChallenge(ScientificBridgeModel):
    schema_name: Literal["aletheia.validation_issuance_challenge"] = (
        "aletheia.validation_issuance_challenge"
    )
    schema_version: Literal[1] = SCIENTIFIC_BRIDGE_SCHEMA_VERSION
    message: ValidationIssuanceChallengeMessage
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)

    @property
    def signature_message(self) -> bytes:
        return _signature_message("validation_issuance_challenge", self.message)

    @property
    def challenge_sha256(self) -> str:
        return canonical_sha256(self)


class AdmissionIssuanceChallengeMessage(ScientificBridgeModel):
    schema_name: Literal["aletheia.admission_issuance_challenge_message"] = (
        "aletheia.admission_issuance_challenge_message"
    )
    schema_version: Literal[1] = SCIENTIFIC_BRIDGE_SCHEMA_VERSION
    purpose: Literal["issue_observation_admission_decision"] = (
        "issue_observation_admission_decision"
    )
    nonce_sha256: str = Field(pattern=_SHA256_PATTERN)
    row_scope: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    scientific_slot_id: str = Field(pattern=r"^sos_[0-9a-f]{32}$")
    committed_validation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    validation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation_admission_deadline: AwareDatetime
    database_authority_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    issued_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    issuance_key_id: str = Field(pattern=_SHA256_PATTERN)
    issued_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def _challenge_is_fresh(self) -> "AdmissionIssuanceChallengeMessage":
        if self.row_scope != scientific_observation_row_scope(self.scientific_slot_id):
            raise ValueError("admission issuance challenge changed the scientific-slot row")
        if not self.issued_at < self.expires_at <= self.observation_admission_deadline:
            raise ValueError("admission issuance challenge has no live half-open database window")
        return self


class AdmissionIssuanceChallenge(ScientificBridgeModel):
    schema_name: Literal["aletheia.admission_issuance_challenge"] = (
        "aletheia.admission_issuance_challenge"
    )
    schema_version: Literal[1] = SCIENTIFIC_BRIDGE_SCHEMA_VERSION
    message: AdmissionIssuanceChallengeMessage
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)

    @property
    def signature_message(self) -> bytes:
        return _signature_message("admission_issuance_challenge", self.message)

    @property
    def challenge_sha256(self) -> str:
        return canonical_sha256(self)


class ObservationValidationReceiptMessage(ScientificBridgeModel):
    """Independent bridge signature over one raw run and one exact F9 validation campaign."""

    schema_name: Literal["aletheia.observation_validation_receipt_message"] = (
        "aletheia.observation_validation_receipt_message"
    )
    schema_version: Literal[1] = SCIENTIFIC_BRIDGE_SCHEMA_VERSION
    scientific_slot_id: str = Field(pattern=r"^sos_[0-9a-f]{32}$")
    raw_run: RawRunEnvelope
    issuance_challenge: ValidationIssuanceChallenge
    validation_campaign_projection: VerifiedObservationValidationCampaignProjection | None = None
    disposition: BridgeValidationDisposition
    outcome: ScientificObservationOutcome | None = None
    scientific_observation_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    blocker_codes: tuple[str, ...] = Field(max_length=256)
    validator_authority_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    validated_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    validation_key_id: str = Field(pattern=_SHA256_PATTERN)
    validated_at: AwareDatetime
    independent_from_executor: Literal[True] = True
    source_qualification_only: Literal[True] = True
    direct_scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _validation_is_derived(self) -> "ObservationValidationReceiptMessage":
        authorization = self.raw_run.scientific_authorization.message
        terminal = self.raw_run.accepted_terminal_submission.disposition
        challenge = self.issuance_challenge.message
        if self.scientific_slot_id != authorization.scientific_slot_id:
            raise ValueError("validation receipt changed the scientific slot")
        if (
            challenge.scientific_slot_id != self.scientific_slot_id
            or challenge.raw_run_sha256 != self.raw_run.raw_run_sha256
            or challenge.observation_admission_deadline
            != authorization.observation_admission_deadline
            or self.validated_at != challenge.issued_at
        ):
            raise ValueError("validation receipt differs from its database issuance challenge")
        if (
            self.validator_authority_policy_sha256
            != authorization.validator_authority_policy_sha256
            or self.validated_by_principal_id != authorization.validator_principal_id
            or self.validation_key_id != authorization.validator_key_id
        ):
            raise ValueError("validation receipt changed the preregistered validator authority")
        if self.blocker_codes != tuple(sorted(set(self.blocker_codes))):
            raise ValueError("validation blocker codes must be unique and canonical")

        if terminal != "process_succeeded":
            expected_blockers = (f"qualification_terminal:{terminal}",)
            if (
                self.validation_campaign_projection is not None
                or challenge.validation_campaign_sha256 is not None
                or self.disposition is not BridgeValidationDisposition.BLOCKED_EXECUTION
                or self.outcome is not None
                or self.scientific_observation_sha256 is not None
                or self.blocker_codes != expected_blockers
            ):
                raise ValueError("terminal engineering failure cannot become a scientific outcome")
            if not (
                self.raw_run.assembled_at
                <= self.validated_at
                < authorization.observation_admission_deadline
            ):
                raise ValueError("blocked validation receipt falls outside observation window")
            return self

        projection = self.validation_campaign_projection
        if projection is None:
            raise ValueError("successful execution requires a verified validation projection")
        if (
            challenge.validation_campaign_sha256 != projection.campaign_sha256
            or projection.committed_at > challenge.issued_at
            or projection.validator_manifest_sha256 != authorization.validator_manifest_sha256
            or projection.observation_validation_policy_sha256
            != authorization.observation_validation_policy_sha256
            or projection.disposition is not self.disposition
            or projection.generated_at < self.raw_run.assembled_at
            or self.validated_at < projection.generated_at
        ):
            raise ValueError("validation projection escaped its preregistered raw run")
        artifact_binding = authorization.scientific_observation_artifact_binding
        observation_entry = next(
            item
            for item in self.raw_run.artifact_manifest.entries
            if item.artifact_key == authorization.observation_artifact_key
        )
        observation_receipt = next(
            item
            for item in self.raw_run.artifact_verified_receipts
            if item.artifact.artifact_key == artifact_binding.artifact_key
        )
        if (
            projection.observation_namespace_sha256 != artifact_binding.observation_namespace_sha256
            or projection.protocol_sha256
            != authorization.action_protocol_binding.compilation_request.protocol.protocol_sha256
            or projection.selection_campaign_sha256 != artifact_binding.selection_campaign_sha256
            or projection.prediction_campaign_sha256 != artifact_binding.prediction_campaign_sha256
            or projection.prediction_commitment_sha256
            != artifact_binding.prediction_commitment_sha256
            or projection.raw_run_sha256 != self.raw_run.raw_run_sha256
            or projection.scientific_observation_artifact_binding_sha256
            != artifact_binding.binding_sha256
            or projection.artifact_verified_receipt_sha256
            != observation_receipt.verified_receipt_sha256
        ):
            raise ValueError("validation projection rebound preregistration or raw-run custody")
        if projection.raw_observation_content_sha256 != observation_entry.content_sha256:
            raise ValueError("validation projection observed different bytes from the raw run")
        if self.blocker_codes != projection.blocker_codes:
            raise ValueError("bridge validation blockers differ from the verified projection")
        if self.disposition is BridgeValidationDisposition.VALIDATED_CONFIRMATION:
            if projection.outcome_bin_id is None:
                raise ValueError("validated projection requires its exact outcome bin")
            mapped = _mapped_outcome(
                authorization.admission_policy,
                projection.outcome_bin_id,
            )
            expected_observation = _scientific_observation_sha256(
                scientific_slot_id=self.scientific_slot_id,
                projection=projection,
                outcome=mapped,
            )
            if (
                self.outcome is not mapped
                or self.scientific_observation_sha256 != expected_observation
                or self.blocker_codes
            ):
                raise ValueError("validated scientific observation is not mechanically derived")
        elif self.outcome is not None or self.scientific_observation_sha256 is not None:
            raise ValueError("rejected or blocked scientific validation cannot carry an outcome")
        if self.validated_at >= authorization.observation_admission_deadline:
            raise ValueError(
                "observation validation reached or exceeded its authorization deadline"
            )
        return self

    @property
    def message_sha256(self) -> str:
        return canonical_sha256(self)


class ObservationValidationReceipt(ScientificBridgeModel):
    schema_name: Literal["aletheia.observation_validation_receipt"] = (
        "aletheia.observation_validation_receipt"
    )
    schema_version: Literal[1] = SCIENTIFIC_BRIDGE_SCHEMA_VERSION
    message: ObservationValidationReceiptMessage
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    authority_state: Literal["proposal"] = "proposal"
    scientific_authority_conferred: Literal[False] = False

    @property
    def signature_message(self) -> bytes:
        return _signature_message("observation_validation_receipt", self.message)

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


class CommittedObservationValidationReceiptMessage(ScientificBridgeModel):
    schema_name: Literal["aletheia.committed_observation_validation_receipt_message"] = (
        "aletheia.committed_observation_validation_receipt_message"
    )
    schema_version: Literal[1] = SCIENTIFIC_BRIDGE_SCHEMA_VERSION
    receipt: ObservationValidationReceipt
    validation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    issuance_challenge_sha256: str = Field(pattern=_SHA256_PATTERN)
    row_scope: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    registered_at: AwareDatetime
    committed_at: AwareDatetime
    database_authority_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    committed_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    commit_key_id: str = Field(pattern=_SHA256_PATTERN)
    persisted: Literal[True] = True
    historical_validation_authority: Literal[True] = True

    @model_validator(mode="after")
    def _commit_is_exact(self) -> "CommittedObservationValidationReceiptMessage":
        challenge = self.receipt.message.issuance_challenge
        challenge_message = challenge.message
        if (
            self.validation_receipt_sha256 != self.receipt.receipt_sha256
            or self.issuance_challenge_sha256 != challenge.challenge_sha256
            or self.row_scope != challenge_message.row_scope
            or self.database_authority_policy_sha256
            != challenge_message.database_authority_policy_sha256
            or self.committed_by_principal_id != challenge_message.issued_by_principal_id
            or self.commit_key_id != challenge_message.issuance_key_id
        ):
            raise ValueError("validation commit rebound receipt, challenge, row, or DB authority")
        if not (
            challenge_message.issued_at
            <= self.registered_at
            <= self.committed_at
            < challenge_message.expires_at
            <= challenge_message.observation_admission_deadline
        ):
            raise ValueError("validation commit is outside its half-open DB challenge window")
        return self

    @property
    def message_sha256(self) -> str:
        return canonical_sha256(self)


class CommittedObservationValidationReceipt(ScientificBridgeModel):
    schema_name: Literal["aletheia.committed_observation_validation_receipt"] = (
        "aletheia.committed_observation_validation_receipt"
    )
    schema_version: Literal[1] = SCIENTIFIC_BRIDGE_SCHEMA_VERSION
    message: CommittedObservationValidationReceiptMessage
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)

    @property
    def signature_message(self) -> bytes:
        return _signature_message("committed_observation_validation_receipt", self.message)

    @property
    def committed_receipt_sha256(self) -> str:
        return canonical_sha256(self)


class ObservationAdmissionDecisionMessage(ScientificBridgeModel):
    """Admission authority decision; persistence must still enforce an empty-slot CAS."""

    schema_name: Literal["aletheia.observation_admission_decision_message"] = (
        "aletheia.observation_admission_decision_message"
    )
    schema_version: Literal[1] = SCIENTIFIC_BRIDGE_SCHEMA_VERSION
    scientific_slot_id: str = Field(pattern=r"^sos_[0-9a-f]{32}$")
    committed_validation_receipt: CommittedObservationValidationReceipt
    issuance_challenge: AdmissionIssuanceChallenge
    disposition: ObservationAdmissionDisposition
    admitted_observation_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    reason_codes: tuple[str, ...] = Field(max_length=64)
    admission_authority_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    decided_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    decision_key_id: str = Field(pattern=_SHA256_PATTERN)
    decided_at: AwareDatetime
    slot_precondition: Literal["must_be_empty"] = "must_be_empty"
    maximum_admissions_per_scientific_slot: Literal[1] = 1
    persistence_committed: Literal[False] = False

    @model_validator(mode="after")
    def _decision_is_derived(self) -> "ObservationAdmissionDecisionMessage":
        receipt = self.committed_validation_receipt.message.receipt.message
        authorization = receipt.raw_run.scientific_authorization.message
        challenge = self.issuance_challenge.message
        if self.scientific_slot_id != receipt.scientific_slot_id:
            raise ValueError("admission decision changed the scientific slot")
        if (
            challenge.scientific_slot_id != self.scientific_slot_id
            or challenge.committed_validation_receipt_sha256
            != self.committed_validation_receipt.committed_receipt_sha256
            or challenge.validation_receipt_sha256
            != self.committed_validation_receipt.message.validation_receipt_sha256
            or challenge.observation_admission_deadline
            != authorization.observation_admission_deadline
            or self.decided_at != challenge.issued_at
        ):
            raise ValueError("admission decision differs from its DB issuance challenge")
        if (
            challenge.row_scope != self.committed_validation_receipt.message.row_scope
            or challenge.issued_at < self.committed_validation_receipt.message.committed_at
        ):
            raise ValueError("admission challenge escaped the committed validation row")
        if (
            self.admission_authority_policy_sha256
            != authorization.admission_authority_policy_sha256
            or self.decided_by_principal_id != authorization.admission_principal_id
            or self.decision_key_id != authorization.admission_key_id
        ):
            raise ValueError("admission decision changed the preregistered admission authority")
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValueError("admission reason codes must be unique and canonical")
        admissible = (
            receipt.disposition is BridgeValidationDisposition.VALIDATED_CONFIRMATION
            and receipt.outcome is not None
            and receipt.scientific_observation_sha256 is not None
        )
        if self.disposition is ObservationAdmissionDisposition.ADMITTED:
            if (
                not admissible
                or self.admitted_observation_sha256 != receipt.scientific_observation_sha256
                or self.reason_codes
            ):
                raise ValueError("only an independently validated observation may be admitted")
        elif self.admitted_observation_sha256 is not None or not self.reason_codes:
            raise ValueError("rejected admission requires reasons and cannot carry an observation")
        if (
            not receipt.validated_at
            <= self.decided_at
            < authorization.observation_admission_deadline
        ):
            raise ValueError("admission decision falls outside its authorized observation window")
        return self

    @property
    def message_sha256(self) -> str:
        return canonical_sha256(self)


class ObservationAdmissionDecision(ScientificBridgeModel):
    schema_name: Literal["aletheia.observation_admission_decision"] = (
        "aletheia.observation_admission_decision"
    )
    schema_version: Literal[1] = SCIENTIFIC_BRIDGE_SCHEMA_VERSION
    message: ObservationAdmissionDecisionMessage
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)
    authority_state: Literal["proposal"] = "proposal"
    scientific_authority_conferred: Literal[False] = False

    @property
    def signature_message(self) -> bytes:
        return _signature_message("observation_admission_decision", self.message)

    @property
    def decision_sha256(self) -> str:
        return canonical_sha256(self)


class CommittedObservationAdmissionMessage(ScientificBridgeModel):
    schema_name: Literal["aletheia.committed_observation_admission_message"] = (
        "aletheia.committed_observation_admission_message"
    )
    schema_version: Literal[1] = SCIENTIFIC_BRIDGE_SCHEMA_VERSION
    decision: ObservationAdmissionDecision
    decision_sha256: str = Field(pattern=_SHA256_PATTERN)
    committed_validation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    exact_registered_validation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    issuance_challenge_sha256: str = Field(pattern=_SHA256_PATTERN)
    row_scope: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    registered_at: AwareDatetime
    committed_at: AwareDatetime
    database_authority_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    committed_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    commit_key_id: str = Field(pattern=_SHA256_PATTERN)
    scientific_slot_was_empty: Literal[True] = True
    transaction_was_atomic: Literal[True] = True
    persistence_committed: Literal[True] = True
    scientific_authority_conferred: Literal[True] = True

    @model_validator(mode="after")
    def _commit_is_exact(self) -> "CommittedObservationAdmissionMessage":
        decision_message = self.decision.message
        challenge = decision_message.issuance_challenge
        challenge_message = challenge.message
        committed_validation = decision_message.committed_validation_receipt
        if (
            decision_message.disposition is not ObservationAdmissionDisposition.ADMITTED
            or decision_message.admitted_observation_sha256 is None
        ):
            raise ValueError("only an admitted proposal may become a committed observation")
        if (
            self.decision_sha256 != self.decision.decision_sha256
            or self.committed_validation_receipt_sha256
            != committed_validation.committed_receipt_sha256
            or self.exact_registered_validation_receipt_sha256
            != committed_validation.message.validation_receipt_sha256
            or self.issuance_challenge_sha256 != challenge.challenge_sha256
            or self.row_scope != challenge_message.row_scope
            or self.database_authority_policy_sha256
            != challenge_message.database_authority_policy_sha256
            or self.committed_by_principal_id != challenge_message.issued_by_principal_id
            or self.commit_key_id != challenge_message.issuance_key_id
        ):
            raise ValueError("admission commit rebound decision, receipt, challenge, or DB row")
        if not (
            challenge_message.issued_at
            <= self.registered_at
            <= self.committed_at
            < challenge_message.expires_at
            <= challenge_message.observation_admission_deadline
        ):
            raise ValueError("admission commit is outside its half-open DB challenge window")
        return self

    @property
    def message_sha256(self) -> str:
        return canonical_sha256(self)


class CommittedObservationAdmission(ScientificBridgeModel):
    schema_name: Literal["aletheia.committed_observation_admission"] = (
        "aletheia.committed_observation_admission"
    )
    schema_version: Literal[1] = SCIENTIFIC_BRIDGE_SCHEMA_VERSION
    message: CommittedObservationAdmissionMessage
    signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)

    @property
    def signature_message(self) -> bytes:
        return _signature_message("committed_observation_admission", self.message)

    @property
    def committed_admission_sha256(self) -> str:
        return canonical_sha256(self)


class ScientificBridgeVerificationError(ValueError):
    """A bridge contract, external authority pin, or signature failed closed."""


def validate_raw_run_structure(raw_run: RawRunEnvelope) -> RawRunEnvelope:
    """Revalidate raw-run DTO structure only; this does not verify PR-4/CAS custody."""

    try:
        return RawRunEnvelope.model_validate(raw_run.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ScientificBridgeVerificationError("raw run failed structural validation") from exc


def engineering_qualification_admission_sha256(
    verified: VerifiedEngineeringQualification,
) -> str:
    """Stable identity of a verified PR-4 qualification, excluding observation time."""

    verified = VerifiedEngineeringQualification.model_validate(verified.model_dump(mode="python"))
    return execution_canonical_sha256(verified.model_dump(mode="json", exclude={"verified_at"}))


def _validate_verified_qualification(
    *,
    candidate: VerifiedEngineeringQualification,
    bundle: EngineeringQualificationBundle,
    grant: EngineeringQualificationGrant,
    verified_at: datetime | None = None,
    qualification_admission_sha256: str | None = None,
) -> VerifiedEngineeringQualification:
    verified = VerifiedEngineeringQualification.model_validate(candidate.model_dump(mode="python"))
    prior_sha256 = (
        bundle.prior_execution_receipt.execution_receipt_sha256
        if bundle.prior_execution_receipt is not None
        else None
    )
    expected: dict[str, object] = {
        "grant_sha256": grant.grant_sha256,
        "bundle_sha256": bundle.bundle_sha256,
        "intent_sha256": bundle.intent.intent_sha256,
        "execution_id": bundle.intent.execution_id,
        "infrastructure_attempt_id": (
            bundle.intent.infrastructure_attempt.infrastructure_attempt_id
        ),
        "input_artifact_verified_receipt_sha256s": (bundle.input_artifact_verified_receipt_sha256s),
        "prior_execution_receipt_sha256": prior_sha256,
        "budget_authorization_sha256": bundle.budget_authorization.authorization_sha256,
        "cost_quote_sha256": bundle.cost_quote.quote_sha256,
        "qualification_only": True,
        "scientific_admission_allowed": False,
    }
    if any(getattr(verified, key) != value for key, value in expected.items()):
        raise ScientificBridgeVerificationError(
            "engineering qualification custody returned rebound verification"
        )
    if verified_at is not None and verified.verified_at != verified_at:
        raise ScientificBridgeVerificationError(
            "engineering qualification was not freshly verified at online observation time"
        )
    if (
        qualification_admission_sha256 is not None
        and engineering_qualification_admission_sha256(verified) != qualification_admission_sha256
    ):
        raise ScientificBridgeVerificationError(
            "qualification admission hash differs from exact verified PR-4 material"
        )
    return verified


def _verify_action_binding_custody(
    *,
    authorization: ScientificExecutionAuthorization,
    action_authority: ResearchActionAuthorityVerificationPort,
    observed_at: datetime,
) -> None:
    message = authorization.message
    binding_hash = action_authority.verify_action_protocol_binding(
        binding=message.action_protocol_binding,
        observed_at=observed_at,
    )
    if binding_hash != message.action_protocol_binding.binding_sha256:
        raise ScientificBridgeVerificationError(
            "action authority returned another action-protocol binding"
        )


def _verify_action_and_qualification_custody(
    *,
    authorization: ScientificExecutionAuthorization,
    action_authority: ResearchActionAuthorityVerificationPort,
    qualification_custody: EngineeringQualificationCustodyVerificationPort,
    observed_at: datetime,
) -> None:
    _verify_action_binding_custody(
        authorization=authorization,
        action_authority=action_authority,
        observed_at=observed_at,
    )
    message = authorization.message
    candidate = qualification_custody.verify_engineering_qualification_custody(
        bundle=message.qualification_bundle,
        grant=message.qualification_grant,
        observed_at=observed_at,
    )
    _validate_verified_qualification(
        candidate=candidate,
        bundle=message.qualification_bundle,
        grant=message.qualification_grant,
        verified_at=observed_at,
    )


def _verify_raw_run_custody(
    *,
    raw_run: RawRunEnvelope,
    action_authority: ResearchActionAuthorityVerificationPort,
    qualification_custody: EngineeringQualificationCustodyVerificationPort,
    raw_run_custody: RawRunCustodyVerificationPort,
    observed_at: datetime,
) -> None:
    raw_run = validate_raw_run_structure(raw_run)
    authorization = raw_run.scientific_authorization.message
    _verify_action_binding_custody(
        authorization=raw_run.scientific_authorization,
        action_authority=action_authority,
        observed_at=observed_at,
    )
    candidate = qualification_custody.verify_qualification_admission(
        qualification_admission_sha256=raw_run.qualification_admission_sha256,
        bundle=authorization.qualification_bundle,
        grant=authorization.qualification_grant,
        observed_at=observed_at,
    )
    _validate_verified_qualification(
        candidate=candidate,
        bundle=authorization.qualification_bundle,
        grant=authorization.qualification_grant,
        qualification_admission_sha256=raw_run.qualification_admission_sha256,
    )
    candidate_projection = raw_run_custody.verify_raw_run_custody(
        raw_run=raw_run,
        observed_at=observed_at,
    )
    _validate_verified_raw_run_custody_projection(
        candidate=candidate_projection,
        raw_run=raw_run,
        observed_at=observed_at,
    )


def _validate_verified_raw_run_custody_projection(
    *,
    candidate: VerifiedRawRunCustodyProjection,
    raw_run: RawRunEnvelope,
    observed_at: datetime,
) -> VerifiedRawRunCustodyProjection:
    try:
        projection = VerifiedRawRunCustodyProjection.model_validate(
            candidate.model_dump(mode="python")
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ScientificBridgeVerificationError(
            "raw-run custody returned an invalid typed projection"
        ) from exc

    authorization = raw_run.scientific_authorization
    authorization_message = authorization.message
    bundle = authorization_message.qualification_bundle
    grant_message = authorization_message.qualification_grant.message
    accepted = raw_run.accepted_runtime_termination
    submission = raw_run.terminal_submission
    terminal = raw_run.accepted_terminal_submission
    manifest = raw_run.artifact_manifest
    receipt_hashes = _artifact_receipt_hashes(
        manifest=manifest,
        receipts=raw_run.artifact_verified_receipts,
    )
    expected: dict[str, object] = {
        "raw_run_sha256": raw_run.raw_run_sha256,
        "scientific_execution_authorization_sha256": authorization.authorization_sha256,
        "scientific_slot_id": authorization_message.scientific_slot_id,
        "qualification_admission_sha256": raw_run.qualification_admission_sha256,
        "terminal_submission_sha256": submission.terminal_submission_sha256,
        "terminal_acceptance_sha256": terminal.accepted_terminal_submission_sha256,
        "terminal_accepted_at": terminal.accepted_at,
        "cost_quote_sha256": bundle.cost_quote.quote_sha256,
        "artifact_manifest_sha256": manifest.manifest_sha256,
        "output_tree_sha256": submission.output_tree_sha256,
        "artifact_verified_receipt_sha256s": receipt_hashes,
        "verified_at": observed_at,
    }
    if any(getattr(projection, key) != value for key, value in expected.items()):
        raise ScientificBridgeVerificationError(
            "raw-run custody returned a rebound lineage projection"
        )
    node_manifest = projection.terminal_worker_node_manifest
    if (
        node_manifest.manifest_sha256 != submission.node_manifest_sha256
        or node_manifest.manifest_sha256 != bundle.cost_quote.selected_node_manifest_sha256
        or projection.quoted_worker_node_manifest != node_manifest
        or projection.sea_registered_at < authorization_message.authorized_at
        or projection.sea_registered_at >= authorization_message.expires_at
        or projection.qualification_admitted_at < grant_message.authorized_at
        or projection.qualification_admitted_at >= grant_message.expires_at
        or projection.resource_reserved_at >= bundle.cost_quote.expires_at
        or projection.runtime_launched_at > accepted.runtime_ended_at
    ):
        raise ScientificBridgeVerificationError(
            "raw-run custody escaped SEA registration, placement, or live admission"
        )
    expected_authorities = (
        (
            projection.allocator_authority,
            bundle.cost_quote.quoted_by_principal_id,
            bundle.cost_quote.pricing_policy_sha256,
        ),
        (
            projection.qualification_authority,
            grant_message.authorized_by_principal_id,
            grant_message.qualification_authority_policy_sha256,
        ),
        (
            projection.runtime_control_authority,
            accepted.accepted_by_principal_id,
            accepted.runtime_control_policy_sha256,
        ),
        (
            projection.terminal_acceptance_authority,
            terminal.accepted_by_principal_id,
            terminal.runtime_control_policy_sha256,
        ),
    )
    if any(
        authority.principal_id != principal_id or authority.policy_sha256 != policy_sha256
        for authority, principal_id, policy_sha256 in expected_authorities
    ):
        raise ScientificBridgeVerificationError(
            "raw-run custody authority projection differs from signed lineage"
        )
    if (
        projection.qualification_authority.key_id != grant_message.authorization_key_id
        or projection.runtime_control_authority.key_id != accepted.acceptance_key_id
        or projection.terminal_acceptance_authority.key_id != terminal.acceptance_key_id
        or projection.terminal_submission_authority.key_id != submission.signing_key_id
    ):
        raise ScientificBridgeVerificationError(
            "raw-run custody key projection differs from signed lineage"
        )
    receipt_by_key = {
        receipt.artifact.artifact_key: receipt for receipt in raw_run.artifact_verified_receipts
    }
    if tuple(item.artifact_key for item in projection.fresh_artifacts) != tuple(
        sorted(receipt_by_key)
    ):
        raise ScientificBridgeVerificationError(
            "raw-run custody does not cover the exact artifact set"
        )
    for artifact in projection.fresh_artifacts:
        receipt = receipt_by_key[artifact.artifact_key]
        if (
            artifact.content_sha256 != receipt.artifact.content_sha256
            or artifact.artifact_verified_receipt_sha256 != receipt.verified_receipt_sha256
            or artifact.authority.principal_id != receipt.verifier_principal_id
        ):
            raise ScientificBridgeVerificationError(
                "raw-run custody returned rebound artifact bytes or authority"
            )
    return projection


def _verify_raw_run_for_observation_validation(
    *,
    raw_run: RawRunEnvelope,
    qualification_authority: QualificationAuthorityVerifier,
    action_authority: ResearchActionAuthorityVerificationPort,
    qualification_custody: EngineeringQualificationCustodyVerificationPort,
    raw_run_custody: RawRunCustodyVerificationPort,
    execution_authority_pin: ScientificBridgeAuthorityPin,
    validator_authority_pin: ScientificBridgeAuthorityPin,
    admission_authority_pin: ScientificBridgeAuthorityPin,
    observed_at: datetime,
) -> RawRunEnvelope:
    """Verify all external input authority before a validation receipt may be signed."""

    try:
        raw_run = validate_raw_run_structure(raw_run)
        verify_scientific_execution_authorization_historical(
            authorization=raw_run.scientific_authorization,
            qualification_authority=qualification_authority,
            execution_authority_pin=execution_authority_pin,
            validator_authority_pin=validator_authority_pin,
            admission_authority_pin=admission_authority_pin,
            observed_at=observed_at,
        )
        _verify_raw_run_custody(
            raw_run=raw_run,
            action_authority=action_authority,
            qualification_custody=qualification_custody,
            raw_run_custody=raw_run_custody,
            observed_at=observed_at,
        )
        return raw_run
    except ScientificBridgeVerificationError:
        raise
    except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
        raise ScientificBridgeVerificationError(
            "observation validation receipt failed closed input custody verification"
        ) from exc


def verify_raw_run_for_independent_validation(
    *,
    raw_run: RawRunEnvelope,
    qualification_authority: QualificationAuthorityVerifier,
    action_authority: ResearchActionAuthorityVerificationPort,
    qualification_custody: EngineeringQualificationCustodyVerificationPort,
    raw_run_custody: RawRunCustodyVerificationPort,
    execution_authority_pin: ScientificBridgeAuthorityPin,
    validator_authority_pin: ScientificBridgeAuthorityPin,
    admission_authority_pin: ScientificBridgeAuthorityPin,
    observed_at: datetime,
) -> RawRunEnvelope:
    """Verify the complete historical authority/custody chain before external analysis.

    This public seam deliberately grants no validation or admission authority.  It lets an
    independently deployed validator reject untrusted raw material before spending analysis work
    or signing a campaign; validation-receipt issuance repeats the same checks at DB challenge
    time.
    """

    return _verify_raw_run_for_observation_validation(
        raw_run=raw_run,
        qualification_authority=qualification_authority,
        action_authority=action_authority,
        qualification_custody=qualification_custody,
        raw_run_custody=raw_run_custody,
        execution_authority_pin=execution_authority_pin,
        validator_authority_pin=validator_authority_pin,
        admission_authority_pin=admission_authority_pin,
        observed_at=observed_at,
    )


def _resolve_validation_campaign_projection(
    *,
    campaign_sha256: str,
    raw_run: RawRunEnvelope,
    validation_campaign_custody: ObservationValidationCampaignVerificationPort,
    observed_at: datetime,
) -> VerifiedObservationValidationCampaignProjection:
    authorization = raw_run.scientific_authorization.message
    try:
        candidate = validation_campaign_custody.verify_observation_validation_campaign(
            campaign_sha256=campaign_sha256,
            raw_run=raw_run,
            expected_validator_manifest_sha256=authorization.validator_manifest_sha256,
            expected_observation_validation_policy_sha256=(
                authorization.observation_validation_policy_sha256
            ),
            observed_at=observed_at,
        )
        projection = VerifiedObservationValidationCampaignProjection.model_validate(
            candidate.model_dump(mode="python")
        )
    except ScientificBridgeVerificationError:
        raise
    except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
        raise ScientificBridgeVerificationError(
            "observation validation receipt failed closed campaign custody verification"
        ) from exc
    if (
        projection.campaign_sha256 != campaign_sha256
        or projection.validator_manifest_sha256 != authorization.validator_manifest_sha256
        or projection.observation_validation_policy_sha256
        != authorization.observation_validation_policy_sha256
        or projection.generated_at > observed_at
    ):
        raise ScientificBridgeVerificationError(
            "observation-validation custody returned a rebound campaign projection"
        )
    return projection


def issue_scientific_execution_authorization(
    *,
    action_protocol_binding: ScientificActionProtocolBinding,
    qualification_bundle: EngineeringQualificationBundle,
    qualification_grant: EngineeringQualificationGrant,
    validator_manifest_sha256: str,
    observation_validation_policy_sha256: str,
    admission_policy: ObservationAdmissionPolicy,
    scientific_observation_artifact_binding: ScientificObservationArtifactBinding,
    qualification_authority: QualificationAuthorityVerifier,
    action_authority: ResearchActionAuthorityVerificationPort,
    qualification_custody: EngineeringQualificationCustodyVerificationPort,
    execution_authority_pin: ScientificBridgeAuthorityPin,
    validator_authority_pin: ScientificBridgeAuthorityPin,
    admission_authority_pin: ScientificBridgeAuthorityPin,
    private_key: bytes,
    authorized_at: datetime,
    expires_at: datetime,
    observation_admission_deadline: datetime,
) -> ScientificExecutionAuthorization:
    """Sign structural scientific eligibility after callers verify external custody ports."""

    _require_pin_roles(
        execution=execution_authority_pin,
        validator=validator_authority_pin,
        admission=admission_authority_pin,
    )
    _require_private_key(private_key, execution_authority_pin)
    message = ScientificExecutionAuthorizationMessage(
        scientific_slot_id=action_protocol_binding.scientific_slot_id,
        action_protocol_binding=action_protocol_binding,
        qualification_bundle=qualification_bundle,
        qualification_grant=qualification_grant,
        validator_manifest_sha256=validator_manifest_sha256,
        observation_validation_policy_sha256=observation_validation_policy_sha256,
        admission_policy=admission_policy,
        scientific_observation_artifact_binding=(scientific_observation_artifact_binding),
        execution_authority_policy_sha256=execution_authority_pin.policy_sha256,
        authorized_by_principal_id=execution_authority_pin.principal_id,
        authorization_key_id=execution_authority_pin.key_id,
        validator_authority_policy_sha256=validator_authority_pin.policy_sha256,
        validator_principal_id=validator_authority_pin.principal_id,
        validator_key_id=validator_authority_pin.key_id,
        admission_authority_policy_sha256=admission_authority_pin.policy_sha256,
        admission_principal_id=admission_authority_pin.principal_id,
        admission_key_id=admission_authority_pin.key_id,
        authorized_at=authorized_at,
        expires_at=expires_at,
        observation_admission_deadline=observation_admission_deadline,
    )
    unsigned = ScientificExecutionAuthorization(message=message, signature_ed25519_hex="0" * 128)
    try:
        qualification_authority.verify_signature(
            message.qualification_grant,
            observed_at=authorized_at,
        )
        _verify_action_and_qualification_custody(
            authorization=unsigned,
            action_authority=action_authority,
            qualification_custody=qualification_custody,
            observed_at=authorized_at,
        )
    except ScientificBridgeVerificationError:
        raise
    except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
        raise ScientificBridgeVerificationError(
            "scientific execution authorization issuance failed closed input custody verification"
        ) from exc
    signed = unsigned.model_copy(
        update={
            "signature_ed25519_hex": Ed25519PrivateKey.from_private_bytes(private_key)
            .sign(unsigned.signature_message)
            .hex()
        }
    )
    verify_scientific_execution_authorization(
        authorization=signed,
        qualification_authority=qualification_authority,
        action_authority=action_authority,
        qualification_custody=qualification_custody,
        execution_authority_pin=execution_authority_pin,
        validator_authority_pin=validator_authority_pin,
        admission_authority_pin=admission_authority_pin,
        observed_at=authorized_at,
    )
    return signed


def verify_scientific_execution_authorization(
    *,
    authorization: ScientificExecutionAuthorization,
    qualification_authority: QualificationAuthorityVerifier,
    action_authority: ResearchActionAuthorityVerificationPort,
    qualification_custody: EngineeringQualificationCustodyVerificationPort,
    execution_authority_pin: ScientificBridgeAuthorityPin,
    validator_authority_pin: ScientificBridgeAuthorityPin,
    admission_authority_pin: ScientificBridgeAuthorityPin,
    observed_at: datetime,
) -> None:
    """Online verifier: require a live SEA and call both external custody authorities."""

    try:
        authorization = verify_scientific_execution_authorization_historical(
            authorization=authorization,
            qualification_authority=qualification_authority,
            execution_authority_pin=execution_authority_pin,
            validator_authority_pin=validator_authority_pin,
            admission_authority_pin=admission_authority_pin,
            observed_at=observed_at,
        )
        message = authorization.message
        if not (
            message.authorized_at <= observed_at < message.expires_at
            and execution_authority_pin.active_at(observed_at)
        ):
            raise ScientificBridgeVerificationError(
                "scientific execution authorization is not live at online observation time"
            )
        _verify_action_and_qualification_custody(
            authorization=authorization,
            action_authority=action_authority,
            qualification_custody=qualification_custody,
            observed_at=observed_at,
        )
    except ScientificBridgeVerificationError:
        raise
    except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
        raise ScientificBridgeVerificationError(
            "online scientific execution authorization failed closed verification"
        ) from exc


def validate_scientific_execution_authorization_structure(
    authorization: ScientificExecutionAuthorization,
) -> ScientificExecutionAuthorization:
    """Revalidate closed DTO structure only; this does not verify signatures or custody."""

    try:
        return ScientificExecutionAuthorization.model_validate(
            authorization.model_dump(mode="python")
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ScientificBridgeVerificationError(
            "scientific execution authorization failed structural validation"
        ) from exc


def verify_scientific_execution_authorization_historical(
    *,
    authorization: ScientificExecutionAuthorization,
    qualification_authority: QualificationAuthorityVerifier,
    execution_authority_pin: ScientificBridgeAuthorityPin,
    validator_authority_pin: ScientificBridgeAuthorityPin,
    admission_authority_pin: ScientificBridgeAuthorityPin,
    observed_at: datetime,
) -> ScientificExecutionAuthorization:
    """Historically verify structure and signing-time authority without requiring a live SEA."""

    try:
        _require_utc(observed_at, "scientific authorization observed_at")
        authorization = validate_scientific_execution_authorization_structure(authorization)
        _require_pin_roles(
            execution=execution_authority_pin,
            validator=validator_authority_pin,
            admission=admission_authority_pin,
        )
        message = authorization.message
        _require_message_pin(
            policy_sha256=message.execution_authority_policy_sha256,
            principal_id=message.authorized_by_principal_id,
            key_id=message.authorization_key_id,
            pin=execution_authority_pin,
        )
        _require_message_pin(
            policy_sha256=message.validator_authority_policy_sha256,
            principal_id=message.validator_principal_id,
            key_id=message.validator_key_id,
            pin=validator_authority_pin,
        )
        _require_message_pin(
            policy_sha256=message.admission_authority_policy_sha256,
            principal_id=message.admission_principal_id,
            key_id=message.admission_key_id,
            pin=admission_authority_pin,
        )
        if message.expires_at > execution_authority_pin.active_until:
            raise ScientificBridgeVerificationError("scientific authorization outlives signer pin")
        if message.observation_admission_deadline > min(
            validator_authority_pin.active_until,
            admission_authority_pin.active_until,
        ):
            raise ScientificBridgeVerificationError(
                "observation window outlives validator/admitter"
            )
        if not (
            validator_authority_pin.active_at(message.authorized_at)
            and admission_authority_pin.active_at(message.authorized_at)
        ):
            raise ScientificBridgeVerificationError(
                "validator/admitter pins were not active when scientific execution was authorized"
            )
        qualification_authority.verify_signature(
            message.qualification_grant,
            observed_at=message.authorized_at,
        )
        _verify_signature(
            pin=execution_authority_pin,
            signed_at=message.authorized_at,
            observed_at=observed_at,
            message=authorization.signature_message,
            signature_ed25519_hex=authorization.signature_ed25519_hex,
        )
        return authorization
    except ScientificBridgeVerificationError:
        raise
    except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
        raise ScientificBridgeVerificationError(
            "historical scientific execution authorization failed closed verification"
        ) from exc


def issue_validation_issuance_challenge(
    *,
    raw_run: RawRunEnvelope,
    validation_campaign_sha256: str | None,
    nonce_sha256: str,
    database_authority_pin: ObservationDatabaseAuthorityPin,
    private_key: bytes,
    issued_at: datetime,
    expires_at: datetime,
) -> ValidationIssuanceChallenge:
    """Issue a DB-signed, short-lived capability for one validation proposal."""

    raw_run = validate_raw_run_structure(raw_run)
    authorization = raw_run.scientific_authorization.message
    _require_utc(issued_at, "validation challenge issued_at")
    _require_utc(expires_at, "validation challenge expires_at")
    _require_private_key(private_key, database_authority_pin)
    _require_database_authority_separation(
        database=database_authority_pin,
        authorization=authorization,
        raw_run=raw_run,
    )
    terminal_succeeded = raw_run.accepted_terminal_submission.disposition == "process_succeeded"
    if terminal_succeeded != (validation_campaign_sha256 is not None):
        raise ScientificBridgeVerificationError(
            "validation challenge campaign differs from terminal engineering disposition"
        )
    if not (
        raw_run.assembled_at
        <= issued_at
        < expires_at
        <= min(
            authorization.observation_admission_deadline,
            database_authority_pin.active_until,
        )
        and database_authority_pin.active_at(issued_at)
    ):
        raise ScientificBridgeVerificationError(
            "validation challenge is outside its DB or observation window"
        )
    message = ValidationIssuanceChallengeMessage(
        nonce_sha256=nonce_sha256,
        row_scope=scientific_observation_row_scope(authorization.scientific_slot_id),
        scientific_slot_id=authorization.scientific_slot_id,
        raw_run_sha256=raw_run.raw_run_sha256,
        validation_campaign_sha256=validation_campaign_sha256,
        observation_admission_deadline=authorization.observation_admission_deadline,
        database_authority_policy_sha256=database_authority_pin.policy_sha256,
        issued_by_principal_id=database_authority_pin.principal_id,
        issuance_key_id=database_authority_pin.key_id,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    unsigned = ValidationIssuanceChallenge(message=message, signature_ed25519_hex="0" * 128)
    signed = unsigned.model_copy(
        update={
            "signature_ed25519_hex": Ed25519PrivateKey.from_private_bytes(private_key)
            .sign(unsigned.signature_message)
            .hex()
        }
    )
    _verify_validation_issuance_challenge(
        challenge=signed,
        raw_run=raw_run,
        expected_validation_campaign_sha256=validation_campaign_sha256,
        database_authority_pin=database_authority_pin,
        observed_at=issued_at,
        require_live=True,
    )
    return signed


def verify_validation_issuance_challenge(
    *,
    challenge: ValidationIssuanceChallenge,
    raw_run: RawRunEnvelope,
    expected_validation_campaign_sha256: str | None,
    database_authority_pin: ObservationDatabaseAuthorityPin,
    observed_at: datetime,
) -> ValidationIssuanceChallenge:
    """Historically verify the external DB authority and exact validation scope."""

    return _verify_validation_issuance_challenge(
        challenge=challenge,
        raw_run=raw_run,
        expected_validation_campaign_sha256=expected_validation_campaign_sha256,
        database_authority_pin=database_authority_pin,
        observed_at=observed_at,
        require_live=False,
    )


def _verify_validation_issuance_challenge(
    *,
    challenge: ValidationIssuanceChallenge,
    raw_run: RawRunEnvelope,
    expected_validation_campaign_sha256: str | None,
    database_authority_pin: ObservationDatabaseAuthorityPin,
    observed_at: datetime,
    require_live: bool,
) -> ValidationIssuanceChallenge:
    try:
        _require_utc(observed_at, "validation challenge observed_at")
        raw_run = validate_raw_run_structure(raw_run)
        challenge = ValidationIssuanceChallenge.model_validate(challenge.model_dump(mode="python"))
        message = challenge.message
        authorization = raw_run.scientific_authorization.message
        _require_database_authority_separation(
            database=database_authority_pin,
            authorization=authorization,
            raw_run=raw_run,
        )
        _require_database_message_pin(
            policy_sha256=message.database_authority_policy_sha256,
            principal_id=message.issued_by_principal_id,
            key_id=message.issuance_key_id,
            pin=database_authority_pin,
        )
        expected: dict[str, object] = {
            "row_scope": scientific_observation_row_scope(authorization.scientific_slot_id),
            "scientific_slot_id": authorization.scientific_slot_id,
            "raw_run_sha256": raw_run.raw_run_sha256,
            "validation_campaign_sha256": expected_validation_campaign_sha256,
            "observation_admission_deadline": (authorization.observation_admission_deadline),
        }
        if any(getattr(message, key) != value for key, value in expected.items()):
            raise ScientificBridgeVerificationError(
                "validation challenge rebound its slot, raw run, campaign, or deadline"
            )
        terminal_succeeded = raw_run.accepted_terminal_submission.disposition == "process_succeeded"
        if terminal_succeeded != (message.validation_campaign_sha256 is not None):
            raise ScientificBridgeVerificationError(
                "validation challenge campaign differs from terminal disposition"
            )
        if (
            message.issued_at < raw_run.assembled_at
            or message.expires_at > database_authority_pin.active_until
            or observed_at < message.issued_at
            or (
                require_live
                and not (
                    message.issued_at <= observed_at < message.expires_at
                    and database_authority_pin.active_at(observed_at)
                )
            )
        ):
            raise ScientificBridgeVerificationError(
                "validation challenge is stale or outside DB authority"
            )
        _verify_signature(
            pin=database_authority_pin,
            signed_at=message.issued_at,
            observed_at=observed_at,
            message=challenge.signature_message,
            signature_ed25519_hex=challenge.signature_ed25519_hex,
        )
        return challenge
    except ScientificBridgeVerificationError:
        raise
    except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
        raise ScientificBridgeVerificationError(
            "validation issuance challenge failed closed verification"
        ) from exc


def issue_observation_validation_receipt(
    *,
    raw_run: RawRunEnvelope,
    validation_campaign_sha256: str | None,
    issuance_challenge: ValidationIssuanceChallenge,
    qualification_authority: QualificationAuthorityVerifier,
    action_authority: ResearchActionAuthorityVerificationPort,
    qualification_custody: EngineeringQualificationCustodyVerificationPort,
    raw_run_custody: RawRunCustodyVerificationPort,
    validation_campaign_custody: ObservationValidationCampaignVerificationPort,
    execution_authority_pin: ScientificBridgeAuthorityPin,
    validator_authority_pin: ScientificBridgeAuthorityPin,
    admission_authority_pin: ScientificBridgeAuthorityPin,
    database_authority_pin: ObservationDatabaseAuthorityPin,
    private_key: bytes,
) -> ObservationValidationReceipt:
    """Sign a terminal block or an exact existing F9 observation-validation campaign."""

    _require_private_key(private_key, validator_authority_pin)
    validated_at = issuance_challenge.message.issued_at
    _verify_validation_issuance_challenge(
        challenge=issuance_challenge,
        raw_run=raw_run,
        expected_validation_campaign_sha256=validation_campaign_sha256,
        database_authority_pin=database_authority_pin,
        observed_at=validated_at,
        require_live=True,
    )
    raw_run = _verify_raw_run_for_observation_validation(
        raw_run=raw_run,
        qualification_authority=qualification_authority,
        action_authority=action_authority,
        qualification_custody=qualification_custody,
        raw_run_custody=raw_run_custody,
        execution_authority_pin=execution_authority_pin,
        validator_authority_pin=validator_authority_pin,
        admission_authority_pin=admission_authority_pin,
        observed_at=validated_at,
    )
    terminal = raw_run.accepted_terminal_submission.disposition
    if terminal != "process_succeeded":
        if validation_campaign_sha256 is not None:
            raise ScientificBridgeVerificationError(
                "terminal engineering failure cannot select a validation campaign"
            )
        projection = None
        disposition = BridgeValidationDisposition.BLOCKED_EXECUTION
        outcome = None
        observation_sha256 = None
        blockers = (f"qualification_terminal:{terminal}",)
    else:
        if validation_campaign_sha256 is None:
            raise ScientificBridgeVerificationError(
                "successful execution requires an external validation campaign identity"
            )
        projection = _resolve_validation_campaign_projection(
            campaign_sha256=validation_campaign_sha256,
            raw_run=raw_run,
            validation_campaign_custody=validation_campaign_custody,
            observed_at=validated_at,
        )
        disposition = projection.disposition
        blockers = projection.blocker_codes
        if disposition is BridgeValidationDisposition.VALIDATED_CONFIRMATION:
            if projection.outcome_bin_id is None:
                raise ScientificBridgeVerificationError(
                    "validated projection lacks its exact outcome bin"
                )
            outcome = _mapped_outcome(
                raw_run.scientific_authorization.message.admission_policy,
                projection.outcome_bin_id,
            )
            observation_sha256 = _scientific_observation_sha256(
                scientific_slot_id=raw_run.scientific_authorization.message.scientific_slot_id,
                projection=projection,
                outcome=outcome,
            )
        else:
            outcome = None
            observation_sha256 = None
    authorization = raw_run.scientific_authorization.message
    message = ObservationValidationReceiptMessage(
        scientific_slot_id=authorization.scientific_slot_id,
        raw_run=raw_run,
        issuance_challenge=issuance_challenge,
        validation_campaign_projection=projection,
        disposition=disposition,
        outcome=outcome,
        scientific_observation_sha256=observation_sha256,
        blocker_codes=blockers,
        validator_authority_policy_sha256=validator_authority_pin.policy_sha256,
        validated_by_principal_id=validator_authority_pin.principal_id,
        validation_key_id=validator_authority_pin.key_id,
        validated_at=validated_at,
    )
    unsigned = ObservationValidationReceipt(message=message, signature_ed25519_hex="0" * 128)
    signed = unsigned.model_copy(
        update={
            "signature_ed25519_hex": Ed25519PrivateKey.from_private_bytes(private_key)
            .sign(unsigned.signature_message)
            .hex()
        }
    )
    verify_observation_validation_receipt(
        receipt=signed,
        qualification_authority=qualification_authority,
        action_authority=action_authority,
        qualification_custody=qualification_custody,
        raw_run_custody=raw_run_custody,
        validation_campaign_custody=validation_campaign_custody,
        execution_authority_pin=execution_authority_pin,
        validator_authority_pin=validator_authority_pin,
        admission_authority_pin=admission_authority_pin,
        database_authority_pin=database_authority_pin,
        observed_at=validated_at,
    )
    return signed


def verify_observation_validation_receipt(
    *,
    receipt: ObservationValidationReceipt,
    qualification_authority: QualificationAuthorityVerifier,
    action_authority: ResearchActionAuthorityVerificationPort,
    qualification_custody: EngineeringQualificationCustodyVerificationPort,
    raw_run_custody: RawRunCustodyVerificationPort,
    validation_campaign_custody: ObservationValidationCampaignVerificationPort,
    execution_authority_pin: ScientificBridgeAuthorityPin,
    validator_authority_pin: ScientificBridgeAuthorityPin,
    admission_authority_pin: ScientificBridgeAuthorityPin,
    database_authority_pin: ObservationDatabaseAuthorityPin,
    observed_at: datetime,
) -> None:
    """Verify the nested authorization and independent validation signature."""

    try:
        _require_utc(observed_at, "observation validation observed_at")
        receipt = validate_observation_validation_receipt_structure(receipt)
        projection = receipt.message.validation_campaign_projection
        verify_validation_issuance_challenge(
            challenge=receipt.message.issuance_challenge,
            raw_run=receipt.message.raw_run,
            expected_validation_campaign_sha256=(
                projection.campaign_sha256 if projection is not None else None
            ),
            database_authority_pin=database_authority_pin,
            observed_at=observed_at,
        )
        _verify_raw_run_for_observation_validation(
            raw_run=receipt.message.raw_run,
            qualification_authority=qualification_authority,
            action_authority=action_authority,
            qualification_custody=qualification_custody,
            raw_run_custody=raw_run_custody,
            execution_authority_pin=execution_authority_pin,
            validator_authority_pin=validator_authority_pin,
            admission_authority_pin=admission_authority_pin,
            observed_at=observed_at,
        )
        if projection is not None:
            expected_projection = _resolve_validation_campaign_projection(
                campaign_sha256=projection.campaign_sha256,
                raw_run=receipt.message.raw_run,
                validation_campaign_custody=validation_campaign_custody,
                observed_at=observed_at,
            )
            if expected_projection != projection:
                raise ScientificBridgeVerificationError(
                    "signed validation projection differs from external campaign custody"
                )
        _require_message_pin(
            policy_sha256=receipt.message.validator_authority_policy_sha256,
            principal_id=receipt.message.validated_by_principal_id,
            key_id=receipt.message.validation_key_id,
            pin=validator_authority_pin,
        )
        _verify_signature(
            pin=validator_authority_pin,
            signed_at=receipt.message.validated_at,
            observed_at=observed_at,
            message=receipt.signature_message,
            signature_ed25519_hex=receipt.signature_ed25519_hex,
        )
    except ScientificBridgeVerificationError:
        raise
    except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
        raise ScientificBridgeVerificationError(
            "observation validation receipt failed closed verification"
        ) from exc


def validate_observation_validation_receipt_structure(
    receipt: ObservationValidationReceipt,
) -> ObservationValidationReceipt:
    """Revalidate validation DTO structure only; this does not verify signatures or custody."""

    try:
        return ObservationValidationReceipt.model_validate(receipt.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ScientificBridgeVerificationError(
            "observation validation receipt failed structural validation"
        ) from exc


def commit_observation_validation_receipt(
    *,
    receipt: ObservationValidationReceipt,
    qualification_authority: QualificationAuthorityVerifier,
    action_authority: ResearchActionAuthorityVerificationPort,
    qualification_custody: EngineeringQualificationCustodyVerificationPort,
    raw_run_custody: RawRunCustodyVerificationPort,
    validation_campaign_custody: ObservationValidationCampaignVerificationPort,
    execution_authority_pin: ScientificBridgeAuthorityPin,
    validator_authority_pin: ScientificBridgeAuthorityPin,
    admission_authority_pin: ScientificBridgeAuthorityPin,
    database_authority_pin: ObservationDatabaseAuthorityPin,
    private_key: bytes,
    registered_at: datetime,
    committed_at: datetime,
) -> CommittedObservationValidationReceipt:
    """DB-sign the exact validation proposal committed inside its challenge window."""

    receipt = validate_observation_validation_receipt_structure(receipt)
    authorization = receipt.message.raw_run.scientific_authorization.message
    _require_private_key(private_key, database_authority_pin)
    _require_database_authority_separation(
        database=database_authority_pin,
        authorization=authorization,
        raw_run=receipt.message.raw_run,
    )
    if not database_authority_pin.active_at(committed_at):
        raise ScientificBridgeVerificationError(
            "validation commit is outside database key authority"
        )
    verify_observation_validation_receipt(
        receipt=receipt,
        qualification_authority=qualification_authority,
        action_authority=action_authority,
        qualification_custody=qualification_custody,
        raw_run_custody=raw_run_custody,
        validation_campaign_custody=validation_campaign_custody,
        execution_authority_pin=execution_authority_pin,
        validator_authority_pin=validator_authority_pin,
        admission_authority_pin=admission_authority_pin,
        database_authority_pin=database_authority_pin,
        observed_at=committed_at,
    )
    challenge = receipt.message.issuance_challenge
    message = CommittedObservationValidationReceiptMessage(
        receipt=receipt,
        validation_receipt_sha256=receipt.receipt_sha256,
        issuance_challenge_sha256=challenge.challenge_sha256,
        row_scope=challenge.message.row_scope,
        registered_at=registered_at,
        committed_at=committed_at,
        database_authority_policy_sha256=database_authority_pin.policy_sha256,
        committed_by_principal_id=database_authority_pin.principal_id,
        commit_key_id=database_authority_pin.key_id,
    )
    unsigned = CommittedObservationValidationReceipt(
        message=message,
        signature_ed25519_hex="0" * 128,
    )
    signed = unsigned.model_copy(
        update={
            "signature_ed25519_hex": Ed25519PrivateKey.from_private_bytes(private_key)
            .sign(unsigned.signature_message)
            .hex()
        }
    )
    verify_committed_observation_validation_receipt(
        committed_receipt=signed,
        qualification_authority=qualification_authority,
        action_authority=action_authority,
        qualification_custody=qualification_custody,
        raw_run_custody=raw_run_custody,
        validation_campaign_custody=validation_campaign_custody,
        execution_authority_pin=execution_authority_pin,
        validator_authority_pin=validator_authority_pin,
        admission_authority_pin=admission_authority_pin,
        database_authority_pin=database_authority_pin,
        observed_at=committed_at,
    )
    return signed


def verify_committed_observation_validation_receipt(
    *,
    committed_receipt: CommittedObservationValidationReceipt,
    qualification_authority: QualificationAuthorityVerifier,
    action_authority: ResearchActionAuthorityVerificationPort,
    qualification_custody: EngineeringQualificationCustodyVerificationPort,
    raw_run_custody: RawRunCustodyVerificationPort,
    validation_campaign_custody: ObservationValidationCampaignVerificationPort,
    execution_authority_pin: ScientificBridgeAuthorityPin,
    validator_authority_pin: ScientificBridgeAuthorityPin,
    admission_authority_pin: ScientificBridgeAuthorityPin,
    database_authority_pin: ObservationDatabaseAuthorityPin,
    observed_at: datetime,
) -> CommittedObservationValidationReceipt:
    """Verify the nested proposal and its exact database commitment historically."""

    try:
        _require_utc(observed_at, "committed validation observed_at")
        committed_receipt = CommittedObservationValidationReceipt.model_validate(
            committed_receipt.model_dump(mode="python")
        )
        message = committed_receipt.message
        receipt = message.receipt
        authorization = receipt.message.raw_run.scientific_authorization.message
        _require_database_authority_separation(
            database=database_authority_pin,
            authorization=authorization,
            raw_run=receipt.message.raw_run,
        )
        _require_database_message_pin(
            policy_sha256=message.database_authority_policy_sha256,
            principal_id=message.committed_by_principal_id,
            key_id=message.commit_key_id,
            pin=database_authority_pin,
        )
        if (
            message.committed_at > observed_at
            or message.receipt.message.issuance_challenge.message.expires_at
            > database_authority_pin.active_until
        ):
            raise ScientificBridgeVerificationError(
                "validation commitment is future-dated or outlives database authority"
            )
        verify_observation_validation_receipt(
            receipt=receipt,
            qualification_authority=qualification_authority,
            action_authority=action_authority,
            qualification_custody=qualification_custody,
            raw_run_custody=raw_run_custody,
            validation_campaign_custody=validation_campaign_custody,
            execution_authority_pin=execution_authority_pin,
            validator_authority_pin=validator_authority_pin,
            admission_authority_pin=admission_authority_pin,
            database_authority_pin=database_authority_pin,
            observed_at=observed_at,
        )
        _verify_signature(
            pin=database_authority_pin,
            signed_at=message.committed_at,
            observed_at=observed_at,
            message=committed_receipt.signature_message,
            signature_ed25519_hex=committed_receipt.signature_ed25519_hex,
        )
        return committed_receipt
    except ScientificBridgeVerificationError:
        raise
    except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
        raise ScientificBridgeVerificationError(
            "committed observation validation failed closed verification"
        ) from exc


def issue_admission_issuance_challenge(
    *,
    committed_validation_receipt: CommittedObservationValidationReceipt,
    nonce_sha256: str,
    database_authority_pin: ObservationDatabaseAuthorityPin,
    private_key: bytes,
    issued_at: datetime,
    expires_at: datetime,
) -> AdmissionIssuanceChallenge:
    """Issue a DB-signed capability for one decision over one committed validation."""

    committed_validation_receipt = CommittedObservationValidationReceipt.model_validate(
        committed_validation_receipt.model_dump(mode="python")
    )
    committed_message = committed_validation_receipt.message
    receipt = committed_message.receipt.message
    authorization = receipt.raw_run.scientific_authorization.message
    _require_utc(issued_at, "admission challenge issued_at")
    _require_utc(expires_at, "admission challenge expires_at")
    _require_private_key(private_key, database_authority_pin)
    _require_database_authority_separation(
        database=database_authority_pin,
        authorization=authorization,
        raw_run=receipt.raw_run,
    )
    if not (
        committed_message.committed_at
        <= issued_at
        < expires_at
        <= min(
            authorization.observation_admission_deadline,
            database_authority_pin.active_until,
        )
        and database_authority_pin.active_at(issued_at)
    ):
        raise ScientificBridgeVerificationError(
            "admission challenge is outside its DB or observation window"
        )
    message = AdmissionIssuanceChallengeMessage(
        nonce_sha256=nonce_sha256,
        row_scope=committed_message.row_scope,
        scientific_slot_id=receipt.scientific_slot_id,
        committed_validation_receipt_sha256=(committed_validation_receipt.committed_receipt_sha256),
        validation_receipt_sha256=committed_message.validation_receipt_sha256,
        observation_admission_deadline=authorization.observation_admission_deadline,
        database_authority_policy_sha256=database_authority_pin.policy_sha256,
        issued_by_principal_id=database_authority_pin.principal_id,
        issuance_key_id=database_authority_pin.key_id,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    unsigned = AdmissionIssuanceChallenge(message=message, signature_ed25519_hex="0" * 128)
    signed = unsigned.model_copy(
        update={
            "signature_ed25519_hex": Ed25519PrivateKey.from_private_bytes(private_key)
            .sign(unsigned.signature_message)
            .hex()
        }
    )
    _verify_admission_issuance_challenge(
        challenge=signed,
        committed_validation_receipt=committed_validation_receipt,
        database_authority_pin=database_authority_pin,
        observed_at=issued_at,
        require_live=True,
    )
    return signed


def verify_admission_issuance_challenge(
    *,
    challenge: AdmissionIssuanceChallenge,
    committed_validation_receipt: CommittedObservationValidationReceipt,
    database_authority_pin: ObservationDatabaseAuthorityPin,
    observed_at: datetime,
) -> AdmissionIssuanceChallenge:
    """Historically verify an exact admission challenge against the external DB pin."""

    return _verify_admission_issuance_challenge(
        challenge=challenge,
        committed_validation_receipt=committed_validation_receipt,
        database_authority_pin=database_authority_pin,
        observed_at=observed_at,
        require_live=False,
    )


def _verify_admission_issuance_challenge(
    *,
    challenge: AdmissionIssuanceChallenge,
    committed_validation_receipt: CommittedObservationValidationReceipt,
    database_authority_pin: ObservationDatabaseAuthorityPin,
    observed_at: datetime,
    require_live: bool,
) -> AdmissionIssuanceChallenge:
    try:
        _require_utc(observed_at, "admission challenge observed_at")
        challenge = AdmissionIssuanceChallenge.model_validate(challenge.model_dump(mode="python"))
        committed_validation_receipt = CommittedObservationValidationReceipt.model_validate(
            committed_validation_receipt.model_dump(mode="python")
        )
        message = challenge.message
        committed_message = committed_validation_receipt.message
        receipt = committed_message.receipt.message
        validation_challenge = receipt.issuance_challenge
        authorization = receipt.raw_run.scientific_authorization.message
        _require_database_authority_separation(
            database=database_authority_pin,
            authorization=authorization,
            raw_run=receipt.raw_run,
        )
        _require_database_message_pin(
            policy_sha256=message.database_authority_policy_sha256,
            principal_id=message.issued_by_principal_id,
            key_id=message.issuance_key_id,
            pin=database_authority_pin,
        )
        _require_database_message_pin(
            policy_sha256=committed_message.database_authority_policy_sha256,
            principal_id=committed_message.committed_by_principal_id,
            key_id=committed_message.commit_key_id,
            pin=database_authority_pin,
        )
        _require_database_message_pin(
            policy_sha256=validation_challenge.message.database_authority_policy_sha256,
            principal_id=validation_challenge.message.issued_by_principal_id,
            key_id=validation_challenge.message.issuance_key_id,
            pin=database_authority_pin,
        )
        expected: dict[str, object] = {
            "row_scope": committed_message.row_scope,
            "scientific_slot_id": receipt.scientific_slot_id,
            "committed_validation_receipt_sha256": (
                committed_validation_receipt.committed_receipt_sha256
            ),
            "validation_receipt_sha256": committed_message.validation_receipt_sha256,
            "observation_admission_deadline": (authorization.observation_admission_deadline),
        }
        if any(getattr(message, key) != value for key, value in expected.items()):
            raise ScientificBridgeVerificationError(
                "admission challenge rebound its committed validation or row"
            )
        if (
            message.issued_at < committed_message.committed_at
            or message.expires_at > database_authority_pin.active_until
            or observed_at < message.issued_at
            or (
                require_live
                and not (
                    message.issued_at <= observed_at < message.expires_at
                    and database_authority_pin.active_at(observed_at)
                )
            )
        ):
            raise ScientificBridgeVerificationError(
                "admission challenge is stale or outside DB authority"
            )
        _verify_signature(
            pin=database_authority_pin,
            signed_at=validation_challenge.message.issued_at,
            observed_at=observed_at,
            message=validation_challenge.signature_message,
            signature_ed25519_hex=validation_challenge.signature_ed25519_hex,
        )
        _verify_signature(
            pin=database_authority_pin,
            signed_at=committed_message.committed_at,
            observed_at=observed_at,
            message=committed_validation_receipt.signature_message,
            signature_ed25519_hex=committed_validation_receipt.signature_ed25519_hex,
        )
        _verify_signature(
            pin=database_authority_pin,
            signed_at=message.issued_at,
            observed_at=observed_at,
            message=challenge.signature_message,
            signature_ed25519_hex=challenge.signature_ed25519_hex,
        )
        return challenge
    except ScientificBridgeVerificationError:
        raise
    except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
        raise ScientificBridgeVerificationError(
            "admission issuance challenge failed closed verification"
        ) from exc


def issue_observation_admission_decision(
    *,
    committed_validation_receipt: CommittedObservationValidationReceipt,
    issuance_challenge: AdmissionIssuanceChallenge,
    disposition: ObservationAdmissionDisposition,
    reason_codes: tuple[str, ...],
    qualification_authority: QualificationAuthorityVerifier,
    action_authority: ResearchActionAuthorityVerificationPort,
    qualification_custody: EngineeringQualificationCustodyVerificationPort,
    raw_run_custody: RawRunCustodyVerificationPort,
    validation_campaign_custody: ObservationValidationCampaignVerificationPort,
    execution_authority_pin: ScientificBridgeAuthorityPin,
    validator_authority_pin: ScientificBridgeAuthorityPin,
    admission_authority_pin: ScientificBridgeAuthorityPin,
    database_authority_pin: ObservationDatabaseAuthorityPin,
    private_key: bytes,
) -> ObservationAdmissionDecision:
    """Sign an admission decision without claiming that its empty-slot CAS has committed."""

    _require_private_key(private_key, admission_authority_pin)
    decided_at = issuance_challenge.message.issued_at
    verify_committed_observation_validation_receipt(
        committed_receipt=committed_validation_receipt,
        qualification_authority=qualification_authority,
        action_authority=action_authority,
        qualification_custody=qualification_custody,
        raw_run_custody=raw_run_custody,
        validation_campaign_custody=validation_campaign_custody,
        execution_authority_pin=execution_authority_pin,
        validator_authority_pin=validator_authority_pin,
        admission_authority_pin=admission_authority_pin,
        database_authority_pin=database_authority_pin,
        observed_at=decided_at,
    )
    _verify_admission_issuance_challenge(
        challenge=issuance_challenge,
        committed_validation_receipt=committed_validation_receipt,
        database_authority_pin=database_authority_pin,
        observed_at=decided_at,
        require_live=True,
    )
    committed_validation_receipt = CommittedObservationValidationReceipt.model_validate(
        committed_validation_receipt.model_dump(mode="python")
    )
    receipt_message = committed_validation_receipt.message.receipt.message
    observation_sha256 = (
        receipt_message.scientific_observation_sha256
        if disposition is ObservationAdmissionDisposition.ADMITTED
        else None
    )
    message = ObservationAdmissionDecisionMessage(
        scientific_slot_id=receipt_message.scientific_slot_id,
        committed_validation_receipt=committed_validation_receipt,
        issuance_challenge=issuance_challenge,
        disposition=disposition,
        admitted_observation_sha256=observation_sha256,
        reason_codes=reason_codes,
        admission_authority_policy_sha256=admission_authority_pin.policy_sha256,
        decided_by_principal_id=admission_authority_pin.principal_id,
        decision_key_id=admission_authority_pin.key_id,
        decided_at=decided_at,
    )
    unsigned = ObservationAdmissionDecision(message=message, signature_ed25519_hex="0" * 128)
    signed = unsigned.model_copy(
        update={
            "signature_ed25519_hex": Ed25519PrivateKey.from_private_bytes(private_key)
            .sign(unsigned.signature_message)
            .hex()
        }
    )
    verify_observation_admission_decision(
        decision=signed,
        qualification_authority=qualification_authority,
        action_authority=action_authority,
        qualification_custody=qualification_custody,
        raw_run_custody=raw_run_custody,
        validation_campaign_custody=validation_campaign_custody,
        execution_authority_pin=execution_authority_pin,
        validator_authority_pin=validator_authority_pin,
        admission_authority_pin=admission_authority_pin,
        database_authority_pin=database_authority_pin,
        observed_at=decided_at,
    )
    return signed


def verify_observation_admission_decision(
    *,
    decision: ObservationAdmissionDecision,
    qualification_authority: QualificationAuthorityVerifier,
    action_authority: ResearchActionAuthorityVerificationPort,
    qualification_custody: EngineeringQualificationCustodyVerificationPort,
    raw_run_custody: RawRunCustodyVerificationPort,
    validation_campaign_custody: ObservationValidationCampaignVerificationPort,
    execution_authority_pin: ScientificBridgeAuthorityPin,
    validator_authority_pin: ScientificBridgeAuthorityPin,
    admission_authority_pin: ScientificBridgeAuthorityPin,
    database_authority_pin: ObservationDatabaseAuthorityPin,
    observed_at: datetime,
) -> None:
    """Verify nested authorization/validation signatures and the admission signature."""

    try:
        _require_utc(observed_at, "observation admission observed_at")
        decision = validate_observation_admission_decision_structure(decision)
        verify_committed_observation_validation_receipt(
            committed_receipt=decision.message.committed_validation_receipt,
            qualification_authority=qualification_authority,
            action_authority=action_authority,
            qualification_custody=qualification_custody,
            raw_run_custody=raw_run_custody,
            validation_campaign_custody=validation_campaign_custody,
            execution_authority_pin=execution_authority_pin,
            validator_authority_pin=validator_authority_pin,
            admission_authority_pin=admission_authority_pin,
            database_authority_pin=database_authority_pin,
            observed_at=observed_at,
        )
        verify_admission_issuance_challenge(
            challenge=decision.message.issuance_challenge,
            committed_validation_receipt=decision.message.committed_validation_receipt,
            database_authority_pin=database_authority_pin,
            observed_at=observed_at,
        )
        _require_message_pin(
            policy_sha256=decision.message.admission_authority_policy_sha256,
            principal_id=decision.message.decided_by_principal_id,
            key_id=decision.message.decision_key_id,
            pin=admission_authority_pin,
        )
        _verify_signature(
            pin=admission_authority_pin,
            signed_at=decision.message.decided_at,
            observed_at=observed_at,
            message=decision.signature_message,
            signature_ed25519_hex=decision.signature_ed25519_hex,
        )
    except ScientificBridgeVerificationError:
        raise
    except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
        raise ScientificBridgeVerificationError(
            "observation admission decision failed closed verification"
        ) from exc


def validate_observation_admission_decision_structure(
    decision: ObservationAdmissionDecision,
) -> ObservationAdmissionDecision:
    """Revalidate admission DTO structure only; this does not verify signatures or custody."""

    try:
        return ObservationAdmissionDecision.model_validate(decision.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ScientificBridgeVerificationError(
            "observation admission decision failed structural validation"
        ) from exc


def commit_observation_admission(
    *,
    decision: ObservationAdmissionDecision,
    qualification_authority: QualificationAuthorityVerifier,
    action_authority: ResearchActionAuthorityVerificationPort,
    qualification_custody: EngineeringQualificationCustodyVerificationPort,
    raw_run_custody: RawRunCustodyVerificationPort,
    validation_campaign_custody: ObservationValidationCampaignVerificationPort,
    execution_authority_pin: ScientificBridgeAuthorityPin,
    validator_authority_pin: ScientificBridgeAuthorityPin,
    admission_authority_pin: ScientificBridgeAuthorityPin,
    database_authority_pin: ObservationDatabaseAuthorityPin,
    private_key: bytes,
    registered_at: datetime,
    committed_at: datetime,
) -> CommittedObservationAdmission:
    """DB-sign the atomic empty-slot commit of one exact admission proposal."""

    decision = validate_observation_admission_decision_structure(decision)
    _require_private_key(private_key, database_authority_pin)
    if not database_authority_pin.active_at(committed_at):
        raise ScientificBridgeVerificationError(
            "admission commit is outside database key authority"
        )
    verify_observation_admission_decision(
        decision=decision,
        qualification_authority=qualification_authority,
        action_authority=action_authority,
        qualification_custody=qualification_custody,
        raw_run_custody=raw_run_custody,
        validation_campaign_custody=validation_campaign_custody,
        execution_authority_pin=execution_authority_pin,
        validator_authority_pin=validator_authority_pin,
        admission_authority_pin=admission_authority_pin,
        database_authority_pin=database_authority_pin,
        observed_at=committed_at,
    )
    decision_message = decision.message
    challenge = decision_message.issuance_challenge
    committed_validation = decision_message.committed_validation_receipt
    message = CommittedObservationAdmissionMessage(
        decision=decision,
        decision_sha256=decision.decision_sha256,
        committed_validation_receipt_sha256=(committed_validation.committed_receipt_sha256),
        exact_registered_validation_receipt_sha256=(
            committed_validation.message.validation_receipt_sha256
        ),
        issuance_challenge_sha256=challenge.challenge_sha256,
        row_scope=challenge.message.row_scope,
        registered_at=registered_at,
        committed_at=committed_at,
        database_authority_policy_sha256=database_authority_pin.policy_sha256,
        committed_by_principal_id=database_authority_pin.principal_id,
        commit_key_id=database_authority_pin.key_id,
    )
    unsigned = CommittedObservationAdmission(
        message=message,
        signature_ed25519_hex="0" * 128,
    )
    signed = unsigned.model_copy(
        update={
            "signature_ed25519_hex": Ed25519PrivateKey.from_private_bytes(private_key)
            .sign(unsigned.signature_message)
            .hex()
        }
    )
    verify_committed_observation_admission(
        committed_admission=signed,
        qualification_authority=qualification_authority,
        action_authority=action_authority,
        qualification_custody=qualification_custody,
        raw_run_custody=raw_run_custody,
        validation_campaign_custody=validation_campaign_custody,
        execution_authority_pin=execution_authority_pin,
        validator_authority_pin=validator_authority_pin,
        admission_authority_pin=admission_authority_pin,
        database_authority_pin=database_authority_pin,
        observed_at=committed_at,
    )
    return signed


def verify_committed_observation_admission(
    *,
    committed_admission: CommittedObservationAdmission,
    qualification_authority: QualificationAuthorityVerifier,
    action_authority: ResearchActionAuthorityVerificationPort,
    qualification_custody: EngineeringQualificationCustodyVerificationPort,
    raw_run_custody: RawRunCustodyVerificationPort,
    validation_campaign_custody: ObservationValidationCampaignVerificationPort,
    execution_authority_pin: ScientificBridgeAuthorityPin,
    validator_authority_pin: ScientificBridgeAuthorityPin,
    admission_authority_pin: ScientificBridgeAuthorityPin,
    database_authority_pin: ObservationDatabaseAuthorityPin,
    observed_at: datetime,
) -> CommittedObservationAdmission:
    """Verify the full authority chain ending in an atomic DB admission commit."""

    try:
        _require_utc(observed_at, "committed admission observed_at")
        committed_admission = CommittedObservationAdmission.model_validate(
            committed_admission.model_dump(mode="python")
        )
        message = committed_admission.message
        decision = message.decision
        receipt_message = decision.message.committed_validation_receipt.message.receipt.message
        authorization = receipt_message.raw_run.scientific_authorization.message
        _require_database_authority_separation(
            database=database_authority_pin,
            authorization=authorization,
            raw_run=receipt_message.raw_run,
        )
        _require_database_message_pin(
            policy_sha256=message.database_authority_policy_sha256,
            principal_id=message.committed_by_principal_id,
            key_id=message.commit_key_id,
            pin=database_authority_pin,
        )
        if (
            message.committed_at > observed_at
            or decision.message.issuance_challenge.message.expires_at
            > database_authority_pin.active_until
        ):
            raise ScientificBridgeVerificationError(
                "admission commitment is future-dated or outlives database authority"
            )
        verify_observation_admission_decision(
            decision=decision,
            qualification_authority=qualification_authority,
            action_authority=action_authority,
            qualification_custody=qualification_custody,
            raw_run_custody=raw_run_custody,
            validation_campaign_custody=validation_campaign_custody,
            execution_authority_pin=execution_authority_pin,
            validator_authority_pin=validator_authority_pin,
            admission_authority_pin=admission_authority_pin,
            database_authority_pin=database_authority_pin,
            observed_at=observed_at,
        )
        _verify_signature(
            pin=database_authority_pin,
            signed_at=message.committed_at,
            observed_at=observed_at,
            message=committed_admission.signature_message,
            signature_ed25519_hex=committed_admission.signature_ed25519_hex,
        )
        return committed_admission
    except ScientificBridgeVerificationError:
        raise
    except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
        raise ScientificBridgeVerificationError(
            "committed observation admission failed closed verification"
        ) from exc


def _signature_message(kind: str, payload: BaseModel) -> bytes:
    return _BRIDGE_SIGNATURE_CONTEXT + kind.encode("ascii") + b"\0" + canonical_json_bytes(payload)


def _require_utc(timestamp: datetime, label: str) -> None:
    if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
        raise ScientificBridgeVerificationError(f"{label} must be timezone-aware UTC")


def _require_private_key(
    private_key: bytes,
    pin: ScientificBridgeAuthorityPin | ObservationDatabaseAuthorityPin,
) -> None:
    try:
        public_key = (
            Ed25519PrivateKey.from_private_bytes(private_key)
            .public_key()
            .public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            .hex()
        )
    except ValueError as exc:
        raise ScientificBridgeVerificationError("bridge private key must contain 32 bytes") from exc
    if public_key != pin.public_key_ed25519_hex:
        raise ScientificBridgeVerificationError("bridge private key differs from external pin")


def _require_pin_roles(
    *,
    execution: ScientificBridgeAuthorityPin,
    validator: ScientificBridgeAuthorityPin,
    admission: ScientificBridgeAuthorityPin,
) -> None:
    pins = tuple(
        ScientificBridgeAuthorityPin.model_validate(item.model_dump(mode="python"))
        for item in (execution, validator, admission)
    )
    if tuple(item.role for item in pins) != (
        ScientificBridgeRole.EXECUTION_AUTHORIZER,
        ScientificBridgeRole.OBSERVATION_VALIDATOR,
        ScientificBridgeRole.OBSERVATION_ADMITTER,
    ):
        raise ScientificBridgeVerificationError("bridge authority pins have the wrong roles")
    for label, values in (
        ("principals", tuple(item.principal_id for item in pins)),
        ("keys", tuple(item.key_id for item in pins)),
        ("policies", tuple(item.policy_sha256 for item in pins)),
    ):
        if len(values) != len(set(values)):
            raise ScientificBridgeVerificationError(f"bridge authority {label} must be separate")


def _require_message_pin(
    *,
    policy_sha256: str,
    principal_id: str,
    key_id: str,
    pin: ScientificBridgeAuthorityPin | ObservationDatabaseAuthorityPin,
) -> None:
    if (policy_sha256, principal_id, key_id) != (
        pin.policy_sha256,
        pin.principal_id,
        pin.key_id,
    ):
        raise ScientificBridgeVerificationError("signed authority differs from external pin")


def _verify_signature(
    *,
    pin: ScientificBridgeAuthorityPin | ObservationDatabaseAuthorityPin,
    signed_at: datetime,
    observed_at: datetime,
    message: bytes,
    signature_ed25519_hex: str,
) -> None:
    if observed_at < signed_at or not pin.active_at(signed_at):
        raise ScientificBridgeVerificationError("bridge signature is outside pinned key validity")
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pin.public_key_ed25519_hex)).verify(
            bytes.fromhex(signature_ed25519_hex),
            message,
        )
    except (InvalidSignature, ValueError) as exc:
        raise ScientificBridgeVerificationError("bridge signature is invalid") from exc


def _require_database_authority_separation(
    *,
    database: ObservationDatabaseAuthorityPin,
    authorization: ScientificExecutionAuthorizationMessage,
    raw_run: RawRunEnvelope | None = None,
) -> None:
    database = ObservationDatabaseAuthorityPin.model_validate(database.model_dump(mode="python"))
    grant = authorization.qualification_grant.message
    principals = {
        authorization.action_protocol_binding.action.proposed_by_principal_id,
        authorization.action_protocol_binding.action_authorized_event.principal_id,
        authorization.authorized_by_principal_id,
        authorization.validator_principal_id,
        authorization.admission_principal_id,
        grant.authorized_by_principal_id,
        authorization.qualification_bundle.cost_quote.quoted_by_principal_id,
        authorization.qualification_bundle.budget_authorization.authorized_by_principal_id,
    }
    keys = {
        authorization.authorization_key_id,
        authorization.validator_key_id,
        authorization.admission_key_id,
        grant.authorization_key_id,
    }
    policies = {
        authorization.execution_authority_policy_sha256,
        authorization.validator_authority_policy_sha256,
        authorization.admission_authority_policy_sha256,
        grant.qualification_authority_policy_sha256,
        authorization.qualification_bundle.cost_quote.pricing_policy_sha256,
    }
    if raw_run is not None:
        accepted = raw_run.accepted_runtime_termination
        terminal = raw_run.accepted_terminal_submission
        principals.update((accepted.accepted_by_principal_id, terminal.accepted_by_principal_id))
        principals.update(
            receipt.verifier_principal_id for receipt in raw_run.artifact_verified_receipts
        )
        keys.update(
            (
                accepted.acceptance_key_id,
                terminal.acceptance_key_id,
                raw_run.terminal_submission.signing_key_id,
            )
        )
        policies.update(
            (accepted.runtime_control_policy_sha256, terminal.runtime_control_policy_sha256)
        )
    if (
        database.principal_id in principals
        or database.key_id in keys
        or database.policy_sha256 in policies
    ):
        raise ScientificBridgeVerificationError(
            "observation database principal, key, and policy must be independent"
        )


def _require_database_message_pin(
    *,
    policy_sha256: str,
    principal_id: str,
    key_id: str,
    pin: ObservationDatabaseAuthorityPin,
) -> None:
    _require_message_pin(
        policy_sha256=policy_sha256,
        principal_id=principal_id,
        key_id=key_id,
        pin=pin,
    )


def _require_grant_matches_bundle(
    *,
    bundle: EngineeringQualificationBundle,
    grant: EngineeringQualificationGrant,
) -> None:
    request = bundle.compilation_request
    result = bundle.compilation_result
    intent = bundle.intent
    prior_sha256 = (
        bundle.prior_execution_receipt.execution_receipt_sha256
        if bundle.prior_execution_receipt is not None
        else None
    )
    expected: dict[str, object] = {
        "bundle_sha256": bundle.bundle_sha256,
        "quest_id": intent.quest_id,
        "graph_scope_sha256": request.protocol.graph_scope.graph_scope_sha256,
        "protocol_sha256": intent.protocol_sha256,
        "compilation_request_sha256": execution_canonical_sha256(request),
        "compilation_result_sha256": execution_canonical_sha256(result),
        "compilation_receipt_sha256": result.receipt.receipt_sha256,
        "work_order_id": bundle.work_order.work_order_id,
        "work_order_sha256": bundle.work_order.work_order_sha256,
        "intent_sha256": intent.intent_sha256,
        "execution_id": intent.execution_id,
        "replicate_slot_id": intent.replicate_slot.replicate_slot_id,
        "infrastructure_attempt_id": intent.infrastructure_attempt.infrastructure_attempt_id,
        "input_artifact_verified_receipt_sha256s": (bundle.input_artifact_verified_receipt_sha256s),
        "prior_execution_receipt_sha256": prior_sha256,
        "budget_authorization_sha256": bundle.budget_authorization.authorization_sha256,
        "cost_quote_sha256": bundle.cost_quote.quote_sha256,
        "qualification_only": True,
        "scientific_admission_allowed": False,
    }
    if any(getattr(grant.message, key) != value for key, value in expected.items()):
        raise ValueError("qualification grant is rebound from its exact frozen bundle")


def _artifact_receipt_hashes(
    *,
    manifest: ArtifactManifest,
    receipts: tuple[ArtifactVerifiedReceipt, ...],
) -> tuple[str, ...]:
    if tuple(item.artifact.artifact_key for item in receipts) != tuple(
        item.artifact_key for item in manifest.entries
    ):
        raise ValueError("artifact receipts must exactly follow manifest artifact-key order")
    for entry, receipt in zip(manifest.entries, receipts, strict=True):
        if (
            receipt.artifact_manifest_sha256 != manifest.manifest_sha256
            or receipt.producer_attempt_id != manifest.infrastructure_attempt_id
            or receipt.artifact != entry
        ):
            raise ValueError("artifact receipt differs from its exact raw-run manifest entry")
    return tuple(sorted(item.verified_receipt_sha256 for item in receipts))


def _validate_artifact_manifest_against_intent(
    *,
    intent: ExecutionIntent,
    manifest: ArtifactManifest,
    success: bool,
) -> None:
    expected = {item.artifact_key: item for item in intent.expected_artifacts}
    entries = {item.artifact_key: item for item in manifest.entries}
    if set(entries) - set(expected):
        raise ValueError("artifact manifest contains undeclared output")
    if sum(item.bytes for item in manifest.entries) > intent.resource_request.artifact_quota_bytes:
        raise ValueError("artifact manifest exceeds the frozen aggregate artifact quota")
    for key, entry in entries.items():
        requirement = expected[key]
        if (
            entry.expected_artifact_id != requirement.expected_artifact_id
            or entry.role is not requirement.role
            or entry.media_type != requirement.media_type
            or entry.schema_sha256 != requirement.schema_sha256
            or entry.bytes > requirement.max_bytes
        ):
            raise ValueError("artifact manifest violates its frozen expectation")
    if success and any(
        requirement.required and key not in entries for key, requirement in expected.items()
    ):
        raise ValueError("successful raw run is missing a required artifact")


def _terminal_disposition(
    *,
    intent: ExecutionIntent,
    accepted: AcceptedRuntimeTermination,
    manifest: ArtifactManifest,
) -> Literal["process_succeeded", "process_failed", "invalid_output", "timeout"]:
    actual_keys = {item.artifact_key for item in manifest.entries}
    missing_required = any(
        item.required and item.artifact_key not in actual_keys for item in intent.expected_artifacts
    )
    if accepted.exit_code != 0:
        return "process_failed"
    if accepted.runtime_ended_at > min(intent.deadline, accepted.hard_deadline):
        return "timeout"
    if missing_required:
        return "invalid_output"
    return "process_succeeded"


def _mapped_outcome(
    policy: ObservationAdmissionPolicy,
    outcome_bin_id: str,
) -> ScientificObservationOutcome:
    matches = tuple(
        item.outcome
        for item in policy.outcome_bin_mappings
        if item.outcome_bin_id == outcome_bin_id
    )
    if len(matches) != 1:
        raise ScientificBridgeVerificationError(
            "validated F9 outcome bin is absent from the frozen admission policy"
        )
    return matches[0]


def _scientific_observation_sha256(
    *,
    scientific_slot_id: str,
    projection: VerifiedObservationValidationCampaignProjection,
    outcome: ScientificObservationOutcome,
) -> str:
    if projection.validation_batch_sha256 is None:
        raise ScientificBridgeVerificationError("validated projection lacks an observation batch")
    return canonical_sha256(
        {
            "schema_name": "aletheia.scientific_observation_identity",
            "schema_version": SCIENTIFIC_BRIDGE_SCHEMA_VERSION,
            "scientific_slot_id": scientific_slot_id,
            "validation_campaign_sha256": projection.campaign_sha256,
            "validation_batch_sha256": projection.validation_batch_sha256,
            "outcome": outcome.value,
        }
    )


class ResearchActionAuthorityVerificationPort(Protocol):
    """Port for replaying Kernel CAS/events and verifying the authorized graph snapshot."""

    def verify_action_protocol_binding(
        self,
        *,
        binding: ScientificActionProtocolBinding,
        observed_at: datetime,
    ) -> str: ...


class EngineeringQualificationCustodyVerificationPort(Protocol):
    """Port for complete PR-4 registry/input custody verification before bridge signing.

    ``verify_qualification_admission`` must resolve and verify the explicit stable admission hash;
    receiving a syntactically valid SHA-256 value is never evidence that the admission existed.
    """

    def verify_engineering_qualification_custody(
        self,
        *,
        bundle: EngineeringQualificationBundle,
        grant: EngineeringQualificationGrant,
        observed_at: datetime,
    ) -> VerifiedEngineeringQualification: ...

    def verify_qualification_admission(
        self,
        *,
        qualification_admission_sha256: str,
        bundle: EngineeringQualificationBundle,
        grant: EngineeringQualificationGrant,
        observed_at: datetime,
    ) -> VerifiedEngineeringQualification: ...


class RawRunCustodyVerificationPort(Protocol):
    """Port for historical prelaunch registration and full run-lineage verification.

    A conforming adapter must prove that this exact SEA was durably registered before the exact
    PR-4 qualification admission/reservation/launch, then verify the full runtime/node signatures
    and fresh CAS byte custody.  The bridge's structural timestamps are not a substitute for that
    historical authority evidence.
    """

    def verify_raw_run_custody(
        self,
        *,
        raw_run: RawRunEnvelope,
        observed_at: datetime,
    ) -> VerifiedRawRunCustodyProjection: ...


class ObservationValidationCampaignVerificationPort(Protocol):
    """Port for resolving and verifying the exact F9 campaign/archive custody."""

    def verify_observation_validation_campaign(
        self,
        *,
        campaign_sha256: str,
        raw_run: RawRunEnvelope,
        expected_validator_manifest_sha256: str,
        expected_observation_validation_policy_sha256: str,
        observed_at: datetime,
    ) -> VerifiedObservationValidationCampaignProjection: ...


class ObservationAdmissionCommitPort(Protocol):
    """Port for atomically committing only while the scientific slot is still empty.

    Implementations need a database uniqueness constraint and transactional comparison.  Merely
    satisfying this Python protocol does not confer CAS, persistence, or exactly-once semantics.
    """

    def commit_if_scientific_slot_empty(
        self,
        *,
        decision: ObservationAdmissionDecision,
        observed_at: datetime,
    ) -> CommittedObservationAdmission: ...


__all__ = [
    "AdmissionIssuanceChallenge",
    "AdmissionIssuanceChallengeMessage",
    "BridgeValidationDisposition",
    "CommittedObservationAdmission",
    "CommittedObservationAdmissionMessage",
    "CommittedObservationValidationReceipt",
    "CommittedObservationValidationReceiptMessage",
    "EngineeringQualificationCustodyVerificationPort",
    "ObservationAdmissionCommitPort",
    "ObservationAdmissionDecision",
    "ObservationAdmissionDecisionMessage",
    "ObservationAdmissionDisposition",
    "ObservationAdmissionPolicy",
    "ObservationDatabaseAuthorityPin",
    "ObservationValidationCampaignVerificationPort",
    "ObservationValidationReceipt",
    "ObservationValidationReceiptMessage",
    "RawRunCustodyVerificationPort",
    "RawRunEnvelope",
    "ResearchActionAuthorityVerificationPort",
    "SCIENTIFIC_BRIDGE_SCHEMA_VERSION",
    "ScientificActionProtocolBinding",
    "ScientificBridgeAuthorityPin",
    "ScientificBridgeRole",
    "ScientificBridgeVerificationError",
    "ScientificExecutionAuthorization",
    "ScientificExecutionAuthorizationMessage",
    "ScientificObservationArtifactBinding",
    "ScientificObservationOutcome",
    "ScientificOutcomeBinMapping",
    "ValidationIssuanceChallenge",
    "ValidationIssuanceChallengeMessage",
    "VerifiedArtifactCustodyProjection",
    "VerifiedExecutionAuthorityProjection",
    "VerifiedObservationValidationCampaignProjection",
    "VerifiedRawRunCustodyProjection",
    "commit_observation_admission",
    "commit_observation_validation_receipt",
    "engineering_qualification_admission_sha256",
    "issue_admission_issuance_challenge",
    "issue_observation_admission_decision",
    "issue_observation_validation_receipt",
    "issue_scientific_execution_authorization",
    "issue_validation_issuance_challenge",
    "scientific_bridge_key_id",
    "scientific_observation_row_scope",
    "verify_admission_issuance_challenge",
    "verify_committed_observation_admission",
    "verify_committed_observation_validation_receipt",
    "verify_observation_admission_decision",
    "verify_observation_validation_receipt",
    "verify_raw_run_for_independent_validation",
    "verify_scientific_execution_authorization",
    "verify_scientific_execution_authorization_historical",
    "verify_validation_issuance_challenge",
    "validate_observation_admission_decision_structure",
    "validate_observation_validation_receipt_structure",
    "validate_raw_run_structure",
    "validate_scientific_execution_authorization_structure",
]
