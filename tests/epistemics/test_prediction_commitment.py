from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from pydantic import ValidationError

import aletheia.epistemics as e
from aletheia.knowledge.response_archive import (
    ContentAddressedResponseArchive,
    ResponseArchiveCorruption,
)
from knowledge.f8s5_fixtures import build_f8s5_direction_fixture, build_f8s5_live_fixture

from .f9s2_fixtures import build_f9s2_fixture, digest, revalidate
from .f9s3_fixtures import build_f9s3_fixture
from .f9s4_fixtures import (
    StaticPredictionAuthor,
    build_calibration_report,
    build_experiment_protocol,
    build_f9s4_fixture,
    build_outcome_schema,
    build_prediction_manifests,
)


@pytest.fixture(scope="module")
def source_fixture(tmp_path_factory):
    live = asyncio.run(
        build_f8s5_live_fixture(
            tmp_path_factory.mktemp("f9s4-strong"),
            novelty_kind="strong",
        )
    )
    gate = build_f8s5_direction_fixture(live)["gate"]
    hypotheses = build_f9s2_fixture(gate)
    hypothesis_campaign = asyncio.run(
        e.run_competing_hypothesis_generation(
            campaign_id="campaign:f9s4:source-hypotheses",
            direction_gate=hypotheses["gate"],
            policy=hypotheses["policy"],
            request=hypotheses["request"],
            generator=hypotheses["generator"],
            deduplicator=hypotheses["deduplicator"],
            clock=hypotheses["clock"],
        )
    )
    causal = build_f9s3_fixture(hypothesis_campaign)
    causal_campaign = asyncio.run(
        e.run_causal_identification_audit(
            campaign_id="campaign:f9s4:source-causal-audit",
            source_campaign=causal["source_campaign"],
            policy=causal["policy"],
            request=causal["request"],
            author=causal["author"],
            reviewer=causal["reviewer"],
            clock=causal["clock"],
        )
    )
    assert causal_campaign.disposition is e.CausalAuditDisposition.READY_IDENTIFIED
    return causal_campaign


async def _run(parts, campaign_id: str = "campaign:f9s4:test"):
    return await e.run_prediction_commitment(
        campaign_id=campaign_id,
        source_causal_campaign=parts["source_campaign"],
        policy=parts["policy"],
        request=parts["request"],
        author=parts["author"],
        calibration_evaluator_manifest=parts["evaluator_manifest"],
        clock=parts["clock"],
    )


def _install_batch(parts, batch: e.PredictionBatch) -> None:
    parts["prediction_batch"] = batch
    parts["author"] = StaticPredictionAuthor(parts["author_manifest"], batch)
    parts["clock"].current = batch.completed_at + timedelta(minutes=1)


def _replace_prediction(
    batch: e.PredictionBatch,
    hypothesis_id: str,
    **updates: object,
) -> e.PredictionBatch:
    predictions = tuple(
        revalidate(e.HypothesisPrediction, item, **updates)
        if item.hypothesis_id == hypothesis_id
        else item
        for item in batch.predictions
    )
    return revalidate(e.PredictionBatch, batch, predictions=predictions)


def _mass(
    bin_ids: tuple[str, ...], expected: str, expected_probability: float
) -> tuple[e.OutcomeProbability, ...]:
    other = (1.0 - expected_probability) / (len(bin_ids) - 1)
    return tuple(
        e.OutcomeProbability(
            bin_id=bin_id,
            probability=expected_probability if bin_id == expected else other,
        )
        for bin_id in sorted(bin_ids)
    )


@pytest.mark.asyncio
async def test_exact_f8_f9_chain_produces_ready_probabilistic_commitment(
    source_fixture,
) -> None:
    parts = build_f9s4_fixture(source_fixture)

    campaign = await _run(parts, "campaign:f9s4:ready-probabilistic")

    assert campaign.disposition is e.PredictionCommitmentDisposition.READY
    assert campaign.eig_eligible is True
    assert campaign.blockers == ()
    assert campaign.failure is None
    assert campaign.prediction_batch is not None
    assert campaign.probe is not None
    assert len(campaign.prediction_batch.predictions) == 3
    assert len(campaign.probe.pairwise_discrimination) == 3
    assert all(
        item.total_variation_distance is not None
        and item.total_variation_distance >= parts["policy"].minimum_pairwise_total_variation
        for item in campaign.probe.pairwise_discrimination
    )
    assert campaign.request.calibration_report is not None
    assert campaign.request.calibration_report.metrics.trial_count == 30
    assert parts["author"].received == {
        "request": parts["request"],
        "source_causal_campaign": source_fixture,
    }
    assert parts["author_manifest"].observation_access == "none"
    assert parts["author_manifest"].tool_names == ()


@pytest.mark.asyncio
async def test_ordinal_commitment_is_ready_but_never_eig_eligible(source_fixture) -> None:
    parts = build_f9s4_fixture(source_fixture, mode=e.PredictionMode.ORDINAL)

    campaign = await _run(parts, "campaign:f9s4:ready-ordinal")

    assert campaign.disposition is e.PredictionCommitmentDisposition.READY
    assert campaign.eig_eligible is False
    assert campaign.request.calibration_report is None
    assert all(
        item.total_variation_distance is None and item.ordinal_order_differs is True
        for item in campaign.probe.pairwise_discrimination
    )
    assert all(
        item.probabilities == () and item.likelihood_model_sha256 is None
        for item in campaign.prediction_batch.predictions
    )


@pytest.mark.asyncio
async def test_continuous_outcome_bins_are_preregistered_before_prediction(
    source_fixture,
) -> None:
    parts = build_f9s4_fixture(source_fixture, continuous=True)
    campaign = await _run(parts, "campaign:f9s4:continuous-bins")

    assert campaign.disposition is e.PredictionCommitmentDisposition.READY
    schema = campaign.request.outcome_schema
    assert schema.kind is e.OutcomeSchemaKind.CONTINUOUS_BINNED
    assert schema.bins[0].lower_bound is None
    assert schema.bins[-1].upper_bound is None

    gap = revalidate(e.OutcomeBin, schema.bins[1], lower_bound=0.1)
    with pytest.raises(ValidationError, match="contiguous"):
        revalidate(e.OutcomeSchema, schema, bins=(schema.bins[0], gap, schema.bins[2]))
    unowned = revalidate(e.OutcomeBin, schema.bins[1], lower_inclusive=False)
    with pytest.raises(ValidationError, match="exactly one"):
        revalidate(e.OutcomeSchema, schema, bins=(schema.bins[0], unowned, schema.bins[2]))


def test_probability_and_calibration_metrics_cannot_be_forged(source_fixture) -> None:
    parts = build_f9s4_fixture(source_fixture)
    batch = parts["prediction_batch"]
    prediction = batch.predictions[0]
    with pytest.raises(ValidationError, match="sum to one"):
        revalidate(
            e.HypothesisPrediction,
            prediction,
            probabilities=tuple(
                revalidate(e.OutcomeProbability, item, probability=0.2)
                for item in prediction.probabilities
            ),
        )
    report = parts["calibration_report"]
    forged_metrics = revalidate(
        e.LikelihoodCalibrationMetrics,
        report.metrics,
        multiclass_brier_score=0.0,
    )
    with pytest.raises(ValidationError, match="not mechanically derived"):
        revalidate(e.LikelihoodCalibrationReport, report, metrics=forged_metrics)
    trial = report.trials[0]
    with pytest.raises(ValidationError, match="must precede"):
        revalidate(e.CalibrationTrial, trial, predicted_at=trial.observed_at)


def test_calibration_evaluator_is_independent_and_unprivileged(source_fixture) -> None:
    shared = digest("f9s4:shared-principal")
    policy, author, evaluator = build_prediction_manifests(
        source_fixture,
        author_principal=shared,
        evaluator_principal=shared,
    )
    schema = build_outcome_schema(source_fixture)
    protocol = build_experiment_protocol(source_fixture, schema)
    report = build_calibration_report(
        source_campaign=source_fixture,
        outcome_schema=schema,
        author_manifest=author,
        evaluator_manifest=evaluator,
    )
    with pytest.raises(ValueError, match="must be independent"):
        e.build_prediction_commitment_request(
            request_id="f9s4-non-independent-request",
            source_causal_campaign=source_fixture,
            prediction_mode=e.PredictionMode.PROBABILISTIC,
            experiment_protocol=protocol,
            outcome_schema=schema,
            policy=policy,
            author_manifest=author,
            calibration_evaluator_manifest=evaluator,
            calibration_report=report,
            issued_at=report.completed_at + timedelta(minutes=1),
        )
    with pytest.raises(ValidationError, match="ambient tool authority"):
        revalidate(e.CalibrationEvaluatorManifest, evaluator, tool_names=("observation.read",))
    with pytest.raises(ValidationError, match="tool authority"):
        revalidate(e.PredictionAuthorManifest, author, tool_names=("search",))


def test_calibration_cannot_use_target_namespace_or_future_report(source_fixture) -> None:
    parts = build_f9s4_fixture(source_fixture)
    report = parts["calibration_report"]
    target_trials = tuple(
        revalidate(
            e.CalibrationTrial,
            item,
            validation_namespace_sha256=parts["protocol"].experiment_namespace_sha256,
        )
        for item in report.trials
    )
    target_report = revalidate(
        e.LikelihoodCalibrationReport,
        report,
        validation_split_sha256=parts["protocol"].experiment_namespace_sha256,
        trials=target_trials,
    )
    with pytest.raises(ValueError, match="cannot calibrate its own predictor"):
        e.build_prediction_commitment_request(
            request_id="f9s4-target-leakage-request",
            source_causal_campaign=source_fixture,
            prediction_mode=e.PredictionMode.PROBABILISTIC,
            experiment_protocol=parts["protocol"],
            outcome_schema=parts["outcome_schema"],
            policy=parts["policy"],
            author_manifest=parts["author_manifest"],
            calibration_evaluator_manifest=parts["evaluator_manifest"],
            calibration_report=target_report,
            issued_at=target_report.completed_at + timedelta(minutes=1),
        )
    with pytest.raises(ValueError, match="predates its calibration report"):
        e.build_prediction_commitment_request(
            request_id="f9s4-future-report-request",
            source_causal_campaign=source_fixture,
            prediction_mode=e.PredictionMode.PROBABILISTIC,
            experiment_protocol=parts["protocol"],
            outcome_schema=parts["outcome_schema"],
            policy=parts["policy"],
            author_manifest=parts["author_manifest"],
            calibration_evaluator_manifest=parts["evaluator_manifest"],
            calibration_report=report,
            issued_at=report.completed_at - timedelta(microseconds=1),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture_updates", "expected_blocker"),
    [
        ({"calibration_trials": 10}, "calibration:insufficient_trials"),
        (
            {"zero_observed_probability": True},
            "calibration:zero_probability_observations",
        ),
        (
            {"calibration_expected_probability": 0.5},
            "calibration:top_label_ece",
        ),
    ],
)
async def test_bad_historical_calibration_blocks_commitment(
    source_fixture,
    fixture_updates,
    expected_blocker,
) -> None:
    parts = build_f9s4_fixture(source_fixture, **fixture_updates)
    campaign = await _run(parts, f"campaign:f9s4:{expected_blocker.replace(':', '-')}")

    assert campaign.disposition is e.PredictionCommitmentDisposition.BLOCKED_CALIBRATION
    assert campaign.eig_eligible is False
    assert expected_blocker in campaign.blockers


@pytest.mark.asyncio
async def test_extreme_low_entropy_predictions_are_blocked(source_fixture) -> None:
    parts = build_f9s4_fixture(source_fixture)
    batch = parts["prediction_batch"]
    bin_ids = tuple(item.bin_id for item in parts["outcome_schema"].bins)
    predictions = tuple(
        revalidate(
            e.HypothesisPrediction,
            item,
            probabilities=_mass(bin_ids, item.expected_outcome_bin_id, 0.99),
        )
        for item in batch.predictions
    )
    _install_batch(parts, revalidate(e.PredictionBatch, batch, predictions=predictions))

    campaign = await _run(parts, "campaign:f9s4:extreme")

    assert campaign.disposition is e.PredictionCommitmentDisposition.BLOCKED_DEGENERACY
    assert any("probability_below_floor" in item for item in campaign.blockers)
    assert any("probability_above_ceiling" in item for item in campaign.blockers)
    assert any("entropy_below_floor" in item for item in campaign.blockers)


@pytest.mark.asyncio
async def test_nearly_identical_hypothesis_likelihoods_are_blocked(source_fixture) -> None:
    parts = build_f9s4_fixture(source_fixture)
    batch = parts["prediction_batch"]
    bin_ids = tuple(item.bin_id for item in parts["outcome_schema"].bins)
    predictions = tuple(
        revalidate(
            e.HypothesisPrediction,
            item,
            probabilities=_mass(bin_ids, item.expected_outcome_bin_id, 0.34),
            sensitivity_predictions=(
                e.SensitivityPrediction(
                    scenario_id="measurement.high",
                    perturbation_sha256=digest("f9s4:near-identical-high"),
                    probabilities=_mass(bin_ids, item.expected_outcome_bin_id, 0.35),
                ),
                e.SensitivityPrediction(
                    scenario_id="measurement.low",
                    perturbation_sha256=digest("f9s4:near-identical-low"),
                    probabilities=_mass(bin_ids, item.expected_outcome_bin_id, 0.36),
                ),
            ),
        )
        for item in batch.predictions
    )
    _install_batch(parts, revalidate(e.PredictionBatch, batch, predictions=predictions))

    campaign = await _run(parts, "campaign:f9s4:near-identical")

    assert campaign.disposition is e.PredictionCommitmentDisposition.BLOCKED_DEGENERACY
    assert any("pairwise_tv" in item for item in campaign.blockers)


@pytest.mark.asyncio
async def test_measurement_sensitivity_instability_is_blocked(source_fixture) -> None:
    parts = build_f9s4_fixture(source_fixture)
    batch = parts["prediction_batch"]
    first = batch.predictions[0]
    bins = tuple(item.bin_id for item in parts["outcome_schema"].bins)
    other_expected = next(item for item in bins if item != first.expected_outcome_bin_id)
    unstable = (
        *first.sensitivity_predictions[:1],
        e.SensitivityPrediction(
            scenario_id="measurement.low",
            perturbation_sha256=digest("f9s4:unstable-measurement"),
            probabilities=_mass(bins, other_expected, 0.7),
        ),
    )
    _install_batch(
        parts,
        _replace_prediction(
            batch,
            first.hypothesis_id,
            sensitivity_predictions=unstable,
        ),
    )

    campaign = await _run(parts, "campaign:f9s4:sensitivity-unstable")

    assert campaign.disposition is e.PredictionCommitmentDisposition.BLOCKED_DEGENERACY
    assert any("sensitivity_instability" in item for item in campaign.blockers)


@pytest.mark.asyncio
async def test_changed_f9s2_prediction_binding_becomes_hash_only_execution_failure(
    source_fixture,
) -> None:
    parts = build_f9s4_fixture(source_fixture)
    batch = parts["prediction_batch"]
    first = batch.predictions[0]
    rebound = _replace_prediction(
        batch,
        first.hypothesis_id,
        source_prediction_sha256=digest("f9s4:forged-source-prediction"),
    )
    _install_batch(parts, rebound)

    campaign = await _run(parts, "campaign:f9s4:forged-binding")

    assert campaign.disposition is e.PredictionCommitmentDisposition.BLOCKED_EXECUTION
    assert campaign.failure.kind is e.PredictionCommitmentFailureKind.AUTHOR_OUTPUT_INVALID
    assert campaign.prediction_batch is None
    assert campaign.failure.raw_output_sha256 == rebound.batch_sha256


@pytest.mark.asyncio
async def test_missing_reordered_and_future_prediction_outputs_are_rejected(
    source_fixture,
) -> None:
    missing = build_f9s4_fixture(source_fixture)
    raw_missing = missing["prediction_batch"].model_dump(mode="python")
    raw_missing["predictions"] = raw_missing["predictions"][:-1]
    missing["author"] = StaticPredictionAuthor(missing["author_manifest"], raw_missing)
    missing_campaign = await _run(missing, "campaign:f9s4:missing")
    assert missing_campaign.disposition is e.PredictionCommitmentDisposition.BLOCKED_EXECUTION

    reordered = build_f9s4_fixture(source_fixture)
    raw_reordered = reordered["prediction_batch"].model_dump(mode="python")
    raw_reordered["predictions"] = tuple(reversed(raw_reordered["predictions"]))
    reordered["author"] = StaticPredictionAuthor(reordered["author_manifest"], raw_reordered)
    reordered_campaign = await _run(reordered, "campaign:f9s4:reordered")
    assert reordered_campaign.disposition is e.PredictionCommitmentDisposition.BLOCKED_EXECUTION

    future = build_f9s4_fixture(source_fixture)
    future_batch = revalidate(
        e.PredictionBatch,
        future["prediction_batch"],
        completed_at=future["clock"].current + timedelta(days=1),
    )
    _install_batch(future, future_batch)
    future["clock"].current = future_batch.completed_at - timedelta(hours=1)
    future_campaign = await _run(future, "campaign:f9s4:future")
    assert future_campaign.disposition is e.PredictionCommitmentDisposition.BLOCKED_EXECUTION


@pytest.mark.asyncio
async def test_author_exception_and_raw_invalid_output_never_leak_details(source_fixture) -> None:
    exception = build_f9s4_fixture(source_fixture)
    exception["author"] = StaticPredictionAuthor(
        exception["author_manifest"], RuntimeError("private observation secret")
    )
    failed = await _run(exception, "campaign:f9s4:author-error")
    assert failed.failure.kind is e.PredictionCommitmentFailureKind.AUTHOR_ERROR
    assert "private observation secret" not in failed.model_dump_json()

    invalid = build_f9s4_fixture(source_fixture)
    invalid["author"] = StaticPredictionAuthor(
        invalid["author_manifest"], {"secret": "unreleased result"}
    )
    failed = await _run(invalid, "campaign:f9s4:author-invalid")
    assert failed.failure.kind is e.PredictionCommitmentFailureKind.AUTHOR_OUTPUT_INVALID
    assert failed.failure.raw_output_sha256 is not None
    assert "unreleased result" not in failed.model_dump_json()


def test_prediction_mode_and_schema_hash_contracts_are_frozen(source_fixture) -> None:
    parts = build_f9s4_fixture(source_fixture)
    assert parts["author_manifest"].output_schema_sha256 == e.PREDICTION_OUTPUT_SCHEMA_SHA256
    assert (
        parts["evaluator_manifest"].output_schema_sha256
        == e.CALIBRATION_REPORT_OUTPUT_SCHEMA_SHA256
    )
    request_payload = parts["request"].model_dump(mode="python")
    request_payload["calibration_report"] = None
    with pytest.raises(ValidationError, match="requires a frozen calibration report"):
        e.PredictionCommitmentRequest.model_validate(request_payload)

    ordinal = build_f9s4_fixture(source_fixture, mode=e.PredictionMode.ORDINAL)
    ordinal_payload = ordinal["request"].model_dump(mode="python")
    ordinal_payload["calibration_report"] = parts["calibration_report"]
    with pytest.raises(ValidationError, match="ordinal prediction cannot attach"):
        e.PredictionCommitmentRequest.model_validate(ordinal_payload)


@pytest.mark.asyncio
async def test_likelihood_model_must_derive_from_calibrated_family(source_fixture) -> None:
    parts = build_f9s4_fixture(source_fixture)
    batch = parts["prediction_batch"]
    first = batch.predictions[0]
    changed = _replace_prediction(
        batch,
        first.hypothesis_id,
        likelihood_model_sha256=digest("f9s4:uncalibrated-likelihood-substitution"),
    )
    _install_batch(parts, changed)

    campaign = await _run(parts, "campaign:f9s4:likelihood-substitution")

    assert campaign.disposition is e.PredictionCommitmentDisposition.BLOCKED_EXECUTION
    assert campaign.failure.kind is e.PredictionCommitmentFailureKind.AUTHOR_OUTPUT_INVALID


@pytest.mark.asyncio
async def test_missing_sensitivity_scenarios_block_probability_commitment(
    source_fixture,
) -> None:
    parts = build_f9s4_fixture(source_fixture)
    batch = parts["prediction_batch"]
    predictions = tuple(
        revalidate(e.HypothesisPrediction, item, sensitivity_predictions=())
        for item in batch.predictions
    )
    _install_batch(parts, revalidate(e.PredictionBatch, batch, predictions=predictions))

    campaign = await _run(parts, "campaign:f9s4:no-sensitivity")

    assert campaign.disposition is e.PredictionCommitmentDisposition.BLOCKED_DEGENERACY
    assert all(
        item.sensitivity_scenario_count == 0
        for item in campaign.probe.hypothesis_diagnostics
    )
    assert any("insufficient_sensitivity_scenarios" in item for item in campaign.blockers)


def test_calibration_trials_cannot_predate_frozen_predictor(source_fixture) -> None:
    parts = build_f9s4_fixture(source_fixture)
    report = parts["calibration_report"]
    first = report.trials[0]
    early_prediction = parts["author_manifest"].frozen_at - timedelta(seconds=2)
    early_trial = revalidate(
        e.CalibrationTrial,
        first,
        predicted_at=early_prediction,
        observed_at=early_prediction + timedelta(seconds=1),
    )
    early_report = revalidate(
        e.LikelihoodCalibrationReport,
        report,
        trials=(early_trial, *report.trials[1:]),
    )
    with pytest.raises(ValueError, match="predates its frozen predictor"):
        e.build_prediction_commitment_request(
            request_id="f9s4-pre-manifest-calibration",
            source_causal_campaign=source_fixture,
            prediction_mode=e.PredictionMode.PROBABILISTIC,
            experiment_protocol=parts["protocol"],
            outcome_schema=parts["outcome_schema"],
            policy=parts["policy"],
            author_manifest=parts["author_manifest"],
            calibration_evaluator_manifest=parts["evaluator_manifest"],
            calibration_report=early_report,
            issued_at=parts["request"].issued_at,
        )


@pytest.mark.asyncio
async def test_ready_bounded_causal_source_does_not_gain_a_higher_claim_ceiling(
    source_fixture,
) -> None:
    causal = build_f9s3_fixture(
        source_fixture.source_campaign,
        latent_confounding=True,
    )
    bounded_source = await e.run_causal_identification_audit(
        campaign_id="campaign:f9s4:bounded-causal-source",
        source_campaign=causal["source_campaign"],
        policy=causal["policy"],
        request=causal["request"],
        author=causal["author"],
        reviewer=causal["reviewer"],
        clock=causal["clock"],
    )
    assert bounded_source.disposition is e.CausalAuditDisposition.READY_BOUNDED
    parts = build_f9s4_fixture(bounded_source)

    campaign = await _run(parts, "campaign:f9s4:bounded-prediction")

    assert campaign.disposition is e.PredictionCommitmentDisposition.READY
    assert (
        campaign.request.experiment_protocol.causal_claim_ceiling
        is e.CausalClaimCeiling.ASSOCIATION_ONLY
    )


@pytest.mark.asyncio
async def test_rejected_causal_assumption_cannot_issue_prediction_request(
    source_fixture,
) -> None:
    causal = build_f9s3_fixture(
        source_fixture.source_campaign,
        decisions={"assumption.consistency": e.AssumptionReviewDecision.REJECT},
    )
    blocked_source = await e.run_causal_identification_audit(
        campaign_id="campaign:f9s4:blocked-causal-source",
        source_campaign=causal["source_campaign"],
        policy=causal["policy"],
        request=causal["request"],
        author=causal["author"],
        reviewer=causal["reviewer"],
        clock=causal["clock"],
    )
    assert blocked_source.disposition is e.CausalAuditDisposition.BLOCKED_ASSUMPTIONS
    with pytest.raises(ValueError, match="does not authorize prediction planning"):
        build_f9s4_fixture(blocked_source)


def test_commitment_time_cannot_predate_generated_campaign(source_fixture, tmp_path) -> None:
    parts = build_f9s4_fixture(source_fixture)
    campaign = asyncio.run(_run(parts, "campaign:f9s4:predated-commit"))
    archive = ContentAddressedResponseArchive(tmp_path / "prediction-archive")
    with pytest.raises(ValueError, match="cannot predate"):
        e.commit_prediction_commitment_campaign(
            archive=archive,
            campaign=campaign,
            committed_at=campaign.generated_at - timedelta(microseconds=1),
        )


def test_model_calibration_evaluator_cannot_reuse_prediction_model(source_fixture) -> None:
    policy, author, evaluator = build_prediction_manifests(source_fixture)
    shared_model = digest("f9s4:shared-model-identity")
    model_author = revalidate(
        e.PredictionAuthorManifest,
        author,
        runtime=e.CausalAdapterRuntime.MODEL,
        instruction_sha256=digest("f9s4:model-author-instruction"),
        model_identity_sha256=shared_model,
        transport_policy="model_transport_only",
    )
    model_evaluator = revalidate(
        e.CalibrationEvaluatorManifest,
        evaluator,
        runtime=e.CausalAdapterRuntime.MODEL,
        instruction_sha256=digest("f9s4:model-evaluator-instruction"),
        model_identity_sha256=shared_model,
        transport_policy="model_transport_only",
    )
    schema = build_outcome_schema(source_fixture)
    protocol = build_experiment_protocol(source_fixture, schema)
    report = build_calibration_report(
        source_campaign=source_fixture,
        outcome_schema=schema,
        author_manifest=model_author,
        evaluator_manifest=model_evaluator,
    )
    with pytest.raises(ValueError, match="independent model identity"):
        e.build_prediction_commitment_request(
            request_id="f9s4-shared-model-request",
            source_causal_campaign=source_fixture,
            prediction_mode=e.PredictionMode.PROBABILISTIC,
            experiment_protocol=protocol,
            outcome_schema=schema,
            policy=policy,
            author_manifest=model_author,
            calibration_evaluator_manifest=model_evaluator,
            calibration_report=report,
            issued_at=report.completed_at + timedelta(minutes=1),
        )


def test_outcome_schema_cannot_rebind_causal_measurement_error(source_fixture) -> None:
    policy, author, evaluator = build_prediction_manifests(source_fixture)
    schema = build_outcome_schema(source_fixture)
    rebound_schema = revalidate(
        e.OutcomeSchema,
        schema,
        measurement_error_model_sha256=digest("f9s4:substituted-error-model"),
    )
    protocol = build_experiment_protocol(source_fixture, rebound_schema)
    report = build_calibration_report(
        source_campaign=source_fixture,
        outcome_schema=rebound_schema,
        author_manifest=author,
        evaluator_manifest=evaluator,
    )
    with pytest.raises(ValueError, match="changed the causal measurement binding"):
        e.build_prediction_commitment_request(
            request_id="f9s4-measurement-rebind-request",
            source_causal_campaign=source_fixture,
            prediction_mode=e.PredictionMode.PROBABILISTIC,
            experiment_protocol=protocol,
            outcome_schema=rebound_schema,
            policy=policy,
            author_manifest=author,
            calibration_evaluator_manifest=evaluator,
            calibration_report=report,
            issued_at=report.completed_at + timedelta(minutes=1),
        )


@pytest.mark.asyncio
async def test_observation_and_namespace_seal_corruption_are_detected(
    source_fixture,
    tmp_path,
) -> None:
    parts = build_f9s4_fixture(source_fixture)
    campaign = await _run(parts, "campaign:f9s4:observation-corruption")
    archive = ContentAddressedResponseArchive(tmp_path / "prediction-archive")
    committed = e.commit_prediction_commitment_campaign(
        archive=archive,
        campaign=campaign,
        committed_at=campaign.generated_at + timedelta(minutes=1),
    )

    raw_store = e.ObservationStagingStore(
        tmp_path / "raw-corruption",
        prediction_archive=archive,
    )
    raw_receipt = raw_store.stage_observation(
        committed_campaign=committed,
        payload=b"immutable-observation",
        media_type="application/octet-stream",
        observed_at=committed.committed_at + timedelta(minutes=1),
        staged_at=committed.committed_at + timedelta(minutes=2),
    )
    raw_path = raw_store.root / raw_receipt.relative_path
    raw_path.chmod(0o600)
    raw_path.write_bytes(b"x" * raw_receipt.observation_bytes)
    with pytest.raises(e.ObservationStagingError, match="content hash changed"):
        raw_store.read_observation(raw_receipt)

    seal_store = e.ObservationStagingStore(
        tmp_path / "seal-corruption",
        prediction_archive=archive,
    )
    seal_receipt = seal_store.stage_observation(
        committed_campaign=committed,
        payload=b"another-observation",
        media_type="application/octet-stream",
        observed_at=committed.committed_at + timedelta(minutes=3),
        staged_at=committed.committed_at + timedelta(minutes=4),
    )
    seal_path = next((seal_store.root / "seals").rglob("*.json"))
    seal_path.chmod(0o600)
    seal_payload = seal_path.read_bytes()
    seal_path.write_bytes(seal_payload[:-1] + b"0")
    with pytest.raises(e.ObservationStagingError, match="seal is invalid"):
        seal_store.read_observation(seal_receipt)


@pytest.mark.asyncio
async def test_campaign_rederives_probe_and_archive_rehashes_bytes(
    source_fixture,
    tmp_path,
) -> None:
    parts = build_f9s4_fixture(source_fixture)
    campaign = await _run(parts, "campaign:f9s4:archive")
    with pytest.raises(ValidationError, match="not mechanically derived"):
        revalidate(
            e.PredictionCommitmentCampaign,
            campaign,
            eig_eligible=False,
        )

    archive = ContentAddressedResponseArchive(tmp_path / "archive")
    committed = e.commit_prediction_commitment_campaign(
        archive=archive,
        campaign=campaign,
        committed_at=campaign.generated_at + timedelta(minutes=1),
    )
    assert (
        e.load_prediction_commitment_campaign(archive=archive, ledger=committed.ledger)
        == campaign
    )
    ledger_path = archive.root / committed.ledger.relative_path
    ledger_path.chmod(0o600)
    payload = ledger_path.read_bytes()
    ledger_path.write_bytes(payload[:-1] + (b"0" if payload[-1:] != b"0" else b"1"))
    with pytest.raises(ResponseArchiveCorruption):
        e.load_prediction_commitment_campaign(archive=archive, ledger=committed.ledger)


@pytest.mark.asyncio
async def test_observation_staging_requires_ready_prior_archived_commitment(
    source_fixture,
    tmp_path,
) -> None:
    parts = build_f9s4_fixture(source_fixture, calibration_trials=10)
    blocked = await _run(parts, "campaign:f9s4:blocked-observation")
    assert blocked.disposition is e.PredictionCommitmentDisposition.BLOCKED_CALIBRATION
    archive = ContentAddressedResponseArchive(tmp_path / "prediction-archive")
    committed = e.commit_prediction_commitment_campaign(
        archive=archive,
        campaign=blocked,
        committed_at=blocked.generated_at + timedelta(minutes=1),
    )
    store = e.ObservationStagingStore(
        tmp_path / "observations",
        prediction_archive=archive,
    )
    with pytest.raises(e.ObservationStagingError, match="only a ready"):
        store.stage_observation(
            committed_campaign=committed,
            payload=b"hidden result",
            media_type="application/octet-stream",
            observed_at=committed.committed_at + timedelta(minutes=1),
            staged_at=committed.committed_at + timedelta(minutes=2),
        )
    assert not list(store.root.rglob("*.observation"))


@pytest.mark.asyncio
async def test_precommit_observation_is_rejected_without_raw_write(source_fixture, tmp_path) -> None:
    parts = build_f9s4_fixture(source_fixture)
    campaign = await _run(parts, "campaign:f9s4:precommit-observation")
    archive = ContentAddressedResponseArchive(tmp_path / "prediction-archive")
    committed = e.commit_prediction_commitment_campaign(
        archive=archive,
        campaign=campaign,
        committed_at=campaign.generated_at + timedelta(minutes=1),
    )
    store = e.ObservationStagingStore(
        tmp_path / "observations",
        prediction_archive=archive,
    )
    with pytest.raises(e.ObservationStagingError, match="must occur after"):
        store.stage_observation(
            committed_campaign=committed,
            payload=b"already observed",
            media_type="application/octet-stream",
            observed_at=committed.committed_at,
            staged_at=committed.committed_at + timedelta(minutes=1),
        )
    assert not list(store.root.rglob("*.observation"))


@pytest.mark.asyncio
async def test_observation_roundtrip_exact_retry_and_mutation_violation(
    source_fixture,
    tmp_path,
) -> None:
    parts = build_f9s4_fixture(source_fixture)
    campaign = await _run(parts, "campaign:f9s4:first-commitment")
    archive = ContentAddressedResponseArchive(tmp_path / "prediction-archive")
    committed = e.commit_prediction_commitment_campaign(
        archive=archive,
        campaign=campaign,
        committed_at=campaign.generated_at + timedelta(minutes=1),
    )
    store = e.ObservationStagingStore(
        tmp_path / "observations",
        prediction_archive=archive,
    )
    receipt = store.stage_observation(
        committed_campaign=committed,
        payload=b'{"outcome":"primary_pattern"}',
        media_type="application/json",
        observed_at=committed.committed_at + timedelta(minutes=1),
        staged_at=committed.committed_at + timedelta(minutes=2),
    )
    assert store.read_observation(receipt) == b'{"outcome":"primary_pattern"}'

    retry = await _run(parts, "campaign:f9s4:exact-retry")
    assert retry.campaign_sha256 != campaign.campaign_sha256
    assert retry.commitment_sha256 == campaign.commitment_sha256
    retry_committed = e.commit_prediction_commitment_campaign(
        archive=archive,
        campaign=retry,
        committed_at=retry.generated_at + timedelta(minutes=1),
    )
    retry_receipt = store.stage_observation(
        committed_campaign=retry_committed,
        payload=b'{"outcome":"replicate"}',
        media_type="application/json",
        observed_at=retry_committed.committed_at + timedelta(minutes=1),
        staged_at=retry_committed.committed_at + timedelta(minutes=2),
    )
    assert store.read_observation(retry_receipt) == b'{"outcome":"replicate"}'

    changed_parts = build_f9s4_fixture(source_fixture)
    changed_batch = changed_parts["prediction_batch"]
    bins = tuple(item.bin_id for item in changed_parts["outcome_schema"].bins)
    changed_predictions = tuple(
        revalidate(
            e.HypothesisPrediction,
            item,
            probabilities=_mass(bins, item.expected_outcome_bin_id, 0.65),
        )
        for item in changed_batch.predictions
    )
    _install_batch(
        changed_parts,
        revalidate(e.PredictionBatch, changed_batch, predictions=changed_predictions),
    )
    changed = await _run(changed_parts, "campaign:f9s4:changed-after-observation")
    assert changed.disposition is e.PredictionCommitmentDisposition.READY
    assert changed.commitment_sha256 != campaign.commitment_sha256
    changed_committed = e.commit_prediction_commitment_campaign(
        archive=archive,
        campaign=changed,
        committed_at=changed.generated_at + timedelta(minutes=1),
    )
    with pytest.raises(e.PostObservationPredictionMutation) as exc_info:
        store.stage_observation(
            committed_campaign=changed_committed,
            payload=b'{"outcome":"rewritten"}',
            media_type="application/json",
            observed_at=changed_committed.committed_at + timedelta(minutes=1),
            staged_at=changed_committed.committed_at + timedelta(minutes=2),
        )
    violation = exc_info.value.violation
    assert violation.severity == "security_and_scientific_integrity"
    assert violation.sealed_commitment_sha256 == campaign.commitment_sha256
    assert violation.attempted_commitment_sha256 == changed.commitment_sha256
    assert list(store.root.rglob("violations/**/*.json"))
    assert b"rewritten" not in tuple(
        path.read_bytes() for path in store.root.rglob("*.observation")
    )


@pytest.mark.asyncio
async def test_missing_prediction_archive_blocks_observation_before_raw_write(
    source_fixture,
    tmp_path,
) -> None:
    parts = build_f9s4_fixture(source_fixture)
    campaign = await _run(parts, "campaign:f9s4:missing-archive")
    archive = ContentAddressedResponseArchive(tmp_path / "prediction-archive")
    committed = e.commit_prediction_commitment_campaign(
        archive=archive,
        campaign=campaign,
        committed_at=campaign.generated_at + timedelta(minutes=1),
    )
    ledger_path = archive.root / committed.ledger.relative_path
    ledger_path.unlink()
    store = e.ObservationStagingStore(
        tmp_path / "observations",
        prediction_archive=archive,
    )
    with pytest.raises(ResponseArchiveCorruption):
        store.stage_observation(
            committed_campaign=committed,
            payload=b"must-not-land",
            media_type="application/octet-stream",
            observed_at=committed.committed_at + timedelta(minutes=1),
            staged_at=committed.committed_at + timedelta(minutes=2),
        )
    assert not list(store.root.rglob("*.observation"))
