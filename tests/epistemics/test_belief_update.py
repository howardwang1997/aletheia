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
from .f9s6_fixtures import FixtureObservationValidator, build_f9s6_fixture


@pytest.fixture(scope="module")
def source_fixture(tmp_path_factory):
    live = asyncio.run(
        build_f8s5_live_fixture(
            tmp_path_factory.mktemp("f9s6-strong"),
            novelty_kind="strong",
        )
    )
    gate = build_f8s5_direction_fixture(live)["gate"]
    hypotheses = build_f9s2_fixture(gate)
    hypothesis_campaign = asyncio.run(
        e.run_competing_hypothesis_generation(
            campaign_id="campaign:f9s6:source-hypotheses",
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
            campaign_id="campaign:f9s6:source-causal-audit",
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
def update_fixture(source_fixture, tmp_path_factory):
    return build_f9s6_fixture(
        source_fixture,
        tmp_path_factory.mktemp("f9s6-update"),
    )


@pytest.fixture(scope="module")
def negative_update_fixture(source_fixture, tmp_path_factory):
    return build_f9s6_fixture(
        source_fixture,
        tmp_path_factory.mktemp("f9s6-negative-update"),
        outcome_role=e.HypothesisRole.NULL,
    )


@pytest.fixture(scope="module")
def uninformative_update_fixture(source_fixture, tmp_path_factory):
    specs = (
        {
            "candidate_id": "candidate.efficient",
            "prediction_probability": 0.65,
            "cost": 200_000,
            "duration": 3_600,
            "risk": e.ExperimentRiskLevel.HIGH,
            "fresh_batches": 2,
            "replication_debt_before": 2,
            "replication_reduction": 0,
        },
        {
            "candidate_id": "candidate.high_info",
            "prediction_probability": 0.85,
            "shared_likelihood_on_primary_bin": 0.4,
            "cost": 200_000,
            "duration": 3_600,
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
            "risk": e.ExperimentRiskLevel.HIGH,
            "fresh_batches": 2,
            "replication_debt_before": 2,
            "replication_reduction": 2,
        },
    )
    return build_f9s6_fixture(
        source_fixture,
        tmp_path_factory.mktemp("f9s6-uninformative-update"),
        candidate_specs=specs,
    )


@pytest.fixture(scope="module")
def unaligned_sensitivity_fixture(source_fixture, tmp_path_factory):
    specs = (
        {
            "candidate_id": "candidate.efficient",
            "prediction_probability": 0.65,
            "cost": 200_000,
            "duration": 3_600,
            "risk": e.ExperimentRiskLevel.HIGH,
            "fresh_batches": 2,
            "replication_debt_before": 2,
            "replication_reduction": 0,
        },
        {
            "candidate_id": "candidate.high_info",
            "prediction_probability": 0.85,
            "unaligned_sensitivity": True,
            "cost": 200_000,
            "duration": 3_600,
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
            "risk": e.ExperimentRiskLevel.HIGH,
            "fresh_batches": 2,
            "replication_debt_before": 2,
            "replication_reduction": 2,
        },
    )
    return build_f9s6_fixture(
        source_fixture,
        tmp_path_factory.mktemp("f9s6-unaligned-sensitivity"),
        candidate_specs=specs,
    )


def _run_validation(
    parts,
    *,
    validator=None,
    policy=None,
    campaign_id="campaign:f9s6:validation-test",
):
    request = parts["validation_request"]
    return asyncio.run(
        e.run_observation_validation(
            campaign_id=campaign_id,
            policy=policy or parts["validation_policy"],
            request=request,
            validator=validator or parts["validator"],
            selection_archive=parts["selection_archive"],
            prediction_archive=parts["prediction_archive"],
            observation_store=parts["observation_store"],
            clock=StepClock(request.issued_at + timedelta(minutes=2)),
        )
    )


def _run_update(parts, *, policy=None, campaign_id="campaign:f9s6:update-test"):
    request = parts["update_request"]
    return e.run_world_belief_update(
        campaign_id=campaign_id,
        policy=policy or parts["update_policy"],
        request=request,
        validation_archive=parts["validation_archive"],
        clock=StepClock(request.issued_at + timedelta(minutes=1)),
    )


def _validator_with(parts, **overrides):
    return FixtureObservationValidator(
        parts["validator_manifest"],
        completed_at=parts["validation_request"].issued_at + timedelta(minutes=1),
        overrides=overrides,
    )


def _with_update_policy(parts, **updates):
    changed = dict(parts)
    policy = revalidate(e.WorldBeliefUpdatePolicy, parts["update_policy"], **updates)
    request = e.build_world_belief_update_request(
        update_id=parts["update_request"].update_id,
        committed_validation=parts["committed_validation"],
        policy=policy,
        validation_archive_custody_sha256=parts["validation_archive_custody_sha256"],
        issued_at=parts["update_request"].issued_at,
    )
    changed["update_policy"] = policy
    changed["update_request"] = request
    return changed


def test_validated_observation_updates_exact_prior_and_preserves_lineage(
    update_fixture,
) -> None:
    campaign = _run_update(update_fixture, campaign_id="campaign:f9s6:robust")

    assert campaign.disposition is e.WorldBeliefUpdateDisposition.UPDATED_ROBUST
    assert campaign.blockers == ()
    assert campaign.failure is None
    assert campaign.audit is not None
    assert campaign.updated_world_model_snapshot is not None
    source = update_fixture[
        "selected_candidate"
    ].committed_prediction.campaign.source_causal_campaign.source_campaign.world_model_snapshot
    assert source is not None
    updated = campaign.updated_world_model_snapshot
    assert updated.belief_state.version == source.belief_state.version + 1
    assert (
        updated.belief_state.parent_belief_state_sha256 == source.belief_state.belief_state_sha256
    )
    assert updated.belief_state.belief_lineage_id == source.belief_state.belief_lineage_id
    assert (
        updated.belief_state.source_observation_receipt_sha256
        == update_fixture["committed_validation"].receipt_sha256
    )
    assert updated.hypotheses == source.hypotheses
    assert updated.assumptions == source.assumptions
    assert updated.predictions == source.predictions


def test_closed_form_posterior_entropy_and_surprise_are_rederived(update_fixture) -> None:
    campaign = _run_update(update_fixture, campaign_id="campaign:f9s6:closed-form")
    audit = campaign.audit
    assert audit is not None
    source = campaign.updated_world_model_snapshot
    assert source is not None
    primary_id = next(
        item.hypothesis_id for item in source.hypotheses if item.role is e.HypothesisRole.PRIMARY
    )
    posterior = {item.hypothesis_id: item for item in audit.hypotheses}

    assert audit.prior_predictive_probability == pytest.approx(1.0 / 3.0)
    assert audit.prior_predictive_surprisal_nats == pytest.approx(math.log(3.0))
    assert posterior[primary_id].posterior_probability == pytest.approx(0.85)
    assert sorted(
        item.posterior_probability for item in audit.hypotheses if item.hypothesis_id != primary_id
    ) == pytest.approx([0.075, 0.075])
    expected_entropy = -(0.85 * math.log(0.85) + 2 * 0.075 * math.log(0.075))
    assert audit.prior_entropy_nats == pytest.approx(math.log(3.0), abs=1e-12)
    assert audit.posterior_entropy_nats == pytest.approx(expected_entropy, abs=1e-12)
    assert audit.realized_entropy_reduction_nats == pytest.approx(
        math.log(3.0) - expected_entropy,
        abs=1e-12,
    )


def test_likelihood_sensitivity_retains_complete_posteriors(update_fixture) -> None:
    campaign = _run_update(update_fixture, campaign_id="campaign:f9s6:sensitivity")
    audit = campaign.audit
    assert audit is not None

    assert len(audit.sensitivity_posteriors) == 2
    assert audit.maximum_sensitivity_total_variation == pytest.approx(0.03)
    assert audit.fragile is False
    for scenario in audit.sensitivity_posteriors:
        assert len(scenario.hypotheses) == 3
        assert sum(item.posterior_probability for item in scenario.hypotheses) == pytest.approx(1.0)
        assert scenario.winner_changed is False


def test_validation_physically_verifies_selection_prediction_and_raw_bytes(
    update_fixture,
) -> None:
    campaign = update_fixture["validation_campaign"]

    assert campaign.disposition is e.ObservationValidationDisposition.VALIDATED_CONFIRMATION
    assert campaign.selection_verification is not None
    assert campaign.prediction_verification is not None
    assert campaign.observation_verification is not None
    assert campaign.probe.valid_for_belief_update is True
    assert campaign.probe.blockers == ()
    assert (
        campaign.observation_verification.observation_sha256
        == update_fixture["observation_receipt"].observation_sha256
    )


def test_raw_observation_access_stops_at_independent_validator(update_fixture) -> None:
    manifest = update_fixture["validator_manifest"]
    validation_request = update_fixture["validation_request"]
    update_request = update_fixture["update_request"]

    assert manifest.observation_access == "exact_staged_payload_only"
    assert manifest.tool_names == ()
    assert validation_request.observation_access == "exact_staged_payload_only"
    assert update_request.observation_access == "validated_artifact_only"
    with pytest.raises(ValidationError, match="ambient tool authority"):
        revalidate(
            e.ObservationValidatorManifest,
            manifest,
            tool_names=("filesystem.read",),
        )


@pytest.mark.parametrize(
    ("overrides", "blocker"),
    [
        (
            {"data_role": e.ObservationDataRole.EXPLORATION},
            "data_role:not_confirmation:exploration",
        ),
        ({"experiment_identity_verified": False}, "identity:experiment_unverified"),
        ({"custody_chain_verified": False}, "custody:unverified"),
        ({"measurement_valid": False}, "measurement:invalid"),
        ({"blinding_intact": False}, "blinding:compromised"),
        (
            {"protocol_adherence": e.ProtocolAdherenceStatus.UNKNOWN},
            "protocol:unknown_adherence",
        ),
        (
            {"audit_status": e.ObservationAuditStatus.UNRESOLVED},
            "audit:not_resolved_accept:unresolved",
        ),
        (
            {"audit_status": e.ObservationAuditStatus.RESOLVED_REJECT},
            "audit:not_resolved_accept:resolved_reject",
        ),
    ],
)
def test_scientifically_invalid_observation_is_rejected_without_update_authority(
    update_fixture,
    overrides,
    blocker,
) -> None:
    campaign = _run_validation(
        update_fixture,
        validator=_validator_with(update_fixture, **overrides),
        campaign_id=f"campaign:f9s6:{blocker.replace(':', '-')}",
    )

    assert campaign.disposition is e.ObservationValidationDisposition.REJECTED_SCIENTIFIC
    assert blocker in campaign.blockers
    assert campaign.probe.valid_for_belief_update is False
    with pytest.raises(ValueError, match="validated confirmation"):
        e.build_world_belief_update_request(
            update_id="f9s6-invalid-observation-update",
            committed_validation=e.commit_observation_validation_campaign(
                archive=update_fixture["validation_archive"],
                campaign=campaign,
                committed_at=campaign.generated_at + timedelta(minutes=1),
            ),
            policy=update_fixture["update_policy"],
            validation_archive_custody_sha256=update_fixture["validation_archive_custody_sha256"],
            issued_at=campaign.generated_at + timedelta(minutes=2),
        )


def test_within_preregistered_protocol_tolerance_remains_valid(update_fixture) -> None:
    protocol = update_fixture[
        "selected_candidate"
    ].committed_prediction.campaign.request.experiment_protocol
    deviation = e.ProtocolDeviation(
        deviation_id="deviation.allowed",
        governing_rule_sha256=protocol.exclusion_rule_sha256,
        evidence_sha256=digest("f9s6:allowed-deviation-evidence"),
        within_preregistered_tolerance=True,
        material=False,
    )
    validator = _validator_with(
        update_fixture,
        protocol_adherence=e.ProtocolAdherenceStatus.WITHIN_PREREGISTERED_TOLERANCE,
        protocol_deviations=(deviation,),
    )

    campaign = _run_validation(
        update_fixture,
        validator=validator,
        campaign_id="campaign:f9s6:within-tolerance",
    )

    assert campaign.disposition is e.ObservationValidationDisposition.VALIDATED_CONFIRMATION
    assert campaign.blockers == ()


def test_material_protocol_deviation_is_a_hard_rejection(update_fixture) -> None:
    protocol = update_fixture[
        "selected_candidate"
    ].committed_prediction.campaign.request.experiment_protocol
    deviation = e.ProtocolDeviation(
        deviation_id="deviation.material",
        governing_rule_sha256=protocol.stopping_rule_sha256,
        evidence_sha256=digest("f9s6:material-deviation-evidence"),
        within_preregistered_tolerance=False,
        material=True,
    )
    campaign = _run_validation(
        update_fixture,
        validator=_validator_with(
            update_fixture,
            protocol_adherence=e.ProtocolAdherenceStatus.MATERIAL_DEVIATION,
            protocol_deviations=(deviation,),
        ),
        campaign_id="campaign:f9s6:material-deviation",
    )

    assert campaign.disposition is e.ObservationValidationDisposition.REJECTED_SCIENTIFIC
    assert "protocol:material_deviation" in campaign.blockers


def test_sample_starvation_requires_a_preregistered_small_sample_rule(
    update_fixture,
) -> None:
    rejected = _run_validation(
        update_fixture,
        validator=_validator_with(update_fixture, sample_count=5),
        campaign_id="campaign:f9s6:sample-starved",
    )
    assert "sample:below_minimum_without_preregistered_rule" in rejected.blockers

    rule = digest("f9s6:preregistered-small-sample-update-rule")
    policy = revalidate(
        e.ObservationValidationPolicy,
        update_fixture["validation_policy"],
        small_sample_update_rule_sha256=rule,
    )
    request = e.build_observation_validation_request(
        validation_id="f9s6-small-sample-validation",
        committed_selection=update_fixture["committed_selection"],
        observation_receipt=update_fixture["observation_receipt"],
        validator_manifest=update_fixture["validator_manifest"],
        policy=policy,
        selection_archive_custody_sha256=update_fixture["selection_archive_custody_sha256"],
        prediction_archive_custody_sha256=update_fixture["prediction_archive_custody_sha256"],
        observation_store_custody_sha256=update_fixture["observation_store_custody_sha256"],
        issued_at=update_fixture["validation_request"].issued_at,
    )
    parts = dict(update_fixture)
    parts["validation_request"] = request
    accepted = _run_validation(
        parts,
        policy=policy,
        validator=FixtureObservationValidator(
            update_fixture["validator_manifest"],
            completed_at=request.issued_at + timedelta(minutes=1),
            overrides={
                "sample_count": 5,
                "small_sample_update_rule_sha256": rule,
            },
        ),
        campaign_id="campaign:f9s6:small-sample-preregistered",
    )
    assert accepted.disposition is e.ObservationValidationDisposition.VALIDATED_CONFIRMATION
    assert accepted.blockers == ()


def test_confirmation_reservation_and_outcome_schema_rebinding_fail_closed(
    update_fixture,
) -> None:
    rebound = _run_validation(
        update_fixture,
        validator=_validator_with(
            update_fixture,
            confirmation_partition_sha256=digest("f9s6:unreserved-partition"),
        ),
        campaign_id="campaign:f9s6:unreserved-partition",
    )
    assert rebound.disposition is e.ObservationValidationDisposition.BLOCKED_EXECUTION
    assert rebound.failure.kind is e.ObservationValidationFailureKind.INVALID_OUTPUT
    assert rebound.validation_batch is None

    outside = _run_validation(
        update_fixture,
        validator=_validator_with(update_fixture, outcome_bin_id="outside.schema"),
        campaign_id="campaign:f9s6:outside-schema",
    )
    assert outside.disposition is e.ObservationValidationDisposition.BLOCKED_EXECUTION
    assert outside.failure.kind is e.ObservationValidationFailureKind.INVALID_OUTPUT


def test_update_revisions_are_append_only_directives_and_contradictions_are_open(
    update_fixture,
) -> None:
    campaign = _run_update(update_fixture, campaign_id="campaign:f9s6:revision-ledger")

    assert len(campaign.hypothesis_revisions) == 3
    assert (
        sum(
            item.action is e.HypothesisRevisionAction.RETAIN
            for item in campaign.hypothesis_revisions
        )
        == 1
    )
    assert (
        sum(
            item.action is e.HypothesisRevisionAction.NARROW
            for item in campaign.hypothesis_revisions
        )
        == 2
    )
    assert all(item.mutation_forbidden for item in campaign.hypothesis_revisions)
    assert all(
        item.new_version_required == (item.action is not e.HypothesisRevisionAction.RETAIN)
        for item in campaign.hypothesis_revisions
    )
    assert campaign.world_revision.action is e.WorldRevisionAction.CONTINUE_CURRENT_SET
    assert len(campaign.contradiction_queue) == 2
    assert all(item.status == "open" for item in campaign.contradiction_queue)
    assert all(
        item.kind is e.ContradictionKind.HYPOTHESIS_PREDICTION_MISS
        for item in campaign.contradiction_queue
    )


def test_likelihood_sensitivity_above_policy_marks_update_fragile(
    update_fixture,
) -> None:
    parts = _with_update_policy(
        update_fixture,
        maximum_posterior_total_variation=0.0,
    )
    campaign = _run_update(parts, campaign_id="campaign:f9s6:fragile")

    assert campaign.disposition is e.WorldBeliefUpdateDisposition.UPDATED_FRAGILE
    assert campaign.audit.fragile is True
    assert campaign.updated_world_model_snapshot is not None
    assert any(
        item.kind is e.ContradictionKind.LIKELIHOOD_SENSITIVITY
        for item in campaign.contradiction_queue
    )


def test_retirement_requires_low_posterior_under_every_sensitivity_case(
    update_fixture,
) -> None:
    parts = _with_update_policy(
        update_fixture,
        retirement_posterior_ceiling=0.1,
    )
    campaign = _run_update(parts, campaign_id="campaign:f9s6:robust-retirement")

    retired = [
        item
        for item in campaign.hypothesis_revisions
        if item.action is e.HypothesisRevisionAction.RETIRE
    ]
    assert len(retired) == 2
    assert all(item.new_version_required for item in retired)
    assert all(item.reasons == ("robust_posterior_below_retirement_ceiling",) for item in retired)


def test_negative_primary_result_is_retained_without_history_mutation(
    negative_update_fixture,
) -> None:
    campaign = _run_update(
        negative_update_fixture,
        campaign_id="campaign:f9s6:negative-primary",
    )
    audit = campaign.audit
    assert audit is not None
    assert audit.primary_negative_result is True
    snapshot = campaign.updated_world_model_snapshot
    assert snapshot is not None
    primary_id = next(
        item.hypothesis_id for item in snapshot.hypotheses if item.role is e.HypothesisRole.PRIMARY
    )
    primary_revision = next(
        item for item in campaign.hypothesis_revisions if item.hypothesis_id == primary_id
    )
    assert primary_revision.action is e.HypothesisRevisionAction.NARROW
    assert primary_revision.new_version_required is True
    assert any(
        item.kind is e.ContradictionKind.HYPOTHESIS_PREDICTION_MISS
        and item.hypothesis_ids == (primary_id,)
        for item in campaign.contradiction_queue
    )


def test_equal_realized_likelihoods_force_new_measurement_or_stop(
    uninformative_update_fixture,
) -> None:
    campaign = _run_update(
        uninformative_update_fixture,
        campaign_id="campaign:f9s6:uninformative-outcome",
    )
    audit = campaign.audit
    assert audit is not None
    assert audit.realized_outcome_uninformative is True
    assert audit.realized_entropy_reduction_nats == pytest.approx(0.0, abs=1e-12)
    assert campaign.world_revision.action is e.WorldRevisionAction.SEEK_NEW_MEASUREMENT_OR_STOP
    assert any(
        item.kind is e.ContradictionKind.REALIZED_OUTCOME_UNINFORMATIVE
        for item in campaign.contradiction_queue
    )


def test_all_models_low_likelihood_forces_new_hypothesis_lineage(
    uninformative_update_fixture,
) -> None:
    parts = _with_update_policy(
        uninformative_update_fixture,
        all_model_miss_probability_ceiling=0.45,
    )
    campaign = _run_update(parts, campaign_id="campaign:f9s6:all-model-miss")

    assert campaign.audit.all_models_low_likelihood is True
    assert campaign.world_revision.action is e.WorldRevisionAction.FORK_HYPOTHESIS_SET
    assert campaign.world_revision.new_hypothesis_lineage_required is True
    assert any(
        item.kind is e.ContradictionKind.ALL_MODELS_LOW_LIKELIHOOD
        for item in campaign.contradiction_queue
    )


def test_unaligned_likelihood_sensitivity_blocks_update(
    unaligned_sensitivity_fixture,
) -> None:
    campaign = _run_update(
        unaligned_sensitivity_fixture,
        campaign_id="campaign:f9s6:unaligned-sensitivity",
    )

    assert campaign.disposition is e.WorldBeliefUpdateDisposition.BLOCKED_LIKELIHOOD
    assert campaign.blockers == ("sensitivity:scenario_matrix_incomplete",)
    assert campaign.audit is None
    assert campaign.updated_world_model_snapshot is None
    assert campaign.hypothesis_revisions == ()


@pytest.mark.parametrize(
    ("boundary", "failure_kind"),
    [
        ("selection", e.ObservationValidationFailureKind.SELECTION_ARCHIVE_INVALID),
        ("prediction", e.ObservationValidationFailureKind.PREDICTION_ARCHIVE_INVALID),
        ("observation", e.ObservationValidationFailureKind.OBSERVATION_STORE_INVALID),
    ],
)
def test_missing_physical_inputs_block_validation_without_partial_evidence(
    update_fixture,
    tmp_path,
    boundary,
    failure_kind,
) -> None:
    selection_archive = update_fixture["selection_archive"]
    prediction_archive = update_fixture["prediction_archive"]
    observation_store = update_fixture["observation_store"]
    if boundary == "selection":
        selection_archive = ContentAddressedResponseArchive(tmp_path / "empty-selection")
    elif boundary == "prediction":
        prediction_archive = ContentAddressedResponseArchive(tmp_path / "empty-prediction")
    else:
        observation_store = e.ObservationStagingStore(
            tmp_path / "empty-observation",
            prediction_archive=prediction_archive,
        )
    request = update_fixture["validation_request"]
    campaign = asyncio.run(
        e.run_observation_validation(
            campaign_id=f"campaign:f9s6:missing-{boundary}",
            policy=update_fixture["validation_policy"],
            request=request,
            validator=update_fixture["validator"],
            selection_archive=selection_archive,
            prediction_archive=prediction_archive,
            observation_store=observation_store,
            clock=StepClock(request.issued_at + timedelta(minutes=2)),
        )
    )

    assert campaign.disposition is e.ObservationValidationDisposition.BLOCKED_EXECUTION
    assert campaign.failure.kind is failure_kind
    assert campaign.selection_verification is None
    assert campaign.prediction_verification is None
    assert campaign.observation_verification is None
    assert campaign.validation_batch is None
    assert campaign.probe is None


def test_validator_exception_invalid_output_and_future_output_are_hash_only_failures(
    update_fixture,
) -> None:
    request = update_fixture["validation_request"]
    raw_output_secret = "f9s6-invalid-validator-output-secret"
    exception = _run_validation(
        update_fixture,
        validator=FixtureObservationValidator(
            update_fixture["validator_manifest"],
            completed_at=request.issued_at + timedelta(minutes=1),
            error=RuntimeError("sensitive raw validator detail"),
        ),
        campaign_id="campaign:f9s6:validator-exception",
    )
    assert exception.failure.kind is e.ObservationValidationFailureKind.VALIDATOR_EXCEPTION
    assert exception.failure.raw_output_sha256 is None
    assert "sensitive" not in exception.model_dump_json()

    invalid = _run_validation(
        update_fixture,
        validator=FixtureObservationValidator(
            update_fixture["validator_manifest"],
            completed_at=request.issued_at + timedelta(minutes=1),
            raw_output={"invalid_payload": raw_output_secret},
        ),
        campaign_id="campaign:f9s6:invalid-output",
    )
    assert invalid.failure.kind is e.ObservationValidationFailureKind.INVALID_OUTPUT
    assert invalid.failure.raw_output_sha256 is not None
    assert raw_output_secret not in invalid.model_dump_json()

    future = _run_validation(
        update_fixture,
        validator=FixtureObservationValidator(
            update_fixture["validator_manifest"],
            completed_at=request.issued_at + timedelta(days=1),
        ),
        campaign_id="campaign:f9s6:future-output",
    )
    assert future.failure.kind is e.ObservationValidationFailureKind.INVALID_OUTPUT
    assert future.failure.raw_output_sha256 is not None


def test_validator_independence_policy_freeze_and_selection_timing_are_enforced(
    update_fixture,
) -> None:
    selection = update_fixture["selection"]
    shared_principal = selection.assessor_manifest.assessor_principal_sha256
    non_independent = revalidate(
        e.ObservationValidatorManifest,
        update_fixture["validator_manifest"],
        validator_principal_sha256=shared_principal,
    )
    with pytest.raises(ValueError, match="must be independent"):
        e.build_observation_validation_request(
            validation_id="f9s6-non-independent-validator",
            committed_selection=update_fixture["committed_selection"],
            observation_receipt=update_fixture["observation_receipt"],
            validator_manifest=non_independent,
            policy=update_fixture["validation_policy"],
            selection_archive_custody_sha256=update_fixture["selection_archive_custody_sha256"],
            prediction_archive_custody_sha256=update_fixture["prediction_archive_custody_sha256"],
            observation_store_custody_sha256=update_fixture["observation_store_custody_sha256"],
            issued_at=update_fixture["validation_request"].issued_at,
        )

    late_policy = revalidate(
        e.ObservationValidationPolicy,
        update_fixture["validation_policy"],
        frozen_at=update_fixture["committed_selection"].committed_at + timedelta(microseconds=1),
    )
    with pytest.raises(ValueError, match="not frozen before selection"):
        e.build_observation_validation_request(
            validation_id="f9s6-late-validation-policy",
            committed_selection=update_fixture["committed_selection"],
            observation_receipt=update_fixture["observation_receipt"],
            validator_manifest=update_fixture["validator_manifest"],
            policy=late_policy,
            selection_archive_custody_sha256=update_fixture["selection_archive_custody_sha256"],
            prediction_archive_custody_sha256=update_fixture["prediction_archive_custody_sha256"],
            observation_store_custody_sha256=update_fixture["observation_store_custody_sha256"],
            issued_at=update_fixture["validation_request"].issued_at,
        )

    observed_at = update_fixture["observation_receipt"].observed_at
    late_selection_time = observed_at + timedelta(microseconds=1)
    committed = update_fixture["committed_selection"]
    late_selection = revalidate(
        e.CommittedExperimentSelectionCampaign,
        committed,
        committed_at=late_selection_time,
        ledger=revalidate(
            type(committed.ledger),
            committed.ledger,
            archived_at=late_selection_time,
        ),
    )
    with pytest.raises(ValueError, match="committed before observation"):
        e.build_observation_validation_request(
            validation_id="f9s6-post-observation-selection",
            committed_selection=late_selection,
            observation_receipt=update_fixture["observation_receipt"],
            validator_manifest=update_fixture["validator_manifest"],
            policy=update_fixture["validation_policy"],
            selection_archive_custody_sha256=update_fixture["selection_archive_custody_sha256"],
            prediction_archive_custody_sha256=update_fixture["prediction_archive_custody_sha256"],
            observation_store_custody_sha256=update_fixture["observation_store_custody_sha256"],
            issued_at=update_fixture["validation_request"].issued_at,
        )


def test_missing_validation_archive_blocks_update_without_partial_posterior(
    update_fixture,
    tmp_path,
) -> None:
    campaign = e.run_world_belief_update(
        campaign_id="campaign:f9s6:missing-validation-archive",
        policy=update_fixture["update_policy"],
        request=update_fixture["update_request"],
        validation_archive=ContentAddressedResponseArchive(tmp_path / "empty-validation-archive"),
        clock=StepClock(update_fixture["update_request"].issued_at + timedelta(minutes=1)),
    )

    assert campaign.disposition is e.WorldBeliefUpdateDisposition.BLOCKED_EXECUTION
    assert campaign.failure.kind is e.WorldBeliefUpdateFailureKind.VALIDATION_ARCHIVE_INVALID
    assert campaign.audit is None
    assert campaign.updated_world_model_snapshot is None
    assert campaign.contradiction_queue == ()


def test_validation_and_update_decisions_are_rederived_and_archives_detect_tampering(
    update_fixture,
    tmp_path,
) -> None:
    validation = update_fixture["validation_campaign"]
    forged_probe = revalidate(
        e.ObservationValidationProbe,
        validation.probe,
        blockers=("measurement:invalid",),
        valid_for_belief_update=False,
    )
    with pytest.raises(ValidationError, match="not mechanically derived"):
        revalidate(
            e.ObservationValidationCampaign,
            validation,
            probe=forged_probe,
            blockers=forged_probe.blockers,
            disposition=e.ObservationValidationDisposition.REJECTED_SCIENTIFIC,
        )

    update = _run_update(update_fixture, campaign_id="campaign:f9s6:archive-update")
    forged_audit = revalidate(
        e.WorldBeliefUpdateAudit,
        update.audit,
        prior_predictive_surprisal_nats=update.audit.prior_predictive_surprisal_nats + 0.1,
    )
    with pytest.raises(ValidationError, match="not mechanically derived"):
        revalidate(e.WorldBeliefUpdateCampaign, update, audit=forged_audit)

    archive = ContentAddressedResponseArchive(tmp_path / "world-update-archive")
    committed_at = update.generated_at + timedelta(minutes=1)
    committed = e.commit_world_belief_update_campaign(
        archive=archive,
        campaign=update,
        committed_at=committed_at,
    )
    assert committed.committed_at == committed_at
    assert len(committed.receipt_sha256) == 64
    assert (
        e.load_world_belief_update_campaign(
            archive=archive,
            ledger=committed.ledger,
        )
        == update
    )
    ledger_path = archive.root / committed.ledger.relative_path
    ledger_path.chmod(0o600)
    payload = ledger_path.read_bytes()
    ledger_path.write_bytes(payload[:-1] + (b"0" if payload[-1:] != b"0" else b"1"))
    with pytest.raises(ResponseArchiveCorruption):
        e.load_world_belief_update_campaign(archive=archive, ledger=committed.ledger)
