from __future__ import annotations

import asyncio
import inspect
from datetime import timedelta

import pytest
from pydantic import ValidationError

import aletheia.epistemics as e
from aletheia.knowledge.response_archive import (
    ContentAddressedResponseArchive,
    ResponseArchiveCorruption,
)
from knowledge.f8s5_fixtures import build_f8s5_direction_fixture, build_f8s5_live_fixture
from aletheia.scheduler.k3_acceptance import score_k3

from .f9s2_fixtures import StepClock, build_f9s2_fixture, digest, revalidate
from .f9s3_fixtures import build_f9s3_fixture
from .f9s7_fixtures import build_f9s7_fixture


def _unaligned_sensitivity_specs():
    return (
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


@pytest.fixture(scope="module")
def source_fixture(tmp_path_factory):
    live = asyncio.run(
        build_f8s5_live_fixture(
            tmp_path_factory.mktemp("f9s7-strong"),
            novelty_kind="strong",
        )
    )
    gate = build_f8s5_direction_fixture(live)["gate"]
    hypotheses = build_f9s2_fixture(gate)
    hypothesis_campaign = asyncio.run(
        e.run_competing_hypothesis_generation(
            campaign_id="campaign:f9s7:source-hypotheses",
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
            campaign_id="campaign:f9s7:source-causal-audit",
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
def acceptance_fixture(source_fixture, tmp_path_factory):
    return build_f9s7_fixture(
        source_fixture,
        tmp_path_factory.mktemp("f9s7-acceptance"),
    )


@pytest.fixture(scope="module")
def negative_acceptance_fixture(source_fixture, tmp_path_factory):
    return build_f9s7_fixture(
        source_fixture,
        tmp_path_factory.mktemp("f9s7-negative"),
        outcome_role=e.HypothesisRole.NULL,
    )


def _run_acceptance(
    parts,
    *,
    campaign_id="campaign:f9s7:acceptance-test",
    selection_archive=None,
    validation_archive=None,
    update_archive=None,
    evidence_archive=None,
):
    request = parts["acceptance_request"]
    return e.run_k3_acceptance(
        campaign_id=campaign_id,
        policy=parts["acceptance_policy"],
        scorer_manifest=parts["scorer_manifest"],
        request=request,
        selection_archive=selection_archive or parts["selection_archive"],
        validation_archive=validation_archive or parts["validation_archive"],
        update_archive=update_archive or parts["update_archive"],
        evidence_archive=evidence_archive or parts["evidence_archive"],
        clock=StepClock(request.issued_at + timedelta(minutes=1)),
    )


def _checks(campaign):
    return {item.kind: item for item in campaign.checks}


def _recommit_evidence(parts, root, *, evidence=None, policy=None, scorer_manifest=None):
    changed = dict(parts)
    evidence = evidence or parts["evidence_ledger"]
    policy = policy or parts["acceptance_policy"]
    scorer_manifest = scorer_manifest or parts["scorer_manifest"]
    archive = ContentAddressedResponseArchive(root / "evidence-archive")
    committed = e.commit_k3_evidence_ledger(
        archive=archive,
        evidence=evidence,
        committed_at=parts["committed_evidence"].committed_at,
    )
    old = parts["acceptance_request"]
    request = e.build_k3_acceptance_request(
        acceptance_id=old.acceptance_id,
        rounds=old.rounds,
        committed_evidence_ledger=committed,
        scorer_manifest=scorer_manifest,
        policy=policy,
        selection_archive_custody_sha256=old.selection_archive_custody_sha256,
        validation_archive_custody_sha256=old.validation_archive_custody_sha256,
        update_archive_custody_sha256=old.update_archive_custody_sha256,
        evidence_archive_custody_sha256=old.evidence_archive_custody_sha256,
        issued_at=old.issued_at,
    )
    changed.update(
        {
            "evidence_ledger": evidence,
            "evidence_archive": archive,
            "committed_evidence": committed,
            "acceptance_policy": policy,
            "scorer_manifest": scorer_manifest,
            "acceptance_request": request,
        }
    )
    return changed


def test_complete_committed_k3_chain_is_independently_accepted(acceptance_fixture) -> None:
    campaign = _run_acceptance(acceptance_fixture)

    assert campaign.disposition is e.K3AcceptanceDisposition.ACCEPTED
    assert len(campaign.round_verifications) == 1
    assert campaign.evidence_verification is not None
    assert len(campaign.checks) == len(e.K3AcceptanceCheckKind)
    checks = _checks(campaign)
    assert checks[e.K3AcceptanceCheckKind.MECHANISM_CLAIM_GATE].status is (
        e.K3AcceptanceCheckStatus.NOT_APPLICABLE
    )
    assert checks[e.K3AcceptanceCheckKind.NEGATIVE_RESULT_REVISION].status is (
        e.K3AcceptanceCheckStatus.NOT_APPLICABLE
    )
    assert all(item.status is not e.K3AcceptanceCheckStatus.FAIL for item in campaign.checks)


def test_no_validated_update_cannot_vacuously_earn_scientific_exit(
    source_fixture,
    tmp_path,
) -> None:
    parts = build_f9s7_fixture(
        source_fixture,
        tmp_path / "no-update",
        validation_overrides={"data_role": e.ObservationDataRole.EXPLORATION},
    )

    campaign = _run_acceptance(parts, campaign_id="campaign:f9s7:no-update")

    assert campaign.disposition is e.K3AcceptanceDisposition.PARTIAL_NO_SCIENTIFIC_EXIT
    checks = _checks(campaign)
    assert checks[e.K3AcceptanceCheckKind.VALID_OBSERVATION_UPDATE_BIJECTION].status is (
        e.K3AcceptanceCheckStatus.PASS
    )
    assert checks[e.K3AcceptanceCheckKind.POSITIVE_VALIDATED_UPDATE].status is (
        e.K3AcceptanceCheckStatus.FAIL
    )


def test_validated_observation_without_its_one_update_is_integrity_rejection(
    source_fixture,
    tmp_path,
) -> None:
    parts = build_f9s7_fixture(
        source_fixture,
        tmp_path / "dropped-update",
        drop_update=True,
    )

    campaign = _run_acceptance(parts, campaign_id="campaign:f9s7:dropped-update")

    assert campaign.disposition is e.K3AcceptanceDisposition.REJECTED_INTEGRITY
    check = _checks(campaign)[e.K3AcceptanceCheckKind.VALID_OBSERVATION_UPDATE_BIJECTION]
    assert check.status is e.K3AcceptanceCheckStatus.FAIL


def test_high_belief_discrimination_is_a_nonvacuous_exit_gate(
    source_fixture,
    tmp_path,
) -> None:
    parts = build_f9s7_fixture(
        source_fixture,
        tmp_path / "high-discrimination-floor",
        acceptance_policy_updates={
            "minimum_high_belief_pairwise_total_variation": 0.99,
        },
    )

    campaign = _run_acceptance(parts, campaign_id="campaign:f9s7:low-discrimination")

    assert campaign.disposition is e.K3AcceptanceDisposition.PARTIAL_NO_SCIENTIFIC_EXIT
    check = _checks(campaign)[e.K3AcceptanceCheckKind.HIGH_BELIEF_DISCRIMINATION]
    assert check.status is e.K3AcceptanceCheckStatus.FAIL
    assert check.observed_value < check.threshold_value


def test_primary_negative_result_materializes_append_only_scope_change(
    negative_acceptance_fixture,
) -> None:
    campaign = _run_acceptance(
        negative_acceptance_fixture,
        campaign_id="campaign:f9s7:negative-result",
    )

    assert campaign.disposition is e.K3AcceptanceDisposition.ACCEPTED
    check = _checks(campaign)[e.K3AcceptanceCheckKind.NEGATIVE_RESULT_REVISION]
    assert check.status is e.K3AcceptanceCheckStatus.PASS
    assert check.observed_count == 1
    assert negative_acceptance_fixture["revision_materializations"]
    for materialization in negative_acceptance_fixture["revision_materializations"]:
        assert materialization.revised_hypothesis.parent_hypothesis_sha256 is not None


def test_negative_result_without_materialized_revision_is_rejected(
    negative_acceptance_fixture,
    tmp_path,
) -> None:
    incomplete = revalidate(
        e.K3EvidenceLedger,
        negative_acceptance_fixture["evidence_ledger"],
        revision_materializations=(),
    )
    parts = _recommit_evidence(
        negative_acceptance_fixture,
        tmp_path / "missing-negative-revision",
        evidence=incomplete,
    )

    campaign = _run_acceptance(parts, campaign_id="campaign:f9s7:missing-negative-revision")

    assert campaign.disposition is e.K3AcceptanceDisposition.REJECTED_INTEGRITY
    checks = _checks(campaign)
    assert checks[e.K3AcceptanceCheckKind.NEGATIVE_RESULT_REVISION].status is (
        e.K3AcceptanceCheckStatus.FAIL
    )
    assert checks[e.K3AcceptanceCheckKind.PERSISTENCE_COMPLETENESS].status is (
        e.K3AcceptanceCheckStatus.FAIL
    )


def test_negative_result_rewording_without_changed_prediction_is_rejected(
    negative_acceptance_fixture,
    tmp_path,
) -> None:
    source = negative_acceptance_fixture["round_evidence"]
    snapshot = source.committed_selection.campaign.request.candidates[
        0
    ].committed_prediction.campaign.source_causal_campaign.source_campaign.world_model_snapshot
    assert snapshot is not None
    primary_id = next(
        item.hypothesis_id for item in snapshot.hypotheses if item.role is e.HypothesisRole.PRIMARY
    )
    target = next(
        item
        for item in negative_acceptance_fixture["revision_materializations"]
        if item.revised_hypothesis.hypothesis_id == primary_id
    )
    assert target.revised_predictions
    source_prediction = next(
        item for item in snapshot.predictions if item.hypothesis_id == primary_id
    )
    unchanged_prediction = revalidate(
        e.Prediction,
        target.revised_predictions[0],
        expected_outcome=source_prediction.expected_outcome,
    )
    reword_only = revalidate(
        e.K3RevisionMaterialization,
        target,
        revised_predictions=(unchanged_prediction,),
    )
    materializations = tuple(
        sorted(
            (
                reword_only if item.directive_sha256 == target.directive_sha256 else item
                for item in negative_acceptance_fixture["revision_materializations"]
            ),
            key=lambda item: (item.source_update_receipt_sha256, item.directive_sha256),
        )
    )
    old = negative_acceptance_fixture["evidence_ledger"]
    evidence = e.build_k3_evidence_ledger(
        ledger_id=old.ledger_id,
        rounds=(negative_acceptance_fixture["round_evidence"],),
        revision_materializations=materializations,
        mechanism_claims=old.mechanism_claims,
        terminal_decision=old.terminal_decision,
        persistence_principal_sha256=old.persistence_principal_sha256,
        persisted_at=old.persisted_at,
    )
    parts = _recommit_evidence(
        negative_acceptance_fixture,
        tmp_path / "reword-only-negative",
        evidence=evidence,
    )

    campaign = _run_acceptance(parts, campaign_id="campaign:f9s7:reword-only-negative")

    assert campaign.disposition is e.K3AcceptanceDisposition.REJECTED_INTEGRITY
    check = _checks(campaign)[e.K3AcceptanceCheckKind.NEGATIVE_RESULT_REVISION]
    assert check.status is e.K3AcceptanceCheckStatus.FAIL
    assert any(
        "narrow_materialization_only_reworded_without_new_prediction" in item
        for item in check.reason_codes
    )


def test_unauthorized_issued_mechanism_claim_is_integrity_rejection(
    acceptance_fixture,
    tmp_path,
) -> None:
    update = acceptance_fixture["committed_updates"][0]
    assert update.campaign.audit is not None
    claim = e.MechanismClaimRecord(
        claim_id="claim.f9s7.unauthorized.001",
        round_id=acceptance_fixture["round_evidence"].round_id,
        source_update_receipt_sha256=update.receipt_sha256,
        hypothesis_id=update.campaign.audit.maximum_hypothesis_ids[0],
        requested_ceiling=e.CausalClaimCeiling.CAUSAL_CANDIDATE,
        disposition=e.MechanismClaimDisposition.ISSUED,
        claim_artifact_sha256=digest("f9s7:unauthorized-claim"),
        evidence_sha256s=(update.receipt_sha256,),
        decided_at=acceptance_fixture["terminal_decision"].decided_at,
    )
    evidence = revalidate(
        e.K3EvidenceLedger,
        acceptance_fixture["evidence_ledger"],
        mechanism_claims=(claim,),
    )
    parts = _recommit_evidence(
        acceptance_fixture,
        tmp_path / "unauthorized-claim",
        evidence=evidence,
    )

    campaign = _run_acceptance(parts, campaign_id="campaign:f9s7:unauthorized-claim")

    assert campaign.disposition is e.K3AcceptanceDisposition.REJECTED_INTEGRITY
    check = _checks(campaign)[e.K3AcceptanceCheckKind.MECHANISM_CLAIM_GATE]
    assert check.status is e.K3AcceptanceCheckStatus.FAIL
    assert any(
        "alternative_explanation_not_robustly_excluded" in item for item in check.reason_codes
    )


def test_withheld_mechanism_claim_preserves_acceptance(
    acceptance_fixture,
    tmp_path,
) -> None:
    update = acceptance_fixture["committed_updates"][0]
    assert update.campaign.audit is not None
    claim = e.MechanismClaimRecord(
        claim_id="claim.f9s7.withheld.001",
        round_id=acceptance_fixture["round_evidence"].round_id,
        source_update_receipt_sha256=update.receipt_sha256,
        hypothesis_id=update.campaign.audit.maximum_hypothesis_ids[0],
        requested_ceiling=e.CausalClaimCeiling.CAUSAL_CANDIDATE,
        disposition=e.MechanismClaimDisposition.WITHHELD,
        evidence_sha256s=(update.receipt_sha256,),
        decided_at=acceptance_fixture["terminal_decision"].decided_at,
    )
    evidence = revalidate(
        e.K3EvidenceLedger,
        acceptance_fixture["evidence_ledger"],
        mechanism_claims=(claim,),
    )
    parts = _recommit_evidence(
        acceptance_fixture,
        tmp_path / "withheld-claim",
        evidence=evidence,
    )

    campaign = _run_acceptance(parts, campaign_id="campaign:f9s7:withheld-claim")

    assert campaign.disposition is e.K3AcceptanceDisposition.ACCEPTED
    assert _checks(campaign)[e.K3AcceptanceCheckKind.MECHANISM_CLAIM_GATE].status is (
        e.K3AcceptanceCheckStatus.PASS
    )


def test_terminal_action_must_follow_world_revision(
    acceptance_fixture,
    tmp_path,
) -> None:
    terminal = revalidate(
        e.K3TerminalDecision,
        acceptance_fixture["terminal_decision"],
        action=e.K3TerminalAction.FORK_HYPOTHESIS_SET,
    )
    evidence = revalidate(
        e.K3EvidenceLedger,
        acceptance_fixture["evidence_ledger"],
        terminal_decision=terminal,
    )
    parts = _recommit_evidence(
        acceptance_fixture,
        tmp_path / "terminal-conflict",
        evidence=evidence,
    )

    campaign = _run_acceptance(parts, campaign_id="campaign:f9s7:terminal-conflict")

    assert campaign.disposition is e.K3AcceptanceDisposition.REJECTED_INTEGRITY
    check = _checks(campaign)[e.K3AcceptanceCheckKind.TERMINAL_DECISION]
    assert check.status is e.K3AcceptanceCheckStatus.FAIL
    assert "terminal_action_conflicts_with_world_revision" in check.reason_codes


def test_persistence_ledger_must_cover_exact_attempt_and_version_sets(
    acceptance_fixture,
    tmp_path,
) -> None:
    evidence = revalidate(
        e.K3EvidenceLedger,
        acceptance_fixture["evidence_ledger"],
        validation_receipt_sha256s=(digest("f9s7:forged-validation-receipt"),),
    )
    parts = _recommit_evidence(
        acceptance_fixture,
        tmp_path / "incomplete-persistence",
        evidence=evidence,
    )

    campaign = _run_acceptance(parts, campaign_id="campaign:f9s7:incomplete-persistence")

    assert campaign.disposition is e.K3AcceptanceDisposition.REJECTED_INTEGRITY
    check = _checks(campaign)[e.K3AcceptanceCheckKind.PERSISTENCE_COMPLETENESS]
    assert check.status is e.K3AcceptanceCheckStatus.FAIL
    assert "persisted_set_not_exact:validation_receipt_sha256s" in check.reason_codes


@pytest.mark.parametrize(
    ("archive_name", "failure_kind"),
    (
        ("selection_archive", e.K3AcceptanceFailureKind.SELECTION_ARCHIVE_INVALID),
        ("validation_archive", e.K3AcceptanceFailureKind.VALIDATION_ARCHIVE_INVALID),
        ("update_archive", e.K3AcceptanceFailureKind.UPDATE_ARCHIVE_INVALID),
        ("evidence_archive", e.K3AcceptanceFailureKind.EVIDENCE_LEDGER_ARCHIVE_INVALID),
    ),
)
def test_missing_physical_archive_blocks_without_partial_scoring(
    acceptance_fixture,
    tmp_path,
    archive_name,
    failure_kind,
) -> None:
    overrides = {archive_name: ContentAddressedResponseArchive(tmp_path / f"empty-{archive_name}")}

    campaign = _run_acceptance(
        acceptance_fixture,
        campaign_id=f"campaign:f9s7:missing-{archive_name}",
        **overrides,
    )

    assert campaign.disposition is e.K3AcceptanceDisposition.BLOCKED_EXECUTION
    assert campaign.failure.kind is failure_kind
    assert campaign.round_verifications == ()
    assert campaign.evidence_verification is None
    assert campaign.checks == ()


def test_scorer_policy_freeze_and_role_independence_are_enforced(
    acceptance_fixture,
) -> None:
    selection_commit = acceptance_fixture["committed_selection"].committed_at
    late_policy = revalidate(
        e.K3AcceptancePolicy,
        acceptance_fixture["acceptance_policy"],
        frozen_at=selection_commit + timedelta(microseconds=1),
    )
    with pytest.raises(ValueError, match="frozen before first selection"):
        e.build_k3_acceptance_request(
            acceptance_id="f9s7-late-policy-request",
            rounds=(acceptance_fixture["round_evidence"],),
            committed_evidence_ledger=acceptance_fixture["committed_evidence"],
            scorer_manifest=acceptance_fixture["scorer_manifest"],
            policy=late_policy,
            selection_archive_custody_sha256=acceptance_fixture["selection_archive_custody_sha256"],
            validation_archive_custody_sha256=acceptance_fixture[
                "validation_archive_custody_sha256"
            ],
            update_archive_custody_sha256=acceptance_fixture["update_archive_custody_sha256"],
            evidence_archive_custody_sha256=acceptance_fixture["evidence_archive_custody_sha256"],
            issued_at=acceptance_fixture["acceptance_request"].issued_at,
        )

    validator_principal = acceptance_fixture[
        "committed_validation"
    ].campaign.validator_manifest.validator_principal_sha256
    shared_manifest = revalidate(
        e.K3AcceptanceScorerManifest,
        acceptance_fixture["scorer_manifest"],
        scorer_principal_sha256=validator_principal,
    )
    shared_policy = revalidate(
        e.K3AcceptancePolicy,
        acceptance_fixture["acceptance_policy"],
        scorer_principal_sha256=validator_principal,
    )
    with pytest.raises(ValueError, match="must be independent"):
        e.build_k3_acceptance_request(
            acceptance_id="f9s7-non-independent-request",
            rounds=(acceptance_fixture["round_evidence"],),
            committed_evidence_ledger=acceptance_fixture["committed_evidence"],
            scorer_manifest=shared_manifest,
            policy=shared_policy,
            selection_archive_custody_sha256=acceptance_fixture["selection_archive_custody_sha256"],
            validation_archive_custody_sha256=acceptance_fixture[
                "validation_archive_custody_sha256"
            ],
            update_archive_custody_sha256=acceptance_fixture["update_archive_custody_sha256"],
            evidence_archive_custody_sha256=acceptance_fixture["evidence_archive_custody_sha256"],
            issued_at=acceptance_fixture["acceptance_request"].issued_at,
        )


def test_checks_are_rederived_and_acceptance_archive_detects_tampering(
    acceptance_fixture,
    tmp_path,
) -> None:
    campaign = _run_acceptance(
        acceptance_fixture,
        campaign_id="campaign:f9s7:rederive-and-archive",
    )
    forged = revalidate(
        e.K3AcceptanceCheck,
        campaign.checks[0],
        status=e.K3AcceptanceCheckStatus.FAIL,
        reason_codes=("forged_failure",),
    )
    with pytest.raises(ValidationError, match="not mechanically derived"):
        revalidate(
            e.K3AcceptanceCampaign,
            campaign,
            checks=(forged, *campaign.checks[1:]),
            disposition=e.K3AcceptanceDisposition.REJECTED_INTEGRITY,
        )

    archive = ContentAddressedResponseArchive(tmp_path / "acceptance-archive")
    committed = e.commit_k3_acceptance_campaign(
        archive=archive,
        campaign=campaign,
        committed_at=campaign.generated_at + timedelta(minutes=1),
    )
    assert e.load_k3_acceptance_campaign(archive=archive, ledger=committed.ledger) == campaign
    ledger_path = archive.root / committed.ledger.relative_path
    ledger_path.chmod(0o600)
    payload = ledger_path.read_bytes()
    ledger_path.write_bytes(payload[:-1] + (b"0" if payload[-1:] != b"0" else b"1"))
    with pytest.raises(ResponseArchiveCorruption):
        e.load_k3_acceptance_campaign(archive=archive, ledger=committed.ledger)


def test_likelihood_blocked_update_attempt_is_honest_partial_not_full(
    source_fixture,
    tmp_path,
) -> None:
    parts = build_f9s7_fixture(
        source_fixture,
        tmp_path / "likelihood-blocked",
        candidate_specs=_unaligned_sensitivity_specs(),
    )
    assert parts["committed_updates"][0].campaign.disposition is (
        e.WorldBeliefUpdateDisposition.BLOCKED_LIKELIHOOD
    )

    campaign = _run_acceptance(parts, campaign_id="campaign:f9s7:likelihood-blocked")

    assert campaign.disposition is e.K3AcceptanceDisposition.PARTIAL_NO_SCIENTIFIC_EXIT
    checks = _checks(campaign)
    assert checks[e.K3AcceptanceCheckKind.VALID_OBSERVATION_UPDATE_BIJECTION].status is (
        e.K3AcceptanceCheckStatus.PASS
    )
    assert checks[e.K3AcceptanceCheckKind.POSITIVE_VALIDATED_UPDATE].status is (
        e.K3AcceptanceCheckStatus.FAIL
    )


def test_nonmechanistic_claim_within_causal_ceiling_does_not_require_exclusion(
    acceptance_fixture,
    tmp_path,
) -> None:
    update = acceptance_fixture["committed_updates"][0]
    assert update.campaign.audit is not None
    claim = e.MechanismClaimRecord(
        claim_id="claim.f9s7.association.001",
        round_id=acceptance_fixture["round_evidence"].round_id,
        source_update_receipt_sha256=update.receipt_sha256,
        hypothesis_id=update.campaign.audit.maximum_hypothesis_ids[0],
        requested_ceiling=e.CausalClaimCeiling.ASSOCIATION_ONLY,
        disposition=e.MechanismClaimDisposition.ISSUED,
        claim_artifact_sha256=digest("f9s7:association-claim"),
        evidence_sha256s=(update.receipt_sha256,),
        decided_at=acceptance_fixture["terminal_decision"].decided_at,
    )
    evidence = revalidate(
        e.K3EvidenceLedger,
        acceptance_fixture["evidence_ledger"],
        mechanism_claims=(claim,),
    )
    parts = _recommit_evidence(
        acceptance_fixture,
        tmp_path / "association-claim",
        evidence=evidence,
    )

    campaign = _run_acceptance(parts, campaign_id="campaign:f9s7:association-claim")

    assert campaign.disposition is e.K3AcceptanceDisposition.ACCEPTED
    assert _checks(campaign)[e.K3AcceptanceCheckKind.MECHANISM_CLAIM_GATE].status is (
        e.K3AcceptanceCheckStatus.PASS
    )


def test_robustly_dominant_mechanism_can_be_issued_within_causal_ceiling(
    acceptance_fixture,
    tmp_path,
) -> None:
    update = acceptance_fixture["committed_updates"][0]
    assert update.campaign.audit is not None
    claim = e.MechanismClaimRecord(
        claim_id="claim.f9s7.authorized-mechanism.001",
        round_id=acceptance_fixture["round_evidence"].round_id,
        source_update_receipt_sha256=update.receipt_sha256,
        hypothesis_id=update.campaign.audit.maximum_hypothesis_ids[0],
        requested_ceiling=e.CausalClaimCeiling.CAUSAL_CANDIDATE,
        disposition=e.MechanismClaimDisposition.ISSUED,
        claim_artifact_sha256=digest("f9s7:authorized-mechanism-claim"),
        evidence_sha256s=(update.receipt_sha256,),
        decided_at=acceptance_fixture["terminal_decision"].decided_at,
    )
    evidence = revalidate(
        e.K3EvidenceLedger,
        acceptance_fixture["evidence_ledger"],
        mechanism_claims=(claim,),
    )
    policy = revalidate(
        e.K3AcceptancePolicy,
        acceptance_fixture["acceptance_policy"],
        mechanism_claim_posterior_floor=0.6,
        alternative_exclusion_posterior_ceiling=0.2,
    )
    parts = _recommit_evidence(
        acceptance_fixture,
        tmp_path / "authorized-mechanism",
        evidence=evidence,
        policy=policy,
    )

    campaign = _run_acceptance(parts, campaign_id="campaign:f9s7:authorized-mechanism")

    assert campaign.disposition is e.K3AcceptanceDisposition.ACCEPTED
    assert _checks(campaign)[e.K3AcceptanceCheckKind.MECHANISM_CLAIM_GATE].status is (
        e.K3AcceptanceCheckStatus.PASS
    )


def test_mechanism_claim_cannot_predate_its_source_update(
    acceptance_fixture,
    tmp_path,
) -> None:
    update = acceptance_fixture["committed_updates"][0]
    assert update.campaign.audit is not None
    claim = e.MechanismClaimRecord(
        claim_id="claim.f9s7.predated.001",
        round_id=acceptance_fixture["round_evidence"].round_id,
        source_update_receipt_sha256=update.receipt_sha256,
        hypothesis_id=update.campaign.audit.maximum_hypothesis_ids[0],
        requested_ceiling=e.CausalClaimCeiling.ASSOCIATION_ONLY,
        disposition=e.MechanismClaimDisposition.ISSUED,
        claim_artifact_sha256=digest("f9s7:predated-claim"),
        evidence_sha256s=(update.receipt_sha256,),
        decided_at=update.committed_at - timedelta(microseconds=1),
    )
    evidence = revalidate(
        e.K3EvidenceLedger,
        acceptance_fixture["evidence_ledger"],
        mechanism_claims=(claim,),
    )
    parts = _recommit_evidence(
        acceptance_fixture,
        tmp_path / "predated-claim",
        evidence=evidence,
    )

    campaign = _run_acceptance(parts, campaign_id="campaign:f9s7:predated-claim")

    assert campaign.disposition is e.K3AcceptanceDisposition.REJECTED_INTEGRITY
    check = _checks(campaign)[e.K3AcceptanceCheckKind.MECHANISM_CLAIM_GATE]
    assert any("mechanism_claim_predates_source_update" in item for item in check.reason_codes)


def test_acceptance_surface_has_no_raw_observation_or_tool_path(acceptance_fixture) -> None:
    signature = inspect.signature(e.run_k3_acceptance)
    assert "raw_observation" not in signature.parameters
    assert "observation_store" not in signature.parameters
    assert acceptance_fixture["acceptance_request"].observation_access == "committed_artifacts_only"
    assert acceptance_fixture["scorer_manifest"].tool_names == ()
    assert acceptance_fixture["scorer_manifest"].tool_policy == "none"

    raw_payload = acceptance_fixture["raw_observation"]
    assert raw_payload.decode() not in acceptance_fixture["acceptance_request"].model_dump_json()


def test_round_archive_verification_exactly_binds_all_receipts(acceptance_fixture) -> None:
    campaign = _run_acceptance(
        acceptance_fixture,
        campaign_id="campaign:f9s7:physical-receipts",
    )
    verification = campaign.round_verifications[0]

    assert (
        verification.selection_receipt_sha256
        == acceptance_fixture["committed_selection"].receipt_sha256
    )
    assert verification.validation_receipt_sha256s == (
        acceptance_fixture["committed_validation"].receipt_sha256,
    )
    assert verification.update_receipt_sha256s == tuple(
        item.receipt_sha256 for item in acceptance_fixture["committed_updates"]
    )
    assert (
        campaign.evidence_verification.evidence_receipt_sha256
        == acceptance_fixture["committed_evidence"].receipt_sha256
    )


def test_evidence_ledger_archive_round_trip_and_byte_tamper_detection(
    acceptance_fixture,
    tmp_path,
) -> None:
    archive = ContentAddressedResponseArchive(tmp_path / "evidence-round-trip")
    committed = e.commit_k3_evidence_ledger(
        archive=archive,
        evidence=acceptance_fixture["evidence_ledger"],
        committed_at=acceptance_fixture["committed_evidence"].committed_at,
    )
    assert e.load_k3_evidence_ledger(archive=archive, ledger=committed.ledger) == committed.evidence

    ledger_path = archive.root / committed.ledger.relative_path
    ledger_path.chmod(0o600)
    payload = ledger_path.read_bytes()
    ledger_path.write_bytes(payload[:-1] + (b"0" if payload[-1:] != b"0" else b"1"))
    with pytest.raises(ResponseArchiveCorruption):
        e.load_k3_evidence_ledger(archive=archive, ledger=committed.ledger)


def test_scheduler_entry_point_uses_the_same_independent_scorer(acceptance_fixture) -> None:
    request = acceptance_fixture["acceptance_request"]
    campaign = score_k3(
        campaign_id="campaign:f9s7:scheduler-entry",
        policy=acceptance_fixture["acceptance_policy"],
        scorer_manifest=acceptance_fixture["scorer_manifest"],
        request=request,
        selection_archive=acceptance_fixture["selection_archive"],
        validation_archive=acceptance_fixture["validation_archive"],
        update_archive=acceptance_fixture["update_archive"],
        evidence_archive=acceptance_fixture["evidence_archive"],
        clock=StepClock(request.issued_at + timedelta(minutes=1)),
    )

    assert campaign.disposition is e.K3AcceptanceDisposition.ACCEPTED
    assert tuple(item.kind for item in campaign.checks) == tuple(e.K3AcceptanceCheckKind)


def test_scorer_manifest_rejects_ambient_tools(acceptance_fixture) -> None:
    with pytest.raises(ValidationError, match="cannot receive ambient tools"):
        revalidate(
            e.K3AcceptanceScorerManifest,
            acceptance_fixture["scorer_manifest"],
            tool_names=("filesystem",),
        )
