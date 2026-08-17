"""Frozen contracts for observation-blind, shadow-only research portfolio planning.

The proposer is allowed to name actions and explain them.  It is deliberately not allowed to
provide a total score.  Independent assessments provide frozen, provenance-carrying inputs and the
harness derives hard blockers, expected information gain, costs, replication-debt reduction, and
the final batch ranking.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from aletheia.jobs.outbox import ScientificCommandReceipt
from aletheia.programs.schemas import BudgetKind, DataRole, QuestGraphSnapshot
from aletheia.reproducibility.manifest import content_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_QUEST_ID_PATTERN = r"^qst_[0-9a-f]{32}$"
_NODE_ID_PATTERN = r"^(qst|prg|cmp)_[0-9a-f]{32}$"
_PROGRAM_ID_PATTERN = r"^prg_[0-9a-f]{32}$"
_FAMILY_ID_PATTERN = r"^fam_[0-9a-f]{32}$"
_SLATE_ID_PATTERN = r"^psl_[0-9a-f]{32}$"
_CANDIDATE_ID_PATTERN = r"^pca_[0-9a-f]{32}$"
_HUMAN_PLAN_ID_PATTERN = r"^php_[0-9a-f]{32}$"
_EPOCH_ID_PATTERN = r"^pep_[0-9a-f]{32}$"
_CONTEXT_RECEIPT_ID_PATTERN = r"^mctx_[0-9a-f]{32}$"
_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"
_LOCAL_ID_PATTERN = r"^[a-z][a-z0-9_.-]{0,79}$"
_PPM = 1_000_000


class FrozenPortfolioModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PortfolioActionType(str, Enum):
    ADVANCE_CAMPAIGN = "advance_campaign"
    DISCRIMINATING_EXPERIMENT = "discriminating_experiment"
    REPLICATION = "replication"
    MECHANISM_TEST = "mechanism_test"
    ACQUIRE_DATA = "acquire_data"
    REPAIR_CAPABILITY = "repair_capability"
    START_CAMPAIGN = "start_campaign"
    PAUSE_PROGRAM = "pause_program"
    STOP_PROGRAM = "stop_program"


CAMPAIGN_ACTIONS = frozenset(
    {
        PortfolioActionType.ADVANCE_CAMPAIGN,
        PortfolioActionType.DISCRIMINATING_EXPERIMENT,
        PortfolioActionType.REPLICATION,
        PortfolioActionType.MECHANISM_TEST,
    }
)
PROGRAM_ACTIONS = frozenset(
    {
        PortfolioActionType.REPAIR_CAPABILITY,
        PortfolioActionType.START_CAMPAIGN,
        PortfolioActionType.PAUSE_PROGRAM,
        PortfolioActionType.STOP_PROGRAM,
    }
)
INFORMATION_ACTIONS = frozenset(
    {
        PortfolioActionType.ADVANCE_CAMPAIGN,
        PortfolioActionType.DISCRIMINATING_EXPERIMENT,
        PortfolioActionType.REPLICATION,
        PortfolioActionType.MECHANISM_TEST,
        PortfolioActionType.ACQUIRE_DATA,
    }
)


class PortfolioRiskLevel(str, Enum):
    NEGLIGIBLE = "negligible"
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    PROHIBITED = "prohibited"


class PortfolioMeasurementStatus(str, Enum):
    VALIDATED = "validated"
    BOUNDED = "bounded"
    UNKNOWN = "unknown"
    INVALID = "invalid"


class PortfolioAssessorKind(str, Enum):
    DETERMINISTIC_HARNESS = "deterministic_harness"
    INDEPENDENT_MODEL = "independent_model"
    INDEPENDENT_HUMAN = "independent_human"


class PortfolioEpochDisposition(str, Enum):
    SHADOW_READY = "shadow_ready"
    NO_FEASIBLE_ACTION = "no_feasible_action"
    POLICY_BLOCKED = "policy_blocked"


class PortfolioActionSpec(FrozenPortfolioModel):
    action_type: PortfolioActionType
    target_node_id: str = Field(pattern=_NODE_ID_PATTERN)
    family_id: str | None = Field(default=None, pattern=_FAMILY_ID_PATTERN)
    task_key: str = Field(pattern=_IDENTITY_PATTERN)
    title: str = Field(min_length=1, max_length=240)
    rationale: str = Field(min_length=1, max_length=4_000)

    @model_validator(mode="after")
    def _target_matches_action(self) -> "PortfolioActionSpec":
        if self.action_type in CAMPAIGN_ACTIONS and not self.target_node_id.startswith("cmp_"):
            raise ValueError("campaign portfolio action requires a Campaign target")
        if self.action_type in PROGRAM_ACTIONS and not self.target_node_id.startswith("prg_"):
            raise ValueError("program portfolio action requires a Program target")
        if (
            self.action_type is PortfolioActionType.ACQUIRE_DATA
            and not self.target_node_id.startswith(("prg_", "cmp_"))
        ):
            raise ValueError("data acquisition requires a Program or Campaign target")
        if self.action_type is PortfolioActionType.START_CAMPAIGN:
            if self.family_id is None:
                raise ValueError("starting a Campaign requires an existing scientific family")
        elif self.family_id is not None and not self.target_node_id.startswith("cmp_"):
            raise ValueError("only Campaign actions or start_campaign may name a family")
        object.__setattr__(self, "title", self.title.strip())
        object.__setattr__(self, "rationale", self.rationale.strip())
        return self

    @property
    def action_sha256(self) -> str:
        return content_sha256(self)

    @property
    def candidate_id(self) -> str:
        return (
            "pca_"
            + content_sha256(
                {
                    "schema": "aletheia.portfolio_action_identity.v1",
                    "action_type": self.action_type.value,
                    "target_node_id": self.target_node_id,
                    "family_id": self.family_id,
                    "task_key": self.task_key,
                    "title": self.title,
                }
            )[:32]
        )


class PortfolioProposal(FrozenPortfolioModel):
    schema_version: Literal[1] = 1
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    graph_sha256: str = Field(pattern=_SHA256_PATTERN)
    memory_context_receipt_id: str = Field(pattern=_CONTEXT_RECEIPT_ID_PATTERN)
    proposer_principal: str = Field(min_length=1, max_length=191)
    proposer_provider: str = Field(min_length=1, max_length=64)
    proposer_model: str = Field(min_length=1, max_length=256)
    proposer_model_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    prompt_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidates: tuple[PortfolioActionSpec, ...] = Field(min_length=2, max_length=64)
    generated_at: AwareDatetime

    @model_validator(mode="after")
    def _candidates_are_canonical(self) -> "PortfolioProposal":
        candidates = tuple(sorted(self.candidates, key=lambda item: item.candidate_id))
        identities = [item.candidate_id for item in candidates]
        if identities != sorted(set(identities)):
            raise ValueError("portfolio proposal candidates must have unique identities")
        object.__setattr__(self, "candidates", candidates)
        return self

    @property
    def proposal_sha256(self) -> str:
        return content_sha256(self)


class PortfolioCostEstimate(FrozenPortfolioModel):
    kind: BudgetKind
    amount_microunits: int = Field(gt=0)


class PortfolioPriorProbability(FrozenPortfolioModel):
    hypothesis_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    probability_ppm: int = Field(ge=0, le=_PPM)


class PortfolioOutcomeProbability(FrozenPortfolioModel):
    outcome_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    probability_ppm: int = Field(ge=0, le=_PPM)


class PortfolioHypothesisLikelihood(FrozenPortfolioModel):
    hypothesis_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    outcomes: tuple[PortfolioOutcomeProbability, ...] = Field(min_length=2, max_length=64)

    @model_validator(mode="after")
    def _outcomes_are_a_distribution(self) -> "PortfolioHypothesisLikelihood":
        outcomes = tuple(sorted(self.outcomes, key=lambda item: item.outcome_id))
        if [item.outcome_id for item in outcomes] != sorted({item.outcome_id for item in outcomes}):
            raise ValueError("likelihood outcomes must be unique")
        if sum(item.probability_ppm for item in outcomes) != _PPM:
            raise ValueError("likelihood outcome probabilities must sum to one million ppm")
        object.__setattr__(self, "outcomes", outcomes)
        return self


class PortfolioInformationModel(FrozenPortfolioModel):
    belief_state_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    priors: tuple[PortfolioPriorProbability, ...] = Field(min_length=2, max_length=64)
    likelihoods: tuple[PortfolioHypothesisLikelihood, ...] = Field(min_length=2, max_length=64)

    @model_validator(mode="after")
    def _model_is_complete_and_canonical(self) -> "PortfolioInformationModel":
        priors = tuple(sorted(self.priors, key=lambda item: item.hypothesis_id))
        likelihoods = tuple(sorted(self.likelihoods, key=lambda item: item.hypothesis_id))
        prior_ids = [item.hypothesis_id for item in priors]
        likelihood_ids = [item.hypothesis_id for item in likelihoods]
        if prior_ids != sorted(set(prior_ids)) or prior_ids != likelihood_ids:
            raise ValueError(
                "information model priors and likelihoods must cover the same hypotheses"
            )
        if sum(item.probability_ppm for item in priors) != _PPM:
            raise ValueError("hypothesis priors must sum to one million ppm")
        outcome_sets = [tuple(item.outcome_id for item in value.outcomes) for value in likelihoods]
        if len(set(outcome_sets)) != 1:
            raise ValueError("every hypothesis likelihood must use the same outcome bins")
        object.__setattr__(self, "priors", priors)
        object.__setattr__(self, "likelihoods", likelihoods)
        return self

    @property
    def information_model_sha256(self) -> str:
        return content_sha256(self)


class PortfolioApprovalReceipt(FrozenPortfolioModel):
    approval_id: str = Field(pattern=_IDENTITY_PATTERN)
    action_sha256: str = Field(pattern=_SHA256_PATTERN)
    approver_principal: str = Field(min_length=1, max_length=191)
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    decision: Literal["approved"] = "approved"
    issued_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def _approval_has_a_window(self) -> "PortfolioApprovalReceipt":
        if self.expires_at <= self.issued_at:
            raise ValueError("portfolio approval must expire after it is issued")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class PortfolioCandidateAssessment(FrozenPortfolioModel):
    candidate_id: str = Field(pattern=_CANDIDATE_ID_PATTERN)
    action_sha256: str = Field(pattern=_SHA256_PATTERN)
    estimated_costs: tuple[PortfolioCostEstimate, ...] = Field(max_length=16)
    estimated_duration_seconds: int = Field(ge=0, le=10 * 365 * 24 * 3600)
    risk_level: PortfolioRiskLevel
    measurement_status: PortfolioMeasurementStatus
    measurement_evidence_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    required_capability_sha256s: tuple[str, ...] = Field(max_length=128)
    available_capability_sha256s: tuple[str, ...] = Field(max_length=128)
    required_data_roles: tuple[DataRole, ...] = Field(max_length=16)
    data_readiness_evidence_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    information_model: PortfolioInformationModel | None = None
    importance_ppm: int = Field(ge=0, le=_PPM)
    novelty_ppm: int = Field(ge=0, le=_PPM)
    success_probability_ppm: int = Field(ge=0, le=_PPM)
    value_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    replication_debt_ledger_sha256: str = Field(pattern=_SHA256_PATTERN)
    replication_debt_before: int = Field(ge=0, le=1_000_000_000)
    expected_replication_debt_reduction: int = Field(ge=0, le=1_000_000_000)
    independent_replication_protocol_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    correlation_tags: tuple[str, ...] = Field(max_length=64)
    diversity_tags: tuple[str, ...] = Field(max_length=64)
    approval: PortfolioApprovalReceipt | None = None
    assessment_evidence_sha256s: tuple[str, ...] = Field(min_length=1, max_length=256)
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def _assessment_is_canonical(self) -> "PortfolioCandidateAssessment":
        costs = tuple(sorted(self.estimated_costs, key=lambda item: item.kind.value))
        if [item.kind for item in costs] != sorted(
            {item.kind for item in costs}, key=lambda item: item.value
        ):
            raise ValueError("portfolio cost estimates must use unique budget kinds")
        object.__setattr__(self, "estimated_costs", costs)
        for field_name in (
            "required_capability_sha256s",
            "available_capability_sha256s",
            "correlation_tags",
            "diversity_tags",
            "assessment_evidence_sha256s",
        ):
            values = tuple(sorted(getattr(self, field_name)))
            if values != tuple(sorted(set(values))):
                raise ValueError(f"portfolio assessment {field_name} must be unique")
            object.__setattr__(self, field_name, values)
        roles = tuple(sorted(self.required_data_roles, key=lambda item: item.value))
        if roles != tuple(sorted(set(roles), key=lambda item: item.value)):
            raise ValueError("portfolio required data roles must be unique")
        object.__setattr__(self, "required_data_roles", roles)
        if self.measurement_status is PortfolioMeasurementStatus.VALIDATED:
            if self.measurement_evidence_sha256 is None:
                raise ValueError("validated portfolio measurement requires evidence")
        if self.required_data_roles and self.data_readiness_evidence_sha256 is None:
            raise ValueError("portfolio data-role requirements need readiness evidence")
        if self.expected_replication_debt_reduction > self.replication_debt_before:
            raise ValueError("replication-debt reduction cannot exceed existing debt")
        if self.expected_replication_debt_reduction > 0:
            if self.independent_replication_protocol_sha256 is None:
                raise ValueError("replication-debt reduction requires an independent protocol")
        elif self.independent_replication_protocol_sha256 is not None:
            raise ValueError("independent replication protocol requires positive debt reduction")
        if self.approval is not None and self.approval.issued_at > self.completed_at:
            raise ValueError("portfolio assessment predates its approval evidence")
        return self

    @property
    def assessment_sha256(self) -> str:
        return content_sha256(self)


PORTFOLIO_ASSESSMENT_OUTPUT_SCHEMA_SHA256 = content_sha256(
    PortfolioCandidateAssessment.model_json_schema()
)


class PortfolioAssessmentManifest(FrozenPortfolioModel):
    assessor_principal: str = Field(min_length=1, max_length=191)
    assessor_kind: PortfolioAssessorKind
    assessor_code_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_schema_sha256: str = Field(pattern=_SHA256_PATTERN)
    model_identity_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    instruction_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    observation_access: Literal["none"] = "none"
    frozen_at: AwareDatetime

    @model_validator(mode="after")
    def _manifest_matches_runtime(self) -> "PortfolioAssessmentManifest":
        if self.output_schema_sha256 != PORTFOLIO_ASSESSMENT_OUTPUT_SCHEMA_SHA256:
            raise ValueError("portfolio assessor uses another output schema")
        model_fields = (
            self.model_identity_sha256 is not None and self.instruction_sha256 is not None
        )
        if self.assessor_kind is PortfolioAssessorKind.INDEPENDENT_MODEL:
            if not model_fields:
                raise ValueError("model portfolio assessor requires frozen model and instructions")
        elif self.model_identity_sha256 is not None or self.instruction_sha256 is not None:
            raise ValueError("non-model portfolio assessor cannot declare model transport")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class PortfolioAssessmentBatch(FrozenPortfolioModel):
    manifest: PortfolioAssessmentManifest
    assessments: tuple[PortfolioCandidateAssessment, ...] = Field(min_length=2, max_length=64)
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def _batch_is_complete_and_canonical(self) -> "PortfolioAssessmentBatch":
        assessments = tuple(sorted(self.assessments, key=lambda item: item.candidate_id))
        identities = [item.candidate_id for item in assessments]
        if identities != sorted(set(identities)):
            raise ValueError("portfolio assessments require unique candidate identities")
        if self.completed_at < max(item.completed_at for item in assessments):
            raise ValueError("portfolio assessment batch predates an assessment")
        if self.completed_at < self.manifest.frozen_at:
            raise ValueError("portfolio assessment batch predates its manifest")
        if any(item.completed_at < self.manifest.frozen_at for item in assessments):
            raise ValueError("portfolio candidate assessment predates its manifest")
        object.__setattr__(self, "assessments", assessments)
        return self

    @property
    def batch_sha256(self) -> str:
        return content_sha256(self)


class PortfolioUtilityWeights(FrozenPortfolioModel):
    expected_information_gain: int = Field(default=250_000, ge=0, le=_PPM)
    importance: int = Field(default=150_000, ge=0, le=_PPM)
    novelty: int = Field(default=100_000, ge=0, le=_PPM)
    success_probability: int = Field(default=100_000, ge=0, le=_PPM)
    replication_debt_reduction: int = Field(default=150_000, ge=0, le=_PPM)
    diversity: int = Field(default=100_000, ge=0, le=_PPM)
    cost_penalty: int = Field(default=75_000, ge=0, le=_PPM)
    duration_penalty: int = Field(default=50_000, ge=0, le=_PPM)
    risk_penalty: int = Field(default=25_000, ge=0, le=_PPM)

    @model_validator(mode="after")
    def _weights_are_normalized(self) -> "PortfolioUtilityWeights":
        if sum(self.model_dump(mode="python").values()) != _PPM:
            raise ValueError("portfolio utility weights must sum to one million ppm")
        return self


class PortfolioSelectionPolicy(FrozenPortfolioModel):
    schema_version: Literal[1] = 1
    policy_id: str = Field(pattern=_IDENTITY_PATTERN)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    mode: Literal["shadow"] = "shadow"
    memory_task_key: str = Field(default="portfolio-plan", pattern=_IDENTITY_PATTERN)
    maximum_selected_actions: int = Field(default=3, ge=1, le=16)
    maximum_actions_per_program: int = Field(default=2, ge=1, le=16)
    maximum_actions_per_family: int = Field(default=1, ge=1, le=16)
    maximum_actions_per_correlation_tag: int = Field(default=1, ge=1, le=16)
    maximum_duration_seconds: int = Field(default=7 * 24 * 3600, gt=0)
    maximum_risk_level: PortfolioRiskLevel = PortfolioRiskLevel.MODERATE
    minimum_expected_information_gain_ratio_ppm: int = Field(default=10_000, ge=0, le=_PPM)
    minimum_replication_actions: int = Field(default=0, ge=0, le=16)
    required_approval_risks: tuple[PortfolioRiskLevel, ...] = (
        PortfolioRiskLevel.MODERATE,
        PortfolioRiskLevel.HIGH,
    )
    require_memory_context: Literal[True] = True
    require_validated_measurement: Literal[True] = True
    selector_code_sha256: str = Field(pattern=_SHA256_PATTERN)
    weights: PortfolioUtilityWeights = Field(default_factory=PortfolioUtilityWeights)
    frozen_at: AwareDatetime

    @model_validator(mode="after")
    def _policy_is_coherent(self) -> "PortfolioSelectionPolicy":
        if self.maximum_risk_level is PortfolioRiskLevel.PROHIBITED:
            raise ValueError("portfolio policy cannot authorize prohibited risk")
        if self.minimum_replication_actions > self.maximum_selected_actions:
            raise ValueError("replication quota exceeds the portfolio action cap")
        risks = tuple(sorted(set(self.required_approval_risks), key=lambda item: item.value))
        if PortfolioRiskLevel.PROHIBITED in risks:
            raise ValueError("prohibited actions cannot become approvable")
        object.__setattr__(self, "required_approval_risks", risks)
        return self

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class PortfolioSlateSpec(FrozenPortfolioModel):
    policy: PortfolioSelectionPolicy
    proposal: PortfolioProposal
    assessment_batch: PortfolioAssessmentBatch

    @model_validator(mode="after")
    def _slate_bindings_are_exact(self) -> "PortfolioSlateSpec":
        if self.policy.quest_id != self.proposal.quest_id:
            raise ValueError("portfolio policy and proposal belong to different Quests")
        manifest = self.assessment_batch.manifest
        if manifest.assessor_principal == self.proposal.proposer_principal:
            raise ValueError("portfolio assessor must be independent from the proposer")
        if (
            manifest.model_identity_sha256 is not None
            and manifest.model_identity_sha256 == self.proposal.proposer_model_identity_sha256
        ):
            raise ValueError("portfolio assessor cannot reuse the proposer model identity")
        proposals = {item.candidate_id: item for item in self.proposal.candidates}
        assessments = {item.candidate_id: item for item in self.assessment_batch.assessments}
        if set(proposals) != set(assessments):
            raise ValueError("portfolio assessments must cover every proposal exactly once")
        for candidate_id, proposal in proposals.items():
            assessment = assessments[candidate_id]
            if assessment.action_sha256 != proposal.action_sha256:
                raise ValueError("portfolio assessment changed its action binding")
            if proposal.action_type in INFORMATION_ACTIONS and assessment.information_model is None:
                raise ValueError("information-seeking portfolio action requires a prediction model")
            if proposal.action_type not in INFORMATION_ACTIONS and assessment.information_model:
                raise ValueError(
                    "non-information portfolio action cannot claim expected information"
                )
            if proposal.action_type is PortfolioActionType.REPLICATION:
                if assessment.expected_replication_debt_reduction <= 0:
                    raise ValueError("replication action must reduce frozen replication debt")
            elif assessment.expected_replication_debt_reduction > 0:
                raise ValueError("only a replication action can reduce replication debt")
            if assessment.approval is not None:
                if assessment.approval.action_sha256 != proposal.action_sha256:
                    raise ValueError("portfolio approval is rebound from its exact action")
                if assessment.approval.approver_principal in {
                    self.proposal.proposer_principal,
                    manifest.assessor_principal,
                }:
                    raise ValueError("portfolio approver must be independent")
        if self.policy.frozen_at > self.proposal.generated_at:
            raise ValueError("portfolio proposal predates its selection policy")
        if self.assessment_batch.completed_at < self.proposal.generated_at:
            raise ValueError("portfolio assessment batch predates its proposal")
        if any(
            item.completed_at < self.proposal.generated_at
            for item in self.assessment_batch.assessments
        ):
            raise ValueError("portfolio candidate assessment predates its proposal")
        return self

    @property
    def slate_id(self) -> str:
        return (
            "psl_"
            + content_sha256(
                {
                    "schema": "aletheia.portfolio_slate_identity.v1",
                    "quest_id": self.policy.quest_id,
                    "policy_sha256": self.policy.policy_sha256,
                    "proposal_sha256": self.proposal.proposal_sha256,
                    "assessment_batch_sha256": self.assessment_batch.batch_sha256,
                }
            )[:32]
        )

    @property
    def spec_sha256(self) -> str:
        return content_sha256(self)


class HumanPortfolioPlanSpec(FrozenPortfolioModel):
    selected_candidate_ids: tuple[str, ...] = Field(max_length=16)
    rationale: str = Field(min_length=1, max_length=4_000)
    planner_output_access: Literal["none"] = "none"
    issued_at: AwareDatetime

    @model_validator(mode="after")
    def _selection_is_unique(self) -> "HumanPortfolioPlanSpec":
        if len(set(self.selected_candidate_ids)) != len(self.selected_candidate_ids):
            raise ValueError("human portfolio plan cannot repeat a candidate")
        if any(
            not value.startswith("pca_") or len(value) != 36
            for value in self.selected_candidate_ids
        ):
            raise ValueError("human portfolio plan contains an invalid candidate ID")
        object.__setattr__(self, "rationale", self.rationale.strip())
        return self

    @property
    def plan_sha256(self) -> str:
        return content_sha256(self)


def human_plan_id(slate_id: str, plan_sha256: str) -> str:
    return (
        "php_"
        + content_sha256(
            {
                "schema": "aletheia.human_portfolio_plan_identity.v1",
                "slate_id": slate_id,
                "plan_sha256": plan_sha256,
            }
        )[:32]
    )


class PortfolioBudgetAvailability(FrozenPortfolioModel):
    allocation_id: str = Field(pattern=r"^bga_[0-9a-f]{32}$")
    program_id: str = Field(pattern=_PROGRAM_ID_PATTERN)
    kind: BudgetKind
    cap_microunits: int = Field(gt=0)
    spent_microunits: int = Field(ge=0)
    available_microunits: int = Field(ge=0)

    @model_validator(mode="after")
    def _availability_is_exact(self) -> "PortfolioBudgetAvailability":
        if self.spent_microunits + self.available_microunits != self.cap_microunits:
            raise ValueError("portfolio budget availability does not reconcile to its cap")
        return self


class PortfolioInformationAudit(FrozenPortfolioModel):
    information_model_sha256: str = Field(pattern=_SHA256_PATTERN)
    prior_entropy_micronats: int = Field(ge=0)
    expected_posterior_entropy_micronats: int = Field(ge=0)
    expected_information_gain_micronats: int = Field(ge=0)
    expected_information_gain_ratio_ppm: int = Field(ge=0, le=_PPM)

    @model_validator(mode="after")
    def _entropy_reconciles(self) -> "PortfolioInformationAudit":
        if (
            self.expected_posterior_entropy_micronats + self.expected_information_gain_micronats
            != self.prior_entropy_micronats
        ):
            raise ValueError("portfolio information audit entropy does not reconcile")
        return self

    @property
    def audit_sha256(self) -> str:
        return content_sha256(self)


class PortfolioCandidateScore(FrozenPortfolioModel):
    candidate_id: str = Field(pattern=_CANDIDATE_ID_PATTERN)
    action_sha256: str = Field(pattern=_SHA256_PATTERN)
    assessment_sha256: str = Field(pattern=_SHA256_PATTERN)
    program_id: str = Field(pattern=_PROGRAM_ID_PATTERN)
    family_id: str | None = Field(default=None, pattern=_FAMILY_ID_PATTERN)
    information_audit: PortfolioInformationAudit | None = None
    importance_ppm: int = Field(ge=0, le=_PPM)
    novelty_ppm: int = Field(ge=0, le=_PPM)
    success_probability_ppm: int = Field(ge=0, le=_PPM)
    replication_debt_reduction_ppm: int = Field(ge=0, le=_PPM)
    cost_burden_ppm: int = Field(ge=0, le=_PPM)
    duration_burden_ppm: int = Field(ge=0, le=_PPM)
    risk_burden_ppm: int = Field(ge=0, le=_PPM)
    base_utility_microscore: int
    feasible: bool
    blockers: tuple[str, ...]

    @model_validator(mode="after")
    def _score_is_coherent(self) -> "PortfolioCandidateScore":
        if self.blockers != tuple(sorted(set(self.blockers))):
            raise ValueError("portfolio candidate blockers must be unique and canonical")
        if self.feasible != (not self.blockers):
            raise ValueError("portfolio candidate feasibility must be derived from blockers")
        return self

    @property
    def score_sha256(self) -> str:
        return content_sha256(self)


class PortfolioBudgetProjection(FrozenPortfolioModel):
    allocation_id: str = Field(pattern=r"^bga_[0-9a-f]{32}$")
    kind: BudgetKind
    before_microunits: int = Field(ge=0)
    selected_microunits: int = Field(ge=0)
    after_microunits: int = Field(ge=0)

    @model_validator(mode="after")
    def _projection_reconciles(self) -> "PortfolioBudgetProjection":
        if self.before_microunits - self.selected_microunits != self.after_microunits:
            raise ValueError("portfolio budget projection does not reconcile")
        return self


class PortfolioSelectionEntry(FrozenPortfolioModel):
    rank: int = Field(ge=1, le=64)
    candidate_id: str = Field(pattern=_CANDIDATE_ID_PATTERN)
    score_sha256: str = Field(pattern=_SHA256_PATTERN)
    selected: bool
    marginal_utility_microscore: int | None = None
    reasons: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _entry_is_canonical(self) -> "PortfolioSelectionEntry":
        if self.reasons != tuple(sorted(set(self.reasons))):
            raise ValueError("portfolio ranking reasons must be unique and canonical")
        if self.selected != (self.marginal_utility_microscore is not None):
            raise ValueError("selected portfolio entry requires marginal utility")
        return self


class PortfolioSelectionDecision(FrozenPortfolioModel):
    selected_candidate_ids: tuple[str, ...] = Field(max_length=16)
    rankings: tuple[PortfolioSelectionEntry, ...] = Field(min_length=2, max_length=64)
    budget_projection: tuple[PortfolioBudgetProjection, ...]
    disposition: PortfolioEpochDisposition
    shadow_only: Literal[True] = True
    actions_enqueued: Literal[False] = False

    @model_validator(mode="after")
    def _decision_is_complete(self) -> "PortfolioSelectionDecision":
        if [item.rank for item in self.rankings] != list(range(1, len(self.rankings) + 1)):
            raise ValueError("portfolio ranking must use contiguous ranks")
        ranked_ids = [item.candidate_id for item in self.rankings]
        if len(set(ranked_ids)) != len(ranked_ids):
            raise ValueError("portfolio ranking cannot repeat a candidate")
        selected = tuple(item.candidate_id for item in self.rankings if item.selected)
        if selected != self.selected_candidate_ids:
            raise ValueError("portfolio selected IDs differ from selected rankings")
        allocation_ids = [item.allocation_id for item in self.budget_projection]
        if allocation_ids != sorted(set(allocation_ids)):
            raise ValueError("portfolio budget projection must be unique and canonical")
        if self.disposition is PortfolioEpochDisposition.SHADOW_READY:
            if not self.selected_candidate_ids:
                raise ValueError("shadow-ready portfolio decision requires a selected action")
        elif self.selected_candidate_ids:
            raise ValueError("blocked portfolio decision cannot select actions")
        return self

    @property
    def decision_sha256(self) -> str:
        return content_sha256(self)


class PortfolioShadowComparison(FrozenPortfolioModel):
    human_selected_candidate_ids: tuple[str, ...]
    planner_selected_candidate_ids: tuple[str, ...]
    overlap_count: int = Field(ge=0)
    union_count: int = Field(ge=0)
    jaccard_ppm: int = Field(ge=0, le=_PPM)
    exact_set_match: bool
    human_hard_filter_violations: tuple[str, ...]
    human_batch_constraint_violations: tuple[str, ...]
    planner_base_utility_sum: int
    human_feasible_base_utility_sum: int

    @model_validator(mode="after")
    def _comparison_is_canonical(self) -> "PortfolioShadowComparison":
        for name in ("human_hard_filter_violations", "human_batch_constraint_violations"):
            value = getattr(self, name)
            if value != tuple(sorted(set(value))):
                raise ValueError(f"portfolio comparison {name} must be canonical")
        human = set(self.human_selected_candidate_ids)
        planner = set(self.planner_selected_candidate_ids)
        if len(human) != len(self.human_selected_candidate_ids) or len(planner) != len(
            self.planner_selected_candidate_ids
        ):
            raise ValueError("portfolio comparison selections cannot repeat candidates")
        if self.overlap_count != len(human & planner) or self.union_count != len(human | planner):
            raise ValueError("portfolio comparison overlap/union does not match selections")
        expected_jaccard = (
            _PPM if not (human | planner) else len(human & planner) * _PPM // len(human | planner)
        )
        if self.jaccard_ppm != expected_jaccard or self.exact_set_match != (human == planner):
            raise ValueError("portfolio comparison agreement metrics are inconsistent")
        return self

    @property
    def comparison_sha256(self) -> str:
        return content_sha256(self)


class PortfolioMutationReceipt(FrozenPortfolioModel):
    object_id: str
    command: ScientificCommandReceipt

    @property
    def created(self) -> bool:
        return self.command.created


class PortfolioSlateSnapshot(FrozenPortfolioModel):
    slate_id: str = Field(pattern=_SLATE_ID_PATTERN)
    spec: PortfolioSlateSpec
    graph_snapshot: QuestGraphSnapshot
    budget_state: tuple[PortfolioBudgetAvailability, ...]
    human_plan_id: str | None = Field(default=None, pattern=_HUMAN_PLAN_ID_PATTERN)
    human_plan: HumanPortfolioPlanSpec | None = None
    epoch_id: str | None = Field(default=None, pattern=_EPOCH_ID_PATTERN)
    command_id: str
    created_by: str
    created_at: AwareDatetime
    snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _snapshot_hash_is_valid(self) -> "PortfolioSlateSnapshot":
        if (self.human_plan_id is None) != (self.human_plan is None):
            raise ValueError("portfolio human plan identity and payload must appear together")
        allocation_ids = [item.allocation_id for item in self.budget_state]
        if allocation_ids != sorted(set(allocation_ids)):
            raise ValueError("portfolio frozen budget state must be unique and canonical")
        expected = content_sha256(self.model_dump(mode="json", exclude={"snapshot_sha256"}))
        if self.snapshot_sha256 != expected:
            raise ValueError("portfolio slate snapshot hash does not match its ledger")
        return self


class PortfolioEpochSnapshot(FrozenPortfolioModel):
    epoch_id: str = Field(pattern=_EPOCH_ID_PATTERN)
    slate_id: str = Field(pattern=_SLATE_ID_PATTERN)
    human_plan_id: str = Field(pattern=_HUMAN_PLAN_ID_PATTERN)
    scores: tuple[PortfolioCandidateScore, ...]
    decision: PortfolioSelectionDecision
    comparison: PortfolioShadowComparison
    evaluated_at: AwareDatetime
    command_id: str
    created_by: str
    created_at: AwareDatetime
    epoch_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _epoch_hash_is_valid(self) -> "PortfolioEpochSnapshot":
        candidate_ids = [item.candidate_id for item in self.scores]
        if candidate_ids != sorted(set(candidate_ids)):
            raise ValueError("portfolio epoch scores must be unique and canonical")
        rankings = {item.candidate_id: item for item in self.decision.rankings}
        if set(candidate_ids) != set(rankings) or any(
            rankings[item.candidate_id].score_sha256 != item.score_sha256 for item in self.scores
        ):
            raise ValueError("portfolio epoch score/ranking bindings are incomplete")
        if self.comparison.planner_selected_candidate_ids != self.decision.selected_candidate_ids:
            raise ValueError("portfolio epoch comparison uses another planner decision")
        expected = content_sha256(self.model_dump(mode="json", exclude={"epoch_sha256"}))
        if self.epoch_sha256 != expected:
            raise ValueError("portfolio epoch hash does not match its ledger")
        return self


class PortfolioShadowAuditPolicy(FrozenPortfolioModel):
    minimum_epochs: int = Field(default=20, ge=1, le=10_000)
    minimum_mean_jaccard_ppm: int = Field(default=600_000, ge=0, le=_PPM)
    maximum_human_hard_filter_violations: int = Field(default=0, ge=0)
    maximum_planner_empty_epochs: int = Field(default=0, ge=0)


class PortfolioShadowAudit(FrozenPortfolioModel):
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    policy: PortfolioShadowAuditPolicy
    epoch_count: int = Field(ge=0)
    mean_jaccard_ppm: int = Field(ge=0, le=_PPM)
    human_hard_filter_violation_count: int = Field(ge=0)
    planner_empty_epoch_count: int = Field(ge=0)
    eligible_for_human_activation_review: bool
    autonomous_allocation_enabled: Literal[False] = False
    blockers: tuple[str, ...]

    @model_validator(mode="after")
    def _audit_blockers_are_canonical(self) -> "PortfolioShadowAudit":
        if self.blockers != tuple(sorted(set(self.blockers))):
            raise ValueError("portfolio shadow audit blockers must be canonical")
        if self.eligible_for_human_activation_review != (not self.blockers):
            raise ValueError("portfolio shadow readiness must be derived from blockers")
        return self


__all__ = [
    "CAMPAIGN_ACTIONS",
    "INFORMATION_ACTIONS",
    "PORTFOLIO_ASSESSMENT_OUTPUT_SCHEMA_SHA256",
    "PROGRAM_ACTIONS",
    "HumanPortfolioPlanSpec",
    "PortfolioActionSpec",
    "PortfolioActionType",
    "PortfolioApprovalReceipt",
    "PortfolioAssessmentBatch",
    "PortfolioAssessmentManifest",
    "PortfolioAssessorKind",
    "PortfolioBudgetAvailability",
    "PortfolioBudgetProjection",
    "PortfolioCandidateAssessment",
    "PortfolioCandidateScore",
    "PortfolioCostEstimate",
    "PortfolioEpochDisposition",
    "PortfolioEpochSnapshot",
    "PortfolioHypothesisLikelihood",
    "PortfolioInformationAudit",
    "PortfolioInformationModel",
    "PortfolioMeasurementStatus",
    "PortfolioMutationReceipt",
    "PortfolioOutcomeProbability",
    "PortfolioPriorProbability",
    "PortfolioProposal",
    "PortfolioRiskLevel",
    "PortfolioSelectionDecision",
    "PortfolioSelectionEntry",
    "PortfolioSelectionPolicy",
    "PortfolioShadowAudit",
    "PortfolioShadowAuditPolicy",
    "PortfolioShadowComparison",
    "PortfolioSlateSnapshot",
    "PortfolioSlateSpec",
    "PortfolioUtilityWeights",
    "human_plan_id",
]
