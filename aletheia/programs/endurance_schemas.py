"""Frozen contracts for the F11-S7 research endurance gate.

The production evidence class is deliberately impossible to accelerate: a real-time report must
cover at least 72 hours using database-observed timestamps.  Accelerated runs exercise the same
ledger and recovery machinery, but remain permanently labelled as engineering evidence.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from aletheia.programs.schemas import BudgetKind, GraphNodeState
from aletheia.reproducibility.manifest import content_sha256

REAL_72H_SECONDS = 72 * 60 * 60

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_QUEST_ID_PATTERN = r"^qst_[0-9a-f]{32}$"
_CAMPAIGN_ID_PATTERN = r"^cmp_[0-9a-f]{32}$"
_FAULT_CAMPAIGN_ID_PATTERN = r"^fic_[0-9a-f]{32}$"
_FACT_ID_PATTERN = r"^mem_[0-9a-f]{32}$"
_EPOCH_ID_PATTERN = r"^pep_[0-9a-f]{32}$"
_GATE_ID_PATTERN = r"^edg_[0-9a-f]{32}$"
_CHECKPOINT_ID_PATTERN = r"^edc_[0-9a-f]{32}$"
_REPRODUCTION_ID_PATTERN = r"^edr_[0-9a-f]{32}$"
_INTERRUPTION_ID_PATTERN = r"^edi_[0-9a-f]{32}$"
_PIVOT_ID_PATTERN = r"^edp_[0-9a-f]{32}$"
_EFFICIENCY_ID_PATTERN = r"^ede_[0-9a-f]{32}$"
_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"


class FrozenEnduranceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EnduranceEvidenceClass(str, Enum):
    ACCELERATED_ENGINEERING = "accelerated_engineering"
    REAL_TIME_72H = "real_time_72h"


class EnduranceGateDisposition(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


class EnduranceInterruptionKind(str, Enum):
    PROCESS_KILL = "process_kill"
    PROVIDER_TRANSPORT = "provider_transport"


class EnduranceReproductionConclusion(str, Enum):
    CONFIRMED = "confirmed"
    CONTRADICTED = "contradicted"
    INCONCLUSIVE = "inconclusive"


class EnduranceEfficiencyMetric(str, Enum):
    INFORMATION_GAIN = "information_gain"
    QUESTION_COVERAGE = "question_coverage"


class EnduranceGateManifest(FrozenEnduranceModel):
    """Immutable policy and source identities sealed before the gate starts."""

    schema_version: Literal[1] = 1
    gate_id: str | None = Field(default=None, pattern=_GATE_ID_PATTERN)
    gate_key: str = Field(pattern=_IDENTITY_PATTERN)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    evidence_class: EnduranceEvidenceClass
    required_duration_seconds: int = Field(ge=1, le=31 * 24 * 60 * 60)
    checkpoint_interval_seconds: int = Field(ge=1, le=24 * 60 * 60)
    maximum_checkpoint_gap_seconds: int = Field(ge=1, le=48 * 60 * 60)
    frozen_quest_spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    initial_graph_sha256: str = Field(pattern=_SHA256_PATTERN)
    frozen_question_sha256s: tuple[str, ...] = Field(min_length=2, max_length=10_000)
    initial_campaign_ids: tuple[str, ...] = Field(min_length=3, max_length=10_000)
    frozen_budget_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    frozen_data_role_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    prerequisite_fault_campaign_id: str = Field(pattern=_FAULT_CAMPAIGN_ID_PATTERN)
    prerequisite_fault_report_sha256: str = Field(pattern=_SHA256_PATTERN)
    harness_code_sha256: str = Field(pattern=_SHA256_PATTERN)
    environment_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    minimum_negative_results: int = Field(default=1, ge=1, le=1_000)
    minimum_reproductions: int = Field(default=1, ge=1, le=1_000)
    minimum_process_kills: int = Field(default=1, ge=1, le=1_000)
    minimum_provider_interruptions: int = Field(default=1, ge=1, le=1_000)
    minimum_structural_pivots: int = Field(default=1, ge=1, le=1_000)
    minimum_portfolio_epochs: int = Field(default=1, ge=1, le=10_000)
    minimum_efficiency_improvement_ppm: int = Field(default=100_000, ge=1, le=10_000_000)
    outward_actions_allowed: Literal[False] = False
    autonomous_allocation_enabled: Literal[False] = False

    @model_validator(mode="after")
    def _canonical_and_non_weakened(self) -> "EnduranceGateManifest":
        questions = tuple(sorted(set(self.frozen_question_sha256s)))
        campaigns = tuple(sorted(set(self.initial_campaign_ids)))
        if questions != self.frozen_question_sha256s:
            raise ValueError("frozen endurance questions must be unique and canonical")
        if campaigns != self.initial_campaign_ids:
            raise ValueError("initial endurance campaigns must be unique and canonical")
        if any(len(item) != 64 for item in questions):
            raise ValueError("frozen endurance question identities must be SHA-256 values")
        if any(not item.startswith("cmp_") or len(item) != 36 for item in campaigns):
            raise ValueError("initial endurance campaign identities are invalid")
        if self.maximum_checkpoint_gap_seconds < self.checkpoint_interval_seconds:
            raise ValueError("maximum checkpoint gap cannot be shorter than the interval")
        if self.checkpoint_interval_seconds > self.required_duration_seconds:
            raise ValueError("checkpoint interval cannot exceed required gate duration")
        if self.evidence_class is EnduranceEvidenceClass.REAL_TIME_72H:
            if self.required_duration_seconds < REAL_72H_SECONDS:
                raise ValueError("real-time endurance evidence must cover at least 72 hours")
            if self.maximum_checkpoint_gap_seconds > 12 * 60 * 60:
                raise ValueError("real-time endurance checkpoints cannot be more than 12 hours apart")
        elif self.required_duration_seconds > 24 * 60 * 60:
            raise ValueError("accelerated engineering gates cannot claim more than one day")
        expected_id = f"edg_{self.manifest_sha256[:32]}"
        if self.gate_id is not None and self.gate_id != expected_id:
            raise ValueError("endurance gate ID does not match its manifest")
        object.__setattr__(self, "gate_id", expected_id)
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self.model_dump(mode="json", exclude={"gate_id"}))


class EnduranceStrategyFingerprint(FrozenEnduranceModel):
    hypothesis_semantics_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_pattern_sha256: str = Field(pattern=_SHA256_PATTERN)
    capability_input_sha256: str = Field(pattern=_SHA256_PATTERN)
    analysis_plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    discriminated_pairs_sha256: str = Field(pattern=_SHA256_PATTERN)

    @property
    def fingerprint_sha256(self) -> str:
        return content_sha256(self)


class EnduranceReproductionReceipt(FrozenEnduranceModel):
    receipt_id: str | None = Field(default=None, pattern=_REPRODUCTION_ID_PATTERN)
    original_campaign_id: str = Field(pattern=_CAMPAIGN_ID_PATTERN)
    reproduction_campaign_id: str = Field(pattern=_CAMPAIGN_ID_PATTERN)
    protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    original_result_sha256: str = Field(pattern=_SHA256_PATTERN)
    reproduction_result_sha256: str = Field(pattern=_SHA256_PATTERN)
    conclusion: EnduranceReproductionConclusion
    evidence_sha256s: tuple[str, ...] = Field(min_length=1, max_length=128)
    validated_by: str = Field(min_length=1, max_length=128)
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def _canonical_receipt(self) -> "EnduranceReproductionReceipt":
        if self.original_campaign_id == self.reproduction_campaign_id:
            raise ValueError("reproduction must use a distinct campaign branch")
        evidence = tuple(sorted(set(self.evidence_sha256s)))
        if evidence != self.evidence_sha256s:
            raise ValueError("reproduction evidence hashes must be unique and canonical")
        expected_id = f"edr_{self.receipt_sha256[:32]}"
        if self.receipt_id is not None and self.receipt_id != expected_id:
            raise ValueError("reproduction receipt ID does not match its contents")
        object.__setattr__(self, "receipt_id", expected_id)
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self.model_dump(mode="json", exclude={"receipt_id"}))


class EnduranceInterruptionReceipt(FrozenEnduranceModel):
    receipt_id: str | None = Field(default=None, pattern=_INTERRUPTION_ID_PATTERN)
    kind: EnduranceInterruptionKind
    fault_campaign_id: str = Field(pattern=_FAULT_CAMPAIGN_ID_PATTERN)
    fault_report_sha256: str = Field(pattern=_SHA256_PATTERN)
    scenario_id: str = Field(pattern=_IDENTITY_PATTERN)
    recovery_evidence_sha256s: tuple[str, ...] = Field(min_length=1, max_length=128)
    injection_confirmed: Literal[True] = True
    recovery_confirmed: Literal[True] = True
    occurred_at: AwareDatetime

    @model_validator(mode="after")
    def _canonical_receipt(self) -> "EnduranceInterruptionReceipt":
        evidence = tuple(sorted(set(self.recovery_evidence_sha256s)))
        if evidence != self.recovery_evidence_sha256s:
            raise ValueError("interruption recovery evidence must be unique and canonical")
        expected_id = f"edi_{self.receipt_sha256[:32]}"
        if self.receipt_id is not None and self.receipt_id != expected_id:
            raise ValueError("interruption receipt ID does not match its contents")
        object.__setattr__(self, "receipt_id", expected_id)
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self.model_dump(mode="json", exclude={"receipt_id"}))


class EnduranceStructuralPivotReceipt(FrozenEnduranceModel):
    receipt_id: str | None = Field(default=None, pattern=_PIVOT_ID_PATTERN)
    negative_result_fact_id: str = Field(pattern=_FACT_ID_PATTERN)
    source_campaign_id: str = Field(pattern=_CAMPAIGN_ID_PATTERN)
    successor_campaign_id: str = Field(pattern=_CAMPAIGN_ID_PATTERN)
    source_transition_id: str = Field(min_length=1, max_length=96)
    successor_transition_id: str = Field(min_length=1, max_length=96)
    before: EnduranceStrategyFingerprint
    after: EnduranceStrategyFingerprint
    assessor_code_sha256: str = Field(pattern=_SHA256_PATTERN)
    assessed_by: str = Field(min_length=1, max_length=128)
    evidence_sha256s: tuple[str, ...] = Field(min_length=1, max_length=128)
    occurred_at: AwareDatetime

    @model_validator(mode="after")
    def _requires_a_structural_change(self) -> "EnduranceStructuralPivotReceipt":
        if self.source_campaign_id == self.successor_campaign_id:
            raise ValueError("structural pivot requires a distinct successor campaign")
        evidence = tuple(sorted(set(self.evidence_sha256s)))
        if evidence != self.evidence_sha256s:
            raise ValueError("pivot evidence hashes must be unique and canonical")
        changed = {
            field
            for field in (
                "hypothesis_semantics_sha256",
                "prediction_pattern_sha256",
                "capability_input_sha256",
                "analysis_plan_sha256",
                "discriminated_pairs_sha256",
            )
            if getattr(self.before, field) != getattr(self.after, field)
        }
        if not changed.intersection(
            {"prediction_pattern_sha256", "discriminated_pairs_sha256"}
        ):
            raise ValueError(
                "structural pivot must change predictions or discriminated hypothesis pairs"
            )
        if len(changed) < 2:
            raise ValueError("structural pivot must change at least two strategy dimensions")
        expected_id = f"edp_{self.receipt_sha256[:32]}"
        if self.receipt_id is not None and self.receipt_id != expected_id:
            raise ValueError("structural pivot receipt ID does not match its contents")
        object.__setattr__(self, "receipt_id", expected_id)
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self.model_dump(mode="json", exclude={"receipt_id"}))


class EnduranceEfficiencyReceipt(FrozenEnduranceModel):
    receipt_id: str | None = Field(default=None, pattern=_EFFICIENCY_ID_PATTERN)
    metric: EnduranceEfficiencyMetric
    baseline_value_units: int = Field(gt=0)
    baseline_cost_microunits: int = Field(gt=0)
    endurance_value_units: int = Field(gt=0)
    endurance_cost_microunits: int = Field(gt=0)
    improvement_ppm: int
    evidence_sha256s: tuple[str, ...] = Field(min_length=1, max_length=128)
    assessor_code_sha256: str = Field(pattern=_SHA256_PATTERN)
    assessed_by: str = Field(min_length=1, max_length=128)
    assessed_at: AwareDatetime

    @model_validator(mode="after")
    def _derived_improvement(self) -> "EnduranceEfficiencyReceipt":
        denominator = self.baseline_value_units * self.endurance_cost_microunits
        numerator = self.endurance_value_units * self.baseline_cost_microunits
        expected = ((numerator - denominator) * 1_000_000) // denominator
        if self.improvement_ppm != expected:
            raise ValueError("efficiency improvement is not derived from value and cost")
        evidence = tuple(sorted(set(self.evidence_sha256s)))
        if evidence != self.evidence_sha256s:
            raise ValueError("efficiency evidence hashes must be unique and canonical")
        expected_id = f"ede_{self.receipt_sha256[:32]}"
        if self.receipt_id is not None and self.receipt_id != expected_id:
            raise ValueError("efficiency receipt ID does not match its contents")
        object.__setattr__(self, "receipt_id", expected_id)
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self.model_dump(mode="json", exclude={"receipt_id"}))


class EnduranceCheckpointEvidence(FrozenEnduranceModel):
    reproductions: tuple[EnduranceReproductionReceipt, ...] = ()
    interruptions: tuple[EnduranceInterruptionReceipt, ...] = ()
    structural_pivots: tuple[EnduranceStructuralPivotReceipt, ...] = ()

    @model_validator(mode="after")
    def _canonical_evidence(self) -> "EnduranceCheckpointEvidence":
        for field in ("reproductions", "interruptions", "structural_pivots"):
            items = tuple(sorted(getattr(self, field), key=lambda item: item.receipt_id or ""))
            ids = [item.receipt_id for item in items]
            if ids != sorted(set(ids)):
                raise ValueError(f"checkpoint {field} must be unique and canonical")
            object.__setattr__(self, field, items)
        return self


class EnduranceBudgetState(FrozenEnduranceModel):
    allocation_id: str = Field(pattern=r"^bga_[0-9a-f]{32}$")
    scope_node_id: str = Field(pattern=r"^(qst|prg)_[0-9a-f]{32}$")
    kind: BudgetKind
    cap_microunits: int = Field(gt=0)
    spent_microunits: int = Field(ge=0)
    available_microunits: int = Field(ge=0)

    @model_validator(mode="after")
    def _balanced(self) -> "EnduranceBudgetState":
        if self.spent_microunits + self.available_microunits != self.cap_microunits:
            raise ValueError("endurance budget state does not balance to its cap")
        return self


class EnduranceLedgerObservation(FrozenEnduranceModel):
    quest_spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    graph_sha256: str = Field(pattern=_SHA256_PATTERN)
    question_sha256s: tuple[str, ...]
    campaign_ids: tuple[str, ...]
    negative_result_fact_ids: tuple[str, ...]
    portfolio_epoch_ids: tuple[str, ...]
    budget_state: tuple[EnduranceBudgetState, ...]
    one_time_action_count: int = Field(ge=0)
    one_time_action_receipt_count: int = Field(ge=0)
    reconciliation_required_count: int = Field(ge=0)
    scientific_state_loss_count: int = Field(ge=0)
    duplicate_scientific_state_count: int = Field(ge=0)
    duplicate_budget_charge_count: int = Field(ge=0)
    duplicate_outward_action_count: int = Field(ge=0)
    unresolved_ambiguity_without_block_count: int = Field(ge=0)
    event_state_mismatch_count: int = Field(ge=0)
    observed_at: AwareDatetime
    observation_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _canonical_and_hashed(self) -> "EnduranceLedgerObservation":
        for field in (
            "question_sha256s",
            "campaign_ids",
            "negative_result_fact_ids",
            "portfolio_epoch_ids",
        ):
            values = tuple(sorted(set(getattr(self, field))))
            if values != getattr(self, field):
                raise ValueError(f"endurance observation {field} must be unique and canonical")
        budgets = tuple(sorted(self.budget_state, key=lambda item: item.allocation_id))
        if budgets != self.budget_state or len({item.allocation_id for item in budgets}) != len(
            budgets
        ):
            raise ValueError("endurance observation budgets must be unique and canonical")
        expected = content_sha256(
            self.model_dump(mode="json", exclude={"observation_sha256"})
        )
        if self.observation_sha256 is not None and self.observation_sha256 != expected:
            raise ValueError("endurance observation hash does not match its contents")
        object.__setattr__(self, "observation_sha256", expected)
        return self

    @property
    def core_zero(self) -> bool:
        return all(
            getattr(self, field) == 0
            for field in (
                "scientific_state_loss_count",
                "duplicate_scientific_state_count",
                "duplicate_budget_charge_count",
                "duplicate_outward_action_count",
                "unresolved_ambiguity_without_block_count",
                "event_state_mismatch_count",
            )
        )


class EnduranceCheckpoint(FrozenEnduranceModel):
    checkpoint_id: str | None = Field(default=None, pattern=_CHECKPOINT_ID_PATTERN)
    gate_id: str = Field(pattern=_GATE_ID_PATTERN)
    sequence: int = Field(ge=1)
    parent_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation: EnduranceLedgerObservation
    evidence: EnduranceCheckpointEvidence = EnduranceCheckpointEvidence()
    checkpoint_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _content_addressed(self) -> "EnduranceCheckpoint":
        expected_sha = content_sha256(
            self.model_dump(mode="json", exclude={"checkpoint_id", "checkpoint_sha256"})
        )
        if self.checkpoint_sha256 is not None and self.checkpoint_sha256 != expected_sha:
            raise ValueError("endurance checkpoint hash does not match its contents")
        expected_id = f"edc_{expected_sha[:32]}"
        if self.checkpoint_id is not None and self.checkpoint_id != expected_id:
            raise ValueError("endurance checkpoint ID does not match its contents")
        object.__setattr__(self, "checkpoint_sha256", expected_sha)
        object.__setattr__(self, "checkpoint_id", expected_id)
        return self


class EnduranceCampaignStatus(FrozenEnduranceModel):
    campaign_id: str = Field(pattern=_CAMPAIGN_ID_PATTERN)
    state: GraphNodeState
    state_version: int = Field(ge=1)
    latest_transition_id: str = Field(min_length=1, max_length=96)
    reason: str = Field(min_length=1, max_length=4_000)


class EndurancePortfolioReport(FrozenEnduranceModel):
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    graph_sha256: str = Field(pattern=_SHA256_PATTERN)
    question_sha256s: tuple[str, ...] = Field(min_length=2)
    campaigns: tuple[EnduranceCampaignStatus, ...] = Field(min_length=3)
    negative_result_fact_ids: tuple[str, ...]
    reproduction_receipt_ids: tuple[str, ...]
    interruption_receipt_ids: tuple[str, ...]
    structural_pivot_receipt_ids: tuple[str, ...]
    portfolio_epoch_ids: tuple[str, ...]
    budget_state: tuple[EnduranceBudgetState, ...]
    report_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _canonical_and_hashed(self) -> "EndurancePortfolioReport":
        for field in (
            "question_sha256s",
            "negative_result_fact_ids",
            "reproduction_receipt_ids",
            "interruption_receipt_ids",
            "structural_pivot_receipt_ids",
            "portfolio_epoch_ids",
        ):
            values = tuple(sorted(set(getattr(self, field))))
            if values != getattr(self, field):
                raise ValueError(f"portfolio report {field} must be unique and canonical")
        campaigns = tuple(sorted(self.campaigns, key=lambda item: item.campaign_id))
        if campaigns != self.campaigns or len({item.campaign_id for item in campaigns}) != len(
            campaigns
        ):
            raise ValueError("portfolio report campaigns must be unique and canonical")
        budgets = tuple(sorted(self.budget_state, key=lambda item: item.allocation_id))
        if budgets != self.budget_state:
            raise ValueError("portfolio report budgets must be canonical")
        expected = content_sha256(self.model_dump(mode="json", exclude={"report_sha256"}))
        if self.report_sha256 is not None and self.report_sha256 != expected:
            raise ValueError("endurance portfolio report hash does not match its contents")
        object.__setattr__(self, "report_sha256", expected)
        return self


class EnduranceGateReport(FrozenEnduranceModel):
    manifest: EnduranceGateManifest
    started_at: AwareDatetime
    completed_at: AwareDatetime
    elapsed_seconds: int = Field(ge=0)
    checkpoint_count: int = Field(ge=0)
    maximum_observed_gap_seconds: int = Field(ge=0)
    checkpoint_chain_sha256: str = Field(pattern=_SHA256_PATTERN)
    negative_result_count: int = Field(ge=0)
    reproduction_count: int = Field(ge=0)
    process_kill_count: int = Field(ge=0)
    provider_interruption_count: int = Field(ge=0)
    structural_pivot_count: int = Field(ge=0)
    portfolio_epoch_count: int = Field(ge=0)
    efficiency: EnduranceEfficiencyReceipt | None
    final_portfolio: EndurancePortfolioReport
    disposition: EnduranceGateDisposition
    blockers: tuple[str, ...]
    real_72h_passed: bool
    eligible_for_f11_scientific_exit_review: bool
    autonomous_allocation_enabled: Literal[False] = False
    report_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _derived_verdict_and_hash(self) -> "EnduranceGateReport":
        if self.completed_at < self.started_at:
            raise ValueError("endurance gate completes before it starts")
        expected_elapsed = int((self.completed_at - self.started_at).total_seconds())
        if self.elapsed_seconds != expected_elapsed:
            raise ValueError("endurance elapsed seconds are not timestamp-derived")
        blockers = tuple(sorted(set(self.blockers)))
        if blockers != self.blockers:
            raise ValueError("endurance report blockers must be unique and canonical")
        expected_disposition = (
            EnduranceGateDisposition.FAILED
            if any(item.startswith("integrity:") for item in blockers)
            else EnduranceGateDisposition.PASSED
            if not blockers
            else EnduranceGateDisposition.BLOCKED
        )
        if self.disposition is not expected_disposition:
            raise ValueError("endurance disposition is not derived from blockers")
        expected_real = (
            self.disposition is EnduranceGateDisposition.PASSED
            and self.manifest.evidence_class is EnduranceEvidenceClass.REAL_TIME_72H
            and self.elapsed_seconds >= REAL_72H_SECONDS
        )
        if self.real_72h_passed != expected_real:
            raise ValueError("real 72-hour verdict is inconsistent with duration/evidence class")
        if self.eligible_for_f11_scientific_exit_review != expected_real:
            raise ValueError("F11 exit eligibility must equal a real-time passing gate")
        if self.final_portfolio.quest_id != self.manifest.quest_id:
            raise ValueError("endurance portfolio report belongs to another Quest")
        expected_sha = content_sha256(self.model_dump(mode="json", exclude={"report_sha256"}))
        if self.report_sha256 is not None and self.report_sha256 != expected_sha:
            raise ValueError("endurance gate report hash does not match its contents")
        object.__setattr__(self, "report_sha256", expected_sha)
        return self


class EnduranceCommandContext(FrozenEnduranceModel):
    idempotency_key: str = Field(pattern=_IDENTITY_PATTERN)
    principal: str = Field(min_length=1, max_length=128)
    source_event_key: str | None = Field(default=None, pattern=_IDENTITY_PATTERN)


class EnduranceMutationReceipt(FrozenEnduranceModel):
    object_id: str
    command_id: str
    created: bool


class EnduranceCheckpointSnapshot(FrozenEnduranceModel):
    checkpoint: EnduranceCheckpoint
    command_id: str
    created_by: str
    created_at: AwareDatetime


class EnduranceGateSnapshot(FrozenEnduranceModel):
    manifest: EnduranceGateManifest
    started_at: AwareDatetime
    checkpoints: tuple[EnduranceCheckpointSnapshot, ...]
    report: EnduranceGateReport | None
    start_command_id: str
    started_by: str
    created_at: AwareDatetime


class EnduranceGateAudit(FrozenEnduranceModel):
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    gate_count: int = Field(ge=0)
    latest_gate_id: str | None = Field(default=None, pattern=_GATE_ID_PATTERN)
    latest_disposition: EnduranceGateDisposition | None
    latest_evidence_class: EnduranceEvidenceClass | None
    latest_real_72h_passed: bool
    eligible_for_f11_scientific_exit_review: bool
    autonomous_allocation_enabled: Literal[False] = False
    blockers: tuple[str, ...]

    @model_validator(mode="after")
    def _canonical(self) -> "EnduranceGateAudit":
        blockers = tuple(sorted(set(self.blockers)))
        if blockers != self.blockers:
            raise ValueError("endurance audit blockers must be unique and canonical")
        if (self.latest_gate_id is None) != (self.gate_count == 0):
            raise ValueError("endurance audit latest gate differs from gate count")
        if self.eligible_for_f11_scientific_exit_review != (
            self.latest_real_72h_passed and not blockers
        ):
            raise ValueError("endurance audit eligibility differs from its evidence")
        return self


__all__ = [
    "REAL_72H_SECONDS",
    "EnduranceBudgetState",
    "EnduranceCampaignStatus",
    "EnduranceCheckpoint",
    "EnduranceCheckpointEvidence",
    "EnduranceCheckpointSnapshot",
    "EnduranceCommandContext",
    "EnduranceEfficiencyMetric",
    "EnduranceEfficiencyReceipt",
    "EnduranceEvidenceClass",
    "EnduranceGateAudit",
    "EnduranceGateDisposition",
    "EnduranceGateManifest",
    "EnduranceGateReport",
    "EnduranceGateSnapshot",
    "EnduranceInterruptionKind",
    "EnduranceInterruptionReceipt",
    "EnduranceLedgerObservation",
    "EnduranceMutationReceipt",
    "EndurancePortfolioReport",
    "EnduranceReproductionConclusion",
    "EnduranceReproductionReceipt",
    "EnduranceStrategyFingerprint",
    "EnduranceStructuralPivotReceipt",
]
