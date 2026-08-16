"""Independent F9/K3 acceptance scoring over committed scientific artifacts.

The scorer receives no raw observation and no model-authored summary. It physically reloads the
selection, validation, update, and persistence artifacts, then mechanically re-derives the checks
that distinguish an intact K3 evidence spine from a genuine scientific-exit demonstration.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from enum import Enum
from itertools import combinations
from typing import Callable, Literal

from pydantic import AwareDatetime, Field, ValidationError, model_validator

from aletheia.epistemics.belief_update import (
    CommittedObservationValidationCampaign,
    CommittedWorldBeliefUpdateCampaign,
    ContradictionKind,
    HypothesisRevisionAction,
    HypothesisRevisionDirective,
    ObservationValidationDisposition,
    WorldBeliefUpdateDisposition,
    WorldRevisionAction,
    load_observation_validation_campaign,
    load_world_belief_update_campaign,
)
from aletheia.epistemics.causal import CausalClaimCeiling
from aletheia.epistemics.schemas import (
    EpistemicModel,
    HypothesisLifecycle,
    HypothesisRole,
    HypothesisVersion,
    Prediction,
    WorldModelSnapshot,
)
from aletheia.epistemics.selector import (
    CommittedExperimentSelectionCampaign,
    ExperimentCandidate,
    ExperimentSelectionDisposition,
    load_experiment_selection_campaign,
)
from aletheia.knowledge.response_archive import (
    ArchivedKnowledgeLedger,
    ContentAddressedResponseArchive,
    ResponseArchiveError,
)
from aletheia.reproducibility.manifest import canonical_json_bytes, content_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_LOCAL_ID_PATTERN = r"^[a-z][a-z0-9_.-]{1,79}$"
_ACTOR_ID_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$"
_HYPOTHESIS_ID_PATTERN = r"^hyp_[0-9a-f]{32}$"
_QUESTION_ID_PATTERN = r"^rq_[0-9a-f]{32}$"
_BELIEF_ID_PATTERN = r"^blf_[0-9a-f]{32}$"
_METRIC_DIGITS = 12


class K3AcceptanceDisposition(str, Enum):
    ACCEPTED = "accepted"
    PARTIAL_NO_SCIENTIFIC_EXIT = "partial_no_scientific_exit"
    REJECTED_INTEGRITY = "rejected_integrity"
    BLOCKED_EXECUTION = "blocked_execution"


class K3AcceptanceCheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"


class K3AcceptanceCheckKind(str, Enum):
    COMPETING_HYPOTHESES = "competing_hypotheses"
    PREOBSERVATION_CHRONOLOGY = "preobservation_chronology"
    VALID_OBSERVATION_UPDATE_BIJECTION = "valid_observation_update_bijection"
    HIGH_BELIEF_DISCRIMINATION = "high_belief_discrimination"
    BELIEF_LINEAGE = "belief_lineage"
    MECHANISM_CLAIM_GATE = "mechanism_claim_gate"
    NEGATIVE_RESULT_REVISION = "negative_result_revision"
    CONTRADICTION_RETENTION = "contradiction_retention"
    PERSISTENCE_COMPLETENESS = "persistence_completeness"
    TERMINAL_DECISION = "terminal_decision"
    POSITIVE_VALIDATED_UPDATE = "positive_validated_update"


class K3AcceptanceFailureKind(str, Enum):
    SELECTION_ARCHIVE_INVALID = "selection_archive_invalid"
    VALIDATION_ARCHIVE_INVALID = "validation_archive_invalid"
    UPDATE_ARCHIVE_INVALID = "update_archive_invalid"
    EVIDENCE_LEDGER_ARCHIVE_INVALID = "evidence_ledger_archive_invalid"


class MechanismClaimDisposition(str, Enum):
    WITHHELD = "withheld"
    ISSUED = "issued"


class K3TerminalAction(str, Enum):
    CONTINUE_RESEARCH = "continue_research"
    FORK_HYPOTHESIS_SET = "fork_hypothesis_set"
    SEEK_NEW_MEASUREMENT = "seek_new_measurement"
    STOP_AND_ARCHIVE = "stop_and_archive"


class K3AcceptanceCheck(EpistemicModel):
    kind: K3AcceptanceCheckKind
    status: K3AcceptanceCheckStatus
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=32)
    evidence_sha256s: tuple[str, ...] = Field(max_length=512)
    observed_count: int | None = Field(default=None, ge=0)
    required_count: int | None = Field(default=None, ge=0)
    observed_value: float | None = None
    threshold_value: float | None = None

    @model_validator(mode="after")
    def _check_is_canonical_and_finite(self) -> "K3AcceptanceCheck":
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValueError("K3 acceptance reasons must be unique and canonical")
        if self.evidence_sha256s != tuple(sorted(set(self.evidence_sha256s))):
            raise ValueError("K3 acceptance evidence must be unique and canonical")
        numeric = (self.observed_value, self.threshold_value)
        if any(value is not None and not math.isfinite(value) for value in numeric):
            raise ValueError("K3 acceptance metrics must be finite")
        return self

    @property
    def check_sha256(self) -> str:
        return content_sha256(self)


K3_ACCEPTANCE_OUTPUT_SCHEMA_SHA256 = content_sha256(K3AcceptanceCheck.model_json_schema())


class K3AcceptanceScorerManifest(EpistemicModel):
    schema_version: Literal[1] = 1
    scorer_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    scorer_code_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_schema_sha256: str = Field(pattern=_SHA256_PATTERN)
    scorer_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    tool_names: tuple[str, ...] = ()
    tool_policy: Literal["none"] = "none"
    observation_access: Literal["committed_artifacts_only"] = "committed_artifacts_only"
    runtime: Literal["deterministic"] = "deterministic"
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _scorer_is_bounded(self) -> "K3AcceptanceScorerManifest":
        if self.output_schema_sha256 != K3_ACCEPTANCE_OUTPUT_SCHEMA_SHA256:
            raise ValueError("K3 scorer uses another output schema")
        if self.tool_names:
            raise ValueError("K3 scorer cannot receive ambient tools")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class K3AcceptancePolicy(EpistemicModel):
    schema_version: Literal[1] = 1
    policy_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    minimum_active_hypotheses: int = Field(default=3, ge=3, le=64)
    high_belief_probability_floor: float = Field(default=0.2, gt=0.0, lt=0.5)
    minimum_high_belief_pairwise_total_variation: float = Field(default=0.2, gt=0.0, le=1.0)
    mechanism_claim_posterior_floor: float = Field(default=0.8, gt=0.5, le=1.0)
    alternative_exclusion_posterior_ceiling: float = Field(default=0.05, ge=0.0, lt=0.5)
    require_robust_mechanism_claim: Literal[True] = True
    require_positive_validated_update: Literal[True] = True
    require_terminal_decision: Literal[True] = True
    scorer_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _thresholds_are_finite_and_ordered(self) -> "K3AcceptancePolicy":
        numeric = (
            self.high_belief_probability_floor,
            self.minimum_high_belief_pairwise_total_variation,
            self.mechanism_claim_posterior_floor,
            self.alternative_exclusion_posterior_ceiling,
        )
        if any(not math.isfinite(value) for value in numeric):
            raise ValueError("K3 acceptance thresholds must be finite")
        if self.alternative_exclusion_posterior_ceiling >= self.mechanism_claim_posterior_floor:
            raise ValueError("alternative exclusion ceiling must be below mechanism claim floor")
        return self

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class MechanismClaimRecord(EpistemicModel):
    schema_version: Literal[1] = 1
    claim_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    round_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    source_update_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    hypothesis_id: str = Field(pattern=_HYPOTHESIS_ID_PATTERN)
    requested_ceiling: CausalClaimCeiling
    disposition: MechanismClaimDisposition
    claim_artifact_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    evidence_sha256s: tuple[str, ...] = Field(min_length=1, max_length=128)
    decided_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _claim_record_is_canonical(self) -> "MechanismClaimRecord":
        if self.evidence_sha256s != tuple(sorted(set(self.evidence_sha256s))):
            raise ValueError("mechanism claim evidence must be unique and canonical")
        issued = self.disposition is MechanismClaimDisposition.ISSUED
        if issued != (self.claim_artifact_sha256 is not None):
            raise ValueError("only an issued mechanism claim has a claim artifact")
        return self

    @property
    def record_sha256(self) -> str:
        return content_sha256(self)


class K3RevisionMaterialization(EpistemicModel):
    schema_version: Literal[1] = 1
    round_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    source_update_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    directive_sha256: str = Field(pattern=_SHA256_PATTERN)
    revised_hypothesis: HypothesisVersion
    revised_predictions: tuple[Prediction, ...] = Field(max_length=32)
    materialized_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _revised_predictions_are_canonical(self) -> "K3RevisionMaterialization":
        identities = [item.prediction_id for item in self.revised_predictions]
        if identities != sorted(set(identities)):
            raise ValueError("K3 revised predictions must use unique canonical IDs")
        return self

    @property
    def materialization_sha256(self) -> str:
        return content_sha256(self)


class K3TerminalDecision(EpistemicModel):
    schema_version: Literal[1] = 1
    decision_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    final_round_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    action: K3TerminalAction
    source_update_receipt_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    source_world_revision_directive_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=32)
    evidence_sha256s: tuple[str, ...] = Field(min_length=1, max_length=256)
    decided_by_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    decided_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _terminal_decision_is_canonical(self) -> "K3TerminalDecision":
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValueError("terminal decision reasons must be unique and canonical")
        if self.evidence_sha256s != tuple(sorted(set(self.evidence_sha256s))):
            raise ValueError("terminal decision evidence must be unique and canonical")
        update_bound = self.source_update_receipt_sha256 is not None
        if update_bound != (self.source_world_revision_directive_sha256 is not None):
            raise ValueError("terminal decision update and world-revision bindings are inseparable")
        return self

    @property
    def decision_sha256(self) -> str:
        return content_sha256(self)


class K3EvidenceLedger(EpistemicModel):
    schema_version: Literal[1] = 1
    ledger_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    question_id: str = Field(pattern=_QUESTION_ID_PATTERN)
    belief_lineage_id: str = Field(pattern=_BELIEF_ID_PATTERN)
    selection_receipt_sha256s: tuple[str, ...] = Field(min_length=1, max_length=64)
    validation_receipt_sha256s: tuple[str, ...] = Field(min_length=1, max_length=64)
    update_receipt_sha256s: tuple[str, ...] = Field(max_length=64)
    persisted_world_snapshot_sha256s: tuple[str, ...] = Field(min_length=1, max_length=128)
    persisted_belief_state_sha256s: tuple[str, ...] = Field(min_length=1, max_length=128)
    persisted_hypothesis_version_sha256s: tuple[str, ...] = Field(min_length=3, max_length=512)
    persisted_prediction_version_sha256s: tuple[str, ...] = Field(min_length=3, max_length=1024)
    persisted_revision_directive_sha256s: tuple[str, ...] = Field(max_length=512)
    persisted_contradiction_sha256s: tuple[str, ...] = Field(max_length=1024)
    revision_materializations: tuple[K3RevisionMaterialization, ...] = Field(max_length=512)
    mechanism_claims: tuple[MechanismClaimRecord, ...] = Field(max_length=256)
    terminal_decision: K3TerminalDecision
    persistence_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    persisted_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _ledger_collections_are_canonical(self) -> "K3EvidenceLedger":
        set_fields = (
            self.selection_receipt_sha256s,
            self.validation_receipt_sha256s,
            self.update_receipt_sha256s,
            self.persisted_world_snapshot_sha256s,
            self.persisted_belief_state_sha256s,
            self.persisted_hypothesis_version_sha256s,
            self.persisted_prediction_version_sha256s,
            self.persisted_revision_directive_sha256s,
            self.persisted_contradiction_sha256s,
        )
        if any(values != tuple(sorted(set(values))) for values in set_fields):
            raise ValueError("K3 evidence ledger hash collections must be unique and canonical")
        materialization_keys = [
            (item.source_update_receipt_sha256, item.directive_sha256)
            for item in self.revision_materializations
        ]
        if materialization_keys != sorted(set(materialization_keys)):
            raise ValueError("K3 revision materializations must be unique and canonical")
        claim_ids = [item.claim_id for item in self.mechanism_claims]
        if claim_ids != sorted(set(claim_ids)):
            raise ValueError("K3 mechanism claims must use unique canonical IDs")
        if self.terminal_decision.decided_at > self.persisted_at:
            raise ValueError("K3 evidence cannot be persisted before its terminal decision")
        return self

    @property
    def evidence_sha256(self) -> str:
        return content_sha256(self)


class CommittedK3EvidenceLedger(EpistemicModel):
    schema_version: Literal[1] = 1
    evidence: K3EvidenceLedger
    ledger: ArchivedKnowledgeLedger
    committed_at: AwareDatetime

    @model_validator(mode="after")
    def _archive_commits_evidence(self) -> "CommittedK3EvidenceLedger":
        payload = canonical_json_bytes(self.evidence)
        if (
            self.ledger.object_sha256 != self.evidence.evidence_sha256
            or self.ledger.ledger_sha256 != hashlib.sha256(payload).hexdigest()
            or self.ledger.ledger_bytes != len(payload)
            or self.ledger.archived_at != self.committed_at
        ):
            raise ValueError("K3 evidence ledger archive does not commit exact bytes and time")
        if self.committed_at < self.evidence.persisted_at:
            raise ValueError("K3 evidence commitment predates persistence completion")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class K3RoundEvidence(EpistemicModel):
    schema_version: Literal[1] = 1
    round_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    ordinal: int = Field(ge=1, le=64)
    committed_selection: CommittedExperimentSelectionCampaign
    committed_validations: tuple[CommittedObservationValidationCampaign, ...] = Field(
        min_length=1, max_length=1
    )
    committed_updates: tuple[CommittedWorldBeliefUpdateCampaign, ...] = Field(max_length=1)

    @model_validator(mode="after")
    def _round_is_exact_bound(self) -> "K3RoundEvidence":
        selection = self.committed_selection.campaign
        if selection.disposition is not ExperimentSelectionDisposition.READY_SELECTED:
            raise ValueError("K3 acceptance round requires one selected F9-S5 experiment")
        validation = self.committed_validations[0]
        if (
            validation.campaign.request.committed_selection.receipt_sha256
            != self.committed_selection.receipt_sha256
        ):
            raise ValueError("K3 validation attempt belongs to another selection")
        for update in self.committed_updates:
            if update.campaign.request.selection_campaign_sha256 != selection.campaign_sha256:
                raise ValueError("K3 update attempt belongs to another selection")
        return self

    @property
    def round_sha256(self) -> str:
        return content_sha256(self)


class K3AcceptanceRequest(EpistemicModel):
    schema_version: Literal[1] = 1
    acceptance_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    rounds: tuple[K3RoundEvidence, ...] = Field(min_length=1, max_length=64)
    committed_evidence_ledger: CommittedK3EvidenceLedger
    scorer_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    selection_archive_custody_sha256: str = Field(pattern=_SHA256_PATTERN)
    validation_archive_custody_sha256: str = Field(pattern=_SHA256_PATTERN)
    update_archive_custody_sha256: str = Field(pattern=_SHA256_PATTERN)
    evidence_archive_custody_sha256: str = Field(pattern=_SHA256_PATTERN)
    issued_at: AwareDatetime
    observation_access: Literal["committed_artifacts_only"] = "committed_artifacts_only"

    @model_validator(mode="after")
    def _rounds_are_canonical(self) -> "K3AcceptanceRequest":
        ordinals = [item.ordinal for item in self.rounds]
        if ordinals != list(range(1, len(self.rounds) + 1)):
            raise ValueError("K3 acceptance rounds must use contiguous canonical ordinals")
        round_ids = [item.round_id for item in self.rounds]
        if len(round_ids) != len(set(round_ids)):
            raise ValueError("K3 acceptance round IDs must be unique")
        if any(item.committed_selection.committed_at > self.issued_at for item in self.rounds):
            raise ValueError("K3 acceptance request predates a selection commitment")
        if self.committed_evidence_ledger.committed_at > self.issued_at:
            raise ValueError("K3 acceptance request predates its evidence-ledger commitment")
        return self

    @property
    def request_sha256(self) -> str:
        return content_sha256(self)


class K3RoundArchiveVerification(EpistemicModel):
    round_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    selection_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    selection_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    validation_campaign_sha256s: tuple[str, ...] = Field(min_length=1, max_length=1)
    validation_receipt_sha256s: tuple[str, ...] = Field(min_length=1, max_length=1)
    update_campaign_sha256s: tuple[str, ...] = Field(max_length=1)
    update_receipt_sha256s: tuple[str, ...] = Field(max_length=1)
    selection_archive_custody_sha256: str = Field(pattern=_SHA256_PATTERN)
    validation_archive_custody_sha256: str = Field(pattern=_SHA256_PATTERN)
    update_archive_custody_sha256: str = Field(pattern=_SHA256_PATTERN)
    verified_at: AwareDatetime

    @model_validator(mode="after")
    def _verification_lists_align(self) -> "K3RoundArchiveVerification":
        if len(self.validation_campaign_sha256s) != len(self.validation_receipt_sha256s):
            raise ValueError("K3 validation archive verifications do not align")
        if len(self.update_campaign_sha256s) != len(self.update_receipt_sha256s):
            raise ValueError("K3 update archive verifications do not align")
        return self

    @property
    def verification_sha256(self) -> str:
        return content_sha256(self)


class K3EvidenceArchiveVerification(EpistemicModel):
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    evidence_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    ledger_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    archive_custody_sha256: str = Field(pattern=_SHA256_PATTERN)
    verified_at: AwareDatetime

    @property
    def verification_sha256(self) -> str:
        return content_sha256(self)


class K3AcceptanceFailure(EpistemicModel):
    kind: K3AcceptanceFailureKind
    subject_sha256: str = Field(pattern=_SHA256_PATTERN)
    error_class: str = Field(min_length=1, max_length=256)
    error_detail_sha256: str = Field(pattern=_SHA256_PATTERN)
    occurred_at: AwareDatetime

    @property
    def failure_sha256(self) -> str:
        return content_sha256(self)


class _DerivedK3Acceptance(EpistemicModel):
    checks: tuple[K3AcceptanceCheck, ...]
    disposition: K3AcceptanceDisposition


class K3AcceptanceCampaign(EpistemicModel):
    schema_version: Literal[1] = 1
    campaign_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    policy: K3AcceptancePolicy
    scorer_manifest: K3AcceptanceScorerManifest
    request: K3AcceptanceRequest
    round_verifications: tuple[K3RoundArchiveVerification, ...]
    evidence_verification: K3EvidenceArchiveVerification | None = None
    checks: tuple[K3AcceptanceCheck, ...]
    failure: K3AcceptanceFailure | None = None
    disposition: K3AcceptanceDisposition
    generated_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _campaign_is_mechanically_derived(self) -> "K3AcceptanceCampaign":
        _validate_acceptance_request(
            policy=self.policy,
            scorer_manifest=self.scorer_manifest,
            request=self.request,
        )
        if self.failure is not None:
            if (
                self.round_verifications
                or self.evidence_verification is not None
                or self.checks
                or self.disposition is not K3AcceptanceDisposition.BLOCKED_EXECUTION
            ):
                raise ValueError("failed K3 acceptance cannot retain partial scoring evidence")
            if self.failure.occurred_at < self.request.issued_at:
                raise ValueError("K3 acceptance failure predates its request")
            if self.generated_at < self.failure.occurred_at:
                raise ValueError("K3 acceptance campaign predates its failure")
            return self
        if len(self.round_verifications) != len(self.request.rounds):
            raise ValueError("K3 acceptance requires one verification per round")
        for evidence, verification in zip(
            self.request.rounds, self.round_verifications, strict=True
        ):
            _validate_round_verification(
                request=self.request, evidence=evidence, actual=verification
            )
        if self.evidence_verification is None:
            raise ValueError("K3 acceptance requires evidence-ledger verification")
        _validate_evidence_verification(
            request=self.request,
            actual=self.evidence_verification,
        )
        derived = _derive_acceptance(policy=self.policy, request=self.request)
        if self.checks != derived.checks or self.disposition is not derived.disposition:
            raise ValueError("K3 acceptance checks or disposition are not mechanically derived")
        latest = max(
            *(item.verified_at for item in self.round_verifications),
            self.evidence_verification.verified_at,
        )
        if self.generated_at < latest:
            raise ValueError("K3 acceptance campaign predates physical verification")
        return self

    @property
    def campaign_sha256(self) -> str:
        return content_sha256(self)


class CommittedK3AcceptanceCampaign(EpistemicModel):
    schema_version: Literal[1] = 1
    campaign: K3AcceptanceCampaign
    ledger: ArchivedKnowledgeLedger
    committed_at: AwareDatetime

    @model_validator(mode="after")
    def _archive_commits_campaign(self) -> "CommittedK3AcceptanceCampaign":
        payload = canonical_json_bytes(self.campaign)
        if (
            self.ledger.object_sha256 != self.campaign.campaign_sha256
            or self.ledger.ledger_sha256 != hashlib.sha256(payload).hexdigest()
            or self.ledger.ledger_bytes != len(payload)
            or self.ledger.archived_at != self.committed_at
        ):
            raise ValueError("K3 acceptance archive does not commit exact campaign and time")
        if self.committed_at < self.campaign.generated_at:
            raise ValueError("K3 acceptance commitment predates campaign generation")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


def _now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("K3 acceptance clock must return a timezone-aware timestamp")
    return value


def _selected_candidate(selection) -> ExperimentCandidate:
    if selection.decision is None or selection.decision.selected_candidate_id is None:
        raise ValueError("K3 acceptance selection has no selected candidate")
    selected_id = selection.decision.selected_candidate_id
    matches = [item for item in selection.request.candidates if item.candidate_id == selected_id]
    if len(matches) != 1:
        raise ValueError("K3 acceptance selection does not resolve exactly one candidate")
    return matches[0]


def _source_snapshot(round_evidence: K3RoundEvidence) -> WorldModelSnapshot:
    candidate = _selected_candidate(round_evidence.committed_selection.campaign)
    return candidate.committed_prediction.campaign.source_causal_campaign.world_model_snapshot


def _successful_update(
    round_evidence: K3RoundEvidence,
) -> CommittedWorldBeliefUpdateCampaign | None:
    if not round_evidence.committed_updates:
        return None
    update = round_evidence.committed_updates[0]
    if update.campaign.disposition in {
        WorldBeliefUpdateDisposition.UPDATED_ROBUST,
        WorldBeliefUpdateDisposition.UPDATED_FRAGILE,
    }:
        return update
    return None


def _expected_persistence_hashes(
    rounds: tuple[K3RoundEvidence, ...],
    revision_materializations: tuple[K3RevisionMaterialization, ...],
) -> dict[str, tuple[str, ...]]:
    selections = {item.committed_selection.receipt_sha256 for item in rounds}
    validations = {
        validation.receipt_sha256 for item in rounds for validation in item.committed_validations
    }
    updates = {update.receipt_sha256 for item in rounds for update in item.committed_updates}
    snapshots: set[str] = set()
    beliefs: set[str] = set()
    hypotheses: set[str] = set()
    predictions: set[str] = set()
    directives: set[str] = set()
    contradictions: set[str] = set()
    for item in rounds:
        source = _source_snapshot(item)
        snapshots.add(source.snapshot_sha256)
        beliefs.add(source.belief_state.belief_state_sha256)
        hypotheses.update(hypothesis.hypothesis_sha256 for hypothesis in source.hypotheses)
        predictions.update(prediction.prediction_sha256 for prediction in source.predictions)
        for update in item.committed_updates:
            campaign = update.campaign
            if campaign.updated_world_model_snapshot is not None:
                snapshots.add(campaign.updated_world_model_snapshot.snapshot_sha256)
                beliefs.add(campaign.updated_world_model_snapshot.belief_state.belief_state_sha256)
            directives.update(
                directive.directive_sha256 for directive in campaign.hypothesis_revisions
            )
            if campaign.world_revision is not None:
                directives.add(campaign.world_revision.directive_sha256)
            contradictions.update(
                record.contradiction_sha256 for record in campaign.contradiction_queue
            )
    hypotheses.update(
        item.revised_hypothesis.hypothesis_sha256 for item in revision_materializations
    )
    predictions.update(
        prediction.prediction_sha256
        for item in revision_materializations
        for prediction in item.revised_predictions
    )
    return {
        "selection_receipt_sha256s": tuple(sorted(selections)),
        "validation_receipt_sha256s": tuple(sorted(validations)),
        "update_receipt_sha256s": tuple(sorted(updates)),
        "persisted_world_snapshot_sha256s": tuple(sorted(snapshots)),
        "persisted_belief_state_sha256s": tuple(sorted(beliefs)),
        "persisted_hypothesis_version_sha256s": tuple(sorted(hypotheses)),
        "persisted_prediction_version_sha256s": tuple(sorted(predictions)),
        "persisted_revision_directive_sha256s": tuple(sorted(directives)),
        "persisted_contradiction_sha256s": tuple(sorted(contradictions)),
    }


def build_k3_evidence_ledger(
    *,
    ledger_id: str,
    rounds: tuple[K3RoundEvidence, ...],
    revision_materializations: tuple[K3RevisionMaterialization, ...],
    mechanism_claims: tuple[MechanismClaimRecord, ...],
    terminal_decision: K3TerminalDecision,
    persistence_principal_sha256: str,
    persisted_at: datetime,
) -> K3EvidenceLedger:
    if not rounds:
        raise ValueError("K3 evidence ledger requires at least one round")
    source = _source_snapshot(rounds[0])
    hashes = _expected_persistence_hashes(rounds, revision_materializations)
    return K3EvidenceLedger(
        ledger_id=ledger_id,
        question_id=source.question.question_id,
        belief_lineage_id=source.belief_state.belief_lineage_id,
        **hashes,
        revision_materializations=revision_materializations,
        mechanism_claims=mechanism_claims,
        terminal_decision=terminal_decision,
        persistence_principal_sha256=persistence_principal_sha256,
        persisted_at=persisted_at,
    )


def commit_k3_evidence_ledger(
    *,
    archive: ContentAddressedResponseArchive,
    evidence: K3EvidenceLedger,
    committed_at: datetime,
) -> CommittedK3EvidenceLedger:
    if committed_at.tzinfo is None or committed_at.utcoffset() is None:
        raise ValueError("K3 evidence commitment time must be timezone-aware")
    if committed_at < evidence.persisted_at:
        raise ValueError("K3 evidence commitment cannot predate persistence completion")
    ledger = archive.store_ledger(
        value=evidence,
        object_sha256=evidence.evidence_sha256,
        archived_at=committed_at,
    )
    return CommittedK3EvidenceLedger(
        evidence=evidence,
        ledger=ledger,
        committed_at=committed_at,
    )


def load_k3_evidence_ledger(
    *,
    archive: ContentAddressedResponseArchive,
    ledger: ArchivedKnowledgeLedger,
) -> K3EvidenceLedger:
    payload = archive.read_ledger(ledger)
    evidence = K3EvidenceLedger.model_validate_json(payload)
    if canonical_json_bytes(evidence) != payload:
        raise ValueError("archived K3 evidence ledger is not canonical JSON")
    if evidence.evidence_sha256 != ledger.object_sha256:
        raise ValueError("archived K3 evidence ledger changed object identity")
    return evidence


def _scientific_principals(request: K3AcceptanceRequest) -> set[str]:
    principals: set[str] = {
        request.committed_evidence_ledger.evidence.persistence_principal_sha256,
        request.committed_evidence_ledger.evidence.terminal_decision.decided_by_principal_sha256,
    }
    for item in request.rounds:
        selection = item.committed_selection.campaign
        principals.update(
            {
                selection.assessor_manifest.assessor_principal_sha256,
                selection.policy.harness_principal_sha256,
            }
        )
        for candidate in selection.request.candidates:
            prediction = candidate.committed_prediction.campaign
            causal = prediction.source_causal_campaign
            hypothesis = causal.source_campaign
            principals.update(
                {
                    prediction.author_manifest.author_principal_sha256,
                    prediction.calibration_evaluator_manifest.evaluator_principal_sha256,
                    causal.author_manifest.author_principal_sha256,
                    causal.reviewer_manifest.reviewer_principal_sha256,
                    hypothesis.generator_manifest.generator_principal_sha256,
                    hypothesis.deduplicator_manifest.reviewer_principal_sha256,
                }
            )
        for validation in item.committed_validations:
            principals.add(validation.campaign.validator_manifest.validator_principal_sha256)
            principals.add(validation.campaign.policy.harness_principal_sha256)
        for update in item.committed_updates:
            principals.add(update.campaign.policy.harness_principal_sha256)
    return principals


def _validate_acceptance_request(
    *,
    policy: K3AcceptancePolicy,
    scorer_manifest: K3AcceptanceScorerManifest,
    request: K3AcceptanceRequest,
) -> None:
    if request.policy_sha256 != policy.policy_sha256:
        raise ValueError("K3 acceptance request changed its policy binding")
    if request.scorer_manifest_sha256 != scorer_manifest.manifest_sha256:
        raise ValueError("K3 acceptance request changed its scorer binding")
    if policy.scorer_principal_sha256 != scorer_manifest.scorer_principal_sha256:
        raise ValueError("K3 acceptance policy and scorer principal differ")
    earliest_selection = min(item.committed_selection.committed_at for item in request.rounds)
    if policy.frozen_at > earliest_selection or scorer_manifest.frozen_at > earliest_selection:
        raise ValueError("K3 acceptance policy and scorer must be frozen before first selection")
    if scorer_manifest.scorer_principal_sha256 in _scientific_principals(request):
        raise ValueError(
            "K3 acceptance scorer must be independent from scientific/persistence roles"
        )
    evidence = request.committed_evidence_ledger.evidence
    first = _source_snapshot(request.rounds[0])
    if (
        evidence.question_id != first.question.question_id
        or evidence.belief_lineage_id != first.belief_state.belief_lineage_id
    ):
        raise ValueError("K3 evidence ledger belongs to another question or belief lineage")
    latest_commit = max(
        request.committed_evidence_ledger.committed_at,
        *(item.committed_selection.committed_at for item in request.rounds),
        *(
            validation.committed_at
            for item in request.rounds
            for validation in item.committed_validations
        ),
        *(update.committed_at for item in request.rounds for update in item.committed_updates),
    )
    if latest_commit > request.issued_at:
        raise ValueError("K3 acceptance request predates committed evidence")


def build_k3_acceptance_request(
    *,
    acceptance_id: str,
    rounds: tuple[K3RoundEvidence, ...],
    committed_evidence_ledger: CommittedK3EvidenceLedger,
    scorer_manifest: K3AcceptanceScorerManifest,
    policy: K3AcceptancePolicy,
    selection_archive_custody_sha256: str,
    validation_archive_custody_sha256: str,
    update_archive_custody_sha256: str,
    evidence_archive_custody_sha256: str,
    issued_at: datetime,
) -> K3AcceptanceRequest:
    request = K3AcceptanceRequest(
        acceptance_id=acceptance_id,
        rounds=rounds,
        committed_evidence_ledger=committed_evidence_ledger,
        scorer_manifest_sha256=scorer_manifest.manifest_sha256,
        policy_sha256=policy.policy_sha256,
        selection_archive_custody_sha256=selection_archive_custody_sha256,
        validation_archive_custody_sha256=validation_archive_custody_sha256,
        update_archive_custody_sha256=update_archive_custody_sha256,
        evidence_archive_custody_sha256=evidence_archive_custody_sha256,
        issued_at=issued_at,
    )
    _validate_acceptance_request(
        policy=policy,
        scorer_manifest=scorer_manifest,
        request=request,
    )
    return request


def _check(
    kind: K3AcceptanceCheckKind,
    ok: bool | None,
    *,
    pass_reason: str,
    fail_reasons: tuple[str, ...] = (),
    evidence: set[str] | tuple[str, ...] = (),
    observed_count: int | None = None,
    required_count: int | None = None,
    observed_value: float | None = None,
    threshold_value: float | None = None,
) -> K3AcceptanceCheck:
    if ok is None:
        status = K3AcceptanceCheckStatus.NOT_APPLICABLE
        reasons = ("not_applicable",)
    elif ok:
        status = K3AcceptanceCheckStatus.PASS
        reasons = (pass_reason,)
    else:
        status = K3AcceptanceCheckStatus.FAIL
        reasons = fail_reasons or ("requirement_failed",)
    return K3AcceptanceCheck(
        kind=kind,
        status=status,
        reason_codes=tuple(sorted(set(reasons))),
        evidence_sha256s=tuple(sorted(set(evidence))),
        observed_count=observed_count,
        required_count=required_count,
        observed_value=(
            round(observed_value, _METRIC_DIGITS) if observed_value is not None else None
        ),
        threshold_value=(
            round(threshold_value, _METRIC_DIGITS) if threshold_value is not None else None
        ),
    )


def _competing_hypotheses_check(
    *, policy: K3AcceptancePolicy, request: K3AcceptanceRequest
) -> K3AcceptanceCheck:
    failures: set[str] = set()
    evidence: set[str] = set()
    minimum_count = 64
    for item in request.rounds:
        snapshot = _source_snapshot(item)
        evidence.add(snapshot.snapshot_sha256)
        active = [
            hypothesis
            for hypothesis in snapshot.hypotheses
            if hypothesis.lifecycle is not HypothesisLifecycle.RETIRED
        ]
        minimum_count = min(minimum_count, len(active))
        roles = {hypothesis.role for hypothesis in active}
        if len(active) < policy.minimum_active_hypotheses:
            failures.add("active_hypothesis_count_below_minimum")
        if not {
            HypothesisRole.NULL,
            HypothesisRole.PRIMARY,
            HypothesisRole.ALTERNATIVE,
        }.issubset(roles):
            failures.add("required_competing_roles_missing")
        statements = [" ".join(hypothesis.statement.casefold().split()) for hypothesis in active]
        if len(statements) != len(set(statements)):
            failures.add("duplicate_active_hypothesis_statement")
        versions = [hypothesis.hypothesis_sha256 for hypothesis in active]
        if len(versions) != len(set(versions)):
            failures.add("duplicate_active_hypothesis_version")
    ok = not failures
    return _check(
        K3AcceptanceCheckKind.COMPETING_HYPOTHESES,
        ok,
        pass_reason="nonduplicate_competing_active_set_present",
        fail_reasons=tuple(failures),
        evidence=evidence,
        observed_count=minimum_count,
        required_count=policy.minimum_active_hypotheses,
    )


def _chronology_check(request: K3AcceptanceRequest) -> K3AcceptanceCheck:
    failures: set[str] = set()
    evidence: set[str] = set()
    for item in request.rounds:
        selection_commit = item.committed_selection.committed_at
        evidence.add(item.committed_selection.receipt_sha256)
        validation = item.committed_validations[0]
        validation_campaign = validation.campaign
        observation = validation_campaign.request.observation_receipt
        selected = _selected_candidate(item.committed_selection.campaign)
        prediction_commit = selected.committed_prediction.committed_at
        evidence.update(
            {
                selected.committed_prediction.receipt_sha256,
                observation.receipt_sha256,
                validation.receipt_sha256,
            }
        )
        if not (
            prediction_commit < observation.observed_at
            and selection_commit < observation.observed_at
            and observation.observed_at <= observation.staged_at
            and observation.staged_at <= validation_campaign.request.issued_at
            and validation_campaign.request.issued_at
            <= validation_campaign.generated_at
            <= validation.committed_at
        ):
            failures.add(f"round_chronology_invalid:{item.round_id}")
        for update in item.committed_updates:
            evidence.add(update.receipt_sha256)
            if not (
                validation.committed_at
                <= update.campaign.request.issued_at
                <= update.campaign.generated_at
                <= update.committed_at
            ):
                failures.add(f"update_chronology_invalid:{item.round_id}")
    return _check(
        K3AcceptanceCheckKind.PREOBSERVATION_CHRONOLOGY,
        not failures,
        pass_reason="prediction_selection_validation_update_order_preserved",
        fail_reasons=tuple(failures),
        evidence=evidence,
        observed_count=len(request.rounds),
        required_count=len(request.rounds),
    )


def _update_bijection_check(request: K3AcceptanceRequest) -> K3AcceptanceCheck:
    failures: set[str] = set()
    evidence: set[str] = set()
    validated_count = 0
    update_count = 0
    observation_receipts: set[str] = set()
    for item in request.rounds:
        validation = item.committed_validations[0]
        evidence.add(validation.receipt_sha256)
        observation_receipt = validation.campaign.request.observation_receipt.receipt_sha256
        if observation_receipt in observation_receipts:
            failures.add("duplicate_observation_attempt")
        observation_receipts.add(observation_receipt)
        valid = (
            validation.campaign.disposition
            is ObservationValidationDisposition.VALIDATED_CONFIRMATION
        )
        if valid:
            validated_count += 1
        if item.committed_updates:
            update_count += 1
            update = item.committed_updates[0]
            evidence.add(update.receipt_sha256)
            if (
                update.campaign.request.committed_validation.receipt_sha256
                != validation.receipt_sha256
            ):
                failures.add(f"update_validation_rebound:{item.round_id}")
        if valid != bool(item.committed_updates):
            failures.add(f"validated_observation_update_count_mismatch:{item.round_id}")
    if validated_count != update_count:
        failures.add("global_validated_observation_update_count_mismatch")
    return _check(
        K3AcceptanceCheckKind.VALID_OBSERVATION_UPDATE_BIJECTION,
        not failures,
        pass_reason="one_update_attempt_per_validated_observation_only",
        fail_reasons=tuple(failures),
        evidence=evidence,
        observed_count=update_count,
        required_count=validated_count,
    )


def _pairwise_total_variation(left, right) -> float:
    left_map = {item.bin_id: item.probability for item in left.probabilities}
    right_map = {item.bin_id: item.probability for item in right.probabilities}
    if set(left_map) != set(right_map):
        raise ValueError("K3 discrimination compared different outcome spaces")
    return 0.5 * math.fsum(abs(left_map[key] - right_map[key]) for key in sorted(left_map))


def _discrimination_check(
    *, policy: K3AcceptancePolicy, request: K3AcceptanceRequest
) -> K3AcceptanceCheck:
    failures: set[str] = set()
    evidence: set[str] = set()
    round_values: list[float] = []
    for item in request.rounds:
        selection = item.committed_selection.campaign
        selected = _selected_candidate(selection)
        prediction_campaign = selected.committed_prediction.campaign
        snapshot = _source_snapshot(item)
        high_ids = {
            belief.hypothesis_id
            for belief in snapshot.belief_state.hypotheses
            if belief.probability >= policy.high_belief_probability_floor
        }
        evidence.update(
            {
                selection.campaign_sha256,
                snapshot.belief_state.belief_state_sha256,
                prediction_campaign.commitment_sha256,
            }
        )
        if len(high_ids) < 2 or prediction_campaign.prediction_batch is None:
            failures.add(f"fewer_than_two_high_belief_predictions:{item.round_id}")
            round_values.append(0.0)
            continue
        predictions = {
            prediction.hypothesis_id: prediction
            for prediction in prediction_campaign.prediction_batch.predictions
            if prediction.hypothesis_id in high_ids
        }
        if set(predictions) != high_ids:
            failures.add(f"high_belief_prediction_missing:{item.round_id}")
            round_values.append(0.0)
            continue
        maximum = max(
            _pairwise_total_variation(predictions[left], predictions[right])
            for left, right in combinations(sorted(high_ids), 2)
        )
        round_values.append(maximum)
        if maximum < policy.minimum_high_belief_pairwise_total_variation:
            failures.add(f"high_belief_discrimination_below_floor:{item.round_id}")
    observed = min(round_values) if round_values else 0.0
    return _check(
        K3AcceptanceCheckKind.HIGH_BELIEF_DISCRIMINATION,
        not failures,
        pass_reason="selected_experiments_discriminate_high_belief_rivals",
        fail_reasons=tuple(failures),
        evidence=evidence,
        observed_count=len(round_values),
        required_count=len(request.rounds),
        observed_value=observed,
        threshold_value=policy.minimum_high_belief_pairwise_total_variation,
    )


def _belief_lineage_check(request: K3AcceptanceRequest) -> K3AcceptanceCheck:
    failures: set[str] = set()
    evidence: set[str] = set()
    first = _source_snapshot(request.rounds[0])
    expected_question = first.question.question_sha256
    expected_lineage = first.belief_state.belief_lineage_id
    expected_source = first.snapshot_sha256
    for item in request.rounds:
        source = _source_snapshot(item)
        evidence.update({source.snapshot_sha256, source.belief_state.belief_state_sha256})
        if (
            source.question.question_sha256 != expected_question
            or source.belief_state.belief_lineage_id != expected_lineage
            or source.snapshot_sha256 != expected_source
        ):
            failures.add(f"round_source_lineage_disconnected:{item.round_id}")
        successful = _successful_update(item)
        if successful is not None:
            child = successful.campaign.updated_world_model_snapshot
            assert child is not None
            evidence.update({child.snapshot_sha256, child.belief_state.belief_state_sha256})
            if (
                child.belief_state.parent_belief_state_sha256
                != source.belief_state.belief_state_sha256
                or child.belief_state.version != source.belief_state.version + 1
                or child.question != source.question
                or child.hypotheses != source.hypotheses
                or child.assumptions != source.assumptions
                or child.predictions != source.predictions
            ):
                failures.add(f"child_belief_lineage_invalid:{item.round_id}")
            expected_source = child.snapshot_sha256
    return _check(
        K3AcceptanceCheckKind.BELIEF_LINEAGE,
        not failures,
        pass_reason="all_rounds_and_child_beliefs_form_one_exact_lineage",
        fail_reasons=tuple(failures),
        evidence=evidence,
        observed_count=len(request.rounds),
        required_count=len(request.rounds),
    )


_CLAIM_CEILING_ORDER = {
    CausalClaimCeiling.NONE: 0,
    CausalClaimCeiling.DESCRIPTIVE_ONLY: 1,
    CausalClaimCeiling.ASSOCIATION_ONLY: 2,
    CausalClaimCeiling.WITHIN_MODEL_CAUSAL_ONLY: 3,
    CausalClaimCeiling.CAUSAL_CANDIDATE: 4,
}


def _updates_by_receipt(
    request: K3AcceptanceRequest,
) -> dict[str, tuple[K3RoundEvidence, CommittedWorldBeliefUpdateCampaign]]:
    return {
        update.receipt_sha256: (item, update)
        for item in request.rounds
        for update in item.committed_updates
    }


def _mechanism_claim_authorized(
    *,
    policy: K3AcceptancePolicy,
    record: MechanismClaimRecord,
    round_evidence: K3RoundEvidence,
    update: CommittedWorldBeliefUpdateCampaign,
) -> tuple[bool, tuple[str, ...]]:
    reasons: set[str] = set()
    campaign = update.campaign
    audit = campaign.audit
    if (
        campaign.disposition is not WorldBeliefUpdateDisposition.UPDATED_ROBUST
        or audit is None
        or campaign.world_revision is None
    ):
        reasons.add("claim_source_update_not_robust")
        return False, tuple(sorted(reasons))
    source = _source_snapshot(round_evidence)
    hypotheses = {item.hypothesis_id: item for item in source.hypotheses}
    target = hypotheses.get(record.hypothesis_id)
    if target is None or target.role is HypothesisRole.NULL or target.mechanism is None:
        reasons.add("claim_target_is_not_mechanistic_hypothesis")
        return False, tuple(sorted(reasons))
    selected = _selected_candidate(round_evidence.committed_selection.campaign)
    source_ceiling = selected.committed_prediction.campaign.source_causal_campaign.claim_ceiling
    if (
        record.requested_ceiling is CausalClaimCeiling.NONE
        or _CLAIM_CEILING_ORDER[record.requested_ceiling] > _CLAIM_CEILING_ORDER[source_ceiling]
    ):
        reasons.add("claim_exceeds_causal_audit_ceiling")
    update_by_id = {item.hypothesis_id: item for item in audit.hypotheses}
    target_update = update_by_id.get(record.hypothesis_id)
    if target_update is None:
        reasons.add("claim_target_missing_from_posterior")
        return False, tuple(sorted(reasons))
    if record.decided_at < update.committed_at:
        reasons.add("mechanism_claim_predates_source_update")
    if record.source_update_receipt_sha256 not in record.evidence_sha256s:
        reasons.add("claim_evidence_omits_source_update")
    mechanism_level = (
        _CLAIM_CEILING_ORDER[record.requested_ceiling]
        >= _CLAIM_CEILING_ORDER[CausalClaimCeiling.WITHIN_MODEL_CAUSAL_ONLY]
    )
    if mechanism_level:
        if audit.fragile:
            reasons.add("mechanism_claim_posterior_is_fragile")
        if campaign.world_revision.action is not WorldRevisionAction.CONTINUE_CURRENT_SET:
            reasons.add("mechanism_claim_world_set_not_stable")
        target_probabilities = [target_update.posterior_probability]
        alternative_probabilities: dict[str, list[float]] = {
            hypothesis_id: [item.posterior_probability]
            for hypothesis_id, item in update_by_id.items()
            if hypothesis_id != record.hypothesis_id
        }
        for scenario in audit.sensitivity_posteriors:
            scenario_by_id = {
                item.hypothesis_id: item.posterior_probability for item in scenario.hypotheses
            }
            target_probabilities.append(scenario_by_id[record.hypothesis_id])
            for hypothesis_id in alternative_probabilities:
                alternative_probabilities[hypothesis_id].append(scenario_by_id[hypothesis_id])
        if min(target_probabilities) < policy.mechanism_claim_posterior_floor:
            reasons.add("mechanism_posterior_below_floor")
        if any(
            max(probabilities) > policy.alternative_exclusion_posterior_ceiling
            for probabilities in alternative_probabilities.values()
        ):
            reasons.add("alternative_explanation_not_robustly_excluded")
        target_revision = next(
            (
                item
                for item in campaign.hypothesis_revisions
                if item.hypothesis_id == record.hypothesis_id
            ),
            None,
        )
        if target_revision is None or target_revision.action is not HypothesisRevisionAction.RETAIN:
            reasons.add("mechanism_hypothesis_not_retained")
    return not reasons, tuple(sorted(reasons))


def _mechanism_claim_check(
    *, policy: K3AcceptancePolicy, request: K3AcceptanceRequest
) -> K3AcceptanceCheck:
    records = request.committed_evidence_ledger.evidence.mechanism_claims
    if not records:
        return _check(
            K3AcceptanceCheckKind.MECHANISM_CLAIM_GATE,
            None,
            pass_reason="no_mechanism_claim_attempted",
        )
    updates = _updates_by_receipt(request)
    failures: set[str] = set()
    evidence: set[str] = set()
    issued = 0
    for record in records:
        evidence.add(record.record_sha256)
        source = updates.get(record.source_update_receipt_sha256)
        if source is None or source[0].round_id != record.round_id:
            failures.add(f"claim_source_update_missing_or_rebound:{record.claim_id}")
            continue
        round_evidence, update = source
        authorized, reasons = _mechanism_claim_authorized(
            policy=policy,
            record=record,
            round_evidence=round_evidence,
            update=update,
        )
        if record.disposition is MechanismClaimDisposition.ISSUED:
            issued += 1
            if not authorized:
                failures.update(f"{reason}:{record.claim_id}" for reason in reasons)
        else:
            binding_reasons = {
                "mechanism_claim_predates_source_update",
                "claim_evidence_omits_source_update",
            }
            failures.update(
                f"{reason}:{record.claim_id}" for reason in reasons if reason in binding_reasons
            )
    return _check(
        K3AcceptanceCheckKind.MECHANISM_CLAIM_GATE,
        not failures,
        pass_reason="issued_claims_respect_causal_and_exclusion_gates",
        fail_reasons=tuple(failures),
        evidence=evidence,
        observed_count=issued,
        required_count=0,
    )


def revision_materialization_failure_reasons(
    *,
    round_evidence: K3RoundEvidence,
    update: CommittedWorldBeliefUpdateCampaign,
    directive: HypothesisRevisionDirective,
    materialization: K3RevisionMaterialization,
    persisted_at: datetime,
) -> tuple[str, ...]:
    failures: set[str] = set()
    source = _source_snapshot(round_evidence)
    source_hypothesis = next(
        item for item in source.hypotheses if item.hypothesis_id == directive.hypothesis_id
    )
    source_predictions = {
        item.prediction_id: item
        for item in source.predictions
        if item.hypothesis_id == directive.hypothesis_id
    }
    revised = materialization.revised_hypothesis
    if (
        materialization.round_id != round_evidence.round_id
        or materialization.source_update_receipt_sha256 != update.receipt_sha256
        or materialization.directive_sha256 != directive.directive_sha256
    ):
        failures.add("revision_materialization_binding_changed")
    if (
        revised.hypothesis_id != source_hypothesis.hypothesis_id
        or revised.parent_hypothesis_sha256 != source_hypothesis.hypothesis_sha256
        or revised.version != source_hypothesis.version + 1
        or revised.run_id != source_hypothesis.run_id
        or revised.question_id != source_hypothesis.question_id
        or revised.question_version_sha256 != source_hypothesis.question_version_sha256
        or revised.role is not source_hypothesis.role
    ):
        failures.add("revised_hypothesis_lineage_changed")
    if directive.action is HypothesisRevisionAction.NARROW:
        if revised.lifecycle is not HypothesisLifecycle.NARROWED:
            failures.add("narrow_directive_not_materialized_as_narrowed")
        if (
            revised.statement == source_hypothesis.statement
            and revised.mechanism == source_hypothesis.mechanism
            and revised.rationale_sha256 == source_hypothesis.rationale_sha256
        ):
            failures.add("narrow_materialization_has_no_substantive_change")
        revised_predictions = {
            item.prediction_id: item for item in materialization.revised_predictions
        }
        if not source_predictions or set(revised_predictions) != set(source_predictions):
            failures.add("narrow_materialization_prediction_set_not_exact")
        else:
            for prediction_id, source_prediction in source_predictions.items():
                changed = revised_predictions[prediction_id]
                if (
                    changed.parent_prediction_sha256 != source_prediction.prediction_sha256
                    or changed.version != source_prediction.version + 1
                    or changed.run_id != source_prediction.run_id
                    or changed.hypothesis_id != revised.hypothesis_id
                    or changed.hypothesis_version_sha256 != revised.hypothesis_sha256
                ):
                    failures.add("revised_prediction_lineage_changed")
                substantive = (
                    changed.observable_id != source_prediction.observable_id
                    or changed.outcome_space != source_prediction.outcome_space
                    or changed.expected_outcome != source_prediction.expected_outcome
                    or changed.direction is not source_prediction.direction
                    or changed.discriminates_from_hypothesis_ids
                    != source_prediction.discriminates_from_hypothesis_ids
                    or changed.measurement_protocol_sha256
                    != source_prediction.measurement_protocol_sha256
                )
                if not substantive:
                    failures.add("narrow_materialization_only_reworded_without_new_prediction")
                if not (
                    update.committed_at <= changed.frozen_at <= materialization.materialized_at
                ):
                    failures.add("revised_prediction_chronology_invalid")
    elif directive.action is HypothesisRevisionAction.RETIRE:
        if revised.lifecycle is not HypothesisLifecycle.RETIRED:
            failures.add("retire_directive_not_materialized_as_retired")
        if materialization.revised_predictions:
            failures.add("retired_hypothesis_cannot_add_predictions")
    else:
        failures.add("retain_directive_cannot_have_revision_materialization")
    if not (update.committed_at <= materialization.materialized_at <= persisted_at):
        failures.add("revision_materialization_chronology_invalid")
    return tuple(sorted(failures))


def _negative_result_revision_check(request: K3AcceptanceRequest) -> K3AcceptanceCheck:
    ledger = request.committed_evidence_ledger.evidence
    materializations = {
        (item.source_update_receipt_sha256, item.directive_sha256): item
        for item in ledger.revision_materializations
    }
    failures: set[str] = set()
    evidence: set[str] = set()
    negative_count = 0
    for round_evidence in request.rounds:
        update = _successful_update(round_evidence)
        if update is None or update.campaign.audit is None:
            continue
        if not update.campaign.audit.primary_negative_result:
            continue
        negative_count += 1
        evidence.add(update.receipt_sha256)
        source = _source_snapshot(round_evidence)
        primary_id = next(
            item.hypothesis_id for item in source.hypotheses if item.role is HypothesisRole.PRIMARY
        )
        directive = next(
            item
            for item in update.campaign.hypothesis_revisions
            if item.hypothesis_id == primary_id
        )
        evidence.add(directive.directive_sha256)
        if (
            directive.action
            not in {HypothesisRevisionAction.NARROW, HypothesisRevisionAction.RETIRE}
            or not directive.new_version_required
            or not directive.mutation_forbidden
        ):
            failures.add(f"primary_negative_did_not_require_revision:{round_evidence.round_id}")
            continue
        materialization = materializations.get((update.receipt_sha256, directive.directive_sha256))
        if materialization is None:
            failures.add(f"primary_negative_revision_not_materialized:{round_evidence.round_id}")
            continue
        evidence.add(materialization.materialization_sha256)
        failures.update(
            f"{reason}:{round_evidence.round_id}"
            for reason in revision_materialization_failure_reasons(
                round_evidence=round_evidence,
                update=update,
                directive=directive,
                materialization=materialization,
                persisted_at=ledger.persisted_at,
            )
        )
    return _check(
        K3AcceptanceCheckKind.NEGATIVE_RESULT_REVISION,
        None if negative_count == 0 else not failures,
        pass_reason="primary_negative_results_materialized_append_only_change",
        fail_reasons=tuple(failures),
        evidence=evidence,
        observed_count=negative_count,
        required_count=negative_count,
    )


def _contradiction_retention_check(request: K3AcceptanceRequest) -> K3AcceptanceCheck:
    ledger = request.committed_evidence_ledger.evidence
    expected: set[str] = set()
    evidence: set[str] = set()
    failures: set[str] = set()
    for round_evidence in request.rounds:
        for update in round_evidence.committed_updates:
            campaign = update.campaign
            expected.update(item.contradiction_sha256 for item in campaign.contradiction_queue)
            evidence.add(update.receipt_sha256)
            if campaign.audit is None:
                continue
            kinds = {item.kind for item in campaign.contradiction_queue}
            if campaign.audit.primary_negative_result:
                source = _source_snapshot(round_evidence)
                primary_id = next(
                    item.hypothesis_id
                    for item in source.hypotheses
                    if item.role is HypothesisRole.PRIMARY
                )
                if not any(
                    item.kind is ContradictionKind.HYPOTHESIS_PREDICTION_MISS
                    and primary_id in item.hypothesis_ids
                    for item in campaign.contradiction_queue
                ):
                    failures.add(
                        f"primary_negative_contradiction_missing:{round_evidence.round_id}"
                    )
            if campaign.audit.fragile and ContradictionKind.LIKELIHOOD_SENSITIVITY not in kinds:
                failures.add(f"fragility_contradiction_missing:{round_evidence.round_id}")
            if (
                campaign.audit.all_models_low_likelihood
                and ContradictionKind.ALL_MODELS_LOW_LIKELIHOOD not in kinds
            ):
                failures.add(f"all_models_low_contradiction_missing:{round_evidence.round_id}")
            if (
                campaign.audit.realized_outcome_uninformative
                and ContradictionKind.REALIZED_OUTCOME_UNINFORMATIVE not in kinds
            ):
                failures.add(f"uninformative_contradiction_missing:{round_evidence.round_id}")
    persisted = set(ledger.persisted_contradiction_sha256s)
    evidence.update(expected)
    if persisted != expected:
        failures.add("persisted_contradiction_set_not_exact")
    return _check(
        K3AcceptanceCheckKind.CONTRADICTION_RETENTION,
        not failures,
        pass_reason="all_derived_contradictions_persisted_open_and_exact",
        fail_reasons=tuple(failures),
        evidence=evidence,
        observed_count=len(persisted),
        required_count=len(expected),
    )


def _persistence_completeness_check(request: K3AcceptanceRequest) -> K3AcceptanceCheck:
    ledger = request.committed_evidence_ledger.evidence
    expected = _expected_persistence_hashes(request.rounds, ledger.revision_materializations)
    failures: set[str] = set()
    evidence: set[str] = {ledger.evidence_sha256}
    for field_name, expected_values in expected.items():
        actual_values = getattr(ledger, field_name)
        evidence.update(expected_values)
        if actual_values != expected_values:
            failures.add(f"persisted_set_not_exact:{field_name}")
    required_materializations: dict[
        tuple[str, str], tuple[K3RoundEvidence, CommittedWorldBeliefUpdateCampaign, object]
    ] = {}
    for round_evidence in request.rounds:
        for update in round_evidence.committed_updates:
            for directive in update.campaign.hypothesis_revisions:
                if directive.new_version_required:
                    required_materializations[
                        (update.receipt_sha256, directive.directive_sha256)
                    ] = (
                        round_evidence,
                        update,
                        directive,
                    )
    actual_materializations = {
        (item.source_update_receipt_sha256, item.directive_sha256): item
        for item in ledger.revision_materializations
    }
    if set(actual_materializations) != set(required_materializations):
        failures.add("revision_materialization_set_not_exact")
    for key, (round_evidence, update, directive) in required_materializations.items():
        materialization = actual_materializations.get(key)
        if materialization is None:
            continue
        evidence.add(materialization.materialization_sha256)
        failures.update(
            revision_materialization_failure_reasons(
                round_evidence=round_evidence,
                update=update,
                directive=directive,
                materialization=materialization,
                persisted_at=ledger.persisted_at,
            )
        )
    latest_artifact = max(
        *(item.committed_selection.committed_at for item in request.rounds),
        *(
            validation.committed_at
            for item in request.rounds
            for validation in item.committed_validations
        ),
        *(update.committed_at for item in request.rounds for update in item.committed_updates),
    )
    if ledger.persisted_at < latest_artifact:
        failures.add("evidence_persisted_before_latest_attempt")
    if any(record.decided_at > ledger.persisted_at for record in ledger.mechanism_claims):
        failures.add("mechanism_claim_persisted_before_decision")
    return _check(
        K3AcceptanceCheckKind.PERSISTENCE_COMPLETENESS,
        not failures,
        pass_reason="all_attempts_versions_revisions_and_contradictions_persisted",
        fail_reasons=tuple(failures),
        evidence=evidence,
        observed_count=sum(len(getattr(ledger, key)) for key in expected),
        required_count=sum(len(values) for values in expected.values()),
    )


def _terminal_decision_check(request: K3AcceptanceRequest) -> K3AcceptanceCheck:
    ledger = request.committed_evidence_ledger.evidence
    decision = ledger.terminal_decision
    final_round = request.rounds[-1]
    failures: set[str] = set()
    evidence: set[str] = {decision.decision_sha256}
    if decision.final_round_id != final_round.round_id:
        failures.add("terminal_decision_final_round_rebound")
    last_update = _successful_update(final_round)
    latest_commit = max(
        final_round.committed_selection.committed_at,
        *(item.committed_at for item in final_round.committed_validations),
        *(item.committed_at for item in final_round.committed_updates),
    )
    if decision.decided_at < latest_commit:
        failures.add("terminal_decision_predates_final_attempt")
    if last_update is None:
        expected_evidence = final_round.committed_validations[-1].receipt_sha256
        evidence.add(expected_evidence)
        if (
            decision.source_update_receipt_sha256 is not None
            or decision.source_world_revision_directive_sha256 is not None
            or decision.action is not K3TerminalAction.STOP_AND_ARCHIVE
        ):
            failures.add("terminal_action_without_successful_update_invalid")
        if expected_evidence not in decision.evidence_sha256s:
            failures.add("terminal_decision_omits_final_validation_evidence")
    else:
        campaign = last_update.campaign
        assert campaign.world_revision is not None
        expected_receipt = last_update.receipt_sha256
        expected_directive = campaign.world_revision.directive_sha256
        evidence.update({expected_receipt, expected_directive})
        if (
            decision.source_update_receipt_sha256 != expected_receipt
            or decision.source_world_revision_directive_sha256 != expected_directive
        ):
            failures.add("terminal_decision_changed_update_or_world_revision_binding")
        if not {expected_receipt, expected_directive}.issubset(decision.evidence_sha256s):
            failures.add("terminal_decision_omits_update_evidence")
        allowed = {
            WorldRevisionAction.CONTINUE_CURRENT_SET: {
                K3TerminalAction.CONTINUE_RESEARCH,
                K3TerminalAction.STOP_AND_ARCHIVE,
            },
            WorldRevisionAction.FORK_HYPOTHESIS_SET: {
                K3TerminalAction.FORK_HYPOTHESIS_SET,
                K3TerminalAction.STOP_AND_ARCHIVE,
            },
            WorldRevisionAction.SEEK_NEW_MEASUREMENT_OR_STOP: {
                K3TerminalAction.SEEK_NEW_MEASUREMENT,
                K3TerminalAction.STOP_AND_ARCHIVE,
            },
        }[campaign.world_revision.action]
        if decision.action not in allowed:
            failures.add("terminal_action_conflicts_with_world_revision")
    return _check(
        K3AcceptanceCheckKind.TERMINAL_DECISION,
        not failures,
        pass_reason="terminal_action_and_reasons_persisted_after_final_evidence",
        fail_reasons=tuple(failures),
        evidence=evidence,
        observed_count=1,
        required_count=1,
    )


def _positive_update_check(request: K3AcceptanceRequest) -> K3AcceptanceCheck:
    updates = [
        update
        for item in request.rounds
        for update in item.committed_updates
        if update.campaign.disposition
        in {
            WorldBeliefUpdateDisposition.UPDATED_ROBUST,
            WorldBeliefUpdateDisposition.UPDATED_FRAGILE,
        }
    ]
    return _check(
        K3AcceptanceCheckKind.POSITIVE_VALIDATED_UPDATE,
        bool(updates),
        pass_reason="at_least_one_validated_observation_changed_belief",
        fail_reasons=("no_successful_validated_belief_update",),
        evidence={item.receipt_sha256 for item in updates},
        observed_count=len(updates),
        required_count=1,
    )


_SPINE_CHECKS = {
    K3AcceptanceCheckKind.COMPETING_HYPOTHESES,
    K3AcceptanceCheckKind.PREOBSERVATION_CHRONOLOGY,
    K3AcceptanceCheckKind.VALID_OBSERVATION_UPDATE_BIJECTION,
    K3AcceptanceCheckKind.BELIEF_LINEAGE,
    K3AcceptanceCheckKind.MECHANISM_CLAIM_GATE,
    K3AcceptanceCheckKind.NEGATIVE_RESULT_REVISION,
    K3AcceptanceCheckKind.CONTRADICTION_RETENTION,
    K3AcceptanceCheckKind.PERSISTENCE_COMPLETENESS,
    K3AcceptanceCheckKind.TERMINAL_DECISION,
}


def _derive_acceptance(
    *, policy: K3AcceptancePolicy, request: K3AcceptanceRequest
) -> _DerivedK3Acceptance:
    checks = (
        _competing_hypotheses_check(policy=policy, request=request),
        _chronology_check(request),
        _update_bijection_check(request),
        _discrimination_check(policy=policy, request=request),
        _belief_lineage_check(request),
        _mechanism_claim_check(policy=policy, request=request),
        _negative_result_revision_check(request),
        _contradiction_retention_check(request),
        _persistence_completeness_check(request),
        _terminal_decision_check(request),
        _positive_update_check(request),
    )
    if tuple(item.kind for item in checks) != tuple(K3AcceptanceCheckKind):
        raise ValueError("K3 acceptance checks are not in canonical complete order")
    spine_intact = all(
        item.status is not K3AcceptanceCheckStatus.FAIL
        for item in checks
        if item.kind in _SPINE_CHECKS
    )
    scientific_exit = all(
        item.status is K3AcceptanceCheckStatus.PASS
        for item in checks
        if item.kind
        in {
            K3AcceptanceCheckKind.HIGH_BELIEF_DISCRIMINATION,
            K3AcceptanceCheckKind.POSITIVE_VALIDATED_UPDATE,
        }
    )
    if not spine_intact:
        disposition = K3AcceptanceDisposition.REJECTED_INTEGRITY
    elif scientific_exit:
        disposition = K3AcceptanceDisposition.ACCEPTED
    else:
        disposition = K3AcceptanceDisposition.PARTIAL_NO_SCIENTIFIC_EXIT
    return _DerivedK3Acceptance(checks=checks, disposition=disposition)


def _validate_round_verification(
    *,
    request: K3AcceptanceRequest,
    evidence: K3RoundEvidence,
    actual: K3RoundArchiveVerification,
) -> None:
    expected = {
        "round_id": evidence.round_id,
        "selection_campaign_sha256": evidence.committed_selection.campaign.campaign_sha256,
        "selection_receipt_sha256": evidence.committed_selection.receipt_sha256,
        "validation_campaign_sha256s": tuple(
            item.campaign.campaign_sha256 for item in evidence.committed_validations
        ),
        "validation_receipt_sha256s": tuple(
            item.receipt_sha256 for item in evidence.committed_validations
        ),
        "update_campaign_sha256s": tuple(
            item.campaign.campaign_sha256 for item in evidence.committed_updates
        ),
        "update_receipt_sha256s": tuple(item.receipt_sha256 for item in evidence.committed_updates),
        "selection_archive_custody_sha256": request.selection_archive_custody_sha256,
        "validation_archive_custody_sha256": request.validation_archive_custody_sha256,
        "update_archive_custody_sha256": request.update_archive_custody_sha256,
    }
    for field_name, value in expected.items():
        if getattr(actual, field_name) != value:
            raise ValueError(f"K3 round verification changed exact {field_name}")
    if actual.verified_at < request.issued_at:
        raise ValueError("K3 round archive verification predates request")


def _validate_evidence_verification(
    *, request: K3AcceptanceRequest, actual: K3EvidenceArchiveVerification
) -> None:
    committed = request.committed_evidence_ledger
    expected = {
        "evidence_sha256": committed.evidence.evidence_sha256,
        "evidence_receipt_sha256": committed.receipt_sha256,
        "ledger_receipt_sha256": committed.ledger.receipt_sha256,
        "archive_custody_sha256": request.evidence_archive_custody_sha256,
    }
    for field_name, value in expected.items():
        if getattr(actual, field_name) != value:
            raise ValueError(f"K3 evidence verification changed exact {field_name}")
    if actual.verified_at < request.issued_at:
        raise ValueError("K3 evidence archive verification predates request")


def _acceptance_failure(
    *,
    kind: K3AcceptanceFailureKind,
    subject_sha256: str,
    error: Exception,
    occurred_at: datetime,
) -> K3AcceptanceFailure:
    return K3AcceptanceFailure(
        kind=kind,
        subject_sha256=subject_sha256,
        error_class=type(error).__name__,
        error_detail_sha256=hashlib.sha256(str(error).encode("utf-8")).hexdigest(),
        occurred_at=occurred_at,
    )


def _failed_acceptance_campaign(
    *,
    campaign_id: str,
    policy: K3AcceptancePolicy,
    scorer_manifest: K3AcceptanceScorerManifest,
    request: K3AcceptanceRequest,
    failure: K3AcceptanceFailure,
    clock: Callable[[], datetime],
) -> K3AcceptanceCampaign:
    return K3AcceptanceCampaign(
        campaign_id=campaign_id,
        policy=policy,
        scorer_manifest=scorer_manifest,
        request=request,
        round_verifications=(),
        checks=(),
        failure=failure,
        disposition=K3AcceptanceDisposition.BLOCKED_EXECUTION,
        generated_at=_now(clock),
    )


def run_k3_acceptance(
    *,
    campaign_id: str,
    policy: K3AcceptancePolicy,
    scorer_manifest: K3AcceptanceScorerManifest,
    request: K3AcceptanceRequest,
    selection_archive: ContentAddressedResponseArchive,
    validation_archive: ContentAddressedResponseArchive,
    update_archive: ContentAddressedResponseArchive,
    evidence_archive: ContentAddressedResponseArchive,
    clock: Callable[[], datetime] | None = None,
) -> K3AcceptanceCampaign:
    """Physically verify and independently score a committed F9/K3 evidence chain."""

    clock = clock or (lambda: datetime.now(timezone.utc))
    _validate_acceptance_request(
        policy=policy,
        scorer_manifest=scorer_manifest,
        request=request,
    )
    verifications: list[K3RoundArchiveVerification] = []
    for round_evidence in request.rounds:
        selection = round_evidence.committed_selection
        try:
            loaded_selection = load_experiment_selection_campaign(
                archive=selection_archive,
                ledger=selection.ledger,
            )
            if loaded_selection != selection.campaign:
                raise ValueError("embedded F9-S5 campaign differs from archived bytes")
        except (ResponseArchiveError, ValidationError, ValueError, TypeError) as exc:
            failure = _acceptance_failure(
                kind=K3AcceptanceFailureKind.SELECTION_ARCHIVE_INVALID,
                subject_sha256=selection.receipt_sha256,
                error=exc,
                occurred_at=_now(clock),
            )
            return _failed_acceptance_campaign(
                campaign_id=campaign_id,
                policy=policy,
                scorer_manifest=scorer_manifest,
                request=request,
                failure=failure,
                clock=clock,
            )
        try:
            for validation in round_evidence.committed_validations:
                loaded_validation = load_observation_validation_campaign(
                    archive=validation_archive,
                    ledger=validation.ledger,
                )
                if loaded_validation != validation.campaign:
                    raise ValueError("embedded F9-S6 validation differs from archived bytes")
        except (ResponseArchiveError, ValidationError, ValueError, TypeError) as exc:
            failure = _acceptance_failure(
                kind=K3AcceptanceFailureKind.VALIDATION_ARCHIVE_INVALID,
                subject_sha256=round_evidence.committed_validations[0].receipt_sha256,
                error=exc,
                occurred_at=_now(clock),
            )
            return _failed_acceptance_campaign(
                campaign_id=campaign_id,
                policy=policy,
                scorer_manifest=scorer_manifest,
                request=request,
                failure=failure,
                clock=clock,
            )
        try:
            for update in round_evidence.committed_updates:
                loaded_update = load_world_belief_update_campaign(
                    archive=update_archive,
                    ledger=update.ledger,
                )
                if loaded_update != update.campaign:
                    raise ValueError("embedded F9-S6 update differs from archived bytes")
        except (ResponseArchiveError, ValidationError, ValueError, TypeError) as exc:
            failure = _acceptance_failure(
                kind=K3AcceptanceFailureKind.UPDATE_ARCHIVE_INVALID,
                subject_sha256=(
                    round_evidence.committed_updates[0].receipt_sha256
                    if round_evidence.committed_updates
                    else round_evidence.round_sha256
                ),
                error=exc,
                occurred_at=_now(clock),
            )
            return _failed_acceptance_campaign(
                campaign_id=campaign_id,
                policy=policy,
                scorer_manifest=scorer_manifest,
                request=request,
                failure=failure,
                clock=clock,
            )
        verifications.append(
            K3RoundArchiveVerification(
                round_id=round_evidence.round_id,
                selection_campaign_sha256=loaded_selection.campaign_sha256,
                selection_receipt_sha256=selection.receipt_sha256,
                validation_campaign_sha256s=tuple(
                    item.campaign.campaign_sha256 for item in round_evidence.committed_validations
                ),
                validation_receipt_sha256s=tuple(
                    item.receipt_sha256 for item in round_evidence.committed_validations
                ),
                update_campaign_sha256s=tuple(
                    item.campaign.campaign_sha256 for item in round_evidence.committed_updates
                ),
                update_receipt_sha256s=tuple(
                    item.receipt_sha256 for item in round_evidence.committed_updates
                ),
                selection_archive_custody_sha256=request.selection_archive_custody_sha256,
                validation_archive_custody_sha256=request.validation_archive_custody_sha256,
                update_archive_custody_sha256=request.update_archive_custody_sha256,
                verified_at=_now(clock),
            )
        )
    committed_evidence = request.committed_evidence_ledger
    try:
        loaded_evidence = load_k3_evidence_ledger(
            archive=evidence_archive,
            ledger=committed_evidence.ledger,
        )
        if loaded_evidence != committed_evidence.evidence:
            raise ValueError("embedded K3 evidence ledger differs from archived bytes")
    except (ResponseArchiveError, ValidationError, ValueError, TypeError) as exc:
        failure = _acceptance_failure(
            kind=K3AcceptanceFailureKind.EVIDENCE_LEDGER_ARCHIVE_INVALID,
            subject_sha256=committed_evidence.receipt_sha256,
            error=exc,
            occurred_at=_now(clock),
        )
        return _failed_acceptance_campaign(
            campaign_id=campaign_id,
            policy=policy,
            scorer_manifest=scorer_manifest,
            request=request,
            failure=failure,
            clock=clock,
        )
    evidence_verification = K3EvidenceArchiveVerification(
        evidence_sha256=loaded_evidence.evidence_sha256,
        evidence_receipt_sha256=committed_evidence.receipt_sha256,
        ledger_receipt_sha256=committed_evidence.ledger.receipt_sha256,
        archive_custody_sha256=request.evidence_archive_custody_sha256,
        verified_at=_now(clock),
    )
    derived = _derive_acceptance(policy=policy, request=request)
    return K3AcceptanceCampaign(
        campaign_id=campaign_id,
        policy=policy,
        scorer_manifest=scorer_manifest,
        request=request,
        round_verifications=tuple(verifications),
        evidence_verification=evidence_verification,
        checks=derived.checks,
        disposition=derived.disposition,
        generated_at=_now(clock),
    )


def commit_k3_acceptance_campaign(
    *,
    archive: ContentAddressedResponseArchive,
    campaign: K3AcceptanceCampaign,
    committed_at: datetime,
) -> CommittedK3AcceptanceCampaign:
    if committed_at.tzinfo is None or committed_at.utcoffset() is None:
        raise ValueError("K3 acceptance commitment time must be timezone-aware")
    if committed_at < campaign.generated_at:
        raise ValueError("K3 acceptance commitment cannot predate campaign generation")
    ledger = archive.store_ledger(
        value=campaign,
        object_sha256=campaign.campaign_sha256,
        archived_at=committed_at,
    )
    return CommittedK3AcceptanceCampaign(
        campaign=campaign,
        ledger=ledger,
        committed_at=committed_at,
    )


def load_k3_acceptance_campaign(
    *,
    archive: ContentAddressedResponseArchive,
    ledger: ArchivedKnowledgeLedger,
) -> K3AcceptanceCampaign:
    payload = archive.read_ledger(ledger)
    campaign = K3AcceptanceCampaign.model_validate_json(payload)
    if canonical_json_bytes(campaign) != payload:
        raise ValueError("archived K3 acceptance campaign is not canonical JSON")
    if campaign.campaign_sha256 != ledger.object_sha256:
        raise ValueError("archived K3 acceptance campaign changed object identity")
    return campaign


__all__ = [
    "K3_ACCEPTANCE_OUTPUT_SCHEMA_SHA256",
    "CommittedK3AcceptanceCampaign",
    "CommittedK3EvidenceLedger",
    "K3AcceptanceCampaign",
    "K3AcceptanceCheck",
    "K3AcceptanceCheckKind",
    "K3AcceptanceCheckStatus",
    "K3AcceptanceDisposition",
    "K3AcceptanceFailure",
    "K3AcceptanceFailureKind",
    "K3AcceptancePolicy",
    "K3AcceptanceRequest",
    "K3AcceptanceScorerManifest",
    "K3EvidenceArchiveVerification",
    "K3EvidenceLedger",
    "K3RevisionMaterialization",
    "K3RoundArchiveVerification",
    "K3RoundEvidence",
    "K3TerminalAction",
    "K3TerminalDecision",
    "MechanismClaimDisposition",
    "MechanismClaimRecord",
    "build_k3_acceptance_request",
    "build_k3_evidence_ledger",
    "commit_k3_acceptance_campaign",
    "commit_k3_evidence_ledger",
    "load_k3_acceptance_campaign",
    "load_k3_evidence_ledger",
    "revision_materialization_failure_reasons",
    "run_k3_acceptance",
]
