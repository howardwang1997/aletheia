"""Auditable multi-channel nearest-prior-art retrieval, reranking, and relation matching."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, model_validator

from aletheia.knowledge.claim_extraction import (
    ExtractedAtomicClaimGraphBundle,
)
from aletheia.knowledge.response_archive import (
    ArchivedKnowledgeLedger,
    ContentAddressedResponseArchive,
)
from aletheia.knowledge.schemas import (
    AtomicClaim,
    ClaimOrigin,
    ComponentDifference,
    DifferenceComponent,
    EvidenceReviewStatus,
    KnowledgeModel,
    PriorArtRelation,
    PriorArtRelationType,
    RetrievalSignals,
)
from aletheia.reproducibility.manifest import canonical_json_bytes, content_sha256


class PriorArtRecallChannel(str, Enum):
    LEXICAL = "lexical"
    EMBEDDING = "embedding"
    CITATION = "citation"
    ENTITY = "entity"


class RecallAttemptOutcome(str, Enum):
    SUCCESS = "success"
    ERROR = "error"


class RecallSupportDisposition(str, Enum):
    MULTICHANNEL_ELIGIBLE = "multichannel_eligible"
    INSUFFICIENT_CHANNEL_SUPPORT = "insufficient_channel_support"


class RerankSelectionDisposition(str, Enum):
    SELECTED_FOR_JUDGMENT = "selected_for_judgment"
    BELOW_RELATION_BUDGET = "below_relation_budget"
    INSUFFICIENT_CHANNEL_SUPPORT = "insufficient_channel_support"


class PriorArtRelationDisposition(str, Enum):
    AUTO_ACCEPTED = "auto_accepted"
    REVIEW_REQUIRED = "review_required"


class PriorArtReviewReason(str, Enum):
    BLOCKING_RELATION = "blocking_relation"
    LOW_CHANNEL_SUPPORT = "low_channel_support"
    LOW_RELATION_CONFIDENCE = "low_relation_confidence"
    LOW_DIFFERENCE_CONFIDENCE = "low_difference_confidence"


_REVIEW_REASON_ORDER = tuple(PriorArtReviewReason)


class PriorArtMatchingStage(str, Enum):
    RECALL = "recall"
    UNION = "union"
    RERANK = "rerank"
    JUDGMENT = "judgment"


class PriorArtMatchingFailureKind(str, Enum):
    RECALL_ADAPTER_ERROR = "recall_adapter_error"
    RECALL_SCHEMA_ERROR = "recall_schema_error"
    RECALL_BINDING_ERROR = "recall_binding_error"
    EMPTY_RECALL_UNION = "empty_recall_union"
    INSUFFICIENT_CHANNEL_AGREEMENT = "insufficient_channel_agreement"
    RERANKER_ERROR = "reranker_error"
    RERANK_SCHEMA_ERROR = "rerank_schema_error"
    RERANK_BINDING_ERROR = "rerank_binding_error"
    JUDGMENT_ERROR = "judgment_error"
    JUDGMENT_SCHEMA_ERROR = "judgment_schema_error"
    JUDGMENT_BINDING_ERROR = "judgment_binding_error"
    JUDGMENT_EVIDENCE_ERROR = "judgment_evidence_error"


class PriorArtMatchingDisposition(str, Enum):
    READY = "ready"
    PENDING_REVIEW = "pending_review"
    BLOCKED = "blocked"


class PriorArtReviewKind(str, Enum):
    HUMAN = "human"
    SECOND_MODEL = "second_model"


class PriorArtReviewDecision(str, Enum):
    ACCEPT = "accept"
    REVISE = "revise"
    REJECT = "reject"


class PriorArtResolutionDecision(str, Enum):
    AUTO_ACCEPT = "auto_accept"
    REVIEW_ACCEPT = "review_accept"
    REVIEW_REVISE = "review_revise"


class RecallChannelScores(KnowledgeModel):
    schema_version: Literal[1] = 1
    lexical: float | None = Field(default=None, ge=0.0, le=1.0)
    embedding: float | None = Field(default=None, ge=0.0, le=1.0)
    citation: float | None = Field(default=None, ge=0.0, le=1.0)
    entity: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _at_least_one_channel_observed(self) -> "RecallChannelScores":
        if self.observed_count < 1:
            raise ValueError("recall candidate requires at least one observed channel")
        return self

    @property
    def observed_count(self) -> int:
        return sum(
            value is not None
            for value in (self.lexical, self.embedding, self.citation, self.entity)
        )

    @property
    def observed_channels(self) -> tuple[PriorArtRecallChannel, ...]:
        return tuple(
            channel for channel in PriorArtRecallChannel if getattr(self, channel.value) is not None
        )


class RecallChannelManifest(KnowledgeModel):
    schema_version: Literal[1] = 1
    manifest_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    channel: PriorArtRecallChannel
    adapter_code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scorer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_identity_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    maximum_results_per_claim: int = Field(ge=1, le=100_000)
    score_range: Literal["closed_unit_interval"] = "closed_unit_interval"
    score_direction: Literal["higher_is_more_relevant"] = "higher_is_more_relevant"
    result_order: Literal["score_desc_prior_sha256_asc"] = "score_desc_prior_sha256_asc"
    tool_names: tuple[str, ...] = ()
    tool_policy: Literal["none"] = "none"
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _manifest_is_channel_explicit(self) -> "RecallChannelManifest":
        if self.tool_names:
            raise ValueError("prior-art recall adapters cannot receive tool authority")
        if self.channel is PriorArtRecallChannel.EMBEDDING and self.model_identity_sha256 is None:
            raise ValueError("embedding recall requires a frozen model identity")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class PriorArtJudgmentDraft(KnowledgeModel):
    schema_version: Literal[1] = 1
    candidate_claim_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prior_claim_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    relation: PriorArtRelationType
    differences: tuple[ComponentDifference, ...] = ()
    evidence_span_sha256s: tuple[str, ...] = Field(min_length=1)
    semantic_assessment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    relation_confidence: float = Field(ge=0.0, le=1.0)
    difference_confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _judgment_has_exact_difference_semantics(self) -> "PriorArtJudgmentDraft":
        equivalent = self.relation is PriorArtRelationType.EQUIVALENT
        if equivalent:
            if self.differences or self.difference_confidence is not None:
                raise ValueError("equivalent prior art cannot declare component differences")
        elif not self.differences or self.difference_confidence is None:
            raise ValueError(
                "non-equivalent prior art requires differences and difference confidence"
            )
        components = [difference.component for difference in self.differences]
        if len(components) != len(set(components)):
            raise ValueError("prior-art judgment difference components must be unique")
        if self.evidence_span_sha256s != tuple(sorted(set(self.evidence_span_sha256s))):
            raise ValueError("prior-art judgment evidence spans must be unique and sorted")
        evidence = set(self.evidence_span_sha256s)
        if any(
            not set(difference.evidence_span_sha256s).issubset(evidence)
            for difference in self.differences
        ):
            raise ValueError("component differences cite evidence outside the judgment")
        return self

    @property
    def judgment_sha256(self) -> str:
        return content_sha256(self)


class PriorArtJudgmentBatch(KnowledgeModel):
    schema_version: Literal[1] = 1
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    judgments: tuple[PriorArtJudgmentDraft, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _judgments_are_unique(self) -> "PriorArtJudgmentBatch":
        pairs = [(item.candidate_claim_sha256, item.prior_claim_sha256) for item in self.judgments]
        if len(pairs) != len(set(pairs)):
            raise ValueError("prior-art judgment batch cannot repeat a claim pair")
        return self

    @property
    def batch_sha256(self) -> str:
        return content_sha256(self)


PRIOR_ART_JUDGMENT_SCHEMA_SHA256 = content_sha256(PriorArtJudgmentBatch.model_json_schema())


class PriorArtMatcherManifest(KnowledgeModel):
    schema_version: Literal[1] = 1
    manifest_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    reranker_code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reranker_model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reranker_parser_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    judgment_code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    judgment_model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    judgment_instruction_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    judgment_parser_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    judgment_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    supported_relations: tuple[PriorArtRelationType, ...] = tuple(PriorArtRelationType)
    supported_difference_components: tuple[DifferenceComponent, ...] = tuple(DifferenceComponent)
    tool_names: tuple[str, ...] = ()
    tool_policy: Literal["none"] = "none"
    rerank_output_policy: Literal["score_every_union_candidate_no_filter"] = (
        "score_every_union_candidate_no_filter"
    )
    judgment_output_policy: Literal["strict_selected_pair_batch"] = "strict_selected_pair_batch"
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _matcher_supports_the_complete_schema(self) -> "PriorArtMatcherManifest":
        if self.judgment_schema_sha256 != PRIOR_ART_JUDGMENT_SCHEMA_SHA256:
            raise ValueError("prior-art matcher is bound to another judgment schema")
        if self.supported_relations != tuple(PriorArtRelationType):
            raise ValueError("prior-art matcher must preserve every relation type")
        if self.supported_difference_components != tuple(DifferenceComponent):
            raise ValueError("prior-art matcher must preserve every difference component")
        if self.tool_names:
            raise ValueError("prior-art matcher cannot receive tool authority")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class PriorArtMatchingTarget(KnowledgeModel):
    schema_version: Literal[1] = 1
    ordinal: int = Field(ge=0, le=100_000)
    candidate_claim_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def target_sha256(self) -> str:
        return content_sha256(self)


class PriorArtMatchingProtocol(KnowledgeModel):
    schema_version: Literal[1] = 1
    protocol_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    claim_graph_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    claim_graph_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    corpus_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    targets: tuple[PriorArtMatchingTarget, ...] = Field(min_length=1)
    prior_claim_sha256s: tuple[str, ...] = Field(min_length=1)
    recall_manifests: tuple[RecallChannelManifest, ...] = Field(min_length=4, max_length=4)
    matcher_manifest: PriorArtMatcherManifest
    required_channels: tuple[PriorArtRecallChannel, ...] = tuple(PriorArtRecallChannel)
    minimum_relation_channels: Literal[2] = 2
    minimum_auto_accept_channels: int = Field(ge=2, le=4)
    maximum_relations: int = Field(ge=1, le=10_000)
    minimum_auto_relation_confidence: float = Field(ge=0.0, le=1.0)
    minimum_auto_difference_confidence: float = Field(ge=0.0, le=1.0)
    blocking_relations_require_review: Literal[True] = True
    recall_union_policy: Literal["retain_every_unique_hit"] = "retain_every_unique_hit"
    rerank_policy: Literal["score_complete_union_then_harness_orders"] = (
        "score_complete_union_then_harness_orders"
    )
    selection_policy: Literal["score_desc_candidate_then_prior_sha256"] = (
        "score_desc_candidate_then_prior_sha256"
    )
    failure_policy: Literal["record_all_recall_then_block_downstream"] = (
        "record_all_recall_then_block_downstream"
    )
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _protocol_is_closed(self) -> "PriorArtMatchingProtocol":
        ordinals = [target.ordinal for target in self.targets]
        candidate_hashes = [target.candidate_claim_sha256 for target in self.targets]
        if ordinals != list(range(len(self.targets))):
            raise ValueError("prior-art matching targets must have contiguous ordinals")
        if len(candidate_hashes) != len(set(candidate_hashes)):
            raise ValueError("prior-art matching candidate claims must be unique")
        if self.prior_claim_sha256s != tuple(sorted(set(self.prior_claim_sha256s))):
            raise ValueError("prior-art claim pool must be unique and sorted")
        if self.required_channels != tuple(PriorArtRecallChannel):
            raise ValueError("prior-art matching requires all four recall channels")
        channels = tuple(manifest.channel for manifest in self.recall_manifests)
        if channels != self.required_channels:
            raise ValueError("recall manifests must follow exact required-channel order")
        hashes = [manifest.manifest_sha256 for manifest in self.recall_manifests]
        if len(hashes) != len(set(hashes)):
            raise ValueError("recall channel manifests must be unique")
        return self

    @property
    def protocol_sha256(self) -> str:
        return content_sha256(self)


class PriorArtRecallQuery(KnowledgeModel):
    schema_version: Literal[1] = 1
    request_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_claim_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    channel: PriorArtRecallChannel
    channel_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prior_pool_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prior_claim_count: int = Field(ge=1, le=10_000_000)
    maximum_results: int = Field(ge=1, le=100_000)
    issued_at: AwareDatetime

    @property
    def query_sha256(self) -> str:
        return content_sha256(self)


class PriorArtRecallHit(KnowledgeModel):
    schema_version: Literal[1] = 1
    prior_claim_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    channel: PriorArtRecallChannel
    rank: int = Field(ge=1, le=100_000)
    score: float = Field(ge=0.0, le=1.0)
    score_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def hit_sha256(self) -> str:
        return content_sha256(self)


class PriorArtRecallResult(KnowledgeModel):
    schema_version: Literal[1] = 1
    query_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    channel: PriorArtRecallChannel
    hits: tuple[PriorArtRecallHit, ...]
    examined_prior_claims: int = Field(ge=1, le=10_000_000)
    exhaustive: bool
    truncated: bool
    cutoff_score: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _result_is_ranked_and_terminal(self) -> "PriorArtRecallResult":
        if self.exhaustive == self.truncated:
            raise ValueError("recall result must be exhaustive or truncated, never both/neither")
        if self.truncated != (self.cutoff_score is not None):
            raise ValueError("truncated recall result requires an exact cutoff score")
        if any(hit.channel is not self.channel for hit in self.hits):
            raise ValueError("recall hit belongs to another channel")
        ranks = [hit.rank for hit in self.hits]
        if ranks != list(range(1, len(self.hits) + 1)):
            raise ValueError("recall hit ranks must be contiguous")
        hashes = [hit.prior_claim_sha256 for hit in self.hits]
        if len(hashes) != len(set(hashes)):
            raise ValueError("one recall result cannot repeat a prior claim")
        expected = sorted(self.hits, key=lambda hit: (-hit.score, hit.prior_claim_sha256))
        if list(self.hits) != expected:
            raise ValueError("recall hits must use canonical score/hash order")
        if self.cutoff_score is not None and self.hits and self.cutoff_score != self.hits[-1].score:
            raise ValueError("recall cutoff score must equal the final retained hit")
        return self

    @property
    def result_sha256(self) -> str:
        return content_sha256(self)


class PriorArtRecallAdapter(Protocol):
    @property
    def manifest(self) -> RecallChannelManifest: ...

    async def retrieve(
        self,
        *,
        query: PriorArtRecallQuery,
        candidate_claim: AtomicClaim,
        prior_claims: tuple[AtomicClaim, ...],
    ) -> object: ...


class PriorArtMatchingFailure(KnowledgeModel):
    schema_version: Literal[1] = 1
    request_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    stage: PriorArtMatchingStage
    kind: PriorArtMatchingFailureKind
    candidate_claim_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    channel: PriorArtRecallChannel | None = None
    error_class: str = Field(min_length=1, max_length=256)
    error_detail_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    occurred_at: AwareDatetime

    @property
    def failure_sha256(self) -> str:
        return content_sha256(self)


class PriorArtRecallAttempt(KnowledgeModel):
    schema_version: Literal[1] = 1
    query: PriorArtRecallQuery
    outcome: RecallAttemptOutcome
    result: PriorArtRecallResult | None = None
    failure_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def _attempt_matches_outcome(self) -> "PriorArtRecallAttempt":
        if self.outcome is RecallAttemptOutcome.SUCCESS:
            if self.result is None or self.failure_sha256 is not None:
                raise ValueError("successful recall attempt requires one result")
            if self.result.query_sha256 != self.query.query_sha256:
                raise ValueError("recall result is bound to another query")
        elif self.result is not None or self.failure_sha256 is None:
            raise ValueError("failed recall attempt requires one failure and no result")
        return self

    @property
    def attempt_sha256(self) -> str:
        return content_sha256(self)


class PriorArtChannelEvidence(KnowledgeModel):
    schema_version: Literal[1] = 1
    channel: PriorArtRecallChannel
    rank: int = Field(ge=1, le=100_000)
    score: float = Field(ge=0.0, le=1.0)
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    hit_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PriorArtRecallCandidate(KnowledgeModel):
    schema_version: Literal[1] = 1
    candidate_claim_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prior_claim_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    channel_evidence: tuple[PriorArtChannelEvidence, ...] = Field(min_length=1)
    channel_scores: RecallChannelScores
    disposition: RecallSupportDisposition

    @model_validator(mode="after")
    def _candidate_preserves_every_channel_hit(self) -> "PriorArtRecallCandidate":
        channels = tuple(item.channel for item in self.channel_evidence)
        expected_channels = tuple(
            channel for channel in PriorArtRecallChannel if channel in set(channels)
        )
        if channels != expected_channels:
            raise ValueError("prior-art channel evidence must be unique and canonical")
        if channels != self.channel_scores.observed_channels:
            raise ValueError("prior-art channel evidence and scores differ")
        if any(
            item.score != getattr(self.channel_scores, item.channel.value)
            for item in self.channel_evidence
        ):
            raise ValueError("prior-art channel evidence changed its score")
        expected = (
            RecallSupportDisposition.MULTICHANNEL_ELIGIBLE
            if len(channels) >= 2
            else RecallSupportDisposition.INSUFFICIENT_CHANNEL_SUPPORT
        )
        if self.disposition is not expected:
            raise ValueError("prior-art recall support disposition is not derived")
        return self

    @property
    def candidate_sha256(self) -> str:
        return content_sha256(self)


class PriorArtRerankRequest(KnowledgeModel):
    schema_version: Literal[1] = 1
    request_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    matcher_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recall_candidate_sha256s: tuple[str, ...] = Field(min_length=1)
    issued_at: AwareDatetime

    @property
    def request_sha256(self) -> str:
        return content_sha256(self)


class PriorArtRerankScore(KnowledgeModel):
    schema_version: Literal[1] = 1
    recall_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_claim_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prior_claim_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    score: float = Field(ge=0.0, le=1.0)
    score_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PriorArtRerankBatch(KnowledgeModel):
    schema_version: Literal[1] = 1
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scores: tuple[PriorArtRerankScore, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _scores_are_unique(self) -> "PriorArtRerankBatch":
        identities = [item.recall_candidate_sha256 for item in self.scores]
        if len(identities) != len(set(identities)):
            raise ValueError("reranker cannot repeat a recall candidate")
        return self

    @property
    def batch_sha256(self) -> str:
        return content_sha256(self)


class RerankedPriorArtCandidate(KnowledgeModel):
    schema_version: Literal[1] = 1
    recall_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_claim_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prior_claim_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rerank_score: float = Field(ge=0.0, le=1.0)
    global_rank: int = Field(ge=1, le=10_000_000)
    selection: RerankSelectionDisposition

    @property
    def reranked_sha256(self) -> str:
        return content_sha256(self)


class PriorArtJudgmentRequest(KnowledgeModel):
    schema_version: Literal[1] = 1
    request_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    matcher_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_reranked_sha256s: tuple[str, ...] = Field(min_length=1)
    issued_at: AwareDatetime

    @property
    def request_sha256(self) -> str:
        return content_sha256(self)


class PriorArtJudgmentContext(KnowledgeModel):
    schema_version: Literal[1] = 1
    reranked_candidate: RerankedPriorArtCandidate
    candidate_claim: AtomicClaim
    prior_claim: AtomicClaim
    prior_evidence_span_sha256s: tuple[str, ...] = Field(min_length=1)
    retrieval_signals: RetrievalSignals

    @model_validator(mode="after")
    def _context_matches_pair(self) -> "PriorArtJudgmentContext":
        pair = self.reranked_candidate
        if (
            self.candidate_claim.claim_sha256 != pair.candidate_claim_sha256
            or self.prior_claim.claim_sha256 != pair.prior_claim_sha256
        ):
            raise ValueError("judgment context claims differ from the reranked pair")
        if self.candidate_claim.origin is not ClaimOrigin.CANDIDATE:
            raise ValueError("judgment context query must be a candidate-origin claim")
        if self.prior_claim.origin is not ClaimOrigin.PRIOR_ART:
            raise ValueError("judgment context result must be a prior-art claim")
        if self.prior_evidence_span_sha256s != tuple(sorted(set(self.prior_evidence_span_sha256s))):
            raise ValueError("judgment context evidence spans must be unique and sorted")
        return self


class PriorArtMatcherAdapter(Protocol):
    @property
    def manifest(self) -> PriorArtMatcherManifest: ...

    async def rerank(
        self,
        *,
        request: PriorArtRerankRequest,
        candidates: tuple[PriorArtRecallCandidate, ...],
        claims: Mapping[str, AtomicClaim],
    ) -> object: ...

    async def judge(
        self,
        *,
        request: PriorArtJudgmentRequest,
        contexts: tuple[PriorArtJudgmentContext, ...],
    ) -> object: ...


class PriorArtRelationCandidate(KnowledgeModel):
    schema_version: Literal[1] = 1
    reranked_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_rank: int = Field(ge=1, le=10_000)
    judgment: PriorArtJudgmentDraft
    relation: PriorArtRelation
    relation_confidence: float = Field(ge=0.0, le=1.0)
    difference_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    disposition: PriorArtRelationDisposition
    review_reasons: tuple[PriorArtReviewReason, ...]

    @model_validator(mode="after")
    def _relation_candidate_is_exact(self) -> "PriorArtRelationCandidate":
        if (
            self.relation.candidate_claim_sha256 != self.judgment.candidate_claim_sha256
            or self.relation.prior_claim_sha256 != self.judgment.prior_claim_sha256
            or self.relation.relation is not self.judgment.relation
            or self.relation.differences != self.judgment.differences
            or self.relation.evidence_span_sha256s != self.judgment.evidence_span_sha256s
        ):
            raise ValueError("prior-art relation candidate changed its judgment")
        if self.relation.rank != self.selected_rank:
            raise ValueError("prior-art relation rank must be harness-derived")
        if self.relation.reviewer_status is not EvidenceReviewStatus.UNREVIEWED:
            raise ValueError("new prior-art relation candidates must begin unreviewed")
        if (
            self.relation_confidence != self.judgment.relation_confidence
            or self.difference_confidence != self.judgment.difference_confidence
        ):
            raise ValueError("prior-art relation candidate changed judgment confidence")
        expected_reasons = tuple(sorted(set(self.review_reasons), key=_REVIEW_REASON_ORDER.index))
        if self.review_reasons != expected_reasons:
            raise ValueError("prior-art review reasons must be unique and canonical")
        expected = (
            PriorArtRelationDisposition.REVIEW_REQUIRED
            if self.review_reasons
            else PriorArtRelationDisposition.AUTO_ACCEPTED
        )
        if self.disposition is not expected:
            raise ValueError("prior-art relation disposition does not match review reasons")
        return self

    @property
    def relation_candidate_sha256(self) -> str:
        return content_sha256(self)


class PriorArtReviewTask(KnowledgeModel):
    schema_version: Literal[1] = 1
    relation_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_claim_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prior_claim_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reasons: tuple[PriorArtReviewReason, ...] = Field(min_length=1)
    evidence_package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    permitted_review_kinds: tuple[PriorArtReviewKind, ...] = tuple(PriorArtReviewKind)

    @model_validator(mode="after")
    def _task_is_canonical(self) -> "PriorArtReviewTask":
        if self.reasons != tuple(sorted(set(self.reasons), key=_REVIEW_REASON_ORDER.index)):
            raise ValueError("prior-art review task reasons must be canonical")
        if self.permitted_review_kinds != tuple(PriorArtReviewKind):
            raise ValueError("prior-art review must allow human or independent second model")
        return self

    @property
    def task_sha256(self) -> str:
        return content_sha256(self)


class PriorArtReviewQueue(KnowledgeModel):
    schema_version: Literal[1] = 1
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tasks: tuple[PriorArtReviewTask, ...]
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _tasks_are_unique(self) -> "PriorArtReviewQueue":
        identities = [task.relation_candidate_sha256 for task in self.tasks]
        if len(identities) != len(set(identities)):
            raise ValueError("prior-art review queue cannot repeat a relation candidate")
        return self

    @property
    def queue_sha256(self) -> str:
        return content_sha256(self)


class PriorArtMatchingExecution(KnowledgeModel):
    schema_version: Literal[1] = 1
    execution_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    protocol: PriorArtMatchingProtocol
    recall_attempts: tuple[PriorArtRecallAttempt, ...] = Field(min_length=4)
    recall_candidates: tuple[PriorArtRecallCandidate, ...]
    rerank_request: PriorArtRerankRequest | None = None
    rerank_batch: PriorArtRerankBatch | None = None
    reranked_candidates: tuple[RerankedPriorArtCandidate, ...]
    judgment_request: PriorArtJudgmentRequest | None = None
    judgment_batch: PriorArtJudgmentBatch | None = None
    relation_candidates: tuple[PriorArtRelationCandidate, ...]
    failures: tuple[PriorArtMatchingFailure, ...]
    review_queue: PriorArtReviewQueue
    disposition: PriorArtMatchingDisposition
    started_at: AwareDatetime
    completed_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _execution_is_a_closed_audit_chain(self) -> "PriorArtMatchingExecution":
        expected_attempts = len(self.protocol.targets) * len(self.protocol.recall_manifests)
        if len(self.recall_attempts) != expected_attempts:
            raise ValueError("every candidate/channel pair requires one recall attempt")
        expected_queries = [
            (target, manifest)
            for target in self.protocol.targets
            for manifest in self.protocol.recall_manifests
        ]
        for (target, manifest), attempt in zip(expected_queries, self.recall_attempts, strict=True):
            query = attempt.query
            if (
                query.protocol_sha256 != self.protocol.protocol_sha256
                or query.target_sha256 != target.target_sha256
                or query.candidate_claim_sha256 != target.candidate_claim_sha256
                or query.channel is not manifest.channel
                or query.channel_manifest_sha256 != manifest.manifest_sha256
                or query.prior_pool_sha256 != _prior_pool_sha256(self.protocol.prior_claim_sha256s)
                or query.prior_claim_count != len(self.protocol.prior_claim_sha256s)
                or query.maximum_results != manifest.maximum_results_per_claim
            ):
                raise ValueError("recall attempts do not preserve protocol target/channel order")
            if attempt.completed_at < query.issued_at:
                raise ValueError("recall attempt completed before its query was issued")
        request_ids = [attempt.query.request_id for attempt in self.recall_attempts]
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("prior-art matching request IDs must be unique")
        failures = {failure.failure_sha256: failure for failure in self.failures}
        if len(failures) != len(self.failures):
            raise ValueError("prior-art matching failures must be unique")
        for attempt in self.recall_attempts:
            if attempt.failure_sha256 is None:
                continue
            failure = failures.get(attempt.failure_sha256)
            if failure is None:
                raise ValueError("recall attempt references an unknown matching failure")
            if (
                failure.request_id != attempt.query.request_id
                or failure.stage is not PriorArtMatchingStage.RECALL
                or failure.candidate_claim_sha256 != attempt.query.candidate_claim_sha256
                or failure.channel is not attempt.query.channel
                or failure.occurred_at != attempt.completed_at
            ):
                raise ValueError("recall attempt failure does not match its exact query")
        referenced_failures = {
            attempt.failure_sha256
            for attempt in self.recall_attempts
            if attempt.failure_sha256 is not None
        }
        recall_failures = {
            failure.failure_sha256
            for failure in self.failures
            if failure.stage is PriorArtMatchingStage.RECALL
        }
        if referenced_failures != recall_failures:
            raise ValueError("recall failures must be referenced by exactly one attempt")

        recall_hashes = [item.candidate_sha256 for item in self.recall_candidates]
        if len(recall_hashes) != len(set(recall_hashes)):
            raise ValueError("prior-art recall candidates must be unique")
        if self.recall_candidates != _derive_recall_candidates(
            protocol=self.protocol,
            attempts=self.recall_attempts,
        ):
            raise ValueError("prior-art recall union differs from exact channel results")

        def validate_rerank_closure() -> None:
            if self.rerank_request is None or self.rerank_batch is None:
                raise ValueError("completed reranking requires its request and batch")
            if self.rerank_batch.request_sha256 != self.rerank_request.request_sha256:
                raise ValueError("prior-art rerank batch is bound to another request")
            if self.rerank_request.recall_candidate_sha256s != tuple(recall_hashes):
                raise ValueError("reranker input must retain the complete recall union")
            if [item.recall_candidate_sha256 for item in self.rerank_batch.scores] != recall_hashes:
                raise ValueError("reranker must score every recall-union candidate in input order")
            recall_by_hash = {item.candidate_sha256: item for item in self.recall_candidates}
            score_by_hash: dict[str, float] = {}
            for score in self.rerank_batch.scores:
                recall = recall_by_hash[score.recall_candidate_sha256]
                if (
                    score.candidate_claim_sha256 != recall.candidate_claim_sha256
                    or score.prior_claim_sha256 != recall.prior_claim_sha256
                ):
                    raise ValueError("reranker changed a recall candidate claim pair")
                score_by_hash[score.recall_candidate_sha256] = score.score
            reranked_hashes = [item.recall_candidate_sha256 for item in self.reranked_candidates]
            if set(reranked_hashes) != set(recall_hashes) or len(reranked_hashes) != len(
                recall_hashes
            ):
                raise ValueError("reranked candidates cannot delete or invent recall candidates")
            if [item.global_rank for item in self.reranked_candidates] != list(
                range(1, len(self.reranked_candidates) + 1)
            ):
                raise ValueError("reranked candidate ranks must be contiguous")
            target_order = {
                target.candidate_claim_sha256: target.ordinal for target in self.protocol.targets
            }
            expected_order = sorted(
                self.recall_candidates,
                key=lambda item: (
                    -score_by_hash[item.candidate_sha256],
                    target_order[item.candidate_claim_sha256],
                    item.prior_claim_sha256,
                ),
            )
            selected_count = 0
            for expected_rank, (recall, reranked) in enumerate(
                zip(expected_order, self.reranked_candidates, strict=True), start=1
            ):
                if (
                    recall.disposition is RecallSupportDisposition.MULTICHANNEL_ELIGIBLE
                    and selected_count < self.protocol.maximum_relations
                ):
                    expected_selection = RerankSelectionDisposition.SELECTED_FOR_JUDGMENT
                    selected_count += 1
                elif recall.disposition is RecallSupportDisposition.MULTICHANNEL_ELIGIBLE:
                    expected_selection = RerankSelectionDisposition.BELOW_RELATION_BUDGET
                else:
                    expected_selection = RerankSelectionDisposition.INSUFFICIENT_CHANNEL_SUPPORT
                if (
                    reranked.recall_candidate_sha256 != recall.candidate_sha256
                    or reranked.candidate_claim_sha256 != recall.candidate_claim_sha256
                    or reranked.prior_claim_sha256 != recall.prior_claim_sha256
                    or reranked.rerank_score != score_by_hash[recall.candidate_sha256]
                    or reranked.global_rank != expected_rank
                    or reranked.selection is not expected_selection
                ):
                    raise ValueError(
                        "reranked candidates differ from deterministic harness ordering"
                    )

        if self.failures:
            if self.judgment_batch is not None or self.relation_candidates:
                raise ValueError("blocked prior-art matching cannot emit relations")
            stages = {failure.stage for failure in self.failures}
            if stages & {PriorArtMatchingStage.RECALL, PriorArtMatchingStage.UNION}:
                if stages - {
                    PriorArtMatchingStage.RECALL,
                    PriorArtMatchingStage.UNION,
                }:
                    raise ValueError("blocked prior-art matching cannot skip failure stages")
                if (
                    any(
                        value is not None
                        for value in (
                            self.rerank_request,
                            self.rerank_batch,
                            self.judgment_request,
                        )
                    )
                    or self.reranked_candidates
                ):
                    raise ValueError("recall or union failure cannot emit reranking state")
            elif stages == {PriorArtMatchingStage.RERANK}:
                if self.rerank_request is None:
                    raise ValueError("rerank failure requires its exact request")
                if (
                    self.rerank_batch is not None
                    or self.reranked_candidates
                    or self.judgment_request is not None
                ):
                    raise ValueError("rerank failure cannot claim downstream completion")
            elif stages == {PriorArtMatchingStage.JUDGMENT}:
                validate_rerank_closure()
                if self.judgment_request is None:
                    raise ValueError("judgment failure requires its exact request")
            else:
                raise ValueError("blocked prior-art matching has an invalid failure stage")
        else:
            if self.judgment_request is None or self.judgment_batch is None:
                raise ValueError("successful prior-art matching requires rerank and judgment")
            validate_rerank_closure()
            if self.judgment_batch.request_sha256 != self.judgment_request.request_sha256:
                raise ValueError("prior-art judgment batch is bound to another request")
            selected = [
                item
                for item in self.reranked_candidates
                if item.selection is RerankSelectionDisposition.SELECTED_FOR_JUDGMENT
            ]
            if self.judgment_request.selected_reranked_sha256s != tuple(
                item.reranked_sha256 for item in selected
            ):
                raise ValueError("judgment request must preserve exact selected rerank order")
            judgment_pairs = [
                (item.candidate_claim_sha256, item.prior_claim_sha256)
                for item in self.judgment_batch.judgments
            ]
            selected_pairs = [
                (item.candidate_claim_sha256, item.prior_claim_sha256) for item in selected
            ]
            if judgment_pairs != selected_pairs:
                raise ValueError("judgments must exactly cover selected reranked pairs")
            if (
                tuple(item.judgment for item in self.relation_candidates)
                != self.judgment_batch.judgments
            ):
                raise ValueError("relation candidates must preserve the exact judgment batch")
            if [item.selected_rank for item in self.relation_candidates] != list(
                range(1, len(self.relation_candidates) + 1)
            ):
                raise ValueError("prior-art relation ranks must be contiguous")
            if [item.reranked_candidate_sha256 for item in self.relation_candidates] != [
                item.reranked_sha256 for item in selected
            ]:
                raise ValueError("relation candidates must exactly preserve selected rerank order")
            recall_by_hash = {item.candidate_sha256: item for item in self.recall_candidates}
            for relation_candidate, reranked in zip(
                self.relation_candidates, selected, strict=True
            ):
                recall = recall_by_hash[reranked.recall_candidate_sha256]
                expected_signals = _retrieval_signals(recall.channel_scores)
                if (
                    relation_candidate.relation.retrieval_signals != expected_signals
                    or relation_candidate.relation.matcher_manifest_sha256
                    != self.protocol.matcher_manifest.manifest_sha256
                ):
                    raise ValueError(
                        "prior-art relation changed frozen retrieval or matcher evidence"
                    )
                expected_reasons = _review_reasons(
                    judgment=relation_candidate.judgment,
                    relation=relation_candidate.relation,
                    channel_count=recall.channel_scores.observed_count,
                    protocol=self.protocol,
                )
                if relation_candidate.review_reasons != expected_reasons:
                    raise ValueError("prior-art review requirements are not derived")

        queued = [
            item
            for item in self.relation_candidates
            if item.disposition is PriorArtRelationDisposition.REVIEW_REQUIRED
        ]
        if [task.relation_candidate_sha256 for task in self.review_queue.tasks] != [
            item.relation_candidate_sha256 for item in queued
        ]:
            raise ValueError("prior-art review queue must cover exact review-required relations")
        for task, candidate in zip(self.review_queue.tasks, queued, strict=True):
            if (
                task.candidate_claim_sha256 != candidate.judgment.candidate_claim_sha256
                or task.prior_claim_sha256 != candidate.judgment.prior_claim_sha256
                or task.reasons != candidate.review_reasons
                or task.evidence_package_sha256
                != _review_package_sha256(
                    protocol=self.protocol,
                    graph_bundle_sha256=self.protocol.claim_graph_bundle_sha256,
                    candidate=candidate,
                )
            ):
                raise ValueError("prior-art review task differs from derived evidence package")
        if self.review_queue.protocol_sha256 != self.protocol.protocol_sha256:
            raise ValueError("prior-art review queue is bound to another protocol")
        expected_disposition = (
            PriorArtMatchingDisposition.BLOCKED
            if self.failures
            else PriorArtMatchingDisposition.PENDING_REVIEW
            if queued
            else PriorArtMatchingDisposition.READY
        )
        if self.disposition is not expected_disposition:
            raise ValueError("prior-art matching disposition is not derived")
        if self.completed_at < self.started_at:
            raise ValueError("prior-art matching completed before it started")
        return self

    @property
    def execution_sha256(self) -> str:
        return content_sha256(self)


class CommittedPriorArtMatching(KnowledgeModel):
    schema_version: Literal[1] = 1
    execution: PriorArtMatchingExecution
    ledger: ArchivedKnowledgeLedger

    @model_validator(mode="after")
    def _ledger_commits_execution(self) -> "CommittedPriorArtMatching":
        payload = canonical_json_bytes(self.execution)
        if self.ledger.object_sha256 != self.execution.execution_sha256:
            raise ValueError("prior-art matching ledger names another execution")
        if self.ledger.ledger_sha256 != hashlib.sha256(payload).hexdigest():
            raise ValueError("prior-art matching ledger hash does not commit its execution")
        if self.ledger.ledger_bytes != len(payload):
            raise ValueError("prior-art matching ledger size does not commit its execution")
        return self


class PriorArtRelationReview(KnowledgeModel):
    schema_version: Literal[1] = 1
    review_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    relation_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_kind: PriorArtReviewKind
    reviewer_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    matcher_excluded: Literal[True] = True
    decision: PriorArtReviewDecision
    replacement_judgment: PriorArtJudgmentDraft | None = None
    rationale_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _review_is_independent_and_explicit(self) -> "PriorArtRelationReview":
        if self.reviewer_kind is PriorArtReviewKind.SECOND_MODEL:
            if self.reviewer_manifest_sha256 is None:
                raise ValueError("second-model prior-art review requires a reviewer manifest")
        elif self.reviewer_manifest_sha256 is not None:
            raise ValueError("human prior-art review cannot impersonate a model manifest")
        if (self.decision is PriorArtReviewDecision.REVISE) != (
            self.replacement_judgment is not None
        ):
            raise ValueError("only revise decisions require a replacement judgment")
        return self

    @property
    def review_sha256(self) -> str:
        return content_sha256(self)


class ResolvedPriorArtRelation(KnowledgeModel):
    schema_version: Literal[1] = 1
    original_relation_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: PriorArtResolutionDecision
    relation: PriorArtRelation
    review_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _resolved_review_state_matches(self) -> "ResolvedPriorArtRelation":
        reviewed = self.decision is not PriorArtResolutionDecision.AUTO_ACCEPT
        if reviewed != (self.review_sha256 is not None):
            raise ValueError("reviewed prior-art resolution requires an exact review identity")
        if reviewed == (self.relation.reviewer_status is EvidenceReviewStatus.UNREVIEWED):
            raise ValueError("resolved prior-art review status differs from its decision")
        return self

    @property
    def resolved_sha256(self) -> str:
        return content_sha256(self)


class PriorArtMatchingResolution(KnowledgeModel):
    schema_version: Literal[1] = 1
    resolution_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    execution: PriorArtMatchingExecution
    reviews: tuple[PriorArtRelationReview, ...]
    accepted: tuple[ResolvedPriorArtRelation, ...]
    rejected_relation_candidate_sha256s: tuple[str, ...]
    resolved_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _resolution_partitions_every_relation(self) -> "PriorArtMatchingResolution":
        if self.execution.failures:
            raise ValueError("blocked prior-art matching cannot be resolved")
        candidates = {
            item.relation_candidate_sha256: item for item in self.execution.relation_candidates
        }
        tasks = {task.relation_candidate_sha256: task for task in self.execution.review_queue.tasks}
        reviews = {review.relation_candidate_sha256: review for review in self.reviews}
        if len(reviews) != len(self.reviews) or set(reviews) != set(tasks):
            raise ValueError("prior-art reviews must exactly resolve the review queue")
        if [review.relation_candidate_sha256 for review in self.reviews] != list(tasks):
            raise ValueError("prior-art reviews must preserve review-queue order")
        if len({review.review_id for review in self.reviews}) != len(self.reviews):
            raise ValueError("prior-art review IDs must be unique")
        for identity, review in reviews.items():
            candidate = candidates[identity]
            task = tasks[identity]
            if review.evidence_package_sha256 != task.evidence_package_sha256:
                raise ValueError("prior-art review is bound to another evidence package")
            if review.reviewed_at < self.execution.completed_at:
                raise ValueError("prior-art review cannot predate matching completion")
            if (
                review.reviewer_kind is PriorArtReviewKind.SECOND_MODEL
                and review.reviewer_manifest_sha256
                == self.execution.protocol.matcher_manifest.manifest_sha256
            ):
                raise ValueError("prior-art matcher and second-model reviewer must be independent")
            if review.replacement_judgment is not None and (
                review.replacement_judgment.candidate_claim_sha256
                != candidate.judgment.candidate_claim_sha256
                or review.replacement_judgment.prior_claim_sha256
                != candidate.judgment.prior_claim_sha256
            ):
                raise ValueError("prior-art review revision cannot change its claim pair")
            allowed_spans = set(candidate.judgment.evidence_span_sha256s)
            if (
                review.replacement_judgment is not None
                and set(review.replacement_judgment.evidence_span_sha256s) != allowed_spans
            ):
                raise ValueError("prior-art review revision cannot change its evidence closure")

        accepted_ids = [item.original_relation_candidate_sha256 for item in self.accepted]
        rejected = list(self.rejected_relation_candidate_sha256s)
        if len(accepted_ids) != len(set(accepted_ids)) or len(rejected) != len(set(rejected)):
            raise ValueError("prior-art resolution cannot repeat accepted or rejected relations")
        if set(accepted_ids) & set(rejected) or set(accepted_ids + rejected) != set(candidates):
            raise ValueError("prior-art resolution must partition every relation candidate")
        candidate_order = [
            item.relation_candidate_sha256 for item in self.execution.relation_candidates
        ]
        if accepted_ids != [
            identity for identity in candidate_order if identity in set(accepted_ids)
        ]:
            raise ValueError("accepted prior-art relations must preserve matcher order")
        if rejected != [identity for identity in candidate_order if identity in set(rejected)]:
            raise ValueError("rejected prior-art relations must preserve matcher order")
        accepted_map = {item.original_relation_candidate_sha256: item for item in self.accepted}
        accepted_rank_by_identity = {
            item.original_relation_candidate_sha256: item.relation.rank for item in self.accepted
        }
        for identity, candidate in candidates.items():
            review = reviews.get(identity)
            resolved = accepted_map.get(identity)
            if review is None:
                rank = accepted_rank_by_identity.get(identity)
                if (
                    candidate.disposition is not PriorArtRelationDisposition.AUTO_ACCEPTED
                    or resolved is None
                    or resolved.decision is not PriorArtResolutionDecision.AUTO_ACCEPT
                    or rank is None
                    or resolved
                    != _resolved_auto_relation(
                        candidate=candidate,
                        rank=rank,
                    )
                ):
                    raise ValueError("auto-accepted prior-art relation changed during resolution")
                continue
            if review.decision is PriorArtReviewDecision.REJECT:
                if identity not in set(rejected):
                    raise ValueError("rejected prior-art review is absent from rejection ledger")
                continue
            if resolved is None or resolved.review_sha256 != review.review_sha256:
                raise ValueError("accepted prior-art review lacks its exact resolution")
            if resolved != _resolved_relation_from_review(
                candidate=candidate,
                review=review,
                rank=resolved.relation.rank,
            ):
                raise ValueError("resolved prior-art relation differs from accepted review")
        ranks = [item.relation.rank for item in self.accepted]
        if ranks != list(range(1, len(self.accepted) + 1)):
            raise ValueError("resolved prior-art relation ranks must be contiguous")
        if self.resolved_at < max(
            (review.reviewed_at for review in self.reviews),
            default=self.execution.completed_at,
        ):
            raise ValueError("prior-art resolution cannot predate its reviews")
        return self

    @property
    def resolution_sha256(self) -> str:
        return content_sha256(self)


class CommittedPriorArtMatchingResolution(KnowledgeModel):
    schema_version: Literal[1] = 1
    resolution: PriorArtMatchingResolution
    ledger: ArchivedKnowledgeLedger

    @model_validator(mode="after")
    def _ledger_commits_resolution(self) -> "CommittedPriorArtMatchingResolution":
        payload = canonical_json_bytes(self.resolution)
        if self.ledger.object_sha256 != self.resolution.resolution_sha256:
            raise ValueError("prior-art resolution ledger names another resolution")
        if self.ledger.ledger_sha256 != hashlib.sha256(payload).hexdigest():
            raise ValueError("prior-art resolution ledger hash does not commit its resolution")
        if self.ledger.ledger_bytes != len(payload):
            raise ValueError("prior-art resolution ledger size does not commit its resolution")
        return self


class _PriorArtBoundaryError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stage: PriorArtMatchingStage,
        kind: PriorArtMatchingFailureKind,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.kind = kind


def _error_class(error: Exception) -> str:
    return f"{type(error).__module__}.{type(error).__qualname__}"[:256]


def _error_detail_sha256(
    error: Exception,
    *,
    stage: PriorArtMatchingStage,
    kind: PriorArtMatchingFailureKind,
) -> str:
    return content_sha256(
        {
            "error_class": _error_class(error),
            "message_sha256": hashlib.sha256(
                str(error).encode("utf-8", errors="replace")
            ).hexdigest(),
            "stage": stage.value,
            "kind": kind.value,
        }
    )


def _prior_pool_sha256(prior_claim_sha256s: tuple[str, ...]) -> str:
    return content_sha256(
        {
            "policy": "f8s4-prior-claim-pool-v1",
            "prior_claim_sha256s": prior_claim_sha256s,
        }
    )


def _derive_recall_candidates(
    *,
    protocol: PriorArtMatchingProtocol,
    attempts: tuple[PriorArtRecallAttempt, ...],
) -> tuple[PriorArtRecallCandidate, ...]:
    accumulated: dict[tuple[str, str], dict[PriorArtRecallChannel, PriorArtChannelEvidence]] = {}
    prior_pool = set(protocol.prior_claim_sha256s)
    for attempt in attempts:
        if attempt.result is None:
            continue
        query = attempt.query
        result = attempt.result
        if (
            result.channel is not query.channel
            or result.examined_prior_claims != query.prior_claim_count
            or len(result.hits) > query.maximum_results
            or (result.truncated and len(result.hits) != query.maximum_results)
        ):
            raise ValueError("recall result differs from its frozen query boundary")
        if any(hit.prior_claim_sha256 not in prior_pool for hit in result.hits):
            raise ValueError("recall result contains a claim outside the prior pool")
        for hit in result.hits:
            key = (query.candidate_claim_sha256, hit.prior_claim_sha256)
            accumulated.setdefault(key, {})[query.channel] = PriorArtChannelEvidence(
                channel=query.channel,
                rank=hit.rank,
                score=hit.score,
                result_sha256=result.result_sha256,
                hit_sha256=hit.hit_sha256,
            )
    target_order = {target.candidate_claim_sha256: target.ordinal for target in protocol.targets}
    candidates: list[PriorArtRecallCandidate] = []
    for (candidate_sha256, prior_sha256), evidence_map in sorted(
        accumulated.items(), key=lambda item: (target_order[item[0][0]], item[0][1])
    ):
        evidence = tuple(
            evidence_map[channel] for channel in PriorArtRecallChannel if channel in evidence_map
        )
        scores = RecallChannelScores(**{item.channel.value: item.score for item in evidence})
        candidates.append(
            PriorArtRecallCandidate(
                candidate_claim_sha256=candidate_sha256,
                prior_claim_sha256=prior_sha256,
                channel_evidence=evidence,
                channel_scores=scores,
                disposition=(
                    RecallSupportDisposition.MULTICHANNEL_ELIGIBLE
                    if len(evidence) >= protocol.minimum_relation_channels
                    else RecallSupportDisposition.INSUFFICIENT_CHANNEL_SUPPORT
                ),
            )
        )
    return tuple(candidates)


def _retrieval_signals(scores: RecallChannelScores) -> RetrievalSignals:
    if scores.observed_count < 2:
        raise ValueError("formal prior-art relation requires at least two recall channels")
    return RetrievalSignals(
        lexical=scores.lexical,
        embedding=scores.embedding,
        citation=scores.citation,
        entity=scores.entity,
    )


def _relation_blocks_novelty(relation: PriorArtRelationType) -> bool:
    return relation in {
        PriorArtRelationType.EQUIVALENT,
        PriorArtRelationType.SUBSUMES,
        PriorArtRelationType.SPECIAL_CASE,
    }


def _relation_from_judgment(
    *,
    judgment: PriorArtJudgmentDraft,
    rank: int,
    retrieval_signals: RetrievalSignals,
    matcher_manifest_sha256: str,
    reviewer_status: EvidenceReviewStatus = EvidenceReviewStatus.UNREVIEWED,
    reviewer_principal_sha256: str | None = None,
    reviewed_at: AwareDatetime | None = None,
) -> PriorArtRelation:
    return PriorArtRelation(
        candidate_claim_sha256=judgment.candidate_claim_sha256,
        prior_claim_sha256=judgment.prior_claim_sha256,
        relation=judgment.relation,
        rank=rank,
        retrieval_signals=retrieval_signals,
        differences=judgment.differences,
        evidence_span_sha256s=judgment.evidence_span_sha256s,
        matcher_manifest_sha256=matcher_manifest_sha256,
        reviewer_status=reviewer_status,
        reviewer_principal_sha256=reviewer_principal_sha256,
        reviewed_at=reviewed_at,
        blocks_strong_novelty=_relation_blocks_novelty(judgment.relation),
    )


def _review_reasons(
    *,
    judgment: PriorArtJudgmentDraft,
    relation: PriorArtRelation,
    channel_count: int,
    protocol: PriorArtMatchingProtocol,
) -> tuple[PriorArtReviewReason, ...]:
    reasons: list[PriorArtReviewReason] = []
    if relation.blocks_strong_novelty:
        reasons.append(PriorArtReviewReason.BLOCKING_RELATION)
    if channel_count < protocol.minimum_auto_accept_channels:
        reasons.append(PriorArtReviewReason.LOW_CHANNEL_SUPPORT)
    if judgment.relation_confidence < protocol.minimum_auto_relation_confidence:
        reasons.append(PriorArtReviewReason.LOW_RELATION_CONFIDENCE)
    if (
        judgment.difference_confidence is not None
        and judgment.difference_confidence < protocol.minimum_auto_difference_confidence
    ):
        reasons.append(PriorArtReviewReason.LOW_DIFFERENCE_CONFIDENCE)
    return tuple(sorted(set(reasons), key=_REVIEW_REASON_ORDER.index))


def _review_package_sha256(
    *,
    protocol: PriorArtMatchingProtocol,
    graph_bundle_sha256: str,
    candidate: PriorArtRelationCandidate,
) -> str:
    return content_sha256(
        {
            "policy": "f8s4-prior-art-review-package-v1",
            "protocol_sha256": protocol.protocol_sha256,
            "claim_graph_bundle_sha256": graph_bundle_sha256,
            "relation_candidate_sha256": candidate.relation_candidate_sha256,
            "prior_art_relation_sha256": candidate.relation.relation_sha256,
            "judgment_sha256": candidate.judgment.judgment_sha256,
        }
    )


def build_prior_art_matching_protocol(
    *,
    protocol_id: str,
    graph_bundle: ExtractedAtomicClaimGraphBundle,
    recall_manifests: tuple[RecallChannelManifest, ...],
    matcher_manifest: PriorArtMatcherManifest,
    maximum_relations: int,
    minimum_auto_accept_channels: int,
    minimum_auto_relation_confidence: float,
    minimum_auto_difference_confidence: float,
    frozen_at: AwareDatetime,
) -> PriorArtMatchingProtocol:
    candidate_claims = tuple(
        claim for claim in graph_bundle.graph.claims if claim.origin is ClaimOrigin.CANDIDATE
    )
    prior_claims = tuple(
        claim for claim in graph_bundle.graph.claims if claim.origin is ClaimOrigin.PRIOR_ART
    )
    return PriorArtMatchingProtocol(
        protocol_id=protocol_id,
        claim_graph_bundle_sha256=graph_bundle.bundle_sha256,
        claim_graph_sha256=graph_bundle.graph.graph_sha256,
        corpus_snapshot_sha256=graph_bundle.graph.corpus_snapshot_sha256,
        targets=tuple(
            PriorArtMatchingTarget(
                ordinal=index,
                candidate_claim_sha256=claim.claim_sha256,
            )
            for index, claim in enumerate(candidate_claims)
        ),
        prior_claim_sha256s=tuple(sorted(claim.claim_sha256 for claim in prior_claims)),
        recall_manifests=recall_manifests,
        matcher_manifest=matcher_manifest,
        minimum_auto_accept_channels=minimum_auto_accept_channels,
        maximum_relations=maximum_relations,
        minimum_auto_relation_confidence=minimum_auto_relation_confidence,
        minimum_auto_difference_confidence=minimum_auto_difference_confidence,
        frozen_at=frozen_at,
    )


class PriorArtMatchingExecutor:
    """Preserve complete recall union; allow reranking to order but never delete it."""

    def __init__(
        self,
        *,
        graph_bundle: ExtractedAtomicClaimGraphBundle,
        recall_adapters: Mapping[str, PriorArtRecallAdapter],
        matcher: PriorArtMatcherAdapter,
        archive: ContentAddressedResponseArchive | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.graph_bundle = graph_bundle
        self.recall_adapters = dict(recall_adapters)
        self.matcher = matcher
        self.archive = archive
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("prior-art matching clock must return timezone-aware datetimes")
        return value

    def _preflight(
        self, protocol: PriorArtMatchingProtocol
    ) -> tuple[
        dict[str, AtomicClaim],
        tuple[AtomicClaim, ...],
        dict[str, int],
        dict[str, tuple[str, ...]],
    ]:
        if protocol.claim_graph_bundle_sha256 != self.graph_bundle.bundle_sha256:
            raise ValueError("prior-art protocol is bound to another extracted graph bundle")
        if protocol.claim_graph_sha256 != self.graph_bundle.graph.graph_sha256:
            raise ValueError("prior-art protocol is bound to another atomic claim graph")
        if protocol.corpus_snapshot_sha256 != self.graph_bundle.graph.corpus_snapshot_sha256:
            raise ValueError("prior-art protocol is bound to another corpus")
        claims = {claim.claim_sha256: claim for claim in self.graph_bundle.graph.claims}
        candidate_hashes = tuple(
            claim.claim_sha256
            for claim in self.graph_bundle.graph.claims
            if claim.origin is ClaimOrigin.CANDIDATE
        )
        if candidate_hashes != tuple(target.candidate_claim_sha256 for target in protocol.targets):
            raise ValueError("prior-art protocol targets differ from graph candidate claims")
        prior_claims = tuple(
            claim
            for claim in self.graph_bundle.graph.claims
            if claim.origin is ClaimOrigin.PRIOR_ART
        )
        if tuple(sorted(claim.claim_sha256 for claim in prior_claims)) != (
            protocol.prior_claim_sha256s
        ):
            raise ValueError("prior-art protocol pool differs from graph prior claims")
        manifests = {manifest.manifest_sha256: manifest for manifest in protocol.recall_manifests}
        if set(self.recall_adapters) != set(manifests):
            raise ValueError("runtime recall adapters must exactly match frozen manifests")
        for identity, manifest in manifests.items():
            if self.recall_adapters[identity].manifest != manifest:
                raise ValueError("runtime recall adapter manifest differs from protocol")
        if self.matcher.manifest != protocol.matcher_manifest:
            raise ValueError("runtime prior-art matcher manifest differs from protocol")
        target_order = {
            target.candidate_claim_sha256: target.ordinal for target in protocol.targets
        }
        evidence_by_prior: dict[str, list[str]] = {claim.claim_sha256: [] for claim in prior_claims}
        for edge in self.graph_bundle.graph.evidence_edges:
            if edge.claim_sha256 in evidence_by_prior:
                evidence_by_prior[edge.claim_sha256].append(edge.source_span_sha256)
        evidence = {
            claim_sha256: tuple(sorted(set(spans)))
            for claim_sha256, spans in evidence_by_prior.items()
        }
        if any(not spans for spans in evidence.values()):
            raise ValueError("every prior claim requires evidence before matching")
        return claims, prior_claims, target_order, evidence

    def _recall_query(
        self,
        *,
        execution_id: str,
        protocol: PriorArtMatchingProtocol,
        target: PriorArtMatchingTarget,
        manifest: RecallChannelManifest,
        issued_at: datetime,
    ) -> PriorArtRecallQuery:
        return PriorArtRecallQuery(
            request_id=(f"{execution_id}:recall:{target.ordinal:05d}:{manifest.channel.value}"),
            protocol_sha256=protocol.protocol_sha256,
            target_sha256=target.target_sha256,
            candidate_claim_sha256=target.candidate_claim_sha256,
            channel=manifest.channel,
            channel_manifest_sha256=manifest.manifest_sha256,
            prior_pool_sha256=_prior_pool_sha256(protocol.prior_claim_sha256s),
            prior_claim_count=len(protocol.prior_claim_sha256s),
            maximum_results=manifest.maximum_results_per_claim,
            issued_at=issued_at,
        )

    def _failure(
        self,
        *,
        request_id: str,
        stage: PriorArtMatchingStage,
        kind: PriorArtMatchingFailureKind,
        error: Exception,
        candidate_claim_sha256: str | None = None,
        channel: PriorArtRecallChannel | None = None,
    ) -> PriorArtMatchingFailure:
        if isinstance(error, _PriorArtBoundaryError):
            stage = error.stage
            kind = error.kind
        return PriorArtMatchingFailure(
            request_id=request_id,
            stage=stage,
            kind=kind,
            candidate_claim_sha256=candidate_claim_sha256,
            channel=channel,
            error_class=_error_class(error),
            error_detail_sha256=_error_detail_sha256(error, stage=stage, kind=kind),
            occurred_at=self._now(),
        )

    def _recall_candidates(
        self,
        *,
        protocol: PriorArtMatchingProtocol,
        attempts: tuple[PriorArtRecallAttempt, ...],
    ) -> tuple[PriorArtRecallCandidate, ...]:
        return _derive_recall_candidates(protocol=protocol, attempts=attempts)

    def _blocked_execution(
        self,
        *,
        execution_id: str,
        protocol: PriorArtMatchingProtocol,
        attempts: list[PriorArtRecallAttempt],
        recall_candidates: tuple[PriorArtRecallCandidate, ...],
        failures: list[PriorArtMatchingFailure],
        started_at: datetime,
        rerank_request: PriorArtRerankRequest | None = None,
        rerank_batch: PriorArtRerankBatch | None = None,
        reranked_candidates: tuple[RerankedPriorArtCandidate, ...] = (),
        judgment_request: PriorArtJudgmentRequest | None = None,
    ) -> PriorArtMatchingExecution:
        return PriorArtMatchingExecution(
            execution_id=execution_id,
            protocol=protocol,
            recall_attempts=tuple(attempts),
            recall_candidates=recall_candidates,
            rerank_request=rerank_request,
            rerank_batch=rerank_batch,
            reranked_candidates=reranked_candidates,
            judgment_request=judgment_request,
            judgment_batch=None,
            relation_candidates=(),
            failures=tuple(failures),
            review_queue=PriorArtReviewQueue(
                protocol_sha256=protocol.protocol_sha256,
                tasks=(),
            ),
            disposition=PriorArtMatchingDisposition.BLOCKED,
            started_at=started_at,
            completed_at=self._now(),
        )

    async def execute(
        self, *, protocol: PriorArtMatchingProtocol, execution_id: str
    ) -> PriorArtMatchingExecution:
        claims, prior_claims, target_order, evidence_by_prior = self._preflight(protocol)
        started_at = self._now()
        attempts: list[PriorArtRecallAttempt] = []
        failures: list[PriorArtMatchingFailure] = []

        for target in protocol.targets:
            candidate_claim = claims[target.candidate_claim_sha256]
            for manifest in protocol.recall_manifests:
                query = self._recall_query(
                    execution_id=execution_id,
                    protocol=protocol,
                    target=target,
                    manifest=manifest,
                    issued_at=self._now(),
                )
                adapter = self.recall_adapters[manifest.manifest_sha256]
                try:
                    raw_result = await adapter.retrieve(
                        query=query,
                        candidate_claim=candidate_claim,
                        prior_claims=prior_claims,
                    )
                except Exception as error:
                    failure = self._failure(
                        request_id=query.request_id,
                        stage=PriorArtMatchingStage.RECALL,
                        kind=PriorArtMatchingFailureKind.RECALL_ADAPTER_ERROR,
                        error=error,
                        candidate_claim_sha256=target.candidate_claim_sha256,
                        channel=manifest.channel,
                    )
                    failures.append(failure)
                    attempts.append(
                        PriorArtRecallAttempt(
                            query=query,
                            outcome=RecallAttemptOutcome.ERROR,
                            failure_sha256=failure.failure_sha256,
                            completed_at=failure.occurred_at,
                        )
                    )
                    continue
                try:
                    result = PriorArtRecallResult.model_validate(raw_result)
                    if (
                        result.query_sha256 != query.query_sha256
                        or result.channel is not query.channel
                    ):
                        raise _PriorArtBoundaryError(
                            "recall result is bound to another query or channel",
                            stage=PriorArtMatchingStage.RECALL,
                            kind=PriorArtMatchingFailureKind.RECALL_BINDING_ERROR,
                        )
                    if result.examined_prior_claims != query.prior_claim_count:
                        raise _PriorArtBoundaryError(
                            "recall result did not examine the frozen prior pool",
                            stage=PriorArtMatchingStage.RECALL,
                            kind=PriorArtMatchingFailureKind.RECALL_BINDING_ERROR,
                        )
                    if len(result.hits) > query.maximum_results or (
                        result.truncated and len(result.hits) != query.maximum_results
                    ):
                        raise _PriorArtBoundaryError(
                            "recall result exceeds or misstates its frozen result limit",
                            stage=PriorArtMatchingStage.RECALL,
                            kind=PriorArtMatchingFailureKind.RECALL_BINDING_ERROR,
                        )
                    unknown = {hit.prior_claim_sha256 for hit in result.hits} - set(
                        protocol.prior_claim_sha256s
                    )
                    if unknown:
                        raise _PriorArtBoundaryError(
                            "recall result invented a claim outside the prior pool",
                            stage=PriorArtMatchingStage.RECALL,
                            kind=PriorArtMatchingFailureKind.RECALL_BINDING_ERROR,
                        )
                except Exception as error:
                    failure = self._failure(
                        request_id=query.request_id,
                        stage=PriorArtMatchingStage.RECALL,
                        kind=PriorArtMatchingFailureKind.RECALL_SCHEMA_ERROR,
                        error=error,
                        candidate_claim_sha256=target.candidate_claim_sha256,
                        channel=manifest.channel,
                    )
                    failures.append(failure)
                    attempts.append(
                        PriorArtRecallAttempt(
                            query=query,
                            outcome=RecallAttemptOutcome.ERROR,
                            failure_sha256=failure.failure_sha256,
                            completed_at=failure.occurred_at,
                        )
                    )
                    continue
                attempts.append(
                    PriorArtRecallAttempt(
                        query=query,
                        outcome=RecallAttemptOutcome.SUCCESS,
                        result=result,
                        completed_at=self._now(),
                    )
                )

        recall_candidates = self._recall_candidates(protocol=protocol, attempts=tuple(attempts))
        if failures:
            return self._blocked_execution(
                execution_id=execution_id,
                protocol=protocol,
                attempts=attempts,
                recall_candidates=recall_candidates,
                failures=failures,
                started_at=started_at,
            )
        if not recall_candidates:
            error = _PriorArtBoundaryError(
                "all four recall channels returned an empty union",
                stage=PriorArtMatchingStage.UNION,
                kind=PriorArtMatchingFailureKind.EMPTY_RECALL_UNION,
            )
            failures.append(
                self._failure(
                    request_id=f"{execution_id}:union",
                    stage=error.stage,
                    kind=error.kind,
                    error=error,
                )
            )
            return self._blocked_execution(
                execution_id=execution_id,
                protocol=protocol,
                attempts=attempts,
                recall_candidates=recall_candidates,
                failures=failures,
                started_at=started_at,
            )
        if not any(
            item.disposition is RecallSupportDisposition.MULTICHANNEL_ELIGIBLE
            for item in recall_candidates
        ):
            error = _PriorArtBoundaryError(
                "no recall-union pair has the required channel agreement",
                stage=PriorArtMatchingStage.UNION,
                kind=PriorArtMatchingFailureKind.INSUFFICIENT_CHANNEL_AGREEMENT,
            )
            failures.append(
                self._failure(
                    request_id=f"{execution_id}:union",
                    stage=error.stage,
                    kind=error.kind,
                    error=error,
                )
            )
            return self._blocked_execution(
                execution_id=execution_id,
                protocol=protocol,
                attempts=attempts,
                recall_candidates=recall_candidates,
                failures=failures,
                started_at=started_at,
            )

        rerank_request = PriorArtRerankRequest(
            request_id=f"{execution_id}:rerank",
            protocol_sha256=protocol.protocol_sha256,
            matcher_manifest_sha256=protocol.matcher_manifest.manifest_sha256,
            recall_candidate_sha256s=tuple(item.candidate_sha256 for item in recall_candidates),
            issued_at=self._now(),
        )
        try:
            raw_rerank = await self.matcher.rerank(
                request=rerank_request,
                candidates=recall_candidates,
                claims=claims,
            )
        except Exception as error:
            failures.append(
                self._failure(
                    request_id=rerank_request.request_id,
                    stage=PriorArtMatchingStage.RERANK,
                    kind=PriorArtMatchingFailureKind.RERANKER_ERROR,
                    error=error,
                )
            )
            return self._blocked_execution(
                execution_id=execution_id,
                protocol=protocol,
                attempts=attempts,
                recall_candidates=recall_candidates,
                failures=failures,
                started_at=started_at,
                rerank_request=rerank_request,
            )
        try:
            rerank_batch = PriorArtRerankBatch.model_validate(raw_rerank)
            if rerank_batch.request_sha256 != rerank_request.request_sha256:
                raise _PriorArtBoundaryError(
                    "rerank output is bound to another request",
                    stage=PriorArtMatchingStage.RERANK,
                    kind=PriorArtMatchingFailureKind.RERANK_BINDING_ERROR,
                )
            expected_hashes = [item.candidate_sha256 for item in recall_candidates]
            if [item.recall_candidate_sha256 for item in rerank_batch.scores] != (expected_hashes):
                raise _PriorArtBoundaryError(
                    "reranker deleted, added, or reordered recall candidates",
                    stage=PriorArtMatchingStage.RERANK,
                    kind=PriorArtMatchingFailureKind.RERANK_BINDING_ERROR,
                )
            for candidate, score in zip(recall_candidates, rerank_batch.scores, strict=True):
                if (
                    score.candidate_claim_sha256 != candidate.candidate_claim_sha256
                    or score.prior_claim_sha256 != candidate.prior_claim_sha256
                ):
                    raise _PriorArtBoundaryError(
                        "rerank output switched a recall candidate claim pair",
                        stage=PriorArtMatchingStage.RERANK,
                        kind=PriorArtMatchingFailureKind.RERANK_BINDING_ERROR,
                    )
        except Exception as error:
            failures.append(
                self._failure(
                    request_id=rerank_request.request_id,
                    stage=PriorArtMatchingStage.RERANK,
                    kind=PriorArtMatchingFailureKind.RERANK_SCHEMA_ERROR,
                    error=error,
                )
            )
            return self._blocked_execution(
                execution_id=execution_id,
                protocol=protocol,
                attempts=attempts,
                recall_candidates=recall_candidates,
                failures=failures,
                started_at=started_at,
                rerank_request=rerank_request,
            )

        score_by_candidate = {
            item.recall_candidate_sha256: item.score for item in rerank_batch.scores
        }
        ordered_recall = sorted(
            recall_candidates,
            key=lambda item: (
                -score_by_candidate[item.candidate_sha256],
                target_order[item.candidate_claim_sha256],
                item.prior_claim_sha256,
            ),
        )
        selected_count = 0
        reranked: list[RerankedPriorArtCandidate] = []
        for global_rank, candidate in enumerate(ordered_recall, start=1):
            if (
                candidate.disposition is RecallSupportDisposition.MULTICHANNEL_ELIGIBLE
                and selected_count < protocol.maximum_relations
            ):
                selection = RerankSelectionDisposition.SELECTED_FOR_JUDGMENT
                selected_count += 1
            elif candidate.disposition is RecallSupportDisposition.MULTICHANNEL_ELIGIBLE:
                selection = RerankSelectionDisposition.BELOW_RELATION_BUDGET
            else:
                selection = RerankSelectionDisposition.INSUFFICIENT_CHANNEL_SUPPORT
            reranked.append(
                RerankedPriorArtCandidate(
                    recall_candidate_sha256=candidate.candidate_sha256,
                    candidate_claim_sha256=candidate.candidate_claim_sha256,
                    prior_claim_sha256=candidate.prior_claim_sha256,
                    rerank_score=score_by_candidate[candidate.candidate_sha256],
                    global_rank=global_rank,
                    selection=selection,
                )
            )
        selected = tuple(
            item
            for item in reranked
            if item.selection is RerankSelectionDisposition.SELECTED_FOR_JUDGMENT
        )
        recall_by_hash = {item.candidate_sha256: item for item in recall_candidates}
        contexts = tuple(
            PriorArtJudgmentContext(
                reranked_candidate=item,
                candidate_claim=claims[item.candidate_claim_sha256],
                prior_claim=claims[item.prior_claim_sha256],
                prior_evidence_span_sha256s=evidence_by_prior[item.prior_claim_sha256],
                retrieval_signals=_retrieval_signals(
                    recall_by_hash[item.recall_candidate_sha256].channel_scores
                ),
            )
            for item in selected
        )
        judgment_request = PriorArtJudgmentRequest(
            request_id=f"{execution_id}:judgment",
            protocol_sha256=protocol.protocol_sha256,
            matcher_manifest_sha256=protocol.matcher_manifest.manifest_sha256,
            selected_reranked_sha256s=tuple(item.reranked_sha256 for item in selected),
            issued_at=self._now(),
        )
        try:
            raw_judgments = await self.matcher.judge(
                request=judgment_request,
                contexts=contexts,
            )
        except Exception as error:
            failures.append(
                self._failure(
                    request_id=judgment_request.request_id,
                    stage=PriorArtMatchingStage.JUDGMENT,
                    kind=PriorArtMatchingFailureKind.JUDGMENT_ERROR,
                    error=error,
                )
            )
            return self._blocked_execution(
                execution_id=execution_id,
                protocol=protocol,
                attempts=attempts,
                recall_candidates=recall_candidates,
                failures=failures,
                started_at=started_at,
                rerank_request=rerank_request,
                rerank_batch=rerank_batch,
                reranked_candidates=tuple(reranked),
                judgment_request=judgment_request,
            )
        try:
            judgment_batch = PriorArtJudgmentBatch.model_validate(raw_judgments)
            if judgment_batch.request_sha256 != judgment_request.request_sha256:
                raise _PriorArtBoundaryError(
                    "judgment output is bound to another request",
                    stage=PriorArtMatchingStage.JUDGMENT,
                    kind=PriorArtMatchingFailureKind.JUDGMENT_BINDING_ERROR,
                )
            expected_pairs = [
                (item.candidate_claim_sha256, item.prior_claim_sha256) for item in selected
            ]
            actual_pairs = [
                (item.candidate_claim_sha256, item.prior_claim_sha256)
                for item in judgment_batch.judgments
            ]
            if actual_pairs != expected_pairs:
                raise _PriorArtBoundaryError(
                    "judgment output deleted, added, or reordered selected pairs",
                    stage=PriorArtMatchingStage.JUDGMENT,
                    kind=PriorArtMatchingFailureKind.JUDGMENT_BINDING_ERROR,
                )
            for judgment in judgment_batch.judgments:
                expected_spans = evidence_by_prior[judgment.prior_claim_sha256]
                if judgment.evidence_span_sha256s != expected_spans:
                    raise _PriorArtBoundaryError(
                        "judgment output changed prior-claim evidence closure",
                        stage=PriorArtMatchingStage.JUDGMENT,
                        kind=PriorArtMatchingFailureKind.JUDGMENT_EVIDENCE_ERROR,
                    )
        except Exception as error:
            failures.append(
                self._failure(
                    request_id=judgment_request.request_id,
                    stage=PriorArtMatchingStage.JUDGMENT,
                    kind=PriorArtMatchingFailureKind.JUDGMENT_SCHEMA_ERROR,
                    error=error,
                )
            )
            return self._blocked_execution(
                execution_id=execution_id,
                protocol=protocol,
                attempts=attempts,
                recall_candidates=recall_candidates,
                failures=failures,
                started_at=started_at,
                rerank_request=rerank_request,
                rerank_batch=rerank_batch,
                reranked_candidates=tuple(reranked),
                judgment_request=judgment_request,
            )

        relation_candidates: list[PriorArtRelationCandidate] = []
        review_tasks: list[PriorArtReviewTask] = []
        for selected_rank, (reranked_item, context, judgment) in enumerate(
            zip(selected, contexts, judgment_batch.judgments, strict=True), start=1
        ):
            relation = _relation_from_judgment(
                judgment=judgment,
                rank=selected_rank,
                retrieval_signals=context.retrieval_signals,
                matcher_manifest_sha256=protocol.matcher_manifest.manifest_sha256,
            )
            channel_count = recall_by_hash[
                reranked_item.recall_candidate_sha256
            ].channel_scores.observed_count
            reasons = _review_reasons(
                judgment=judgment,
                relation=relation,
                channel_count=channel_count,
                protocol=protocol,
            )
            relation_candidate = PriorArtRelationCandidate(
                reranked_candidate_sha256=reranked_item.reranked_sha256,
                selected_rank=selected_rank,
                judgment=judgment,
                relation=relation,
                relation_confidence=judgment.relation_confidence,
                difference_confidence=judgment.difference_confidence,
                disposition=(
                    PriorArtRelationDisposition.REVIEW_REQUIRED
                    if reasons
                    else PriorArtRelationDisposition.AUTO_ACCEPTED
                ),
                review_reasons=reasons,
            )
            relation_candidates.append(relation_candidate)
            if reasons:
                review_tasks.append(
                    PriorArtReviewTask(
                        relation_candidate_sha256=(relation_candidate.relation_candidate_sha256),
                        candidate_claim_sha256=judgment.candidate_claim_sha256,
                        prior_claim_sha256=judgment.prior_claim_sha256,
                        reasons=reasons,
                        evidence_package_sha256=_review_package_sha256(
                            protocol=protocol,
                            graph_bundle_sha256=self.graph_bundle.bundle_sha256,
                            candidate=relation_candidate,
                        ),
                    )
                )
        review_queue = PriorArtReviewQueue(
            protocol_sha256=protocol.protocol_sha256,
            tasks=tuple(review_tasks),
        )
        return PriorArtMatchingExecution(
            execution_id=execution_id,
            protocol=protocol,
            recall_attempts=tuple(attempts),
            recall_candidates=recall_candidates,
            rerank_request=rerank_request,
            rerank_batch=rerank_batch,
            reranked_candidates=tuple(reranked),
            judgment_request=judgment_request,
            judgment_batch=judgment_batch,
            relation_candidates=tuple(relation_candidates),
            failures=(),
            review_queue=review_queue,
            disposition=(
                PriorArtMatchingDisposition.PENDING_REVIEW
                if review_tasks
                else PriorArtMatchingDisposition.READY
            ),
            started_at=started_at,
            completed_at=self._now(),
        )

    async def execute_and_commit(
        self, *, protocol: PriorArtMatchingProtocol, execution_id: str
    ) -> CommittedPriorArtMatching:
        if self.archive is None:
            raise ValueError("prior-art matching commit requires a content-addressed archive")
        execution = await self.execute(protocol=protocol, execution_id=execution_id)
        ledger = self.archive.store_ledger(
            value=execution,
            object_sha256=execution.execution_sha256,
            archived_at=execution.completed_at,
        )
        return CommittedPriorArtMatching(execution=execution, ledger=ledger)


def load_prior_art_matching(
    *, archive: ContentAddressedResponseArchive, ledger: ArchivedKnowledgeLedger
) -> PriorArtMatchingExecution:
    payload = archive.read_ledger(ledger)
    execution = PriorArtMatchingExecution.model_validate_json(payload)
    canonical = canonical_json_bytes(execution)
    if canonical != payload:
        raise ValueError("archived prior-art matching execution is not canonical JSON")
    if execution.execution_sha256 != ledger.object_sha256:
        raise ValueError("archived prior-art matching execution has another object identity")
    return execution


def _resolved_relation_from_review(
    *,
    candidate: PriorArtRelationCandidate,
    review: PriorArtRelationReview,
    rank: int,
) -> ResolvedPriorArtRelation:
    if review.decision is PriorArtReviewDecision.REJECT:
        raise ValueError("rejected prior-art review has no resolved relation")
    judgment = review.replacement_judgment or candidate.judgment
    status = (
        EvidenceReviewStatus.HUMAN_VERIFIED
        if review.reviewer_kind is PriorArtReviewKind.HUMAN
        else EvidenceReviewStatus.SECOND_MODEL_VERIFIED
    )
    relation = _relation_from_judgment(
        judgment=judgment,
        rank=rank,
        retrieval_signals=candidate.relation.retrieval_signals,
        matcher_manifest_sha256=candidate.relation.matcher_manifest_sha256,
        reviewer_status=status,
        reviewer_principal_sha256=review.reviewer_principal_sha256,
        reviewed_at=review.reviewed_at,
    )
    return ResolvedPriorArtRelation(
        original_relation_candidate_sha256=candidate.relation_candidate_sha256,
        decision=(
            PriorArtResolutionDecision.REVIEW_REVISE
            if review.decision is PriorArtReviewDecision.REVISE
            else PriorArtResolutionDecision.REVIEW_ACCEPT
        ),
        relation=relation,
        review_sha256=review.review_sha256,
    )


def _resolved_auto_relation(
    *, candidate: PriorArtRelationCandidate, rank: int
) -> ResolvedPriorArtRelation:
    return ResolvedPriorArtRelation(
        original_relation_candidate_sha256=candidate.relation_candidate_sha256,
        decision=PriorArtResolutionDecision.AUTO_ACCEPT,
        relation=_relation_from_judgment(
            judgment=candidate.judgment,
            rank=rank,
            retrieval_signals=candidate.relation.retrieval_signals,
            matcher_manifest_sha256=candidate.relation.matcher_manifest_sha256,
        ),
    )


def resolve_prior_art_matching(
    *,
    execution: PriorArtMatchingExecution,
    reviews: tuple[PriorArtRelationReview, ...],
    resolution_id: str,
    resolved_at: AwareDatetime,
) -> PriorArtMatchingResolution:
    if execution.failures:
        raise ValueError("blocked prior-art matching cannot be resolved")
    review_map = {review.relation_candidate_sha256: review for review in reviews}
    if len(review_map) != len(reviews):
        raise ValueError("prior-art reviews cannot repeat a relation candidate")
    task_ids = {task.relation_candidate_sha256 for task in execution.review_queue.tasks}
    if set(review_map) != task_ids:
        raise ValueError("prior-art reviews must exactly cover the review queue")
    accepted: list[ResolvedPriorArtRelation] = []
    rejected: list[str] = []
    for candidate in execution.relation_candidates:
        review = review_map.get(candidate.relation_candidate_sha256)
        if review is None:
            accepted.append(
                _resolved_auto_relation(
                    candidate=candidate,
                    rank=len(accepted) + 1,
                )
            )
        elif review.decision is PriorArtReviewDecision.REJECT:
            rejected.append(candidate.relation_candidate_sha256)
        else:
            accepted.append(
                _resolved_relation_from_review(
                    candidate=candidate,
                    review=review,
                    rank=len(accepted) + 1,
                )
            )
    return PriorArtMatchingResolution(
        resolution_id=resolution_id,
        execution=execution,
        reviews=reviews,
        accepted=tuple(accepted),
        rejected_relation_candidate_sha256s=tuple(rejected),
        resolved_at=resolved_at,
    )


def commit_prior_art_matching_resolution(
    *,
    archive: ContentAddressedResponseArchive,
    resolution: PriorArtMatchingResolution,
) -> CommittedPriorArtMatchingResolution:
    ledger = archive.store_ledger(
        value=resolution,
        object_sha256=resolution.resolution_sha256,
        archived_at=resolution.resolved_at,
    )
    return CommittedPriorArtMatchingResolution(resolution=resolution, ledger=ledger)


def load_prior_art_matching_resolution(
    *, archive: ContentAddressedResponseArchive, ledger: ArchivedKnowledgeLedger
) -> PriorArtMatchingResolution:
    payload = archive.read_ledger(ledger)
    resolution = PriorArtMatchingResolution.model_validate_json(payload)
    canonical = canonical_json_bytes(resolution)
    if canonical != payload:
        raise ValueError("archived prior-art matching resolution is not canonical JSON")
    if resolution.resolution_sha256 != ledger.object_sha256:
        raise ValueError("archived prior-art matching resolution has another object identity")
    return resolution


__all__ = [
    "PRIOR_ART_JUDGMENT_SCHEMA_SHA256",
    "CommittedPriorArtMatching",
    "CommittedPriorArtMatchingResolution",
    "PriorArtChannelEvidence",
    "PriorArtJudgmentBatch",
    "PriorArtJudgmentContext",
    "PriorArtJudgmentDraft",
    "PriorArtJudgmentRequest",
    "PriorArtMatcherAdapter",
    "PriorArtMatcherManifest",
    "PriorArtMatchingDisposition",
    "PriorArtMatchingExecution",
    "PriorArtMatchingExecutor",
    "PriorArtMatchingFailure",
    "PriorArtMatchingFailureKind",
    "PriorArtMatchingProtocol",
    "PriorArtMatchingResolution",
    "PriorArtMatchingStage",
    "PriorArtMatchingTarget",
    "PriorArtRecallAdapter",
    "PriorArtRecallCandidate",
    "PriorArtRecallChannel",
    "PriorArtRecallHit",
    "PriorArtRecallQuery",
    "PriorArtRecallResult",
    "PriorArtRelationCandidate",
    "PriorArtRelationDisposition",
    "PriorArtRelationReview",
    "PriorArtRerankBatch",
    "PriorArtRerankRequest",
    "PriorArtRerankScore",
    "PriorArtResolutionDecision",
    "PriorArtReviewDecision",
    "PriorArtReviewKind",
    "PriorArtReviewQueue",
    "PriorArtReviewReason",
    "PriorArtReviewTask",
    "RecallAttemptOutcome",
    "RecallChannelManifest",
    "RecallChannelScores",
    "RecallSupportDisposition",
    "RerankSelectionDisposition",
    "RerankedPriorArtCandidate",
    "ResolvedPriorArtRelation",
    "build_prior_art_matching_protocol",
    "commit_prior_art_matching_resolution",
    "load_prior_art_matching",
    "load_prior_art_matching_resolution",
    "resolve_prior_art_matching",
]
