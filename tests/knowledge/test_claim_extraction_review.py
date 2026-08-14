from __future__ import annotations

import pytest
from pydantic import ValidationError

import aletheia.knowledge as k
from .f8s3_fixtures import (
    build_executor,
    build_f8s3_fixture,
    build_review,
    sha,
)
from .test_schema_spike import _time


def _revalidate(model_type, model, **updates):
    payload = model.model_dump(mode="python")
    payload.update(updates)
    return model_type.model_validate(payload)


async def _execution():
    fixture = build_f8s3_fixture()
    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s3-review-execution"
    )
    return fixture, execution


@pytest.mark.asyncio
async def test_unresolved_low_confidence_candidate_cannot_enter_graph() -> None:
    _, execution = await _execution()

    with pytest.raises(ValueError, match="exactly cover"):
        k.resolve_claim_extraction(
            execution=execution,
            reviews=(),
            resolution_id="resolution:f8s3:missing-review",
            resolved_at=_time("2025-01-06T00:00:00Z"),
        )


@pytest.mark.asyncio
async def test_human_accept_closes_graph_and_preserves_conflicting_edges() -> None:
    fixture, execution = await _execution()
    low = execution.candidates[-1]
    review = build_review(execution=execution, candidate=low)
    resolution = k.resolve_claim_extraction(
        execution=execution,
        reviews=(review,),
        resolution_id="resolution:f8s3:human-accept",
        resolved_at=_time("2025-01-06T00:00:00Z"),
    )
    graph = k.build_extracted_atomic_claim_graph(
        resolution=resolution,
        candidate_claims=(fixture["candidate_claim"],),
        graph_id="f8s3-human-reviewed-graph",
        frozen_at=_time("2025-01-06T01:00:00Z"),
    )

    assert len(resolution.accepted) == 3
    assert len(graph.claims) == 4
    assert [edge.relation for edge in graph.evidence_edges] == [
        k.ClaimEvidenceRelation.SUPPORTS,
        k.ClaimEvidenceRelation.REFUTES,
        k.ClaimEvidenceRelation.QUALIFIES,
    ]
    assert graph.evidence_edges[-1].reviewer_status is k.EvidenceReviewStatus.HUMAN_VERIFIED
    assert graph.evidence_edges[0].reviewer_status is k.EvidenceReviewStatus.UNREVIEWED
    prior_hashes = {
        claim.claim_sha256 for claim in graph.claims if claim.origin is k.ClaimOrigin.PRIOR_ART
    }
    assert prior_hashes == {edge.claim_sha256 for edge in graph.evidence_edges}
    assert graph.extraction_policy_sha256 == execution.protocol.protocol_sha256


@pytest.mark.asyncio
async def test_rejected_low_confidence_candidate_stays_in_rejection_ledger() -> None:
    fixture, execution = await _execution()
    low = execution.candidates[-1]
    review = build_review(execution=execution, candidate=low, decision="reject")
    resolution = k.resolve_claim_extraction(
        execution=execution,
        reviews=(review,),
        resolution_id="resolution:f8s3:reject",
        resolved_at=_time("2025-01-06T00:00:00Z"),
    )
    graph = k.build_extracted_atomic_claim_graph(
        resolution=resolution,
        candidate_claims=(fixture["candidate_claim"],),
        graph_id="f8s3-rejected-low-confidence-graph",
        frozen_at=_time("2025-01-06T01:00:00Z"),
    )

    assert resolution.rejected_candidate_sha256s == (low.candidate_sha256,)
    assert low.candidate_sha256 not in {
        item.original_candidate_sha256 for item in resolution.accepted
    }
    assert len(graph.claims) == 3
    assert [edge.relation for edge in graph.evidence_edges] == [
        k.ClaimEvidenceRelation.SUPPORTS,
        k.ClaimEvidenceRelation.REFUTES,
    ]


@pytest.mark.asyncio
async def test_review_resolution_is_write_once_content_addressed(tmp_path) -> None:
    _, execution = await _execution()
    low = execution.candidates[-1]
    resolution = k.resolve_claim_extraction(
        execution=execution,
        reviews=(build_review(execution=execution, candidate=low),),
        resolution_id="resolution:f8s3:committed",
        resolved_at=_time("2025-01-06T00:00:00Z"),
    )
    archive = k.ContentAddressedResponseArchive(tmp_path / "resolution-ledger")

    first = k.commit_claim_extraction_resolution(archive=archive, resolution=resolution)
    second = k.commit_claim_extraction_resolution(archive=archive, resolution=resolution)
    assert first.ledger == second.ledger
    assert k.load_claim_extraction_resolution(archive=archive, ledger=first.ledger) == resolution


@pytest.mark.asyncio
async def test_graph_bundle_commits_exact_resolution_view_and_rejects_edge_loss(tmp_path) -> None:
    fixture, execution = await _execution()
    low = execution.candidates[-1]
    resolution = k.resolve_claim_extraction(
        execution=execution,
        reviews=(build_review(execution=execution, candidate=low),),
        resolution_id="resolution:f8s3:graph-bundle",
        resolved_at=_time("2025-01-06T00:00:00Z"),
    )
    graph_bundle = k.build_extracted_atomic_claim_graph_bundle(
        resolution=resolution,
        candidate_claims=(fixture["candidate_claim"],),
        bundle_id="f8s3-extracted-graph-bundle",
        graph_id="f8s3-extracted-graph",
        built_at=_time("2025-01-06T01:00:00Z"),
    )
    archive = k.ContentAddressedResponseArchive(tmp_path / "graph-ledger")
    committed = k.commit_extracted_atomic_claim_graph(archive=archive, bundle=graph_bundle)

    assert (
        k.load_extracted_atomic_claim_graph(archive=archive, ledger=committed.ledger)
        == graph_bundle
    )
    weakened_graph = _revalidate(
        k.AtomicClaimGraph,
        graph_bundle.graph,
        claims=graph_bundle.graph.claims[:-1],
        evidence_edges=graph_bundle.graph.evidence_edges[:-1],
    )
    with pytest.raises(ValidationError, match="differs from the exact reviewed"):
        _revalidate(
            k.ExtractedAtomicClaimGraphBundle,
            graph_bundle,
            graph=weakened_graph,
        )


@pytest.mark.asyncio
async def test_review_revision_is_strict_structured_and_attributable() -> None:
    _, execution = await _execution()
    low = execution.candidates[-1]
    replacement = _revalidate(
        k.StructuredClaimDraft,
        low.draft,
        object="outcome Y after calibration",
        quantitative_effect=_revalidate(
            k.QuantitativeEffect,
            low.draft.quantitative_effect,
            estimate=0.45,
        ),
        claim_confidence=0.99,
        evidence_confidence=0.99,
        quantitative_grounding_confidence=0.99,
    )
    review = build_review(
        execution=execution,
        candidate=low,
        decision="revise",
        replacement_draft=replacement,
    )
    resolution = k.resolve_claim_extraction(
        execution=execution,
        reviews=(review,),
        resolution_id="resolution:f8s3:revise",
        resolved_at=_time("2025-01-06T00:00:00Z"),
    )
    revised = resolution.accepted[-1]

    assert revised.decision is k.ClaimResolutionDecision.REVIEW_REVISE
    assert revised.final_claim.object == "outcome Y after calibration"
    assert revised.final_claim.quantitative_effect is not None
    assert revised.final_claim.quantitative_effect.estimate == 0.45
    assert revised.final_evidence_edge.reviewer_status is k.EvidenceReviewStatus.HUMAN_VERIFIED
    assert revised.final_evidence_edge.claim_sha256 == revised.final_claim.claim_sha256
    assert revised.review_sha256 == review.review_sha256


@pytest.mark.asyncio
async def test_second_model_review_must_use_an_independent_manifest() -> None:
    _, execution = await _execution()
    low = execution.candidates[-1]
    independent = build_review(
        execution=execution,
        candidate=low,
        reviewer_kind="second_model",
    )
    same_extractor = _revalidate(
        k.ClaimCandidateReview,
        independent,
        reviewer_manifest_sha256=low.extractor_manifest_sha256,
    )

    with pytest.raises(ValidationError, match="must be independent"):
        k.resolve_claim_extraction(
            execution=execution,
            reviews=(same_extractor,),
            resolution_id="resolution:f8s3:same-model",
            resolved_at=_time("2025-01-06T00:00:00Z"),
        )


@pytest.mark.asyncio
async def test_review_cannot_switch_evidence_package_or_source_span() -> None:
    _, execution = await _execution()
    low = execution.candidates[-1]
    review = build_review(execution=execution, candidate=low)

    with pytest.raises(ValueError, match="evidence-package identity mismatch"):
        k.resolve_claim_extraction(
            execution=execution,
            reviews=(
                _revalidate(
                    k.ClaimCandidateReview,
                    review,
                    evidence_package_sha256=sha("another-evidence-package"),
                ),
            ),
            resolution_id="resolution:f8s3:wrong-package",
            resolved_at=_time("2025-01-06T00:00:00Z"),
        )

    wrong_span_draft = _revalidate(
        k.StructuredClaimDraft,
        low.draft,
        source_span_sha256=execution.candidates[0].source_span_sha256,
    )
    switched = build_review(
        execution=execution,
        candidate=low,
        decision="revise",
        replacement_draft=wrong_span_draft,
    )
    with pytest.raises(ValidationError, match="cannot change its source span"):
        k.resolve_claim_extraction(
            execution=execution,
            reviews=(switched,),
            resolution_id="resolution:f8s3:wrong-span",
            resolved_at=_time("2025-01-06T00:00:00Z"),
        )


@pytest.mark.asyncio
async def test_directly_forged_resolved_claim_is_rejected() -> None:
    _, execution = await _execution()
    low = execution.candidates[-1]
    review = build_review(execution=execution, candidate=low)
    resolution = k.resolve_claim_extraction(
        execution=execution,
        reviews=(review,),
        resolution_id="resolution:f8s3:valid-before-forgery",
        resolved_at=_time("2025-01-06T00:00:00Z"),
    )
    original = resolution.accepted[-1]
    forged_claim = _revalidate(
        k.AtomicClaim,
        original.final_claim,
        subject="forged subject",
    )
    forged_edge = _revalidate(
        k.ClaimEvidenceEdge,
        original.final_evidence_edge,
        claim_sha256=forged_claim.claim_sha256,
    )
    forged = _revalidate(
        k.ResolvedClaimCandidate,
        original,
        final_claim=forged_claim,
        final_evidence_edge=forged_edge,
    )

    with pytest.raises(ValidationError, match="differs from its accepted review"):
        _revalidate(
            k.ClaimExtractionResolution,
            resolution,
            accepted=(*resolution.accepted[:-1], forged),
        )


@pytest.mark.asyncio
async def test_blocked_execution_and_non_candidate_graph_input_fail_closed() -> None:
    fixture = build_f8s3_fixture()
    span = fixture["spans"]["model"]
    manifest_sha256 = fixture["protocol"].targets[0].extractor_manifest_sha256
    fixture["extractors"][manifest_sha256].errors[span.span_sha256] = RuntimeError(
        "synthetic extractor failure"
    )
    blocked = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s3-blocked-review"
    )
    with pytest.raises(ValueError, match="blocked extraction"):
        k.resolve_claim_extraction(
            execution=blocked,
            reviews=(),
            resolution_id="resolution:f8s3:blocked",
            resolved_at=_time("2025-01-06T00:00:00Z"),
        )

    clean_fixture, clean = await _execution()
    low = clean.candidates[-1]
    resolution = k.resolve_claim_extraction(
        execution=clean,
        reviews=(build_review(execution=clean, candidate=low),),
        resolution_id="resolution:f8s3:graph-origin",
        resolved_at=_time("2025-01-06T00:00:00Z"),
    )
    with pytest.raises(ValueError, match="candidate-origin"):
        k.build_extracted_atomic_claim_graph(
            resolution=resolution,
            candidate_claims=(resolution.accepted[0].final_claim,),
            graph_id="f8s3-wrong-origin-graph",
            frozen_at=_time("2025-01-06T01:00:00Z"),
        )
    assert clean_fixture["candidate_claim"].origin is k.ClaimOrigin.CANDIDATE
