from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

import aletheia.knowledge as k
from .f8s3_fixtures import sha
from .f8s5_fixtures import (
    RECEIPT_KEY,
    build_f8s5_direction_fixture,
    build_f8s5_live_fixture,
)
from .test_schema_spike import _time


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    return {
        kind: asyncio.run(
            build_f8s5_live_fixture(
                tmp_path_factory.mktemp(f"f8s5-{kind}"),
                novelty_kind=kind,
            )
        )
        for kind in ("incremental", "strong", "known")
    }


def _revalidate(model_type, model, **updates):
    raw = model.model_dump(mode="python")
    raw.update(updates)
    return model_type.model_validate(raw)


def _incomplete_coverage(fixture):
    correction = _revalidate(
        k.ContradictionCorrectionReport,
        fixture["correction_report"],
        correction_retraction_check_complete=False,
    )
    return k.build_calibrated_novelty_coverage_assessment(
        assessment_id="f8s5-incomplete-decision-coverage",
        calibration_report=fixture["report"],
        calibration_receipt_key=RECEIPT_KEY,
        ingestion_bundle=fixture["ingestion_bundle"],
        claim_graph_bundle=fixture["graph_bundle"],
        prior_art_resolution=fixture["prior_art_resolution"],
        correction_report=correction,
        campaign=fixture["campaign"],
        policy_frozen_at=_time("2025-08-02T00:00:00Z"),
        generated_at=_time("2025-08-06T00:00:00Z"),
    )


def test_incremental_direction_advances_only_with_a_weak_claim_ceiling(fixtures) -> None:
    result = build_f8s5_direction_fixture(fixtures["incremental"])

    assert result["package"].derived_classification is (
        k.NoveltyClassification.INCREMENTAL_EXTENSION
    )
    assert result["decision"].assessment.strong_novelty_eligible is False
    assert result["decision"].assessment.claim_strength_ceiling is (k.NoveltyClaimCeiling.WEAK)
    assert result["gate"].disposition is k.ResearchDirectionDisposition.ADVANCE_BOUNDED
    assert result["gate"].experiment_authorized is True


def test_calibrated_reviewed_method_novelty_advances_with_moderate_ceiling(fixtures) -> None:
    result = build_f8s5_direction_fixture(fixtures["strong"])

    assert result["package"].derived_classification is k.NoveltyClassification.NOVEL_METHOD
    assert result["decision"].assessment.strong_novelty_eligible is True
    assert result["decision"].assessment.claim_strength_ceiling is (k.NoveltyClaimCeiling.MODERATE)
    assert result["gate"].disposition is k.ResearchDirectionDisposition.ADVANCE_STRONG
    assert result["gate"].experiment_authorized is True


def test_equivalent_prior_art_rejects_the_direction_and_all_novelty_claims(fixtures) -> None:
    result = build_f8s5_direction_fixture(fixtures["known"])

    assert result["package"].derived_classification is (k.NoveltyClassification.KNOWN_EQUIVALENT)
    assert result["decision"].assessment.claim_strength_ceiling is k.NoveltyClaimCeiling.NONE
    assert result["gate"].disposition is k.ResearchDirectionDisposition.REJECT_KNOWN
    assert result["gate"].experiment_authorized is False


def test_insufficient_artifact_coverage_forces_indeterminate_speculative_block(fixtures) -> None:
    fixture = fixtures["incremental"]
    coverage = _incomplete_coverage(fixture)
    result = build_f8s5_direction_fixture(fixture, coverage=coverage)

    assert result["package"].derived_classification is (
        k.NoveltyClassification.INDETERMINATE_DUE_TO_COVERAGE
    )
    assert result["decision"].assessment.claim_strength_ceiling is (
        k.NoveltyClaimCeiling.SPECULATIVE
    )
    assert result["gate"].disposition is (k.ResearchDirectionDisposition.BLOCKED_COVERAGE)
    assert result["gate"].experiment_authorized is False


def test_review_request_for_more_search_blocks_strong_novelty(fixtures) -> None:
    result = build_f8s5_direction_fixture(
        fixtures["strong"],
        verdicts=(
            k.NoveltyReviewVerdict.CONFIRM_EVIDENCE_PACKAGE,
            k.NoveltyReviewVerdict.REQUEST_MORE_SEARCH,
        ),
    )
    assessment = result["decision"].assessment

    assert assessment.classification is k.NoveltyClassification.NOVEL_METHOD
    assert assessment.strong_novelty_eligible is False
    assert assessment.claim_strength_ceiling is k.NoveltyClaimCeiling.SPECULATIVE
    assert any(
        blocker.startswith("review_request_more_search:")
        for blocker in assessment.unresolved_blockers
    )
    assert result["gate"].disposition is k.ResearchDirectionDisposition.RESEARCH_MORE
    assert result["gate"].experiment_authorized is False


def test_two_domain_experts_do_not_replace_the_research_librarian(fixtures) -> None:
    result = build_f8s5_direction_fixture(
        fixtures["strong"],
        roles=("domain_expert", "domain_expert"),
    )

    assert "missing_confirmed_review_role:research_librarian" in (
        result["decision"].assessment.unresolved_blockers
    )
    assert result["gate"].disposition is k.ResearchDirectionDisposition.RESEARCH_MORE


def test_no_reviews_cannot_advance_even_a_bounded_direction(fixtures) -> None:
    result = build_f8s5_direction_fixture(
        fixtures["incremental"],
        roles=(),
        verdicts=(),
    )

    assert "independent_review_floor_not_met" in (result["decision"].assessment.unresolved_blockers)
    assert result["gate"].experiment_authorized is False


def test_candidate_author_cannot_impersonate_an_independent_reviewer(fixtures) -> None:
    result = build_f8s5_direction_fixture(fixtures["incremental"])
    reviews = list(result["reviews"])
    reviews[0] = _revalidate(
        k.CalibratedNoveltyReview,
        reviews[0],
        reviewer_principal_sha256=(result["authorship"].author_principal_sha256s[0]),
    )

    with pytest.raises(ValueError, match="authors cannot review"):
        k.build_reviewed_novelty_decision(
            decision_id="author-review-conflict",
            assessment_id="author-review-conflict-assessment",
            coverage=result["coverage"],
            calibration_receipt_key=RECEIPT_KEY,
            authorship_manifest=result["authorship"],
            evidence_package=result["package"],
            independent_reviews=tuple(reviews),
            generated_at=_time("2025-08-09T00:00:00Z"),
        )


def test_review_cannot_be_rebound_to_another_evidence_package(fixtures) -> None:
    result = build_f8s5_direction_fixture(fixtures["incremental"])
    reviews = list(result["reviews"])
    reviews[0] = _revalidate(
        k.CalibratedNoveltyReview,
        reviews[0],
        evidence_package_sha256=sha("another-evidence-package"),
    )

    with pytest.raises(ValueError, match="bound to another package/time"):
        k.build_reviewed_novelty_decision(
            decision_id="rebound-review",
            assessment_id="rebound-review-assessment",
            coverage=result["coverage"],
            calibration_receipt_key=RECEIPT_KEY,
            authorship_manifest=result["authorship"],
            evidence_package=result["package"],
            independent_reviews=tuple(reviews),
            generated_at=_time("2025-08-09T00:00:00Z"),
        )


def test_caller_cannot_upgrade_derived_classification(fixtures) -> None:
    result = build_f8s5_direction_fixture(fixtures["incremental"])
    package = _revalidate(
        k.NoveltyEvidencePackage,
        result["package"],
        derived_classification=k.NoveltyClassification.NOVEL_METHOD,
    )
    raw = result["decision"].model_dump(mode="python")
    raw["evidence_package"] = package

    with pytest.raises(ValidationError, match="not derived from exact artifacts"):
        k.ReviewedNoveltyDecision.model_validate(raw)


def test_direction_gate_authorization_and_ceiling_cannot_be_forged(fixtures) -> None:
    result = build_f8s5_direction_fixture(fixtures["incremental"])
    raw = result["gate"].model_dump(mode="python")
    raw.update(
        disposition=k.ResearchDirectionDisposition.ADVANCE_STRONG,
        maximum_novelty_claim=k.NoveltyClaimCeiling.MODERATE,
    )

    with pytest.raises(ValidationError, match="not mechanically derived"):
        k.ResearchDirectionGate.model_validate(raw)


def test_research_direction_archive_round_trip_rechecks_calibration_key(fixtures, tmp_path) -> None:
    result = build_f8s5_direction_fixture(fixtures["strong"])
    archive = k.ContentAddressedResponseArchive(tmp_path / "direction-gate-archive")
    committed = k.commit_research_direction_gate(
        archive=archive,
        gate=result["gate"],
    )

    loaded = k.load_research_direction_gate(
        archive=archive,
        ledger=committed.ledger,
        calibration_receipt_key=RECEIPT_KEY,
    )

    assert loaded == result["gate"]
    with pytest.raises(ValueError, match="signature is invalid"):
        k.load_research_direction_gate(
            archive=archive,
            ledger=committed.ledger,
            calibration_receipt_key=bytes.fromhex(sha("wrong-direction-key")),
        )
