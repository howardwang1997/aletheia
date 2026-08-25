from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import aletheia.epistemics as e
from aletheia.knowledge.response_archive import ContentAddressedResponseArchive
from aletheia.reproducibility.manifest import canonical_json_bytes

from .f9s2_fixtures import StepClock, digest, revalidate
from .f9s5_fixtures import build_f9s5_fixture


def _selected_candidate(selection: e.ExperimentSelectionCampaign):
    assert selection.decision is not None
    selected_id = selection.decision.selected_candidate_id
    assert selected_id is not None
    return next(item for item in selection.request.candidates if item.candidate_id == selected_id)


def _selected_assessment(selection: e.ExperimentSelectionCampaign):
    candidate = _selected_candidate(selection)
    return next(
        item
        for item in selection.request.assessment_batch.assessments
        if item.candidate_id == candidate.candidate_id
    )


def _measurement_process(campaign: e.PredictionCommitmentCampaign):
    causal = campaign.source_causal_campaign
    assert causal.contract_batch is not None
    contract = causal.contract_batch.contract
    return next(
        item
        for item in contract.measurement_processes
        if item.process_id == contract.outcome_measurement_process_id
    )


def _outcome_for_role(
    campaign: e.PredictionCommitmentCampaign,
    role: e.HypothesisRole,
) -> str:
    snapshot = campaign.source_causal_campaign.source_campaign.world_model_snapshot
    assert snapshot is not None
    hypothesis_id = next(item.hypothesis_id for item in snapshot.hypotheses if item.role is role)
    assert campaign.prediction_batch is not None
    return next(
        item.expected_outcome_bin_id
        for item in campaign.prediction_batch.predictions
        if item.hypothesis_id == hypothesis_id
    )


def build_observation_validator_manifest(*, frozen_at) -> e.ObservationValidatorManifest:
    return e.ObservationValidatorManifest(
        validator_id="f9s6-independent-observation-validator-v1",
        runtime=e.ObservationValidationRuntime.DETERMINISTIC,
        adapter_code_sha256=digest("f9s6:observation-validator-code"),
        parser_sha256=digest("f9s6:observation-validator-parser"),
        output_schema_sha256=e.OBSERVATION_VALIDATION_OUTPUT_SCHEMA_SHA256,
        validator_principal_sha256=digest("f9s6:observation-validator-principal"),
        transport_policy="none",
        frozen_at=frozen_at,
    )


class FixtureObservationValidator:
    def __init__(
        self,
        manifest: e.ObservationValidatorManifest,
        *,
        completed_at,
        overrides: dict[str, object] | None = None,
        error: Exception | None = None,
        raw_output: object | None = None,
    ) -> None:
        self._manifest = manifest
        self.completed_at = completed_at
        self.overrides = overrides or {}
        self.error = error
        self.raw_output = raw_output

    @property
    def manifest(self) -> e.ObservationValidatorManifest:
        return self._manifest

    async def validate(
        self,
        *,
        request: e.ObservationValidationRequest,
        raw_observation: bytes,
    ) -> object:
        if self.error is not None:
            raise self.error
        if self.raw_output is not None:
            return self.raw_output
        decoded = json.loads(raw_observation)
        selection = request.committed_selection.campaign
        candidate = _selected_candidate(selection)
        campaign = candidate.committed_prediction.campaign
        protocol = campaign.request.experiment_protocol
        process = _measurement_process(campaign)
        payload: dict[str, object] = {
            "request_sha256": request.request_sha256,
            "selection_campaign_sha256": selection.campaign_sha256,
            "selection_commitment_receipt_sha256": request.committed_selection.receipt_sha256,
            "selected_candidate_id": candidate.candidate_id,
            "prediction_campaign_sha256": campaign.campaign_sha256,
            "prediction_commitment_sha256": campaign.commitment_sha256,
            "observation_receipt_sha256": request.observation_receipt.receipt_sha256,
            "observation_sha256": request.observation_receipt.observation_sha256,
            "confirmation_batch_sha256": decoded["confirmation_batch_sha256"],
            "confirmation_partition_sha256": decoded["confirmation_partition_sha256"],
            "outcome_bin_id": decoded["outcome_bin_id"],
            "sample_count": decoded["sample_count"],
            "data_role": e.ObservationDataRole.CONFIRMATION,
            "experiment_identity_verified": True,
            "custody_chain_verified": True,
            "measurement_valid": True,
            "blinding_intact": True,
            "protocol_adherence": e.ProtocolAdherenceStatus.EXACT,
            "protocol_deviations": (),
            "small_sample_update_rule_sha256": None,
            "observation_parser_sha256": protocol.observation_parser_sha256,
            "analysis_plan_sha256": protocol.analysis_plan_sha256,
            "measurement_protocol_sha256": process.measurement_protocol_sha256,
            "measurement_error_model_sha256": process.error_model_sha256,
            "analysis_execution_sha256": digest("f9s6:analysis-execution"),
            "parser_execution_sha256": digest("f9s6:parser-execution"),
            "audit_status": e.ObservationAuditStatus.RESOLVED_ACCEPT,
            "evidence_sha256s": (digest("f9s6:validation-evidence"),),
            "validator_manifest_sha256": self.manifest.manifest_sha256,
            "completed_at": self.completed_at,
        }
        payload.update(self.overrides)
        return payload


def rebuild_validation_request(
    parts: dict[str, Any],
    *,
    policy: e.ObservationValidationPolicy | None = None,
    validator_manifest: e.ObservationValidatorManifest | None = None,
    issued_at=None,
) -> None:
    policy = policy or parts["validation_policy"]
    validator_manifest = validator_manifest or parts["validator_manifest"]
    request = e.build_observation_validation_request(
        validation_id=parts["validation_request"].validation_id,
        committed_selection=parts["committed_selection"],
        observation_receipt=parts["observation_receipt"],
        validator_manifest=validator_manifest,
        policy=policy,
        selection_archive_custody_sha256=parts["selection_archive_custody_sha256"],
        prediction_archive_custody_sha256=parts["prediction_archive_custody_sha256"],
        observation_store_custody_sha256=parts["observation_store_custody_sha256"],
        issued_at=issued_at or parts["validation_request"].issued_at,
    )
    parts["validation_policy"] = policy
    parts["validator_manifest"] = validator_manifest
    parts["validation_request"] = request


def build_f9s6_fixture(
    source_campaign: e.CausalAuditCampaign,
    root: Path,
    *,
    outcome_role: e.HypothesisRole = e.HypothesisRole.PRIMARY,
    sample_count: int = 120,
    candidate_specs: tuple[dict[str, object], ...] | None = None,
) -> dict[str, Any]:
    fixture_kwargs = {"candidate_specs": candidate_specs} if candidate_specs is not None else {}
    selection_parts = build_f9s5_fixture(
        source_campaign,
        root / "f9s5",
        **fixture_kwargs,
    )
    selection = e.run_experiment_selection(
        campaign_id="campaign:f9s6:selection",
        policy=selection_parts["policy"],
        assessor_manifest=selection_parts["assessor_manifest"],
        request=selection_parts["request"],
        prediction_archive=selection_parts["prediction_archive"],
        clock=StepClock(selection_parts["request"].issued_at + timedelta(minutes=1)),
    )
    assert selection.disposition is e.ExperimentSelectionDisposition.READY_SELECTED
    selection_archive = ContentAddressedResponseArchive(root / "selection-archive")
    selection_committed_at = selection.generated_at + timedelta(minutes=1)
    committed_selection = e.commit_experiment_selection_campaign(
        archive=selection_archive,
        campaign=selection,
        committed_at=selection_committed_at,
    )
    selected_candidate = _selected_candidate(selection)
    selected_campaign = selected_candidate.committed_prediction.campaign
    selected_assessment = _selected_assessment(selection)
    confirmation = selected_assessment.fresh_confirmation_batches[0]
    outcome_bin_id = _outcome_for_role(selected_campaign, outcome_role)
    raw_observation = canonical_json_bytes(
        {
            "confirmation_batch_sha256": confirmation.batch_sha256,
            "confirmation_partition_sha256": confirmation.partition_sha256,
            "outcome_bin_id": outcome_bin_id,
            "sample_count": sample_count,
        }
    )
    observation_store = e.ObservationStagingStore(
        root / "observation-store",
        prediction_archive=selection_parts["prediction_archive"],
    )
    observed_at = selection_committed_at + timedelta(minutes=1)
    staged_at = observed_at + timedelta(minutes=1)
    observation_receipt = observation_store.stage_observation(
        committed_campaign=selected_candidate.committed_prediction,
        payload=raw_observation,
        media_type="application/json",
        observed_at=observed_at,
        staged_at=staged_at,
    )
    policy_frozen_at = selection_parts["policy"].frozen_at
    validator_manifest = build_observation_validator_manifest(frozen_at=policy_frozen_at)
    validation_policy = e.ObservationValidationPolicy(
        policy_id="f9s6-observation-validation-policy-v1",
        harness_principal_sha256=digest("f9s6:validation-harness-principal"),
        frozen_at=policy_frozen_at,
    )
    validation_issued_at = staged_at + timedelta(minutes=1)
    selection_custody = digest("f9s6:selection-archive-custody")
    observation_custody = digest("f9s6:observation-store-custody")
    validation_request = e.build_observation_validation_request(
        validation_id="f9s6-observation-validation-request-v1",
        committed_selection=committed_selection,
        observation_receipt=observation_receipt,
        validator_manifest=validator_manifest,
        policy=validation_policy,
        selection_archive_custody_sha256=selection_custody,
        prediction_archive_custody_sha256=selection_parts["archive_custody_sha256"],
        observation_store_custody_sha256=observation_custody,
        issued_at=validation_issued_at,
    )
    validator = FixtureObservationValidator(
        validator_manifest,
        completed_at=validation_issued_at + timedelta(minutes=1),
    )
    validation_campaign = asyncio.run(
        e.run_observation_validation(
            campaign_id="campaign:f9s6:observation-validation",
            policy=validation_policy,
            request=validation_request,
            validator=validator,
            selection_archive=selection_archive,
            prediction_archive=selection_parts["prediction_archive"],
            observation_store=observation_store,
            clock=StepClock(validation_issued_at + timedelta(minutes=2)),
        )
    )
    assert (
        validation_campaign.disposition is e.ObservationValidationDisposition.VALIDATED_CONFIRMATION
    )
    validation_archive = ContentAddressedResponseArchive(root / "validation-archive")
    validation_committed_at = validation_campaign.generated_at + timedelta(minutes=1)
    committed_validation = e.commit_observation_validation_campaign(
        archive=validation_archive,
        campaign=validation_campaign,
        committed_at=validation_committed_at,
    )
    update_policy = e.WorldBeliefUpdatePolicy(
        policy_id="f9s6-world-belief-update-policy-v1",
        harness_principal_sha256=digest("f9s6:update-harness-principal"),
        frozen_at=policy_frozen_at,
    )
    validation_archive_custody = digest("f9s6:validation-archive-custody")
    update_issued_at = validation_committed_at + timedelta(minutes=1)
    update_request = e.build_world_belief_update_request(
        update_id="f9s6-world-belief-update-request-v1",
        committed_validation=committed_validation,
        policy=update_policy,
        validation_archive_custody_sha256=validation_archive_custody,
        issued_at=update_issued_at,
    )
    return {
        **selection_parts,
        "selection_parts": selection_parts,
        "selection": selection,
        "selection_archive": selection_archive,
        "selection_archive_custody_sha256": selection_custody,
        "prediction_archive_custody_sha256": selection_parts["archive_custody_sha256"],
        "committed_selection": committed_selection,
        "selected_candidate": selected_candidate,
        "selected_assessment": selected_assessment,
        "confirmation": confirmation,
        "outcome_bin_id": outcome_bin_id,
        "raw_observation": raw_observation,
        "observation_store": observation_store,
        "observation_store_custody_sha256": observation_custody,
        "observation_receipt": observation_receipt,
        "validator_manifest": validator_manifest,
        "validation_policy": validation_policy,
        "validation_request": validation_request,
        "validator": validator,
        "validation_campaign": validation_campaign,
        "validation_archive": validation_archive,
        "validation_archive_custody_sha256": validation_archive_custody,
        "committed_validation": committed_validation,
        "update_policy": update_policy,
        "update_request": update_request,
        "update_clock": StepClock(update_issued_at + timedelta(minutes=1)),
    }


__all__ = [
    "FixtureObservationValidator",
    "build_f9s6_fixture",
    "build_observation_validator_manifest",
    "rebuild_validation_request",
    "revalidate",
]
