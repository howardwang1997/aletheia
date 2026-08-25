from __future__ import annotations

from datetime import timedelta
from typing import Any

import aletheia.epistemics as e

from .f9s2_fixtures import StepClock, digest


class StaticPredictionAuthor:
    def __init__(self, manifest: e.PredictionAuthorManifest, output: object) -> None:
        self._manifest = manifest
        self.output = output
        self.calls = 0
        self.received: dict[str, object] | None = None

    @property
    def manifest(self) -> e.PredictionAuthorManifest:
        return self._manifest

    async def predict(self, **inputs: object) -> object:
        self.calls += 1
        self.received = dict(inputs)
        if isinstance(self.output, BaseException):
            raise self.output
        return self.output


def build_prediction_manifests(
    source_campaign: e.CausalAuditCampaign,
    *,
    author_principal: str | None = None,
    evaluator_principal: str | None = None,
) -> tuple[
    e.PredictionCommitmentPolicy,
    e.PredictionAuthorManifest,
    e.CalibrationEvaluatorManifest,
]:
    frozen_at = source_campaign.generated_at + timedelta(minutes=10)
    author = e.PredictionAuthorManifest(
        author_id="f9s4-deterministic-likelihood-author-v1",
        runtime=e.CausalAdapterRuntime.DETERMINISTIC,
        adapter_code_sha256=digest("f9s4:author-adapter-code"),
        parser_sha256=digest("f9s4:author-parser"),
        likelihood_family_sha256=digest("f9s4:categorical-likelihood-family-v1"),
        output_schema_sha256=e.PREDICTION_OUTPUT_SCHEMA_SHA256,
        author_principal_sha256=author_principal or digest("f9s4:author-principal"),
        transport_policy="none",
        frozen_at=frozen_at,
    )
    evaluator = e.CalibrationEvaluatorManifest(
        evaluator_id="f9s4-independent-calibration-evaluator-v1",
        runtime=e.CausalAdapterRuntime.DETERMINISTIC,
        adapter_code_sha256=digest("f9s4:calibration-evaluator-code"),
        output_schema_sha256=e.CALIBRATION_REPORT_OUTPUT_SCHEMA_SHA256,
        evaluator_principal_sha256=(
            evaluator_principal or digest("f9s4:calibration-evaluator-principal")
        ),
        transport_policy="none",
        frozen_at=frozen_at,
    )
    policy = e.PredictionCommitmentPolicy(
        policy_id="f9s4-probability-commitment-policy-v1",
        harness_principal_sha256=digest("f9s4:trusted-harness-principal"),
        frozen_at=frozen_at,
    )
    return policy, author, evaluator


def _causal_outcome(source_campaign: e.CausalAuditCampaign):
    assert source_campaign.contract_batch is not None
    contract = source_campaign.contract_batch.contract
    process = next(
        item
        for item in contract.measurement_processes
        if item.process_id == contract.outcome_measurement_process_id
    )
    indicator = next(
        item for item in contract.variables if item.variable_id == process.indicator_variable_id
    )
    return contract, process, indicator


def build_outcome_schema(
    source_campaign: e.CausalAuditCampaign,
    *,
    continuous: bool = False,
) -> e.OutcomeSchema:
    contract, process, indicator = _causal_outcome(source_campaign)
    del contract
    snapshot = source_campaign.source_campaign.world_model_snapshot
    assert snapshot is not None
    outcome_ids = snapshot.predictions[0].outcome_space
    if continuous:
        boundaries = ((None, 0.0), (0.0, 1.0), (1.0, None))
        bins = tuple(
            e.OutcomeBin(
                bin_id=bin_id,
                label=f"Preregistered response interval {index}",
                order_index=index,
                lower_bound=boundaries[index][0],
                upper_bound=boundaries[index][1],
                lower_inclusive=index > 0,
                upper_inclusive=False,
            )
            for index, bin_id in enumerate(outcome_ids)
        )
        kind = e.OutcomeSchemaKind.CONTINUOUS_BINNED
        units = "synthetic response units"
    else:
        bins = tuple(
            e.OutcomeBin(
                bin_id=bin_id,
                label=f"Preregistered categorical response {bin_id}",
                order_index=index,
            )
            for index, bin_id in enumerate(outcome_ids)
        )
        kind = e.OutcomeSchemaKind.CATEGORICAL
        units = None
    assert indicator.observable_id is not None
    return e.OutcomeSchema(
        schema_id="endpoint.outcome.v1",
        kind=kind,
        observable_id=indicator.observable_id,
        measurement_protocol_sha256=process.measurement_protocol_sha256,
        measurement_error_model_sha256=process.error_model_sha256,
        units=units,
        bins=bins,
    )


def build_experiment_protocol(
    source_campaign: e.CausalAuditCampaign,
    outcome_schema: e.OutcomeSchema,
    *,
    experiment_id: str = "experiment.discrimination.v1",
) -> e.ExperimentProtocol:
    contract, process, indicator = _causal_outcome(source_campaign)
    intervention_protocol = digest(f"f9s4:{experiment_id}:intervention")
    analysis_plan = digest(f"f9s4:{experiment_id}:analysis")
    exclusion_rule = digest(f"f9s4:{experiment_id}:exclusion")
    stopping_rule = digest(f"f9s4:{experiment_id}:stopping")
    parser = digest(f"f9s4:{experiment_id}:parser")
    namespace = e.derive_experiment_namespace_sha256(
        experiment_id=experiment_id,
        causal_campaign_sha256=source_campaign.campaign_sha256,
        estimand_sha256=contract.estimand.estimand_sha256,
        intervention_protocol_sha256=intervention_protocol,
        target_population_sha256=contract.estimand.target_population_sha256,
        outcome_schema_sha256=outcome_schema.outcome_schema_sha256,
        analysis_plan_sha256=analysis_plan,
        exclusion_rule_sha256=exclusion_rule,
        stopping_rule_sha256=stopping_rule,
        observation_parser_sha256=parser,
    )
    assert indicator.observable_id is not None
    assert source_campaign.contract_batch is not None
    return e.ExperimentProtocol(
        experiment_id=experiment_id,
        experiment_namespace_sha256=namespace,
        causal_campaign_sha256=source_campaign.campaign_sha256,
        causal_contract_batch_sha256=source_campaign.contract_batch.batch_sha256,
        causal_contract_sha256=contract.contract_sha256,
        estimand_sha256=contract.estimand.estimand_sha256,
        proposed_evidence_kind=contract.estimand.proposed_evidence_kind,
        causal_claim_ceiling=source_campaign.claim_ceiling,
        intervention_protocol_sha256=intervention_protocol,
        target_population_sha256=contract.estimand.target_population_sha256,
        outcome_measurement_process_id=process.process_id,
        observable_id=indicator.observable_id,
        measurement_protocol_sha256=process.measurement_protocol_sha256,
        outcome_schema_sha256=outcome_schema.outcome_schema_sha256,
        analysis_plan_sha256=analysis_plan,
        exclusion_rule_sha256=exclusion_rule,
        stopping_rule_sha256=stopping_rule,
        observation_parser_sha256=parser,
        frozen_at=source_campaign.generated_at + timedelta(minutes=15),
    )


def _probability_mass(
    bin_ids: tuple[str, ...],
    expected: str,
    expected_probability: float,
) -> tuple[e.OutcomeProbability, ...]:
    other = (1.0 - expected_probability) / (len(bin_ids) - 1)
    return tuple(
        e.OutcomeProbability(
            bin_id=bin_id,
            probability=expected_probability if bin_id == expected else other,
        )
        for bin_id in sorted(bin_ids)
    )


def build_calibration_report(
    *,
    source_campaign: e.CausalAuditCampaign,
    outcome_schema: e.OutcomeSchema,
    author_manifest: e.PredictionAuthorManifest,
    evaluator_manifest: e.CalibrationEvaluatorManifest,
    trial_count: int = 30,
    expected_probability: float = 0.8,
    zero_observed_probability: bool = False,
) -> e.LikelihoodCalibrationReport:
    validation_split = digest("f9s4:historical-validation-split")
    bin_ids = tuple(item.bin_id for item in outcome_schema.bins)
    start = source_campaign.generated_at + timedelta(minutes=20)
    trials: list[e.CalibrationTrial] = []
    for index in range(trial_count):
        observed = bin_ids[index % len(bin_ids)]
        if zero_observed_probability:
            remaining = [item for item in sorted(bin_ids) if item != observed]
            probabilities = tuple(
                e.OutcomeProbability(
                    bin_id=bin_id,
                    probability=(
                        0.0
                        if bin_id == observed
                        else (0.75 if bin_id == remaining[0] else 0.25)
                    ),
                )
                for bin_id in sorted(bin_ids)
            )
        else:
            probabilities = _probability_mass(bin_ids, observed, expected_probability)
        predicted_at = start + timedelta(seconds=index * 2)
        trials.append(
            e.CalibrationTrial(
                trial_id=f"trial.{index:05d}",
                validation_namespace_sha256=validation_split,
                probabilities=probabilities,
                observed_bin_id=observed,
                predicted_at=predicted_at,
                observed_at=predicted_at + timedelta(seconds=1),
            )
        )
    frozen_trials = tuple(trials)
    metrics = e.derive_likelihood_calibration_metrics(
        frozen_trials,
        scoring_epsilon=1e-12,
        ece_bins=10,
    )
    return e.LikelihoodCalibrationReport(
        report_id="f9s4-historical-calibration-report-v1",
        predictor_manifest_sha256=author_manifest.manifest_sha256,
        evaluator_manifest_sha256=evaluator_manifest.manifest_sha256,
        outcome_schema_sha256=outcome_schema.outcome_schema_sha256,
        measurement_protocol_sha256=outcome_schema.measurement_protocol_sha256,
        validation_split_sha256=validation_split,
        trials=frozen_trials,
        metrics=metrics,
        completed_at=frozen_trials[-1].observed_at + timedelta(seconds=1),
    )


def build_prediction_batch(
    *,
    source_campaign: e.CausalAuditCampaign,
    request: e.PredictionCommitmentRequest,
    author_manifest: e.PredictionAuthorManifest,
) -> e.PredictionBatch:
    assert source_campaign.contract_batch is not None
    snapshot = source_campaign.source_campaign.world_model_snapshot
    assert snapshot is not None
    contract, process, indicator = _causal_outcome(source_campaign)
    graphs = {item.hypothesis_id: item for item in contract.hypothesis_graphs}
    hypotheses = {item.hypothesis_id: item for item in snapshot.hypotheses}
    source_predictions = {
        item.hypothesis_id: item
        for item in snapshot.predictions
        if item.observable_id == indicator.observable_id
        and item.measurement_protocol_sha256 == process.measurement_protocol_sha256
    }
    bin_ids = tuple(item.bin_id for item in request.outcome_schema.bins)
    predictions: list[e.HypothesisPrediction] = []
    for hypothesis_id in sorted(hypotheses):
        hypothesis = hypotheses[hypothesis_id]
        source_prediction = source_predictions[hypothesis_id]
        common = {
            "hypothesis_id": hypothesis_id,
            "hypothesis_version_sha256": hypothesis.hypothesis_sha256,
            "causal_graph_sha256": graphs[hypothesis_id].graph_sha256,
            "source_prediction_sha256": source_prediction.prediction_sha256,
            "observable_id": indicator.observable_id,
            "measurement_protocol_sha256": process.measurement_protocol_sha256,
            "measurement_error_model_sha256": process.error_model_sha256,
            "expected_outcome_bin_id": source_prediction.expected_outcome,
            "mode": request.prediction_mode,
            "rationale_sha256": digest(f"f9s4:rationale:{hypothesis_id}"),
        }
        if request.prediction_mode is e.PredictionMode.PROBABILISTIC:
            expected = source_prediction.expected_outcome
            predictions.append(
                e.HypothesisPrediction(
                    **common,
                    probabilities=_probability_mass(bin_ids, expected, 0.7),
                    likelihood_model_sha256=e.derive_hypothesis_likelihood_model_sha256(
                        author_manifest=author_manifest,
                        hypothesis_id=hypothesis_id,
                        hypothesis_version_sha256=hypothesis.hypothesis_sha256,
                        experiment_protocol_sha256=request.experiment_protocol.protocol_sha256,
                        outcome_schema_sha256=request.outcome_schema.outcome_schema_sha256,
                    ),
                    sensitivity_predictions=(
                        e.SensitivityPrediction(
                            scenario_id="measurement.high",
                            perturbation_sha256=digest("f9s4:measurement-error-high"),
                            probabilities=_probability_mass(bin_ids, expected, 0.65),
                        ),
                        e.SensitivityPrediction(
                            scenario_id="measurement.low",
                            perturbation_sha256=digest("f9s4:measurement-error-low"),
                            probabilities=_probability_mass(bin_ids, expected, 0.75),
                        ),
                    ),
                )
            )
        else:
            predictions.append(
                e.HypothesisPrediction(
                    **common,
                    ordinal_order=(
                        source_prediction.expected_outcome,
                        *sorted(set(bin_ids) - {source_prediction.expected_outcome}),
                    ),
                )
            )
    return e.PredictionBatch(
        request_sha256=request.request_sha256,
        author_manifest_sha256=author_manifest.manifest_sha256,
        experiment_protocol_sha256=request.experiment_protocol.protocol_sha256,
        outcome_schema_sha256=request.outcome_schema.outcome_schema_sha256,
        predictions=tuple(predictions),
        completed_at=request.issued_at + timedelta(minutes=1),
    )


def build_f9s4_fixture(
    source_campaign: e.CausalAuditCampaign,
    *,
    mode: e.PredictionMode = e.PredictionMode.PROBABILISTIC,
    continuous: bool = False,
    calibration_trials: int = 30,
    calibration_expected_probability: float = 0.8,
    zero_observed_probability: bool = False,
    experiment_id: str = "experiment.discrimination.v1",
) -> dict[str, Any]:
    policy, author_manifest, evaluator_manifest = build_prediction_manifests(source_campaign)
    outcome_schema = build_outcome_schema(source_campaign, continuous=continuous)
    protocol = build_experiment_protocol(
        source_campaign,
        outcome_schema,
        experiment_id=experiment_id,
    )
    calibration_report = None
    if mode is e.PredictionMode.PROBABILISTIC:
        calibration_report = build_calibration_report(
            source_campaign=source_campaign,
            outcome_schema=outcome_schema,
            author_manifest=author_manifest,
            evaluator_manifest=evaluator_manifest,
            trial_count=calibration_trials,
            expected_probability=calibration_expected_probability,
            zero_observed_probability=zero_observed_probability,
        )
    report_time = (
        calibration_report.completed_at
        if calibration_report is not None
        else protocol.frozen_at
    )
    request = e.build_prediction_commitment_request(
        request_id="f9s4-prediction-commitment-request-v1",
        source_causal_campaign=source_campaign,
        prediction_mode=mode,
        experiment_protocol=protocol,
        outcome_schema=outcome_schema,
        policy=policy,
        author_manifest=author_manifest,
        calibration_evaluator_manifest=evaluator_manifest,
        calibration_report=calibration_report,
        issued_at=max(protocol.frozen_at, report_time) + timedelta(minutes=1),
    )
    batch = build_prediction_batch(
        source_campaign=source_campaign,
        request=request,
        author_manifest=author_manifest,
    )
    return {
        "source_campaign": source_campaign,
        "policy": policy,
        "author_manifest": author_manifest,
        "evaluator_manifest": evaluator_manifest,
        "outcome_schema": outcome_schema,
        "protocol": protocol,
        "calibration_report": calibration_report,
        "request": request,
        "prediction_batch": batch,
        "author": StaticPredictionAuthor(author_manifest, batch),
        "clock": StepClock(batch.completed_at + timedelta(minutes=1)),
    }


__all__ = [
    "StaticPredictionAuthor",
    "build_calibration_report",
    "build_experiment_protocol",
    "build_f9s4_fixture",
    "build_outcome_schema",
    "build_prediction_batch",
    "build_prediction_manifests",
]
