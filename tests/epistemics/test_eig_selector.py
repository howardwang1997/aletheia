from __future__ import annotations

import asyncio
import math
from datetime import timedelta

import pytest
from pydantic import ValidationError

import aletheia.epistemics as e
from aletheia.knowledge.response_archive import (
    ContentAddressedResponseArchive,
    ResponseArchiveCorruption,
)
from knowledge.f8s5_fixtures import build_f8s5_direction_fixture, build_f8s5_live_fixture

from .f9s2_fixtures import StepClock, build_f9s2_fixture, digest, revalidate
from .f9s3_fixtures import build_f9s3_fixture
from .f9s5_fixtures import (
    DEFAULT_CANDIDATE_SPECS,
    build_f9s5_fixture,
    rebuild_f9s5_request,
)


@pytest.fixture(scope="module")
def source_fixture(tmp_path_factory):
    live = asyncio.run(
        build_f8s5_live_fixture(
            tmp_path_factory.mktemp("f9s5-strong"),
            novelty_kind="strong",
        )
    )
    gate = build_f8s5_direction_fixture(live)["gate"]
    hypotheses = build_f9s2_fixture(gate)
    hypothesis_campaign = asyncio.run(
        e.run_competing_hypothesis_generation(
            campaign_id="campaign:f9s5:source-hypotheses",
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
            campaign_id="campaign:f9s5:source-causal-audit",
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


@pytest.fixture(scope="module")
def selection_fixture(source_fixture, tmp_path_factory):
    return build_f9s5_fixture(
        source_fixture,
        tmp_path_factory.mktemp("f9s5-selection"),
    )


def _run(parts, campaign_id: str = "campaign:f9s5:test"):
    return e.run_experiment_selection(
        campaign_id=campaign_id,
        policy=parts["policy"],
        assessor_manifest=parts["assessor_manifest"],
        request=parts["request"],
        prediction_archive=parts["prediction_archive"],
        clock=StepClock(parts["request"].issued_at + timedelta(minutes=1)),
    )


def _with_assessment(parts, candidate_id: str, **updates: object):
    changed = dict(parts)
    assessments = tuple(
        revalidate(e.CandidateExperimentAssessment, item, **updates)
        if item.candidate_id == candidate_id
        else item
        for item in parts["assessment_batch"].assessments
    )
    rebuild_f9s5_request(changed, assessments=assessments)
    return changed


def _score(campaign, candidate_id):
    return next(item for item in campaign.candidate_scores if item.candidate_id == candidate_id)


def _ranking(campaign, candidate_id):
    return next(item for item in campaign.decision.rankings if item.candidate_id == candidate_id)


def test_exact_archived_candidates_produce_constrained_selection(selection_fixture) -> None:
    campaign = _run(selection_fixture, "campaign:f9s5:ready")

    assert campaign.disposition is e.ExperimentSelectionDisposition.READY_SELECTED
    assert campaign.decision.selected_candidate_id == "candidate.high_info"
    assert campaign.blockers == ()
    assert campaign.failure is None
    assert len(campaign.archive_verifications) == 3
    assert len(campaign.candidate_scores) == 3
    assert all(item.feasible for item in campaign.candidate_scores)
    assert [item.rank for item in campaign.decision.rankings] == [1, 2, 3]
    selected = _ranking(campaign, "candidate.high_info")
    assert selected.disposition is e.CandidateSelectionDisposition.SELECTED
    assert selected.reasons == ("highest_constrained_utility",)
    assert all(
        item.reasons == ("lower_constrained_utility",)
        for item in campaign.decision.rankings
        if item.candidate_id != "candidate.high_info"
    )


def test_eig_matches_closed_form_symmetric_three_hypothesis_case(selection_fixture) -> None:
    campaign = _run(selection_fixture, "campaign:f9s5:eig-closed-form")
    audit = _score(campaign, "candidate.high_info").information_audit
    assert audit is not None
    expected_posterior_entropy = -(0.85 * math.log(0.85) + 2 * 0.075 * math.log(0.075))
    expected_eig = math.log(3.0) - expected_posterior_entropy

    assert audit.prior_entropy_nats == pytest.approx(math.log(3.0), abs=1e-12)
    assert audit.expected_posterior_entropy_nats == pytest.approx(
        expected_posterior_entropy, abs=1e-12
    )
    assert audit.expected_information_gain_nats == pytest.approx(expected_eig, abs=1e-12)
    assert audit.minimum_pairwise_total_variation == pytest.approx(0.775)
    assert audit.maximum_pairwise_total_variation == pytest.approx(0.775)


def test_eig_retains_every_outcome_posterior_and_normalization(selection_fixture) -> None:
    campaign = _run(selection_fixture, "campaign:f9s5:eig-ledger")
    for score in campaign.candidate_scores:
        audit = score.information_audit
        assert audit is not None
        assert sum(item.marginal_probability for item in audit.outcome_posteriors) == pytest.approx(
            1.0
        )
        for outcome in audit.outcome_posteriors:
            assert sum(item.probability for item in outcome.hypothesis_posteriors) == pytest.approx(
                1.0
            )
            assert len(outcome.hypothesis_posteriors) == 3


def test_selector_and_assessor_are_observation_blind_and_schema_frozen(selection_fixture) -> None:
    manifest = selection_fixture["assessor_manifest"]
    request = selection_fixture["request"]
    assert manifest.output_schema_sha256 == e.EXPERIMENT_ASSESSMENT_OUTPUT_SCHEMA_SHA256
    assert manifest.observation_access == "none"
    assert manifest.tool_names == ()
    assert request.observation_access == "none"
    assert all(
        item.committed_prediction.campaign.request.observation_access == "none"
        for item in request.candidates
    )
    with pytest.raises(ValidationError, match="ambient tool authority"):
        revalidate(e.ExperimentAssessmentManifest, manifest, tool_names=("observation.read",))


def test_high_eig_invalid_proxy_is_infeasible_and_cannot_win(selection_fixture) -> None:
    parts = _with_assessment(
        selection_fixture,
        "candidate.high_info",
        measurement_validity_status=e.MeasurementValidityStatus.INVALID,
        proxy_risk_status=e.ProxyRiskStatus.INVALID_SURROGATE,
    )

    campaign = _run(parts, "campaign:f9s5:invalid-proxy")

    assert campaign.decision.selected_candidate_id != "candidate.high_info"
    high = _score(campaign, "candidate.high_info")
    assert high.information_audit is not None
    assert high.information_audit.expected_information_gain_nats > max(
        item.information_audit.expected_information_gain_nats
        for item in campaign.candidate_scores
        if item.candidate_id != "candidate.high_info"
    )
    assert high.feasible is False
    assert high.constrained_utility is None
    assert "measurement:not_validated:invalid" in high.blockers
    assert "measurement:proxy_risk:invalid_surrogate" in high.blockers
    assert (
        _ranking(campaign, "candidate.high_info").disposition
        is e.CandidateSelectionDisposition.INFEASIBLE
    )


@pytest.mark.parametrize(
    ("updates", "blocker"),
    [
        ({"estimated_cost_microunits": 1_000_001}, "cost:budget_exceeded"),
        ({"estimated_duration_seconds": 86_401}, "duration:limit_exceeded"),
        ({"risk_level": e.ExperimentRiskLevel.HIGH}, "risk:level_exceeded"),
        ({"risk_level": e.ExperimentRiskLevel.PROHIBITED}, "risk:prohibited"),
        ({"cost_currency": "USD"}, "cost:currency_mismatch"),
        (
            {"measurement_validity_confidence": 0.79},
            "measurement:confidence_below_floor",
        ),
        (
            {
                "measurement_validity_status": e.MeasurementValidityStatus.BOUNDED,
                "proxy_risk_status": e.ProxyRiskStatus.BOUNDED_SURROGATE,
            },
            "measurement:proxy_risk:bounded_surrogate",
        ),
    ],
)
def test_hard_constraints_block_high_information_candidate(
    selection_fixture,
    updates,
    blocker,
) -> None:
    parts = _with_assessment(selection_fixture, "candidate.high_info", **updates)
    campaign = _run(parts, f"campaign:f9s5:{blocker.replace(':', '-')}")

    high = _score(campaign, "candidate.high_info")
    assert high.feasible is False
    assert blocker in high.blockers
    assert campaign.decision.selected_candidate_id != "candidate.high_info"


def test_missing_required_capability_is_a_hard_blocker(selection_fixture) -> None:
    assessment = next(
        item
        for item in selection_fixture["assessment_batch"].assessments
        if item.candidate_id == "candidate.high_info"
    )
    missing = assessment.required_capability_sha256s[-1]
    parts = _with_assessment(
        selection_fixture,
        "candidate.high_info",
        available_capability_sha256s=assessment.required_capability_sha256s[:-1],
    )
    campaign = _run(parts, "campaign:f9s5:missing-capability")

    assert f"capability:missing:{missing}" in _score(campaign, "candidate.high_info").blockers


def test_missing_fresh_confirmation_is_a_hard_blocker(selection_fixture) -> None:
    parts = _with_assessment(
        selection_fixture,
        "candidate.high_info",
        fresh_confirmation_batches=(),
    )
    campaign = _run(parts, "campaign:f9s5:no-fresh-confirmation")
    score = _score(campaign, "candidate.high_info")

    assert score.valid_fresh_confirmation_batches == 0
    assert "fresh_confirmation:insufficient" in score.blockers
    assert score.feasible is False


def test_duplicate_fresh_confirmation_partition_cannot_be_counted_twice(
    selection_fixture,
) -> None:
    assessment = next(
        item
        for item in selection_fixture["assessment_batch"].assessments
        if item.candidate_id == "candidate.high_info"
    )
    first, second = assessment.fresh_confirmation_batches
    duplicate_partition = revalidate(
        e.FreshConfirmationBatch,
        second,
        partition_sha256=first.partition_sha256,
    )

    with pytest.raises(ValidationError, match="partitions must be unique"):
        revalidate(
            e.CandidateExperimentAssessment,
            assessment,
            fresh_confirmation_batches=(first, duplicate_partition),
        )


@pytest.mark.parametrize("freshness_failure", ["calibration", "expired"])
def test_stale_or_leaking_confirmation_reservation_is_rejected(
    selection_fixture,
    freshness_failure,
) -> None:
    assessment = next(
        item
        for item in selection_fixture["assessment_batch"].assessments
        if item.candidate_id == "candidate.high_info"
    )
    campaign = next(
        item.committed_prediction.campaign
        for item in selection_fixture["candidates"]
        if item.candidate_id == "candidate.high_info"
    )
    first = assessment.fresh_confirmation_batches[0]
    if freshness_failure == "calibration":
        report = campaign.request.calibration_report
        assert report is not None
        changed_first = revalidate(
            e.FreshConfirmationBatch,
            first,
            partition_sha256=report.validation_split_sha256,
        )
        expected = f"fresh_confirmation:reuses_calibration:{first.batch_sha256}"
    else:
        changed_first = revalidate(
            e.FreshConfirmationBatch,
            first,
            available_until=selection_fixture["request"].issued_at,
        )
        expected = f"fresh_confirmation:expired:{first.batch_sha256}"
    parts = _with_assessment(
        selection_fixture,
        "candidate.high_info",
        fresh_confirmation_batches=(
            changed_first,
            *assessment.fresh_confirmation_batches[1:],
        ),
    )
    campaign = _run(parts, f"campaign:f9s5:fresh-{freshness_failure}")

    assert expected in _score(campaign, "candidate.high_info").blockers


def test_replication_debt_can_drive_choice_under_frozen_weights(selection_fixture) -> None:
    parts = dict(selection_fixture)
    weights = e.SelectionUtilityWeights(
        expected_information_gain=0.1,
        minimum_pairwise_discrimination=0.1,
        fresh_confirmation=0.0,
        replication_debt_reduction=0.8,
        cost_penalty=0.0,
        duration_penalty=0.0,
        risk_penalty=0.0,
    )
    policy = revalidate(
        e.ExperimentSelectionPolicy,
        parts["policy"],
        utility_weights=weights,
    )
    rebuild_f9s5_request(parts, policy=policy)

    campaign = _run(parts, "campaign:f9s5:replication-priority")

    assert campaign.decision.selected_candidate_id == "candidate.replication"
    replication = _score(campaign, "candidate.replication")
    assert replication.replication_debt_after == 0
    assert replication.replication_debt_reduction_score == 1.0


def test_cost_can_drive_choice_without_changing_information(selection_fixture) -> None:
    parts = dict(selection_fixture)
    weights = e.SelectionUtilityWeights(
        expected_information_gain=0.15,
        minimum_pairwise_discrimination=0.15,
        fresh_confirmation=0.0,
        replication_debt_reduction=0.0,
        cost_penalty=0.7,
        duration_penalty=0.0,
        risk_penalty=0.0,
    )
    policy = revalidate(
        e.ExperimentSelectionPolicy,
        parts["policy"],
        utility_weights=weights,
    )
    rebuild_f9s5_request(parts, policy=policy)

    campaign = _run(parts, "campaign:f9s5:cost-priority")

    assert campaign.decision.selected_candidate_id == "candidate.efficient"


def test_fixed_utility_does_not_rescale_when_a_candidate_is_removed(
    selection_fixture,
) -> None:
    full = _run(selection_fixture, "campaign:f9s5:full-candidate-set")
    retained_ids = {"candidate.efficient", "candidate.high_info"}
    parts = dict(selection_fixture)
    candidates = tuple(
        item for item in selection_fixture["candidates"] if item.candidate_id in retained_ids
    )
    assessments = tuple(
        item
        for item in selection_fixture["assessment_batch"].assessments
        if item.candidate_id in retained_ids
    )
    rebuild_f9s5_request(parts, candidates=candidates, assessments=assessments)

    reduced = _run(parts, "campaign:f9s5:reduced-candidate-set")

    for candidate_id in retained_ids:
        assert _score(reduced, candidate_id) == _score(full, candidate_id)


def test_tie_breaking_is_deterministic_and_candidate_id_canonical(source_fixture, tmp_path) -> None:
    specs = (
        {
            "candidate_id": "candidate.alpha",
            "prediction_probability": 0.7,
            "cost": 300_000,
            "duration": 3_600,
            "risk": e.ExperimentRiskLevel.LOW,
            "fresh_batches": 2,
            "replication_debt_before": 0,
            "replication_reduction": 0,
        },
        {
            "candidate_id": "candidate.beta",
            "prediction_probability": 0.7,
            "cost": 300_000,
            "duration": 3_600,
            "risk": e.ExperimentRiskLevel.LOW,
            "fresh_batches": 2,
            "replication_debt_before": 0,
            "replication_reduction": 0,
        },
    )
    parts = build_f9s5_fixture(source_fixture, tmp_path, candidate_specs=specs)

    first = _run(parts, "campaign:f9s5:tie-first")
    second = _run(parts, "campaign:f9s5:tie-second")

    assert first.decision.selected_candidate_id == "candidate.alpha"
    assert first.candidate_scores == second.candidate_scores
    assert first.decision == second.decision


@pytest.mark.parametrize(
    ("special", "expected_blocker"),
    [
        ({"ordinal": True}, "prediction:not_eig_eligible"),
        (
            {"calibration_trials": 10},
            "prediction:not_ready:blocked_calibration",
        ),
    ],
)
def test_non_eig_or_blocked_prediction_campaign_is_retained_but_infeasible(
    source_fixture,
    tmp_path,
    special,
    expected_blocker,
) -> None:
    specs = tuple(
        {
            **spec,
            **(special if spec["candidate_id"] == "candidate.high_info" else {}),
        }
        for spec in DEFAULT_CANDIDATE_SPECS
    )
    parts = build_f9s5_fixture(source_fixture, tmp_path, candidate_specs=specs)
    campaign = _run(parts, f"campaign:f9s5:{expected_blocker.replace(':', '-')}")

    high = _score(campaign, "candidate.high_info")
    assert high.feasible is False
    assert expected_blocker in high.blockers
    assert high.information_audit is None


def test_duplicate_commitment_and_reordered_candidates_are_rejected(selection_fixture) -> None:
    request = selection_fixture["request"]
    first = request.candidates[0]
    clone = e.ExperimentCandidate(
        candidate_id="candidate.clone",
        committed_prediction=first.committed_prediction,
    )
    candidates = tuple(sorted((clone, *request.candidates), key=lambda item: item.candidate_id))
    with pytest.raises(ValidationError, match="duplicate substantive commitments"):
        revalidate(e.ExperimentSelectionRequest, request, candidates=candidates)
    with pytest.raises(ValidationError, match="canonical IDs"):
        revalidate(
            e.ExperimentSelectionRequest,
            request,
            candidates=tuple(reversed(request.candidates)),
        )


def test_incomplete_assessment_batch_and_measurement_rebinding_are_rejected(
    selection_fixture,
) -> None:
    parts = dict(selection_fixture)
    with pytest.raises(ValueError, match="cover every candidate"):
        rebuild_f9s5_request(
            parts,
            assessments=selection_fixture["assessment_batch"].assessments[:-1],
        )
    first = selection_fixture["assessment_batch"].assessments[0]
    rebound = revalidate(
        e.CandidateExperimentAssessment,
        first,
        measurement_process_sha256=digest("f9s5:forged-measurement-process"),
    )
    assessments = (rebound, *selection_fixture["assessment_batch"].assessments[1:])
    with pytest.raises(ValueError, match="changed exact measurement_process_sha256"):
        rebuild_f9s5_request(dict(selection_fixture), assessments=assessments)


def test_assessor_must_be_independent_and_frozen_before_assessments(selection_fixture) -> None:
    candidate = selection_fixture["candidates"][0].committed_prediction.campaign
    shared = candidate.author_manifest.author_principal_sha256
    changed_manifest = revalidate(
        e.ExperimentAssessmentManifest,
        selection_fixture["assessor_manifest"],
        assessor_principal_sha256=shared,
    )
    assessments = tuple(
        revalidate(
            e.CandidateExperimentAssessment,
            item,
            assessor_manifest_sha256=changed_manifest.manifest_sha256,
        )
        for item in selection_fixture["assessment_batch"].assessments
    )
    batch = e.ExperimentAssessmentBatch(
        assessor_manifest_sha256=changed_manifest.manifest_sha256,
        assessments=assessments,
        completed_at=selection_fixture["assessment_batch"].completed_at,
    )
    with pytest.raises(ValueError, match="must be independent"):
        e.build_experiment_selection_request(
            selection_id="f9s5-non-independent",
            candidates=selection_fixture["candidates"],
            assessment_batch=batch,
            assessor_manifest=changed_manifest,
            policy=selection_fixture["policy"],
            prediction_archive_custody_sha256=selection_fixture["archive_custody_sha256"],
            issued_at=selection_fixture["request"].issued_at,
        )


def test_future_assessment_and_candidate_commitment_are_rejected(selection_fixture) -> None:
    request = selection_fixture["request"]
    with pytest.raises(ValueError, match="predates a frozen dependency"):
        e.build_experiment_selection_request(
            selection_id="f9s5-future-assessment",
            candidates=selection_fixture["candidates"],
            assessment_batch=selection_fixture["assessment_batch"],
            assessor_manifest=selection_fixture["assessor_manifest"],
            policy=selection_fixture["policy"],
            prediction_archive_custody_sha256=selection_fixture["archive_custody_sha256"],
            issued_at=selection_fixture["assessment_batch"].completed_at
            - timedelta(microseconds=1),
        )
    future_committed = revalidate(
        e.CommittedPredictionCommitmentCampaign,
        request.candidates[0].committed_prediction,
        committed_at=request.issued_at + timedelta(minutes=1),
        ledger=revalidate(
            type(request.candidates[0].committed_prediction.ledger),
            request.candidates[0].committed_prediction.ledger,
            archived_at=request.issued_at + timedelta(minutes=1),
        ),
    )
    changed = e.ExperimentCandidate(
        candidate_id=request.candidates[0].candidate_id,
        committed_prediction=future_committed,
    )
    candidates = (changed, *request.candidates[1:])
    with pytest.raises(ValueError, match="predates a candidate prediction commitment"):
        e.build_experiment_selection_request(
            selection_id="f9s5-future-commitment",
            candidates=candidates,
            assessment_batch=selection_fixture["assessment_batch"],
            assessor_manifest=selection_fixture["assessor_manifest"],
            policy=selection_fixture["policy"],
            prediction_archive_custody_sha256=selection_fixture["archive_custody_sha256"],
            issued_at=request.issued_at,
        )


def test_no_feasible_candidate_is_explicit_not_a_fallback(selection_fixture) -> None:
    parts = dict(selection_fixture)
    assessments = tuple(
        revalidate(
            e.CandidateExperimentAssessment,
            item,
            measurement_validity_status=e.MeasurementValidityStatus.INVALID,
            proxy_risk_status=e.ProxyRiskStatus.INVALID_SURROGATE,
        )
        for item in selection_fixture["assessment_batch"].assessments
    )
    rebuild_f9s5_request(parts, assessments=assessments)

    campaign = _run(parts, "campaign:f9s5:no-feasible")

    assert campaign.disposition is e.ExperimentSelectionDisposition.NO_FEASIBLE_EXPERIMENT
    assert campaign.decision.selected_candidate_id is None
    assert all(not item.feasible for item in campaign.candidate_scores)
    assert all(
        item.disposition is e.CandidateSelectionDisposition.INFEASIBLE
        for item in campaign.decision.rankings
    )


def test_missing_prediction_archive_blocks_whole_selection_with_hash_only_failure(
    source_fixture,
    tmp_path,
) -> None:
    parts = build_f9s5_fixture(source_fixture, tmp_path)
    first = parts["candidates"][0].committed_prediction
    (parts["prediction_archive"].root / first.ledger.relative_path).unlink()

    campaign = _run(parts, "campaign:f9s5:missing-prediction-archive")

    assert campaign.disposition is e.ExperimentSelectionDisposition.BLOCKED_EXECUTION
    assert campaign.failure.kind is e.ExperimentSelectionFailureKind.PREDICTION_ARCHIVE_INVALID
    assert campaign.failure.failed_candidate_id == parts["candidates"][0].candidate_id
    assert campaign.archive_verifications == ()
    assert campaign.candidate_scores == ()
    assert campaign.decision is None


def test_selection_decision_is_rederived_and_campaign_archive_detects_tampering(
    selection_fixture,
    tmp_path,
) -> None:
    campaign = _run(selection_fixture, "campaign:f9s5:archive")
    with pytest.raises(ValidationError, match="differs from its decision"):
        revalidate(
            e.ExperimentSelectionCampaign,
            campaign,
            disposition=e.ExperimentSelectionDisposition.NO_FEASIBLE_EXPERIMENT,
        )
    first_score = campaign.candidate_scores[0]
    forged_score = revalidate(
        e.ExperimentCandidateScore,
        first_score,
        constrained_utility=first_score.constrained_utility + 0.1,
    )
    with pytest.raises(ValidationError, match="not mechanically derived"):
        revalidate(
            e.ExperimentSelectionCampaign,
            campaign,
            candidate_scores=(forged_score, *campaign.candidate_scores[1:]),
        )

    archive = ContentAddressedResponseArchive(tmp_path / "selection-archive")
    committed_at = campaign.generated_at + timedelta(minutes=1)
    committed = e.commit_experiment_selection_campaign(
        archive=archive,
        campaign=campaign,
        committed_at=committed_at,
    )
    assert committed.committed_at == committed_at
    assert committed.ledger.archived_at == committed_at
    assert len(committed.receipt_sha256) == 64
    assert (
        e.load_experiment_selection_campaign(archive=archive, ledger=committed.ledger) == campaign
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        e.commit_experiment_selection_campaign(
            archive=archive,
            campaign=campaign,
            committed_at=committed_at.replace(tzinfo=None),
        )
    with pytest.raises(ValueError, match="cannot predate"):
        e.commit_experiment_selection_campaign(
            archive=archive,
            campaign=campaign,
            committed_at=campaign.generated_at - timedelta(microseconds=1),
        )
    ledger_path = archive.root / committed.ledger.relative_path
    ledger_path.chmod(0o600)
    payload = ledger_path.read_bytes()
    ledger_path.write_bytes(payload[:-1] + (b"0" if payload[-1:] != b"0" else b"1"))
    with pytest.raises(ResponseArchiveCorruption, match="archived object"):
        e.load_experiment_selection_campaign(archive=archive, ledger=committed.ledger)
