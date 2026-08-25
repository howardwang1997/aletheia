"""Fail-closed atomic-claim extraction from ephemeral, licensed source spans.

Literature bytes are deliberately runtime-only.  The immutable execution ledger retains their
content identities, structured scientific facts, confidence decisions, and evidence edges, but
never an evidence quote or a source-text field.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, model_validator

from aletheia.knowledge.ingestion import (
    ContentAccessGrant,
    ContentUse,
    CorpusIngestionBundle,
)
from aletheia.knowledge.response_archive import (
    ArchivedKnowledgeLedger,
    ContentAddressedResponseArchive,
)
from aletheia.knowledge.schemas import (
    AtomicClaim,
    AtomicClaimGraph,
    ClaimDirection,
    ClaimEvidenceEdge,
    ClaimEvidenceRelation,
    ClaimOrigin,
    ClaimType,
    EvidenceReviewStatus,
    ExtractionMethod,
    KnowledgeModel,
    QuantitativeEffect,
    SourceSpan,
)
from aletheia.reproducibility.manifest import canonical_json_bytes, content_sha256


CANONICAL_TEXT_NORMALIZER_SHA256 = hashlib.sha256(
    b"aletheia.f8s3.utf8-nfkc-whitespace-v1"
).hexdigest()


class ClaimExtractorRuntime(str, Enum):
    DETERMINISTIC = "deterministic"
    MODEL = "model"


class ClaimCandidateDisposition(str, Enum):
    AUTO_ACCEPTED = "auto_accepted"
    REVIEW_REQUIRED = "review_required"


class ClaimReviewReason(str, Enum):
    OCR_SOURCE = "ocr_source"
    UNVERIFIED_SOURCE = "unverified_source"
    LOW_SOURCE_CONFIDENCE = "low_source_confidence"
    LOW_CLAIM_CONFIDENCE = "low_claim_confidence"
    LOW_EVIDENCE_CONFIDENCE = "low_evidence_confidence"
    LOW_QUANTITATIVE_CONFIDENCE = "low_quantitative_confidence"


_REVIEW_REASON_ORDER = tuple(ClaimReviewReason)


class ClaimExtractionStage(str, Enum):
    ACCESS = "access"
    CONTENT_RESOLUTION = "content_resolution"
    CONTENT_VERIFICATION = "content_verification"
    EXTRACTOR = "extractor"
    STRUCTURED_OUTPUT = "structured_output"


class ClaimExtractionFailureKind(str, Enum):
    ACCESS_DENIED = "access_denied"
    ACCESS_EXPIRED = "access_expired"
    SOURCE_SPAN_REJECTED = "source_span_rejected"
    CONTENT_UNAVAILABLE = "content_unavailable"
    CONTENT_IDENTITY_MISMATCH = "content_identity_mismatch"
    SPAN_IDENTITY_MISMATCH = "span_identity_mismatch"
    EXTRACTOR_ERROR = "extractor_error"
    OUTPUT_SCHEMA_ERROR = "output_schema_error"
    OUTPUT_BINDING_ERROR = "output_binding_error"
    OUTPUT_POLICY_VIOLATION = "output_policy_violation"


class ClaimExtractionOutcome(str, Enum):
    SUCCESS = "success"
    ERROR = "error"


class ClaimExtractionDisposition(str, Enum):
    READY_FOR_GRAPH = "ready_for_graph"
    PENDING_REVIEW = "pending_review"
    BLOCKED = "blocked"


class ClaimReviewKind(str, Enum):
    HUMAN = "human"
    SECOND_MODEL = "second_model"


class ClaimReviewDecision(str, Enum):
    ACCEPT = "accept"
    REVISE = "revise"
    REJECT = "reject"


class ClaimResolutionDecision(str, Enum):
    AUTO_ACCEPT = "auto_accept"
    REVIEW_ACCEPT = "review_accept"
    REVIEW_REVISE = "review_revise"


class ClaimReplayItemStatus(str, Enum):
    VERIFIED = "verified"
    UNAVAILABLE = "unavailable"
    MISMATCH = "mismatch"


class ClaimReplayStatus(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    MISMATCH = "mismatch"


class StructuredClaimDraft(KnowledgeModel):
    """Strict extractor output; authority, prose evidence, and source identity are not inferred."""

    schema_version: Literal[1] = 1
    local_claim_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")
    source_span_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    subject: str = Field(min_length=1, max_length=2048)
    relation: str = Field(min_length=1, max_length=1024)
    object: str = Field(min_length=1, max_length=2048)
    qualifiers: tuple[str, ...] = ()
    population: str | None = Field(default=None, max_length=2048)
    conditions: tuple[str, ...] = ()
    direction: ClaimDirection
    claim_type: ClaimType
    quantitative_effect: QuantitativeEffect | None = None
    evidence_relation: ClaimEvidenceRelation
    claim_confidence: float = Field(ge=0.0, le=1.0)
    evidence_confidence: float = Field(ge=0.0, le=1.0)
    quantitative_grounding_confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _draft_is_atomic_and_explicit(self) -> "StructuredClaimDraft":
        values = self.qualifiers + self.conditions
        if any(not value.strip() for value in values):
            raise ValueError("claim qualifiers and conditions cannot be blank")
        if len(self.qualifiers) != len(set(self.qualifiers)):
            raise ValueError("claim qualifiers must be unique")
        if len(self.conditions) != len(set(self.conditions)):
            raise ValueError("claim conditions must be unique")
        has_effect = self.quantitative_effect is not None
        if has_effect != (self.quantitative_grounding_confidence is not None):
            raise ValueError(
                "quantitative claims require a separate grounding confidence, and only then"
            )
        return self

    @property
    def draft_sha256(self) -> str:
        return content_sha256(self)


class StructuredClaimBatch(KnowledgeModel):
    schema_version: Literal[1] = 1
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_span_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    claims: tuple[StructuredClaimDraft, ...]
    no_claim_reason_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{2,79}$")

    @model_validator(mode="after")
    def _batch_is_closed(self) -> "StructuredClaimBatch":
        if bool(self.claims) == bool(self.no_claim_reason_code):
            raise ValueError("claim batch requires claims or one no-claim reason, never both")
        local_ids = [claim.local_claim_id for claim in self.claims]
        hashes = [claim.draft_sha256 for claim in self.claims]
        if len(local_ids) != len(set(local_ids)) or len(hashes) != len(set(hashes)):
            raise ValueError("structured claim drafts must have unique IDs and contents")
        if any(claim.source_span_sha256 != self.source_span_sha256 for claim in self.claims):
            raise ValueError("every structured claim must bind the batch source span")
        return self

    @property
    def batch_sha256(self) -> str:
        return content_sha256(self)


CLAIM_OUTPUT_SCHEMA_SHA256 = content_sha256(StructuredClaimBatch.model_json_schema())


class ClaimExtractorManifest(KnowledgeModel):
    schema_version: Literal[1] = 1
    manifest_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    runtime: ClaimExtractorRuntime
    adapter_code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parser_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    instruction_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    model_identity_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    supported_claim_types: tuple[ClaimType, ...] = Field(min_length=1)
    maximum_span_bytes: int = Field(ge=1, le=16 * 1024 * 1024)
    maximum_claims_per_span: int = Field(ge=1, le=100)
    tool_names: tuple[str, ...] = ()
    tool_policy: Literal["none"] = "none"
    source_text_trust: Literal["untrusted_literature_data"] = "untrusted_literature_data"
    transport_policy: Literal["none", "model_transport_only"]
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _manifest_has_no_tool_authority(self) -> "ClaimExtractorManifest":
        if self.output_schema_sha256 != CLAIM_OUTPUT_SCHEMA_SHA256:
            raise ValueError("extractor manifest is not bound to the exact structured schema")
        if self.tool_names:
            raise ValueError("claim extractors cannot receive tool authority")
        types = tuple(self.supported_claim_types)
        if len(types) != len(set(types)):
            raise ValueError("extractor supported claim types must be unique")
        model_fields_present = bool(self.instruction_sha256 and self.model_identity_sha256)
        if self.runtime is ClaimExtractorRuntime.MODEL:
            if not model_fields_present or self.transport_policy != "model_transport_only":
                raise ValueError(
                    "model extractor requires frozen instruction/model identities and model-only transport"
                )
        elif (
            self.instruction_sha256 is not None
            or self.model_identity_sha256 is not None
            or self.transport_policy != "none"
        ):
            raise ValueError("deterministic extractor cannot declare a model or network transport")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class ClaimExtractionTarget(KnowledgeModel):
    schema_version: Literal[1] = 1
    ordinal: int = Field(ge=0, le=100_000)
    source_span_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    extractor_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def target_sha256(self) -> str:
        return content_sha256(self)


class ClaimExtractionProtocol(KnowledgeModel):
    schema_version: Literal[1] = 1
    protocol_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    ingestion_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    corpus_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    access_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_normalizer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    extractors: tuple[ClaimExtractorManifest, ...] = Field(min_length=1)
    targets: tuple[ClaimExtractionTarget, ...] = Field(min_length=1)
    minimum_auto_claim_confidence: float = Field(ge=0.0, le=1.0)
    minimum_auto_evidence_confidence: float = Field(ge=0.0, le=1.0)
    minimum_auto_quantitative_confidence: float = Field(ge=0.0, le=1.0)
    minimum_auto_source_confidence: float = Field(ge=0.0, le=1.0)
    maximum_document_bytes: int = Field(ge=1, le=64 * 1024 * 1024)
    maximum_verbatim_word_run: int = Field(ge=3, le=25)
    ocr_always_requires_review: Literal[True] = True
    unverified_span_requires_review: Literal[True] = True
    structured_facts_only: Literal[True] = True
    required_evidence_relations: tuple[ClaimEvidenceRelation, ...] = (
        ClaimEvidenceRelation.SUPPORTS,
        ClaimEvidenceRelation.REFUTES,
        ClaimEvidenceRelation.QUALIFIES,
        ClaimEvidenceRelation.MENTIONS,
    )
    review_kinds: tuple[ClaimReviewKind, ...] = (
        ClaimReviewKind.HUMAN,
        ClaimReviewKind.SECOND_MODEL,
    )
    failure_policy: Literal["record_every_target_and_block_graph"] = (
        "record_every_target_and_block_graph"
    )
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _protocol_is_exact_and_canonical(self) -> "ClaimExtractionProtocol":
        if self.output_schema_sha256 != CLAIM_OUTPUT_SCHEMA_SHA256:
            raise ValueError("claim extraction protocol is bound to another output schema")
        if self.content_normalizer_sha256 != CANONICAL_TEXT_NORMALIZER_SHA256:
            raise ValueError("claim extraction protocol is bound to another text normalizer")
        manifests = {item.manifest_sha256: item for item in self.extractors}
        manifest_ids = [item.manifest_id for item in self.extractors]
        if len(manifests) != len(self.extractors) or len(manifest_ids) != len(set(manifest_ids)):
            raise ValueError("claim extractor manifests must have unique identities")
        ordinals = [target.ordinal for target in self.targets]
        spans = [target.source_span_sha256 for target in self.targets]
        if ordinals != list(range(len(self.targets))):
            raise ValueError("claim extraction targets must have contiguous ordinals")
        if len(spans) != len(set(spans)):
            raise ValueError("a source span can occur only once in an extraction protocol")
        if any(target.extractor_manifest_sha256 not in manifests for target in self.targets):
            raise ValueError("claim extraction target references an unknown extractor")
        if self.required_evidence_relations != tuple(ClaimEvidenceRelation):
            raise ValueError("claim extraction must preserve every evidence relation")
        if self.review_kinds != tuple(ClaimReviewKind):
            raise ValueError("low-confidence review must permit human or independent second model")
        return self

    @property
    def protocol_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True, repr=False)
class EphemeralSpanContent:
    """Licensed canonical text that must never be placed in a persistent model."""

    paper_snapshot_sha256: str
    document_bytes: bytes
    exact_span_bytes: bytes
    content_normalizer_sha256: str = CANONICAL_TEXT_NORMALIZER_SHA256


class SpanContentResolver(Protocol):
    async def resolve(
        self, *, span: SourceSpan, grant: ContentAccessGrant
    ) -> EphemeralSpanContent: ...


class ClaimExtractionRequest(KnowledgeModel):
    schema_version: Literal[1] = 1
    request_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_span_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    access_grant_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    extractor_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    exact_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_uses: tuple[ContentUse, ...] = Field(min_length=1)
    content_trust: Literal["untrusted_literature_data"] = "untrusted_literature_data"
    tool_authority: Literal["none"] = "none"
    issued_at: AwareDatetime

    @model_validator(mode="after")
    def _request_uses_are_canonical(self) -> "ClaimExtractionRequest":
        expected = tuple(
            use
            for use in (ContentUse.SPAN_EXTRACTION, ContentUse.MODEL_INPUT)
            if use in self.required_uses
        )
        if self.required_uses != expected or ContentUse.SPAN_EXTRACTION not in expected:
            raise ValueError("claim extraction request uses must be unique and canonical")
        return self

    @property
    def request_sha256(self) -> str:
        return content_sha256(self)


class ClaimExtractor(Protocol):
    @property
    def manifest(self) -> ClaimExtractorManifest: ...

    async def extract(self, *, request: ClaimExtractionRequest, source_text: str) -> object: ...


class SpanContentReceipt(KnowledgeModel):
    schema_version: Literal[1] = 1
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_span_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    access_grant_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    paper_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_bytes: int = Field(ge=1, le=64 * 1024 * 1024)
    exact_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    span_bytes: int = Field(ge=1, le=16 * 1024 * 1024)
    required_uses: tuple[ContentUse, ...] = Field(min_length=1)
    authorization_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    accessed_at: AwareDatetime
    retention: Literal["hashes_and_structured_facts_only"] = "hashes_and_structured_facts_only"

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class ClaimExtractionCandidate(KnowledgeModel):
    schema_version: Literal[1] = 1
    attempt_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    extractor_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_span_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    access_grant_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    draft: StructuredClaimDraft
    claim: AtomicClaim
    evidence_edge: ClaimEvidenceEdge
    disposition: ClaimCandidateDisposition
    review_reasons: tuple[ClaimReviewReason, ...]

    @model_validator(mode="after")
    def _candidate_is_evidence_closed(self) -> "ClaimExtractionCandidate":
        if self.draft.source_span_sha256 != self.source_span_sha256:
            raise ValueError("claim draft is bound to another source span")
        if self.claim.origin is not ClaimOrigin.PRIOR_ART:
            raise ValueError("literature extraction can create prior-art claims only")
        if self.evidence_edge.claim_sha256 != self.claim.claim_sha256:
            raise ValueError("claim candidate evidence names another claim")
        if self.evidence_edge.source_span_sha256 != self.source_span_sha256:
            raise ValueError("claim candidate evidence names another source span")
        if self.evidence_edge.relation is not self.draft.evidence_relation:
            raise ValueError("claim candidate erased or changed its evidence relation")
        if self.evidence_edge.extraction_confidence != self.draft.evidence_confidence:
            raise ValueError("claim candidate changed its evidence confidence")
        if self.evidence_edge.reviewer_status is not EvidenceReviewStatus.UNREVIEWED:
            raise ValueError("new extraction candidates must begin unreviewed")
        expected_reasons = tuple(sorted(set(self.review_reasons), key=_REVIEW_REASON_ORDER.index))
        if self.review_reasons != expected_reasons:
            raise ValueError("claim review reasons must be unique and canonically ordered")
        expected_disposition = (
            ClaimCandidateDisposition.REVIEW_REQUIRED
            if self.review_reasons
            else ClaimCandidateDisposition.AUTO_ACCEPTED
        )
        if self.disposition is not expected_disposition:
            raise ValueError("claim disposition does not match its review reasons")
        return self

    @property
    def candidate_sha256(self) -> str:
        return content_sha256(self)


class ClaimExtractionFailure(KnowledgeModel):
    schema_version: Literal[1] = 1
    attempt_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    target_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_span_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    extractor_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage: ClaimExtractionStage
    kind: ClaimExtractionFailureKind
    error_class: str = Field(min_length=1, max_length=256)
    error_detail_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    occurred_at: AwareDatetime

    @property
    def failure_sha256(self) -> str:
        return content_sha256(self)


class ClaimExtractionAttempt(KnowledgeModel):
    schema_version: Literal[1] = 1
    attempt_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    target_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request: ClaimExtractionRequest
    outcome: ClaimExtractionOutcome
    content_receipt: SpanContentReceipt | None = None
    structured_output: StructuredClaimBatch | None = None
    candidate_sha256s: tuple[str, ...]
    failure_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    started_at: AwareDatetime
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def _attempt_matches_outcome(self) -> "ClaimExtractionAttempt":
        if self.completed_at < self.started_at:
            raise ValueError("claim extraction attempt ended before it started")
        if self.request.request_id != self.attempt_id:
            raise ValueError("claim extraction request and attempt IDs differ")
        if self.request.target_sha256 != self.target_sha256:
            raise ValueError("claim extraction request is bound to another target")
        if self.outcome is ClaimExtractionOutcome.SUCCESS:
            if (
                self.content_receipt is None
                or self.structured_output is None
                or self.failure_sha256 is not None
            ):
                raise ValueError("successful extraction requires content and structured output")
            if self.structured_output.request_sha256 != self.request.request_sha256:
                raise ValueError("structured output is bound to another extraction request")
            if self.structured_output.source_span_sha256 != self.request.source_span_sha256:
                raise ValueError("structured output is bound to another source span")
        elif self.failure_sha256 is None or self.structured_output is not None:
            raise ValueError("failed extraction requires one failure and no structured output")
        if len(self.candidate_sha256s) != len(set(self.candidate_sha256s)):
            raise ValueError("claim extraction candidate identities must be unique")
        return self

    @property
    def attempt_sha256(self) -> str:
        return content_sha256(self)


class ClaimReviewTask(KnowledgeModel):
    schema_version: Literal[1] = 1
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    source_span_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reasons: tuple[ClaimReviewReason, ...] = Field(min_length=1)
    evidence_package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    permitted_review_kinds: tuple[ClaimReviewKind, ...] = (
        ClaimReviewKind.HUMAN,
        ClaimReviewKind.SECOND_MODEL,
    )

    @model_validator(mode="after")
    def _task_preserves_review_options(self) -> "ClaimReviewTask":
        if self.reasons != tuple(sorted(set(self.reasons), key=_REVIEW_REASON_ORDER.index)):
            raise ValueError("claim review-task reasons must be canonical")
        if self.permitted_review_kinds != tuple(ClaimReviewKind):
            raise ValueError("claim review task must allow human or independent second model")
        return self

    @property
    def task_sha256(self) -> str:
        return content_sha256(self)


class ClaimReviewQueue(KnowledgeModel):
    schema_version: Literal[1] = 1
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tasks: tuple[ClaimReviewTask, ...]
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _tasks_are_unique(self) -> "ClaimReviewQueue":
        candidates = [task.candidate_sha256 for task in self.tasks]
        if len(candidates) != len(set(candidates)):
            raise ValueError("claim review queue cannot repeat a candidate")
        return self

    @property
    def queue_sha256(self) -> str:
        return content_sha256(self)


class ClaimExtractionExecution(KnowledgeModel):
    schema_version: Literal[1] = 1
    execution_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    protocol: ClaimExtractionProtocol
    attempts: tuple[ClaimExtractionAttempt, ...] = Field(min_length=1)
    candidates: tuple[ClaimExtractionCandidate, ...]
    failures: tuple[ClaimExtractionFailure, ...]
    review_queue: ClaimReviewQueue
    disposition: ClaimExtractionDisposition
    started_at: AwareDatetime
    completed_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _execution_closes_every_target(self) -> "ClaimExtractionExecution":
        if self.completed_at < self.started_at:
            raise ValueError("claim extraction execution ended before it started")
        if len(self.attempts) != len(self.protocol.targets):
            raise ValueError("every frozen claim extraction target requires one attempt")
        for target, attempt in zip(self.protocol.targets, self.attempts, strict=True):
            if (
                attempt.target_sha256 != target.target_sha256
                or attempt.request.protocol_sha256 != self.protocol.protocol_sha256
                or attempt.request.source_span_sha256 != target.source_span_sha256
                or attempt.request.extractor_manifest_sha256 != target.extractor_manifest_sha256
            ):
                raise ValueError("claim extraction attempts do not preserve target order")
        attempt_ids = [attempt.attempt_id for attempt in self.attempts]
        if len(attempt_ids) != len(set(attempt_ids)):
            raise ValueError("claim extraction attempt IDs must be unique")

        candidates = {candidate.candidate_sha256: candidate for candidate in self.candidates}
        if len(candidates) != len(self.candidates):
            raise ValueError("claim extraction candidates must be unique")
        referenced_candidates: list[str] = []
        for attempt in self.attempts:
            referenced_candidates.extend(attempt.candidate_sha256s)
            for candidate_sha256 in attempt.candidate_sha256s:
                candidate = candidates.get(candidate_sha256)
                if candidate is None or candidate.attempt_id != attempt.attempt_id:
                    raise ValueError("claim extraction attempt references another candidate ledger")
            if attempt.outcome is ClaimExtractionOutcome.SUCCESS:
                assert attempt.structured_output is not None
                if len(attempt.candidate_sha256s) != len(attempt.structured_output.claims):
                    raise ValueError("every structured claim must yield exactly one candidate")
            elif attempt.candidate_sha256s:
                raise ValueError("failed extraction attempts cannot yield candidates")
        if referenced_candidates != [candidate.candidate_sha256 for candidate in self.candidates]:
            raise ValueError("candidate ledger must preserve exact attempt/output order")

        failures = {failure.failure_sha256: failure for failure in self.failures}
        if len(failures) != len(self.failures):
            raise ValueError("claim extraction failures must be unique")
        referenced_failures = [
            attempt.failure_sha256
            for attempt in self.attempts
            if attempt.failure_sha256 is not None
        ]
        if referenced_failures != [failure.failure_sha256 for failure in self.failures]:
            raise ValueError("claim extraction attempts must close the exact failure ledger")
        for attempt in self.attempts:
            if attempt.failure_sha256 is None:
                continue
            failure = failures[attempt.failure_sha256]
            if failure.attempt_id != attempt.attempt_id:
                raise ValueError("claim extraction failure names another attempt")

        queued = [
            candidate
            for candidate in self.candidates
            if candidate.disposition is ClaimCandidateDisposition.REVIEW_REQUIRED
        ]
        if [task.candidate_sha256 for task in self.review_queue.tasks] != [
            candidate.candidate_sha256 for candidate in queued
        ]:
            raise ValueError("claim review queue must exactly cover review-required candidates")
        if self.review_queue.protocol_sha256 != self.protocol.protocol_sha256:
            raise ValueError("claim review queue is bound to another protocol")
        for candidate, task in zip(queued, self.review_queue.tasks, strict=True):
            if (
                task.attempt_id != candidate.attempt_id
                or task.source_span_sha256 != candidate.source_span_sha256
                or task.reasons != candidate.review_reasons
            ):
                raise ValueError("claim review task does not preserve its candidate evidence")

        expected = (
            ClaimExtractionDisposition.BLOCKED
            if self.failures
            else ClaimExtractionDisposition.PENDING_REVIEW
            if queued
            else ClaimExtractionDisposition.READY_FOR_GRAPH
        )
        if self.disposition is not expected:
            raise ValueError("claim extraction disposition does not match failures/review work")
        return self

    @property
    def execution_sha256(self) -> str:
        return content_sha256(self)


class CommittedClaimExtraction(KnowledgeModel):
    schema_version: Literal[1] = 1
    execution: ClaimExtractionExecution
    ledger: ArchivedKnowledgeLedger

    @model_validator(mode="after")
    def _ledger_commits_execution(self) -> "CommittedClaimExtraction":
        payload = canonical_json_bytes(self.execution)
        if self.ledger.object_sha256 != self.execution.execution_sha256:
            raise ValueError("claim extraction ledger names another execution")
        if self.ledger.ledger_sha256 != hashlib.sha256(payload).hexdigest():
            raise ValueError("claim extraction ledger hash does not commit its execution")
        if self.ledger.ledger_bytes != len(payload):
            raise ValueError("claim extraction ledger size does not commit its execution")
        return self


class ClaimCandidateReview(KnowledgeModel):
    schema_version: Literal[1] = 1
    review_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_kind: ClaimReviewKind
    reviewer_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    extractor_excluded: Literal[True] = True
    decision: ClaimReviewDecision
    replacement_draft: StructuredClaimDraft | None = None
    rationale_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _review_is_independent_and_explicit(self) -> "ClaimCandidateReview":
        if self.reviewer_kind is ClaimReviewKind.SECOND_MODEL:
            if self.reviewer_manifest_sha256 is None:
                raise ValueError("second-model claim review requires a frozen reviewer manifest")
        elif self.reviewer_manifest_sha256 is not None:
            raise ValueError("human claim review cannot impersonate a model manifest")
        if (self.decision is ClaimReviewDecision.REVISE) != (self.replacement_draft is not None):
            raise ValueError("only revise decisions require one replacement claim draft")
        return self

    @property
    def review_sha256(self) -> str:
        return content_sha256(self)


class ResolvedClaimCandidate(KnowledgeModel):
    schema_version: Literal[1] = 1
    original_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: ClaimResolutionDecision
    final_claim: AtomicClaim
    final_evidence_edge: ClaimEvidenceEdge
    review_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _resolution_is_evidence_closed(self) -> "ResolvedClaimCandidate":
        if self.final_claim.origin is not ClaimOrigin.PRIOR_ART:
            raise ValueError("resolved extraction must remain a prior-art claim")
        if self.final_evidence_edge.claim_sha256 != self.final_claim.claim_sha256:
            raise ValueError("resolved evidence edge names another claim")
        reviewed = self.decision is not ClaimResolutionDecision.AUTO_ACCEPT
        if reviewed != (self.review_sha256 is not None):
            raise ValueError("reviewed claim resolution requires an exact review identity")
        if reviewed == (
            self.final_evidence_edge.reviewer_status is EvidenceReviewStatus.UNREVIEWED
        ):
            raise ValueError("resolved evidence review status does not match its decision")
        return self

    @property
    def resolved_sha256(self) -> str:
        return content_sha256(self)


class ClaimExtractionResolution(KnowledgeModel):
    schema_version: Literal[1] = 1
    resolution_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    execution: ClaimExtractionExecution
    reviews: tuple[ClaimCandidateReview, ...]
    accepted: tuple[ResolvedClaimCandidate, ...]
    rejected_candidate_sha256s: tuple[str, ...]
    resolved_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _resolution_partitions_every_candidate(self) -> "ClaimExtractionResolution":
        if self.execution.failures:
            raise ValueError("blocked extraction executions cannot be resolved into a claim graph")
        candidates = {
            candidate.candidate_sha256: candidate for candidate in self.execution.candidates
        }
        tasks = {task.candidate_sha256: task for task in self.execution.review_queue.tasks}
        reviews = {review.candidate_sha256: review for review in self.reviews}
        if len(reviews) != len(self.reviews) or set(reviews) != set(tasks):
            raise ValueError("reviews must exactly resolve the frozen claim review queue")
        if [review.candidate_sha256 for review in self.reviews] != list(tasks):
            raise ValueError("claim reviews must preserve frozen review-queue order")
        if len({review.review_id for review in self.reviews}) != len(self.reviews):
            raise ValueError("claim review IDs must be unique")
        manifests = {
            manifest.manifest_sha256: manifest for manifest in self.execution.protocol.extractors
        }
        for candidate_sha256, review in reviews.items():
            candidate = candidates[candidate_sha256]
            task = tasks[candidate_sha256]
            if review.evidence_package_sha256 != task.evidence_package_sha256:
                raise ValueError("claim review is bound to another evidence package")
            if review.reviewed_at < self.execution.completed_at:
                raise ValueError("claim review cannot predate extraction completion")
            if (
                review.reviewer_kind is ClaimReviewKind.SECOND_MODEL
                and review.reviewer_manifest_sha256 == candidate.extractor_manifest_sha256
            ):
                raise ValueError("claim extraction and second-model review must be independent")
            if (
                review.replacement_draft is not None
                and review.replacement_draft.source_span_sha256 != candidate.source_span_sha256
            ):
                raise ValueError("claim review revision cannot change its source span")
            if review.replacement_draft is not None and (
                review.replacement_draft.claim_type
                not in set(manifests[candidate.extractor_manifest_sha256].supported_claim_types)
            ):
                raise ValueError("claim review revision exceeds the extractor claim-type policy")

        accepted_ids = [item.original_candidate_sha256 for item in self.accepted]
        rejected = list(self.rejected_candidate_sha256s)
        if len(accepted_ids) != len(set(accepted_ids)) or len(rejected) != len(set(rejected)):
            raise ValueError("claim resolution cannot repeat accepted or rejected candidates")
        if set(accepted_ids) & set(rejected) or set(accepted_ids + rejected) != set(candidates):
            raise ValueError("claim resolution must partition every extraction candidate")
        if accepted_ids != [
            candidate.candidate_sha256
            for candidate in self.execution.candidates
            if candidate.candidate_sha256 in set(accepted_ids)
        ]:
            raise ValueError("accepted claim resolution must preserve extraction order")
        if rejected != [
            candidate.candidate_sha256
            for candidate in self.execution.candidates
            if candidate.candidate_sha256 in set(rejected)
        ]:
            raise ValueError("rejected claim resolution must preserve extraction order")
        accepted_map = {item.original_candidate_sha256: item for item in self.accepted}
        for candidate_sha256, candidate in candidates.items():
            review = reviews.get(candidate_sha256)
            if review is None:
                resolved = accepted_map.get(candidate_sha256)
                if (
                    candidate.disposition is not ClaimCandidateDisposition.AUTO_ACCEPTED
                    or resolved is None
                    or resolved.decision is not ClaimResolutionDecision.AUTO_ACCEPT
                    or resolved.final_claim != candidate.claim
                    or resolved.final_evidence_edge != candidate.evidence_edge
                ):
                    raise ValueError("auto-accepted extraction candidate changed during resolution")
                continue
            if review.decision is ClaimReviewDecision.REJECT:
                if candidate_sha256 not in set(rejected):
                    raise ValueError(
                        "rejected claim review was not retained in the rejection ledger"
                    )
                continue
            resolved = accepted_map.get(candidate_sha256)
            if resolved is None or resolved.review_sha256 != review.review_sha256:
                raise ValueError("accepted claim review lacks its resolved candidate")
            expected_decision = (
                ClaimResolutionDecision.REVIEW_REVISE
                if review.decision is ClaimReviewDecision.REVISE
                else ClaimResolutionDecision.REVIEW_ACCEPT
            )
            if resolved.decision is not expected_decision:
                raise ValueError("resolved claim decision differs from its review")
            if resolved != _resolved_candidate_from_review(candidate=candidate, review=review):
                raise ValueError("resolved claim content differs from its accepted review")
        claim_hashes = [item.final_claim.claim_sha256 for item in self.accepted]
        if len(claim_hashes) != len(set(claim_hashes)):
            raise ValueError("resolved claims must have unique contents")
        if self.resolved_at < max(
            (review.reviewed_at for review in self.reviews),
            default=self.execution.completed_at,
        ):
            raise ValueError("claim resolution cannot predate its reviews")
        return self

    @property
    def resolution_sha256(self) -> str:
        return content_sha256(self)


class CommittedClaimExtractionResolution(KnowledgeModel):
    schema_version: Literal[1] = 1
    resolution: ClaimExtractionResolution
    ledger: ArchivedKnowledgeLedger

    @model_validator(mode="after")
    def _ledger_commits_resolution(self) -> "CommittedClaimExtractionResolution":
        payload = canonical_json_bytes(self.resolution)
        if self.ledger.object_sha256 != self.resolution.resolution_sha256:
            raise ValueError("claim resolution ledger names another resolution")
        if self.ledger.ledger_sha256 != hashlib.sha256(payload).hexdigest():
            raise ValueError("claim resolution ledger hash does not commit its resolution")
        if self.ledger.ledger_bytes != len(payload):
            raise ValueError("claim resolution ledger size does not commit its resolution")
        return self


class ExtractedAtomicClaimGraphBundle(KnowledgeModel):
    """Self-contained proof that an atomic graph is the exact reviewed extraction view."""

    schema_version: Literal[1] = 1
    bundle_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    resolution: ClaimExtractionResolution
    candidate_claims: tuple[AtomicClaim, ...] = Field(min_length=1)
    graph: AtomicClaimGraph
    built_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _graph_is_exact_reviewed_view(self) -> "ExtractedAtomicClaimGraphBundle":
        if any(claim.origin is not ClaimOrigin.CANDIDATE for claim in self.candidate_claims):
            raise ValueError("extracted graph bundle candidate inputs must have candidate origin")
        expected_claims = (
            *self.candidate_claims,
            *(item.final_claim for item in self.resolution.accepted),
        )
        expected_edges = tuple(item.final_evidence_edge for item in self.resolution.accepted)
        if self.graph.claims != expected_claims or self.graph.evidence_edges != expected_edges:
            raise ValueError("extracted graph differs from the exact reviewed claim resolution")
        protocol = self.resolution.execution.protocol
        if (
            self.graph.corpus_snapshot_sha256 != protocol.corpus_snapshot_sha256
            or self.graph.extraction_policy_sha256 != protocol.protocol_sha256
        ):
            raise ValueError("extracted graph is bound to another corpus or extraction protocol")
        if self.graph.frozen_at != self.built_at:
            raise ValueError("extracted graph freeze time must equal its bundle build time")
        return self

    @property
    def bundle_sha256(self) -> str:
        return content_sha256(self)


class CommittedExtractedAtomicClaimGraph(KnowledgeModel):
    schema_version: Literal[1] = 1
    bundle: ExtractedAtomicClaimGraphBundle
    ledger: ArchivedKnowledgeLedger

    @model_validator(mode="after")
    def _ledger_commits_graph_bundle(self) -> "CommittedExtractedAtomicClaimGraph":
        payload = canonical_json_bytes(self.bundle)
        if self.ledger.object_sha256 != self.bundle.bundle_sha256:
            raise ValueError("extracted graph ledger names another graph bundle")
        if self.ledger.ledger_sha256 != hashlib.sha256(payload).hexdigest():
            raise ValueError("extracted graph ledger hash does not commit its bundle")
        if self.ledger.ledger_bytes != len(payload):
            raise ValueError("extracted graph ledger size does not commit its bundle")
        return self


class ClaimReplayItem(KnowledgeModel):
    schema_version: Literal[1] = 1
    attempt_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    status: ClaimReplayItemStatus
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ClaimExtractionReplayAudit(KnowledgeModel):
    schema_version: Literal[1] = 1
    execution_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    items: tuple[ClaimReplayItem, ...] = Field(min_length=1)
    status: ClaimReplayStatus
    audited_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _status_matches_items(self) -> "ClaimExtractionReplayAudit":
        ids = [item.attempt_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("claim replay audit attempt IDs must be unique")
        statuses = {item.status for item in self.items}
        expected = (
            ClaimReplayStatus.MISMATCH
            if ClaimReplayItemStatus.MISMATCH in statuses
            else ClaimReplayStatus.INCOMPLETE
            if ClaimReplayItemStatus.UNAVAILABLE in statuses
            else ClaimReplayStatus.COMPLETE
        )
        if self.status is not expected:
            raise ValueError("claim replay audit status does not match its items")
        return self

    @property
    def audit_sha256(self) -> str:
        return content_sha256(self)


class _ExtractionBoundaryError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stage: ClaimExtractionStage,
        kind: ClaimExtractionFailureKind,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.kind = kind


def _normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _error_class(error: Exception) -> str:
    return f"{type(error).__module__}.{type(error).__qualname__}"[:256]


def _error_detail_sha256(
    error: Exception,
    *,
    stage: ClaimExtractionStage,
    kind: ClaimExtractionFailureKind,
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


def _claim_text_fields(draft: StructuredClaimDraft) -> tuple[str, ...]:
    values = [draft.subject, draft.relation, draft.object]
    values.extend(draft.qualifiers)
    if draft.population is not None:
        values.append(draft.population)
    values.extend(draft.conditions)
    if draft.quantitative_effect is not None:
        values.append(draft.quantitative_effect.unit)
    return tuple(values)


def _word_tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[^\W_]+(?:[-'][^\W_]+)*", value.casefold(), flags=re.UNICODE))


def _longest_source_run(field: str, source_text: str) -> int:
    field_tokens = _word_tokens(_normalize_text(field))
    source_tokens = _word_tokens(_normalize_text(source_text))
    if not field_tokens or not source_tokens:
        return 0
    longest = 0
    previous = [0] * (len(source_tokens) + 1)
    for field_token in field_tokens:
        current = [0]
        for index, source_token in enumerate(source_tokens, start=1):
            value = previous[index - 1] + 1 if field_token == source_token else 0
            current.append(value)
            longest = max(longest, value)
        previous = current
    return longest


def _enforce_structured_fact_policy(
    *, draft: StructuredClaimDraft, source_text: str, maximum_verbatim_word_run: int
) -> None:
    normalized_source = _normalize_text(source_text).casefold()
    for value in _claim_text_fields(draft):
        normalized_value = _normalize_text(value).casefold()
        if normalized_value == normalized_source:
            raise _ExtractionBoundaryError(
                "structured claim field reproduces the entire source span",
                stage=ClaimExtractionStage.STRUCTURED_OUTPUT,
                kind=ClaimExtractionFailureKind.OUTPUT_POLICY_VIOLATION,
            )
        if _longest_source_run(value, source_text) > maximum_verbatim_word_run:
            raise _ExtractionBoundaryError(
                "structured claim exceeds the frozen verbatim word-run limit",
                stage=ClaimExtractionStage.STRUCTURED_OUTPUT,
                kind=ClaimExtractionFailureKind.OUTPUT_POLICY_VIOLATION,
            )


def _claim_from_draft(
    *,
    draft: StructuredClaimDraft,
    claim_id: str,
    paper_snapshot_sha256: str,
    asserted_at: datetime,
) -> AtomicClaim:
    return AtomicClaim(
        claim_id=claim_id,
        origin=ClaimOrigin.PRIOR_ART,
        subject=draft.subject,
        relation=draft.relation,
        object=draft.object,
        qualifiers=draft.qualifiers,
        population=draft.population,
        conditions=draft.conditions,
        direction=draft.direction,
        claim_type=draft.claim_type,
        quantitative_effect=draft.quantitative_effect,
        source_paper_snapshot_sha256=paper_snapshot_sha256,
        asserted_at=asserted_at,
    )


def _review_reasons(
    *,
    draft: StructuredClaimDraft,
    span: SourceSpan,
    protocol: ClaimExtractionProtocol,
) -> tuple[ClaimReviewReason, ...]:
    reasons: list[ClaimReviewReason] = []
    if span.extraction_method is ExtractionMethod.OCR:
        reasons.append(ClaimReviewReason.OCR_SOURCE)
    if span.verification_status is EvidenceReviewStatus.UNREVIEWED:
        reasons.append(ClaimReviewReason.UNVERIFIED_SOURCE)
    if span.extraction_confidence < protocol.minimum_auto_source_confidence:
        reasons.append(ClaimReviewReason.LOW_SOURCE_CONFIDENCE)
    if draft.claim_confidence < protocol.minimum_auto_claim_confidence:
        reasons.append(ClaimReviewReason.LOW_CLAIM_CONFIDENCE)
    if draft.evidence_confidence < protocol.minimum_auto_evidence_confidence:
        reasons.append(ClaimReviewReason.LOW_EVIDENCE_CONFIDENCE)
    if (
        draft.quantitative_grounding_confidence is not None
        and draft.quantitative_grounding_confidence < protocol.minimum_auto_quantitative_confidence
    ):
        reasons.append(ClaimReviewReason.LOW_QUANTITATIVE_CONFIDENCE)
    return tuple(sorted(set(reasons), key=_REVIEW_REASON_ORDER.index))


def _candidate_from_draft(
    *,
    attempt_id: str,
    protocol: ClaimExtractionProtocol,
    manifest: ClaimExtractorManifest,
    span: SourceSpan,
    grant: ContentAccessGrant,
    draft: StructuredClaimDraft,
    asserted_at: datetime,
) -> ClaimExtractionCandidate:
    claim = _claim_from_draft(
        draft=draft,
        claim_id=f"prior:{span.span_sha256[:24]}:{draft.local_claim_id}",
        paper_snapshot_sha256=span.paper_snapshot_sha256,
        asserted_at=asserted_at,
    )
    reasons = _review_reasons(draft=draft, span=span, protocol=protocol)
    edge = ClaimEvidenceEdge(
        claim_sha256=claim.claim_sha256,
        source_span_sha256=span.span_sha256,
        relation=draft.evidence_relation,
        extraction_confidence=draft.evidence_confidence,
        reviewer_status=EvidenceReviewStatus.UNREVIEWED,
    )
    return ClaimExtractionCandidate(
        attempt_id=attempt_id,
        protocol_sha256=protocol.protocol_sha256,
        extractor_manifest_sha256=manifest.manifest_sha256,
        source_span_sha256=span.span_sha256,
        access_grant_sha256=grant.grant_sha256,
        draft=draft,
        claim=claim,
        evidence_edge=edge,
        disposition=(
            ClaimCandidateDisposition.REVIEW_REQUIRED
            if reasons
            else ClaimCandidateDisposition.AUTO_ACCEPTED
        ),
        review_reasons=reasons,
    )


def _review_package_sha256(
    *,
    protocol: ClaimExtractionProtocol,
    candidate: ClaimExtractionCandidate,
    content_receipt: SpanContentReceipt,
) -> str:
    return content_sha256(
        {
            "policy": "f8s3-claim-review-evidence-package-v1",
            "protocol_sha256": protocol.protocol_sha256,
            "candidate_sha256": candidate.candidate_sha256,
            "source_span_sha256": candidate.source_span_sha256,
            "access_grant_sha256": candidate.access_grant_sha256,
            "content_receipt_sha256": content_receipt.receipt_sha256,
            "exact_text_sha256": content_receipt.exact_text_sha256,
        }
    )


def _required_uses(manifest: ClaimExtractorManifest) -> tuple[ContentUse, ...]:
    if manifest.runtime is ClaimExtractorRuntime.MODEL:
        return (ContentUse.SPAN_EXTRACTION, ContentUse.MODEL_INPUT)
    return (ContentUse.SPAN_EXTRACTION,)


def _request_id(execution_id: str, ordinal: int) -> str:
    return f"{execution_id}:span:{ordinal:05d}"


def _classify_error(
    error: Exception, *, default_stage: ClaimExtractionStage
) -> tuple[ClaimExtractionStage, ClaimExtractionFailureKind]:
    if isinstance(error, _ExtractionBoundaryError):
        return error.stage, error.kind
    if default_stage is ClaimExtractionStage.CONTENT_RESOLUTION:
        return default_stage, ClaimExtractionFailureKind.CONTENT_UNAVAILABLE
    if default_stage is ClaimExtractionStage.EXTRACTOR:
        return default_stage, ClaimExtractionFailureKind.EXTRACTOR_ERROR
    return default_stage, ClaimExtractionFailureKind.OUTPUT_SCHEMA_ERROR


class ClaimExtractionExecutor:
    """Run every frozen span target and retain a complete, text-free derivation ledger."""

    def __init__(
        self,
        *,
        bundle: CorpusIngestionBundle,
        resolver: SpanContentResolver,
        extractors: Mapping[str, ClaimExtractor],
        archive: ContentAddressedResponseArchive | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.bundle = bundle
        self.resolver = resolver
        self.extractors = dict(extractors)
        self.archive = archive
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("claim extraction clock must return timezone-aware datetimes")
        return value

    def _preflight(
        self, protocol: ClaimExtractionProtocol
    ) -> tuple[
        dict[str, ClaimExtractorManifest],
        dict[str, SourceSpan],
        dict[str, ContentAccessGrant],
        dict[str, datetime],
    ]:
        if protocol.ingestion_bundle_sha256 != self.bundle.bundle_sha256:
            raise ValueError("claim extraction protocol is bound to another ingestion bundle")
        if protocol.corpus_snapshot_sha256 != self.bundle.corpus.snapshot_sha256:
            raise ValueError("claim extraction protocol is bound to another corpus")
        if protocol.access_policy_sha256 != self.bundle.access_policy.policy_sha256:
            raise ValueError("claim extraction protocol is bound to another access policy")
        manifests = {manifest.manifest_sha256: manifest for manifest in protocol.extractors}
        if set(self.extractors) != set(manifests):
            raise ValueError("runtime extractors must exactly match frozen extractor manifests")
        for manifest_sha256, manifest in manifests.items():
            if self.extractors[manifest_sha256].manifest != manifest:
                raise ValueError("runtime extractor manifest differs from the frozen protocol")
        spans = {span.span_sha256: span for span in self.bundle.corpus.spans}
        missing = {target.source_span_sha256 for target in protocol.targets} - set(spans)
        if missing:
            raise ValueError("claim extraction protocol targets spans outside the corpus")
        grants = {grant.paper_snapshot_sha256: grant for grant in self.bundle.access_grants}
        paper_times = {
            paper.snapshot_sha256: paper.version_public_at for paper in self.bundle.corpus.papers
        }
        return manifests, spans, grants, paper_times

    def _request(
        self,
        *,
        execution_id: str,
        protocol: ClaimExtractionProtocol,
        target: ClaimExtractionTarget,
        span: SourceSpan,
        grant: ContentAccessGrant,
        manifest: ClaimExtractorManifest,
        issued_at: datetime,
    ) -> ClaimExtractionRequest:
        return ClaimExtractionRequest(
            request_id=_request_id(execution_id, target.ordinal),
            protocol_sha256=protocol.protocol_sha256,
            target_sha256=target.target_sha256,
            source_span_sha256=span.span_sha256,
            access_grant_sha256=grant.grant_sha256,
            extractor_manifest_sha256=manifest.manifest_sha256,
            exact_text_sha256=span.exact_text_sha256,
            required_uses=_required_uses(manifest),
            issued_at=issued_at,
        )

    def _authorize(
        self,
        *,
        grant: ContentAccessGrant,
        span: SourceSpan,
        request: ClaimExtractionRequest,
    ) -> None:
        if span.verification_status is EvidenceReviewStatus.REJECTED:
            raise _ExtractionBoundaryError(
                "rejected source spans cannot enter claim extraction",
                stage=ClaimExtractionStage.ACCESS,
                kind=ClaimExtractionFailureKind.SOURCE_SPAN_REJECTED,
            )
        if grant.paper_snapshot_sha256 != span.paper_snapshot_sha256:
            raise _ExtractionBoundaryError(
                "content grant is bound to another source paper",
                stage=ClaimExtractionStage.ACCESS,
                kind=ClaimExtractionFailureKind.ACCESS_DENIED,
            )
        if not set(request.required_uses).issubset(set(grant.permitted_uses)):
            raise _ExtractionBoundaryError(
                "content grant lacks an explicit extraction or model-input use",
                stage=ClaimExtractionStage.ACCESS,
                kind=ClaimExtractionFailureKind.ACCESS_DENIED,
            )
        if grant.expires_at is not None and request.issued_at >= grant.expires_at:
            raise _ExtractionBoundaryError(
                "content access grant expired before claim extraction",
                stage=ClaimExtractionStage.ACCESS,
                kind=ClaimExtractionFailureKind.ACCESS_EXPIRED,
            )

    def _verify_content(
        self,
        *,
        protocol: ClaimExtractionProtocol,
        manifest: ClaimExtractorManifest,
        span: SourceSpan,
        grant: ContentAccessGrant,
        request: ClaimExtractionRequest,
        content: EphemeralSpanContent,
    ) -> tuple[str, SpanContentReceipt]:
        if content.content_normalizer_sha256 != protocol.content_normalizer_sha256:
            raise _ExtractionBoundaryError(
                "source content resolver used another canonicalizer",
                stage=ClaimExtractionStage.CONTENT_VERIFICATION,
                kind=ClaimExtractionFailureKind.CONTENT_IDENTITY_MISMATCH,
            )
        if not isinstance(content.document_bytes, bytes) or not isinstance(
            content.exact_span_bytes, bytes
        ):
            raise _ExtractionBoundaryError(
                "ephemeral source content must be exact bytes",
                stage=ClaimExtractionStage.CONTENT_VERIFICATION,
                kind=ClaimExtractionFailureKind.CONTENT_IDENTITY_MISMATCH,
            )
        if (
            not content.document_bytes
            or len(content.document_bytes) > protocol.maximum_document_bytes
        ):
            raise _ExtractionBoundaryError(
                "canonical source document is empty or exceeds the frozen byte limit",
                stage=ClaimExtractionStage.CONTENT_VERIFICATION,
                kind=ClaimExtractionFailureKind.CONTENT_IDENTITY_MISMATCH,
            )
        if (
            not content.exact_span_bytes
            or len(content.exact_span_bytes) > manifest.maximum_span_bytes
        ):
            raise _ExtractionBoundaryError(
                "source span is empty or exceeds the extractor byte limit",
                stage=ClaimExtractionStage.CONTENT_VERIFICATION,
                kind=ClaimExtractionFailureKind.SPAN_IDENTITY_MISMATCH,
            )
        document_sha256 = _sha256_bytes(content.document_bytes)
        if (
            content.paper_snapshot_sha256 != span.paper_snapshot_sha256
            or grant.content_sha256 is None
            or document_sha256 != grant.content_sha256
        ):
            raise _ExtractionBoundaryError(
                "canonical source document does not match its access grant",
                stage=ClaimExtractionStage.CONTENT_VERIFICATION,
                kind=ClaimExtractionFailureKind.CONTENT_IDENTITY_MISMATCH,
            )
        exact_sha256 = _sha256_bytes(content.exact_span_bytes)
        if (
            exact_sha256 != span.exact_text_sha256
            or len(content.exact_span_bytes) != span.text_bytes
        ):
            raise _ExtractionBoundaryError(
                "ephemeral source span does not match its exact identity",
                stage=ClaimExtractionStage.CONTENT_VERIFICATION,
                kind=ClaimExtractionFailureKind.SPAN_IDENTITY_MISMATCH,
            )
        try:
            document_text = content.document_bytes.decode("utf-8", errors="strict")
            source_text = content.exact_span_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise _ExtractionBoundaryError(
                "canonical source content is not strict UTF-8",
                stage=ClaimExtractionStage.CONTENT_VERIFICATION,
                kind=ClaimExtractionFailureKind.SPAN_IDENTITY_MISMATCH,
            ) from error
        normalized_sha256 = _sha256_bytes(_normalize_text(source_text).encode("utf-8"))
        if (
            normalized_sha256 != span.normalized_text_sha256
            or normalized_sha256 != span.locator.normalized_span_sha256
        ):
            raise _ExtractionBoundaryError(
                "ephemeral source span does not match its normalized identity",
                stage=ClaimExtractionStage.CONTENT_VERIFICATION,
                kind=ClaimExtractionFailureKind.SPAN_IDENTITY_MISMATCH,
            )
        if span.locator.char_start is not None:
            assert span.locator.char_end is not None
            located = document_text[span.locator.char_start : span.locator.char_end]
            if located != source_text:
                raise _ExtractionBoundaryError(
                    "source span bytes do not match the frozen character locator",
                    stage=ClaimExtractionStage.CONTENT_VERIFICATION,
                    kind=ClaimExtractionFailureKind.SPAN_IDENTITY_MISMATCH,
                )
        elif source_text not in document_text:
            raise _ExtractionBoundaryError(
                "source span is absent from the canonical source document",
                stage=ClaimExtractionStage.CONTENT_VERIFICATION,
                kind=ClaimExtractionFailureKind.SPAN_IDENTITY_MISMATCH,
            )
        authorization_evidence_sha256 = content_sha256(
            {
                "policy": "f8s3-explicit-content-use-v1",
                "grant_sha256": grant.grant_sha256,
                "request_sha256": request.request_sha256,
                "required_uses": [use.value for use in request.required_uses],
                "document_content_sha256": document_sha256,
                "exact_text_sha256": exact_sha256,
            }
        )
        return source_text, SpanContentReceipt(
            request_sha256=request.request_sha256,
            source_span_sha256=span.span_sha256,
            access_grant_sha256=grant.grant_sha256,
            paper_snapshot_sha256=span.paper_snapshot_sha256,
            document_content_sha256=document_sha256,
            document_bytes=len(content.document_bytes),
            exact_text_sha256=exact_sha256,
            normalized_text_sha256=normalized_sha256,
            span_bytes=len(content.exact_span_bytes),
            required_uses=request.required_uses,
            authorization_evidence_sha256=authorization_evidence_sha256,
            accessed_at=request.issued_at,
        )

    def _failure(
        self,
        *,
        target: ClaimExtractionTarget,
        request: ClaimExtractionRequest,
        started_at: datetime,
        content_receipt: SpanContentReceipt | None,
        error: Exception,
        default_stage: ClaimExtractionStage,
    ) -> tuple[ClaimExtractionAttempt, ClaimExtractionFailure]:
        occurred_at = self._now()
        stage, kind = _classify_error(error, default_stage=default_stage)
        failure = ClaimExtractionFailure(
            attempt_id=request.request_id,
            target_sha256=target.target_sha256,
            source_span_sha256=target.source_span_sha256,
            extractor_manifest_sha256=target.extractor_manifest_sha256,
            stage=stage,
            kind=kind,
            error_class=_error_class(error),
            error_detail_sha256=_error_detail_sha256(error, stage=stage, kind=kind),
            occurred_at=occurred_at,
        )
        attempt = ClaimExtractionAttempt(
            attempt_id=request.request_id,
            target_sha256=target.target_sha256,
            request=request,
            outcome=ClaimExtractionOutcome.ERROR,
            content_receipt=content_receipt,
            candidate_sha256s=(),
            failure_sha256=failure.failure_sha256,
            started_at=started_at,
            completed_at=occurred_at,
        )
        return attempt, failure

    async def execute(
        self, *, protocol: ClaimExtractionProtocol, execution_id: str
    ) -> ClaimExtractionExecution:
        manifests, spans, grants, paper_times = self._preflight(protocol)
        started_at = self._now()
        attempts: list[ClaimExtractionAttempt] = []
        candidates: list[ClaimExtractionCandidate] = []
        failures: list[ClaimExtractionFailure] = []
        review_tasks: list[ClaimReviewTask] = []

        for target in protocol.targets:
            attempt_started = self._now()
            span = spans[target.source_span_sha256]
            grant = grants[span.paper_snapshot_sha256]
            manifest = manifests[target.extractor_manifest_sha256]
            request = self._request(
                execution_id=execution_id,
                protocol=protocol,
                target=target,
                span=span,
                grant=grant,
                manifest=manifest,
                issued_at=attempt_started,
            )
            try:
                self._authorize(grant=grant, span=span, request=request)
            except Exception as error:
                attempt, failure = self._failure(
                    target=target,
                    request=request,
                    started_at=attempt_started,
                    content_receipt=None,
                    error=error,
                    default_stage=ClaimExtractionStage.ACCESS,
                )
                attempts.append(attempt)
                failures.append(failure)
                continue
            try:
                content_value = await self.resolver.resolve(span=span, grant=grant)
                content = (
                    content_value
                    if isinstance(content_value, EphemeralSpanContent)
                    else EphemeralSpanContent(**content_value)
                )
            except Exception as error:
                attempt, failure = self._failure(
                    target=target,
                    request=request,
                    started_at=attempt_started,
                    content_receipt=None,
                    error=error,
                    default_stage=ClaimExtractionStage.CONTENT_RESOLUTION,
                )
                attempts.append(attempt)
                failures.append(failure)
                continue
            try:
                source_text, content_receipt = self._verify_content(
                    protocol=protocol,
                    manifest=manifest,
                    span=span,
                    grant=grant,
                    request=request,
                    content=content,
                )
            except Exception as error:
                attempt, failure = self._failure(
                    target=target,
                    request=request,
                    started_at=attempt_started,
                    content_receipt=None,
                    error=error,
                    default_stage=ClaimExtractionStage.CONTENT_VERIFICATION,
                )
                attempts.append(attempt)
                failures.append(failure)
                continue
            try:
                raw_output = await self.extractors[manifest.manifest_sha256].extract(
                    request=request,
                    source_text=source_text,
                )
            except Exception as error:
                attempt, failure = self._failure(
                    target=target,
                    request=request,
                    started_at=attempt_started,
                    content_receipt=content_receipt,
                    error=error,
                    default_stage=ClaimExtractionStage.EXTRACTOR,
                )
                attempts.append(attempt)
                failures.append(failure)
                continue
            try:
                output = StructuredClaimBatch.model_validate(raw_output)
                if output.request_sha256 != request.request_sha256:
                    raise _ExtractionBoundaryError(
                        "structured output is bound to another extraction request",
                        stage=ClaimExtractionStage.STRUCTURED_OUTPUT,
                        kind=ClaimExtractionFailureKind.OUTPUT_BINDING_ERROR,
                    )
                if output.source_span_sha256 != span.span_sha256:
                    raise _ExtractionBoundaryError(
                        "structured output is bound to another source span",
                        stage=ClaimExtractionStage.STRUCTURED_OUTPUT,
                        kind=ClaimExtractionFailureKind.OUTPUT_BINDING_ERROR,
                    )
                if len(output.claims) > manifest.maximum_claims_per_span:
                    raise _ExtractionBoundaryError(
                        "structured output exceeds the frozen per-span claim limit",
                        stage=ClaimExtractionStage.STRUCTURED_OUTPUT,
                        kind=ClaimExtractionFailureKind.OUTPUT_POLICY_VIOLATION,
                    )
                target_candidates: list[ClaimExtractionCandidate] = []
                for draft in output.claims:
                    if draft.claim_type not in set(manifest.supported_claim_types):
                        raise _ExtractionBoundaryError(
                            "structured output claim type is outside the extractor manifest",
                            stage=ClaimExtractionStage.STRUCTURED_OUTPUT,
                            kind=ClaimExtractionFailureKind.OUTPUT_POLICY_VIOLATION,
                        )
                    _enforce_structured_fact_policy(
                        draft=draft,
                        source_text=source_text,
                        maximum_verbatim_word_run=protocol.maximum_verbatim_word_run,
                    )
                    target_candidates.append(
                        _candidate_from_draft(
                            attempt_id=request.request_id,
                            protocol=protocol,
                            manifest=manifest,
                            span=span,
                            grant=grant,
                            draft=draft,
                            asserted_at=paper_times[span.paper_snapshot_sha256],
                        )
                    )
                candidate_hashes = [item.candidate_sha256 for item in target_candidates]
                if len(candidate_hashes) != len(set(candidate_hashes)):
                    raise _ExtractionBoundaryError(
                        "structured output produced duplicate claim candidates",
                        stage=ClaimExtractionStage.STRUCTURED_OUTPUT,
                        kind=ClaimExtractionFailureKind.OUTPUT_POLICY_VIOLATION,
                    )
            except Exception as error:
                attempt, failure = self._failure(
                    target=target,
                    request=request,
                    started_at=attempt_started,
                    content_receipt=content_receipt,
                    error=error,
                    default_stage=ClaimExtractionStage.STRUCTURED_OUTPUT,
                )
                attempts.append(attempt)
                failures.append(failure)
                continue

            completed_at = self._now()
            attempts.append(
                ClaimExtractionAttempt(
                    attempt_id=request.request_id,
                    target_sha256=target.target_sha256,
                    request=request,
                    outcome=ClaimExtractionOutcome.SUCCESS,
                    content_receipt=content_receipt,
                    structured_output=output,
                    candidate_sha256s=tuple(candidate_hashes),
                    started_at=attempt_started,
                    completed_at=completed_at,
                )
            )
            candidates.extend(target_candidates)
            for candidate in target_candidates:
                if candidate.disposition is ClaimCandidateDisposition.REVIEW_REQUIRED:
                    review_tasks.append(
                        ClaimReviewTask(
                            candidate_sha256=candidate.candidate_sha256,
                            attempt_id=candidate.attempt_id,
                            source_span_sha256=candidate.source_span_sha256,
                            reasons=candidate.review_reasons,
                            evidence_package_sha256=_review_package_sha256(
                                protocol=protocol,
                                candidate=candidate,
                                content_receipt=content_receipt,
                            ),
                        )
                    )

        completed_at = self._now()
        queue = ClaimReviewQueue(
            protocol_sha256=protocol.protocol_sha256,
            tasks=tuple(review_tasks),
        )
        disposition = (
            ClaimExtractionDisposition.BLOCKED
            if failures
            else ClaimExtractionDisposition.PENDING_REVIEW
            if review_tasks
            else ClaimExtractionDisposition.READY_FOR_GRAPH
        )
        return ClaimExtractionExecution(
            execution_id=execution_id,
            protocol=protocol,
            attempts=tuple(attempts),
            candidates=tuple(candidates),
            failures=tuple(failures),
            review_queue=queue,
            disposition=disposition,
            started_at=started_at,
            completed_at=completed_at,
        )

    async def execute_and_commit(
        self, *, protocol: ClaimExtractionProtocol, execution_id: str
    ) -> CommittedClaimExtraction:
        if self.archive is None:
            raise ValueError("claim extraction commit requires a content-addressed archive")
        execution = await self.execute(protocol=protocol, execution_id=execution_id)
        ledger = self.archive.store_ledger(
            value=execution,
            object_sha256=execution.execution_sha256,
            archived_at=execution.completed_at,
        )
        return CommittedClaimExtraction(execution=execution, ledger=ledger)


def load_claim_extraction(
    *, archive: ContentAddressedResponseArchive, ledger: ArchivedKnowledgeLedger
) -> ClaimExtractionExecution:
    payload = archive.read_ledger(ledger)
    execution = ClaimExtractionExecution.model_validate_json(payload)
    canonical = canonical_json_bytes(execution)
    if canonical != payload:
        raise ValueError("archived claim extraction is not canonical JSON")
    if execution.execution_sha256 != ledger.object_sha256:
        raise ValueError("archived claim extraction has another object identity")
    return execution


def _claim_replay_evidence(
    *, attempt_id: str, status: ClaimReplayItemStatus, detail: object
) -> str:
    return content_sha256(
        {
            "policy": "f8s3-claim-derivation-replay-v1",
            "attempt_id": attempt_id,
            "status": status.value,
            "detail": detail,
        }
    )


async def replay_claim_extraction(
    *,
    execution: ClaimExtractionExecution,
    bundle: CorpusIngestionBundle,
    resolver: SpanContentResolver,
    extractors: Mapping[str, ClaimExtractor],
    audited_at: AwareDatetime,
) -> ClaimExtractionReplayAudit:
    """Revalidate licensed input identities and replay stored structured derivations.

    The extractor transport is intentionally not called again. Model sampling is not assumed to be
    deterministic; replay proves that the frozen structured output still derives the exact claim
    candidates from the same authorized span bytes and frozen manifest.
    """

    protocol = execution.protocol
    if (
        protocol.ingestion_bundle_sha256 != bundle.bundle_sha256
        or protocol.corpus_snapshot_sha256 != bundle.corpus.snapshot_sha256
        or protocol.access_policy_sha256 != bundle.access_policy.policy_sha256
    ):
        raise ValueError("claim replay bundle differs from the frozen extraction protocol")
    manifests = {manifest.manifest_sha256: manifest for manifest in protocol.extractors}
    extractor_map = dict(extractors)
    if set(extractor_map) != set(manifests):
        raise ValueError("claim replay extractors must exactly match frozen manifests")
    spans = {span.span_sha256: span for span in bundle.corpus.spans}
    grants = {grant.paper_snapshot_sha256: grant for grant in bundle.access_grants}
    paper_times = {paper.snapshot_sha256: paper.version_public_at for paper in bundle.corpus.papers}
    candidates_by_attempt: dict[str, list[ClaimExtractionCandidate]] = {}
    for candidate in execution.candidates:
        candidates_by_attempt.setdefault(candidate.attempt_id, []).append(candidate)
    verifier = ClaimExtractionExecutor(
        bundle=bundle,
        resolver=resolver,
        extractors=extractor_map,
    )
    items: list[ClaimReplayItem] = []

    for target, attempt in zip(protocol.targets, execution.attempts, strict=True):
        manifest = manifests[target.extractor_manifest_sha256]
        adapter = extractor_map[manifest.manifest_sha256]
        if adapter.manifest != manifest:
            item_status = ClaimReplayItemStatus.MISMATCH
            detail: object = "extractor_manifest_drift"
        elif attempt.outcome is ClaimExtractionOutcome.ERROR:
            item_status = ClaimReplayItemStatus.UNAVAILABLE
            detail = "failed_attempt_has_no_accepted_structured_output"
        else:
            span = spans[target.source_span_sha256]
            grant = grants[span.paper_snapshot_sha256]
            expected_request = verifier._request(
                execution_id=execution.execution_id,
                protocol=protocol,
                target=target,
                span=span,
                grant=grant,
                manifest=manifest,
                issued_at=attempt.request.issued_at,
            )
            if expected_request != attempt.request:
                item_status = ClaimReplayItemStatus.MISMATCH
                detail = "stored_request_derivation_mismatch"
            else:
                try:
                    verifier._authorize(grant=grant, span=span, request=attempt.request)
                    content_value = await resolver.resolve(span=span, grant=grant)
                    content = (
                        content_value
                        if isinstance(content_value, EphemeralSpanContent)
                        else EphemeralSpanContent(**content_value)
                    )
                    source_text, replay_receipt = verifier._verify_content(
                        protocol=protocol,
                        manifest=manifest,
                        span=span,
                        grant=grant,
                        request=attempt.request,
                        content=content,
                    )
                except _ExtractionBoundaryError as error:
                    item_status = ClaimReplayItemStatus.MISMATCH
                    detail = {
                        "boundary_kind": error.kind.value,
                        "error_detail_sha256": _error_detail_sha256(
                            error, stage=error.stage, kind=error.kind
                        ),
                    }
                except Exception as error:
                    item_status = ClaimReplayItemStatus.UNAVAILABLE
                    detail = {
                        "resolver_error_class": _error_class(error),
                        "resolver_error_sha256": hashlib.sha256(
                            str(error).encode("utf-8", errors="replace")
                        ).hexdigest(),
                    }
                else:
                    assert attempt.structured_output is not None
                    expected_candidates: list[ClaimExtractionCandidate] = []
                    try:
                        for draft in attempt.structured_output.claims:
                            _enforce_structured_fact_policy(
                                draft=draft,
                                source_text=source_text,
                                maximum_verbatim_word_run=protocol.maximum_verbatim_word_run,
                            )
                            expected_candidates.append(
                                _candidate_from_draft(
                                    attempt_id=attempt.attempt_id,
                                    protocol=protocol,
                                    manifest=manifest,
                                    span=span,
                                    grant=grant,
                                    draft=draft,
                                    asserted_at=paper_times[span.paper_snapshot_sha256],
                                )
                            )
                    except Exception as error:
                        item_status = ClaimReplayItemStatus.MISMATCH
                        detail = {
                            "derivation_error_class": _error_class(error),
                            "derivation_error_sha256": hashlib.sha256(
                                str(error).encode("utf-8", errors="replace")
                            ).hexdigest(),
                        }
                    else:
                        stored_candidates = candidates_by_attempt.get(attempt.attempt_id, [])
                        matches = (
                            attempt.content_receipt == replay_receipt
                            and expected_candidates == stored_candidates
                            and tuple(item.candidate_sha256 for item in expected_candidates)
                            == attempt.candidate_sha256s
                        )
                        item_status = (
                            ClaimReplayItemStatus.VERIFIED
                            if matches
                            else ClaimReplayItemStatus.MISMATCH
                        )
                        detail = {
                            "content_receipt_sha256": replay_receipt.receipt_sha256,
                            "candidate_sha256s": [
                                candidate.candidate_sha256 for candidate in expected_candidates
                            ],
                        }
        items.append(
            ClaimReplayItem(
                attempt_id=attempt.attempt_id,
                status=item_status,
                evidence_sha256=_claim_replay_evidence(
                    attempt_id=attempt.attempt_id,
                    status=item_status,
                    detail=detail,
                ),
            )
        )

    statuses = {item.status for item in items}
    status = (
        ClaimReplayStatus.MISMATCH
        if ClaimReplayItemStatus.MISMATCH in statuses
        else ClaimReplayStatus.INCOMPLETE
        if ClaimReplayItemStatus.UNAVAILABLE in statuses
        else ClaimReplayStatus.COMPLETE
    )
    return ClaimExtractionReplayAudit(
        execution_sha256=execution.execution_sha256,
        items=tuple(items),
        status=status,
        audited_at=audited_at,
    )


def _reviewed_edge(
    *,
    claim: AtomicClaim,
    source_span_sha256: str,
    relation: ClaimEvidenceRelation,
    confidence: float,
    review: ClaimCandidateReview,
) -> ClaimEvidenceEdge:
    status = (
        EvidenceReviewStatus.HUMAN_VERIFIED
        if review.reviewer_kind is ClaimReviewKind.HUMAN
        else EvidenceReviewStatus.SECOND_MODEL_VERIFIED
    )
    return ClaimEvidenceEdge(
        claim_sha256=claim.claim_sha256,
        source_span_sha256=source_span_sha256,
        relation=relation,
        extraction_confidence=confidence,
        reviewer_status=status,
        reviewer_principal_sha256=review.reviewer_principal_sha256,
        reviewed_at=review.reviewed_at,
    )


def _resolved_candidate_from_review(
    *,
    candidate: ClaimExtractionCandidate,
    review: ClaimCandidateReview,
) -> ResolvedClaimCandidate:
    if review.decision is ClaimReviewDecision.REJECT:
        raise ValueError("rejected claim reviews do not produce resolved candidates")
    if review.decision is ClaimReviewDecision.REVISE:
        assert review.replacement_draft is not None
        paper_snapshot_sha256 = candidate.claim.source_paper_snapshot_sha256
        assert paper_snapshot_sha256 is not None
        final_claim = _claim_from_draft(
            draft=review.replacement_draft,
            claim_id=f"{candidate.claim.claim_id}:r:{review.review_sha256[:12]}",
            paper_snapshot_sha256=paper_snapshot_sha256,
            asserted_at=candidate.claim.asserted_at,
        )
        relation = review.replacement_draft.evidence_relation
        confidence = review.replacement_draft.evidence_confidence
        decision = ClaimResolutionDecision.REVIEW_REVISE
    else:
        final_claim = candidate.claim
        relation = candidate.evidence_edge.relation
        confidence = candidate.evidence_edge.extraction_confidence
        decision = ClaimResolutionDecision.REVIEW_ACCEPT
    return ResolvedClaimCandidate(
        original_candidate_sha256=candidate.candidate_sha256,
        decision=decision,
        final_claim=final_claim,
        final_evidence_edge=_reviewed_edge(
            claim=final_claim,
            source_span_sha256=candidate.source_span_sha256,
            relation=relation,
            confidence=confidence,
            review=review,
        ),
        review_sha256=review.review_sha256,
    )


def resolve_claim_extraction(
    *,
    execution: ClaimExtractionExecution,
    reviews: tuple[ClaimCandidateReview, ...],
    resolution_id: str,
    resolved_at: AwareDatetime,
) -> ClaimExtractionResolution:
    if execution.failures:
        raise ValueError("blocked extraction executions cannot be resolved")
    review_map = {review.candidate_sha256: review for review in reviews}
    if len(review_map) != len(reviews):
        raise ValueError("claim reviews cannot repeat a candidate")
    tasks = {task.candidate_sha256: task for task in execution.review_queue.tasks}
    if set(review_map) != set(tasks):
        raise ValueError("claim reviews must exactly cover the frozen review queue")
    accepted: list[ResolvedClaimCandidate] = []
    rejected: list[str] = []
    for candidate in execution.candidates:
        review = review_map.get(candidate.candidate_sha256)
        if review is None:
            accepted.append(
                ResolvedClaimCandidate(
                    original_candidate_sha256=candidate.candidate_sha256,
                    decision=ClaimResolutionDecision.AUTO_ACCEPT,
                    final_claim=candidate.claim,
                    final_evidence_edge=candidate.evidence_edge,
                )
            )
            continue
        task = tasks[candidate.candidate_sha256]
        if review.evidence_package_sha256 != task.evidence_package_sha256:
            raise ValueError("claim review evidence-package identity mismatch")
        if review.decision is ClaimReviewDecision.REJECT:
            rejected.append(candidate.candidate_sha256)
            continue
        accepted.append(_resolved_candidate_from_review(candidate=candidate, review=review))
    return ClaimExtractionResolution(
        resolution_id=resolution_id,
        execution=execution,
        reviews=reviews,
        accepted=tuple(accepted),
        rejected_candidate_sha256s=tuple(rejected),
        resolved_at=resolved_at,
    )


def commit_claim_extraction_resolution(
    *,
    archive: ContentAddressedResponseArchive,
    resolution: ClaimExtractionResolution,
) -> CommittedClaimExtractionResolution:
    ledger = archive.store_ledger(
        value=resolution,
        object_sha256=resolution.resolution_sha256,
        archived_at=resolution.resolved_at,
    )
    return CommittedClaimExtractionResolution(resolution=resolution, ledger=ledger)


def load_claim_extraction_resolution(
    *, archive: ContentAddressedResponseArchive, ledger: ArchivedKnowledgeLedger
) -> ClaimExtractionResolution:
    payload = archive.read_ledger(ledger)
    resolution = ClaimExtractionResolution.model_validate_json(payload)
    canonical = canonical_json_bytes(resolution)
    if canonical != payload:
        raise ValueError("archived claim resolution is not canonical JSON")
    if resolution.resolution_sha256 != ledger.object_sha256:
        raise ValueError("archived claim resolution has another object identity")
    return resolution


def build_extracted_atomic_claim_graph(
    *,
    resolution: ClaimExtractionResolution,
    candidate_claims: tuple[AtomicClaim, ...],
    graph_id: str,
    frozen_at: AwareDatetime,
) -> AtomicClaimGraph:
    if not candidate_claims or any(
        claim.origin is not ClaimOrigin.CANDIDATE for claim in candidate_claims
    ):
        raise ValueError("claim graph requires at least one candidate-origin claim")
    if not resolution.accepted:
        raise ValueError("claim graph requires at least one accepted prior-art claim")
    prior_claims = tuple(item.final_claim for item in resolution.accepted)
    evidence_edges = tuple(item.final_evidence_edge for item in resolution.accepted)
    return AtomicClaimGraph(
        graph_id=graph_id,
        corpus_snapshot_sha256=resolution.execution.protocol.corpus_snapshot_sha256,
        claims=(*candidate_claims, *prior_claims),
        evidence_edges=evidence_edges,
        extraction_policy_sha256=resolution.execution.protocol.protocol_sha256,
        frozen_at=frozen_at,
    )


def build_extracted_atomic_claim_graph_bundle(
    *,
    resolution: ClaimExtractionResolution,
    candidate_claims: tuple[AtomicClaim, ...],
    bundle_id: str,
    graph_id: str,
    built_at: AwareDatetime,
) -> ExtractedAtomicClaimGraphBundle:
    graph = build_extracted_atomic_claim_graph(
        resolution=resolution,
        candidate_claims=candidate_claims,
        graph_id=graph_id,
        frozen_at=built_at,
    )
    return ExtractedAtomicClaimGraphBundle(
        bundle_id=bundle_id,
        resolution=resolution,
        candidate_claims=candidate_claims,
        graph=graph,
        built_at=built_at,
    )


def commit_extracted_atomic_claim_graph(
    *,
    archive: ContentAddressedResponseArchive,
    bundle: ExtractedAtomicClaimGraphBundle,
) -> CommittedExtractedAtomicClaimGraph:
    ledger = archive.store_ledger(
        value=bundle,
        object_sha256=bundle.bundle_sha256,
        archived_at=bundle.built_at,
    )
    return CommittedExtractedAtomicClaimGraph(bundle=bundle, ledger=ledger)


def load_extracted_atomic_claim_graph(
    *, archive: ContentAddressedResponseArchive, ledger: ArchivedKnowledgeLedger
) -> ExtractedAtomicClaimGraphBundle:
    payload = archive.read_ledger(ledger)
    bundle = ExtractedAtomicClaimGraphBundle.model_validate_json(payload)
    canonical = canonical_json_bytes(bundle)
    if canonical != payload:
        raise ValueError("archived extracted graph bundle is not canonical JSON")
    if bundle.bundle_sha256 != ledger.object_sha256:
        raise ValueError("archived extracted graph bundle has another object identity")
    return bundle


__all__ = [
    "CANONICAL_TEXT_NORMALIZER_SHA256",
    "CLAIM_OUTPUT_SCHEMA_SHA256",
    "ClaimCandidateDisposition",
    "ClaimCandidateReview",
    "ClaimExtractionAttempt",
    "ClaimExtractionCandidate",
    "ClaimExtractionDisposition",
    "ClaimExtractionExecution",
    "ClaimExtractionExecutor",
    "ClaimExtractionFailure",
    "ClaimExtractionFailureKind",
    "ClaimExtractionOutcome",
    "ClaimExtractionProtocol",
    "ClaimExtractionReplayAudit",
    "ClaimExtractionRequest",
    "ClaimExtractionResolution",
    "ClaimExtractionStage",
    "ClaimExtractionTarget",
    "ClaimExtractor",
    "ClaimExtractorManifest",
    "ClaimExtractorRuntime",
    "ClaimReplayItem",
    "ClaimReplayItemStatus",
    "ClaimReplayStatus",
    "ClaimResolutionDecision",
    "ClaimReviewDecision",
    "ClaimReviewKind",
    "ClaimReviewQueue",
    "ClaimReviewReason",
    "ClaimReviewTask",
    "CommittedClaimExtraction",
    "CommittedClaimExtractionResolution",
    "CommittedExtractedAtomicClaimGraph",
    "EphemeralSpanContent",
    "ExtractedAtomicClaimGraphBundle",
    "ResolvedClaimCandidate",
    "SpanContentReceipt",
    "SpanContentResolver",
    "StructuredClaimBatch",
    "StructuredClaimDraft",
    "build_extracted_atomic_claim_graph",
    "build_extracted_atomic_claim_graph_bundle",
    "commit_claim_extraction_resolution",
    "commit_extracted_atomic_claim_graph",
    "load_claim_extraction",
    "load_claim_extraction_resolution",
    "load_extracted_atomic_claim_graph",
    "replay_claim_extraction",
    "resolve_claim_extraction",
]
