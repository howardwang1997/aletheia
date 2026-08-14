from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

import aletheia.knowledge as k
from .f8s3_fixtures import (
    build_executor as build_claim_executor,
    build_f8s3_fixture,
    build_review as build_claim_review,
    sha,
)
from .test_schema_spike import _time


class StepClock:
    def __init__(self) -> None:
        self.current = _time("2025-01-07T00:00:00Z")

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(milliseconds=100)
        return value


class SyntheticRecallAdapter:
    def __init__(
        self,
        manifest: k.RecallChannelManifest,
        hits: tuple[tuple[str, float], ...],
    ) -> None:
        self._manifest = manifest
        self.hits = hits
        self.error: Exception | None = None
        self.raw_override: object | Callable[[k.PriorArtRecallQuery], object] | None = None
        self.calls: list[k.PriorArtRecallQuery] = []

    @property
    def manifest(self) -> k.RecallChannelManifest:
        return self._manifest

    async def retrieve(
        self,
        *,
        query: k.PriorArtRecallQuery,
        candidate_claim: k.AtomicClaim,
        prior_claims: tuple[k.AtomicClaim, ...],
    ) -> object:
        del candidate_claim, prior_claims
        self.calls.append(query)
        if self.error is not None:
            raise self.error
        if self.raw_override is not None:
            return self.raw_override(query) if callable(self.raw_override) else self.raw_override
        return {
            "query_sha256": query.query_sha256,
            "channel": query.channel,
            "hits": tuple(
                {
                    "prior_claim_sha256": prior_sha256,
                    "channel": query.channel,
                    "rank": rank,
                    "score": score,
                    "score_evidence_sha256": sha(
                        f"f8s4:{query.channel.value}:{prior_sha256}:{score}"
                    ),
                }
                for rank, (prior_sha256, score) in enumerate(self.hits, start=1)
            ),
            "examined_prior_claims": query.prior_claim_count,
            "exhaustive": True,
            "truncated": False,
        }


class SyntheticPriorArtMatcher:
    def __init__(
        self,
        manifest: k.PriorArtMatcherManifest,
        rerank_scores: dict[str, float],
        relation_specs: dict[
            str,
            tuple[
                k.PriorArtRelationType,
                float,
                float | None,
                k.DifferenceComponent | None,
            ],
        ],
    ) -> None:
        self._manifest = manifest
        self.rerank_scores = dict(rerank_scores)
        self.relation_specs = dict(relation_specs)
        self.rerank_error: Exception | None = None
        self.judgment_error: Exception | None = None
        self.rerank_override: object | Callable[..., object] | None = None
        self.judgment_override: object | Callable[..., object] | None = None
        self.rerank_calls: list[
            tuple[k.PriorArtRerankRequest, tuple[k.PriorArtRecallCandidate, ...]]
        ] = []
        self.judgment_calls: list[
            tuple[k.PriorArtJudgmentRequest, tuple[k.PriorArtJudgmentContext, ...]]
        ] = []

    @property
    def manifest(self) -> k.PriorArtMatcherManifest:
        return self._manifest

    async def rerank(
        self,
        *,
        request: k.PriorArtRerankRequest,
        candidates: tuple[k.PriorArtRecallCandidate, ...],
        claims: dict[str, k.AtomicClaim],
    ) -> object:
        del claims
        self.rerank_calls.append((request, candidates))
        if self.rerank_error is not None:
            raise self.rerank_error
        if self.rerank_override is not None:
            return (
                self.rerank_override(request, candidates)
                if callable(self.rerank_override)
                else self.rerank_override
            )
        return {
            "request_sha256": request.request_sha256,
            "scores": tuple(
                {
                    "recall_candidate_sha256": candidate.candidate_sha256,
                    "candidate_claim_sha256": candidate.candidate_claim_sha256,
                    "prior_claim_sha256": candidate.prior_claim_sha256,
                    "score": self.rerank_scores[candidate.prior_claim_sha256],
                    "score_evidence_sha256": sha(f"f8s4:rerank:{candidate.candidate_sha256}"),
                }
                for candidate in candidates
            ),
        }

    def judgment_for(
        self,
        *,
        context: k.PriorArtJudgmentContext,
        relation: k.PriorArtRelationType | None = None,
    ) -> dict[str, Any]:
        configured = self.relation_specs[context.prior_claim.claim_sha256]
        relation = relation or configured[0]
        relation_confidence = configured[1]
        difference_confidence = configured[2]
        component = configured[3]
        evidence = context.prior_evidence_span_sha256s
        if relation is k.PriorArtRelationType.EQUIVALENT:
            differences: tuple[dict[str, Any], ...] = ()
            difference_confidence = None
        else:
            component = component or k.DifferenceComponent.METHOD
            differences = (
                {
                    "component": component,
                    "candidate_value": f"candidate {component.value}",
                    "prior_value": f"prior {component.value}",
                    "difference": f"material {component.value} distinction",
                    "evidence_span_sha256s": evidence,
                },
            )
            difference_confidence = 0.96 if difference_confidence is None else difference_confidence
        return {
            "candidate_claim_sha256": context.candidate_claim.claim_sha256,
            "prior_claim_sha256": context.prior_claim.claim_sha256,
            "relation": relation,
            "differences": differences,
            "evidence_span_sha256s": evidence,
            "semantic_assessment_sha256": sha(
                f"f8s4:semantic:{context.prior_claim.claim_sha256}:{relation.value}"
            ),
            "relation_confidence": relation_confidence,
            "difference_confidence": difference_confidence,
        }

    async def judge(
        self,
        *,
        request: k.PriorArtJudgmentRequest,
        contexts: tuple[k.PriorArtJudgmentContext, ...],
    ) -> object:
        self.judgment_calls.append((request, contexts))
        if self.judgment_error is not None:
            raise self.judgment_error
        if self.judgment_override is not None:
            return (
                self.judgment_override(request, contexts)
                if callable(self.judgment_override)
                else self.judgment_override
            )
        return {
            "request_sha256": request.request_sha256,
            "judgments": tuple(self.judgment_for(context=context) for context in contexts),
        }


async def build_f8s4_fixture() -> dict[str, Any]:
    claim_fixture = build_f8s3_fixture()
    claim_execution = await build_claim_executor(claim_fixture).execute(
        protocol=claim_fixture["protocol"],
        execution_id="f8s4-upstream-claim-extraction",
    )
    low_claim = claim_execution.candidates[-1]
    claim_resolution = k.resolve_claim_extraction(
        execution=claim_execution,
        reviews=(build_claim_review(execution=claim_execution, candidate=low_claim),),
        resolution_id="resolution:f8s4:upstream-claims",
        resolved_at=_time("2025-01-05T00:00:00Z"),
    )
    graph_bundle = k.build_extracted_atomic_claim_graph_bundle(
        resolution=claim_resolution,
        candidate_claims=(claim_fixture["candidate_claim"],),
        bundle_id="f8s4-upstream-claim-graph-bundle",
        graph_id="f8s4-upstream-claim-graph",
        built_at=_time("2025-01-06T00:00:00Z"),
    )
    candidate = graph_bundle.graph.claims[0]
    prior_claims = graph_bundle.graph.claims[1:]
    prior1, prior2, prior3 = prior_claims

    recall_manifests = tuple(
        k.RecallChannelManifest(
            manifest_id=f"f8s4-{channel.value}-recall-v1",
            channel=channel,
            adapter_code_sha256=sha(f"f8s4:{channel.value}:adapter"),
            scorer_sha256=sha(f"f8s4:{channel.value}:scorer"),
            index_snapshot_sha256=sha(f"f8s4:{channel.value}:index-snapshot"),
            index_schema_sha256=sha(f"f8s4:{channel.value}:index-schema"),
            model_identity_sha256=(
                sha("f8s4:embedding:model")
                if channel is k.PriorArtRecallChannel.EMBEDDING
                else None
            ),
            maximum_results_per_claim=3,
            frozen_at=_time("2025-01-06T01:00:00Z"),
        )
        for channel in k.PriorArtRecallChannel
    )
    matcher_manifest = k.PriorArtMatcherManifest(
        manifest_id="f8s4-prior-art-matcher-v1",
        reranker_code_sha256=sha("f8s4:reranker-code"),
        reranker_model_sha256=sha("f8s4:reranker-model"),
        reranker_parser_sha256=sha("f8s4:reranker-parser"),
        judgment_code_sha256=sha("f8s4:judgment-code"),
        judgment_model_sha256=sha("f8s4:judgment-model"),
        judgment_instruction_sha256=sha("f8s4:judgment-instruction"),
        judgment_parser_sha256=sha("f8s4:judgment-parser"),
        judgment_schema_sha256=k.PRIOR_ART_JUDGMENT_SCHEMA_SHA256,
        frozen_at=_time("2025-01-06T01:00:00Z"),
    )
    protocol = k.build_prior_art_matching_protocol(
        protocol_id="f8s4-prior-art-matching-protocol-v1",
        graph_bundle=graph_bundle,
        recall_manifests=recall_manifests,
        matcher_manifest=matcher_manifest,
        maximum_relations=3,
        minimum_auto_accept_channels=3,
        minimum_auto_relation_confidence=0.90,
        minimum_auto_difference_confidence=0.90,
        frozen_at=_time("2025-01-06T02:00:00Z"),
    )
    configured_hits = {
        k.PriorArtRecallChannel.LEXICAL: (
            (prior1.claim_sha256, 0.90),
            (prior2.claim_sha256, 0.75),
        ),
        k.PriorArtRecallChannel.EMBEDDING: (
            (prior1.claim_sha256, 0.85),
            (prior3.claim_sha256, 0.80),
        ),
        k.PriorArtRecallChannel.CITATION: (
            (prior2.claim_sha256, 0.88),
            (prior1.claim_sha256, 0.60),
        ),
        k.PriorArtRecallChannel.ENTITY: (
            (prior1.claim_sha256, 0.95),
            (prior2.claim_sha256, 0.82),
            (prior3.claim_sha256, 0.70),
        ),
    }
    adapters = {
        manifest.manifest_sha256: SyntheticRecallAdapter(
            manifest,
            configured_hits[manifest.channel],
        )
        for manifest in recall_manifests
    }
    matcher = SyntheticPriorArtMatcher(
        matcher_manifest,
        rerank_scores={
            prior1.claim_sha256: 0.95,
            prior2.claim_sha256: 0.90,
            prior3.claim_sha256: 0.40,
        },
        relation_specs={
            prior1.claim_sha256: (
                k.PriorArtRelationType.EXTENSION,
                0.97,
                0.96,
                k.DifferenceComponent.CONDITION,
            ),
            prior2.claim_sha256: (
                k.PriorArtRelationType.CONTRADICTION,
                0.96,
                0.95,
                k.DifferenceComponent.RELATION,
            ),
            prior3.claim_sha256: (
                k.PriorArtRelationType.COMBINATION,
                0.70,
                0.70,
                k.DifferenceComponent.METHOD,
            ),
        },
    )
    return {
        "claim_fixture": claim_fixture,
        "claim_execution": claim_execution,
        "claim_resolution": claim_resolution,
        "graph_bundle": graph_bundle,
        "candidate": candidate,
        "prior_claims": prior_claims,
        "recall_manifests": recall_manifests,
        "matcher_manifest": matcher_manifest,
        "protocol": protocol,
        "configured_hits": configured_hits,
        "adapters": adapters,
        "matcher": matcher,
    }


def build_executor(
    fixture: dict[str, Any], *, archive=None, clock=None
) -> k.PriorArtMatchingExecutor:
    return k.PriorArtMatchingExecutor(
        graph_bundle=fixture["graph_bundle"],
        recall_adapters=fixture["adapters"],
        matcher=fixture["matcher"],
        archive=archive,
        clock=clock or StepClock(),
    )


def build_review(
    *,
    execution: k.PriorArtMatchingExecution,
    candidate: k.PriorArtRelationCandidate,
    decision: str = "accept",
    reviewer_kind: str = "human",
    replacement_judgment: k.PriorArtJudgmentDraft | None = None,
) -> k.PriorArtRelationReview:
    task = next(
        task
        for task in execution.review_queue.tasks
        if task.relation_candidate_sha256 == candidate.relation_candidate_sha256
    )
    return k.PriorArtRelationReview(
        review_id=f"review:f8s4:{candidate.selected_rank}",
        relation_candidate_sha256=candidate.relation_candidate_sha256,
        evidence_package_sha256=task.evidence_package_sha256,
        reviewer_principal_sha256=sha(f"f8s4:reviewer:{candidate.selected_rank}"),
        reviewer_kind=reviewer_kind,
        reviewer_manifest_sha256=(
            sha("f8s4:independent-reviewer-manifest") if reviewer_kind == "second_model" else None
        ),
        decision=decision,
        replacement_judgment=replacement_judgment,
        rationale_sha256=sha(f"f8s4:review-rationale:{candidate.selected_rank}:{decision}"),
        reviewed_at=_time("2025-01-08T00:00:00Z"),
    )
