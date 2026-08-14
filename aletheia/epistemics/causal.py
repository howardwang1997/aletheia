"""F9-S3 explicit causal contracts and conservative identification audit.

The causal author proposes a graph.  A deterministic harness validates graph structure and a
separate reviewer adjudicates every identification assumption.  The only mechanically supported
identification criterion in this slice is Pearl's back-door criterion; unsupported strategies stay
explicitly blocked instead of being mislabeled as non-identifiable or causal.
"""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, ValidationError, model_validator

from aletheia.epistemics.hypotheses import (
    HypothesisGenerationCampaign,
    HypothesisGenerationDisposition,
)
from aletheia.epistemics.schemas import EpistemicModel, HypothesisRole
from aletheia.knowledge.response_archive import (
    ArchivedKnowledgeLedger,
    ContentAddressedResponseArchive,
)
from aletheia.knowledge.schemas import AtomicClaim
from aletheia.reproducibility.manifest import canonical_json_bytes, content_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_LOCAL_ID_PATTERN = r"^[a-z][a-z0-9_.-]{1,79}$"


class CausalAdapterRuntime(str, Enum):
    DETERMINISTIC = "deterministic"
    MODEL = "model"


class CausalVariableRole(str, Enum):
    EXPOSURE = "exposure"
    OUTCOME = "outcome"
    MEDIATOR = "mediator"
    CONFOUNDER = "confounder"
    COVARIATE = "covariate"
    SELECTION = "selection"
    MEASUREMENT = "measurement"
    CONTEXT = "context"
    NOISE = "noise"


class CausalObservability(str, Enum):
    OBSERVED = "observed"
    LATENT = "latent"


class CausalIntervenability(str, Enum):
    DIRECT = "direct"
    INDIRECT = "indirect"
    NOT_INTERVENABLE = "not_intervenable"


class CausalValueKind(str, Enum):
    BINARY = "binary"
    CATEGORICAL = "categorical"
    CONTINUOUS = "continuous"
    COUNT = "count"
    TIME_TO_EVENT = "time_to_event"


class CausalEvidenceKind(str, Enum):
    DESCRIPTIVE = "descriptive"
    OBSERVATIONAL_ASSOCIATION = "observational_association"
    NATURAL_EXPERIMENT = "natural_experiment"
    CONTROLLED_INTERVENTION = "controlled_intervention"
    SIMULATION_INTERVENTION = "simulation_intervention"
    MEASUREMENT_VALIDATION = "measurement_validation"
    INDEPENDENT_REPLICATION = "independent_replication"


class CausalEffectScale(str, Enum):
    MEAN_DIFFERENCE = "mean_difference"
    RISK_DIFFERENCE = "risk_difference"
    RISK_RATIO = "risk_ratio"
    DISTRIBUTION_SHIFT = "distribution_shift"
    QUALITATIVE = "qualitative"


class IdentificationStrategy(str, Enum):
    BACKDOOR_ADJUSTMENT = "backdoor_adjustment"
    RANDOMIZED_INTERVENTION = "randomized_intervention"
    FRONTDOOR_ADJUSTMENT = "frontdoor_adjustment"
    INSTRUMENTAL_VARIABLE = "instrumental_variable"
    GENERAL_ID_ALGORITHM = "general_id_algorithm"


class IdentificationAssumptionKind(str, Enum):
    CONSISTENCY = "consistency"
    POSITIVITY = "positivity"
    EXCHANGEABILITY = "exchangeability"
    NO_INTERFERENCE = "no_interference"
    TEMPORAL_ORDER = "temporal_order"
    MEASUREMENT_VALIDITY = "measurement_validity"
    SELECTION_EXCHANGEABILITY = "selection_exchangeability"
    MODEL_CORRECTNESS = "model_correctness"
    DOMAIN_INVARIANCE = "domain_invariance"


class AssumptionReviewDecision(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    UNRESOLVED = "unresolved"


class AssumptionResolutionStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNRESOLVED = "unresolved"
    LOW_CONFIDENCE = "low_confidence"


class BackdoorAuditStatus(str, Enum):
    IDENTIFIED = "identified_by_backdoor"
    OPEN_BACKDOOR_PATH = "open_backdoor_path"
    INVALID_ADJUSTMENT = "invalid_adjustment_set"
    INVALID_GRAPH = "invalid_graph"
    SELECTION_RECOVERABILITY_UNSUPPORTED = "selection_recoverability_unsupported"
    UNSUPPORTED_STRATEGY = "unsupported_identification_strategy"


class CausalClaimCeiling(str, Enum):
    NONE = "none"
    DESCRIPTIVE_ONLY = "descriptive_only"
    ASSOCIATION_ONLY = "association_only"
    WITHIN_MODEL_CAUSAL_ONLY = "within_model_causal_only"
    CAUSAL_CANDIDATE = "causal_candidate"


class CausalAuditDisposition(str, Enum):
    READY_IDENTIFIED = "ready_identified"
    READY_BOUNDED = "ready_bounded"
    BLOCKED_STRUCTURE = "blocked_structure"
    BLOCKED_ASSUMPTIONS = "blocked_assumptions"
    BLOCKED_EXECUTION = "blocked_execution"


class CausalAuditFailureKind(str, Enum):
    AUTHOR_ERROR = "author_error"
    AUTHOR_OUTPUT_INVALID = "author_output_invalid"
    REVIEWER_ERROR = "reviewer_error"
    REVIEWER_OUTPUT_INVALID = "reviewer_output_invalid"


class CausalVariable(EpistemicModel):
    variable_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    label: str = Field(min_length=1, max_length=512)
    definition: str = Field(min_length=1, max_length=8192)
    roles: tuple[CausalVariableRole, ...] = Field(min_length=1)
    value_kind: CausalValueKind
    units: str | None = Field(default=None, max_length=256)
    observability: CausalObservability
    intervenability: CausalIntervenability
    observable_id: str | None = Field(default=None, max_length=512)
    measurement_protocol_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    grounding_claim_sha256s: tuple[str, ...] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def _variable_is_explicit_and_canonical(self) -> "CausalVariable":
        if not self.label.strip() or not self.definition.strip():
            raise ValueError("causal variable label and definition cannot be blank")
        if self.roles != tuple(sorted(set(self.roles), key=lambda item: item.value)):
            raise ValueError("causal variable roles must be unique and canonical")
        if self.grounding_claim_sha256s != tuple(sorted(set(self.grounding_claim_sha256s))):
            raise ValueError("causal variable grounding must be unique and canonical")
        observed = self.observability is CausalObservability.OBSERVED
        has_observable = self.observable_id is not None
        has_protocol = self.measurement_protocol_sha256 is not None
        if observed != (has_observable and has_protocol):
            raise ValueError("observed variables require observable and measurement-protocol IDs")
        if self.observable_id is not None and not self.observable_id.strip():
            raise ValueError("causal variable observable ID cannot be blank")
        if CausalVariableRole.MEASUREMENT in self.roles and not observed:
            raise ValueError("measurement variables must be observed")
        if self.units is not None and not self.units.strip():
            raise ValueError("causal variable units cannot be blank")
        return self

    @property
    def variable_sha256(self) -> str:
        return content_sha256(self)


class IdentificationAssumption(EpistemicModel):
    assumption_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    kind: IdentificationAssumptionKind
    statement: str = Field(min_length=1, max_length=8192)
    risk_if_violated: str = Field(min_length=1, max_length=8192)
    applies_to_hypothesis_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    variable_ids: tuple[str, ...] = Field(min_length=1, max_length=256)
    grounding_claim_sha256s: tuple[str, ...] = Field(min_length=1, max_length=256)
    diagnostic_protocol_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    required_for_identification: Literal[True] = True

    @model_validator(mode="after")
    def _assumption_is_canonical(self) -> "IdentificationAssumption":
        if not self.statement.strip() or not self.risk_if_violated.strip():
            raise ValueError("identification assumption text cannot be blank")
        for values, label in (
            (self.applies_to_hypothesis_ids, "hypothesis IDs"),
            (self.variable_ids, "variable IDs"),
            (self.grounding_claim_sha256s, "grounding claims"),
        ):
            if values != tuple(sorted(set(values))):
                raise ValueError(f"identification assumption {label} must be unique and canonical")
        return self

    @property
    def assumption_sha256(self) -> str:
        return content_sha256(self)


class CausalEdge(EpistemicModel):
    edge_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    source_variable_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    target_variable_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    mechanism: str = Field(min_length=1, max_length=8192)
    assumption_ids: tuple[str, ...] = Field(min_length=1, max_length=512)
    grounding_claim_sha256s: tuple[str, ...] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def _edge_is_canonical(self) -> "CausalEdge":
        if self.source_variable_id == self.target_variable_id:
            raise ValueError("causal edge cannot be a self-loop")
        if not self.mechanism.strip():
            raise ValueError("causal edge mechanism cannot be blank")
        if self.assumption_ids != tuple(sorted(set(self.assumption_ids))):
            raise ValueError("causal edge assumptions must be unique and canonical")
        if self.grounding_claim_sha256s != tuple(sorted(set(self.grounding_claim_sha256s))):
            raise ValueError("causal edge grounding must be unique and canonical")
        return self

    @property
    def edge_sha256(self) -> str:
        return content_sha256(self)


class LatentConfounder(EpistemicModel):
    confounder_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    variable_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    affected_variable_ids: tuple[str, ...] = Field(min_length=2, max_length=256)
    assumption_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    grounding_claim_sha256s: tuple[str, ...] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def _confounder_is_canonical(self) -> "LatentConfounder":
        if self.affected_variable_ids != tuple(sorted(set(self.affected_variable_ids))):
            raise ValueError("latent-confounder targets must be unique and canonical")
        if self.variable_id in self.affected_variable_ids:
            raise ValueError("latent confounder cannot affect itself")
        if self.grounding_claim_sha256s != tuple(sorted(set(self.grounding_claim_sha256s))):
            raise ValueError("latent-confounder grounding must be unique and canonical")
        return self

    @property
    def confounder_sha256(self) -> str:
        return content_sha256(self)


class MeasurementProcess(EpistemicModel):
    process_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    construct_variable_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    indicator_variable_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    measurement_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    error_model_sha256: str = Field(pattern=_SHA256_PATTERN)
    validity_assumption_id: str = Field(pattern=_LOCAL_ID_PATTERN)

    @model_validator(mode="after")
    def _measurement_is_nontrivial(self) -> "MeasurementProcess":
        if self.construct_variable_id == self.indicator_variable_id:
            raise ValueError("measurement process requires distinct construct and indicator")
        return self

    @property
    def process_sha256(self) -> str:
        return content_sha256(self)


class SelectionMechanism(EpistemicModel):
    mechanism_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    selection_variable_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    parent_variable_ids: tuple[str, ...] = Field(min_length=1)
    selection_rule_sha256: str = Field(pattern=_SHA256_PATTERN)
    exchangeability_assumption_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    analysis_conditions_on_selection: bool

    @model_validator(mode="after")
    def _selection_is_canonical(self) -> "SelectionMechanism":
        if self.parent_variable_ids != tuple(sorted(set(self.parent_variable_ids))):
            raise ValueError("selection parents must be unique and canonical")
        if self.selection_variable_id in self.parent_variable_ids:
            raise ValueError("selection mechanism cannot select itself")
        return self

    @property
    def mechanism_sha256(self) -> str:
        return content_sha256(self)


class CausalEstimand(EpistemicModel):
    estimand_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    exposure_variable_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    outcome_variable_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    intervention_levels: tuple[str, str]
    effect_scale: CausalEffectScale
    target_population_sha256: str = Field(pattern=_SHA256_PATTERN)
    identification_strategy: IdentificationStrategy
    adjustment_variable_ids: tuple[str, ...] = ()
    proposed_evidence_kind: CausalEvidenceKind

    @model_validator(mode="after")
    def _estimand_is_explicit(self) -> "CausalEstimand":
        if self.exposure_variable_id == self.outcome_variable_id:
            raise ValueError("causal estimand exposure and outcome must differ")
        levels = tuple(item.strip() for item in self.intervention_levels)
        if any(not item for item in levels) or levels[0] == levels[1]:
            raise ValueError("causal estimand requires two distinct intervention levels")
        if self.adjustment_variable_ids != tuple(sorted(set(self.adjustment_variable_ids))):
            raise ValueError("causal adjustment variables must be unique and canonical")
        return self

    @property
    def estimand_sha256(self) -> str:
        return content_sha256(self)


class HypothesisCausalGraph(EpistemicModel):
    hypothesis_id: str = Field(pattern=r"^hyp_[0-9a-f]{32}$")
    hypothesis_version_sha256: str = Field(pattern=_SHA256_PATTERN)
    edges: tuple[CausalEdge, ...] = Field(max_length=512)
    latent_confounders: tuple[LatentConfounder, ...] = Field(default=(), max_length=256)
    grounding_claim_sha256s: tuple[str, ...] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def _graph_is_canonical(self) -> "HypothesisCausalGraph":
        edge_ids = [item.edge_id for item in self.edges]
        confounder_ids = [item.confounder_id for item in self.latent_confounders]
        if edge_ids != sorted(set(edge_ids)):
            raise ValueError("causal graph edges must use unique canonical IDs")
        if confounder_ids != sorted(set(confounder_ids)):
            raise ValueError("latent confounders must use unique canonical IDs")
        if self.grounding_claim_sha256s != tuple(sorted(set(self.grounding_claim_sha256s))):
            raise ValueError("hypothesis causal grounding must be unique and canonical")
        return self

    @property
    def graph_sha256(self) -> str:
        return content_sha256(self)


class CausalContract(EpistemicModel):
    schema_version: Literal[1] = 1
    world_model_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    question_sha256: str = Field(pattern=_SHA256_PATTERN)
    variables: tuple[CausalVariable, ...] = Field(min_length=3, max_length=256)
    assumptions: tuple[IdentificationAssumption, ...] = Field(min_length=1, max_length=512)
    measurement_processes: tuple[MeasurementProcess, ...] = Field(min_length=1, max_length=128)
    selection_mechanisms: tuple[SelectionMechanism, ...] = Field(default=(), max_length=128)
    estimand: CausalEstimand
    outcome_measurement_process_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    hypothesis_graphs: tuple[HypothesisCausalGraph, ...] = Field(min_length=3, max_length=64)

    @model_validator(mode="after")
    def _contract_collections_are_canonical(self) -> "CausalContract":
        collections = (
            ([item.variable_id for item in self.variables], "variables"),
            ([item.assumption_id for item in self.assumptions], "assumptions"),
            ([item.process_id for item in self.measurement_processes], "measurement processes"),
            ([item.mechanism_id for item in self.selection_mechanisms], "selection mechanisms"),
            ([item.hypothesis_id for item in self.hypothesis_graphs], "hypothesis graphs"),
        )
        for identities, label in collections:
            if identities != sorted(set(identities)):
                raise ValueError(f"causal contract {label} must use unique canonical IDs")
        return self

    @property
    def contract_sha256(self) -> str:
        return content_sha256(self)


class CausalContractBatch(EpistemicModel):
    schema_version: Literal[1] = 1
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    author_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    contract: CausalContract
    completed_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @property
    def batch_sha256(self) -> str:
        return content_sha256(self)


CAUSAL_CONTRACT_OUTPUT_SCHEMA_SHA256 = content_sha256(CausalContractBatch.model_json_schema())


class CausalContractAuthorManifest(EpistemicModel):
    schema_version: Literal[1] = 1
    author_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    runtime: CausalAdapterRuntime
    adapter_code_sha256: str = Field(pattern=_SHA256_PATTERN)
    parser_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_schema_sha256: str = Field(pattern=_SHA256_PATTERN)
    instruction_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    model_identity_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    author_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    maximum_variables: int = Field(default=128, ge=3, le=256)
    maximum_edges_per_hypothesis: int = Field(default=128, ge=0, le=512)
    maximum_assumptions: int = Field(default=256, ge=1, le=512)
    tool_names: tuple[str, ...] = ()
    tool_policy: Literal["none"] = "none"
    transport_policy: Literal["none", "model_transport_only"]
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _author_is_frozen_and_unprivileged(self) -> "CausalContractAuthorManifest":
        if self.output_schema_sha256 != CAUSAL_CONTRACT_OUTPUT_SCHEMA_SHA256:
            raise ValueError("causal author uses another output schema")
        if self.tool_names:
            raise ValueError("causal author cannot receive tool authority")
        model_fields = self.instruction_sha256 is not None and self.model_identity_sha256 is not None
        if self.runtime is CausalAdapterRuntime.MODEL:
            if not model_fields or self.transport_policy != "model_transport_only":
                raise ValueError("model causal author requires frozen instruction/model and transport")
        elif (
            self.instruction_sha256 is not None
            or self.model_identity_sha256 is not None
            or self.transport_policy != "none"
        ):
            raise ValueError("deterministic causal author cannot declare model transport")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class CausalAssumptionReview(EpistemicModel):
    schema_version: Literal[1] = 1
    assumption_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    assumption_sha256: str = Field(pattern=_SHA256_PATTERN)
    decision: AssumptionReviewDecision
    confidence: float = Field(ge=0.0, le=1.0)
    rationale_sha256: str = Field(pattern=_SHA256_PATTERN)
    evidence_claim_sha256s: tuple[str, ...] = Field(max_length=256)
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def _review_is_canonical(self) -> "CausalAssumptionReview":
        if self.evidence_claim_sha256s != tuple(sorted(set(self.evidence_claim_sha256s))):
            raise ValueError("causal-assumption review evidence must be unique and canonical")
        if self.decision is AssumptionReviewDecision.ACCEPT and not self.evidence_claim_sha256s:
            raise ValueError("accepted identification assumption requires F8 evidence")
        return self

    @property
    def review_sha256(self) -> str:
        return content_sha256(self)


class CausalAssumptionReviewBatch(EpistemicModel):
    schema_version: Literal[1] = 1
    causal_contract_batch_sha256: str = Field(pattern=_SHA256_PATTERN)
    reviewer_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    reviews: tuple[CausalAssumptionReview, ...] = Field(min_length=1, max_length=512)
    completed_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _reviews_are_canonical(self) -> "CausalAssumptionReviewBatch":
        identities = [item.assumption_id for item in self.reviews]
        if identities != sorted(set(identities)):
            raise ValueError("causal-assumption reviews must use unique canonical IDs")
        if self.completed_at < max(item.completed_at for item in self.reviews):
            raise ValueError("causal-assumption review batch predates a review")
        return self

    @property
    def batch_sha256(self) -> str:
        return content_sha256(self)


CAUSAL_REVIEW_OUTPUT_SCHEMA_SHA256 = content_sha256(
    CausalAssumptionReviewBatch.model_json_schema()
)


class CausalAssumptionReviewerManifest(EpistemicModel):
    schema_version: Literal[1] = 1
    reviewer_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    runtime: CausalAdapterRuntime
    adapter_code_sha256: str = Field(pattern=_SHA256_PATTERN)
    parser_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_schema_sha256: str = Field(pattern=_SHA256_PATTERN)
    instruction_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    model_identity_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    reviewer_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    tool_names: tuple[str, ...] = ()
    tool_policy: Literal["none"] = "none"
    transport_policy: Literal["none", "model_transport_only"]
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _reviewer_is_frozen_and_unprivileged(self) -> "CausalAssumptionReviewerManifest":
        if self.output_schema_sha256 != CAUSAL_REVIEW_OUTPUT_SCHEMA_SHA256:
            raise ValueError("causal-assumption reviewer uses another output schema")
        if self.tool_names:
            raise ValueError("causal-assumption reviewer cannot receive tool authority")
        model_fields = self.instruction_sha256 is not None and self.model_identity_sha256 is not None
        if self.runtime is CausalAdapterRuntime.MODEL:
            if not model_fields or self.transport_policy != "model_transport_only":
                raise ValueError(
                    "model causal-assumption reviewer requires frozen instruction/model and transport"
                )
        elif (
            self.instruction_sha256 is not None
            or self.model_identity_sha256 is not None
            or self.transport_policy != "none"
        ):
            raise ValueError("deterministic causal-assumption reviewer cannot declare model transport")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class CausalAuditPolicy(EpistemicModel):
    schema_version: Literal[1] = 1
    policy_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    minimum_review_confidence: float = Field(default=0.8, gt=0.5, le=1.0)
    maximum_variables: int = Field(default=128, ge=3, le=256)
    maximum_edges_per_hypothesis: int = Field(default=128, ge=0, le=512)
    maximum_assumptions: int = Field(default=256, ge=1, le=512)
    supported_identification_strategy: Literal[IdentificationStrategy.BACKDOOR_ADJUSTMENT] = (
        IdentificationStrategy.BACKDOOR_ADJUSTMENT
    )
    require_complete_independent_review: Literal[True] = True
    harness_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class CausalHypothesisBinding(EpistemicModel):
    hypothesis_id: str = Field(pattern=r"^hyp_[0-9a-f]{32}$")
    hypothesis_version_sha256: str = Field(pattern=_SHA256_PATTERN)
    role: HypothesisRole


class CausalContractRequest(EpistemicModel):
    schema_version: Literal[1] = 1
    request_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    source_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    direction_gate_sha256: str = Field(pattern=_SHA256_PATTERN)
    world_model_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    question_sha256: str = Field(pattern=_SHA256_PATTERN)
    hypothesis_bindings: tuple[CausalHypothesisBinding, ...] = Field(min_length=3, max_length=64)
    input_claim_sha256s: tuple[str, ...] = Field(min_length=2)
    accepted_prior_art_relation_sha256s: tuple[str, ...] = Field(min_length=1)
    proposed_evidence_kind: CausalEvidenceKind
    author_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    reviewer_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    issued_at: AwareDatetime
    observation_access: Literal["none"] = "none"

    @model_validator(mode="after")
    def _request_is_canonical(self) -> "CausalContractRequest":
        identities = [item.hypothesis_id for item in self.hypothesis_bindings]
        if identities != sorted(set(identities)):
            raise ValueError("causal request hypothesis bindings must be unique and canonical")
        if self.input_claim_sha256s != tuple(sorted(set(self.input_claim_sha256s))):
            raise ValueError("causal request input claims must be unique and canonical")
        relations = self.accepted_prior_art_relation_sha256s
        if len(relations) != len(set(relations)):
            raise ValueError("causal request accepted prior-art relations must be unique")
        return self

    @property
    def request_sha256(self) -> str:
        return content_sha256(self)


class HypothesisGraphAudit(EpistemicModel):
    hypothesis_id: str = Field(pattern=r"^hyp_[0-9a-f]{32}$")
    hypothesis_version_sha256: str = Field(pattern=_SHA256_PATTERN)
    graph_sha256: str = Field(pattern=_SHA256_PATTERN)
    directed_exposure_outcome_path: tuple[str, ...]
    open_backdoor_path: tuple[str, ...]
    adjustment_variable_ids: tuple[str, ...]
    backdoor_status: BackdoorAuditStatus
    blockers: tuple[str, ...]

    @property
    def audit_sha256(self) -> str:
        return content_sha256(self)


class CausalAssumptionResolution(EpistemicModel):
    assumption_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    assumption_sha256: str = Field(pattern=_SHA256_PATTERN)
    review_sha256: str = Field(pattern=_SHA256_PATTERN)
    status: AssumptionResolutionStatus

    @property
    def resolution_sha256(self) -> str:
        return content_sha256(self)


class CausalAuditFailure(EpistemicModel):
    kind: CausalAuditFailureKind
    error_class: str = Field(min_length=1, max_length=256)
    error_detail_sha256: str = Field(pattern=_SHA256_PATTERN)
    raw_output_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    occurred_at: AwareDatetime

    @property
    def failure_sha256(self) -> str:
        return content_sha256(self)


class _DerivedCausalOutputs(EpistemicModel):
    graph_audits: tuple[HypothesisGraphAudit, ...]
    assumption_resolutions: tuple[CausalAssumptionResolution, ...]
    blockers: tuple[str, ...]
    disposition: CausalAuditDisposition
    claim_ceiling: CausalClaimCeiling
    prediction_planning_authorized: bool


class CausalAuditCampaign(EpistemicModel):
    schema_version: Literal[1] = 1
    campaign_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    source_campaign: HypothesisGenerationCampaign
    policy: CausalAuditPolicy
    author_manifest: CausalContractAuthorManifest
    reviewer_manifest: CausalAssumptionReviewerManifest
    request: CausalContractRequest
    contract_batch: CausalContractBatch | None = None
    review_batch: CausalAssumptionReviewBatch | None = None
    failure: CausalAuditFailure | None = None
    graph_audits: tuple[HypothesisGraphAudit, ...]
    assumption_resolutions: tuple[CausalAssumptionResolution, ...]
    blockers: tuple[str, ...]
    disposition: CausalAuditDisposition
    claim_ceiling: CausalClaimCeiling
    prediction_planning_authorized: bool
    generated_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _campaign_is_mechanically_derived(self) -> "CausalAuditCampaign":
        _validate_request_bindings(
            source_campaign=self.source_campaign,
            policy=self.policy,
            author_manifest=self.author_manifest,
            reviewer_manifest=self.reviewer_manifest,
            request=self.request,
        )
        if self.failure is not None:
            if self.review_batch is not None:
                raise ValueError("failed causal audit cannot retain a review batch")
            author_failure = self.failure.kind in {
                CausalAuditFailureKind.AUTHOR_ERROR,
                CausalAuditFailureKind.AUTHOR_OUTPUT_INVALID,
            }
            if author_failure != (self.contract_batch is None):
                raise ValueError("causal audit failure stage does not match retained contract")
            expected_audits: tuple[HypothesisGraphAudit, ...] = ()
            if not author_failure:
                assert self.contract_batch is not None
                _validate_contract_batch(
                    batch=self.contract_batch,
                    request=self.request,
                    manifest=self.author_manifest,
                    policy=self.policy,
                )
                expected_audits, structural_blockers = _derive_static_audits(
                    source_campaign=self.source_campaign,
                    request=self.request,
                    policy=self.policy,
                    contract=self.contract_batch.contract,
                )
                if structural_blockers:
                    raise ValueError("reviewer failure cannot follow a structurally blocked contract")
                if self.failure.occurred_at < self.contract_batch.completed_at:
                    raise ValueError("causal reviewer failure predates retained contract")
            expected_blockers = (f"execution_failure:{self.failure.kind.value}",)
            if (
                self.graph_audits != expected_audits
                or self.assumption_resolutions
                or self.blockers != expected_blockers
                or self.disposition is not CausalAuditDisposition.BLOCKED_EXECUTION
                or self.claim_ceiling is not CausalClaimCeiling.NONE
                or self.prediction_planning_authorized
            ):
                raise ValueError("failed causal-audit outputs are not mechanically derived")
            if self.failure.occurred_at < self.request.issued_at:
                raise ValueError("causal-audit failure predates its request")
            if self.generated_at < self.failure.occurred_at:
                raise ValueError("causal-audit campaign predates its failure")
            return self

        if self.contract_batch is None:
            raise ValueError("causal audit requires a contract batch")
        _validate_contract_batch(
            batch=self.contract_batch,
            request=self.request,
            manifest=self.author_manifest,
            policy=self.policy,
        )
        audits, structural_blockers = _derive_static_audits(
            source_campaign=self.source_campaign,
            request=self.request,
            policy=self.policy,
            contract=self.contract_batch.contract,
        )
        if structural_blockers:
            derived = _DerivedCausalOutputs(
                graph_audits=audits,
                assumption_resolutions=(),
                blockers=structural_blockers,
                disposition=CausalAuditDisposition.BLOCKED_STRUCTURE,
                claim_ceiling=CausalClaimCeiling.NONE,
                prediction_planning_authorized=False,
            )
            if self.review_batch is not None:
                raise ValueError("structurally blocked causal contract cannot have assumption review")
        else:
            if self.review_batch is None:
                raise ValueError("structurally valid causal contract requires complete review")
            _validate_review_batch(
                batch=self.review_batch,
                contract_batch=self.contract_batch,
                manifest=self.reviewer_manifest,
                request=self.request,
            )
            derived = _derive_reviewed_outputs(
                request=self.request,
                policy=self.policy,
                contract=self.contract_batch.contract,
                review_batch=self.review_batch,
                graph_audits=audits,
            )
        if (
            self.graph_audits != derived.graph_audits
            or self.assumption_resolutions != derived.assumption_resolutions
            or self.blockers != derived.blockers
            or self.disposition is not derived.disposition
            or self.claim_ceiling is not derived.claim_ceiling
            or self.prediction_planning_authorized != derived.prediction_planning_authorized
        ):
            raise ValueError("causal-audit campaign outputs are not mechanically derived")
        latest = (
            self.review_batch.completed_at
            if self.review_batch is not None
            else self.contract_batch.completed_at
        )
        if self.generated_at < latest:
            raise ValueError("causal-audit campaign predates its latest input")
        return self

    @property
    def campaign_sha256(self) -> str:
        return content_sha256(self)


class CommittedCausalAuditCampaign(EpistemicModel):
    schema_version: Literal[1] = 1
    campaign: CausalAuditCampaign
    ledger: ArchivedKnowledgeLedger

    @model_validator(mode="after")
    def _ledger_commits_campaign(self) -> "CommittedCausalAuditCampaign":
        payload = canonical_json_bytes(self.campaign)
        if (
            self.ledger.object_sha256 != self.campaign.campaign_sha256
            or self.ledger.ledger_sha256 != hashlib.sha256(payload).hexdigest()
            or self.ledger.ledger_bytes != len(payload)
        ):
            raise ValueError("causal-audit ledger does not commit its campaign")
        return self


class CausalContractAuthorAdapter(Protocol):
    @property
    def manifest(self) -> CausalContractAuthorManifest: ...

    async def author(
        self,
        *,
        request: CausalContractRequest,
        source_campaign: HypothesisGenerationCampaign,
        claims: tuple[AtomicClaim, ...],
    ) -> object: ...


class CausalAssumptionReviewerAdapter(Protocol):
    @property
    def manifest(self) -> CausalAssumptionReviewerManifest: ...

    async def review(
        self,
        *,
        contract_batch: CausalContractBatch,
        graph_audits: tuple[HypothesisGraphAudit, ...],
    ) -> object: ...


def _validate_independence(
    *,
    source_campaign: HypothesisGenerationCampaign,
    author: CausalContractAuthorManifest,
    reviewer: CausalAssumptionReviewerManifest,
) -> None:
    forbidden_review_principals = {
        author.author_principal_sha256,
        source_campaign.generator_manifest.generator_principal_sha256,
        source_campaign.deduplicator_manifest.reviewer_principal_sha256,
    }
    if reviewer.reviewer_principal_sha256 in forbidden_review_principals:
        raise ValueError("causal-assumption reviewer must be independent from prior proposal roles")
    forbidden_review_models = {
        item
        for item in (
            author.model_identity_sha256,
            source_campaign.generator_manifest.model_identity_sha256,
            source_campaign.deduplicator_manifest.model_identity_sha256,
        )
        if item is not None
    }
    if reviewer.model_identity_sha256 in forbidden_review_models:
        raise ValueError("causal-assumption reviewer must use an independent model")


def _hypothesis_bindings(
    source_campaign: HypothesisGenerationCampaign,
) -> tuple[CausalHypothesisBinding, ...]:
    snapshot = source_campaign.world_model_snapshot
    assert snapshot is not None
    return tuple(
        CausalHypothesisBinding(
            hypothesis_id=item.hypothesis_id,
            hypothesis_version_sha256=item.hypothesis_sha256,
            role=item.role,
        )
        for item in snapshot.hypotheses
    )


def _validate_request_bindings(
    *,
    source_campaign: HypothesisGenerationCampaign,
    policy: CausalAuditPolicy,
    author_manifest: CausalContractAuthorManifest,
    reviewer_manifest: CausalAssumptionReviewerManifest,
    request: CausalContractRequest,
) -> None:
    if (
        source_campaign.disposition is not HypothesisGenerationDisposition.READY
        or source_campaign.world_model_snapshot is None
    ):
        raise ValueError("causal contract requires a ready F9-S2 campaign")
    _validate_independence(
        source_campaign=source_campaign,
        author=author_manifest,
        reviewer=reviewer_manifest,
    )
    if (
        policy.maximum_variables > author_manifest.maximum_variables
        or policy.maximum_edges_per_hypothesis > author_manifest.maximum_edges_per_hypothesis
        or policy.maximum_assumptions > author_manifest.maximum_assumptions
    ):
        raise ValueError("causal-audit policy exceeds frozen author capacity")
    for frozen_at, label in (
        (policy.frozen_at, "causal policy"),
        (author_manifest.frozen_at, "causal author manifest"),
        (reviewer_manifest.frozen_at, "causal reviewer manifest"),
        (source_campaign.generated_at, "source hypothesis campaign"),
    ):
        if frozen_at > request.issued_at:
            raise ValueError(f"{label} must freeze before the causal request")
    snapshot = source_campaign.world_model_snapshot
    gate = source_campaign.direction_gate
    expected = {
        "source_campaign_sha256": source_campaign.campaign_sha256,
        "direction_gate_sha256": gate.gate_sha256,
        "world_model_snapshot_sha256": snapshot.snapshot_sha256,
        "question_sha256": snapshot.question.question_sha256,
        "hypothesis_bindings": _hypothesis_bindings(source_campaign),
        "input_claim_sha256s": source_campaign.request.input_claim_sha256s,
        "accepted_prior_art_relation_sha256s": (
            source_campaign.request.accepted_prior_art_relation_sha256s
        ),
        "author_manifest_sha256": author_manifest.manifest_sha256,
        "reviewer_manifest_sha256": reviewer_manifest.manifest_sha256,
        "policy_sha256": policy.policy_sha256,
    }
    for field, value in expected.items():
        if getattr(request, field) != value:
            raise ValueError(f"causal-contract request changed exact {field}")


def build_causal_contract_request(
    *,
    request_id: str,
    source_campaign: HypothesisGenerationCampaign,
    proposed_evidence_kind: CausalEvidenceKind,
    policy: CausalAuditPolicy,
    author_manifest: CausalContractAuthorManifest,
    reviewer_manifest: CausalAssumptionReviewerManifest,
    issued_at: AwareDatetime,
) -> CausalContractRequest:
    if (
        source_campaign.disposition is not HypothesisGenerationDisposition.READY
        or source_campaign.world_model_snapshot is None
    ):
        raise ValueError("causal contract requires a ready F9-S2 campaign")
    request = CausalContractRequest(
        request_id=request_id,
        source_campaign_sha256=source_campaign.campaign_sha256,
        direction_gate_sha256=source_campaign.direction_gate.gate_sha256,
        world_model_snapshot_sha256=source_campaign.world_model_snapshot.snapshot_sha256,
        question_sha256=source_campaign.world_model_snapshot.question.question_sha256,
        hypothesis_bindings=_hypothesis_bindings(source_campaign),
        input_claim_sha256s=source_campaign.request.input_claim_sha256s,
        accepted_prior_art_relation_sha256s=(
            source_campaign.request.accepted_prior_art_relation_sha256s
        ),
        proposed_evidence_kind=proposed_evidence_kind,
        author_manifest_sha256=author_manifest.manifest_sha256,
        reviewer_manifest_sha256=reviewer_manifest.manifest_sha256,
        policy_sha256=policy.policy_sha256,
        issued_at=issued_at,
    )
    _validate_request_bindings(
        source_campaign=source_campaign,
        policy=policy,
        author_manifest=author_manifest,
        reviewer_manifest=reviewer_manifest,
        request=request,
    )
    return request


def _validate_contract_batch(
    *,
    batch: CausalContractBatch,
    request: CausalContractRequest,
    manifest: CausalContractAuthorManifest,
    policy: CausalAuditPolicy,
    received_at: datetime | None = None,
) -> None:
    if (
        batch.request_sha256 != request.request_sha256
        or batch.author_manifest_sha256 != manifest.manifest_sha256
    ):
        raise ValueError("causal contract is bound to another request/author")
    if batch.completed_at < request.issued_at:
        raise ValueError("causal contract predates its request")
    if received_at is not None and batch.completed_at > received_at:
        raise ValueError("causal contract claims a future completion time")
    contract = batch.contract
    if len(contract.variables) > min(policy.maximum_variables, manifest.maximum_variables):
        raise ValueError("causal contract exceeds variable capacity")
    if len(contract.assumptions) > min(
        policy.maximum_assumptions, manifest.maximum_assumptions
    ):
        raise ValueError("causal contract exceeds assumption capacity")
    relation_limit = min(
        policy.maximum_edges_per_hypothesis,
        manifest.maximum_edges_per_hypothesis,
    )
    common_relations = len(contract.measurement_processes) + sum(
        len(item.parent_variable_ids) for item in contract.selection_mechanisms
    )
    if any(
        len(graph.edges)
        + sum(len(item.affected_variable_ids) for item in graph.latent_confounders)
        + common_relations
        > relation_limit
        for graph in contract.hypothesis_graphs
    ):
        raise ValueError("causal contract exceeds per-hypothesis edge capacity")


def _known_claims(source_campaign: HypothesisGenerationCampaign) -> dict[str, AtomicClaim]:
    graph = source_campaign.direction_gate.novelty_decision.coverage.claim_graph_bundle.graph
    return {item.claim_sha256: item for item in graph.claims}


def _expanded_edges(
    contract: CausalContract,
    graph: HypothesisCausalGraph,
) -> list[tuple[str, str]]:
    edges = [(item.source_variable_id, item.target_variable_id) for item in graph.edges]
    edges.extend(
        (item.variable_id, target)
        for item in graph.latent_confounders
        for target in item.affected_variable_ids
    )
    edges.extend(
        (item.construct_variable_id, item.indicator_variable_id)
        for item in contract.measurement_processes
    )
    edges.extend(
        (parent, item.selection_variable_id)
        for item in contract.selection_mechanisms
        for parent in item.parent_variable_ids
    )
    return edges


def _shortest_directed_path(
    source: str,
    target: str,
    edges: list[tuple[str, str]],
) -> tuple[str, ...]:
    children: dict[str, set[str]] = {}
    for left, right in edges:
        children.setdefault(left, set()).add(right)
    queue = deque([(source, (source,))])
    visited = {source}
    while queue:
        node, path = queue.popleft()
        if node == target:
            return path
        for child in sorted(children.get(node, ())):
            if child not in visited:
                visited.add(child)
                queue.append((child, (*path, child)))
    return ()


def _cycle_path(nodes: set[str], edges: list[tuple[str, str]]) -> tuple[str, ...]:
    children: dict[str, list[str]] = {item: [] for item in nodes}
    for left, right in edges:
        if left in nodes and right in nodes:
            children[left].append(right)
    for values in children.values():
        values.sort()
    state: dict[str, int] = {item: 0 for item in nodes}
    stack: list[str] = []
    positions: dict[str, int] = {}

    def visit(node: str) -> tuple[str, ...]:
        state[node] = 1
        positions[node] = len(stack)
        stack.append(node)
        for child in children[node]:
            if state[child] == 0:
                cycle = visit(child)
                if cycle:
                    return cycle
            elif state[child] == 1:
                return (*stack[positions[child] :], child)
        stack.pop()
        positions.pop(node)
        state[node] = 2
        return ()

    for node in sorted(nodes):
        if state[node] == 0:
            cycle = visit(node)
            if cycle:
                return cycle
    return ()


def _descendants(source: str, edges: list[tuple[str, str]]) -> set[str]:
    children: dict[str, set[str]] = {}
    for left, right in edges:
        children.setdefault(left, set()).add(right)
    result: set[str] = set()
    frontier = list(children.get(source, ()))
    while frontier:
        node = frontier.pop()
        if node in result:
            continue
        result.add(node)
        frontier.extend(children.get(node, ()))
    return result


def _open_backdoor_path(
    *,
    nodes: set[str],
    edges: list[tuple[str, str]],
    exposure: str,
    outcome: str,
    conditioned: set[str],
) -> tuple[str, ...]:
    # Pearl's back-door graph removes arrows emanating from the exposure.  D-separation in a DAG is
    # then tested through the ancestral moral graph of exposure, outcome, and conditioned nodes.
    mutilated = [(left, right) for left, right in edges if left != exposure]
    parents: dict[str, set[str]] = {item: set() for item in nodes}
    for left, right in mutilated:
        if left in nodes and right in nodes:
            parents[right].add(left)
    ancestors = {exposure, outcome, *conditioned}
    frontier = list(ancestors)
    while frontier:
        node = frontier.pop()
        for parent in parents.get(node, ()):
            if parent not in ancestors:
                ancestors.add(parent)
                frontier.append(parent)
    undirected: dict[str, set[str]] = {item: set() for item in ancestors}
    for left, right in mutilated:
        if left in ancestors and right in ancestors:
            undirected[left].add(right)
            undirected[right].add(left)
    for child in ancestors:
        child_parents = sorted(parents.get(child, ()) & ancestors)
        for index, left in enumerate(child_parents):
            for right in child_parents[index + 1 :]:
                undirected[left].add(right)
                undirected[right].add(left)
    remaining = ancestors - conditioned
    if exposure not in remaining or outcome not in remaining:
        return ()
    queue = deque([(exposure, (exposure,))])
    visited = {exposure}
    while queue:
        node, path = queue.popleft()
        if node == outcome:
            return path
        for neighbor in sorted(undirected[node] & remaining):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, (*path, neighbor)))
    return ()


def _assumption_applies(
    assumption: IdentificationAssumption,
    *,
    hypothesis_id: str,
) -> bool:
    return hypothesis_id in assumption.applies_to_hypothesis_ids


def _derive_static_audits(
    *,
    source_campaign: HypothesisGenerationCampaign,
    request: CausalContractRequest,
    policy: CausalAuditPolicy,
    contract: CausalContract,
) -> tuple[tuple[HypothesisGraphAudit, ...], tuple[str, ...]]:
    snapshot = source_campaign.world_model_snapshot
    assert snapshot is not None
    hypotheses = {item.hypothesis_id: item for item in snapshot.hypotheses}
    bindings = {item.hypothesis_id: item for item in request.hypothesis_bindings}
    variables = {item.variable_id: item for item in contract.variables}
    assumptions = {item.assumption_id: item for item in contract.assumptions}
    measurements = {item.process_id: item for item in contract.measurement_processes}
    known_claims = _known_claims(source_campaign)
    known_claim_ids = set(known_claims)
    candidate_sha256 = source_campaign.request.candidate_claim_sha256
    linked_prior_claims = {
        item.relation.prior_claim_sha256
        for item in source_campaign.direction_gate.novelty_decision.coverage.prior_art_resolution.accepted
        if item.relation.candidate_claim_sha256 == candidate_sha256
    }
    blockers: list[str] = []
    if contract.world_model_snapshot_sha256 != snapshot.snapshot_sha256:
        blockers.append("contract_changed_world_model_snapshot")
    if contract.question_sha256 != snapshot.question.question_sha256:
        blockers.append("contract_changed_question_version")
    if contract.estimand.proposed_evidence_kind is not request.proposed_evidence_kind:
        blockers.append("contract_changed_proposed_evidence_kind")
    if contract.estimand.identification_strategy is not policy.supported_identification_strategy:
        blockers.append(
            f"unsupported_identification_strategy:{contract.estimand.identification_strategy.value}"
        )
    graph_ids = {item.hypothesis_id for item in contract.hypothesis_graphs}
    if graph_ids != set(hypotheses):
        blockers.append("causal_graphs_do_not_cover_exact_hypothesis_set")
    for graph in contract.hypothesis_graphs:
        binding = bindings.get(graph.hypothesis_id)
        if binding is None or graph.hypothesis_version_sha256 != binding.hypothesis_version_sha256:
            blockers.append(f"causal_graph_changed_hypothesis_version:{graph.hypothesis_id}")

    exposure = variables.get(contract.estimand.exposure_variable_id)
    outcome = variables.get(contract.estimand.outcome_variable_id)
    if exposure is None:
        blockers.append("undefined_estimand_exposure")
    if outcome is None:
        blockers.append("undefined_estimand_outcome")
    if exposure is not None:
        if CausalVariableRole.EXPOSURE not in exposure.roles:
            blockers.append("estimand_exposure_lacks_exposure_role")
        if exposure.observability is CausalObservability.LATENT:
            blockers.append("estimand_exposure_is_latent")
        if (
            request.proposed_evidence_kind
            in {
                CausalEvidenceKind.CONTROLLED_INTERVENTION,
                CausalEvidenceKind.SIMULATION_INTERVENTION,
            }
            and exposure.intervenability is not CausalIntervenability.DIRECT
        ):
            blockers.append("proposed_intervention_exposure_is_not_directly_intervenable")
    if outcome is not None and CausalVariableRole.OUTCOME not in outcome.roles:
        blockers.append("estimand_outcome_lacks_outcome_role")

    for variable in contract.variables:
        if set(variable.grounding_claim_sha256s) - known_claim_ids:
            blockers.append(f"unknown_variable_grounding:{variable.variable_id}")
    for assumption in contract.assumptions:
        if set(assumption.applies_to_hypothesis_ids) - set(hypotheses):
            blockers.append(f"assumption_unknown_hypothesis:{assumption.assumption_id}")
        if set(assumption.variable_ids) - set(variables):
            blockers.append(f"assumption_unknown_variable:{assumption.assumption_id}")
        if set(assumption.grounding_claim_sha256s) - known_claim_ids:
            blockers.append(f"unknown_assumption_grounding:{assumption.assumption_id}")

    outcome_measurement = measurements.get(contract.outcome_measurement_process_id)
    if outcome_measurement is None:
        blockers.append("undefined_outcome_measurement_process")
    for process in contract.measurement_processes:
        construct = variables.get(process.construct_variable_id)
        indicator = variables.get(process.indicator_variable_id)
        assumption = assumptions.get(process.validity_assumption_id)
        if construct is None or indicator is None:
            blockers.append(f"measurement_process_undefined_variable:{process.process_id}")
            continue
        if (
            indicator.observability is not CausalObservability.OBSERVED
            or CausalVariableRole.MEASUREMENT not in indicator.roles
            or indicator.measurement_protocol_sha256 != process.measurement_protocol_sha256
        ):
            blockers.append(f"measurement_process_invalid_indicator:{process.process_id}")
        if (
            assumption is None
            or assumption.kind is not IdentificationAssumptionKind.MEASUREMENT_VALIDITY
            or process.construct_variable_id not in assumption.variable_ids
            or process.indicator_variable_id not in assumption.variable_ids
        ):
            blockers.append(f"measurement_process_missing_validity_assumption:{process.process_id}")
    if outcome_measurement is not None and outcome is not None:
        if outcome_measurement.construct_variable_id != outcome.variable_id:
            blockers.append("outcome_measurement_does_not_measure_estimand_outcome")
        indicator = variables.get(outcome_measurement.indicator_variable_id)
        if indicator is not None and indicator.observable_id is not None:
            for hypothesis in snapshot.hypotheses:
                if not any(
                    prediction.hypothesis_id == hypothesis.hypothesis_id
                    and prediction.observable_id == indicator.observable_id
                    and prediction.measurement_protocol_sha256
                    == outcome_measurement.measurement_protocol_sha256
                    for prediction in snapshot.predictions
                ):
                    blockers.append(
                        f"outcome_measurement_not_bound_to_prediction:{hypothesis.hypothesis_id}"
                    )

    for mechanism in contract.selection_mechanisms:
        selection = variables.get(mechanism.selection_variable_id)
        assumption = assumptions.get(mechanism.exchangeability_assumption_id)
        if selection is None or set(mechanism.parent_variable_ids) - set(variables):
            blockers.append(f"selection_mechanism_undefined_variable:{mechanism.mechanism_id}")
            continue
        if CausalVariableRole.SELECTION not in selection.roles:
            blockers.append(f"selection_variable_lacks_selection_role:{mechanism.mechanism_id}")
        if (
            assumption is None
            or assumption.kind is not IdentificationAssumptionKind.SELECTION_EXCHANGEABILITY
            or mechanism.selection_variable_id not in assumption.variable_ids
        ):
            blockers.append(f"selection_mechanism_missing_assumption:{mechanism.mechanism_id}")

    required_kinds = {
        IdentificationAssumptionKind.CONSISTENCY,
        IdentificationAssumptionKind.POSITIVITY,
        IdentificationAssumptionKind.EXCHANGEABILITY,
        IdentificationAssumptionKind.NO_INTERFERENCE,
        IdentificationAssumptionKind.TEMPORAL_ORDER,
        IdentificationAssumptionKind.MEASUREMENT_VALIDITY,
    }
    if request.proposed_evidence_kind is CausalEvidenceKind.SIMULATION_INTERVENTION:
        required_kinds.add(IdentificationAssumptionKind.MODEL_CORRECTNESS)
    for hypothesis_id in sorted(hypotheses):
        present = {
            assumption.kind
            for assumption in contract.assumptions
            if _assumption_applies(assumption, hypothesis_id=hypothesis_id)
        }
        for kind in sorted(required_kinds - present, key=lambda item: item.value):
            blockers.append(f"missing_identification_assumption:{hypothesis_id}:{kind.value}")

    audits: list[HypothesisGraphAudit] = []
    adjustment = contract.estimand.adjustment_variable_ids
    for graph in contract.hypothesis_graphs:
        graph_blockers: list[str] = []
        hypothesis = hypotheses.get(graph.hypothesis_id)
        if set(graph.grounding_claim_sha256s) - known_claim_ids:
            graph_blockers.append(f"unknown_graph_grounding:{graph.hypothesis_id}")
        if hypothesis is not None:
            if hypothesis.role in {HypothesisRole.NULL, HypothesisRole.PRIMARY} and (
                candidate_sha256 not in graph.grounding_claim_sha256s
            ):
                graph_blockers.append(f"candidate_grounding_missing:{graph.hypothesis_id}")
            if hypothesis.role is HypothesisRole.ALTERNATIVE and not (
                set(graph.grounding_claim_sha256s) & linked_prior_claims
            ):
                graph_blockers.append(f"alternative_prior_grounding_missing:{graph.hypothesis_id}")
        directed_pairs: list[tuple[str, str]] = []
        for edge in graph.edges:
            if edge.source_variable_id not in variables or edge.target_variable_id not in variables:
                graph_blockers.append(f"edge_undefined_variable:{graph.hypothesis_id}:{edge.edge_id}")
            if set(edge.assumption_ids) - set(assumptions):
                graph_blockers.append(f"edge_unknown_assumption:{graph.hypothesis_id}:{edge.edge_id}")
            elif any(
                graph.hypothesis_id not in assumptions[item].applies_to_hypothesis_ids
                for item in edge.assumption_ids
            ):
                graph_blockers.append(
                    f"edge_assumption_wrong_hypothesis:{graph.hypothesis_id}:{edge.edge_id}"
                )
            if set(edge.grounding_claim_sha256s) - known_claim_ids:
                graph_blockers.append(f"edge_unknown_grounding:{graph.hypothesis_id}:{edge.edge_id}")
            directed_pairs.append((edge.source_variable_id, edge.target_variable_id))
        for confounder in graph.latent_confounders:
            variable = variables.get(confounder.variable_id)
            assumption = assumptions.get(confounder.assumption_id)
            if variable is None or set(confounder.affected_variable_ids) - set(variables):
                graph_blockers.append(
                    f"latent_confounder_undefined_variable:{graph.hypothesis_id}:{confounder.confounder_id}"
                )
                continue
            if (
                variable.observability is not CausalObservability.LATENT
                or CausalVariableRole.CONFOUNDER not in variable.roles
            ):
                graph_blockers.append(
                    f"latent_confounder_not_latent:{graph.hypothesis_id}:{confounder.confounder_id}"
                )
            if (
                assumption is None
                or assumption.kind is not IdentificationAssumptionKind.EXCHANGEABILITY
                or graph.hypothesis_id not in assumption.applies_to_hypothesis_ids
            ):
                graph_blockers.append(
                    f"latent_confounder_missing_assumption:{graph.hypothesis_id}:{confounder.confounder_id}"
                )
            if set(confounder.grounding_claim_sha256s) - known_claim_ids:
                graph_blockers.append(
                    f"latent_confounder_unknown_grounding:{graph.hypothesis_id}:{confounder.confounder_id}"
                )
        edges = _expanded_edges(contract, graph)
        if len(edges) != len(set(edges)):
            graph_blockers.append(f"duplicate_directed_relation:{graph.hypothesis_id}")
        cycle = _cycle_path(set(variables), edges)
        if cycle:
            graph_blockers.append(f"causal_cycle:{graph.hypothesis_id}:{':'.join(cycle)}")
        causal_path = ()
        if exposure is not None and outcome is not None and not cycle:
            causal_path = _shortest_directed_path(exposure.variable_id, outcome.variable_id, edges)
            if hypothesis is not None:
                if hypothesis.role is HypothesisRole.NULL and causal_path:
                    graph_blockers.append(f"null_contains_causal_effect_path:{graph.hypothesis_id}")
                if hypothesis.role is not HypothesisRole.NULL and not causal_path:
                    graph_blockers.append(f"mechanism_lacks_causal_effect_path:{graph.hypothesis_id}")

        backdoor_status = BackdoorAuditStatus.INVALID_GRAPH
        open_path: tuple[str, ...] = ()
        if contract.estimand.identification_strategy is not IdentificationStrategy.BACKDOOR_ADJUSTMENT:
            backdoor_status = BackdoorAuditStatus.UNSUPPORTED_STRATEGY
        elif not graph_blockers and exposure is not None and outcome is not None:
            invalid_adjustment = False
            descendants = _descendants(exposure.variable_id, edges)
            for variable_id in adjustment:
                variable = variables.get(variable_id)
                if (
                    variable is None
                    or variable_id in {exposure.variable_id, outcome.variable_id}
                    or variable.observability is not CausalObservability.OBSERVED
                    or variable_id in descendants
                    or CausalVariableRole.SELECTION in variable.roles
                    or CausalVariableRole.MEASUREMENT in variable.roles
                ):
                    graph_blockers.append(
                        f"invalid_adjustment_variable:{graph.hypothesis_id}:{variable_id}"
                    )
                    invalid_adjustment = True
            if invalid_adjustment:
                backdoor_status = BackdoorAuditStatus.INVALID_ADJUSTMENT
            elif any(
                item.analysis_conditions_on_selection
                for item in contract.selection_mechanisms
            ):
                # Selection recoverability needs a selection diagram/transport argument.  Treating
                # selection as an ordinary adjustment variable would overclaim the back-door test.
                backdoor_status = BackdoorAuditStatus.SELECTION_RECOVERABILITY_UNSUPPORTED
            else:
                conditioned = set(adjustment)
                open_path = _open_backdoor_path(
                    nodes=set(variables),
                    edges=edges,
                    exposure=exposure.variable_id,
                    outcome=outcome.variable_id,
                    conditioned=conditioned,
                )
                backdoor_status = (
                    BackdoorAuditStatus.OPEN_BACKDOOR_PATH
                    if open_path
                    else BackdoorAuditStatus.IDENTIFIED
                )
        blockers.extend(graph_blockers)
        audits.append(
            HypothesisGraphAudit(
                hypothesis_id=graph.hypothesis_id,
                hypothesis_version_sha256=graph.hypothesis_version_sha256,
                graph_sha256=graph.graph_sha256,
                directed_exposure_outcome_path=causal_path,
                open_backdoor_path=open_path,
                adjustment_variable_ids=adjustment,
                backdoor_status=backdoor_status,
                blockers=tuple(dict.fromkeys(graph_blockers)),
            )
        )
    return tuple(audits), tuple(dict.fromkeys(blockers))


def _validate_review_batch(
    *,
    batch: CausalAssumptionReviewBatch,
    contract_batch: CausalContractBatch,
    manifest: CausalAssumptionReviewerManifest,
    request: CausalContractRequest,
    received_at: datetime | None = None,
) -> None:
    if (
        batch.causal_contract_batch_sha256 != contract_batch.batch_sha256
        or batch.reviewer_manifest_sha256 != manifest.manifest_sha256
    ):
        raise ValueError("causal-assumption review is bound to another contract/reviewer")
    if batch.completed_at < contract_batch.completed_at:
        raise ValueError("causal-assumption review predates its contract")
    if received_at is not None and batch.completed_at > received_at:
        raise ValueError("causal-assumption review claims a future completion time")
    assumptions = {
        item.assumption_id: item for item in contract_batch.contract.assumptions
    }
    actual = [item.assumption_id for item in batch.reviews]
    if actual != sorted(assumptions):
        raise ValueError("reviewer must adjudicate every identification assumption exactly once")
    known_claims = set(request.input_claim_sha256s)
    for review in batch.reviews:
        assumption = assumptions[review.assumption_id]
        if review.assumption_sha256 != assumption.assumption_sha256:
            raise ValueError("causal review changed an identification assumption identity")
        if review.completed_at < contract_batch.completed_at:
            raise ValueError("causal-assumption review predates its contract")
        if set(review.evidence_claim_sha256s) - known_claims:
            raise ValueError("causal-assumption review cites evidence outside exact F8 claims")
        if set(review.evidence_claim_sha256s) - set(assumption.grounding_claim_sha256s):
            raise ValueError("causal-assumption review changed its frozen evidence closure")


def _base_claim_ceiling(kind: CausalEvidenceKind) -> CausalClaimCeiling:
    if kind in {CausalEvidenceKind.DESCRIPTIVE, CausalEvidenceKind.MEASUREMENT_VALIDATION}:
        return CausalClaimCeiling.DESCRIPTIVE_ONLY
    if kind is CausalEvidenceKind.OBSERVATIONAL_ASSOCIATION:
        return CausalClaimCeiling.ASSOCIATION_ONLY
    if kind is CausalEvidenceKind.SIMULATION_INTERVENTION:
        return CausalClaimCeiling.WITHIN_MODEL_CAUSAL_ONLY
    return CausalClaimCeiling.CAUSAL_CANDIDATE


def _derive_reviewed_outputs(
    *,
    request: CausalContractRequest,
    policy: CausalAuditPolicy,
    contract: CausalContract,
    review_batch: CausalAssumptionReviewBatch,
    graph_audits: tuple[HypothesisGraphAudit, ...],
) -> _DerivedCausalOutputs:
    assumptions = {item.assumption_id: item for item in contract.assumptions}
    resolutions: list[CausalAssumptionResolution] = []
    blockers: list[str] = []
    for review in review_batch.reviews:
        if review.confidence < policy.minimum_review_confidence:
            status = AssumptionResolutionStatus.LOW_CONFIDENCE
            blockers.append(f"low_confidence_assumption:{review.assumption_id}")
        elif review.decision is AssumptionReviewDecision.ACCEPT:
            status = AssumptionResolutionStatus.ACCEPTED
        elif review.decision is AssumptionReviewDecision.REJECT:
            status = AssumptionResolutionStatus.REJECTED
            blockers.append(f"rejected_identification_assumption:{review.assumption_id}")
        else:
            status = AssumptionResolutionStatus.UNRESOLVED
            blockers.append(f"unresolved_identification_assumption:{review.assumption_id}")
        resolutions.append(
            CausalAssumptionResolution(
                assumption_id=review.assumption_id,
                assumption_sha256=assumptions[review.assumption_id].assumption_sha256,
                review_sha256=review.review_sha256,
                status=status,
            )
        )
    rejected = any(item.status is AssumptionResolutionStatus.REJECTED for item in resolutions)
    unresolved = any(
        item.status
        in {AssumptionResolutionStatus.UNRESOLVED, AssumptionResolutionStatus.LOW_CONFIDENCE}
        for item in resolutions
    )
    graph_identified = all(
        item.backdoor_status is BackdoorAuditStatus.IDENTIFIED for item in graph_audits
    )
    if rejected:
        disposition = CausalAuditDisposition.BLOCKED_ASSUMPTIONS
        ceiling = CausalClaimCeiling.NONE
        prediction_authorized = False
    elif unresolved or not graph_identified:
        disposition = CausalAuditDisposition.READY_BOUNDED
        base = _base_claim_ceiling(request.proposed_evidence_kind)
        ceiling = (
            CausalClaimCeiling.DESCRIPTIVE_ONLY
            if base is CausalClaimCeiling.DESCRIPTIVE_ONLY
            else CausalClaimCeiling.ASSOCIATION_ONLY
        )
        prediction_authorized = True
        if not graph_identified:
            blockers.extend(
                f"backdoor_not_identified:{item.hypothesis_id}:{item.backdoor_status.value}"
                for item in graph_audits
                if item.backdoor_status is not BackdoorAuditStatus.IDENTIFIED
            )
    else:
        disposition = CausalAuditDisposition.READY_IDENTIFIED
        ceiling = _base_claim_ceiling(request.proposed_evidence_kind)
        prediction_authorized = True
    return _DerivedCausalOutputs(
        graph_audits=graph_audits,
        assumption_resolutions=tuple(resolutions),
        blockers=tuple(dict.fromkeys(blockers)),
        disposition=disposition,
        claim_ceiling=ceiling,
        prediction_planning_authorized=prediction_authorized,
    )


def _now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("causal-audit clock must return a timezone-aware datetime")
    return value


def _opaque_output_sha256(value: object) -> str:
    try:
        return content_sha256(value)
    except Exception:  # noqa: BLE001 - opaque fallback is hashed and never retained
        return hashlib.sha256(repr(value).encode("utf-8", errors="replace")).hexdigest()


def _failure(
    *,
    kind: CausalAuditFailureKind,
    error: Exception,
    occurred_at: datetime,
    raw_output: object | None = None,
) -> CausalAuditFailure:
    detail = f"{type(error).__module__}.{type(error).__qualname__}:{error}"
    return CausalAuditFailure(
        kind=kind,
        error_class=type(error).__name__,
        error_detail_sha256=hashlib.sha256(detail.encode("utf-8")).hexdigest(),
        raw_output_sha256=None if raw_output is None else _opaque_output_sha256(raw_output),
        occurred_at=occurred_at,
    )


async def run_causal_identification_audit(
    *,
    campaign_id: str,
    source_campaign: HypothesisGenerationCampaign,
    policy: CausalAuditPolicy,
    request: CausalContractRequest,
    author: CausalContractAuthorAdapter,
    reviewer: CausalAssumptionReviewerAdapter,
    clock: Callable[[], datetime] | None = None,
) -> CausalAuditCampaign:
    """Run proposal, structural audit, and complete independent assumption review."""

    clock = clock or (lambda: datetime.now(timezone.utc))
    if author.manifest.manifest_sha256 != request.author_manifest_sha256:
        raise ValueError("runtime causal author differs from the frozen request")
    if reviewer.manifest.manifest_sha256 != request.reviewer_manifest_sha256:
        raise ValueError("runtime causal reviewer differs from the frozen request")
    _validate_request_bindings(
        source_campaign=source_campaign,
        policy=policy,
        author_manifest=author.manifest,
        reviewer_manifest=reviewer.manifest,
        request=request,
    )
    claims = tuple(_known_claims(source_campaign).values())
    try:
        raw_contract = await author.author(
            request=request,
            source_campaign=source_campaign,
            claims=claims,
        )
    except Exception as exc:  # noqa: BLE001 - explicit sanitized failure artifact
        failure = _failure(
            kind=CausalAuditFailureKind.AUTHOR_ERROR,
            error=exc,
            occurred_at=_now(clock),
        )
        return CausalAuditCampaign(
            campaign_id=campaign_id,
            source_campaign=source_campaign,
            policy=policy,
            author_manifest=author.manifest,
            reviewer_manifest=reviewer.manifest,
            request=request,
            failure=failure,
            graph_audits=(),
            assumption_resolutions=(),
            blockers=(f"execution_failure:{failure.kind.value}",),
            disposition=CausalAuditDisposition.BLOCKED_EXECUTION,
            claim_ceiling=CausalClaimCeiling.NONE,
            prediction_planning_authorized=False,
            generated_at=_now(clock),
        )
    contract_received_at = _now(clock)
    try:
        contract_batch = (
            raw_contract
            if isinstance(raw_contract, CausalContractBatch)
            else CausalContractBatch.model_validate(raw_contract)
        )
        _validate_contract_batch(
            batch=contract_batch,
            request=request,
            manifest=author.manifest,
            policy=policy,
            received_at=contract_received_at,
        )
    except (ValidationError, ValueError, TypeError) as exc:
        failure = _failure(
            kind=CausalAuditFailureKind.AUTHOR_OUTPUT_INVALID,
            error=exc,
            occurred_at=contract_received_at,
            raw_output=raw_contract,
        )
        return CausalAuditCampaign(
            campaign_id=campaign_id,
            source_campaign=source_campaign,
            policy=policy,
            author_manifest=author.manifest,
            reviewer_manifest=reviewer.manifest,
            request=request,
            failure=failure,
            graph_audits=(),
            assumption_resolutions=(),
            blockers=(f"execution_failure:{failure.kind.value}",),
            disposition=CausalAuditDisposition.BLOCKED_EXECUTION,
            claim_ceiling=CausalClaimCeiling.NONE,
            prediction_planning_authorized=False,
            generated_at=_now(clock),
        )
    graph_audits, structural_blockers = _derive_static_audits(
        source_campaign=source_campaign,
        request=request,
        policy=policy,
        contract=contract_batch.contract,
    )
    if structural_blockers:
        return CausalAuditCampaign(
            campaign_id=campaign_id,
            source_campaign=source_campaign,
            policy=policy,
            author_manifest=author.manifest,
            reviewer_manifest=reviewer.manifest,
            request=request,
            contract_batch=contract_batch,
            graph_audits=graph_audits,
            assumption_resolutions=(),
            blockers=structural_blockers,
            disposition=CausalAuditDisposition.BLOCKED_STRUCTURE,
            claim_ceiling=CausalClaimCeiling.NONE,
            prediction_planning_authorized=False,
            generated_at=_now(clock),
        )
    try:
        raw_review = await reviewer.review(
            contract_batch=contract_batch,
            graph_audits=graph_audits,
        )
    except Exception as exc:  # noqa: BLE001 - explicit sanitized failure artifact
        failure = _failure(
            kind=CausalAuditFailureKind.REVIEWER_ERROR,
            error=exc,
            occurred_at=_now(clock),
        )
        return CausalAuditCampaign(
            campaign_id=campaign_id,
            source_campaign=source_campaign,
            policy=policy,
            author_manifest=author.manifest,
            reviewer_manifest=reviewer.manifest,
            request=request,
            contract_batch=contract_batch,
            failure=failure,
            graph_audits=graph_audits,
            assumption_resolutions=(),
            blockers=(f"execution_failure:{failure.kind.value}",),
            disposition=CausalAuditDisposition.BLOCKED_EXECUTION,
            claim_ceiling=CausalClaimCeiling.NONE,
            prediction_planning_authorized=False,
            generated_at=_now(clock),
        )
    review_received_at = _now(clock)
    try:
        review_batch = (
            raw_review
            if isinstance(raw_review, CausalAssumptionReviewBatch)
            else CausalAssumptionReviewBatch.model_validate(raw_review)
        )
        _validate_review_batch(
            batch=review_batch,
            contract_batch=contract_batch,
            manifest=reviewer.manifest,
            request=request,
            received_at=review_received_at,
        )
    except (ValidationError, ValueError, TypeError) as exc:
        failure = _failure(
            kind=CausalAuditFailureKind.REVIEWER_OUTPUT_INVALID,
            error=exc,
            occurred_at=review_received_at,
            raw_output=raw_review,
        )
        return CausalAuditCampaign(
            campaign_id=campaign_id,
            source_campaign=source_campaign,
            policy=policy,
            author_manifest=author.manifest,
            reviewer_manifest=reviewer.manifest,
            request=request,
            contract_batch=contract_batch,
            failure=failure,
            graph_audits=graph_audits,
            assumption_resolutions=(),
            blockers=(f"execution_failure:{failure.kind.value}",),
            disposition=CausalAuditDisposition.BLOCKED_EXECUTION,
            claim_ceiling=CausalClaimCeiling.NONE,
            prediction_planning_authorized=False,
            generated_at=_now(clock),
        )
    derived = _derive_reviewed_outputs(
        request=request,
        policy=policy,
        contract=contract_batch.contract,
        review_batch=review_batch,
        graph_audits=graph_audits,
    )
    return CausalAuditCampaign(
        campaign_id=campaign_id,
        source_campaign=source_campaign,
        policy=policy,
        author_manifest=author.manifest,
        reviewer_manifest=reviewer.manifest,
        request=request,
        contract_batch=contract_batch,
        review_batch=review_batch,
        graph_audits=derived.graph_audits,
        assumption_resolutions=derived.assumption_resolutions,
        blockers=derived.blockers,
        disposition=derived.disposition,
        claim_ceiling=derived.claim_ceiling,
        prediction_planning_authorized=derived.prediction_planning_authorized,
        generated_at=_now(clock),
    )


def commit_causal_audit_campaign(
    *,
    archive: ContentAddressedResponseArchive,
    campaign: CausalAuditCampaign,
) -> CommittedCausalAuditCampaign:
    ledger = archive.store_ledger(
        value=campaign,
        object_sha256=campaign.campaign_sha256,
        archived_at=campaign.generated_at,
    )
    return CommittedCausalAuditCampaign(campaign=campaign, ledger=ledger)


def load_causal_audit_campaign(
    *,
    archive: ContentAddressedResponseArchive,
    ledger: ArchivedKnowledgeLedger,
) -> CausalAuditCampaign:
    payload = archive.read_ledger(ledger)
    campaign = CausalAuditCampaign.model_validate_json(payload)
    if canonical_json_bytes(campaign) != payload:
        raise ValueError("archived causal-audit campaign is not canonical JSON")
    if campaign.campaign_sha256 != ledger.object_sha256:
        raise ValueError("archived causal-audit campaign changed object identity")
    return campaign


__all__ = [
    "CAUSAL_CONTRACT_OUTPUT_SCHEMA_SHA256",
    "CAUSAL_REVIEW_OUTPUT_SCHEMA_SHA256",
    "AssumptionResolutionStatus",
    "AssumptionReviewDecision",
    "BackdoorAuditStatus",
    "CausalAdapterRuntime",
    "CausalAssumptionResolution",
    "CausalAssumptionReview",
    "CausalAssumptionReviewBatch",
    "CausalAssumptionReviewerAdapter",
    "CausalAssumptionReviewerManifest",
    "CausalAuditCampaign",
    "CausalAuditDisposition",
    "CausalAuditFailure",
    "CausalAuditFailureKind",
    "CausalAuditPolicy",
    "CausalClaimCeiling",
    "CausalContract",
    "CausalContractAuthorAdapter",
    "CausalContractAuthorManifest",
    "CausalContractBatch",
    "CausalContractRequest",
    "CausalEdge",
    "CausalEffectScale",
    "CausalEstimand",
    "CausalEvidenceKind",
    "CausalHypothesisBinding",
    "CausalIntervenability",
    "CausalObservability",
    "CausalValueKind",
    "CausalVariable",
    "CausalVariableRole",
    "CommittedCausalAuditCampaign",
    "HypothesisCausalGraph",
    "HypothesisGraphAudit",
    "IdentificationAssumption",
    "IdentificationAssumptionKind",
    "IdentificationStrategy",
    "LatentConfounder",
    "MeasurementProcess",
    "SelectionMechanism",
    "build_causal_contract_request",
    "commit_causal_audit_campaign",
    "load_causal_audit_campaign",
    "run_causal_identification_audit",
]
