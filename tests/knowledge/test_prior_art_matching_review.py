from __future__ import annotations

import pytest
from pydantic import ValidationError

import aletheia.knowledge as k
from .f8s3_fixtures import sha
from .f8s4_fixtures import (
    build_executor,
    build_f8s4_fixture,
    build_review,
)
from .test_schema_spike import _time


def _revalidate(model_type, model, **updates):
    payload = model.model_dump(mode="python")
    payload.update(updates)
    return model_type.model_validate(payload)


async def _execution():
    fixture = await build_f8s4_fixture()
    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s4-review-execution"
    )
    return fixture, execution


@pytest.mark.asyncio
async def test_unresolved_relation_cannot_enter_final_prior_art_view() -> None:
    _, execution = await _execution()

    with pytest.raises(ValueError, match="exactly cover"):
        k.resolve_prior_art_matching(
            execution=execution,
            reviews=(),
            resolution_id="resolution:f8s4:missing-review",
            resolved_at=_time("2025-01-09T00:00:00Z"),
        )


@pytest.mark.asyncio
async def test_human_accept_preserves_order_and_adds_attributable_review_state() -> None:
    _, execution = await _execution()
    low = execution.relation_candidates[-1]
    review = build_review(execution=execution, candidate=low)
    resolution = k.resolve_prior_art_matching(
        execution=execution,
        reviews=(review,),
        resolution_id="resolution:f8s4:human-accept",
        resolved_at=_time("2025-01-09T00:00:00Z"),
    )

    assert [item.relation.rank for item in resolution.accepted] == [1, 2, 3]
    assert resolution.accepted[-1].decision is k.PriorArtResolutionDecision.REVIEW_ACCEPT
    assert resolution.accepted[-1].relation.reviewer_status is (
        k.EvidenceReviewStatus.HUMAN_VERIFIED
    )
    assert resolution.accepted[-1].relation.reviewer_principal_sha256 == (
        review.reviewer_principal_sha256
    )
    assert all(
        item.relation.reviewer_status is k.EvidenceReviewStatus.UNREVIEWED
        for item in resolution.accepted[:2]
    )


@pytest.mark.asyncio
async def test_reject_keeps_original_candidate_in_rejection_ledger() -> None:
    _, execution = await _execution()
    low = execution.relation_candidates[-1]
    review = build_review(execution=execution, candidate=low, decision="reject")
    resolution = k.resolve_prior_art_matching(
        execution=execution,
        reviews=(review,),
        resolution_id="resolution:f8s4:reject",
        resolved_at=_time("2025-01-09T00:00:00Z"),
    )

    assert resolution.rejected_relation_candidate_sha256s == (low.relation_candidate_sha256,)
    assert len(resolution.accepted) == 2
    assert [item.relation.rank for item in resolution.accepted] == [1, 2]


@pytest.mark.asyncio
async def test_rejecting_middle_relation_reranks_survivors_without_losing_audit_identity() -> None:
    fixture = await build_f8s4_fixture()
    middle_prior = fixture["prior_claims"][1]
    _, confidence, difference_confidence, component = fixture["matcher"].relation_specs[
        middle_prior.claim_sha256
    ]
    fixture["matcher"].relation_specs[middle_prior.claim_sha256] = (
        k.PriorArtRelationType.SUBSUMES,
        confidence,
        difference_confidence,
        component,
    )
    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s4-middle-review"
    )
    middle = execution.relation_candidates[1]
    final = execution.relation_candidates[2]
    reviews = (
        build_review(execution=execution, candidate=middle, decision="reject"),
        build_review(execution=execution, candidate=final),
    )
    resolution = k.resolve_prior_art_matching(
        execution=execution,
        reviews=reviews,
        resolution_id="resolution:f8s4:middle-rejected",
        resolved_at=_time("2025-01-09T00:00:00Z"),
    )

    assert [item.relation.rank for item in resolution.accepted] == [1, 2]
    assert resolution.accepted[-1].original_relation_candidate_sha256 == (
        final.relation_candidate_sha256
    )
    assert final.relation.rank == 3
    assert resolution.accepted[-1].relation.rank == 2
    assert resolution.rejected_relation_candidate_sha256s == (middle.relation_candidate_sha256,)


@pytest.mark.asyncio
async def test_review_revision_is_strict_structured_and_attributable() -> None:
    _, execution = await _execution()
    low = execution.relation_candidates[-1]
    evidence = low.judgment.evidence_span_sha256s
    replacement = _revalidate(
        k.PriorArtJudgmentDraft,
        low.judgment,
        relation=k.PriorArtRelationType.EXTENSION,
        differences=(
            k.ComponentDifference(
                component=k.DifferenceComponent.DATASET,
                candidate_value="candidate dataset",
                prior_value="prior dataset",
                difference="candidate uses a materially distinct dataset",
                evidence_span_sha256s=evidence,
            ),
        ),
        semantic_assessment_sha256=sha("f8s4:revised-semantic-assessment"),
        relation_confidence=0.99,
        difference_confidence=0.98,
    )
    review = build_review(
        execution=execution,
        candidate=low,
        decision="revise",
        replacement_judgment=replacement,
    )
    resolution = k.resolve_prior_art_matching(
        execution=execution,
        reviews=(review,),
        resolution_id="resolution:f8s4:revised",
        resolved_at=_time("2025-01-09T00:00:00Z"),
    )
    revised = resolution.accepted[-1]

    assert revised.decision is k.PriorArtResolutionDecision.REVIEW_REVISE
    assert revised.relation.relation is k.PriorArtRelationType.EXTENSION
    assert revised.relation.differences[0].component is k.DifferenceComponent.DATASET
    assert revised.relation.evidence_span_sha256s == evidence
    assert revised.review_sha256 == review.review_sha256


@pytest.mark.asyncio
async def test_second_model_review_must_be_independent_from_matcher() -> None:
    _, execution = await _execution()
    low = execution.relation_candidates[-1]
    independent = build_review(
        execution=execution,
        candidate=low,
        reviewer_kind="second_model",
    )
    same_matcher = _revalidate(
        k.PriorArtRelationReview,
        independent,
        reviewer_manifest_sha256=execution.protocol.matcher_manifest.manifest_sha256,
    )

    with pytest.raises(ValidationError, match="must be independent"):
        k.resolve_prior_art_matching(
            execution=execution,
            reviews=(same_matcher,),
            resolution_id="resolution:f8s4:same-model",
            resolved_at=_time("2025-01-09T00:00:00Z"),
        )


@pytest.mark.asyncio
async def test_independent_second_model_acceptance_is_recorded_on_relation() -> None:
    _, execution = await _execution()
    low = execution.relation_candidates[-1]
    review = build_review(
        execution=execution,
        candidate=low,
        reviewer_kind="second_model",
    )
    resolution = k.resolve_prior_art_matching(
        execution=execution,
        reviews=(review,),
        resolution_id="resolution:f8s4:second-model",
        resolved_at=_time("2025-01-09T00:00:00Z"),
    )

    assert resolution.accepted[-1].relation.reviewer_status is (
        k.EvidenceReviewStatus.SECOND_MODEL_VERIFIED
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ("pair", "evidence"))
async def test_revision_cannot_change_claim_pair_or_evidence_closure(tamper: str) -> None:
    _, execution = await _execution()
    low = execution.relation_candidates[-1]
    updates = (
        {"prior_claim_sha256": sha("different-prior-claim")}
        if tamper == "pair"
        else {
            "evidence_span_sha256s": (sha("different-evidence-span"),),
            "differences": tuple(
                _revalidate(
                    k.ComponentDifference,
                    difference,
                    evidence_span_sha256s=(sha("different-evidence-span"),),
                )
                for difference in low.judgment.differences
            ),
        }
    )
    replacement = _revalidate(k.PriorArtJudgmentDraft, low.judgment, **updates)
    review = build_review(
        execution=execution,
        candidate=low,
        decision="revise",
        replacement_judgment=replacement,
    )

    with pytest.raises(ValidationError, match="cannot change"):
        k.resolve_prior_art_matching(
            execution=execution,
            reviews=(review,),
            resolution_id=f"resolution:f8s4:changed-{tamper}",
            resolved_at=_time("2025-01-09T00:00:00Z"),
        )


@pytest.mark.asyncio
async def test_review_must_bind_exact_evidence_package() -> None:
    _, execution = await _execution()
    low = execution.relation_candidates[-1]
    review = _revalidate(
        k.PriorArtRelationReview,
        build_review(execution=execution, candidate=low),
        evidence_package_sha256=sha("wrong-evidence-package"),
    )

    with pytest.raises(ValidationError, match="another evidence package"):
        k.resolve_prior_art_matching(
            execution=execution,
            reviews=(review,),
            resolution_id="resolution:f8s4:wrong-package",
            resolved_at=_time("2025-01-09T00:00:00Z"),
        )


@pytest.mark.asyncio
async def test_reviews_preserve_queue_order_when_multiple_items_require_review() -> None:
    fixture = await build_f8s4_fixture()
    first_prior = fixture["prior_claims"][0]
    _, confidence, difference_confidence, component = fixture["matcher"].relation_specs[
        first_prior.claim_sha256
    ]
    fixture["matcher"].relation_specs[first_prior.claim_sha256] = (
        k.PriorArtRelationType.EQUIVALENT,
        confidence,
        difference_confidence,
        component,
    )
    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s4-two-reviews"
    )
    reviews = tuple(
        build_review(execution=execution, candidate=candidate)
        for candidate in execution.relation_candidates
        if candidate.disposition is k.PriorArtRelationDisposition.REVIEW_REQUIRED
    )
    assert len(reviews) == 2

    with pytest.raises(ValidationError, match="preserve review-queue order"):
        k.resolve_prior_art_matching(
            execution=execution,
            reviews=tuple(reversed(reviews)),
            resolution_id="resolution:f8s4:review-order",
            resolved_at=_time("2025-01-09T00:00:00Z"),
        )


@pytest.mark.asyncio
async def test_resolution_commits_and_loads_as_write_once_ledger(tmp_path) -> None:
    _, execution = await _execution()
    low = execution.relation_candidates[-1]
    resolution = k.resolve_prior_art_matching(
        execution=execution,
        reviews=(build_review(execution=execution, candidate=low),),
        resolution_id="resolution:f8s4:committed",
        resolved_at=_time("2025-01-09T00:00:00Z"),
    )
    archive = k.ContentAddressedResponseArchive(tmp_path / "prior-art-resolution-ledger")

    first = k.commit_prior_art_matching_resolution(archive=archive, resolution=resolution)
    second = k.commit_prior_art_matching_resolution(archive=archive, resolution=resolution)
    assert first.ledger == second.ledger
    assert k.load_prior_art_matching_resolution(archive=archive, ledger=first.ledger) == resolution
