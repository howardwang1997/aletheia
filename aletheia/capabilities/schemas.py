"""F10-S1 schemas for explicit, discoverable scientific experiment capabilities."""

from __future__ import annotations

import math
from enum import Enum
from typing import Any, Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.evals.schemas import FrozenModel
from aletheia.reproducibility.manifest import content_sha256


class CapabilityLifecycle(str, Enum):
    PROVISIONAL = "provisional"
    REGISTERED = "registered"
    RETIRED = "retired"


class CapabilityEvidenceLevel(str, Enum):
    EXPLORATORY = "exploratory"
    CONFIRMATORY_INTERNAL = "confirmatory_internal"
    INDEPENDENT_COMPUTATIONAL = "independent_computational"
    EXTERNAL_EXPERIMENTAL = "external_experimental"


_EVIDENCE_RANK = {
    CapabilityEvidenceLevel.EXPLORATORY: 0,
    CapabilityEvidenceLevel.CONFIRMATORY_INTERNAL: 1,
    CapabilityEvidenceLevel.INDEPENDENT_COMPUTATIONAL: 2,
    CapabilityEvidenceLevel.EXTERNAL_EXPERIMENTAL: 3,
}


def evidence_level_rank(value: CapabilityEvidenceLevel) -> int:
    return _EVIDENCE_RANK[value]


class CapabilityClaimType(str, Enum):
    DESCRIPTIVE = "descriptive"
    PREDICTIVE = "predictive"
    ASSOCIATION = "association"
    WITHIN_MODEL_CAUSAL = "within_model_causal"
    MECHANISM_CANDIDATE = "mechanism_candidate"
    EXPERIMENTAL_CAUSAL = "experimental_causal"


class CapabilityActionType(str, Enum):
    DATA_AUDIT = "data_audit"
    COMPUTATIONAL_EXPERIMENT = "computational_experiment"
    SIMULATION = "simulation"
    STRUCTURAL_INTERVENTION = "structural_intervention"
    EXTERNAL_MEASUREMENT = "external_measurement"
    SYNTHESIS = "synthesis"


class CapabilityRuntime(str, Enum):
    DETERMINISTIC = "deterministic"
    SANDBOXED_CODE = "sandboxed_code"
    CONTAINER = "container"
    EXTERNAL_SERVICE = "external_service"
    PHYSICAL_SITE = "physical_site"


class CapabilityBoundary(str, Enum):
    NO_EXECUTION = "no_execution"
    HARD_SANDBOX = "hard_sandbox"
    DIGEST_PINNED_CONTAINER = "digest_pinned_container"
    AUTHENTICATED_EXTERNAL = "authenticated_external"


class CapabilityRole(str, Enum):
    PLANNER = "planner"
    EXECUTOR = "executor"
    OBSERVATION_PARSER = "observation_parser"
    VALIDATOR = "validator"


_ROLE_ORDER = (
    CapabilityRole.PLANNER,
    CapabilityRole.EXECUTOR,
    CapabilityRole.OBSERVATION_PARSER,
    CapabilityRole.VALIDATOR,
)


class SafetyClass(str, Enum):
    READ_ONLY = "read_only"
    LOW_RISK_COMPUTE = "low_risk_compute"
    CONTROLLED_COMPUTE = "controlled_compute"
    PHYSICAL_HAZARD = "physical_hazard"


_SAFETY_RANK = {
    SafetyClass.READ_ONLY: 0,
    SafetyClass.LOW_RISK_COMPUTE: 1,
    SafetyClass.CONTROLLED_COMPUTE: 2,
    SafetyClass.PHYSICAL_HAZARD: 3,
}


def safety_class_rank(value: SafetyClass) -> int:
    return _SAFETY_RANK[value]


def _canonical_value(value: Any) -> str:
    return value.value if isinstance(value, Enum) else str(value)


class ApprovalClass(str, Enum):
    NONE = "none"
    OPERATOR = "operator"
    DOMAIN_REVIEWER = "domain_reviewer"
    INSTITUTIONAL = "institutional"


class ControlKind(str, Enum):
    POSITIVE = "positive_control"
    NEGATIVE = "negative_control"
    MATCHED = "matched_control"
    SHAM = "sham_control"
    BASELINE = "baseline_control"


class CapabilitySchemaDescriptor(FrozenModel):
    schema_version: Literal[1] = 1
    schema_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    media_type: Literal["application/schema+json"] = "application/schema+json"
    json_schema: dict[str, Any]
    json_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _schema_hash_matches(self) -> "CapabilitySchemaDescriptor":
        if self.json_schema.get("type") != "object":
            raise ValueError("capability JSON schemas must describe an object")
        if content_sha256(self.json_schema) != self.json_schema_sha256:
            raise ValueError("capability JSON schema hash is invalid")
        return self


class CapabilityRoleBinding(FrozenModel):
    schema_version: Literal[1] = 1
    role: CapabilityRole
    adapter_ref: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_.]*:[a-zA-Z_][a-zA-Z0-9_.]*$")
    implementation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime: CapabilityRuntime
    boundary: CapabilityBoundary
    allowed_tools: tuple[str, ...] = ()
    agent_authored: bool = False
    frozen_at: AwareDatetime

    @model_validator(mode="after")
    def _tools_are_canonical(self) -> "CapabilityRoleBinding":
        if self.allowed_tools != tuple(sorted(set(self.allowed_tools))):
            raise ValueError("capability role tools must be unique and sorted")
        if (
            self.role is CapabilityRole.PLANNER
            and self.boundary is not CapabilityBoundary.NO_EXECUTION
        ):
            raise ValueError("capability planner must have a no-execution boundary")
        return self


class CapabilityControlRequirement(FrozenModel):
    schema_version: Literal[1] = 1
    control_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    kind: ControlKind
    description: str = Field(min_length=1, max_length=2048)
    required: Literal[True] = True


class CapabilityAssumption(FrozenModel):
    schema_version: Literal[1] = 1
    assumption_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    statement: str = Field(min_length=1, max_length=2048)
    violation_consequence: str = Field(min_length=1, max_length=2048)
    test_or_monitor: str = Field(min_length=1, max_length=2048)


class CapabilityFailureMode(FrozenModel):
    schema_version: Literal[1] = 1
    failure_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    description: str = Field(min_length=1, max_length=2048)
    detection: str = Field(min_length=1, max_length=2048)
    disposition: Literal["invalid", "blocked", "qualified_negative", "exploratory_only"]


class CapabilityMinimumSampleRule(FrozenModel):
    schema_version: Literal[1] = 1
    sampling_unit: str = Field(min_length=1, max_length=128)
    minimum_count: int = Field(gt=0)
    power_or_precision_rule: str = Field(min_length=1, max_length=2048)
    rule_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CapabilityResourceEstimate(FrozenModel):
    schema_version: Literal[1] = 1
    estimated_cost_usd: float = Field(ge=0)
    estimated_wall_time_seconds: int = Field(gt=0)
    cpu_seconds: int = Field(ge=0)
    memory_mb: int = Field(gt=0)
    gpu_seconds: int = Field(ge=0)
    external_resource_units: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _resource_values_are_finite(self) -> "CapabilityResourceEstimate":
        if any(
            not key.strip() or not math.isfinite(value) or value < 0
            for key, value in self.external_resource_units.items()
        ):
            raise ValueError("external resource estimates must be finite and nonnegative")
        return self


class CapabilityNondeterminismPolicy(FrozenModel):
    schema_version: Literal[1] = 1
    mode: Literal["deterministic", "frozen_seeds", "declared_stochastic"]
    frozen_seeds: tuple[int, ...] = ()
    maximum_attempts_per_slot: int = Field(default=1, ge=1)
    best_of_n_forbidden: Literal[True] = True
    aggregation_rule: str = Field(min_length=1, max_length=2048)
    stopping_rule: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def _seeds_match_mode(self) -> "CapabilityNondeterminismPolicy":
        if self.frozen_seeds != tuple(sorted(set(self.frozen_seeds))):
            raise ValueError("frozen seeds must be unique and sorted")
        if self.mode == "deterministic" and self.frozen_seeds:
            raise ValueError("deterministic capability cannot declare stochastic seeds")
        if self.mode == "frozen_seeds" and not self.frozen_seeds:
            raise ValueError("frozen-seed capability requires at least one seed")
        return self


class CapabilityReproductionPolicy(FrozenModel):
    schema_version: Literal[1] = 1
    minimum_exact_reexecutions: int = Field(ge=1)
    independent_validator_recomputation: Literal[True] = True
    independent_implementation_required: bool
    independent_dataset_required: bool
    metric_tolerance: float = Field(ge=0)
    failure_retention: Literal[True] = True


class CapabilityLicenseEgressPolicy(FrozenModel):
    schema_version: Literal[1] = 1
    allowed_data_classes: tuple[str, ...] = Field(min_length=1)
    network_egress: Literal["none", "allowlisted", "site_managed"]
    raw_data_retention: Literal["required", "hash_only", "site_controlled"]
    license_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _data_classes_are_canonical(self) -> "CapabilityLicenseEgressPolicy":
        if self.allowed_data_classes != tuple(sorted(set(self.allowed_data_classes))):
            raise ValueError("allowed data classes must be unique and sorted")
        return self


class CapabilityRegistrationEvidence(FrozenModel):
    schema_version: Literal[1] = 1
    reference_fixtures_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    adversarial_fixtures_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    positive_control_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    negative_control_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    independent_recomputation_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproduction_policy_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    safety_review_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    domain_review_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    domain_reviewer_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    promotion_auditor_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_at: AwareDatetime


class ExperimentCapabilityManifest(FrozenModel):
    """Versioned contract for one scientific action and its trust boundaries."""

    schema_name: Literal["aletheia.experiment_capability_manifest"] = (
        "aletheia.experiment_capability_manifest"
    )
    schema_version: Literal[1] = 1
    capability_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    domain: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    lifecycle: CapabilityLifecycle
    scientific_question_ids: tuple[str, ...] = Field(min_length=1)
    claim_types_supported: tuple[CapabilityClaimType, ...] = Field(min_length=1)
    maximum_evidence_level: CapabilityEvidenceLevel
    input_schema: CapabilitySchemaDescriptor
    output_schema: CapabilitySchemaDescriptor
    accepted_data_modalities: tuple[str, ...] = Field(min_length=1)
    required_metadata: tuple[str, ...] = ()
    units_and_ontologies: tuple[str, ...] = Field(min_length=1)
    action_type: CapabilityActionType
    roles: tuple[CapabilityRoleBinding, ...] = Field(min_length=4, max_length=4)
    preregistration_schema: CapabilitySchemaDescriptor
    controls_required: tuple[CapabilityControlRequirement, ...]
    assumptions: tuple[CapabilityAssumption, ...] = Field(min_length=1)
    known_failure_modes: tuple[CapabilityFailureMode, ...] = Field(min_length=1)
    minimum_sample_rule: CapabilityMinimumSampleRule
    resources: CapabilityResourceEstimate
    nondeterminism_policy: CapabilityNondeterminismPolicy
    reproduction_policy: CapabilityReproductionPolicy
    safety_class: SafetyClass
    approval_class: ApprovalClass
    license_egress_policy: CapabilityLicenseEgressPolicy
    supersedes_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    registration_evidence: CapabilityRegistrationEvidence | None = None
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _manifest_is_closed_and_role_separated(self) -> "ExperimentCapabilityManifest":
        canonical_fields: tuple[tuple[Any, ...], str] = (
            (self.scientific_question_ids, "scientific questions"),
            (self.claim_types_supported, "claim types"),
            (self.accepted_data_modalities, "data modalities"),
            (self.required_metadata, "required metadata"),
            (self.units_and_ontologies, "units/ontologies"),
        )
        for values, label in canonical_fields:
            if values != tuple(sorted(set(values), key=_canonical_value)):
                raise ValueError(f"capability {label} must be unique and sorted")
        if tuple(item.role for item in self.roles) != _ROLE_ORDER:
            raise ValueError(
                "capability roles must contain planner/executor/parser/validator in order"
            )
        principals = [item.principal_sha256 for item in self.roles]
        if len(principals) != len(set(principals)):
            raise ValueError("capability scientific roles require distinct principals")
        executor = self.roles[1]
        validator = self.roles[3]
        if executor.adapter_ref == validator.adapter_ref:
            raise ValueError("capability executor and validator adapters must differ")
        if self.lifecycle is CapabilityLifecycle.PROVISIONAL:
            if self.maximum_evidence_level is not CapabilityEvidenceLevel.EXPLORATORY:
                raise ValueError("provisional capabilities can produce exploratory evidence only")
            if self.registration_evidence is not None:
                raise ValueError("provisional capability cannot carry registration evidence")
            forbidden = {
                CapabilityClaimType.MECHANISM_CANDIDATE,
                CapabilityClaimType.EXPERIMENTAL_CAUSAL,
            }
            if forbidden.intersection(self.claim_types_supported):
                raise ValueError("provisional capability cannot support strong causal claims")
        elif self.lifecycle is CapabilityLifecycle.REGISTERED:
            if self.registration_evidence is None:
                raise ValueError("registered capability requires complete promotion evidence")
            if validator.agent_authored:
                raise ValueError("registered capability validator cannot be agent-authored")
            control_kinds = {item.kind for item in self.controls_required}
            if not {ControlKind.POSITIVE, ControlKind.NEGATIVE}.issubset(control_kinds):
                raise ValueError("registered capability requires positive and negative controls")
            evidence = self.registration_evidence
            if evidence.reviewed_at < max(item.frozen_at for item in self.roles):
                raise ValueError("capability registration review predates a role implementation")
            excluded = set(principals)
            if {
                evidence.domain_reviewer_principal_sha256,
                evidence.promotion_auditor_principal_sha256,
            } & excluded:
                raise ValueError("capability promotion reviewers must be role-independent")
            if (
                evidence.domain_reviewer_principal_sha256
                == evidence.promotion_auditor_principal_sha256
            ):
                raise ValueError("domain reviewer and promotion auditor must differ")
        elif self.registration_evidence is None:
            raise ValueError("retired capability must retain its registration evidence")
        if self.safety_class is SafetyClass.PHYSICAL_HAZARD and self.approval_class not in {
            ApprovalClass.DOMAIN_REVIEWER,
            ApprovalClass.INSTITUTIONAL,
        }:
            raise ValueError("physical-hazard capability requires domain/institutional approval")
        ids_and_label = (
            (tuple(item.control_id for item in self.controls_required), "controls"),
            (tuple(item.assumption_id for item in self.assumptions), "assumptions"),
            (tuple(item.failure_id for item in self.known_failure_modes), "failure modes"),
        )
        for values, label in ids_and_label:
            if values != tuple(sorted(set(values))):
                raise ValueError(f"capability {label} must be unique and sorted")
        return self

    @property
    def semantic_version(self) -> tuple[int, int, int]:
        return tuple(int(item) for item in self.version.split("."))  # type: ignore[return-value]

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


def schema_descriptor(
    *, schema_id: str, version: str, json_schema: dict[str, Any]
) -> CapabilitySchemaDescriptor:
    return CapabilitySchemaDescriptor(
        schema_id=schema_id,
        version=version,
        json_schema=json_schema,
        json_schema_sha256=content_sha256(json_schema),
    )


__all__ = [
    "ApprovalClass",
    "CapabilityActionType",
    "CapabilityAssumption",
    "CapabilityBoundary",
    "CapabilityClaimType",
    "CapabilityControlRequirement",
    "CapabilityEvidenceLevel",
    "CapabilityFailureMode",
    "CapabilityLicenseEgressPolicy",
    "CapabilityLifecycle",
    "CapabilityMinimumSampleRule",
    "CapabilityNondeterminismPolicy",
    "CapabilityRegistrationEvidence",
    "CapabilityReproductionPolicy",
    "CapabilityResourceEstimate",
    "CapabilityRole",
    "CapabilityRoleBinding",
    "CapabilityRuntime",
    "CapabilitySchemaDescriptor",
    "ControlKind",
    "ExperimentCapabilityManifest",
    "SafetyClass",
    "evidence_level_rank",
    "safety_class_rank",
    "schema_descriptor",
]
