from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

import aletheia.knowledge as k
from .f8s3_fixtures import sha
from .f8s4_fixtures import build_executor, build_f8s4_fixture


def _revalidate(model_type, model, **updates):
    payload = model.model_dump(mode="python")
    payload.update(updates)
    return model_type.model_validate(payload)


def _recall_adapter(fixture, channel: k.PriorArtRecallChannel):
    return next(
        adapter for adapter in fixture["adapters"].values() if adapter.manifest.channel is channel
    )


def _recall_result(
    query: k.PriorArtRecallQuery,
    hits: tuple[dict[str, Any], ...],
    **updates,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "query_sha256": query.query_sha256,
        "channel": query.channel,
        "hits": hits,
        "examined_prior_claims": query.prior_claim_count,
        "exhaustive": True,
        "truncated": False,
    }
    payload.update(updates)
    return payload


@pytest.mark.asyncio
async def test_runtime_manifest_drift_is_rejected_before_any_recall() -> None:
    fixture = await build_f8s4_fixture()
    adapter = next(iter(fixture["adapters"].values()))
    adapter._manifest = _revalidate(
        k.RecallChannelManifest,
        adapter.manifest,
        adapter_code_sha256=sha("drifted-recall-adapter"),
    )

    with pytest.raises(ValueError, match="runtime recall adapter manifest differs"):
        await build_executor(fixture).execute(
            protocol=fixture["protocol"], execution_id="f8s4-manifest-drift"
        )
    assert all(not current.calls for current in fixture["adapters"].values())
    assert not fixture["matcher"].rerank_calls


@pytest.mark.asyncio
async def test_recall_cannot_invent_prior_claim_outside_frozen_pool() -> None:
    fixture = await build_f8s4_fixture()
    lexical = _recall_adapter(fixture, k.PriorArtRecallChannel.LEXICAL)

    def invented(query):
        return _recall_result(
            query,
            (
                {
                    "prior_claim_sha256": sha("invented-prior-claim"),
                    "channel": query.channel,
                    "rank": 1,
                    "score": 0.99,
                    "score_evidence_sha256": sha("invented-score"),
                },
            ),
        )

    lexical.raw_override = invented
    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s4-invented-prior"
    )

    assert len(execution.recall_attempts) == 4
    assert execution.failures[0].kind is (k.PriorArtMatchingFailureKind.RECALL_BINDING_ERROR)
    assert execution.disposition is k.PriorArtMatchingDisposition.BLOCKED


@pytest.mark.asyncio
async def test_recall_output_cannot_smuggle_tool_authority() -> None:
    fixture = await build_f8s4_fixture()
    lexical = _recall_adapter(fixture, k.PriorArtRecallChannel.LEXICAL)

    def elevated(query):
        return _recall_result(query, (), tool_authority="elevated")

    lexical.raw_override = elevated
    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s4-recall-tool-smuggling"
    )

    assert execution.failures[0].kind is (k.PriorArtMatchingFailureKind.RECALL_SCHEMA_ERROR)
    assert "elevated" not in execution.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("attack", ("delete", "reorder", "switch_pair"))
async def test_reranker_cannot_delete_reorder_or_switch_union_candidates(
    attack: str,
) -> None:
    fixture = await build_f8s4_fixture()
    matcher = fixture["matcher"]

    def malicious(request, candidates):
        scores = [
            {
                "recall_candidate_sha256": candidate.candidate_sha256,
                "candidate_claim_sha256": candidate.candidate_claim_sha256,
                "prior_claim_sha256": candidate.prior_claim_sha256,
                "score": matcher.rerank_scores[candidate.prior_claim_sha256],
                "score_evidence_sha256": sha(f"f8s4:malicious-rerank:{candidate.candidate_sha256}"),
            }
            for candidate in candidates
        ]
        if attack == "delete":
            scores.pop()
        elif attack == "reorder":
            scores.reverse()
        else:
            scores[0]["prior_claim_sha256"] = candidates[1].prior_claim_sha256
        return {
            "request_sha256": request.request_sha256,
            "scores": tuple(scores),
        }

    matcher.rerank_override = malicious
    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id=f"f8s4-rerank-{attack}"
    )

    assert execution.failures[0].kind is (k.PriorArtMatchingFailureKind.RERANK_BINDING_ERROR)
    assert len(execution.recall_candidates) == 3
    assert execution.rerank_request is not None
    assert execution.rerank_batch is None
    assert not execution.relation_candidates


@pytest.mark.asyncio
@pytest.mark.parametrize("attack", ("delete", "reorder", "switch_pair"))
async def test_judge_cannot_delete_reorder_or_switch_selected_pairs(attack: str) -> None:
    fixture = await build_f8s4_fixture()
    matcher = fixture["matcher"]

    def malicious(request, contexts):
        judgments = [matcher.judgment_for(context=context) for context in contexts]
        if attack == "delete":
            judgments.pop()
        elif attack == "reorder":
            judgments.reverse()
        else:
            judgments[0]["prior_claim_sha256"] = sha("switched-prior-claim")
        return {
            "request_sha256": request.request_sha256,
            "judgments": tuple(judgments),
        }

    matcher.judgment_override = malicious
    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id=f"f8s4-judgment-{attack}"
    )

    assert execution.failures[0].kind is (k.PriorArtMatchingFailureKind.JUDGMENT_BINDING_ERROR)
    assert execution.rerank_batch is not None
    assert execution.judgment_request is not None
    assert execution.judgment_batch is None
    assert not execution.relation_candidates


@pytest.mark.asyncio
async def test_judge_cannot_replace_exact_source_span_evidence_closure() -> None:
    fixture = await build_f8s4_fixture()
    matcher = fixture["matcher"]

    def substituted(request, contexts):
        judgments = [matcher.judgment_for(context=context) for context in contexts]
        fake_span = sha("substituted-source-span")
        judgments[0]["evidence_span_sha256s"] = (fake_span,)
        judgments[0]["differences"] = tuple(
            {
                **difference,
                "evidence_span_sha256s": (fake_span,),
            }
            for difference in judgments[0]["differences"]
        )
        return {
            "request_sha256": request.request_sha256,
            "judgments": tuple(judgments),
        }

    matcher.judgment_override = substituted
    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s4-substituted-evidence"
    )

    assert execution.failures[0].kind is (k.PriorArtMatchingFailureKind.JUDGMENT_EVIDENCE_ERROR)


@pytest.mark.asyncio
async def test_judgment_output_cannot_smuggle_tool_authority() -> None:
    fixture = await build_f8s4_fixture()
    matcher = fixture["matcher"]

    def elevated(request, contexts):
        judgments = [matcher.judgment_for(context=context) for context in contexts]
        judgments[0]["tool_authority"] = "elevated"
        return {
            "request_sha256": request.request_sha256,
            "judgments": tuple(judgments),
        }

    matcher.judgment_override = elevated
    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s4-judgment-tool-smuggling"
    )

    assert execution.failures[0].kind is (k.PriorArtMatchingFailureKind.JUDGMENT_SCHEMA_ERROR)
    assert "elevated" not in execution.model_dump_json()


@pytest.mark.asyncio
async def test_serialized_execution_cannot_bypass_derived_review_thresholds() -> None:
    fixture = await build_f8s4_fixture()
    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s4-review-bypass-source"
    )
    low = execution.relation_candidates[-1]
    bypassed = _revalidate(
        k.PriorArtRelationCandidate,
        low,
        review_reasons=(),
        disposition=k.PriorArtRelationDisposition.AUTO_ACCEPTED,
    )
    queue = k.PriorArtReviewQueue(
        protocol_sha256=execution.protocol.protocol_sha256,
        tasks=(),
    )

    with pytest.raises(ValidationError, match="review requirements are not derived"):
        _revalidate(
            k.PriorArtMatchingExecution,
            execution,
            relation_candidates=(*execution.relation_candidates[:-1], bypassed),
            review_queue=queue,
            disposition=k.PriorArtMatchingDisposition.READY,
        )


@pytest.mark.asyncio
async def test_serialized_execution_cannot_forge_retrieval_signals() -> None:
    fixture = await build_f8s4_fixture()
    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s4-signal-forgery-source"
    )
    first = execution.relation_candidates[0]
    forged_relation = _revalidate(
        k.PriorArtRelation,
        first.relation,
        retrieval_signals=k.RetrievalSignals(lexical=0.01, embedding=0.01),
    )
    forged_candidate = _revalidate(
        k.PriorArtRelationCandidate,
        first,
        relation=forged_relation,
    )

    with pytest.raises(ValidationError, match="changed frozen retrieval"):
        _revalidate(
            k.PriorArtMatchingExecution,
            execution,
            relation_candidates=(forged_candidate, *execution.relation_candidates[1:]),
        )


@pytest.mark.asyncio
async def test_serialized_execution_cannot_delete_a_recall_union_hit() -> None:
    fixture = await build_f8s4_fixture()
    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s4-union-deletion-source"
    )

    with pytest.raises(ValidationError, match="union differs from exact channel results"):
        _revalidate(
            k.PriorArtMatchingExecution,
            execution,
            recall_candidates=execution.recall_candidates[:-1],
        )


@pytest.mark.asyncio
async def test_serialized_execution_cannot_replace_judgment_batch_derivation() -> None:
    fixture = await build_f8s4_fixture()
    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s4-judgment-replacement-source"
    )
    first = execution.relation_candidates[0]
    substituted_judgment = _revalidate(
        k.PriorArtJudgmentDraft,
        first.judgment,
        relation=k.PriorArtRelationType.COMBINATION,
        semantic_assessment_sha256=sha("substituted-semantic-assessment"),
    )
    substituted_relation = _revalidate(
        k.PriorArtRelation,
        first.relation,
        relation=k.PriorArtRelationType.COMBINATION,
    )
    substituted_candidate = _revalidate(
        k.PriorArtRelationCandidate,
        first,
        judgment=substituted_judgment,
        relation=substituted_relation,
    )

    with pytest.raises(ValidationError, match="preserve the exact judgment batch"):
        _revalidate(
            k.PriorArtMatchingExecution,
            execution,
            relation_candidates=(
                substituted_candidate,
                *execution.relation_candidates[1:],
            ),
        )


@pytest.mark.asyncio
async def test_serialized_execution_cannot_forge_review_evidence_package() -> None:
    fixture = await build_f8s4_fixture()
    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s4-review-package-source"
    )
    forged_task = _revalidate(
        k.PriorArtReviewTask,
        execution.review_queue.tasks[0],
        evidence_package_sha256=sha("forged-review-evidence-package"),
    )
    forged_queue = _revalidate(
        k.PriorArtReviewQueue,
        execution.review_queue,
        tasks=(forged_task,),
    )

    with pytest.raises(ValidationError, match="derived evidence package"):
        _revalidate(
            k.PriorArtMatchingExecution,
            execution,
            review_queue=forged_queue,
        )
