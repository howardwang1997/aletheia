"""Domain-independent Scientific Protocol IR and deterministic work-order contracts.

The IR freezes scientific intent before any observation is visible.  It deliberately describes
static structure only: capability qualification and resource *shape* can be checked here, while
live placement, leases, execution, and scientific admission belong to later authority layers.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.execution.schemas import (
    ArtifactRole,
    ExecutionEffectClass,
    ExecutionResourceRequest,
    ExecutionRetryPolicy,
    ExpectedArtifact,
    ScientificReplicateKind,
)
from aletheia.protocols.base import (
    LOCAL_ID_PATTERN,
    PRINCIPAL_ID_PATTERN,
    PROTOCOL_SCHEMA_VERSION,
    SEMVER_PATTERN,
    SHA256_PATTERN,
    JsonSchemaRef,
    ProtocolModel,
    ProtocolScope,
    canonical_models,
    canonical_sha256s,
    canonical_sha256,
    canonical_strings,
)
from aletheia.protocols.capabilities import (
    ArtifactKind,
    DataClassification,
    QualificationStatus,
)
from aletheia.protocols.claim_contracts import (
    ClaimContract,
    EpistemicContract,
    ObservableSpec,
)
from aletheia.protocols.world_models import WorldModelSnapshotV2

COMPILER_CONTRACT_VERSION = 1


class ProtocolActionCategory(str, Enum):
    LITERATURE_KNOWLEDGE_AUDIT = "literature_knowledge_audit"
    DETERMINISTIC_ANALYSIS = "deterministic_analysis"
    COMPUTATIONAL_EXPERIMENT = "computational_experiment"
    STRUCTURAL_INTERVENTION = "structural_intervention"
    CALIBRATION_REPRODUCTION = "calibration_reproduction"
    EXTERNAL_MEASUREMENT_REQUEST = "external_measurement_request"
    CAPABILITY_AUTHORING_QUALIFICATION = "capability_authoring_qualification"
    EVIDENCE_SYNTHESIS = "evidence_synthesis"


class DataRole(str, Enum):
    EXPLORATION = "exploration"
    CONFIRMATION = "confirmation"
    REPLICATION = "replication"
    PRIVATE_VALIDATION = "private_validation"
    CALIBRATION = "calibration"


class PreauthorizationVisibility(str, Enum):
    VISIBLE = "visible"
    AGGREGATE_ONLY = "aggregate_only"
    HIDDEN = "hidden"


class ProtocolPortDirection(str, Enum):
    INPUT = "input"
    INTERMEDIATE = "intermediate"
    OUTPUT = "output"


class ProtocolStepRole(str, Enum):
    SCIENTIFIC_EXECUTOR = "scientific_executor"
    OBSERVATION_PARSER = "observation_parser"
    INDEPENDENT_VALIDATOR = "independent_validator"
    CONTROL = "control"
    ANALYSIS = "analysis"
    CALIBRATION = "calibration"


class ControlFailureClass(str, Enum):
    EMPTY_INPUT = "empty_input"
    DATA_LEAKAGE = "data_leakage"
    DEGENERACY = "degeneracy"
    DRIFT = "drift"
    POSITIVE_CONTROL_FAILURE = "positive_control_failure"
    NEGATIVE_CONTROL_FAILURE = "negative_control_failure"


class CompatibilityDimension(str, Enum):
    JSON_SCHEMA = "json_schema"
    UNIT_OR_ONTOLOGY = "unit_or_ontology"
    DATA_CLASSIFICATION = "data_classification"
    LICENSE = "license"
    EGRESS = "egress"
    IDENTITY_LINEAGE = "identity_lineage"


class CapabilityAuditKind(str, Enum):
    APPLICABILITY = "applicability"
    FAILURE_MODES = "failure_modes"
    SAMPLE_FLOOR = "sample_floor"
    RUNTIME = "runtime"
    CALIBRATION = "calibration"
    SAFETY = "safety"
    LICENSE_EGRESS = "license_egress"


class ProtocolBlockerCode(str, Enum):
    SCOPE_MISMATCH = "scope_mismatch"
    OBJECTIVE_BINDING_MISMATCH = "objective_binding_mismatch"
    WORLD_MODEL_MISSING = "world_model_missing"
    WORLD_MODEL_MISMATCH = "world_model_mismatch"
    HYPOTHESIS_PREDICTION_MISSING = "hypothesis_prediction_missing"
    OBSERVABLE_MISSING = "observable_missing"
    OBSERVABLE_CAPABILITY_MISMATCH = "observable_capability_mismatch"
    CALIBRATION_UNCOVERED = "calibration_uncovered"
    IDENTITY_LINEAGE_OPEN = "identity_lineage_open"
    CONTROL_COVERAGE_MISSING = "control_coverage_missing"
    DATA_ROLE_CONFLICT = "data_role_conflict"
    ANALYSIS_NOT_PREREGISTERED = "analysis_not_preregistered"
    DAG_CYCLE = "dag_cycle"
    PORT_UNBOUND = "port_unbound"
    PORT_SCHEMA_INCOMPATIBLE = "port_schema_incompatible"
    PORT_CLASSIFICATION_INCOMPATIBLE = "port_classification_incompatible"
    PORT_LICENSE_INCOMPATIBLE = "port_license_incompatible"
    PORT_EGRESS_INCOMPATIBLE = "port_egress_incompatible"
    INDEPENDENCE_CONFLICT = "independence_conflict"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    CAPABILITY_AMBIGUOUS = "capability_ambiguous"
    CAPABILITY_NOT_QUALIFIED = "capability_not_qualified"
    CAPABILITY_APPLICABILITY_MISMATCH = "capability_applicability_mismatch"
    CAPABILITY_AUDIT_MISSING = "capability_audit_missing"
    RESOURCE_SCHEMA_INCOMPATIBLE = "resource_schema_incompatible"
    UNSUPPORTED_CLAIM = "unsupported_claim"
    PARAMETER_HASH_UNCOVERED = "parameter_hash_uncovered"
    RETRY_POLICY_INCOMPATIBLE = "retry_policy_incompatible"


class ProtocolBlocker(ProtocolModel):
    code: ProtocolBlockerCode
    location: str = Field(pattern=r"^[a-z][a-z0-9_.:/\[\]-]{0,255}$")
    subject_id: str = Field(pattern=LOCAL_ID_PATTERN)
    detail: str = Field(min_length=1, max_length=4_000)
    evidence_sha256s: tuple[str, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def _evidence_is_canonical(self) -> "ProtocolBlocker":
        canonical_sha256s(self.evidence_sha256s, "blocker evidence hashes")
        return self

    @property
    def blocker_sha256(self) -> str:
        return canonical_sha256(self)


class CompatibilityAuditReceipt(ProtocolModel):
    """Direction-bound proof for a compatibility relation that cannot be equality-checked."""

    schema_name: Literal["aletheia.protocol_compatibility_audit"] = (
        "aletheia.protocol_compatibility_audit"
    )
    schema_version: Literal[1] = PROTOCOL_SCHEMA_VERSION
    dimension: CompatibilityDimension
    source_sha256: str = Field(pattern=SHA256_PATTERN)
    target_sha256: str = Field(pattern=SHA256_PATTERN)
    compatible: Literal[True] = True
    audit_policy_sha256: str = Field(pattern=SHA256_PATTERN)
    evidence_sha256s: tuple[str, ...] = Field(min_length=1, max_length=64)
    audited_by_principal_id: str = Field(pattern=PRINCIPAL_ID_PATTERN)
    audited_at: AwareDatetime

    @model_validator(mode="after")
    def _receipt_is_directional(self) -> "CompatibilityAuditReceipt":
        canonical_sha256s(
            self.evidence_sha256s,
            "compatibility audit evidence",
            required=True,
        )
        if self.source_sha256 == self.target_sha256:
            raise ValueError("equal identities do not require a compatibility audit")
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


class ObjectiveContractVersion(ProtocolModel):
    schema_name: Literal["aletheia.objective_contract"] = "aletheia.objective_contract"
    schema_version: Literal[1] = PROTOCOL_SCHEMA_VERSION
    objective_id: str = Field(pattern=LOCAL_ID_PATTERN)
    version: int = Field(ge=1)
    revision_parent_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    graph_scope_sha256: str = Field(pattern=SHA256_PATTERN)
    action_category: ProtocolActionCategory
    objective: str = Field(min_length=1, max_length=8_000)
    candidate_outcome_sha256s: tuple[str, ...] = Field(min_length=1, max_length=128)
    value_receipt_sha256: str = Field(pattern=SHA256_PATTERN)
    semantic_delta: str = Field(min_length=1, max_length=4_000)
    authored_by_principal_id: str = Field(pattern=PRINCIPAL_ID_PATTERN)
    authored_at: AwareDatetime

    @model_validator(mode="after")
    def _objective_is_versioned(self) -> "ObjectiveContractVersion":
        if (self.version == 1) != (self.revision_parent_sha256 is None):
            raise ValueError("only objective version 1 may omit its revision parent")
        canonical_sha256s(self.candidate_outcome_sha256s, "candidate outcomes", required=True)
        return self

    @property
    def objective_sha256(self) -> str:
        return canonical_sha256(self)


class DesignFactor(ProtocolModel):
    factor_id: str = Field(pattern=LOCAL_ID_PATTERN)
    factor_kind: Literal["intervention", "exposure", "comparator", "covariate", "nuisance"]
    value_schema: JsonSchemaRef
    assignment_rule_sha256: str = Field(pattern=SHA256_PATTERN)
    caller_mutable: bool = False


class DesignSpaceVersion(ProtocolModel):
    schema_name: Literal["aletheia.design_space"] = "aletheia.design_space"
    schema_version: Literal[1] = PROTOCOL_SCHEMA_VERSION
    design_space_id: str = Field(pattern=LOCAL_ID_PATTERN)
    version: int = Field(ge=1)
    revision_parent_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    graph_scope_sha256: str = Field(pattern=SHA256_PATTERN)
    population_sha256: str = Field(pattern=SHA256_PATTERN)
    sampling_unit_schema_sha256: str = Field(pattern=SHA256_PATTERN)
    specimen_genealogy_sha256: str = Field(pattern=SHA256_PATTERN)
    factors: tuple[DesignFactor, ...] = Field(default=(), max_length=128)
    constraint_sha256s: tuple[str, ...] = Field(default=(), max_length=256)
    randomization_rule_sha256: str = Field(pattern=SHA256_PATTERN)
    allocation_rule_sha256: str = Field(pattern=SHA256_PATTERN)
    blocking_rule_sha256: str = Field(pattern=SHA256_PATTERN)
    semantic_delta: str = Field(min_length=1, max_length=4_000)
    authored_by_principal_id: str = Field(pattern=PRINCIPAL_ID_PATTERN)
    authored_at: AwareDatetime

    @model_validator(mode="after")
    def _design_is_versioned_and_canonical(self) -> "DesignSpaceVersion":
        if (self.version == 1) != (self.revision_parent_sha256 is None):
            raise ValueError("only design-space version 1 may omit its revision parent")
        canonical_models(self.factors, key=lambda item: item.factor_id, label="design factors")
        canonical_sha256s(self.constraint_sha256s, "design constraints")
        return self

    @property
    def design_space_sha256(self) -> str:
        return canonical_sha256(self)


class MethodVersion(ProtocolModel):
    schema_name: Literal["aletheia.method"] = "aletheia.method"
    schema_version: Literal[1] = PROTOCOL_SCHEMA_VERSION
    method_id: str = Field(pattern=LOCAL_ID_PATTERN)
    version: int = Field(ge=1)
    revision_parent_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    graph_scope_sha256: str = Field(pattern=SHA256_PATTERN)
    method_family: str = Field(pattern=LOCAL_ID_PATTERN)
    method_contract_sha256: str = Field(pattern=SHA256_PATTERN)
    assumption_sha256s: tuple[str, ...] = Field(default=(), max_length=256)
    limitation_sha256s: tuple[str, ...] = Field(min_length=1, max_length=256)
    semantic_delta: str = Field(min_length=1, max_length=4_000)
    authored_by_principal_id: str = Field(pattern=PRINCIPAL_ID_PATTERN)
    authored_at: AwareDatetime

    @model_validator(mode="after")
    def _method_is_versioned_and_canonical(self) -> "MethodVersion":
        if (self.version == 1) != (self.revision_parent_sha256 is None):
            raise ValueError("only method version 1 may omit its revision parent")
        canonical_sha256s(self.assumption_sha256s, "method assumptions")
        canonical_sha256s(self.limitation_sha256s, "method limitations", required=True)
        return self

    @property
    def method_sha256(self) -> str:
        return canonical_sha256(self)


class ProtocolDataPort(ProtocolModel):
    port_id: str = Field(pattern=LOCAL_ID_PATTERN)
    direction: ProtocolPortDirection
    schema_ref: JsonSchemaRef
    artifact_kind: ArtifactKind
    data_classification: DataClassification
    data_role: DataRole
    preauthorization_visibility: PreauthorizationVisibility
    license_policy_sha256: str = Field(pattern=SHA256_PATTERN)
    egress_policy_sha256: str = Field(pattern=SHA256_PATTERN)
    identity_schema_sha256: str = Field(pattern=SHA256_PATTERN)
    unit_or_ontology_sha256: str = Field(pattern=SHA256_PATTERN)


class IdentityLineageContract(ProtocolModel):
    input_identity_schema_sha256s: tuple[str, ...] = Field(min_length=1, max_length=128)
    output_identity_schema_sha256s: tuple[str, ...] = Field(min_length=1, max_length=128)
    genealogy_rule_sha256: str = Field(pattern=SHA256_PATTERN)
    lineage_artifact_port_id: str = Field(pattern=LOCAL_ID_PATTERN)
    allows_identity_loss: Literal[False] = False

    @model_validator(mode="after")
    def _identity_sets_are_canonical(self) -> "IdentityLineageContract":
        canonical_sha256s(
            self.input_identity_schema_sha256s,
            "input identity schemas",
            required=True,
        )
        canonical_sha256s(
            self.output_identity_schema_sha256s,
            "output identity schemas",
            required=True,
        )
        return self


class ControlSpec(ProtocolModel):
    control_id: str = Field(pattern=LOCAL_ID_PATTERN)
    catches: tuple[ControlFailureClass, ...] = Field(min_length=1, max_length=8)
    input_port_ids: tuple[str, ...] = Field(default=(), max_length=64)
    observable_spec_sha256s: tuple[str, ...] = Field(default=(), max_length=64)
    decision_rule_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _control_is_canonical(self) -> "ControlSpec":
        expected = tuple(sorted(set(self.catches), key=lambda item: item.value))
        if self.catches != expected:
            raise ValueError("control failure classes must be unique and canonical")
        canonical_strings(self.input_port_ids, "control input ports")
        canonical_sha256s(self.observable_spec_sha256s, "control observables")
        return self


class AnalysisPlan(ProtocolModel):
    primary_endpoint_sha256s: tuple[str, ...] = Field(min_length=1, max_length=64)
    secondary_endpoint_sha256s: tuple[str, ...] = Field(default=(), max_length=128)
    estimator_or_likelihood_sha256: str = Field(pattern=SHA256_PATTERN)
    sample_size_or_precision_rule_sha256: str = Field(pattern=SHA256_PATTERN)
    missingness_rule_sha256: str = Field(pattern=SHA256_PATTERN)
    exclusion_rule_sha256: str = Field(pattern=SHA256_PATTERN)
    multiplicity_rule_sha256: str = Field(pattern=SHA256_PATTERN)
    stopping_rule_sha256: str = Field(pattern=SHA256_PATTERN)
    futility_rule_sha256: str = Field(pattern=SHA256_PATTERN)
    positive_decision_rule_sha256: str = Field(pattern=SHA256_PATTERN)
    negative_decision_rule_sha256: str = Field(pattern=SHA256_PATTERN)
    inconclusive_decision_rule_sha256: str = Field(pattern=SHA256_PATTERN)
    robustness_analysis_sha256s: tuple[str, ...] = Field(default=(), max_length=128)
    frozen_before_observation: bool
    preregistration_seal_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _analysis_is_canonical(self) -> "AnalysisPlan":
        canonical_sha256s(self.primary_endpoint_sha256s, "primary endpoints", required=True)
        canonical_sha256s(self.secondary_endpoint_sha256s, "secondary endpoints")
        canonical_sha256s(self.robustness_analysis_sha256s, "robustness analyses")
        return self

    @property
    def outcome_space_sha256(self) -> str:
        """Identity of the preregistered endpoints and three-way decision space."""

        return canonical_sha256(
            {
                "schema_name": "aletheia.analysis_outcome_space",
                "schema_version": 1,
                "primary_endpoint_sha256s": self.primary_endpoint_sha256s,
                "secondary_endpoint_sha256s": self.secondary_endpoint_sha256s,
                "positive_decision_rule_sha256": self.positive_decision_rule_sha256,
                "negative_decision_rule_sha256": self.negative_decision_rule_sha256,
                "inconclusive_decision_rule_sha256": self.inconclusive_decision_rule_sha256,
            }
        )


class IndependenceContract(ProtocolModel):
    executor_group_id: str = Field(pattern=LOCAL_ID_PATTERN)
    parser_group_id: str = Field(pattern=LOCAL_ID_PATTERN)
    validator_group_id: str = Field(pattern=LOCAL_ID_PATTERN)
    claim_approver_group_id: str = Field(pattern=LOCAL_ID_PATTERN)
    executor_principal_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    parser_principal_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    validator_principal_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    claim_approver_principal_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    policy_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _principals_are_canonical(self) -> "IndependenceContract":
        for values, label in (
            (self.executor_principal_ids, "executor principals"),
            (self.parser_principal_ids, "parser principals"),
            (self.validator_principal_ids, "validator principals"),
            (self.claim_approver_principal_ids, "claim approver principals"),
        ):
            canonical_strings(values, label, required=True)
        return self


class ResourceBudgetContract(ProtocolModel):
    """Static protocol envelope; live reservation remains an allocator responsibility."""

    currency_code: str = Field(pattern=r"^[A-Z]{3}$")
    maximum_cost_microunits: int = Field(ge=0)
    maximum_total_artifact_bytes: int = Field(ge=1)
    deadline: AwareDatetime
    budget_authorization_sha256: str = Field(pattern=SHA256_PATTERN)
    checkpoint_policy_sha256: str = Field(pattern=SHA256_PATTERN)
    permitted_retention_policy_sha256s: tuple[str, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _retention_policies_are_canonical(self) -> "ResourceBudgetContract":
        canonical_sha256s(
            self.permitted_retention_policy_sha256s,
            "permitted retention policies",
            required=True,
        )
        return self

    @property
    def resource_budget_sha256(self) -> str:
        return canonical_sha256(self)


class CapabilityAuditBinding(ProtocolModel):
    """Typed, time-bounded binding to an audit declared by a separate principal identity."""

    audit_kind: CapabilityAuditKind
    capability_manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    receipt_sha256: str = Field(pattern=SHA256_PATTERN)
    audit_policy_sha256: str = Field(pattern=SHA256_PATTERN)
    auditor_principal_id: str = Field(pattern=PRINCIPAL_ID_PATTERN)
    valid_from: AwareDatetime
    expires_at: AwareDatetime | None = None
    conclusion: Literal["passed"] = "passed"

    @model_validator(mode="after")
    def _interval_is_valid(self) -> "CapabilityAuditBinding":
        if self.expires_at is not None and self.expires_at <= self.valid_from:
            raise ValueError("capability audit expiry must follow its validity start")
        return self

    @property
    def binding_sha256(self) -> str:
        return canonical_sha256(self)


class CapabilityRequirement(ProtocolModel):
    requirement_id: str = Field(pattern=LOCAL_ID_PATTERN)
    operation_id: str = Field(pattern=LOCAL_ID_PATTERN)
    capability_id: str | None = Field(default=None, pattern=LOCAL_ID_PATTERN)
    semantic_version: str | None = Field(default=None, pattern=SEMVER_PATTERN)
    manifest_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    required_condition_sha256s: tuple[str, ...] = Field(default=(), max_length=128)
    minimum_qualification_status: QualificationStatus = QualificationStatus.QUALIFIED
    audit_bindings: tuple[CapabilityAuditBinding, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _selector_is_closed(self) -> "CapabilityRequirement":
        canonical_sha256s(self.required_condition_sha256s, "required capability conditions")
        canonical_models(
            self.audit_bindings,
            key=lambda item: item.audit_kind.value,
            label="capability audit bindings",
        )
        if self.semantic_version is not None and self.capability_id is None:
            raise ValueError("capability semantic version requires a capability identity")
        if self.manifest_sha256 is not None and (
            self.capability_id is None or self.semantic_version is None
        ):
            raise ValueError("exact manifest pin requires capability identity and version")
        return self


class CallerParameterBinding(ProtocolModel):
    parameter_id: str = Field(pattern=LOCAL_ID_PATTERN)
    value_sha256: str = Field(pattern=SHA256_PATTERN)


class ProtocolContractKind(str, Enum):
    DESIGN_SPACE = "design_space"
    METHOD = "method"
    CONTROL = "control"
    ANALYSIS = "analysis"
    EPISTEMIC = "epistemic"


class StepContractBinding(ProtocolModel):
    """Exact preregistered scientific contract implemented by one atomic step."""

    contract_kind: ProtocolContractKind
    contract_sha256: str = Field(pattern=SHA256_PATTERN)


class ObservableOutputBinding(ProtocolModel):
    """Exact observable, producer step, and output port relation."""

    observable_spec_sha256: str = Field(pattern=SHA256_PATTERN)
    producer_step_id: str = Field(pattern=LOCAL_ID_PATTERN)
    output_port_id: str = Field(pattern=LOCAL_ID_PATTERN)


def caller_parameter_manifest_sha256(
    bindings: tuple[CallerParameterBinding, ...],
) -> str:
    """Hash the canonical serialized parameter set without relying on tuple model coercion."""

    return canonical_sha256([item.model_dump(mode="json", exclude_none=True) for item in bindings])


class ProtocolStep(ProtocolModel):
    step_id: str = Field(pattern=LOCAL_ID_PATTERN)
    role: ProtocolStepRole
    capability_requirement: CapabilityRequirement
    depends_on_step_ids: tuple[str, ...] = Field(default=(), max_length=128)
    input_port_ids: tuple[str, ...] = Field(default=(), max_length=128)
    output_port_ids: tuple[str, ...] = Field(min_length=1, max_length=128)
    resource_request: ExecutionResourceRequest
    expected_artifacts: tuple[ExpectedArtifact, ...] = Field(min_length=1, max_length=128)
    contract_bindings: tuple[StepContractBinding, ...] = Field(default=(), max_length=256)
    caller_parameter_ids: tuple[str, ...] = Field(default=(), max_length=1024)
    operation_batch_size: int = Field(default=1, ge=1, le=1_000_000_000)
    replicate_kind: ScientificReplicateKind
    replicate_preregistration_sha256: str = Field(pattern=SHA256_PATTERN)
    replicate_seed_sha256s: tuple[str, ...] = Field(min_length=1, max_length=10_000)
    independent_site_required: bool = False
    scientific_replicate_count: int = Field(default=1, ge=1, le=10_000)
    execution_parameters_sha256: str = Field(pattern=SHA256_PATTERN)
    environment_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _step_is_canonical(self) -> "ProtocolStep":
        canonical_strings(self.depends_on_step_ids, "step dependencies")
        canonical_strings(self.input_port_ids, "step input ports")
        canonical_strings(self.output_port_ids, "step output ports", required=True)
        canonical_strings(self.caller_parameter_ids, "step caller parameter IDs")
        if len(self.replicate_seed_sha256s) != self.scientific_replicate_count or any(
            re.fullmatch(SHA256_PATTERN, item) is None for item in self.replicate_seed_sha256s
        ):
            raise ValueError("every scientific replicate slot requires one seed commitment")
        canonical_models(
            self.contract_bindings,
            key=lambda item: f"{item.contract_kind.value}:{item.contract_sha256}",
            label="step contract bindings",
        )
        canonical_models(
            self.expected_artifacts,
            key=lambda item: item.artifact_key,
            label="expected artifacts",
        )
        return self


class ProtocolIR(ProtocolModel):
    """One immutable preregistered scientific protocol, before authorization or execution."""

    schema_name: Literal["aletheia.scientific_protocol_ir"] = "aletheia.scientific_protocol_ir"
    schema_version: Literal[1] = PROTOCOL_SCHEMA_VERSION
    protocol_id: str = Field(pattern=LOCAL_ID_PATTERN)
    version: int = Field(ge=1)
    revision_parent_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    graph_scope: ProtocolScope
    objective: ObjectiveContractVersion
    design_space: DesignSpaceVersion
    method: MethodVersion
    epistemic_contract: EpistemicContract
    world_model: WorldModelSnapshotV2 | None = None
    observables: tuple[ObservableSpec, ...] = Field(default=(), max_length=256)
    observable_output_bindings: tuple[ObservableOutputBinding, ...] = Field(
        default=(), max_length=256
    )
    data_ports: tuple[ProtocolDataPort, ...] = Field(min_length=1, max_length=512)
    identity_lineage: IdentityLineageContract
    controls: tuple[ControlSpec, ...] = Field(default=(), max_length=128)
    analysis_plan: AnalysisPlan
    independence: IndependenceContract
    resource_budget: ResourceBudgetContract
    steps: tuple[ProtocolStep, ...] = Field(min_length=1, max_length=512)
    claim_contract: ClaimContract
    compatibility_audit_receipts: tuple[CompatibilityAuditReceipt, ...] = Field(
        default=(), max_length=512
    )
    caller_parameter_bindings: tuple[CallerParameterBinding, ...] = Field(
        default=(), max_length=1024
    )
    caller_parameter_manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    authored_by_principal_id: str = Field(pattern=PRINCIPAL_ID_PATTERN)
    authored_at: AwareDatetime

    @model_validator(mode="after")
    def _protocol_is_canonical(self) -> "ProtocolIR":
        if (self.version == 1) != (self.revision_parent_sha256 is None):
            raise ValueError("only protocol version 1 may omit its revision parent")
        scope_hash = self.graph_scope.graph_scope_sha256
        for value, label in (
            (self.objective, "objective"),
            (self.design_space, "design space"),
            (self.method, "method"),
            (self.epistemic_contract, "epistemic contract"),
            (self.claim_contract, "claim contract"),
        ):
            if value.graph_scope_sha256 != scope_hash:
                raise ValueError(f"protocol {label} escaped its exact graph scope")
        canonical_models(
            self.observables,
            key=lambda item: item.observable_id,
            label="protocol observables",
        )
        canonical_models(
            self.observable_output_bindings,
            key=lambda item: item.observable_spec_sha256,
            label="observable output bindings",
        )
        if any(item.graph_scope_sha256 != scope_hash for item in self.observables):
            raise ValueError("protocol observable escaped its exact graph scope")
        canonical_models(self.data_ports, key=lambda item: item.port_id, label="protocol ports")
        canonical_models(self.controls, key=lambda item: item.control_id, label="protocol controls")
        canonical_models(self.steps, key=lambda item: item.step_id, label="protocol steps")
        canonical_models(
            self.compatibility_audit_receipts,
            key=lambda item: item.receipt_sha256,
            label="compatibility audit receipts",
        )
        canonical_models(
            self.caller_parameter_bindings,
            key=lambda item: item.parameter_id,
            label="caller parameter bindings",
        )
        if self.world_model is not None and (
            self.world_model.graph_scope.graph_scope_sha256 != scope_hash
        ):
            raise ValueError("protocol world model escaped its exact graph scope")
        return self

    @property
    def protocol_sha256(self) -> str:
        return canonical_sha256(self)


class ProtocolCheckReport(ProtocolModel):
    schema_name: Literal["aletheia.protocol_check_report"] = "aletheia.protocol_check_report"
    schema_version: Literal[1] = COMPILER_CONTRACT_VERSION
    protocol_sha256: str = Field(pattern=SHA256_PATTERN)
    capability_catalog_sha256: str = Field(pattern=SHA256_PATTERN)
    resource_catalog_sha256: str = Field(pattern=SHA256_PATTERN)
    blockers: tuple[ProtocolBlocker, ...] = ()

    @model_validator(mode="after")
    def _blockers_are_canonical(self) -> "ProtocolCheckReport":
        expected = tuple(
            sorted(
                self.blockers,
                key=lambda item: (
                    item.code.value,
                    item.location,
                    item.subject_id,
                    item.blocker_sha256,
                ),
            )
        )
        if self.blockers != expected or len({item.blocker_sha256 for item in self.blockers}) != len(
            self.blockers
        ):
            raise ValueError("protocol blockers must be unique and canonical")
        return self

    @property
    def accepted(self) -> bool:
        return not self.blockers

    @property
    def report_sha256(self) -> str:
        return canonical_sha256(self)


class WorkOrderNode(ProtocolModel):
    node_id: str = Field(pattern=LOCAL_ID_PATTERN)
    protocol_step_id: str = Field(pattern=LOCAL_ID_PATTERN)
    role: ProtocolStepRole
    capability_id: str = Field(pattern=LOCAL_ID_PATTERN)
    capability_manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    command_sha256: str = Field(pattern=SHA256_PATTERN)
    external_action_kind: str | None = Field(default=None, pattern=LOCAL_ID_PATTERN)
    effect_class: ExecutionEffectClass
    dependency_node_ids: tuple[str, ...] = Field(default=(), max_length=128)
    input_port_ids: tuple[str, ...] = Field(default=(), max_length=128)
    output_port_ids: tuple[str, ...] = Field(min_length=1, max_length=128)
    resource_request: ExecutionResourceRequest
    retry_policy: ExecutionRetryPolicy
    expected_artifacts: tuple[ExpectedArtifact, ...] = Field(min_length=1, max_length=128)
    contract_bindings: tuple[StepContractBinding, ...] = Field(default=(), max_length=256)
    observable_output_bindings: tuple[ObservableOutputBinding, ...] = Field(
        default=(), max_length=256
    )
    caller_parameter_bindings: tuple[CallerParameterBinding, ...] = Field(
        default=(), max_length=1024
    )
    operation_batch_size: int = Field(ge=1, le=1_000_000_000)
    replicate_kind: ScientificReplicateKind
    replicate_preregistration_sha256: str = Field(pattern=SHA256_PATTERN)
    replicate_seed_sha256s: tuple[str, ...] = Field(min_length=1, max_length=10_000)
    independent_site_required: bool = False
    scientific_replicate_count: int = Field(ge=1, le=10_000)
    execution_parameters_sha256: str = Field(pattern=SHA256_PATTERN)
    environment_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _node_is_canonical(self) -> "WorkOrderNode":
        canonical_strings(self.dependency_node_ids, "work-order dependencies")
        canonical_strings(self.input_port_ids, "work-order input ports")
        canonical_strings(self.output_port_ids, "work-order output ports", required=True)
        canonical_models(
            self.contract_bindings,
            key=lambda item: f"{item.contract_kind.value}:{item.contract_sha256}",
            label="work-order contract bindings",
        )
        canonical_models(
            self.observable_output_bindings,
            key=lambda item: item.observable_spec_sha256,
            label="work-order observable bindings",
        )
        canonical_models(
            self.caller_parameter_bindings,
            key=lambda item: item.parameter_id,
            label="work-order caller parameter bindings",
        )
        if len(self.replicate_seed_sha256s) != self.scientific_replicate_count or any(
            re.fullmatch(SHA256_PATTERN, item) is None for item in self.replicate_seed_sha256s
        ):
            raise ValueError("work-order replicate slots require exact seed commitments")
        canonical_models(
            self.expected_artifacts,
            key=lambda item: item.artifact_key,
            label="work-order artifacts",
        )
        raw_output_keys = tuple(
            item.artifact_key
            for item in self.expected_artifacts
            if item.role is ArtifactRole.RAW_OUTPUT
        )
        if raw_output_keys != self.output_port_ids:
            raise ValueError("work-order raw-output artifacts must bind exactly every output port")
        provider_receipts = tuple(
            item for item in self.expected_artifacts if item.role is ArtifactRole.PROVIDER_RECEIPT
        )
        external = self.effect_class is not ExecutionEffectClass.REPLAY_SAFE
        provider_contract_valid = len(provider_receipts) == 1 and provider_receipts[0].required
        if external != provider_contract_valid:
            raise ValueError("external work-order nodes require exactly one provider receipt")
        if external and self.external_action_kind is None:
            raise ValueError("external work-order nodes require an exact external action kind")
        if (
            self.resource_request.max_infrastructure_attempts
            > self.retry_policy.maximum_attempts_per_scientific_slot
        ):
            raise ValueError("work-order resource retry bound exceeds its capability policy")
        if (
            self.resource_request.checkpoint_interval_seconds is not None
            and self.retry_policy.mode.value != "checkpoint_resume"
        ):
            raise ValueError("work-order checkpointing requires checkpoint-resume policy")
        if self.effect_class is ExecutionEffectClass.ONE_TIME_EXTERNAL and (
            self.resource_request.max_infrastructure_attempts != 1
            or self.retry_policy.mode.value != "never"
        ):
            raise ValueError("one-time external work permits exactly one attempt")
        if any(
            item.producer_step_id != self.protocol_step_id
            or item.output_port_id not in self.output_port_ids
            for item in self.observable_output_bindings
        ):
            raise ValueError("work-order observable binding escaped its producer node")
        return self

    @property
    def node_sha256(self) -> str:
        return canonical_sha256(self)


class WorkOrderDAG(ProtocolModel):
    schema_name: Literal["aletheia.work_order_dag"] = "aletheia.work_order_dag"
    schema_version: Literal[1] = COMPILER_CONTRACT_VERSION
    quest_id: str
    graph_scope_sha256: str = Field(pattern=SHA256_PATTERN)
    protocol_sha256: str = Field(pattern=SHA256_PATTERN)
    capability_catalog_sha256: str = Field(pattern=SHA256_PATTERN)
    resource_catalog_sha256: str = Field(pattern=SHA256_PATTERN)
    resource_budget_sha256: str = Field(pattern=SHA256_PATTERN)
    nodes: tuple[WorkOrderNode, ...] = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def _nodes_are_canonical(self) -> "WorkOrderDAG":
        canonical_models(self.nodes, key=lambda item: item.node_id, label="work-order nodes")
        node_ids = {item.node_id for item in self.nodes}
        producers: dict[str, str] = {}
        for node in self.nodes:
            if node.node_id in node.dependency_node_ids or not set(
                node.dependency_node_ids
            ).issubset(node_ids):
                raise ValueError("work-order node has an unknown or self dependency")
            for port_id in node.output_port_ids:
                if port_id in producers:
                    raise ValueError("work-order output port has multiple producers")
                producers[port_id] = node.node_id
        for node in self.nodes:
            if any(
                producers.get(port_id) is not None
                and producers[port_id] not in node.dependency_node_ids
                for port_id in node.input_port_ids
            ):
                raise ValueError("work-order input is not bound to a dependency producer")
            if any(
                producers.get(port_id) is not None
                and next(
                    item for item in self.nodes if item.node_id == producers[port_id]
                ).scientific_replicate_count
                != node.scientific_replicate_count
                for port_id in node.input_port_ids
            ):
                raise ValueError("work-order v1 requires one-to-one intermediate replicate slots")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> bool:
            if node_id in visiting:
                return False
            if node_id in visited:
                return True
            visiting.add(node_id)
            node = next(item for item in self.nodes if item.node_id == node_id)
            if any(not visit(item) for item in node.dependency_node_ids):
                return False
            visiting.remove(node_id)
            visited.add(node_id)
            return True

        if any(not visit(node_id) for node_id in node_ids if node_id not in visited):
            raise ValueError("work-order dependency graph contains a cycle")
        return self

    @property
    def work_order_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def work_order_id(self) -> str:
        return f"wo_{self.work_order_sha256[:32]}"


class CompilationReceipt(ProtocolModel):
    schema_name: Literal["aletheia.protocol_compilation_receipt"] = (
        "aletheia.protocol_compilation_receipt"
    )
    schema_version: Literal[1] = COMPILER_CONTRACT_VERSION
    protocol_sha256: str = Field(pattern=SHA256_PATTERN)
    typecheck_report_sha256: str = Field(pattern=SHA256_PATTERN)
    compiler_implementation_sha256: str = Field(pattern=SHA256_PATTERN)
    capability_catalog_sha256: str = Field(pattern=SHA256_PATTERN)
    resource_catalog_sha256: str = Field(pattern=SHA256_PATTERN)
    work_order_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    blocker_sha256s: tuple[str, ...] = Field(default=(), max_length=1024)

    @model_validator(mode="after")
    def _disposition_is_exact(self) -> "CompilationReceipt":
        canonical_sha256s(self.blocker_sha256s, "compilation blockers")
        if (self.work_order_sha256 is None) == (not self.blocker_sha256s):
            raise ValueError("compilation must contain exactly one of work order or blockers")
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


class ProtocolCompilationResult(ProtocolModel):
    report: ProtocolCheckReport
    work_order: WorkOrderDAG | None = None
    receipt: CompilationReceipt

    @model_validator(mode="after")
    def _result_is_bound(self) -> "ProtocolCompilationResult":
        if self.report.accepted != (self.work_order is not None):
            raise ValueError("accepted compilation requires exactly one work order")
        if self.receipt.protocol_sha256 != self.report.protocol_sha256:
            raise ValueError("compilation receipt belongs to another protocol")
        if self.receipt.typecheck_report_sha256 != self.report.report_sha256:
            raise ValueError("compilation receipt does not bind its typecheck report")
        expected_work_order = self.work_order.work_order_sha256 if self.work_order else None
        if self.receipt.work_order_sha256 != expected_work_order:
            raise ValueError("compilation receipt does not bind its exact work order")
        expected_blockers = tuple(sorted(item.blocker_sha256 for item in self.report.blockers))
        if self.receipt.blocker_sha256s != expected_blockers:
            raise ValueError("compilation receipt does not bind its exact blockers")
        return self


__all__ = [
    "AnalysisPlan",
    "CapabilityRequirement",
    "CapabilityAuditBinding",
    "CapabilityAuditKind",
    "CallerParameterBinding",
    "CompatibilityAuditReceipt",
    "CompatibilityDimension",
    "CompilationReceipt",
    "ControlFailureClass",
    "ControlSpec",
    "DataRole",
    "DesignFactor",
    "DesignSpaceVersion",
    "IdentityLineageContract",
    "IndependenceContract",
    "MethodVersion",
    "ObjectiveContractVersion",
    "ObservableOutputBinding",
    "PreauthorizationVisibility",
    "ProtocolActionCategory",
    "ProtocolContractKind",
    "ProtocolBlocker",
    "ProtocolBlockerCode",
    "ProtocolCheckReport",
    "ProtocolCompilationResult",
    "ProtocolDataPort",
    "ProtocolIR",
    "ProtocolPortDirection",
    "ProtocolStep",
    "ProtocolStepRole",
    "ResourceBudgetContract",
    "StepContractBinding",
    "WorkOrderDAG",
    "WorkOrderNode",
    "caller_parameter_manifest_sha256",
]
