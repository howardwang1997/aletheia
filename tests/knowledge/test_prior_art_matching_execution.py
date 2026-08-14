from __future__ import annotations

import asyncio

import pytest

import aletheia.knowledge as k
from .f8s3_fixtures import DOCUMENTS
from .f8s4_fixtures import StepClock, build_executor, build_f8s4_fixture


def _revalidate(model_type, model, **updates):
    payload = model.model_dump(mode="python")
    payload.update(updates)
    return model_type.model_validate(payload)


@pytest.mark.asyncio
async def test_complete_execution_preserves_four_channel_union_and_exact_differences() -> None:
    fixture = await build_f8s4_fixture()
    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s4-complete"
    )

    assert execution.disposition is k.PriorArtMatchingDisposition.PENDING_REVIEW
    assert len(execution.recall_attempts) == 4
    assert [attempt.query.channel for attempt in execution.recall_attempts] == list(
        k.PriorArtRecallChannel
    )
    assert {attempt.outcome for attempt in execution.recall_attempts} == {
        k.RecallAttemptOutcome.SUCCESS
    }
    assert len(execution.recall_candidates) == 3
    by_prior = {item.prior_claim_sha256: item for item in execution.recall_candidates}
    assert [
        by_prior[claim.claim_sha256].channel_scores.observed_count
        for claim in fixture["prior_claims"]
    ] == [4, 3, 2]
    assert execution.rerank_request is not None
    assert execution.rerank_request.recall_candidate_sha256s == tuple(
        item.candidate_sha256 for item in execution.recall_candidates
    )
    assert [item.rerank_score for item in execution.reranked_candidates] == [
        0.95,
        0.90,
        0.40,
    ]
    assert [item.relation.relation for item in execution.relation_candidates] == [
        k.PriorArtRelationType.EXTENSION,
        k.PriorArtRelationType.CONTRADICTION,
        k.PriorArtRelationType.COMBINATION,
    ]
    assert [item.relation.differences[0].component for item in execution.relation_candidates] == [
        k.DifferenceComponent.CONDITION,
        k.DifferenceComponent.RELATION,
        k.DifferenceComponent.METHOD,
    ]
    assert execution.relation_candidates[-1].review_reasons == (
        k.PriorArtReviewReason.LOW_CHANNEL_SUPPORT,
        k.PriorArtReviewReason.LOW_RELATION_CONFIDENCE,
        k.PriorArtReviewReason.LOW_DIFFERENCE_CONFIDENCE,
    )


@pytest.mark.asyncio
async def test_single_channel_hit_remains_in_union_but_cannot_become_relation() -> None:
    fixture = await build_f8s4_fixture()
    prior3 = fixture["prior_claims"][2]
    embedding = next(
        adapter
        for adapter in fixture["adapters"].values()
        if adapter.manifest.channel is k.PriorArtRecallChannel.EMBEDDING
    )
    embedding.hits = tuple(hit for hit in embedding.hits if hit[0] != prior3.claim_sha256)

    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s4-single-channel-audit"
    )
    singleton = next(
        item
        for item in execution.recall_candidates
        if item.prior_claim_sha256 == prior3.claim_sha256
    )
    reranked = next(
        item
        for item in execution.reranked_candidates
        if item.prior_claim_sha256 == prior3.claim_sha256
    )
    assert singleton.channel_scores.observed_count == 1
    assert singleton.disposition is k.RecallSupportDisposition.INSUFFICIENT_CHANNEL_SUPPORT
    assert reranked.selection is k.RerankSelectionDisposition.INSUFFICIENT_CHANNEL_SUPPORT
    assert prior3.claim_sha256 not in {
        item.relation.prior_claim_sha256 for item in execution.relation_candidates
    }


@pytest.mark.asyncio
async def test_relation_budget_never_deletes_below_budget_rerank_audit() -> None:
    fixture = await build_f8s4_fixture()
    protocol = _revalidate(
        k.PriorArtMatchingProtocol,
        fixture["protocol"],
        maximum_relations=2,
    )
    execution = await build_executor(fixture).execute(
        protocol=protocol, execution_id="f8s4-budget-two"
    )

    assert len(execution.reranked_candidates) == len(execution.recall_candidates) == 3
    assert execution.reranked_candidates[-1].selection is (
        k.RerankSelectionDisposition.BELOW_RELATION_BUDGET
    )
    assert len(execution.relation_candidates) == 2


@pytest.mark.asyncio
async def test_harness_not_model_controls_final_rerank_order() -> None:
    fixture = await build_f8s4_fixture()
    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s4-harness-order"
    )

    assert execution.rerank_batch is not None
    assert [item.recall_candidate_sha256 for item in execution.rerank_batch.scores] == [
        item.candidate_sha256 for item in execution.recall_candidates
    ]
    assert [item.prior_claim_sha256 for item in execution.reranked_candidates] == [
        claim.claim_sha256 for claim in fixture["prior_claims"]
    ]
    assert [item.global_rank for item in execution.reranked_candidates] == [1, 2, 3]


@pytest.mark.asyncio
async def test_judgment_receives_exact_prior_evidence_closure() -> None:
    fixture = await build_f8s4_fixture()
    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s4-evidence-closure"
    )
    _, contexts = fixture["matcher"].judgment_calls[0]
    graph_edges = fixture["graph_bundle"].graph.evidence_edges

    for context, judgment in zip(contexts, execution.judgment_batch.judgments, strict=True):
        expected = tuple(
            sorted(
                edge.source_span_sha256
                for edge in graph_edges
                if edge.claim_sha256 == context.prior_claim.claim_sha256
            )
        )
        assert context.prior_evidence_span_sha256s == expected
        assert judgment.evidence_span_sha256s == expected
        assert all(
            set(difference.evidence_span_sha256s).issubset(expected)
            for difference in judgment.differences
        )


@pytest.mark.asyncio
async def test_execution_is_deterministic_under_same_frozen_inputs_and_clock() -> None:
    fixture = await build_f8s4_fixture()
    first = await build_executor(fixture, clock=StepClock()).execute(
        protocol=fixture["protocol"], execution_id="f8s4-deterministic"
    )
    second = await build_executor(fixture, clock=StepClock()).execute(
        protocol=fixture["protocol"], execution_id="f8s4-deterministic"
    )

    assert first == second
    assert first.execution_sha256 == second.execution_sha256


@pytest.mark.asyncio
async def test_execution_commits_and_loads_as_write_once_ledger(tmp_path) -> None:
    fixture = await build_f8s4_fixture()
    archive = k.ContentAddressedResponseArchive(tmp_path / "prior-art-ledger")
    first = await build_executor(fixture, archive=archive).execute_and_commit(
        protocol=fixture["protocol"], execution_id="f8s4-committed"
    )
    second = await build_executor(fixture, archive=archive).execute_and_commit(
        protocol=fixture["protocol"], execution_id="f8s4-committed"
    )

    assert first.ledger == second.ledger
    assert k.load_prior_art_matching(archive=archive, ledger=first.ledger) == first.execution


@pytest.mark.asyncio
async def test_one_recall_failure_still_records_every_channel_then_blocks() -> None:
    fixture = await build_f8s4_fixture()
    lexical = next(
        adapter
        for adapter in fixture["adapters"].values()
        if adapter.manifest.channel is k.PriorArtRecallChannel.LEXICAL
    )
    lexical.error = RuntimeError("secret lexical backend token and failure detail")

    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s4-recall-failure"
    )
    assert execution.disposition is k.PriorArtMatchingDisposition.BLOCKED
    assert len(execution.recall_attempts) == 4
    assert [attempt.outcome for attempt in execution.recall_attempts].count(
        k.RecallAttemptOutcome.ERROR
    ) == 1
    assert execution.failures[0].kind is (k.PriorArtMatchingFailureKind.RECALL_ADAPTER_ERROR)
    assert execution.recall_candidates
    assert execution.rerank_request is None
    assert "secret lexical backend" not in execution.model_dump_json()


@pytest.mark.asyncio
async def test_empty_union_and_singleton_only_union_fail_with_distinct_reasons() -> None:
    empty_fixture = await build_f8s4_fixture()
    for adapter in empty_fixture["adapters"].values():
        adapter.hits = ()
    empty = await build_executor(empty_fixture).execute(
        protocol=empty_fixture["protocol"], execution_id="f8s4-empty-union"
    )
    assert empty.failures[0].kind is k.PriorArtMatchingFailureKind.EMPTY_RECALL_UNION

    singleton_fixture = await build_f8s4_fixture()
    for adapter in singleton_fixture["adapters"].values():
        adapter.hits = ()
    for manifest, prior in zip(
        singleton_fixture["recall_manifests"][:3],
        singleton_fixture["prior_claims"],
        strict=True,
    ):
        singleton_fixture["adapters"][manifest.manifest_sha256].hits = ((prior.claim_sha256, 0.8),)
    singleton = await build_executor(singleton_fixture).execute(
        protocol=singleton_fixture["protocol"],
        execution_id="f8s4-singleton-only-union",
    )
    assert singleton.failures[0].kind is (
        k.PriorArtMatchingFailureKind.INSUFFICIENT_CHANNEL_AGREEMENT
    )
    assert len(singleton.recall_candidates) == 3
    assert all(
        item.disposition is k.RecallSupportDisposition.INSUFFICIENT_CHANNEL_SUPPORT
        for item in singleton.recall_candidates
    )


@pytest.mark.asyncio
async def test_rerank_and_judgment_transport_failures_keep_completed_audit_stages() -> None:
    rerank_fixture = await build_f8s4_fixture()
    rerank_fixture["matcher"].rerank_error = RuntimeError("reranker unavailable")
    rerank_failed = await build_executor(rerank_fixture).execute(
        protocol=rerank_fixture["protocol"], execution_id="f8s4-rerank-error"
    )
    assert rerank_failed.failures[0].kind is k.PriorArtMatchingFailureKind.RERANKER_ERROR
    assert rerank_failed.rerank_request is not None
    assert rerank_failed.rerank_batch is None
    assert not rerank_failed.reranked_candidates

    judgment_fixture = await build_f8s4_fixture()
    judgment_fixture["matcher"].judgment_error = RuntimeError("judge unavailable")
    judgment_failed = await build_executor(judgment_fixture).execute(
        protocol=judgment_fixture["protocol"], execution_id="f8s4-judgment-error"
    )
    assert judgment_failed.failures[0].kind is k.PriorArtMatchingFailureKind.JUDGMENT_ERROR
    assert judgment_failed.rerank_batch is not None
    assert len(judgment_failed.reranked_candidates) == 3
    assert judgment_failed.judgment_request is not None
    assert judgment_failed.judgment_batch is None


@pytest.mark.asyncio
async def test_cancellation_propagates_instead_of_becoming_scientific_failure() -> None:
    fixture = await build_f8s4_fixture()
    first_adapter = next(iter(fixture["adapters"].values()))
    first_adapter.error = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await build_executor(fixture).execute(
            protocol=fixture["protocol"], execution_id="f8s4-cancelled"
        )


@pytest.mark.asyncio
async def test_persisted_matching_audit_contains_no_whole_source_document() -> None:
    fixture = await build_f8s4_fixture()
    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s4-hash-only-audit"
    )
    serialized = execution.model_dump_json()

    assert all(text not in serialized for _, text in DOCUMENTS)
    assert "document_bytes" not in serialized
    assert "exact_span_bytes" not in serialized
