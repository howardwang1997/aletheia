"""F9-S6 validated-observation belief update and immutable revision policy.

Raw observations terminate at an independently manifested validator.  The updater consumes only a
committed validation campaign, recomputes Bayesian posteriors from the exact F9-S1 prior and F9-S4
likelihoods, audits likelihood sensitivity, and emits append-only revision/contradiction directives.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, ValidationError, model_validator

from aletheia.epistemics.prediction import (
    ObservationStagingError,
    ObservationStagingReceipt,
    ObservationStagingStore,
    PredictionCommitmentCampaign,
    load_prediction_commitment_campaign,
)
from aletheia.epistemics.schemas import (
    BeliefState,
    BeliefUpdateKind,
    EpistemicModel,
    HypothesisBelief,
    HypothesisLifecycle,
    HypothesisRole,
    WorldModelSnapshot,
)
from aletheia.epistemics.selector import (
    CommittedExperimentSelectionCampaign,
    ExperimentCandidate,
    ExperimentSelectionCampaign,
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
_METRIC_DIGITS = 12


class ObservationValidationRuntime(str, Enum):
    DETERMINISTIC = "deterministic"
    MODEL = "model"


class ObservationDataRole(str, Enum):
    CONFIRMATION = "confirmation"
    EXPLORATION = "exploration"
    CALIBRATION = "calibration"


class ProtocolAdherenceStatus(str, Enum):
    EXACT = "exact"
    WITHIN_PREREGISTERED_TOLERANCE = "within_preregistered_tolerance"
    MATERIAL_DEVIATION = "material_deviation"
    UNKNOWN = "unknown"


class ObservationAuditStatus(str, Enum):
    RESOLVED_ACCEPT = "resolved_accept"
    RESOLVED_REJECT = "resolved_reject"
    UNRESOLVED = "unresolved"


class ObservationValidationDisposition(str, Enum):
    VALIDATED_CONFIRMATION = "validated_confirmation"
    REJECTED_SCIENTIFIC = "rejected_scientific"
    BLOCKED_EXECUTION = "blocked_execution"


class ObservationValidationFailureKind(str, Enum):
    SELECTION_ARCHIVE_INVALID = "selection_archive_invalid"
    PREDICTION_ARCHIVE_INVALID = "prediction_archive_invalid"
    OBSERVATION_STORE_INVALID = "observation_store_invalid"
    VALIDATOR_EXCEPTION = "validator_exception"
    INVALID_OUTPUT = "invalid_output"


class ProtocolDeviation(EpistemicModel):
    deviation_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    governing_rule_sha256: str = Field(pattern=_SHA256_PATTERN)
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    within_preregistered_tolerance: bool
    material: bool

    @model_validator(mode="after")
    def _material_deviation_cannot_be_within_tolerance(self) -> "ProtocolDeviation":
        if self.material and self.within_preregistered_tolerance:
            raise ValueError("material protocol deviation cannot be within tolerance")
        return self


class ObservationValidationBatch(EpistemicModel):
    schema_version: Literal[1] = 1
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    selection_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    selection_commitment_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    selected_candidate_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    prediction_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_commitment_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    confirmation_batch_sha256: str = Field(pattern=_SHA256_PATTERN)
    confirmation_partition_sha256: str = Field(pattern=_SHA256_PATTERN)
    outcome_bin_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    sample_count: int = Field(ge=1, le=1_000_000_000)
    data_role: ObservationDataRole
    experiment_identity_verified: bool
    custody_chain_verified: bool
    measurement_valid: bool
    blinding_intact: bool
    protocol_adherence: ProtocolAdherenceStatus
    protocol_deviations: tuple[ProtocolDeviation, ...] = Field(max_length=128)
    small_sample_update_rule_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    observation_parser_sha256: str = Field(pattern=_SHA256_PATTERN)
    analysis_plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    measurement_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    measurement_error_model_sha256: str = Field(pattern=_SHA256_PATTERN)
    analysis_execution_sha256: str = Field(pattern=_SHA256_PATTERN)
    parser_execution_sha256: str = Field(pattern=_SHA256_PATTERN)
    audit_status: ObservationAuditStatus
    evidence_sha256s: tuple[str, ...] = Field(min_length=1, max_length=256)
    validator_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    completed_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _batch_is_canonical_and_protocol_coherent(self) -> "ObservationValidationBatch":
        if self.evidence_sha256s != tuple(sorted(set(self.evidence_sha256s))):
            raise ValueError("observation validation evidence must be unique and canonical")
        deviation_ids = [item.deviation_id for item in self.protocol_deviations]
        if deviation_ids != sorted(set(deviation_ids)):
            raise ValueError("protocol deviations must use unique canonical IDs")
        if self.protocol_adherence is ProtocolAdherenceStatus.EXACT:
            if self.protocol_deviations:
                raise ValueError("exact protocol adherence cannot contain deviations")
        elif self.protocol_adherence is ProtocolAdherenceStatus.WITHIN_PREREGISTERED_TOLERANCE:
            if not self.protocol_deviations or any(
                item.material or not item.within_preregistered_tolerance
                for item in self.protocol_deviations
            ):
                raise ValueError("within-tolerance status requires only bounded deviations")
        elif self.protocol_adherence is ProtocolAdherenceStatus.MATERIAL_DEVIATION:
            if not self.protocol_deviations or not any(
                item.material or not item.within_preregistered_tolerance
                for item in self.protocol_deviations
            ):
                raise ValueError(
                    "material-deviation status requires an outside-tolerance deviation"
                )
        elif self.protocol_deviations:
            raise ValueError("unknown protocol adherence cannot claim classified deviations")
        return self

    @property
    def batch_sha256(self) -> str:
        return content_sha256(self)


OBSERVATION_VALIDATION_OUTPUT_SCHEMA_SHA256 = content_sha256(
    ObservationValidationBatch.model_json_schema()
)


class ObservationValidatorManifest(EpistemicModel):
    schema_version: Literal[1] = 1
    validator_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    runtime: ObservationValidationRuntime
    adapter_code_sha256: str = Field(pattern=_SHA256_PATTERN)
    parser_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_schema_sha256: str = Field(pattern=_SHA256_PATTERN)
    validator_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    instruction_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    model_identity_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    tool_names: tuple[str, ...] = ()
    tool_policy: Literal["none"] = "none"
    observation_access: Literal["exact_staged_payload_only"] = "exact_staged_payload_only"
    transport_policy: Literal["none", "model_transport_only"]
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _validator_is_frozen_and_bounded(self) -> "ObservationValidatorManifest":
        if self.output_schema_sha256 != OBSERVATION_VALIDATION_OUTPUT_SCHEMA_SHA256:
            raise ValueError("observation validator uses another output schema")
        if self.tool_names:
            raise ValueError("observation validator cannot receive ambient tool authority")
        model_fields = (
            self.instruction_sha256 is not None and self.model_identity_sha256 is not None
        )
        if self.runtime is ObservationValidationRuntime.MODEL:
            if not model_fields or self.transport_policy != "model_transport_only":
                raise ValueError("model observation validator requires frozen model transport")
        elif (
            self.instruction_sha256 is not None
            or self.model_identity_sha256 is not None
            or self.transport_policy != "none"
        ):
            raise ValueError("deterministic observation validator cannot declare model transport")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class ObservationValidationPolicy(EpistemicModel):
    schema_version: Literal[1] = 1
    policy_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    minimum_confirmatory_sample_count: int = Field(default=30, ge=1, le=1_000_000_000)
    small_sample_update_rule_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    require_confirmation_role: Literal[True] = True
    require_experiment_identity: Literal[True] = True
    require_custody_chain: Literal[True] = True
    require_valid_measurement: Literal[True] = True
    require_blinding_intact: Literal[True] = True
    allow_preregistered_tolerance: Literal[True] = True
    require_resolved_accept_audit: Literal[True] = True
    harness_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class ObservationValidationRequest(EpistemicModel):
    schema_version: Literal[1] = 1
    validation_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    committed_selection: CommittedExperimentSelectionCampaign
    selected_candidate_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    observation_receipt: ObservationStagingReceipt
    validator_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    selection_archive_custody_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_archive_custody_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation_store_custody_sha256: str = Field(pattern=_SHA256_PATTERN)
    issued_at: AwareDatetime
    observation_access: Literal["exact_staged_payload_only"] = "exact_staged_payload_only"

    @property
    def request_sha256(self) -> str:
        return content_sha256(self)


class ObservationValidationProbe(EpistemicModel):
    batch_sha256: str = Field(pattern=_SHA256_PATTERN)
    outcome_bin_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    sample_count: int = Field(ge=1, le=1_000_000_000)
    blockers: tuple[str, ...]
    valid_for_belief_update: bool

    @model_validator(mode="after")
    def _validity_is_derived_from_blockers(self) -> "ObservationValidationProbe":
        if self.blockers != tuple(sorted(set(self.blockers))):
            raise ValueError("observation validation blockers must be unique and canonical")
        if self.valid_for_belief_update != (not self.blockers):
            raise ValueError("observation update validity must be exactly derived from blockers")
        return self

    @property
    def probe_sha256(self) -> str:
        return content_sha256(self)


class SelectionArchiveVerification(EpistemicModel):
    selection_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    selection_commitment_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    ledger_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    archive_custody_sha256: str = Field(pattern=_SHA256_PATTERN)
    verified_at: AwareDatetime

    @property
    def verification_sha256(self) -> str:
        return content_sha256(self)


class SelectedPredictionArchiveVerification(EpistemicModel):
    candidate_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    prediction_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_commitment_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_commitment_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    ledger_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    archive_custody_sha256: str = Field(pattern=_SHA256_PATTERN)
    verified_at: AwareDatetime

    @property
    def verification_sha256(self) -> str:
        return content_sha256(self)


class RawObservationVerification(EpistemicModel):
    observation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation_bytes: int = Field(ge=1, le=64 * 1024 * 1024)
    namespace_seal_sha256: str = Field(pattern=_SHA256_PATTERN)
    store_custody_sha256: str = Field(pattern=_SHA256_PATTERN)
    verified_at: AwareDatetime

    @property
    def verification_sha256(self) -> str:
        return content_sha256(self)


class ObservationValidationFailure(EpistemicModel):
    kind: ObservationValidationFailureKind
    error_class: str = Field(min_length=1, max_length=256)
    error_detail_sha256: str = Field(pattern=_SHA256_PATTERN)
    raw_output_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    occurred_at: AwareDatetime

    @property
    def failure_sha256(self) -> str:
        return content_sha256(self)


class ObservationValidationCampaign(EpistemicModel):
    schema_version: Literal[1] = 1
    campaign_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    policy: ObservationValidationPolicy
    validator_manifest: ObservationValidatorManifest
    request: ObservationValidationRequest
    selection_verification: SelectionArchiveVerification | None = None
    prediction_verification: SelectedPredictionArchiveVerification | None = None
    observation_verification: RawObservationVerification | None = None
    validation_batch: ObservationValidationBatch | None = None
    probe: ObservationValidationProbe | None = None
    failure: ObservationValidationFailure | None = None
    blockers: tuple[str, ...]
    disposition: ObservationValidationDisposition
    generated_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _campaign_is_mechanically_derived(self) -> "ObservationValidationCampaign":
        _validate_observation_request(
            policy=self.policy,
            validator_manifest=self.validator_manifest,
            request=self.request,
        )
        if self.failure is not None:
            expected = (f"execution_failure:{self.failure.kind.value}",)
            if (
                self.selection_verification is not None
                or self.prediction_verification is not None
                or self.observation_verification is not None
                or self.validation_batch is not None
                or self.probe is not None
                or self.blockers != expected
                or self.disposition is not ObservationValidationDisposition.BLOCKED_EXECUTION
            ):
                raise ValueError("failed observation validation outputs are not derived")
            if self.failure.occurred_at < self.request.issued_at:
                raise ValueError("observation validation failure predates its request")
            if self.generated_at < self.failure.occurred_at:
                raise ValueError("observation validation campaign predates its failure")
            return self
        if (
            self.selection_verification is None
            or self.prediction_verification is None
            or self.observation_verification is None
            or self.validation_batch is None
            or self.probe is None
        ):
            raise ValueError("successful observation validation requires complete evidence")
        _validate_physical_verifications(
            request=self.request,
            selection=self.selection_verification,
            prediction=self.prediction_verification,
            observation=self.observation_verification,
        )
        _validate_observation_batch(
            policy=self.policy,
            validator_manifest=self.validator_manifest,
            request=self.request,
            batch=self.validation_batch,
        )
        expected_probe = _derive_observation_probe(
            policy=self.policy,
            request=self.request,
            batch=self.validation_batch,
        )
        expected_disposition = (
            ObservationValidationDisposition.VALIDATED_CONFIRMATION
            if expected_probe.valid_for_belief_update
            else ObservationValidationDisposition.REJECTED_SCIENTIFIC
        )
        if self.probe != expected_probe or self.blockers != expected_probe.blockers:
            raise ValueError("observation validation probe is not mechanically derived")
        if self.disposition is not expected_disposition:
            raise ValueError("observation validation disposition is not mechanically derived")
        latest = max(
            self.selection_verification.verified_at,
            self.prediction_verification.verified_at,
            self.observation_verification.verified_at,
            self.validation_batch.completed_at,
        )
        if self.generated_at < latest:
            raise ValueError("observation validation campaign predates its evidence")
        return self

    @property
    def campaign_sha256(self) -> str:
        return content_sha256(self)


class CommittedObservationValidationCampaign(EpistemicModel):
    schema_version: Literal[1] = 1
    campaign: ObservationValidationCampaign
    ledger: ArchivedKnowledgeLedger
    committed_at: AwareDatetime

    @model_validator(mode="after")
    def _ledger_commits_campaign(self) -> "CommittedObservationValidationCampaign":
        payload = canonical_json_bytes(self.campaign)
        if (
            self.ledger.object_sha256 != self.campaign.campaign_sha256
            or self.ledger.ledger_sha256 != hashlib.sha256(payload).hexdigest()
            or self.ledger.ledger_bytes != len(payload)
            or self.ledger.archived_at != self.committed_at
        ):
            raise ValueError("observation validation ledger does not commit its campaign and time")
        if self.committed_at < self.campaign.generated_at:
            raise ValueError("observation validation commitment predates campaign generation")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class ObservationValidatorAdapter(Protocol):
    @property
    def manifest(self) -> ObservationValidatorManifest: ...

    async def validate(
        self,
        *,
        request: ObservationValidationRequest,
        raw_observation: bytes,
    ) -> object: ...


def _selected_candidate(selection: ExperimentSelectionCampaign) -> ExperimentCandidate:
    if (
        selection.disposition is not ExperimentSelectionDisposition.READY_SELECTED
        or selection.decision is None
        or selection.decision.selected_candidate_id is None
    ):
        raise ValueError("observation validation requires a ready selected experiment")
    selected_id = selection.decision.selected_candidate_id
    candidate = next(
        (item for item in selection.request.candidates if item.candidate_id == selected_id),
        None,
    )
    if candidate is None:
        raise ValueError("selected experiment is absent from its selection request")
    return candidate


def _selected_assessment(selection: ExperimentSelectionCampaign):
    candidate = _selected_candidate(selection)
    return next(
        item
        for item in selection.request.assessment_batch.assessments
        if item.candidate_id == candidate.candidate_id
    )


def _outcome_measurement_process(campaign: PredictionCommitmentCampaign):
    causal = campaign.source_causal_campaign
    if causal.contract_batch is None:
        raise ValueError("selected prediction lacks a causal contract")
    contract = causal.contract_batch.contract
    process = next(
        (
            item
            for item in contract.measurement_processes
            if item.process_id == contract.outcome_measurement_process_id
        ),
        None,
    )
    if process is None:
        raise ValueError("selected prediction lacks its outcome measurement process")
    return process


def _validate_validator_independence(
    *,
    validator: ObservationValidatorManifest,
    selection: ExperimentSelectionCampaign,
) -> None:
    forbidden_principals = {
        selection.assessor_manifest.assessor_principal_sha256,
    }
    forbidden_models: set[str] = set()
    if selection.assessor_manifest.model_identity_sha256 is not None:
        forbidden_models.add(selection.assessor_manifest.model_identity_sha256)
    for candidate in selection.request.candidates:
        campaign = candidate.committed_prediction.campaign
        causal = campaign.source_causal_campaign
        hypothesis = causal.source_campaign
        forbidden_principals.update(
            {
                campaign.author_manifest.author_principal_sha256,
                campaign.calibration_evaluator_manifest.evaluator_principal_sha256,
                causal.author_manifest.author_principal_sha256,
                causal.reviewer_manifest.reviewer_principal_sha256,
                hypothesis.generator_manifest.generator_principal_sha256,
                hypothesis.deduplicator_manifest.reviewer_principal_sha256,
            }
        )
        for identity in (
            campaign.author_manifest.model_identity_sha256,
            campaign.calibration_evaluator_manifest.model_identity_sha256,
            causal.author_manifest.model_identity_sha256,
            causal.reviewer_manifest.model_identity_sha256,
            hypothesis.generator_manifest.model_identity_sha256,
            hypothesis.deduplicator_manifest.model_identity_sha256,
        ):
            if identity is not None:
                forbidden_models.add(identity)
    if validator.validator_principal_sha256 in forbidden_principals:
        raise ValueError("observation validator must be independent from prior scientific roles")
    if (
        validator.model_identity_sha256 is not None
        and validator.model_identity_sha256 in forbidden_models
    ):
        raise ValueError("observation validator must use an independent model identity")


def _validate_observation_request(
    *,
    policy: ObservationValidationPolicy,
    validator_manifest: ObservationValidatorManifest,
    request: ObservationValidationRequest,
) -> None:
    if request.policy_sha256 != policy.policy_sha256:
        raise ValueError("observation validation request changed its policy binding")
    if request.validator_manifest_sha256 != validator_manifest.manifest_sha256:
        raise ValueError("observation validation request changed its validator binding")
    selection = request.committed_selection.campaign
    candidate = _selected_candidate(selection)
    if request.selected_candidate_id != candidate.candidate_id:
        raise ValueError("observation validation request changed the selected candidate")
    prediction = candidate.committed_prediction
    campaign = prediction.campaign
    protocol = campaign.request.experiment_protocol
    receipt = request.observation_receipt
    expected_receipt = {
        "experiment_namespace_sha256": protocol.experiment_namespace_sha256,
        "experiment_protocol_sha256": protocol.protocol_sha256,
        "commitment_sha256": campaign.commitment_sha256,
        "prediction_campaign_sha256": campaign.campaign_sha256,
        "prediction_commitment_receipt_sha256": prediction.receipt_sha256,
    }
    for field_name, expected_value in expected_receipt.items():
        if getattr(receipt, field_name) != expected_value:
            raise ValueError(f"observation receipt changed exact {field_name} binding")
    if request.committed_selection.committed_at >= receipt.observed_at:
        raise ValueError("experiment selection must be committed before observation")
    if receipt.staged_at > request.issued_at:
        raise ValueError("observation validation request predates observation staging")
    if policy.frozen_at > request.committed_selection.committed_at:
        raise ValueError("observation validation policy was not frozen before selection")
    if validator_manifest.frozen_at > request.committed_selection.committed_at:
        raise ValueError("observation validator was not frozen before selection")
    _validate_validator_independence(validator=validator_manifest, selection=selection)


def build_observation_validation_request(
    *,
    validation_id: str,
    committed_selection: CommittedExperimentSelectionCampaign,
    observation_receipt: ObservationStagingReceipt,
    validator_manifest: ObservationValidatorManifest,
    policy: ObservationValidationPolicy,
    selection_archive_custody_sha256: str,
    prediction_archive_custody_sha256: str,
    observation_store_custody_sha256: str,
    issued_at: datetime,
) -> ObservationValidationRequest:
    candidate = _selected_candidate(committed_selection.campaign)
    request = ObservationValidationRequest(
        validation_id=validation_id,
        committed_selection=committed_selection,
        selected_candidate_id=candidate.candidate_id,
        observation_receipt=observation_receipt,
        validator_manifest_sha256=validator_manifest.manifest_sha256,
        policy_sha256=policy.policy_sha256,
        selection_archive_custody_sha256=selection_archive_custody_sha256,
        prediction_archive_custody_sha256=prediction_archive_custody_sha256,
        observation_store_custody_sha256=observation_store_custody_sha256,
        issued_at=issued_at,
    )
    _validate_observation_request(
        policy=policy,
        validator_manifest=validator_manifest,
        request=request,
    )
    return request


def _validate_observation_batch(
    *,
    policy: ObservationValidationPolicy,
    validator_manifest: ObservationValidatorManifest,
    request: ObservationValidationRequest,
    batch: ObservationValidationBatch,
) -> None:
    selection = request.committed_selection.campaign
    candidate = _selected_candidate(selection)
    prediction = candidate.committed_prediction.campaign
    protocol = prediction.request.experiment_protocol
    process = _outcome_measurement_process(prediction)
    expected = {
        "request_sha256": request.request_sha256,
        "selection_campaign_sha256": selection.campaign_sha256,
        "selection_commitment_receipt_sha256": request.committed_selection.receipt_sha256,
        "selected_candidate_id": candidate.candidate_id,
        "prediction_campaign_sha256": prediction.campaign_sha256,
        "prediction_commitment_sha256": prediction.commitment_sha256,
        "observation_receipt_sha256": request.observation_receipt.receipt_sha256,
        "observation_sha256": request.observation_receipt.observation_sha256,
        "observation_parser_sha256": protocol.observation_parser_sha256,
        "analysis_plan_sha256": protocol.analysis_plan_sha256,
        "measurement_protocol_sha256": process.measurement_protocol_sha256,
        "measurement_error_model_sha256": process.error_model_sha256,
        "validator_manifest_sha256": validator_manifest.manifest_sha256,
    }
    for field_name, expected_value in expected.items():
        if getattr(batch, field_name) != expected_value:
            raise ValueError(f"observation validation changed exact {field_name} binding")
    outcome_ids = {item.bin_id for item in prediction.request.outcome_schema.bins}
    if batch.outcome_bin_id not in outcome_ids:
        raise ValueError("validated observation outcome is outside the frozen schema")
    governing_rules = {
        protocol.analysis_plan_sha256,
        protocol.exclusion_rule_sha256,
        protocol.stopping_rule_sha256,
        protocol.observation_parser_sha256,
    }
    if any(item.governing_rule_sha256 not in governing_rules for item in batch.protocol_deviations):
        raise ValueError("protocol deviation references a rule outside the frozen protocol")
    assessment = _selected_assessment(selection)
    reservation = next(
        (
            item
            for item in assessment.fresh_confirmation_batches
            if item.batch_sha256 == batch.confirmation_batch_sha256
            and item.partition_sha256 == batch.confirmation_partition_sha256
        ),
        None,
    )
    if reservation is None:
        raise ValueError("observation does not use a selected fresh-confirmation reservation")
    if not (
        reservation.sealed_at
        <= request.observation_receipt.observed_at
        < reservation.available_until
    ):
        raise ValueError("confirmation reservation was not available at observation time")
    if batch.completed_at < request.issued_at:
        raise ValueError("observation validation output predates its request")
    if batch.small_sample_update_rule_sha256 is not None and (
        batch.small_sample_update_rule_sha256 != policy.small_sample_update_rule_sha256
    ):
        raise ValueError("observation validation changed the small-sample rule")


def _derive_observation_probe(
    *,
    policy: ObservationValidationPolicy,
    request: ObservationValidationRequest,
    batch: ObservationValidationBatch,
) -> ObservationValidationProbe:
    blockers: list[str] = []
    if batch.data_role is not ObservationDataRole.CONFIRMATION:
        blockers.append(f"data_role:not_confirmation:{batch.data_role.value}")
    if not batch.experiment_identity_verified:
        blockers.append("identity:experiment_unverified")
    if not batch.custody_chain_verified:
        blockers.append("custody:unverified")
    if not batch.measurement_valid:
        blockers.append("measurement:invalid")
    if not batch.blinding_intact:
        blockers.append("blinding:compromised")
    if batch.protocol_adherence is ProtocolAdherenceStatus.MATERIAL_DEVIATION:
        blockers.append("protocol:material_deviation")
    elif batch.protocol_adherence is ProtocolAdherenceStatus.UNKNOWN:
        blockers.append("protocol:unknown_adherence")
    if batch.audit_status is not ObservationAuditStatus.RESOLVED_ACCEPT:
        blockers.append(f"audit:not_resolved_accept:{batch.audit_status.value}")
    if batch.sample_count < policy.minimum_confirmatory_sample_count:
        if (
            policy.small_sample_update_rule_sha256 is None
            or batch.small_sample_update_rule_sha256 != policy.small_sample_update_rule_sha256
        ):
            blockers.append("sample:below_minimum_without_preregistered_rule")
    elif batch.small_sample_update_rule_sha256 is not None:
        blockers.append("sample:unexpected_small_sample_rule")
    canonical = tuple(sorted(set(blockers)))
    return ObservationValidationProbe(
        batch_sha256=batch.batch_sha256,
        outcome_bin_id=batch.outcome_bin_id,
        sample_count=batch.sample_count,
        blockers=canonical,
        valid_for_belief_update=not canonical,
    )


def _validate_physical_verifications(
    *,
    request: ObservationValidationRequest,
    selection: SelectionArchiveVerification,
    prediction: SelectedPredictionArchiveVerification,
    observation: RawObservationVerification,
) -> None:
    committed_selection = request.committed_selection
    candidate = _selected_candidate(committed_selection.campaign)
    committed_prediction = candidate.committed_prediction
    expected_selection = {
        "selection_campaign_sha256": committed_selection.campaign.campaign_sha256,
        "selection_commitment_receipt_sha256": committed_selection.receipt_sha256,
        "ledger_receipt_sha256": committed_selection.ledger.receipt_sha256,
        "archive_custody_sha256": request.selection_archive_custody_sha256,
    }
    expected_prediction = {
        "candidate_id": candidate.candidate_id,
        "prediction_campaign_sha256": committed_prediction.campaign.campaign_sha256,
        "prediction_commitment_sha256": committed_prediction.campaign.commitment_sha256,
        "prediction_commitment_receipt_sha256": committed_prediction.receipt_sha256,
        "ledger_receipt_sha256": committed_prediction.ledger.receipt_sha256,
        "archive_custody_sha256": request.prediction_archive_custody_sha256,
    }
    receipt = request.observation_receipt
    expected_observation = {
        "observation_receipt_sha256": receipt.receipt_sha256,
        "observation_sha256": receipt.observation_sha256,
        "observation_bytes": receipt.observation_bytes,
        "namespace_seal_sha256": receipt.namespace_seal_sha256,
        "store_custody_sha256": request.observation_store_custody_sha256,
    }
    for verification, expected in (
        (selection, expected_selection),
        (prediction, expected_prediction),
        (observation, expected_observation),
    ):
        for field_name, expected_value in expected.items():
            if getattr(verification, field_name) != expected_value:
                raise ValueError(f"physical verification changed exact {field_name} binding")
        if verification.verified_at < request.issued_at:
            raise ValueError("physical verification predates observation validation request")


def _now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("F9-S6 clock must return a timezone-aware timestamp")
    return value


def _opaque_sha256(value: object) -> str:
    try:
        return content_sha256(value)
    except (TypeError, ValueError, UnicodeError):
        return hashlib.sha256(repr(value).encode("utf-8", errors="replace")).hexdigest()


def _validation_failure(
    *,
    kind: ObservationValidationFailureKind,
    error: Exception,
    occurred_at: datetime,
    raw_output: object | None = None,
) -> ObservationValidationFailure:
    return ObservationValidationFailure(
        kind=kind,
        error_class=type(error).__name__,
        error_detail_sha256=hashlib.sha256(str(error).encode("utf-8")).hexdigest(),
        raw_output_sha256=_opaque_sha256(raw_output) if raw_output is not None else None,
        occurred_at=occurred_at,
    )


def _failed_validation_campaign(
    *,
    campaign_id: str,
    policy: ObservationValidationPolicy,
    validator_manifest: ObservationValidatorManifest,
    request: ObservationValidationRequest,
    failure: ObservationValidationFailure,
    clock: Callable[[], datetime],
) -> ObservationValidationCampaign:
    return ObservationValidationCampaign(
        campaign_id=campaign_id,
        policy=policy,
        validator_manifest=validator_manifest,
        request=request,
        failure=failure,
        blockers=(f"execution_failure:{failure.kind.value}",),
        disposition=ObservationValidationDisposition.BLOCKED_EXECUTION,
        generated_at=_now(clock),
    )


async def run_observation_validation(
    *,
    campaign_id: str,
    policy: ObservationValidationPolicy,
    request: ObservationValidationRequest,
    validator: ObservationValidatorAdapter,
    selection_archive: ContentAddressedResponseArchive,
    prediction_archive: ContentAddressedResponseArchive,
    observation_store: ObservationStagingStore,
    clock: Callable[[], datetime] | None = None,
) -> ObservationValidationCampaign:
    """Validate one selected staged observation; raw bytes never leave this boundary."""

    clock = clock or (lambda: datetime.now(timezone.utc))
    _validate_observation_request(
        policy=policy,
        validator_manifest=validator.manifest,
        request=request,
    )
    try:
        loaded_selection = load_experiment_selection_campaign(
            archive=selection_archive,
            ledger=request.committed_selection.ledger,
        )
        if loaded_selection != request.committed_selection.campaign:
            raise ValueError("embedded selection campaign differs from archived bytes")
    except (ResponseArchiveError, ValidationError, ValueError, TypeError) as exc:
        failure = _validation_failure(
            kind=ObservationValidationFailureKind.SELECTION_ARCHIVE_INVALID,
            error=exc,
            occurred_at=_now(clock),
        )
        return _failed_validation_campaign(
            campaign_id=campaign_id,
            policy=policy,
            validator_manifest=validator.manifest,
            request=request,
            failure=failure,
            clock=clock,
        )
    candidate = _selected_candidate(loaded_selection)
    committed_prediction = candidate.committed_prediction
    try:
        loaded_prediction = load_prediction_commitment_campaign(
            archive=prediction_archive,
            ledger=committed_prediction.ledger,
        )
        if loaded_prediction != committed_prediction.campaign:
            raise ValueError("selected prediction campaign differs from archived bytes")
    except (ResponseArchiveError, ValidationError, ValueError, TypeError) as exc:
        failure = _validation_failure(
            kind=ObservationValidationFailureKind.PREDICTION_ARCHIVE_INVALID,
            error=exc,
            occurred_at=_now(clock),
        )
        return _failed_validation_campaign(
            campaign_id=campaign_id,
            policy=policy,
            validator_manifest=validator.manifest,
            request=request,
            failure=failure,
            clock=clock,
        )
    try:
        raw_observation = observation_store.read_observation(request.observation_receipt)
        if (
            hashlib.sha256(raw_observation).hexdigest()
            != request.observation_receipt.observation_sha256
        ):
            raise ObservationStagingError("reloaded observation differs from its receipt")
    except (ObservationStagingError, ValidationError, ValueError, TypeError) as exc:
        failure = _validation_failure(
            kind=ObservationValidationFailureKind.OBSERVATION_STORE_INVALID,
            error=exc,
            occurred_at=_now(clock),
        )
        return _failed_validation_campaign(
            campaign_id=campaign_id,
            policy=policy,
            validator_manifest=validator.manifest,
            request=request,
            failure=failure,
            clock=clock,
        )
    selection_verification = SelectionArchiveVerification(
        selection_campaign_sha256=loaded_selection.campaign_sha256,
        selection_commitment_receipt_sha256=request.committed_selection.receipt_sha256,
        ledger_receipt_sha256=request.committed_selection.ledger.receipt_sha256,
        archive_custody_sha256=request.selection_archive_custody_sha256,
        verified_at=_now(clock),
    )
    prediction_verification = SelectedPredictionArchiveVerification(
        candidate_id=candidate.candidate_id,
        prediction_campaign_sha256=loaded_prediction.campaign_sha256,
        prediction_commitment_sha256=loaded_prediction.commitment_sha256,
        prediction_commitment_receipt_sha256=committed_prediction.receipt_sha256,
        ledger_receipt_sha256=committed_prediction.ledger.receipt_sha256,
        archive_custody_sha256=request.prediction_archive_custody_sha256,
        verified_at=_now(clock),
    )
    observation_verification = RawObservationVerification(
        observation_receipt_sha256=request.observation_receipt.receipt_sha256,
        observation_sha256=request.observation_receipt.observation_sha256,
        observation_bytes=len(raw_observation),
        namespace_seal_sha256=request.observation_receipt.namespace_seal_sha256,
        store_custody_sha256=request.observation_store_custody_sha256,
        verified_at=_now(clock),
    )
    raw_output: object | None = None
    try:
        raw_output = await validator.validate(
            request=request,
            raw_observation=raw_observation,
        )
    except Exception as exc:  # noqa: BLE001 - adapter boundary sanitizes arbitrary failures
        failure = _validation_failure(
            kind=ObservationValidationFailureKind.VALIDATOR_EXCEPTION,
            error=exc,
            occurred_at=_now(clock),
        )
        return _failed_validation_campaign(
            campaign_id=campaign_id,
            policy=policy,
            validator_manifest=validator.manifest,
            request=request,
            failure=failure,
            clock=clock,
        )
    try:
        batch = ObservationValidationBatch.model_validate(raw_output)
        _validate_observation_batch(
            policy=policy,
            validator_manifest=validator.manifest,
            request=request,
            batch=batch,
        )
        if batch.completed_at > _now(clock):
            raise ValueError("observation validation output is future-dated")
    except (ValidationError, ValueError, TypeError) as exc:
        failure = _validation_failure(
            kind=ObservationValidationFailureKind.INVALID_OUTPUT,
            error=exc,
            occurred_at=_now(clock),
            raw_output=raw_output,
        )
        return _failed_validation_campaign(
            campaign_id=campaign_id,
            policy=policy,
            validator_manifest=validator.manifest,
            request=request,
            failure=failure,
            clock=clock,
        )
    probe = _derive_observation_probe(policy=policy, request=request, batch=batch)
    disposition = (
        ObservationValidationDisposition.VALIDATED_CONFIRMATION
        if probe.valid_for_belief_update
        else ObservationValidationDisposition.REJECTED_SCIENTIFIC
    )
    return ObservationValidationCampaign(
        campaign_id=campaign_id,
        policy=policy,
        validator_manifest=validator.manifest,
        request=request,
        selection_verification=selection_verification,
        prediction_verification=prediction_verification,
        observation_verification=observation_verification,
        validation_batch=batch,
        probe=probe,
        blockers=probe.blockers,
        disposition=disposition,
        generated_at=_now(clock),
    )


def commit_observation_validation_campaign(
    *,
    archive: ContentAddressedResponseArchive,
    campaign: ObservationValidationCampaign,
    committed_at: datetime,
) -> CommittedObservationValidationCampaign:
    if committed_at.tzinfo is None or committed_at.utcoffset() is None:
        raise ValueError("observation validation commitment time must be timezone-aware")
    if committed_at < campaign.generated_at:
        raise ValueError("observation validation commitment cannot predate campaign generation")
    ledger = archive.store_ledger(
        value=campaign,
        object_sha256=campaign.campaign_sha256,
        archived_at=committed_at,
    )
    return CommittedObservationValidationCampaign(
        campaign=campaign,
        ledger=ledger,
        committed_at=committed_at,
    )


def load_observation_validation_campaign(
    *,
    archive: ContentAddressedResponseArchive,
    ledger: ArchivedKnowledgeLedger,
) -> ObservationValidationCampaign:
    payload = archive.read_ledger(ledger)
    campaign = ObservationValidationCampaign.model_validate_json(payload)
    if canonical_json_bytes(campaign) != payload:
        raise ValueError("archived observation validation campaign is not canonical JSON")
    if campaign.campaign_sha256 != ledger.object_sha256:
        raise ValueError("archived observation validation campaign changed object identity")
    return campaign


class WorldBeliefUpdateDisposition(str, Enum):
    UPDATED_ROBUST = "updated_robust"
    UPDATED_FRAGILE = "updated_fragile"
    BLOCKED_LIKELIHOOD = "blocked_likelihood"
    BLOCKED_EXECUTION = "blocked_execution"


class WorldBeliefUpdateFailureKind(str, Enum):
    VALIDATION_ARCHIVE_INVALID = "validation_archive_invalid"


class HypothesisRevisionAction(str, Enum):
    RETAIN = "retain"
    RETIRE = "retire"
    NARROW = "narrow"


class WorldRevisionAction(str, Enum):
    CONTINUE_CURRENT_SET = "continue_current_set"
    FORK_HYPOTHESIS_SET = "fork_hypothesis_set"
    SEEK_NEW_MEASUREMENT_OR_STOP = "seek_new_measurement_or_stop"


class ContradictionKind(str, Enum):
    HYPOTHESIS_PREDICTION_MISS = "hypothesis_prediction_miss"
    PRIOR_PREDICTIVE_SURPRISE = "prior_predictive_surprise"
    LIKELIHOOD_SENSITIVITY = "likelihood_sensitivity"
    ALL_MODELS_LOW_LIKELIHOOD = "all_models_low_likelihood"
    REALIZED_OUTCOME_UNINFORMATIVE = "realized_outcome_uninformative"


class ContradictionSeverity(str, Enum):
    MODERATE = "moderate"
    HIGH = "high"


class WorldBeliefUpdatePolicy(EpistemicModel):
    schema_version: Literal[1] = 1
    policy_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    minimum_sensitivity_scenarios: int = Field(default=2, ge=1, le=32)
    maximum_posterior_total_variation: float = Field(default=0.15, ge=0.0, le=1.0)
    fragile_on_winner_change: Literal[True] = True
    prior_predictive_surprisal_threshold_nats: float = Field(default=3.0, gt=0.0)
    all_model_miss_probability_ceiling: float = Field(default=0.1, ge=0.0, lt=0.5)
    retirement_posterior_ceiling: float = Field(default=0.05, ge=0.0, lt=0.5)
    realized_likelihood_equality_tolerance: float = Field(default=1e-12, gt=0.0, le=1e-6)
    harness_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _thresholds_are_finite(self) -> "WorldBeliefUpdatePolicy":
        numeric = (
            self.maximum_posterior_total_variation,
            self.prior_predictive_surprisal_threshold_nats,
            self.all_model_miss_probability_ceiling,
            self.retirement_posterior_ceiling,
            self.realized_likelihood_equality_tolerance,
        )
        if any(not math.isfinite(value) for value in numeric):
            raise ValueError("world-belief update policy thresholds must be finite")
        return self

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class WorldBeliefUpdateRequest(EpistemicModel):
    schema_version: Literal[1] = 1
    update_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    committed_validation: CommittedObservationValidationCampaign
    question_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_world_model_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_belief_state_sha256: str = Field(pattern=_SHA256_PATTERN)
    selection_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_commitment_sha256: str = Field(pattern=_SHA256_PATTERN)
    validated_outcome_bin_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    validation_archive_custody_sha256: str = Field(pattern=_SHA256_PATTERN)
    issued_at: AwareDatetime
    observation_access: Literal["validated_artifact_only"] = "validated_artifact_only"

    @property
    def request_sha256(self) -> str:
        return content_sha256(self)


class HypothesisPosteriorUpdate(EpistemicModel):
    hypothesis_id: str = Field(pattern=_HYPOTHESIS_ID_PATTERN)
    hypothesis_version_sha256: str = Field(pattern=_SHA256_PATTERN)
    prior_probability: float = Field(ge=0.0, le=1.0)
    observed_outcome_likelihood: float = Field(ge=0.0, le=1.0)
    unnormalized_posterior_mass: float = Field(ge=0.0, le=1.0)
    posterior_probability: float = Field(ge=0.0, le=1.0)
    modal_prediction_matched: bool


class SensitivityHypothesisPosterior(EpistemicModel):
    hypothesis_id: str = Field(pattern=_HYPOTHESIS_ID_PATTERN)
    hypothesis_version_sha256: str = Field(pattern=_SHA256_PATTERN)
    perturbation_sha256: str = Field(pattern=_SHA256_PATTERN)
    observed_outcome_likelihood: float = Field(ge=0.0, le=1.0)
    posterior_probability: float = Field(ge=0.0, le=1.0)


class LikelihoodSensitivityPosterior(EpistemicModel):
    scenario_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    prior_predictive_probability: float = Field(gt=0.0, le=1.0)
    hypotheses: tuple[SensitivityHypothesisPosterior, ...] = Field(min_length=2, max_length=64)
    posterior_total_variation: float = Field(ge=0.0, le=1.0)
    maximum_hypothesis_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    winner_changed: bool

    @model_validator(mode="after")
    def _posterior_is_normalized_and_canonical(self) -> "LikelihoodSensitivityPosterior":
        identities = [item.hypothesis_id for item in self.hypotheses]
        if identities != sorted(set(identities)):
            raise ValueError("sensitivity posterior hypotheses must be unique and canonical")
        if not math.isclose(
            math.fsum(item.posterior_probability for item in self.hypotheses),
            1.0,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError("sensitivity posterior probabilities must sum to one")
        if self.maximum_hypothesis_ids != tuple(sorted(set(self.maximum_hypothesis_ids))):
            raise ValueError("sensitivity winners must be unique and canonical")
        if not set(self.maximum_hypothesis_ids).issubset(identities):
            raise ValueError("sensitivity winner is outside its posterior")
        return self

    @property
    def sensitivity_sha256(self) -> str:
        return content_sha256(self)


class WorldBeliefUpdateAudit(EpistemicModel):
    source_belief_state_sha256: str = Field(pattern=_SHA256_PATTERN)
    validation_commitment_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_commitment_sha256: str = Field(pattern=_SHA256_PATTERN)
    observed_outcome_bin_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    prior_entropy_nats: float = Field(ge=0.0)
    posterior_entropy_nats: float = Field(ge=0.0)
    realized_entropy_reduction_nats: float
    prior_predictive_probability: float = Field(gt=0.0, le=1.0)
    prior_predictive_surprisal_nats: float = Field(ge=0.0)
    hypotheses: tuple[HypothesisPosteriorUpdate, ...] = Field(min_length=2, max_length=64)
    maximum_hypothesis_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    sensitivity_posteriors: tuple[LikelihoodSensitivityPosterior, ...] = Field(
        min_length=1, max_length=32
    )
    maximum_sensitivity_total_variation: float = Field(ge=0.0, le=1.0)
    fragile: bool
    primary_negative_result: bool
    all_models_low_likelihood: bool
    realized_outcome_uninformative: bool
    likelihood_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _audit_is_normalized_and_canonical(self) -> "WorldBeliefUpdateAudit":
        identities = [item.hypothesis_id for item in self.hypotheses]
        if identities != sorted(set(identities)):
            raise ValueError("world update hypotheses must be unique and canonical")
        if not math.isclose(
            math.fsum(item.prior_probability for item in self.hypotheses),
            1.0,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError("world update prior probabilities must sum to one")
        if not math.isclose(
            math.fsum(item.posterior_probability for item in self.hypotheses),
            1.0,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError("world update posterior probabilities must sum to one")
        scenario_ids = [item.scenario_id for item in self.sensitivity_posteriors]
        if scenario_ids != sorted(set(scenario_ids)):
            raise ValueError("likelihood sensitivity scenarios must be unique and canonical")
        if self.maximum_hypothesis_ids != tuple(sorted(set(self.maximum_hypothesis_ids))):
            raise ValueError("world update winners must be unique and canonical")
        if not set(self.maximum_hypothesis_ids).issubset(identities):
            raise ValueError("world update winner is outside its posterior")
        if not math.isfinite(self.realized_entropy_reduction_nats):
            raise ValueError("realized entropy reduction must be finite")
        return self

    @property
    def audit_sha256(self) -> str:
        return content_sha256(self)


class HypothesisRevisionDirective(EpistemicModel):
    hypothesis_id: str = Field(pattern=_HYPOTHESIS_ID_PATTERN)
    source_hypothesis_version_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_lifecycle: HypothesisLifecycle
    action: HypothesisRevisionAction
    new_version_required: bool
    mutation_forbidden: Literal[True] = True
    reasons: tuple[str, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _revision_requires_append_only_versioning(self) -> "HypothesisRevisionDirective":
        if self.reasons != tuple(sorted(set(self.reasons))):
            raise ValueError("hypothesis revision reasons must be unique and canonical")
        if self.new_version_required != (self.action is not HypothesisRevisionAction.RETAIN):
            raise ValueError("retire/narrow require a new version; retain does not")
        return self

    @property
    def directive_sha256(self) -> str:
        return content_sha256(self)


class WorldRevisionDirective(EpistemicModel):
    source_world_model_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_belief_state_sha256: str = Field(pattern=_SHA256_PATTERN)
    action: WorldRevisionAction
    new_hypothesis_lineage_required: bool
    reasons: tuple[str, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _world_revision_is_canonical(self) -> "WorldRevisionDirective":
        if self.reasons != tuple(sorted(set(self.reasons))):
            raise ValueError("world revision reasons must be unique and canonical")
        if self.new_hypothesis_lineage_required != (
            self.action is WorldRevisionAction.FORK_HYPOTHESIS_SET
        ):
            raise ValueError("only a hypothesis-set fork requires a new lineage")
        return self

    @property
    def directive_sha256(self) -> str:
        return content_sha256(self)


class ContradictionRecord(EpistemicModel):
    kind: ContradictionKind
    severity: ContradictionSeverity
    hypothesis_ids: tuple[str, ...] = Field(max_length=64)
    observation_validation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_commitment_sha256: str = Field(pattern=_SHA256_PATTERN)
    evidence_sha256s: tuple[str, ...] = Field(min_length=1, max_length=32)
    reason_code: str = Field(pattern=_LOCAL_ID_PATTERN)
    detected_at: AwareDatetime
    status: Literal["open"] = "open"

    @model_validator(mode="after")
    def _contradiction_is_canonical(self) -> "ContradictionRecord":
        if self.hypothesis_ids != tuple(sorted(set(self.hypothesis_ids))):
            raise ValueError("contradiction hypotheses must be unique and canonical")
        if self.evidence_sha256s != tuple(sorted(set(self.evidence_sha256s))):
            raise ValueError("contradiction evidence must be unique and canonical")
        return self

    @property
    def contradiction_sha256(self) -> str:
        return content_sha256(self)


class ValidationArchiveVerification(EpistemicModel):
    validation_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    validation_commitment_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    ledger_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    archive_custody_sha256: str = Field(pattern=_SHA256_PATTERN)
    verified_at: AwareDatetime

    @property
    def verification_sha256(self) -> str:
        return content_sha256(self)


class WorldBeliefUpdateFailure(EpistemicModel):
    kind: WorldBeliefUpdateFailureKind
    error_class: str = Field(min_length=1, max_length=256)
    error_detail_sha256: str = Field(pattern=_SHA256_PATTERN)
    occurred_at: AwareDatetime

    @property
    def failure_sha256(self) -> str:
        return content_sha256(self)


class _DerivedWorldBeliefUpdate(EpistemicModel):
    audit: WorldBeliefUpdateAudit | None = None
    updated_world_model_snapshot: WorldModelSnapshot | None = None
    hypothesis_revisions: tuple[HypothesisRevisionDirective, ...]
    world_revision: WorldRevisionDirective | None = None
    contradiction_queue: tuple[ContradictionRecord, ...]
    blockers: tuple[str, ...]
    disposition: WorldBeliefUpdateDisposition


class WorldBeliefUpdateCampaign(EpistemicModel):
    schema_version: Literal[1] = 1
    campaign_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    policy: WorldBeliefUpdatePolicy
    request: WorldBeliefUpdateRequest
    validation_verification: ValidationArchiveVerification | None = None
    audit: WorldBeliefUpdateAudit | None = None
    updated_world_model_snapshot: WorldModelSnapshot | None = None
    hypothesis_revisions: tuple[HypothesisRevisionDirective, ...]
    world_revision: WorldRevisionDirective | None = None
    contradiction_queue: tuple[ContradictionRecord, ...]
    failure: WorldBeliefUpdateFailure | None = None
    blockers: tuple[str, ...]
    disposition: WorldBeliefUpdateDisposition
    generated_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _campaign_is_mechanically_derived(self) -> "WorldBeliefUpdateCampaign":
        _validate_world_update_request(policy=self.policy, request=self.request)
        if self.failure is not None:
            expected = (f"execution_failure:{self.failure.kind.value}",)
            if (
                self.validation_verification is not None
                or self.audit is not None
                or self.updated_world_model_snapshot is not None
                or self.hypothesis_revisions
                or self.world_revision is not None
                or self.contradiction_queue
                or self.blockers != expected
                or self.disposition is not WorldBeliefUpdateDisposition.BLOCKED_EXECUTION
            ):
                raise ValueError("failed world-belief update outputs are not derived")
            if self.failure.occurred_at < self.request.issued_at:
                raise ValueError("world-belief update failure predates its request")
            if self.generated_at < self.failure.occurred_at:
                raise ValueError("world-belief update campaign predates its failure")
            return self
        if self.validation_verification is None:
            raise ValueError("world-belief update requires validation archive verification")
        committed = self.request.committed_validation
        expected_verification = {
            "validation_campaign_sha256": committed.campaign.campaign_sha256,
            "validation_commitment_receipt_sha256": committed.receipt_sha256,
            "ledger_receipt_sha256": committed.ledger.receipt_sha256,
            "archive_custody_sha256": self.request.validation_archive_custody_sha256,
        }
        for field_name, expected_value in expected_verification.items():
            if getattr(self.validation_verification, field_name) != expected_value:
                raise ValueError(f"validation archive verification changed {field_name}")
        if self.validation_verification.verified_at < self.request.issued_at:
            raise ValueError("validation archive verification predates update request")
        derived = _derive_world_belief_update(
            policy=self.policy,
            request=self.request,
            generated_at=self.generated_at,
        )
        actual = _DerivedWorldBeliefUpdate(
            audit=self.audit,
            updated_world_model_snapshot=self.updated_world_model_snapshot,
            hypothesis_revisions=self.hypothesis_revisions,
            world_revision=self.world_revision,
            contradiction_queue=self.contradiction_queue,
            blockers=self.blockers,
            disposition=self.disposition,
        )
        if actual != derived:
            raise ValueError("world-belief update outputs are not mechanically derived")
        if self.generated_at < self.validation_verification.verified_at:
            raise ValueError("world-belief update campaign predates archive verification")
        return self

    @property
    def campaign_sha256(self) -> str:
        return content_sha256(self)


class CommittedWorldBeliefUpdateCampaign(EpistemicModel):
    schema_version: Literal[1] = 1
    campaign: WorldBeliefUpdateCampaign
    ledger: ArchivedKnowledgeLedger
    committed_at: AwareDatetime

    @model_validator(mode="after")
    def _ledger_commits_campaign(self) -> "CommittedWorldBeliefUpdateCampaign":
        payload = canonical_json_bytes(self.campaign)
        if (
            self.ledger.object_sha256 != self.campaign.campaign_sha256
            or self.ledger.ledger_sha256 != hashlib.sha256(payload).hexdigest()
            or self.ledger.ledger_bytes != len(payload)
            or self.ledger.archived_at != self.committed_at
        ):
            raise ValueError("world-belief update ledger does not commit its campaign and time")
        if self.committed_at < self.campaign.generated_at:
            raise ValueError("world-belief update commitment predates campaign generation")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


def _validated_source(
    campaign: ObservationValidationCampaign,
) -> tuple[ExperimentSelectionCampaign, ExperimentCandidate, ObservationValidationBatch]:
    if (
        campaign.disposition is not ObservationValidationDisposition.VALIDATED_CONFIRMATION
        or campaign.probe is None
        or not campaign.probe.valid_for_belief_update
        or campaign.validation_batch is None
    ):
        raise ValueError("world-belief update requires a validated confirmation artifact")
    selection = campaign.request.committed_selection.campaign
    candidate = _selected_candidate(selection)
    return selection, candidate, campaign.validation_batch


def _source_snapshot(candidate: ExperimentCandidate) -> WorldModelSnapshot:
    return candidate.committed_prediction.campaign.source_causal_campaign.world_model_snapshot


def _validate_world_update_request(
    *,
    policy: WorldBeliefUpdatePolicy,
    request: WorldBeliefUpdateRequest,
) -> None:
    if request.policy_sha256 != policy.policy_sha256:
        raise ValueError("world-belief update request changed its policy binding")
    validation = request.committed_validation
    selection, candidate, batch = _validated_source(validation.campaign)
    snapshot = _source_snapshot(candidate)
    prediction = candidate.committed_prediction.campaign
    expected = {
        "question_sha256": snapshot.question.question_sha256,
        "source_world_model_snapshot_sha256": snapshot.snapshot_sha256,
        "source_belief_state_sha256": snapshot.belief_state.belief_state_sha256,
        "selection_campaign_sha256": selection.campaign_sha256,
        "prediction_commitment_sha256": prediction.commitment_sha256,
        "validated_outcome_bin_id": batch.outcome_bin_id,
    }
    for field_name, expected_value in expected.items():
        if getattr(request, field_name) != expected_value:
            raise ValueError(f"world-belief update request changed exact {field_name} binding")
    if validation.committed_at > request.issued_at:
        raise ValueError("world-belief update request predates validation commitment")
    selection_commit = validation.campaign.request.committed_selection.committed_at
    if policy.frozen_at > selection_commit:
        raise ValueError("world-belief update policy was not frozen before selection")


def build_world_belief_update_request(
    *,
    update_id: str,
    committed_validation: CommittedObservationValidationCampaign,
    policy: WorldBeliefUpdatePolicy,
    validation_archive_custody_sha256: str,
    issued_at: datetime,
) -> WorldBeliefUpdateRequest:
    selection, candidate, batch = _validated_source(committed_validation.campaign)
    snapshot = _source_snapshot(candidate)
    request = WorldBeliefUpdateRequest(
        update_id=update_id,
        committed_validation=committed_validation,
        question_sha256=snapshot.question.question_sha256,
        source_world_model_snapshot_sha256=snapshot.snapshot_sha256,
        source_belief_state_sha256=snapshot.belief_state.belief_state_sha256,
        selection_campaign_sha256=selection.campaign_sha256,
        prediction_commitment_sha256=(candidate.committed_prediction.campaign.commitment_sha256),
        validated_outcome_bin_id=batch.outcome_bin_id,
        policy_sha256=policy.policy_sha256,
        validation_archive_custody_sha256=validation_archive_custody_sha256,
        issued_at=issued_at,
    )
    _validate_world_update_request(policy=policy, request=request)
    return request


def _entropy(probabilities: tuple[float, ...]) -> float:
    return -math.fsum(value * math.log(value) for value in probabilities if value > 0.0)


def _normalize(masses: tuple[float, ...]) -> tuple[float, ...]:
    total = math.fsum(masses)
    if total <= 0.0:
        raise ValueError("posterior normalization requires positive predictive mass")
    values = [value / total for value in masses]
    values[-1] += 1.0 - math.fsum(values)
    return tuple(values)


def _winners(hypothesis_ids: tuple[str, ...], probabilities: tuple[float, ...]) -> tuple[str, ...]:
    maximum = max(probabilities)
    return tuple(
        hypothesis_id
        for hypothesis_id, probability in zip(hypothesis_ids, probabilities, strict=True)
        if probability == maximum
    )


def _prediction_maps(candidate: ExperimentCandidate):
    campaign = candidate.committed_prediction.campaign
    if campaign.prediction_batch is None:
        raise ValueError("selected prediction campaign lacks a prediction batch")
    return {item.hypothesis_id: item for item in campaign.prediction_batch.predictions}


def _blocked_world_update(*blockers: str) -> _DerivedWorldBeliefUpdate:
    canonical = tuple(sorted(set(blockers)))
    return _DerivedWorldBeliefUpdate(
        hypothesis_revisions=(),
        contradiction_queue=(),
        blockers=canonical,
        disposition=WorldBeliefUpdateDisposition.BLOCKED_LIKELIHOOD,
    )


def _derive_world_belief_update(
    *,
    policy: WorldBeliefUpdatePolicy,
    request: WorldBeliefUpdateRequest,
    generated_at: datetime,
) -> _DerivedWorldBeliefUpdate:
    _, candidate, batch = _validated_source(request.committed_validation.campaign)
    snapshot = _source_snapshot(candidate)
    prediction_campaign = candidate.committed_prediction.campaign
    predictions = _prediction_maps(candidate)
    hypothesis_ids = tuple(item.hypothesis_id for item in snapshot.belief_state.hypotheses)
    priors = tuple(item.probability for item in snapshot.belief_state.hypotheses)
    versions = {
        item.hypothesis_id: item.hypothesis_version_sha256
        for item in snapshot.belief_state.hypotheses
    }
    outcome = batch.outcome_bin_id
    likelihoods = tuple(
        next(
            item.probability
            for item in predictions[hypothesis_id].probabilities
            if item.bin_id == outcome
        )
        for hypothesis_id in hypothesis_ids
    )
    masses = tuple(
        prior * likelihood for prior, likelihood in zip(priors, likelihoods, strict=True)
    )
    prior_predictive = math.fsum(masses)
    if prior_predictive <= 0.0:
        return _blocked_world_update("likelihood:zero_prior_predictive_mass")
    posteriors = _normalize(masses)
    base_winners = _winners(hypothesis_ids, posteriors)
    scenario_sets = [
        {item.scenario_id for item in predictions[hypothesis_id].sensitivity_predictions}
        for hypothesis_id in hypothesis_ids
    ]
    shared_scenarios = scenario_sets[0]
    if len(shared_scenarios) < policy.minimum_sensitivity_scenarios or any(
        item != shared_scenarios for item in scenario_sets[1:]
    ):
        return _blocked_world_update("sensitivity:scenario_matrix_incomplete")
    sensitivity_posteriors: list[LikelihoodSensitivityPosterior] = []
    for scenario_id in sorted(shared_scenarios):
        scenario_objects = tuple(
            next(
                item
                for item in predictions[hypothesis_id].sensitivity_predictions
                if item.scenario_id == scenario_id
            )
            for hypothesis_id in hypothesis_ids
        )
        scenario_likelihoods = tuple(
            next(item.probability for item in scenario.probabilities if item.bin_id == outcome)
            for scenario in scenario_objects
        )
        scenario_masses = tuple(
            prior * likelihood
            for prior, likelihood in zip(priors, scenario_likelihoods, strict=True)
        )
        scenario_predictive = math.fsum(scenario_masses)
        if scenario_predictive <= 0.0:
            return _blocked_world_update(f"sensitivity:zero_predictive_mass:{scenario_id}")
        scenario_posterior = _normalize(scenario_masses)
        scenario_winners = _winners(hypothesis_ids, scenario_posterior)
        total_variation = 0.5 * math.fsum(
            abs(left - right) for left, right in zip(posteriors, scenario_posterior, strict=True)
        )
        sensitivity_posteriors.append(
            LikelihoodSensitivityPosterior(
                scenario_id=scenario_id,
                prior_predictive_probability=scenario_predictive,
                hypotheses=tuple(
                    SensitivityHypothesisPosterior(
                        hypothesis_id=hypothesis_id,
                        hypothesis_version_sha256=versions[hypothesis_id],
                        perturbation_sha256=scenario.perturbation_sha256,
                        observed_outcome_likelihood=likelihood,
                        posterior_probability=posterior,
                    )
                    for hypothesis_id, scenario, likelihood, posterior in zip(
                        hypothesis_ids,
                        scenario_objects,
                        scenario_likelihoods,
                        scenario_posterior,
                        strict=True,
                    )
                ),
                posterior_total_variation=round(total_variation, _METRIC_DIGITS),
                maximum_hypothesis_ids=scenario_winners,
                winner_changed=scenario_winners != base_winners,
            )
        )
    maximum_sensitivity_tv = max(item.posterior_total_variation for item in sensitivity_posteriors)
    fragile = maximum_sensitivity_tv > policy.maximum_posterior_total_variation or any(
        item.winner_changed for item in sensitivity_posteriors
    )
    hypothesis_by_id = {item.hypothesis_id: item for item in snapshot.hypotheses}
    posterior_updates = tuple(
        HypothesisPosteriorUpdate(
            hypothesis_id=hypothesis_id,
            hypothesis_version_sha256=versions[hypothesis_id],
            prior_probability=prior,
            observed_outcome_likelihood=likelihood,
            unnormalized_posterior_mass=mass,
            posterior_probability=posterior,
            modal_prediction_matched=(
                predictions[hypothesis_id].expected_outcome_bin_id == outcome
            ),
        )
        for hypothesis_id, prior, likelihood, mass, posterior in zip(
            hypothesis_ids,
            priors,
            likelihoods,
            masses,
            posteriors,
            strict=True,
        )
    )
    primary_id = next(
        item.hypothesis_id for item in snapshot.hypotheses if item.role is HypothesisRole.PRIMARY
    )
    primary_negative = predictions[primary_id].expected_outcome_bin_id != outcome
    all_models_low = max(likelihoods) <= policy.all_model_miss_probability_ceiling
    uninformative = (
        max(likelihoods) - min(likelihoods) <= policy.realized_likelihood_equality_tolerance
    )
    prior_entropy = _entropy(priors)
    posterior_entropy = _entropy(posteriors)
    surprisal = -math.log(prior_predictive)
    likelihood_bundle_sha256 = content_sha256(
        {
            "schema": "aletheia.f9s6.realized_likelihood_bundle.v1",
            "prediction_commitment_sha256": prediction_campaign.commitment_sha256,
            "observed_outcome_bin_id": outcome,
            "members": [
                {
                    "hypothesis_id": hypothesis_id,
                    "hypothesis_version_sha256": versions[hypothesis_id],
                    "likelihood_model_sha256": predictions[hypothesis_id].likelihood_model_sha256,
                    "likelihood": likelihood,
                }
                for hypothesis_id, likelihood in zip(hypothesis_ids, likelihoods, strict=True)
            ],
        }
    )
    audit = WorldBeliefUpdateAudit(
        source_belief_state_sha256=snapshot.belief_state.belief_state_sha256,
        validation_commitment_receipt_sha256=request.committed_validation.receipt_sha256,
        prediction_commitment_sha256=prediction_campaign.commitment_sha256,
        observed_outcome_bin_id=outcome,
        prior_entropy_nats=round(prior_entropy, _METRIC_DIGITS),
        posterior_entropy_nats=round(posterior_entropy, _METRIC_DIGITS),
        realized_entropy_reduction_nats=round(prior_entropy - posterior_entropy, _METRIC_DIGITS),
        prior_predictive_probability=prior_predictive,
        prior_predictive_surprisal_nats=round(surprisal, _METRIC_DIGITS),
        hypotheses=posterior_updates,
        maximum_hypothesis_ids=base_winners,
        sensitivity_posteriors=tuple(sensitivity_posteriors),
        maximum_sensitivity_total_variation=maximum_sensitivity_tv,
        fragile=fragile,
        primary_negative_result=primary_negative,
        all_models_low_likelihood=all_models_low,
        realized_outcome_uninformative=uninformative,
        likelihood_bundle_sha256=likelihood_bundle_sha256,
    )
    sensitivity_by_hypothesis = {
        hypothesis_id: tuple(
            next(
                item.posterior_probability
                for item in scenario.hypotheses
                if item.hypothesis_id == hypothesis_id
            )
            for scenario in sensitivity_posteriors
        )
        for hypothesis_id in hypothesis_ids
    }
    revisions: list[HypothesisRevisionDirective] = []
    for update in posterior_updates:
        if update.modal_prediction_matched:
            action = HypothesisRevisionAction.RETAIN
            reasons = ("observed_modal_prediction",)
        elif (
            max(
                update.posterior_probability,
                *sensitivity_by_hypothesis[update.hypothesis_id],
            )
            <= policy.retirement_posterior_ceiling
        ):
            action = HypothesisRevisionAction.RETIRE
            reasons = ("robust_posterior_below_retirement_ceiling",)
        else:
            action = HypothesisRevisionAction.NARROW
            reasons = ("modal_prediction_missed_but_retirement_not_robust",)
        source = hypothesis_by_id[update.hypothesis_id]
        revisions.append(
            HypothesisRevisionDirective(
                hypothesis_id=update.hypothesis_id,
                source_hypothesis_version_sha256=source.hypothesis_sha256,
                source_lifecycle=source.lifecycle,
                action=action,
                new_version_required=action is not HypothesisRevisionAction.RETAIN,
                reasons=reasons,
            )
        )
    if all_models_low:
        world_action = WorldRevisionAction.FORK_HYPOTHESIS_SET
        world_reasons = ("all_models_low_likelihood",)
    elif uninformative:
        world_action = WorldRevisionAction.SEEK_NEW_MEASUREMENT_OR_STOP
        world_reasons = ("realized_outcome_does_not_discriminate",)
    else:
        world_action = WorldRevisionAction.CONTINUE_CURRENT_SET
        world_reasons = ("current_hypothesis_set_remains_discriminating",)
    world_revision = WorldRevisionDirective(
        source_world_model_snapshot_sha256=snapshot.snapshot_sha256,
        source_belief_state_sha256=snapshot.belief_state.belief_state_sha256,
        action=world_action,
        new_hypothesis_lineage_required=(world_action is WorldRevisionAction.FORK_HYPOTHESIS_SET),
        reasons=world_reasons,
    )
    evidence = tuple(
        sorted(
            {
                request.committed_validation.receipt_sha256,
                batch.batch_sha256,
                prediction_campaign.commitment_sha256,
            }
        )
    )
    contradictions: list[ContradictionRecord] = []
    for update in posterior_updates:
        if not update.modal_prediction_matched:
            contradictions.append(
                ContradictionRecord(
                    kind=ContradictionKind.HYPOTHESIS_PREDICTION_MISS,
                    severity=ContradictionSeverity.MODERATE,
                    hypothesis_ids=(update.hypothesis_id,),
                    observation_validation_receipt_sha256=(
                        request.committed_validation.receipt_sha256
                    ),
                    prediction_commitment_sha256=prediction_campaign.commitment_sha256,
                    evidence_sha256s=evidence,
                    reason_code="modal_prediction_missed",
                    detected_at=generated_at,
                )
            )
    if surprisal >= policy.prior_predictive_surprisal_threshold_nats:
        contradictions.append(
            ContradictionRecord(
                kind=ContradictionKind.PRIOR_PREDICTIVE_SURPRISE,
                severity=ContradictionSeverity.HIGH,
                hypothesis_ids=hypothesis_ids,
                observation_validation_receipt_sha256=(request.committed_validation.receipt_sha256),
                prediction_commitment_sha256=prediction_campaign.commitment_sha256,
                evidence_sha256s=evidence,
                reason_code="prior_predictive_surprisal_above_threshold",
                detected_at=generated_at,
            )
        )
    if fragile:
        contradictions.append(
            ContradictionRecord(
                kind=ContradictionKind.LIKELIHOOD_SENSITIVITY,
                severity=ContradictionSeverity.HIGH,
                hypothesis_ids=hypothesis_ids,
                observation_validation_receipt_sha256=(request.committed_validation.receipt_sha256),
                prediction_commitment_sha256=prediction_campaign.commitment_sha256,
                evidence_sha256s=evidence,
                reason_code="posterior_changes_under_likelihood_sensitivity",
                detected_at=generated_at,
            )
        )
    if all_models_low:
        contradictions.append(
            ContradictionRecord(
                kind=ContradictionKind.ALL_MODELS_LOW_LIKELIHOOD,
                severity=ContradictionSeverity.HIGH,
                hypothesis_ids=hypothesis_ids,
                observation_validation_receipt_sha256=(request.committed_validation.receipt_sha256),
                prediction_commitment_sha256=prediction_campaign.commitment_sha256,
                evidence_sha256s=evidence,
                reason_code="all_models_assign_low_observation_likelihood",
                detected_at=generated_at,
            )
        )
    if uninformative:
        contradictions.append(
            ContradictionRecord(
                kind=ContradictionKind.REALIZED_OUTCOME_UNINFORMATIVE,
                severity=ContradictionSeverity.MODERATE,
                hypothesis_ids=hypothesis_ids,
                observation_validation_receipt_sha256=(request.committed_validation.receipt_sha256),
                prediction_commitment_sha256=prediction_campaign.commitment_sha256,
                evidence_sha256s=evidence,
                reason_code="realized_outcome_likelihoods_are_equal",
                detected_at=generated_at,
            )
        )
    contradictions.sort(key=lambda item: (item.kind.value, item.hypothesis_ids))
    updated_belief = BeliefState(
        run_id=snapshot.belief_state.run_id,
        belief_lineage_id=snapshot.belief_state.belief_lineage_id,
        version=snapshot.belief_state.version + 1,
        parent_belief_state_sha256=snapshot.belief_state.belief_state_sha256,
        question_id=snapshot.belief_state.question_id,
        question_version_sha256=snapshot.belief_state.question_version_sha256,
        hypotheses=tuple(
            HypothesisBelief(
                hypothesis_id=hypothesis_id,
                hypothesis_version_sha256=versions[hypothesis_id],
                probability=posterior,
            )
            for hypothesis_id, posterior in zip(hypothesis_ids, posteriors, strict=True)
        ),
        update_kind=BeliefUpdateKind.VALIDATED_OBSERVATION,
        source_observation_receipt_sha256=request.committed_validation.receipt_sha256,
        likelihood_model_sha256=likelihood_bundle_sha256,
        author_principal_sha256=policy.harness_principal_sha256,
        frozen_at=generated_at,
    )
    updated_snapshot = WorldModelSnapshot(
        question=snapshot.question,
        hypotheses=snapshot.hypotheses,
        assumptions=snapshot.assumptions,
        predictions=snapshot.predictions,
        belief_state=updated_belief,
        frozen_at=generated_at,
    )
    disposition = (
        WorldBeliefUpdateDisposition.UPDATED_FRAGILE
        if fragile
        else WorldBeliefUpdateDisposition.UPDATED_ROBUST
    )
    return _DerivedWorldBeliefUpdate(
        audit=audit,
        updated_world_model_snapshot=updated_snapshot,
        hypothesis_revisions=tuple(revisions),
        world_revision=world_revision,
        contradiction_queue=tuple(contradictions),
        blockers=(),
        disposition=disposition,
    )


def _world_update_failure(
    *,
    error: Exception,
    occurred_at: datetime,
) -> WorldBeliefUpdateFailure:
    return WorldBeliefUpdateFailure(
        kind=WorldBeliefUpdateFailureKind.VALIDATION_ARCHIVE_INVALID,
        error_class=type(error).__name__,
        error_detail_sha256=hashlib.sha256(str(error).encode("utf-8")).hexdigest(),
        occurred_at=occurred_at,
    )


def run_world_belief_update(
    *,
    campaign_id: str,
    policy: WorldBeliefUpdatePolicy,
    request: WorldBeliefUpdateRequest,
    validation_archive: ContentAddressedResponseArchive,
    clock: Callable[[], datetime] | None = None,
) -> WorldBeliefUpdateCampaign:
    """Update from a committed validated artifact without receiving raw observation access."""

    clock = clock or (lambda: datetime.now(timezone.utc))
    _validate_world_update_request(policy=policy, request=request)
    committed = request.committed_validation
    try:
        loaded = load_observation_validation_campaign(
            archive=validation_archive,
            ledger=committed.ledger,
        )
        if loaded != committed.campaign:
            raise ValueError("embedded validation campaign differs from archived bytes")
    except (ResponseArchiveError, ValidationError, ValueError, TypeError) as exc:
        failure = _world_update_failure(error=exc, occurred_at=_now(clock))
        return WorldBeliefUpdateCampaign(
            campaign_id=campaign_id,
            policy=policy,
            request=request,
            hypothesis_revisions=(),
            contradiction_queue=(),
            failure=failure,
            blockers=(f"execution_failure:{failure.kind.value}",),
            disposition=WorldBeliefUpdateDisposition.BLOCKED_EXECUTION,
            generated_at=_now(clock),
        )
    verification = ValidationArchiveVerification(
        validation_campaign_sha256=loaded.campaign_sha256,
        validation_commitment_receipt_sha256=committed.receipt_sha256,
        ledger_receipt_sha256=committed.ledger.receipt_sha256,
        archive_custody_sha256=request.validation_archive_custody_sha256,
        verified_at=_now(clock),
    )
    generated_at = _now(clock)
    derived = _derive_world_belief_update(
        policy=policy,
        request=request,
        generated_at=generated_at,
    )
    return WorldBeliefUpdateCampaign(
        campaign_id=campaign_id,
        policy=policy,
        request=request,
        validation_verification=verification,
        audit=derived.audit,
        updated_world_model_snapshot=derived.updated_world_model_snapshot,
        hypothesis_revisions=derived.hypothesis_revisions,
        world_revision=derived.world_revision,
        contradiction_queue=derived.contradiction_queue,
        blockers=derived.blockers,
        disposition=derived.disposition,
        generated_at=generated_at,
    )


def commit_world_belief_update_campaign(
    *,
    archive: ContentAddressedResponseArchive,
    campaign: WorldBeliefUpdateCampaign,
    committed_at: datetime,
) -> CommittedWorldBeliefUpdateCampaign:
    if committed_at.tzinfo is None or committed_at.utcoffset() is None:
        raise ValueError("world-belief update commitment time must be timezone-aware")
    if committed_at < campaign.generated_at:
        raise ValueError("world-belief update commitment cannot predate campaign generation")
    ledger = archive.store_ledger(
        value=campaign,
        object_sha256=campaign.campaign_sha256,
        archived_at=committed_at,
    )
    return CommittedWorldBeliefUpdateCampaign(
        campaign=campaign,
        ledger=ledger,
        committed_at=committed_at,
    )


def load_world_belief_update_campaign(
    *,
    archive: ContentAddressedResponseArchive,
    ledger: ArchivedKnowledgeLedger,
) -> WorldBeliefUpdateCampaign:
    payload = archive.read_ledger(ledger)
    campaign = WorldBeliefUpdateCampaign.model_validate_json(payload)
    if canonical_json_bytes(campaign) != payload:
        raise ValueError("archived world-belief update campaign is not canonical JSON")
    if campaign.campaign_sha256 != ledger.object_sha256:
        raise ValueError("archived world-belief update campaign changed object identity")
    return campaign


__all__ = [
    "OBSERVATION_VALIDATION_OUTPUT_SCHEMA_SHA256",
    "CommittedObservationValidationCampaign",
    "CommittedWorldBeliefUpdateCampaign",
    "ContradictionKind",
    "ContradictionRecord",
    "ContradictionSeverity",
    "HypothesisPosteriorUpdate",
    "HypothesisRevisionAction",
    "HypothesisRevisionDirective",
    "LikelihoodSensitivityPosterior",
    "ObservationAuditStatus",
    "ObservationDataRole",
    "ObservationValidationBatch",
    "ObservationValidationCampaign",
    "ObservationValidationDisposition",
    "ObservationValidationFailure",
    "ObservationValidationFailureKind",
    "ObservationValidationPolicy",
    "ObservationValidationProbe",
    "ObservationValidationRequest",
    "ObservationValidationRuntime",
    "ObservationValidatorManifest",
    "ProtocolAdherenceStatus",
    "ProtocolDeviation",
    "RawObservationVerification",
    "SelectedPredictionArchiveVerification",
    "SelectionArchiveVerification",
    "SensitivityHypothesisPosterior",
    "ValidationArchiveVerification",
    "WorldBeliefUpdateAudit",
    "WorldBeliefUpdateCampaign",
    "WorldBeliefUpdateDisposition",
    "WorldBeliefUpdateFailure",
    "WorldBeliefUpdateFailureKind",
    "WorldBeliefUpdatePolicy",
    "WorldBeliefUpdateRequest",
    "WorldRevisionAction",
    "WorldRevisionDirective",
    "build_observation_validation_request",
    "build_world_belief_update_request",
    "commit_observation_validation_campaign",
    "commit_world_belief_update_campaign",
    "load_observation_validation_campaign",
    "load_world_belief_update_campaign",
    "run_observation_validation",
    "run_world_belief_update",
]
