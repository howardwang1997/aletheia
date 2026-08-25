"""Atomic, domain-independent capability manifests for the PR-3 compiler.

A manifest describes exactly one operation.  Composition belongs in a protocol/work-order DAG;
putting an internal planner or workflow inside a capability would recreate an unreviewable second
controller and is therefore outside this contract.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.execution.schemas import ExecutionEffectClass
from aletheia.protocols.base import (
    LOCAL_ID_PATTERN,
    PRINCIPAL_ID_PATTERN,
    SEMVER_PATTERN,
    SHA256_PATTERN,
    JsonSchemaRef,
    ProtocolModel,
    canonical_sha256,
    canonical_sha256s,
    canonical_strings,
)
from aletheia.protocols.claim_contracts import ClaimCeiling, EpistemicKind


class PortDirection(str, Enum):
    INPUT = "input"
    OUTPUT = "output"


class PortMultiplicity(str, Enum):
    ONE = "one"
    OPTIONAL = "optional"
    MANY = "many"


class ArtifactKind(str, Enum):
    JSON = "json"
    TABLE = "table"
    TEXT = "text"
    BINARY = "binary"
    MODEL = "model"
    SAMPLE = "sample"
    MEASUREMENT = "measurement"
    PROOF = "proof"
    RECEIPT = "receipt"


class DataClassification(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"
    PRIVATE_EVALUATION = "private_evaluation"
    REGULATED = "regulated"


class CapabilityPort(ProtocolModel):
    port_id: str = Field(pattern=LOCAL_ID_PATTERN)
    direction: PortDirection
    schema_ref: JsonSchemaRef
    artifact_kind: ArtifactKind
    data_classification: DataClassification
    multiplicity: PortMultiplicity = PortMultiplicity.ONE
    unit_or_ontology_refs: tuple[str, ...] = Field(default=(), max_length=64)
    identity_lineage_required: bool = True
    description: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def _port_is_canonical(self) -> "CapabilityPort":
        canonical_strings(self.unit_or_ontology_refs, "port unit/ontology refs")
        return self


class SideEffectClass(str, Enum):
    NONE = "none"
    READ_ONLY_EXTERNAL = "read_only_external"
    EPHEMERAL_WRITE = "ephemeral_write"
    DURABLE_WRITE = "durable_write"
    EXTERNAL_MUTATION = "external_mutation"
    PHYSICAL_ACTION = "physical_action"


class PrincipalKind(str, Enum):
    SERVICE = "service"
    AGENT = "agent"
    HUMAN = "human"
    INSTRUMENT = "instrument"
    ROBOT = "robot"


class PrincipalContract(ProtocolModel):
    executor_principal_id: str = Field(pattern=PRINCIPAL_ID_PATTERN)
    principal_kind: PrincipalKind
    authority_policy_sha256: str = Field(pattern=SHA256_PATTERN)
    credential_class: str = Field(pattern=LOCAL_ID_PATTERN)
    required_independence_groups: tuple[str, ...] = Field(default=(), max_length=32)
    human_intervention_must_be_recorded: Literal[True] = True

    @model_validator(mode="after")
    def _groups_are_canonical(self) -> "PrincipalContract":
        canonical_strings(self.required_independence_groups, "principal independence groups")
        return self


class RuntimeKind(str, Enum):
    DETERMINISTIC_FUNCTION = "deterministic_function"
    HARDENED_SANDBOX = "hardened_sandbox"
    DIGEST_PINNED_CONTAINER = "digest_pinned_container"
    EXTERNAL_SERVICE = "external_service"
    PHYSICAL_SITE = "physical_site"
    HUMAN_PROCEDURE = "human_procedure"


class DeterminismClass(str, Enum):
    DETERMINISTIC = "deterministic"
    FROZEN_SEEDS = "frozen_seeds"
    DECLARED_STOCHASTIC = "declared_stochastic"


class RuntimeContract(ProtocolModel):
    runtime_kind: RuntimeKind
    adapter_ref: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_.]*:[a-zA-Z_][a-zA-Z0-9_.]*$")
    implementation_sha256: str = Field(pattern=SHA256_PATTERN)
    environment_sha256: str = Field(pattern=SHA256_PATTERN)
    determinism: DeterminismClass
    frozen_seeds: tuple[int, ...] = Field(default=(), max_length=1024)
    maximum_wall_time_seconds: int = Field(gt=0)
    checkpoint_supported: bool
    reconciliation_supported: bool

    @model_validator(mode="after")
    def _randomness_is_explicit(self) -> "RuntimeContract":
        if self.frozen_seeds != tuple(sorted(set(self.frozen_seeds))):
            raise ValueError("runtime seeds must be unique and canonical")
        if self.determinism is DeterminismClass.DETERMINISTIC and self.frozen_seeds:
            raise ValueError("deterministic runtime cannot declare random seeds")
        if self.determinism is DeterminismClass.FROZEN_SEEDS and not self.frozen_seeds:
            raise ValueError("frozen-seed runtime requires at least one seed")
        return self


class ApplicabilityContract(ProtocolModel):
    epistemic_kinds: tuple[EpistemicKind, ...] = Field(min_length=1, max_length=16)
    domain_tags: tuple[str, ...] = Field(default=(), max_length=64)
    required_condition_sha256s: tuple[str, ...] = Field(default=(), max_length=128)
    excluded_condition_sha256s: tuple[str, ...] = Field(default=(), max_length=128)
    minimum_batch_size: int = Field(default=1, ge=1)
    maximum_batch_size: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _applicability_is_canonical(self) -> "ApplicabilityContract":
        kinds = tuple(item.value for item in self.epistemic_kinds)
        if kinds != tuple(sorted(set(kinds))):
            raise ValueError("applicable epistemic kinds must be unique and canonical")
        canonical_strings(self.domain_tags, "capability domain tags")
        canonical_sha256s(self.required_condition_sha256s, "required applicability conditions")
        canonical_sha256s(self.excluded_condition_sha256s, "excluded applicability conditions")
        if set(self.required_condition_sha256s) & set(self.excluded_condition_sha256s):
            raise ValueError("required and excluded applicability conditions must be disjoint")
        if (
            self.maximum_batch_size is not None
            and self.maximum_batch_size < self.minimum_batch_size
        ):
            raise ValueError("maximum batch size cannot be below minimum batch size")
        return self


class CalibrationMode(str, Enum):
    NOT_APPLICABLE = "not_applicable"
    SELF_CHECK = "self_check"
    REFERENCE_STANDARD = "reference_standard"
    EXTERNAL_CERTIFICATION = "external_certification"


class CalibrationContract(ProtocolModel):
    mode: CalibrationMode
    calibration_receipt_schema: JsonSchemaRef | None = None
    maximum_age_seconds: int | None = Field(default=None, gt=0)
    operating_envelope_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _calibration_fields_match_mode(self) -> "CalibrationContract":
        specified = (
            self.calibration_receipt_schema is not None
            and self.maximum_age_seconds is not None
            and self.operating_envelope_sha256 is not None
        )
        if self.mode is CalibrationMode.NOT_APPLICABLE and any(
            value is not None
            for value in (
                self.calibration_receipt_schema,
                self.maximum_age_seconds,
                self.operating_envelope_sha256,
            )
        ):
            raise ValueError("non-calibrated capability cannot carry calibration fields")
        if self.mode is not CalibrationMode.NOT_APPLICABLE and not specified:
            raise ValueError("calibrated capability requires receipt schema, age, and envelope")
        return self


class FailureCategory(str, Enum):
    INFRASTRUCTURE = "infrastructure"
    EXECUTION = "execution"
    MEASUREMENT = "measurement"
    INVALID_OUTPUT = "invalid_output"
    POLICY = "policy"
    SAFETY = "safety"


class FailureDisposition(str, Enum):
    RETRYABLE = "retryable"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    INVALID_ATTEMPT = "invalid_attempt"
    BLOCKED = "blocked"
    TERMINAL = "terminal"


class FailureMode(ProtocolModel):
    failure_id: str = Field(pattern=LOCAL_ID_PATTERN)
    category: FailureCategory
    description: str = Field(min_length=1, max_length=2_000)
    detection_rule_sha256: str = Field(pattern=SHA256_PATTERN)
    disposition: FailureDisposition


class RetryMode(str, Enum):
    NEVER = "never"
    IDEMPOTENT_NEW_ATTEMPT = "idempotent_new_attempt"
    RECONCILE_THEN_NEW_ATTEMPT = "reconcile_then_new_attempt"
    CHECKPOINT_RESUME = "checkpoint_resume"


class RetryContract(ProtocolModel):
    mode: RetryMode
    maximum_attempts_per_scientific_slot: int = Field(ge=1, le=100)
    retryable_failure_ids: tuple[str, ...] = Field(default=(), max_length=64)
    idempotency_rule_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    reconciliation_rule_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    checkpoint_schema: JsonSchemaRef | None = None
    best_of_n_forbidden: Literal[True] = True

    @model_validator(mode="after")
    def _retry_contract_is_closed(self) -> "RetryContract":
        canonical_strings(self.retryable_failure_ids, "retryable failure IDs")
        if self.mode is RetryMode.NEVER:
            if (
                self.maximum_attempts_per_scientific_slot != 1
                or self.retryable_failure_ids
                or self.idempotency_rule_sha256 is not None
                or self.reconciliation_rule_sha256 is not None
                or self.checkpoint_schema is not None
            ):
                raise ValueError(
                    "non-retryable capability must have one attempt and no retry rules"
                )
            return self
        if self.maximum_attempts_per_scientific_slot < 2 or not self.retryable_failure_ids:
            raise ValueError("retry modes require multiple attempts and typed retryable failures")
        if self.idempotency_rule_sha256 is None:
            raise ValueError("retry modes require an idempotency rule")
        if (
            self.mode is RetryMode.RECONCILE_THEN_NEW_ATTEMPT
            and self.reconciliation_rule_sha256 is None
        ):
            raise ValueError("reconcile-before-retry mode requires a reconciliation rule")
        if (
            self.mode is not RetryMode.RECONCILE_THEN_NEW_ATTEMPT
            and self.reconciliation_rule_sha256 is not None
        ):
            raise ValueError("only reconcile-before-retry mode may carry a reconciliation rule")
        if self.mode is RetryMode.CHECKPOINT_RESUME and self.checkpoint_schema is None:
            raise ValueError("checkpoint-resume mode requires a checkpoint schema")
        if self.mode is not RetryMode.CHECKPOINT_RESUME and self.checkpoint_schema is not None:
            raise ValueError("only checkpoint-resume mode may carry a checkpoint schema")
        return self


class SafetyClass(str, Enum):
    READ_ONLY = "read_only"
    LOW_RISK_COMPUTE = "low_risk_compute"
    CONTROLLED_COMPUTE = "controlled_compute"
    NETWORKED_EXTERNAL = "networked_external"
    PHYSICAL_HAZARD = "physical_hazard"


class SafetyContract(ProtocolModel):
    safety_class: SafetyClass
    hazard_sha256s: tuple[str, ...] = Field(default=(), max_length=128)
    approval_policy_sha256: str = Field(pattern=SHA256_PATTERN)
    interlock_receipt_schema: JsonSchemaRef | None = None
    emergency_stop_required: bool = False

    @model_validator(mode="after")
    def _safety_requirements_are_canonical(self) -> "SafetyContract":
        canonical_sha256s(self.hazard_sha256s, "safety hazards")
        if self.safety_class is SafetyClass.PHYSICAL_HAZARD and (
            not self.hazard_sha256s
            or self.interlock_receipt_schema is None
            or not self.emergency_stop_required
        ):
            raise ValueError(
                "physical hazard requires hazards, interlock receipt, and emergency stop"
            )
        return self


class NetworkEgressMode(str, Enum):
    NONE = "none"
    ALLOWLISTED = "allowlisted"
    SITE_MANAGED = "site_managed"


class LicenseEgressContract(ProtocolModel):
    license_policy_sha256: str = Field(pattern=SHA256_PATTERN)
    permitted_input_classes: tuple[DataClassification, ...] = Field(min_length=1, max_length=8)
    output_license_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    network_egress: NetworkEgressMode
    allowlisted_destination_ids: tuple[str, ...] = Field(default=(), max_length=128)
    egress_policy_sha256: str = Field(pattern=SHA256_PATTERN)
    retention_policy_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _license_and_egress_are_canonical(self) -> "LicenseEgressContract":
        classes = tuple(item.value for item in self.permitted_input_classes)
        if classes != tuple(sorted(set(classes))):
            raise ValueError("permitted data classes must be unique and canonical")
        canonical_strings(self.output_license_ids, "output license IDs", required=True)
        canonical_strings(self.allowlisted_destination_ids, "egress destinations")
        if (self.network_egress is NetworkEgressMode.ALLOWLISTED) != bool(
            self.allowlisted_destination_ids
        ):
            raise ValueError("only allowlisted egress requires nonempty destination IDs")
        return self


class QualificationStatus(str, Enum):
    PROVISIONAL = "provisional"
    QUALIFIED = "qualified"
    SUSPENDED = "suspended"
    RETIRED = "retired"


class QualificationContract(ProtocolModel):
    status: QualificationStatus
    qualification_rule_sha256: str = Field(pattern=SHA256_PATTERN)
    evidence_receipt_sha256s: tuple[str, ...] = Field(default=(), max_length=128)
    qualified_by_principal_id: str | None = Field(default=None, pattern=PRINCIPAL_ID_PATTERN)
    qualified_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _qualification_is_attributable(self) -> "QualificationContract":
        canonical_sha256s(self.evidence_receipt_sha256s, "qualification evidence")
        attributed = (
            bool(self.evidence_receipt_sha256s)
            and self.qualified_by_principal_id is not None
            and self.qualified_at is not None
        )
        if self.status is QualificationStatus.PROVISIONAL:
            if self.evidence_receipt_sha256s or any(
                value is not None
                for value in (
                    self.qualified_by_principal_id,
                    self.qualified_at,
                    self.expires_at,
                )
            ):
                raise ValueError("provisional capability cannot claim qualification evidence")
        elif not attributed:
            raise ValueError(
                "qualified/suspended/retired capability must retain qualification evidence"
            )
        if self.expires_at is not None and (
            self.qualified_at is None or self.expires_at <= self.qualified_at
        ):
            raise ValueError("qualification expiry must follow qualification time")
        return self


class CapabilityManifestV2(ProtocolModel):
    """Frozen contract for one atomic capability operation."""

    schema_name: Literal["aletheia.capability_manifest"] = "aletheia.capability_manifest"
    schema_version: Literal[2] = 2
    capability_id: str = Field(pattern=LOCAL_ID_PATTERN)
    semantic_version: str = Field(pattern=SEMVER_PATTERN)
    supersedes_manifest_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    operation_id: str = Field(pattern=LOCAL_ID_PATTERN)
    external_action_kind: str | None = Field(default=None, pattern=LOCAL_ID_PATTERN)
    atomic_operation: Literal[True] = True
    title: str = Field(min_length=1, max_length=512)
    description: str = Field(min_length=1, max_length=4_000)
    input_ports: tuple[CapabilityPort, ...] = Field(default=(), max_length=128)
    output_ports: tuple[CapabilityPort, ...] = Field(min_length=1, max_length=128)
    side_effect_class: SideEffectClass
    principal: PrincipalContract
    runtime: RuntimeContract
    applicability: ApplicabilityContract
    calibration: CalibrationContract
    failure_modes: tuple[FailureMode, ...] = Field(min_length=1, max_length=128)
    retry: RetryContract
    safety: SafetyContract
    license_egress: LicenseEgressContract
    qualification: QualificationContract
    claim_ceiling: ClaimCeiling
    frozen_by_principal_id: str = Field(pattern=PRINCIPAL_ID_PATTERN)
    frozen_at: AwareDatetime

    @model_validator(mode="after")
    def _manifest_is_one_closed_operation(self) -> "CapabilityManifestV2":
        external_runtime = self.runtime.runtime_kind in {
            RuntimeKind.EXTERNAL_SERVICE,
            RuntimeKind.PHYSICAL_SITE,
            RuntimeKind.HUMAN_PROCEDURE,
        }
        if external_runtime != (self.external_action_kind is not None):
            raise ValueError("external runtimes require one explicit external action kind")
        if (
            self.side_effect_class
            in {
                SideEffectClass.READ_ONLY_EXTERNAL,
                SideEffectClass.DURABLE_WRITE,
                SideEffectClass.EXTERNAL_MUTATION,
                SideEffectClass.PHYSICAL_ACTION,
            }
            and not external_runtime
        ):
            raise ValueError("external effects require an external runtime")
        if (
            self.qualification.qualified_at is not None
            and self.qualification.qualified_at > self.frozen_at
        ):
            raise ValueError("capability qualification cannot postdate the frozen manifest")
        if self.qualification.qualified_by_principal_id in {
            self.frozen_by_principal_id,
            self.principal.executor_principal_id,
        }:
            raise ValueError("capability author/executor cannot approve its qualification")
        for ports, direction, label in (
            (self.input_ports, PortDirection.INPUT, "input ports"),
            (self.output_ports, PortDirection.OUTPUT, "output ports"),
        ):
            ids = tuple(item.port_id for item in ports)
            if ids != tuple(sorted(set(ids))):
                raise ValueError(f"capability {label} must be unique and canonical")
            if any(item.direction is not direction for item in ports):
                raise ValueError(f"capability {label} have the wrong direction")
        if {item.port_id for item in self.input_ports} & {
            item.port_id for item in self.output_ports
        }:
            raise ValueError("input and output port IDs must be disjoint")
        failure_ids = tuple(item.failure_id for item in self.failure_modes)
        if failure_ids != tuple(sorted(set(failure_ids))):
            raise ValueError("capability failure modes must be unique and canonical")
        retryable = set(self.retry.retryable_failure_ids)
        known = {item.failure_id for item in self.failure_modes}
        if not retryable.issubset(known):
            raise ValueError("retry contract references an undeclared failure mode")
        retry_dispositions = {
            item.failure_id
            for item in self.failure_modes
            if item.disposition
            in {FailureDisposition.RETRYABLE, FailureDisposition.RECONCILIATION_REQUIRED}
        }
        if retryable != retry_dispositions:
            raise ValueError("retry contract must cover exactly the retryable failure modes")
        if any(
            item.disposition
            in {FailureDisposition.RETRYABLE, FailureDisposition.RECONCILIATION_REQUIRED}
            and item.category not in {FailureCategory.INFRASTRUCTURE, FailureCategory.EXECUTION}
            for item in self.failure_modes
        ):
            raise ValueError("only infrastructure or execution failures may authorize recovery")
        reconciliation_required = {
            item.failure_id
            for item in self.failure_modes
            if item.disposition is FailureDisposition.RECONCILIATION_REQUIRED
        }
        if reconciliation_required and self.retry.mode is not RetryMode.RECONCILE_THEN_NEW_ATTEMPT:
            raise ValueError(
                "reconciliation-required failures cannot authorize a direct new attempt"
            )
        if self.retry.mode is RetryMode.CHECKPOINT_RESUME and not self.runtime.checkpoint_supported:
            raise ValueError("checkpoint retry requires runtime checkpoint support")
        if (
            self.retry.mode is RetryMode.RECONCILE_THEN_NEW_ATTEMPT
            and not self.runtime.reconciliation_supported
        ):
            raise ValueError("reconciliation retry requires runtime reconciliation support")
        if (
            self.execution_effect_class is ExecutionEffectClass.ONE_TIME_EXTERNAL
            and self.retry.mode is not RetryMode.NEVER
        ):
            raise ValueError("one-time external effects cannot authorize another attempt")
        physical = self.side_effect_class is SideEffectClass.PHYSICAL_ACTION
        if physical != (self.safety.safety_class is SafetyClass.PHYSICAL_HAZARD):
            raise ValueError("physical actions require, and uniquely use, physical-hazard safety")
        if physical and self.runtime.runtime_kind not in {
            RuntimeKind.PHYSICAL_SITE,
            RuntimeKind.HUMAN_PROCEDURE,
        }:
            raise ValueError("physical action requires a physical-site or human runtime")
        return self

    @property
    def manifest_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def execution_effect_class(self) -> ExecutionEffectClass:
        if self.side_effect_class in {
            SideEffectClass.NONE,
            SideEffectClass.READ_ONLY_EXTERNAL,
            SideEffectClass.EPHEMERAL_WRITE,
        }:
            return ExecutionEffectClass.REPLAY_SAFE
        if (
            self.retry.mode is RetryMode.RECONCILE_THEN_NEW_ATTEMPT
            and self.retry.idempotency_rule_sha256 is not None
            and self.retry.reconciliation_rule_sha256 is not None
            and self.runtime.reconciliation_supported
        ):
            return ExecutionEffectClass.IDEMPOTENT_EXTERNAL
        return ExecutionEffectClass.ONE_TIME_EXTERNAL


class CapabilityCatalog(ProtocolModel):
    """Immutable in-memory catalog with exact-only resolution semantics."""

    schema_name: Literal["aletheia.capability_catalog"] = "aletheia.capability_catalog"
    schema_version: Literal[1] = 1
    manifests: tuple[CapabilityManifestV2, ...] = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def _catalog_is_exact_and_unique(self) -> "CapabilityCatalog":
        hashes = tuple(item.manifest_sha256 for item in self.manifests)
        if hashes != tuple(sorted(set(hashes))):
            raise ValueError("capability manifests must have unique hashes in canonical order")
        identities = tuple((item.capability_id, item.semantic_version) for item in self.manifests)
        if len(identities) != len(set(identities)):
            raise ValueError("capability catalog has duplicate capability/version identities")
        return self

    @property
    def catalog_sha256(self) -> str:
        return canonical_sha256(self)

    def get_exact(self, manifest_sha256: str) -> CapabilityManifestV2:
        matches = tuple(item for item in self.manifests if item.manifest_sha256 == manifest_sha256)
        if len(matches) != 1:
            raise LookupError("capability manifest hash did not resolve exactly once")
        return matches[0]

    def resolve_exact(
        self,
        *,
        capability_id: str,
        semantic_version: str,
        manifest_sha256: str,
    ) -> CapabilityManifestV2:
        manifest = self.get_exact(manifest_sha256)
        if manifest.capability_id != capability_id or manifest.semantic_version != semantic_version:
            raise LookupError("capability identity/version does not match its exact manifest hash")
        return manifest


InMemoryCapabilityCatalog = CapabilityCatalog


__all__ = [
    "ApplicabilityContract",
    "ArtifactKind",
    "CalibrationContract",
    "CalibrationMode",
    "CapabilityCatalog",
    "CapabilityManifestV2",
    "CapabilityPort",
    "DataClassification",
    "DeterminismClass",
    "FailureCategory",
    "FailureDisposition",
    "FailureMode",
    "InMemoryCapabilityCatalog",
    "LicenseEgressContract",
    "NetworkEgressMode",
    "PortDirection",
    "PortMultiplicity",
    "PrincipalContract",
    "PrincipalKind",
    "QualificationContract",
    "QualificationStatus",
    "RetryContract",
    "RetryMode",
    "RuntimeContract",
    "RuntimeKind",
    "SafetyClass",
    "SafetyContract",
    "SideEffectClass",
]
