from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import aletheia.epistemics as e
from aletheia.knowledge.response_archive import ContentAddressedResponseArchive

from .f9s2_fixtures import StepClock, digest, revalidate
from .f9s4_fixtures import StaticPredictionAuthor, build_f9s4_fixture


DEFAULT_CANDIDATE_SPECS: tuple[dict[str, object], ...] = (
    {
        "candidate_id": "candidate.efficient",
        "prediction_probability": 0.65,
        "cost": 200_000,
        "duration": 3_600,
        "risk": e.ExperimentRiskLevel.NEGLIGIBLE,
        "fresh_batches": 2,
        "replication_debt_before": 2,
        "replication_reduction": 0,
    },
    {
        "candidate_id": "candidate.high_info",
        "prediction_probability": 0.85,
        "cost": 700_000,
        "duration": 14_400,
        "risk": e.ExperimentRiskLevel.LOW,
        "fresh_batches": 2,
        "replication_debt_before": 2,
        "replication_reduction": 0,
    },
    {
        "candidate_id": "candidate.replication",
        "prediction_probability": 0.70,
        "cost": 500_000,
        "duration": 7_200,
        "risk": e.ExperimentRiskLevel.NEGLIGIBLE,
        "fresh_batches": 2,
        "replication_debt_before": 2,
        "replication_reduction": 2,
    },
)


def _probability_mass(
    bin_ids: tuple[str, ...],
    expected: str,
    expected_probability: float,
) -> tuple[e.OutcomeProbability, ...]:
    remainder = (1.0 - expected_probability) / (len(bin_ids) - 1)
    return tuple(
        e.OutcomeProbability(
            bin_id=bin_id,
            probability=expected_probability if bin_id == expected else remainder,
        )
        for bin_id in sorted(bin_ids)
    )


def _set_prediction_strength(parts: dict[str, Any], probability: float) -> None:
    batch = parts["prediction_batch"]
    bin_ids = tuple(item.bin_id for item in parts["outcome_schema"].bins)
    predictions = tuple(
        revalidate(
            e.HypothesisPrediction,
            prediction,
            probabilities=_probability_mass(
                bin_ids,
                prediction.expected_outcome_bin_id,
                probability,
            ),
            sensitivity_predictions=(
                e.SensitivityPrediction(
                    scenario_id="measurement.high",
                    perturbation_sha256=digest(
                        f"f9s5:sensitivity-high:{prediction.hypothesis_id}:{probability}"
                    ),
                    probabilities=_probability_mass(
                        bin_ids,
                        prediction.expected_outcome_bin_id,
                        probability - 0.03,
                    ),
                ),
                e.SensitivityPrediction(
                    scenario_id="measurement.low",
                    perturbation_sha256=digest(
                        f"f9s5:sensitivity-low:{prediction.hypothesis_id}:{probability}"
                    ),
                    probabilities=_probability_mass(
                        bin_ids,
                        prediction.expected_outcome_bin_id,
                        probability + 0.03,
                    ),
                ),
            ),
        )
        for prediction in batch.predictions
    )
    changed = revalidate(e.PredictionBatch, batch, predictions=predictions)
    parts["prediction_batch"] = changed
    parts["author"] = StaticPredictionAuthor(parts["author_manifest"], changed)
    parts["clock"].current = changed.completed_at + timedelta(minutes=1)


def _set_prediction_scenarios_unaligned(parts: dict[str, Any]) -> None:
    batch = parts["prediction_batch"]
    predictions = tuple(
        revalidate(
            e.HypothesisPrediction,
            prediction,
            sensitivity_predictions=tuple(
                revalidate(
                    e.SensitivityPrediction,
                    scenario,
                    scenario_id=(
                        f"measurement.{prediction.hypothesis_id[-8:]}.{scenario.scenario_id.rsplit('.', 1)[-1]}"
                    ),
                )
                for scenario in prediction.sensitivity_predictions
            ),
        )
        for prediction in batch.predictions
    )
    changed = revalidate(e.PredictionBatch, batch, predictions=predictions)
    parts["prediction_batch"] = changed
    parts["author"] = StaticPredictionAuthor(parts["author_manifest"], changed)
    parts["clock"].current = changed.completed_at + timedelta(minutes=1)


def _set_shared_primary_outcome_likelihood(
    parts: dict[str, Any],
    *,
    source_campaign: e.CausalAuditCampaign,
    shared_likelihood: float,
) -> None:
    snapshot = source_campaign.source_campaign.world_model_snapshot
    assert snapshot is not None
    primary_id = next(
        item.hypothesis_id for item in snapshot.hypotheses if item.role is e.HypothesisRole.PRIMARY
    )
    batch = parts["prediction_batch"]
    primary_outcome = next(
        item.expected_outcome_bin_id
        for item in batch.predictions
        if item.hypothesis_id == primary_id
    )
    bin_ids = tuple(sorted(item.bin_id for item in parts["outcome_schema"].bins))
    predictions: list[e.HypothesisPrediction] = []
    for prediction in batch.predictions:
        if prediction.expected_outcome_bin_id == primary_outcome:
            remaining = (1.0 - shared_likelihood) / (len(bin_ids) - 1)
            values = {
                bin_id: shared_likelihood if bin_id == primary_outcome else remaining
                for bin_id in bin_ids
            }
        else:
            other_bin = next(
                bin_id
                for bin_id in bin_ids
                if bin_id not in {primary_outcome, prediction.expected_outcome_bin_id}
            )
            values = {
                primary_outcome: shared_likelihood,
                prediction.expected_outcome_bin_id: 0.5,
                other_bin: 0.5 - shared_likelihood,
            }
        probabilities = tuple(
            e.OutcomeProbability(bin_id=bin_id, probability=values[bin_id]) for bin_id in bin_ids
        )
        sensitivities = tuple(
            e.SensitivityPrediction(
                scenario_id=scenario_id,
                perturbation_sha256=digest(
                    f"f9s5:shared-sensitivity:{prediction.hypothesis_id}:{scenario_id}"
                ),
                probabilities=probabilities,
            )
            for scenario_id in ("measurement.high", "measurement.low")
        )
        predictions.append(
            revalidate(
                e.HypothesisPrediction,
                prediction,
                probabilities=probabilities,
                sensitivity_predictions=sensitivities,
            )
        )
    changed = revalidate(e.PredictionBatch, batch, predictions=tuple(predictions))
    parts["prediction_batch"] = changed
    parts["author"] = StaticPredictionAuthor(parts["author_manifest"], changed)
    parts["clock"].current = changed.completed_at + timedelta(minutes=1)


def _build_prediction_candidate(
    *,
    source_campaign: e.CausalAuditCampaign,
    spec: dict[str, object],
    archive: ContentAddressedResponseArchive,
) -> e.ExperimentCandidate:
    candidate_id = str(spec["candidate_id"])
    ordinal = bool(spec.get("ordinal", False))
    calibration_trials = int(spec.get("calibration_trials", 30))
    parts = build_f9s4_fixture(
        source_campaign,
        mode=e.PredictionMode.ORDINAL if ordinal else e.PredictionMode.PROBABILISTIC,
        calibration_trials=calibration_trials,
        experiment_id=f"experiment.{candidate_id.removeprefix('candidate.')}",
    )
    if not ordinal:
        _set_prediction_strength(parts, float(spec["prediction_probability"]))
        if spec.get("shared_likelihood_on_primary_bin") is not None:
            _set_shared_primary_outcome_likelihood(
                parts,
                source_campaign=source_campaign,
                shared_likelihood=float(spec["shared_likelihood_on_primary_bin"]),
            )
        if bool(spec.get("unaligned_sensitivity", False)):
            _set_prediction_scenarios_unaligned(parts)
    campaign = asyncio.run(
        e.run_prediction_commitment(
            campaign_id=f"campaign:f9s5:{candidate_id}",
            source_causal_campaign=source_campaign,
            policy=parts["policy"],
            request=parts["request"],
            author=parts["author"],
            calibration_evaluator_manifest=parts["evaluator_manifest"],
            clock=parts["clock"],
        )
    )
    committed = e.commit_prediction_commitment_campaign(
        archive=archive,
        campaign=campaign,
        committed_at=campaign.generated_at + timedelta(minutes=1),
    )
    return e.ExperimentCandidate(
        candidate_id=candidate_id,
        committed_prediction=committed,
    )


def build_assessor_manifest(
    *,
    frozen_at,
    assessor_principal: str | None = None,
) -> e.ExperimentAssessmentManifest:
    return e.ExperimentAssessmentManifest(
        assessor_id="f9s5-independent-experiment-assessor-v1",
        runtime=e.CausalAdapterRuntime.DETERMINISTIC,
        adapter_code_sha256=digest("f9s5:assessor-code"),
        parser_sha256=digest("f9s5:assessor-parser"),
        output_schema_sha256=e.EXPERIMENT_ASSESSMENT_OUTPUT_SCHEMA_SHA256,
        assessor_principal_sha256=(assessor_principal or digest("f9s5:assessor-principal")),
        transport_policy="none",
        frozen_at=frozen_at,
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


def build_candidate_assessment(
    *,
    candidate: e.ExperimentCandidate,
    spec: dict[str, object],
    assessor_manifest: e.ExperimentAssessmentManifest,
    completed_at,
) -> e.CandidateExperimentAssessment:
    campaign = candidate.committed_prediction.campaign
    process = _measurement_process(campaign)
    required = tuple(
        sorted(
            (
                digest(f"f9s5:capability:instrument:{candidate.candidate_id}"),
                digest(f"f9s5:capability:operator:{candidate.candidate_id}"),
            )
        )
    )
    missing_capability = bool(spec.get("missing_capability", False))
    available = required[:-1] if missing_capability else required
    fresh_batches = tuple(
        sorted(
            (
                e.FreshConfirmationBatch(
                    batch_sha256=digest(f"f9s5:fresh-batch:{candidate.candidate_id}:{index}"),
                    partition_sha256=digest(
                        f"f9s5:fresh-partition:{candidate.candidate_id}:{index}"
                    ),
                    custody_receipt_sha256=digest(
                        f"f9s5:fresh-custody:{candidate.candidate_id}:{index}"
                    ),
                    sealed_at=completed_at - timedelta(minutes=1),
                    available_until=completed_at + timedelta(days=30),
                )
                for index in range(int(spec.get("fresh_batches", 2)))
            ),
            key=lambda item: item.batch_sha256,
        )
    )
    replication_reduction = int(spec.get("replication_reduction", 0))
    independent_replication = replication_reduction > 0
    validity = spec.get("measurement_validity_status", e.MeasurementValidityStatus.VALIDATED)
    proxy = spec.get("proxy_risk_status", e.ProxyRiskStatus.NONE)
    return e.CandidateExperimentAssessment(
        candidate_id=candidate.candidate_id,
        prediction_campaign_sha256=campaign.campaign_sha256,
        prediction_commitment_sha256=campaign.commitment_sha256,
        experiment_protocol_sha256=campaign.request.experiment_protocol.protocol_sha256,
        measurement_process_sha256=process.process_sha256,
        measurement_error_model_sha256=process.error_model_sha256,
        measurement_validity_status=validity,
        measurement_validity_confidence=float(spec.get("validity_confidence", 0.95)),
        measurement_validity_evidence_sha256s=(
            digest(f"f9s5:measurement-validity:{candidate.candidate_id}"),
        ),
        proxy_risk_status=proxy,
        proxy_risk_rationale_sha256=digest(f"f9s5:proxy-risk:{candidate.candidate_id}:{proxy}"),
        estimated_cost_microunits=int(spec.get("cost", 500_000)),
        cost_currency=str(spec.get("currency", "NZD")),
        estimated_duration_seconds=int(spec.get("duration", 7_200)),
        risk_level=spec.get("risk", e.ExperimentRiskLevel.LOW),
        risk_assessment_sha256=digest(f"f9s5:risk:{candidate.candidate_id}"),
        required_capability_sha256s=required,
        available_capability_sha256s=available,
        capability_evidence_sha256=digest(f"f9s5:capability-evidence:{candidate.candidate_id}"),
        fresh_confirmation_batches=fresh_batches,
        replication_debt_ledger_sha256=digest(f"f9s5:replication-debt:{candidate.candidate_id}"),
        replication_debt_before=int(spec.get("replication_debt_before", 2)),
        expected_replication_debt_reduction=replication_reduction,
        independent_replication=independent_replication,
        replication_protocol_sha256=(
            digest(f"f9s5:replication-protocol:{candidate.candidate_id}")
            if independent_replication
            else None
        ),
        assessment_evidence_sha256s=(digest(f"f9s5:assessment-evidence:{candidate.candidate_id}"),),
        assessor_manifest_sha256=assessor_manifest.manifest_sha256,
        completed_at=completed_at,
    )


def rebuild_f9s5_request(
    parts: dict[str, Any],
    *,
    assessments: tuple[e.CandidateExperimentAssessment, ...] | None = None,
    candidates: tuple[e.ExperimentCandidate, ...] | None = None,
    policy: e.ExperimentSelectionPolicy | None = None,
    issued_at=None,
) -> None:
    candidates = candidates or parts["candidates"]
    assessments = assessments or parts["assessment_batch"].assessments
    policy = policy or parts["policy"]
    completed_at = max(item.completed_at for item in assessments) + timedelta(minutes=1)
    batch = e.ExperimentAssessmentBatch(
        assessor_manifest_sha256=parts["assessor_manifest"].manifest_sha256,
        assessments=tuple(sorted(assessments, key=lambda item: item.candidate_id)),
        completed_at=completed_at,
    )
    issue_time = issued_at or completed_at + timedelta(minutes=1)
    request = e.build_experiment_selection_request(
        selection_id=parts["request"].selection_id,
        candidates=tuple(sorted(candidates, key=lambda item: item.candidate_id)),
        assessment_batch=batch,
        assessor_manifest=parts["assessor_manifest"],
        policy=policy,
        prediction_archive_custody_sha256=parts["archive_custody_sha256"],
        issued_at=issue_time,
    )
    parts["candidates"] = request.candidates
    parts["assessments"] = batch.assessments
    parts["assessment_batch"] = batch
    parts["policy"] = policy
    parts["request"] = request
    parts["clock"] = StepClock(issue_time + timedelta(minutes=1))


def build_f9s5_fixture(
    source_campaign: e.CausalAuditCampaign,
    root: Path,
    *,
    candidate_specs: tuple[dict[str, object], ...] = DEFAULT_CANDIDATE_SPECS,
) -> dict[str, Any]:
    prediction_archive = ContentAddressedResponseArchive(root / "prediction-archive")
    specs = tuple(sorted(candidate_specs, key=lambda item: str(item["candidate_id"])))
    candidates = tuple(
        _build_prediction_candidate(
            source_campaign=source_campaign,
            spec=spec,
            archive=prediction_archive,
        )
        for spec in specs
    )
    latest_commit = max(item.committed_prediction.committed_at for item in candidates)
    frozen_at = latest_commit + timedelta(minutes=1)
    assessor_manifest = build_assessor_manifest(frozen_at=frozen_at)
    assessments = tuple(
        build_candidate_assessment(
            candidate=candidate,
            spec=spec,
            assessor_manifest=assessor_manifest,
            completed_at=frozen_at + timedelta(minutes=2),
        )
        for candidate, spec in zip(candidates, specs, strict=True)
    )
    assessment_batch = e.ExperimentAssessmentBatch(
        assessor_manifest_sha256=assessor_manifest.manifest_sha256,
        assessments=assessments,
        completed_at=frozen_at + timedelta(minutes=3),
    )
    policy = e.ExperimentSelectionPolicy(
        policy_id="f9s5-constrained-experiment-selection-policy-v1",
        budget_microunits=1_000_000,
        budget_currency="NZD",
        maximum_duration_seconds=24 * 60 * 60,
        maximum_risk_level=e.ExperimentRiskLevel.MODERATE,
        selector_code_sha256=digest("f9s5:selector-code"),
        harness_principal_sha256=digest("f9s5:trusted-harness-principal"),
        frozen_at=frozen_at,
    )
    archive_custody = digest("f9s5:prediction-archive-custody")
    issued_at = assessment_batch.completed_at + timedelta(minutes=1)
    request = e.build_experiment_selection_request(
        selection_id="f9s5-experiment-selection-request-v1",
        candidates=candidates,
        assessment_batch=assessment_batch,
        assessor_manifest=assessor_manifest,
        policy=policy,
        prediction_archive_custody_sha256=archive_custody,
        issued_at=issued_at,
    )
    return {
        "source_campaign": source_campaign,
        "prediction_archive": prediction_archive,
        "archive_custody_sha256": archive_custody,
        "candidate_specs": specs,
        "candidates": candidates,
        "assessor_manifest": assessor_manifest,
        "assessments": assessments,
        "assessment_batch": assessment_batch,
        "policy": policy,
        "request": request,
        "clock": StepClock(issued_at + timedelta(minutes=1)),
    }


__all__ = [
    "DEFAULT_CANDIDATE_SPECS",
    "build_assessor_manifest",
    "build_candidate_assessment",
    "build_f9s5_fixture",
    "rebuild_f9s5_request",
]
