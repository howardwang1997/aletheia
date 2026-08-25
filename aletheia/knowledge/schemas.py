"""F8 knowledge-boundary schema spike.

These immutable contracts deliberately have no database, network-provider, or scheduler imports.
They define what later retrieval/extraction implementations must prove before a novelty or SOTA
claim can be admitted.  Issue 12 exercises them only with synthetic fixtures.
"""

from __future__ import annotations

import math
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from aletheia.reproducibility.manifest import content_sha256


class KnowledgeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PublicationType(str, Enum):
    PREPRINT = "preprint"
    JOURNAL_ARTICLE = "journal_article"
    CONFERENCE_PAPER = "conference_paper"
    DATASET = "dataset"
    REVIEW = "review"
    CORRECTION = "correction"
    RETRACTION = "retraction"
    OTHER = "other"


class PeerReviewStatus(str, Enum):
    UNKNOWN = "unknown"
    NOT_PEER_REVIEWED = "not_peer_reviewed"
    IN_REVIEW = "in_review"
    PEER_REVIEWED = "peer_reviewed"


class PaperStatus(str, Enum):
    ACTIVE = "active"
    CORRECTED = "corrected"
    RETRACTED = "retracted"
    WITHDRAWN = "withdrawn"


class TextAvailability(str, Enum):
    METADATA_ONLY = "metadata_only"
    ABSTRACT = "abstract"
    FULL_TEXT = "full_text"


class TextScope(str, Enum):
    ABSTRACT = "abstract"
    FULL_TEXT = "full_text"


class ExtractionMethod(str, Enum):
    ABSTRACT_API = "abstract_api"
    PUBLISHER_HTML = "publisher_html"
    PDF_TEXT = "pdf_text"
    GROBID = "grobid"
    OCR = "ocr"
    MANUAL = "manual"


class TemporalSnapshotMode(str, Enum):
    CONTEMPORANEOUS = "contemporaneous"
    RECONSTRUCTED = "reconstructed"


class PublicationUpdateType(str, Enum):
    CORRECTION = "correction"
    RETRACTION = "retraction"
    WITHDRAWAL = "withdrawal"
    EXPRESSION_OF_CONCERN = "expression_of_concern"


class CorpusSourceVersion(KnowledgeModel):
    schema_version: Literal[1] = 1
    source_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,79}$")
    snapshot_id: str = Field(min_length=1, max_length=256)
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    updated_through: AwareDatetime
    retrieved_at: AwareDatetime
    license_id: str = Field(min_length=1, max_length=256)
    terms_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    as_of_evidence_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _source_timeline_is_ordered(self) -> "CorpusSourceVersion":
        if self.retrieved_at < self.updated_through:
            raise ValueError("corpus source cannot be retrieved before its update boundary")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class PaperSnapshot(KnowledgeModel):
    schema_version: Literal[1] = 1
    canonical_id: str = Field(min_length=1, max_length=512)
    version_id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=4096)
    authors: tuple[str, ...] = Field(min_length=1)
    venue: str | None = Field(default=None, max_length=1024)
    publication_type: PublicationType
    first_public_at: AwareDatetime
    version_public_at: AwareDatetime
    observed_at: AwareDatetime
    doi: str | None = Field(default=None, max_length=512)
    source_urls: tuple[str, ...] = Field(min_length=1)
    metadata_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text_availability: TextAvailability
    text_content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    license_id: str = Field(min_length=1, max_length=256)
    license_terms_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    peer_review_status: PeerReviewStatus
    status: PaperStatus = PaperStatus.ACTIVE
    update_notice_ids: tuple[str, ...] = ()
    supersedes_paper_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    as_of_evidence_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _paper_version_is_complete(self) -> "PaperSnapshot":
        if self.version_public_at < self.first_public_at:
            raise ValueError("paper version cannot predate first public availability")
        if self.observed_at < self.version_public_at:
            raise ValueError("paper cannot be observed before its version is public")
        normalized_authors = [author.strip() for author in self.authors]
        if any(not author for author in normalized_authors):
            raise ValueError("paper authors cannot be blank")
        if len(normalized_authors) != len(set(normalized_authors)):
            raise ValueError("paper authors must be unique")
        if len(self.source_urls) != len(set(self.source_urls)):
            raise ValueError("paper source URLs must be unique")
        if any(not url.startswith("https://") for url in self.source_urls):
            raise ValueError("paper source URLs must use HTTPS")
        if self.text_availability is TextAvailability.METADATA_ONLY:
            if self.text_content_sha256 is not None:
                raise ValueError("metadata-only papers cannot claim a text-content hash")
        elif self.text_content_sha256 is None:
            raise ValueError("abstract/full-text availability requires a text-content hash")
        changed = self.status in {
            PaperStatus.CORRECTED,
            PaperStatus.RETRACTED,
            PaperStatus.WITHDRAWN,
        }
        if changed != bool(self.update_notice_ids):
            raise ValueError("changed publication status requires update notices, and only then")
        if len(self.update_notice_ids) != len(set(self.update_notice_ids)):
            raise ValueError("paper update notices must be unique")
        return self

    @property
    def snapshot_sha256(self) -> str:
        return content_sha256(self)


class PublicationUpdate(KnowledgeModel):
    schema_version: Literal[1] = 1
    update_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,255}$")
    update_type: PublicationUpdateType
    target_canonical_id: str = Field(min_length=1, max_length=512)
    notice_paper_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    effective_at: AwareDatetime
    observed_at: AwareDatetime
    source_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _update_is_observed_after_effect(self) -> "PublicationUpdate":
        if self.observed_at < self.effective_at:
            raise ValueError("publication update cannot be observed before it is effective")
        return self

    @property
    def update_sha256(self) -> str:
        return content_sha256(self)


class SpanLocator(KnowledgeModel):
    schema_version: Literal[1] = 1
    section: str | None = Field(default=None, max_length=512)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=1)
    normalized_span_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _locator_ranges_are_complete(self) -> "SpanLocator":
        if (self.page_start is None) != (self.page_end is None):
            raise ValueError("source-span page bounds must appear together")
        if self.page_start is not None and self.page_end < self.page_start:
            raise ValueError("source-span page range is reversed")
        if (self.char_start is None) != (self.char_end is None):
            raise ValueError("source-span character bounds must appear together")
        if self.char_start is not None and self.char_end <= self.char_start:
            raise ValueError("source-span character range must have positive length")
        if self.section is None and self.page_start is None and self.char_start is None:
            raise ValueError("source span requires a section, page range, or character range")
        return self


class EvidenceReviewStatus(str, Enum):
    UNREVIEWED = "unreviewed"
    SECOND_MODEL_VERIFIED = "second_model_verified"
    HUMAN_VERIFIED = "human_verified"
    REJECTED = "rejected"


class SourceSpan(KnowledgeModel):
    schema_version: Literal[1] = 1
    span_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,255}$")
    paper_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text_scope: TextScope
    locator: SpanLocator
    exact_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text_bytes: int = Field(gt=0)
    extraction_method: ExtractionMethod
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    verification_status: EvidenceReviewStatus = EvidenceReviewStatus.UNREVIEWED
    reviewer_principal_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reviewed_at: AwareDatetime | None = None
    extracted_at: AwareDatetime
    content_trust: Literal["untrusted_literature_data"] = "untrusted_literature_data"

    @model_validator(mode="after")
    def _span_review_is_attributable(self) -> "SourceSpan":
        reviewed = self.verification_status is not EvidenceReviewStatus.UNREVIEWED
        if reviewed != bool(self.reviewer_principal_sha256 and self.reviewed_at):
            raise ValueError("reviewed source spans require reviewer identity and time")
        if self.reviewed_at is not None and self.reviewed_at < self.extracted_at:
            raise ValueError("source span cannot be reviewed before extraction")
        if self.extraction_method is ExtractionMethod.OCR and self.extraction_confidence == 1.0:
            raise ValueError("OCR extraction cannot claim perfect confidence")
        return self

    @property
    def span_sha256(self) -> str:
        return content_sha256(self)


class CorpusSnapshot(KnowledgeModel):
    schema_version: Literal[1] = 1
    snapshot_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    version: str = Field(min_length=1, max_length=128)
    cutoff_time: AwareDatetime
    temporal_mode: TemporalSnapshotMode
    sources: tuple[CorpusSourceVersion, ...] = Field(min_length=1)
    papers: tuple[PaperSnapshot, ...] = Field(min_length=1)
    spans: tuple[SourceSpan, ...] = ()
    updates: tuple[PublicationUpdate, ...] = ()
    license_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _corpus_is_temporally_closed(self) -> "CorpusSnapshot":
        if self.frozen_at < self.cutoff_time:
            raise ValueError("corpus cannot freeze before its cutoff")
        source_ids = [source.source_id for source in self.sources]
        source_hashes = [source.manifest_sha256 for source in self.sources]
        if len(source_ids) != len(set(source_ids)) or len(source_hashes) != len(set(source_hashes)):
            raise ValueError("corpus source IDs and versions must be unique")
        if any(source.updated_through > self.cutoff_time for source in self.sources):
            raise ValueError("corpus source includes updates after the cutoff")
        paper_hashes = [paper.snapshot_sha256 for paper in self.papers]
        paper_versions = [(paper.canonical_id, paper.version_id) for paper in self.papers]
        if len(paper_hashes) != len(set(paper_hashes)) or len(paper_versions) != len(
            set(paper_versions)
        ):
            raise ValueError("corpus paper snapshots must be unique")
        if any(paper.version_public_at > self.cutoff_time for paper in self.papers):
            raise ValueError("corpus includes a paper version published after cutoff")
        if self.temporal_mode is TemporalSnapshotMode.CONTEMPORANEOUS:
            if any(source.retrieved_at > self.cutoff_time for source in self.sources):
                raise ValueError("contemporaneous corpus source was retrieved after cutoff")
            if any(paper.observed_at > self.cutoff_time for paper in self.papers):
                raise ValueError("contemporaneous corpus observed a paper after cutoff")
        else:
            reconstructed = [
                paper
                for paper in self.papers
                if paper.observed_at > self.cutoff_time and paper.as_of_evidence_sha256 is None
            ]
            if reconstructed or any(
                source.retrieved_at > self.cutoff_time and source.as_of_evidence_sha256 is None
                for source in self.sources
            ):
                raise ValueError("reconstructed post-cutoff observations require as-of evidence")

        papers = {paper.snapshot_sha256: paper for paper in self.papers}
        span_ids = [span.span_id for span in self.spans]
        span_hashes = [span.span_sha256 for span in self.spans]
        if len(span_ids) != len(set(span_ids)) or len(span_hashes) != len(set(span_hashes)):
            raise ValueError("corpus source spans must be unique")
        for span in self.spans:
            paper = papers.get(span.paper_snapshot_sha256)
            if paper is None:
                raise ValueError("source span belongs to a paper outside the corpus")
            if paper.text_availability is TextAvailability.METADATA_ONLY:
                raise ValueError("metadata-only papers cannot contain source spans")
            if (
                span.text_scope is TextScope.FULL_TEXT
                and paper.text_availability is not TextAvailability.FULL_TEXT
            ):
                raise ValueError("full-text span requires full-text paper availability")
            if span.extracted_at > self.frozen_at:
                raise ValueError("source span was extracted after corpus freeze")

        update_ids = [update.update_id for update in self.updates]
        update_hashes = [update.update_sha256 for update in self.updates]
        if len(update_ids) != len(set(update_ids)) or len(update_hashes) != len(set(update_hashes)):
            raise ValueError("corpus publication updates must be unique")
        canonical_ids = {paper.canonical_id for paper in self.papers}
        paper_hash_set = set(paper_hashes)
        for update in self.updates:
            if update.target_canonical_id not in canonical_ids:
                raise ValueError("publication update targets a paper outside the corpus")
            if update.notice_paper_snapshot_sha256 not in paper_hash_set:
                raise ValueError("publication update notice is outside the corpus")
            if update.observed_at > self.cutoff_time:
                raise ValueError("corpus includes a publication update observed after cutoff")
        return self

    @property
    def snapshot_sha256(self) -> str:
        return content_sha256(self)


class QueryFamily(str, Enum):
    QUEST = "quest"
    MECHANISM = "mechanism"
    OBJECT = "object"
    METHOD = "method"
    DATASET = "dataset"
    RESULT = "result"
    SYNONYM = "synonym"
    ADJACENT_FIELD = "adjacent_field"
    NEGATION = "negation"
    AUTHOR = "author"
    CITATION_BACKWARD = "citation_backward"
    CITATION_FORWARD = "citation_forward"


_CORE_QUERY_FAMILIES = {
    QueryFamily.QUEST,
    QueryFamily.MECHANISM,
    QueryFamily.OBJECT,
    QueryFamily.METHOD,
    QueryFamily.DATASET,
    QueryFamily.RESULT,
    QueryFamily.SYNONYM,
    QueryFamily.ADJACENT_FIELD,
    QueryFamily.NEGATION,
}


class QueryOutcome(str, Enum):
    SUCCESS = "success"
    ERROR = "error"


class SearchStoppingReason(str, Enum):
    SATURATION = "saturation"
    BUDGET_EXHAUSTED = "budget_exhausted"
    SOURCE_EXHAUSTED = "source_exhausted"
    HARD_FAILURE = "hard_failure"


class SaturationRule(KnowledgeModel):
    schema_version: Literal[1] = 1
    minimum_rounds: int = Field(default=2, ge=2, le=100)
    maximum_rounds: int = Field(ge=2, le=100)
    marginal_new_relevant_fraction: float = Field(ge=0.0, le=1.0)
    consecutive_saturated_rounds: int = Field(default=2, ge=1, le=20)

    @model_validator(mode="after")
    def _round_bounds_are_consistent(self) -> "SaturationRule":
        if self.maximum_rounds < self.minimum_rounds:
            raise ValueError("search maximum rounds cannot be below minimum rounds")
        if self.consecutive_saturated_rounds > self.maximum_rounds:
            raise ValueError("saturation window cannot exceed maximum rounds")
        return self


class SearchProtocol(KnowledgeModel):
    schema_version: Literal[1] = 1
    protocol_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    objective: str = Field(min_length=1, max_length=4096)
    corpus_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cutoff_time: AwareDatetime
    candidate_claim_sha256s: tuple[str, ...] = Field(min_length=1)
    required_query_families: tuple[QueryFamily, ...] = Field(min_length=9)
    planned_source_ids: tuple[str, ...] = Field(min_length=2)
    seed_paper_snapshot_sha256s: tuple[str, ...] = Field(min_length=1)
    require_backward_citations: Literal[True] = True
    require_forward_citations: Literal[True] = True
    max_queries: int = Field(ge=1, le=100_000)
    max_results_per_query: int = Field(ge=1, le=10_000)
    saturation_rule: SaturationRule
    perturbation_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    query_planner_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    failure_policy: Literal["record_and_fail_hard_coverage"] = "record_and_fail_hard_coverage"
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _protocol_covers_required_search_axes(self) -> "SearchProtocol":
        families = list(self.required_query_families)
        if len(families) != len(set(families)):
            raise ValueError("required query families must be unique")
        missing = _CORE_QUERY_FAMILIES - set(families)
        if missing:
            raise ValueError(
                "search protocol is missing core query families: "
                + ", ".join(sorted(item.value for item in missing))
            )
        if len(self.planned_source_ids) != len(set(self.planned_source_ids)):
            raise ValueError("planned search sources must be unique")
        identities = self.candidate_claim_sha256s + self.seed_paper_snapshot_sha256s
        if len(identities) != len(set(identities)):
            raise ValueError("candidate claims and seed papers must each be unique")
        if any(
            len(identity) != 64
            or any(character not in "0123456789abcdef" for character in identity)
            for identity in identities
        ):
            raise ValueError("search claim/seed identities must use SHA-256")
        return self

    @property
    def protocol_sha256(self) -> str:
        return content_sha256(self)


class SearchHit(KnowledgeModel):
    schema_version: Literal[1] = 1
    rank: int = Field(ge=1)
    paper_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_record_id: str = Field(min_length=1, max_length=1024)
    retrieval_score: float | None = None

    @model_validator(mode="after")
    def _score_is_finite(self) -> "SearchHit":
        if self.retrieval_score is not None and not math.isfinite(self.retrieval_score):
            raise ValueError("search retrieval score must be finite")
        return self


class SearchQueryRecord(KnowledgeModel):
    schema_version: Literal[1] = 1
    query_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    family: QueryFamily
    source_id: str = Field(min_length=1, max_length=128)
    query_text: str = Field(min_length=1, max_length=8192)
    filters_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    round_index: int = Field(ge=0)
    executed_at: AwareDatetime
    outcome: QueryOutcome
    hits: tuple[SearchHit, ...] = ()
    response_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    error_class: str | None = Field(default=None, max_length=256)
    error_detail_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _query_outcome_is_complete(self) -> "SearchQueryRecord":
        if self.outcome is QueryOutcome.SUCCESS:
            if self.response_sha256 is None or self.error_class or self.error_detail_sha256:
                raise ValueError("successful query requires response hash and no error")
        elif self.response_sha256 is not None or not (
            self.error_class and self.error_detail_sha256
        ):
            raise ValueError("failed query requires error class/detail and no response")
        elif self.hits:
            raise ValueError("failed query cannot claim search hits")
        ranks = [hit.rank for hit in self.hits]
        if ranks and ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("search-hit ranks must be contiguous and ordered")
        hashes = [hit.paper_snapshot_sha256 for hit in self.hits]
        if len(hashes) != len(set(hashes)):
            raise ValueError("one query cannot return a paper snapshot twice")
        return self

    @property
    def query_sha256(self) -> str:
        return content_sha256(self)


class SearchSession(KnowledgeModel):
    schema_version: Literal[1] = 1
    session_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    corpus_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    queries: tuple[SearchQueryRecord, ...] = Field(min_length=1)
    started_at: AwareDatetime
    ended_at: AwareDatetime
    stopping_reason: SearchStoppingReason
    stopping_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    replay_cache_sha256s: tuple[str, ...] = ()
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _session_is_replayable(self) -> "SearchSession":
        if self.ended_at < self.started_at:
            raise ValueError("search session ended before it started")
        ids = [query.query_id for query in self.queries]
        hashes = [query.query_sha256 for query in self.queries]
        if len(ids) != len(set(ids)) or len(hashes) != len(set(hashes)):
            raise ValueError("search query IDs and records must be unique")
        if any(
            query.executed_at < self.started_at or query.executed_at > self.ended_at
            for query in self.queries
        ):
            raise ValueError("search query lies outside the session timeline")
        successful_responses = {
            query.response_sha256 for query in self.queries if query.response_sha256 is not None
        }
        if successful_responses != set(self.replay_cache_sha256s):
            raise ValueError("replay cache must exactly cover successful query responses")
        if len(self.replay_cache_sha256s) != len(set(self.replay_cache_sha256s)):
            raise ValueError("search replay cache identities must be unique")
        return self

    @property
    def session_sha256(self) -> str:
        return content_sha256(self)


class CoverageSignalName(str, Enum):
    KNOWN_ANSWER_RECALL = "known_answer_recall"
    SEED_REFERENCE_RECOVERY = "seed_reference_recovery"
    QUERY_FAMILY_COVERAGE = "query_family_coverage"
    SOURCE_DIVERSITY = "source_diversity"
    CITATION_FRONTIER_SATURATION = "citation_frontier_saturation"
    FULL_TEXT_AVAILABILITY = "full_text_availability"
    SOURCE_SPAN_VERIFICATION = "source_span_verification"
    CORRECTION_RETRACTION_CHECK = "correction_retraction_check"
    PERTURBATION_STABILITY = "perturbation_stability"
    UNCOVERED_SOURCE_FRACTION = "uncovered_source_fraction"


class CoverageDirection(str, Enum):
    MINIMUM = "minimum"
    MAXIMUM = "maximum"


class CoverageSignalStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    MISSING = "missing"


class CoverageVerdict(str, Enum):
    SUFFICIENT = "coverage_sufficient"
    INSUFFICIENT = "coverage_insufficient"


class CoverageRequirement(KnowledgeModel):
    schema_version: Literal[1] = 1
    signal: CoverageSignalName
    direction: CoverageDirection
    threshold: float
    hard: bool = True
    rationale: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def _threshold_is_finite(self) -> "CoverageRequirement":
        if not math.isfinite(self.threshold):
            raise ValueError("coverage threshold must be finite")
        return self


class CoveragePolicy(KnowledgeModel):
    schema_version: Literal[1] = 1
    policy_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    requirements: tuple[CoverageRequirement, ...] = Field(min_length=10, max_length=10)
    minimum_nearest_prior_art: int = Field(default=3, ge=1, le=100)
    minimum_independent_reviewers: int = Field(default=2, ge=1, le=10)
    author_exclusion_required: Literal[True] = True
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _policy_covers_every_signal(self) -> "CoveragePolicy":
        signals = [requirement.signal for requirement in self.requirements]
        if len(signals) != len(set(signals)) or set(signals) != set(CoverageSignalName):
            raise ValueError("coverage policy must define every signal exactly once")
        by_signal = {requirement.signal: requirement for requirement in self.requirements}
        if (
            by_signal[CoverageSignalName.UNCOVERED_SOURCE_FRACTION].direction
            is not CoverageDirection.MAXIMUM
        ):
            raise ValueError("uncovered source fraction requires a maximum threshold")
        if any(
            requirement.direction is not CoverageDirection.MINIMUM
            for signal, requirement in by_signal.items()
            if signal is not CoverageSignalName.UNCOVERED_SOURCE_FRACTION
        ):
            raise ValueError("all non-uncovered coverage signals require minimum thresholds")
        return self

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class CoverageSignalResult(KnowledgeModel):
    schema_version: Literal[1] = 1
    signal: CoverageSignalName
    observed: float | None = None
    numerator: int | None = Field(default=None, ge=0)
    denominator: int | None = Field(default=None, ge=1)
    status: CoverageSignalStatus
    evidence_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    detail: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def _observation_is_attributable(self) -> "CoverageSignalResult":
        if (self.numerator is None) != (self.denominator is None):
            raise ValueError("coverage numerator and denominator must appear together")
        if self.numerator is not None and self.numerator > self.denominator:
            raise ValueError("coverage numerator cannot exceed denominator")
        missing = self.observed is None
        if missing != (self.status is CoverageSignalStatus.MISSING):
            raise ValueError("missing coverage observation must use missing status")
        if self.observed is not None:
            if not math.isfinite(self.observed):
                raise ValueError("coverage observation must be finite")
            if self.evidence_sha256 is None:
                raise ValueError("observed coverage signal requires evidence identity")
        return self


class CoverageReport(KnowledgeModel):
    schema_version: Literal[1] = 1
    report_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    policy: CoveragePolicy
    corpus_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    search_session_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signals: tuple[CoverageSignalResult, ...] = Field(min_length=10, max_length=10)
    verdict: CoverageVerdict
    hard_failure_signals: tuple[CoverageSignalName, ...]
    generated_at: AwareDatetime

    @model_validator(mode="after")
    def _verdict_is_derived_from_hard_requirements(self) -> "CoverageReport":
        results = {result.signal: result for result in self.signals}
        if len(results) != len(self.signals) or set(results) != set(CoverageSignalName):
            raise ValueError("coverage report must contain every signal exactly once")
        failures: list[CoverageSignalName] = []
        for requirement in self.policy.requirements:
            result = results[requirement.signal]
            if result.observed is None:
                passed = False
            else:
                passed = (
                    result.observed >= requirement.threshold
                    if requirement.direction is CoverageDirection.MINIMUM
                    else result.observed <= requirement.threshold
                )
            expected_status = (
                CoverageSignalStatus.MISSING
                if result.observed is None
                else CoverageSignalStatus.PASS
                if passed
                else CoverageSignalStatus.FAIL
            )
            if result.status is not expected_status:
                raise ValueError(
                    f"coverage signal {result.signal.value} status does not match threshold"
                )
            if requirement.hard and not passed:
                failures.append(requirement.signal)
        expected_failures = tuple(sorted(failures, key=lambda item: item.value))
        if self.hard_failure_signals != expected_failures:
            raise ValueError("coverage hard-failure list is not derived from signal results")
        expected_verdict = CoverageVerdict.INSUFFICIENT if failures else CoverageVerdict.SUFFICIENT
        if self.verdict is not expected_verdict:
            raise ValueError("coverage verdict does not match hard signal results")
        return self

    @property
    def report_sha256(self) -> str:
        return content_sha256(self)


class ClaimOrigin(str, Enum):
    CANDIDATE = "candidate"
    PRIOR_ART = "prior_art"


class ClaimType(str, Enum):
    EMPIRICAL = "empirical"
    CAUSAL = "causal"
    MECHANISTIC = "mechanistic"
    METHODOLOGICAL = "methodological"
    THEORETICAL = "theoretical"
    NULL_RESULT = "null_result"
    LIMITATION = "limitation"


class ClaimDirection(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NULL = "null"
    MIXED = "mixed"
    NOT_APPLICABLE = "not_applicable"


class UncertaintyType(str, Enum):
    CONFIDENCE_INTERVAL = "confidence_interval"
    CREDIBLE_INTERVAL = "credible_interval"
    STANDARD_ERROR = "standard_error"
    NONE_REPORTED = "none_reported"


class QuantitativeEffect(KnowledgeModel):
    schema_version: Literal[1] = 1
    estimate: float
    unit: str = Field(min_length=1, max_length=128)
    metric_definition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    uncertainty_type: UncertaintyType
    lower: float | None = None
    upper: float | None = None
    sample_size: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _effect_is_finite_and_ordered(self) -> "QuantitativeEffect":
        values = [self.estimate, self.lower, self.upper]
        if any(value is not None and not math.isfinite(value) for value in values):
            raise ValueError("quantitative effect values must be finite")
        bounded = self.uncertainty_type in {
            UncertaintyType.CONFIDENCE_INTERVAL,
            UncertaintyType.CREDIBLE_INTERVAL,
        }
        if bounded != (self.lower is not None and self.upper is not None):
            raise ValueError("interval uncertainty requires exactly lower and upper bounds")
        if self.lower is not None and not (self.lower <= self.estimate <= self.upper):
            raise ValueError("effect estimate must lie inside its uncertainty interval")
        return self


class AtomicClaim(KnowledgeModel):
    schema_version: Literal[1] = 1
    claim_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    origin: ClaimOrigin
    subject: str = Field(min_length=1, max_length=2048)
    relation: str = Field(min_length=1, max_length=1024)
    object: str = Field(min_length=1, max_length=2048)
    qualifiers: tuple[str, ...] = ()
    population: str | None = Field(default=None, max_length=2048)
    conditions: tuple[str, ...] = ()
    direction: ClaimDirection
    claim_type: ClaimType
    quantitative_effect: QuantitativeEffect | None = None
    source_paper_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    candidate_artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    asserted_at: AwareDatetime

    @model_validator(mode="after")
    def _claim_origin_is_attributable(self) -> "AtomicClaim":
        values = self.qualifiers + self.conditions
        if any(not value.strip() for value in values):
            raise ValueError("claim qualifiers and conditions cannot be blank")
        if len(values) != len(set(values)):
            raise ValueError("claim qualifiers/conditions must not contain duplicates")
        if self.origin is ClaimOrigin.PRIOR_ART:
            if self.source_paper_snapshot_sha256 is None or self.candidate_artifact_sha256:
                raise ValueError("prior claim requires a paper source and no candidate artifact")
        elif self.candidate_artifact_sha256 is None or self.source_paper_snapshot_sha256:
            raise ValueError("candidate claim requires an artifact and no prior-paper source")
        return self

    @property
    def claim_sha256(self) -> str:
        return content_sha256(self)


class ClaimEvidenceRelation(str, Enum):
    SUPPORTS = "supports"
    REFUTES = "refutes"
    QUALIFIES = "qualifies"
    MENTIONS = "mentions"


class ClaimEvidenceEdge(KnowledgeModel):
    schema_version: Literal[1] = 1
    claim_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_span_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    relation: ClaimEvidenceRelation
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    reviewer_status: EvidenceReviewStatus
    reviewer_principal_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reviewed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _edge_review_is_attributable(self) -> "ClaimEvidenceEdge":
        reviewed = self.reviewer_status is not EvidenceReviewStatus.UNREVIEWED
        if reviewed != bool(self.reviewer_principal_sha256 and self.reviewed_at):
            raise ValueError("reviewed claim evidence requires reviewer identity and time")
        return self

    @property
    def edge_sha256(self) -> str:
        return content_sha256(self)


class AtomicClaimGraph(KnowledgeModel):
    schema_version: Literal[1] = 1
    graph_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    corpus_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    claims: tuple[AtomicClaim, ...] = Field(min_length=2)
    evidence_edges: tuple[ClaimEvidenceEdge, ...] = Field(min_length=1)
    extraction_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _graph_references_known_claims(self) -> "AtomicClaimGraph":
        ids = [claim.claim_id for claim in self.claims]
        hashes = [claim.claim_sha256 for claim in self.claims]
        if len(ids) != len(set(ids)) or len(hashes) != len(set(hashes)):
            raise ValueError("atomic claim IDs and contents must be unique")
        origins = {claim.origin for claim in self.claims}
        if origins != {ClaimOrigin.CANDIDATE, ClaimOrigin.PRIOR_ART}:
            raise ValueError("claim graph requires both candidate and prior-art claims")
        edge_hashes = [edge.edge_sha256 for edge in self.evidence_edges]
        if len(edge_hashes) != len(set(edge_hashes)):
            raise ValueError("claim evidence edges must be unique")
        unknown = {edge.claim_sha256 for edge in self.evidence_edges} - set(hashes)
        if unknown:
            raise ValueError("claim evidence edge references an unknown claim")
        evidenced = {edge.claim_sha256 for edge in self.evidence_edges}
        missing_prior = {
            claim.claim_sha256
            for claim in self.claims
            if claim.origin is ClaimOrigin.PRIOR_ART and claim.claim_sha256 not in evidenced
        }
        if missing_prior:
            raise ValueError("every prior-art claim requires source-span evidence")
        return self

    @property
    def graph_sha256(self) -> str:
        return content_sha256(self)


class PriorArtRelationType(str, Enum):
    EQUIVALENT = "equivalent"
    SUBSUMES = "subsumes"
    SPECIAL_CASE = "special_case"
    EXTENSION = "extension"
    COMBINATION = "combination"
    CONTRADICTION = "contradiction"


class DifferenceComponent(str, Enum):
    SUBJECT = "subject"
    RELATION = "relation"
    OBJECT = "object"
    QUALIFIER = "qualifier"
    POPULATION = "population"
    CONDITION = "condition"
    METHOD = "method"
    DATASET = "dataset"
    METRIC = "metric"
    EFFECT = "effect"


class ComponentDifference(KnowledgeModel):
    schema_version: Literal[1] = 1
    component: DifferenceComponent
    candidate_value: str = Field(min_length=1, max_length=4096)
    prior_value: str = Field(min_length=1, max_length=4096)
    difference: str = Field(min_length=1, max_length=4096)
    evidence_span_sha256s: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _difference_evidence_is_unique(self) -> "ComponentDifference":
        if len(self.evidence_span_sha256s) != len(set(self.evidence_span_sha256s)):
            raise ValueError("component-difference evidence must be unique")
        return self


class RetrievalSignals(KnowledgeModel):
    schema_version: Literal[1] = 1
    lexical: float | None = Field(default=None, ge=0.0, le=1.0)
    embedding: float | None = Field(default=None, ge=0.0, le=1.0)
    citation: float | None = Field(default=None, ge=0.0, le=1.0)
    entity: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _multiple_recall_channels_are_retained(self) -> "RetrievalSignals":
        observed = sum(
            value is not None
            for value in (self.lexical, self.embedding, self.citation, self.entity)
        )
        if observed < 2:
            raise ValueError("prior-art audit requires at least two retrieval channels")
        return self


class PriorArtRelation(KnowledgeModel):
    schema_version: Literal[1] = 1
    candidate_claim_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prior_claim_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    relation: PriorArtRelationType
    rank: int = Field(ge=1)
    retrieval_signals: RetrievalSignals
    differences: tuple[ComponentDifference, ...] = ()
    evidence_span_sha256s: tuple[str, ...] = Field(min_length=1)
    matcher_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_status: EvidenceReviewStatus
    reviewer_principal_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reviewed_at: AwareDatetime | None = None
    blocks_strong_novelty: bool

    @model_validator(mode="after")
    def _relation_has_exact_blocking_semantics(self) -> "PriorArtRelation":
        blocking = self.relation in {
            PriorArtRelationType.EQUIVALENT,
            PriorArtRelationType.SUBSUMES,
            PriorArtRelationType.SPECIAL_CASE,
        }
        if self.blocks_strong_novelty != blocking:
            raise ValueError("prior-art blocking flag does not match relation semantics")
        if self.relation is PriorArtRelationType.EQUIVALENT and self.differences:
            raise ValueError("equivalent prior art cannot declare material differences")
        if self.relation is not PriorArtRelationType.EQUIVALENT and not self.differences:
            raise ValueError("non-equivalent prior art requires component differences")
        if len(self.evidence_span_sha256s) != len(set(self.evidence_span_sha256s)):
            raise ValueError("prior-art evidence spans must be unique")
        reviewed = self.reviewer_status is not EvidenceReviewStatus.UNREVIEWED
        if reviewed != bool(self.reviewer_principal_sha256 and self.reviewed_at):
            raise ValueError("reviewed prior-art relation requires reviewer identity and time")
        return self

    @property
    def relation_sha256(self) -> str:
        return content_sha256(self)


class NoveltyClassification(str, Enum):
    KNOWN_EQUIVALENT = "known_equivalent"
    KNOWN_SPECIAL_CASE = "known_special_case"
    INCREMENTAL_EXTENSION = "incremental_extension"
    NOVEL_COMBINATION = "novel_combination"
    NOVEL_METHOD = "novel_method"
    NOVEL_PHENOMENON = "novel_phenomenon"
    CONTRADICTORY_TO_PRIOR = "contradictory_to_prior"
    INDETERMINATE_DUE_TO_COVERAGE = "indeterminate_due_to_coverage"


class NoveltyClaimCeiling(str, Enum):
    NONE = "none"
    SPECULATIVE = "speculative"
    WEAK = "weak"
    MODERATE = "moderate"


class NoveltyReviewVerdict(str, Enum):
    CONFIRM_EVIDENCE_PACKAGE = "confirm_evidence_package"
    REQUEST_MORE_SEARCH = "request_more_search"
    REJECT_CLASSIFICATION = "reject_classification"


class NoveltyReview(KnowledgeModel):
    schema_version: Literal[1] = 1
    reviewer_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_role: Literal["domain_expert", "methodologist", "research_librarian"]
    candidate_author_excluded: Literal[True] = True
    evidence_package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verdict: NoveltyReviewVerdict
    rationale_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_at: AwareDatetime


class NoveltyPolicy(KnowledgeModel):
    schema_version: Literal[1] = 1
    policy_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    minimum_nearest_prior_art: int = Field(ge=1, le=100)
    minimum_independent_reviewers: int = Field(ge=1, le=10)
    strong_classes: tuple[NoveltyClassification, ...] = (
        NoveltyClassification.NOVEL_COMBINATION,
        NoveltyClassification.NOVEL_METHOD,
        NoveltyClassification.NOVEL_PHENOMENON,
    )
    require_temporal_disclosure: Literal[True] = True
    require_model_prior_disclosure: Literal[True] = True
    frozen_at: AwareDatetime

    @model_validator(mode="after")
    def _strong_classes_are_exact(self) -> "NoveltyPolicy":
        expected = {
            NoveltyClassification.NOVEL_COMBINATION,
            NoveltyClassification.NOVEL_METHOD,
            NoveltyClassification.NOVEL_PHENOMENON,
        }
        if (
            len(self.strong_classes) != len(set(self.strong_classes))
            or set(self.strong_classes) != expected
        ):
            raise ValueError("novelty policy strong classes are frozen and exact")
        return self

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class NoveltyAssessment(KnowledgeModel):
    schema_version: Literal[1] = 1
    assessment_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    policy: NoveltyPolicy
    corpus_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    search_session_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    coverage_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    coverage_verdict: CoverageVerdict
    claim_graph_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_claim_sha256s: tuple[str, ...] = Field(min_length=1)
    candidate_author_principal_sha256s: tuple[str, ...] = Field(min_length=1)
    nearest_prior_art: tuple[PriorArtRelation, ...] = Field(min_length=1)
    classification: NoveltyClassification
    exact_differences: tuple[ComponentDifference, ...] = ()
    unresolved_blockers: tuple[str, ...] = ()
    temporal_cutoff: AwareDatetime
    temporal_limitations: str = Field(min_length=1, max_length=4096)
    model_prior_limitations: str = Field(min_length=1, max_length=4096)
    contamination_disclosure: str = Field(min_length=1, max_length=4096)
    evidence_package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviews: tuple[NoveltyReview, ...] = ()
    strong_novelty_eligible: bool
    claim_strength_ceiling: NoveltyClaimCeiling
    assessed_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _assessment_is_fail_closed(self) -> "NoveltyAssessment":
        claim_hashes = set(self.candidate_claim_sha256s)
        if len(claim_hashes) != len(self.candidate_claim_sha256s):
            raise ValueError("novelty candidate claims must be unique")
        authors = set(self.candidate_author_principal_sha256s)
        if len(authors) != len(self.candidate_author_principal_sha256s):
            raise ValueError("novelty candidate authors must be unique")
        ranks = [relation.rank for relation in self.nearest_prior_art]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("nearest prior art ranks must be contiguous and ordered")
        if any(
            relation.candidate_claim_sha256 not in claim_hashes
            for relation in self.nearest_prior_art
        ):
            raise ValueError("prior-art relation belongs to another candidate claim")
        relation_hashes = [relation.relation_sha256 for relation in self.nearest_prior_art]
        if len(relation_hashes) != len(set(relation_hashes)):
            raise ValueError("nearest prior-art relations must be unique")
        expected_package = content_sha256(
            {
                "policy_sha256": self.policy.policy_sha256,
                "corpus_snapshot_sha256": self.corpus_snapshot_sha256,
                "search_session_sha256": self.search_session_sha256,
                "coverage_report_sha256": self.coverage_report_sha256,
                "claim_graph_sha256": self.claim_graph_sha256,
                "candidate_claim_sha256s": self.candidate_claim_sha256s,
                "nearest_prior_art_sha256s": relation_hashes,
                "temporal_cutoff": self.temporal_cutoff.isoformat(),
            }
        )
        if self.evidence_package_sha256 != expected_package:
            raise ValueError("novelty evidence-package hash is invalid")
        reviewer_ids = [review.reviewer_principal_sha256 for review in self.reviews]
        if len(reviewer_ids) != len(set(reviewer_ids)):
            raise ValueError("novelty reviewers must be unique")
        if authors.intersection(reviewer_ids):
            raise ValueError("candidate authors cannot review their own novelty package")
        if any(
            review.evidence_package_sha256 != self.evidence_package_sha256
            for review in self.reviews
        ):
            raise ValueError("novelty review is bound to another evidence package")

        if self.coverage_verdict is CoverageVerdict.INSUFFICIENT:
            if (
                self.classification is not NoveltyClassification.INDETERMINATE_DUE_TO_COVERAGE
                or self.strong_novelty_eligible
                or self.claim_strength_ceiling is not NoveltyClaimCeiling.SPECULATIVE
            ):
                raise ValueError("insufficient coverage forces indeterminate speculative novelty")
            return self

        if self.classification is NoveltyClassification.INDETERMINATE_DUE_TO_COVERAGE:
            raise ValueError(
                "coverage-sufficient evidence cannot use a coverage-indeterminate class"
            )

        differences = [content_sha256(difference) for difference in self.exact_differences]
        if len(differences) != len(set(differences)):
            raise ValueError("novelty exact differences must be unique")
        if any(not blocker.strip() for blocker in self.unresolved_blockers) or len(
            self.unresolved_blockers
        ) != len(set(self.unresolved_blockers)):
            raise ValueError("novelty blockers must be non-blank and unique")

        blocking_prior = any(relation.blocks_strong_novelty for relation in self.nearest_prior_art)
        confirmed_reviews = [
            review
            for review in self.reviews
            if review.verdict is NoveltyReviewVerdict.CONFIRM_EVIDENCE_PACKAGE
        ]
        prerequisites = (
            self.classification in self.policy.strong_classes
            and len(self.nearest_prior_art) >= self.policy.minimum_nearest_prior_art
            and bool(self.exact_differences)
            and not self.unresolved_blockers
            and not blocking_prior
            and len(confirmed_reviews) >= self.policy.minimum_independent_reviewers
            and len(confirmed_reviews) == len(self.reviews)
            and bool(self.temporal_limitations.strip())
            and bool(self.model_prior_limitations.strip())
        )
        if self.strong_novelty_eligible != prerequisites:
            raise ValueError("strong-novelty eligibility does not match frozen prerequisites")
        expected_ceiling = (
            NoveltyClaimCeiling.MODERATE
            if prerequisites
            else NoveltyClaimCeiling.NONE
            if self.classification
            in {
                NoveltyClassification.KNOWN_EQUIVALENT,
                NoveltyClassification.KNOWN_SPECIAL_CASE,
            }
            else NoveltyClaimCeiling.WEAK
            if self.classification
            in {
                NoveltyClassification.INCREMENTAL_EXTENSION,
                NoveltyClassification.CONTRADICTORY_TO_PRIOR,
            }
            else NoveltyClaimCeiling.SPECULATIVE
        )
        if self.claim_strength_ceiling is not expected_ceiling:
            raise ValueError("novelty claim ceiling does not match classification/evidence")
        return self

    @property
    def assessment_sha256(self) -> str:
        return content_sha256(self)


class MethodEntity(KnowledgeModel):
    """Canonical method identity without making method equality a comparability condition."""

    schema_version: Literal[1] = 1
    method_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    canonical_name: str = Field(min_length=1, max_length=1024)
    aliases: tuple[str, ...] = ()
    specification_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    implementation_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _aliases_are_canonical(self) -> "MethodEntity":
        normalized = [alias.strip() for alias in self.aliases]
        if any(not alias for alias in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("method aliases must be non-blank and unique")
        if self.canonical_name.strip() in set(normalized):
            raise ValueError("canonical method name cannot be repeated as an alias")
        return self

    @property
    def method_sha256(self) -> str:
        return content_sha256(self)


class DatasetVersion(KnowledgeModel):
    schema_version: Literal[1] = 1
    dataset_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    canonical_name: str = Field(min_length=1, max_length=1024)
    aliases: tuple[str, ...] = ()
    version_id: str = Field(min_length=1, max_length=256)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    license_id: str = Field(min_length=1, max_length=256)
    source_url: str = Field(min_length=9, max_length=4096)
    released_at: AwareDatetime
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def _dataset_version_is_attributable(self) -> "DatasetVersion":
        normalized = [alias.strip() for alias in self.aliases]
        if any(not alias for alias in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("dataset aliases must be non-blank and unique")
        if self.canonical_name.strip() in set(normalized):
            raise ValueError("canonical dataset name cannot be repeated as an alias")
        if not self.source_url.startswith("https://"):
            raise ValueError("dataset source URL must use HTTPS")
        if self.observed_at < self.released_at:
            raise ValueError("dataset cannot be observed before its release")
        return self

    @property
    def dataset_sha256(self) -> str:
        return content_sha256(self)

    @property
    def comparability_sha256(self) -> str:
        """Identity of scientific bytes/schema, excluding descriptive aliases and observation."""

        return content_sha256(
            {
                "dataset_id": self.dataset_id,
                "version_id": self.version_id,
                "content_sha256": self.content_sha256,
                "schema_sha256": self.schema_sha256,
            }
        )


class MetricDirection(str, Enum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


class MetricDefinition(KnowledgeModel):
    schema_version: Literal[1] = 1
    metric_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    canonical_name: str = Field(min_length=1, max_length=1024)
    aliases: tuple[str, ...] = ()
    formula_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    aggregation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    direction: MetricDirection
    reporting_unit: str = Field(min_length=1, max_length=128)
    valid_minimum: float | None = None
    valid_maximum: float | None = None

    @model_validator(mode="after")
    def _metric_definition_is_exact(self) -> "MetricDefinition":
        normalized = [alias.strip() for alias in self.aliases]
        if any(not alias for alias in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("metric aliases must be non-blank and unique")
        if self.canonical_name.strip() in set(normalized):
            raise ValueError("canonical metric name cannot be repeated as an alias")
        bounds = (self.valid_minimum, self.valid_maximum)
        if any(value is not None and not math.isfinite(value) for value in bounds):
            raise ValueError("metric bounds must be finite")
        if (
            self.valid_minimum is not None
            and self.valid_maximum is not None
            and self.valid_maximum <= self.valid_minimum
        ):
            raise ValueError("metric maximum must exceed its minimum")
        return self

    @property
    def metric_sha256(self) -> str:
        return content_sha256(self)

    @property
    def comparability_sha256(self) -> str:
        return content_sha256(
            {
                "formula_sha256": self.formula_sha256,
                "aggregation_sha256": self.aggregation_sha256,
                "direction": self.direction,
                "reporting_unit": self.reporting_unit,
                "valid_minimum": self.valid_minimum,
                "valid_maximum": self.valid_maximum,
            }
        )


class ResourceBudgetSignature(KnowledgeModel):
    schema_version: Literal[1] = 1
    compute_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_budget_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    hardware_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    accelerator_hours: float = Field(ge=0.0)
    wall_clock_hours: float = Field(gt=0.0)
    maximum_cost_usd: float = Field(ge=0.0)

    @model_validator(mode="after")
    def _budgets_are_finite(self) -> "ResourceBudgetSignature":
        if not all(
            math.isfinite(value)
            for value in (
                self.accelerator_hours,
                self.wall_clock_hours,
                self.maximum_cost_usd,
            )
        ):
            raise ValueError("resource budgets must be finite")
        return self

    @property
    def budget_sha256(self) -> str:
        return content_sha256(self)


class ProtocolSignature(KnowledgeModel):
    schema_version: Literal[1] = 1
    protocol_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    method: MethodEntity
    task_definition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset: DatasetVersion
    split_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    split_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    grouping_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    leakage_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    preprocessing_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    exclusions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    metric: MetricDefinition
    uncertainty_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    statistical_test_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource_budget: ResourceBudgetSignature
    external_resources_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pretraining_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_date: AwareDatetime
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _protocol_precedes_evaluation(self) -> "ProtocolSignature":
        if self.frozen_at < self.dataset.observed_at:
            raise ValueError("protocol signature cannot predate its observed dataset version")
        if self.frozen_at > self.evaluation_date:
            raise ValueError("protocol signature must freeze no later than evaluation")
        return self

    @property
    def protocol_sha256(self) -> str:
        return content_sha256(self)


class ProtocolDimension(str, Enum):
    TASK_DEFINITION = "task_definition"
    DATASET = "dataset"
    SPLIT = "split"
    GROUPING_LEAKAGE = "grouping_leakage"
    PREPROCESSING = "preprocessing"
    EXCLUSIONS = "exclusions"
    METRIC = "metric"
    UNCERTAINTY_STATISTICS = "uncertainty_statistics"
    RESOURCE_BUDGET = "resource_budget"
    EXTERNAL_RESOURCES = "external_resources"
    PRETRAINING = "pretraining"
    EVALUATION_DATE = "evaluation_date"


_PROTOCOL_DIMENSION_ORDER = tuple(ProtocolDimension)


class ProtocolMismatch(KnowledgeModel):
    schema_version: Literal[1] = 1
    dimension: ProtocolDimension
    candidate_value_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_value_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    blocking: bool
    detail: str = Field(min_length=1, max_length=2048)


class ComparabilityStatus(str, Enum):
    COMPATIBLE = "compatible"
    NON_COMPARABLE = "non_comparable"


class ProtocolComparability(KnowledgeModel):
    schema_version: Literal[1] = 1
    candidate_protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: ComparabilityStatus
    mismatches: tuple[ProtocolMismatch, ...]
    assessed_at: AwareDatetime

    @model_validator(mode="after")
    def _status_is_derived_from_mismatches(self) -> "ProtocolComparability":
        dimensions = [mismatch.dimension for mismatch in self.mismatches]
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("protocol mismatches must have unique dimensions")
        ordered = sorted(dimensions, key=lambda value: _PROTOCOL_DIMENSION_ORDER.index(value))
        if dimensions != ordered:
            raise ValueError("protocol mismatches must follow canonical dimension order")
        expected = (
            ComparabilityStatus.NON_COMPARABLE
            if any(mismatch.blocking for mismatch in self.mismatches)
            else ComparabilityStatus.COMPATIBLE
        )
        if self.status is not expected:
            raise ValueError("comparability status does not match blocking mismatches")
        for mismatch in self.mismatches:
            should_block = mismatch.dimension is not ProtocolDimension.EVALUATION_DATE
            if mismatch.blocking != should_block:
                raise ValueError("only evaluation-date mismatch is non-blocking")
            if mismatch.candidate_value_sha256 == mismatch.reference_value_sha256:
                raise ValueError("protocol mismatch cannot contain equal identities")
        return self

    @property
    def comparison_sha256(self) -> str:
        return content_sha256(self)


def _protocol_dimension_identities(
    protocol: ProtocolSignature,
) -> dict[ProtocolDimension, str]:
    return {
        ProtocolDimension.TASK_DEFINITION: protocol.task_definition_sha256,
        ProtocolDimension.DATASET: protocol.dataset.comparability_sha256,
        ProtocolDimension.SPLIT: content_sha256(
            {
                "policy": protocol.split_policy_sha256,
                "content": protocol.split_content_sha256,
            }
        ),
        ProtocolDimension.GROUPING_LEAKAGE: content_sha256(
            {
                "grouping": protocol.grouping_policy_sha256,
                "leakage": protocol.leakage_policy_sha256,
            }
        ),
        ProtocolDimension.PREPROCESSING: protocol.preprocessing_sha256,
        ProtocolDimension.EXCLUSIONS: protocol.exclusions_sha256,
        ProtocolDimension.METRIC: protocol.metric.comparability_sha256,
        ProtocolDimension.UNCERTAINTY_STATISTICS: content_sha256(
            {
                "uncertainty": protocol.uncertainty_policy_sha256,
                "test": protocol.statistical_test_sha256,
            }
        ),
        ProtocolDimension.RESOURCE_BUDGET: protocol.resource_budget.budget_sha256,
        ProtocolDimension.EXTERNAL_RESOURCES: protocol.external_resources_sha256,
        ProtocolDimension.PRETRAINING: protocol.pretraining_sha256,
        ProtocolDimension.EVALUATION_DATE: content_sha256(protocol.evaluation_date.isoformat()),
    }


def assess_protocol_comparability(
    candidate: ProtocolSignature,
    reference: ProtocolSignature,
    *,
    assessed_at: datetime,
) -> ProtocolComparability:
    candidate_values = _protocol_dimension_identities(candidate)
    reference_values = _protocol_dimension_identities(reference)
    mismatches = tuple(
        ProtocolMismatch(
            dimension=dimension,
            candidate_value_sha256=candidate_values[dimension],
            reference_value_sha256=reference_values[dimension],
            blocking=dimension is not ProtocolDimension.EVALUATION_DATE,
            detail=(
                "evaluation dates differ; disclosed as temporal context"
                if dimension is ProtocolDimension.EVALUATION_DATE
                else f"required protocol dimension differs: {dimension.value}"
            ),
        )
        for dimension in _PROTOCOL_DIMENSION_ORDER
        if candidate_values[dimension] != reference_values[dimension]
    )
    status = (
        ComparabilityStatus.NON_COMPARABLE
        if any(mismatch.blocking for mismatch in mismatches)
        else ComparabilityStatus.COMPATIBLE
    )
    return ProtocolComparability(
        candidate_protocol_sha256=candidate.protocol_sha256,
        reference_protocol_sha256=reference.protocol_sha256,
        status=status,
        mismatches=mismatches,
        assessed_at=assessed_at,
    )


class SOTAComparison(KnowledgeModel):
    schema_version: Literal[1] = 1
    comparison_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    candidate_protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    comparability: ProtocolComparability
    metric: MetricDefinition
    candidate_score: float
    reference_score: float
    raw_delta: float | None
    favorable_delta: float | None
    candidate_outperforms: bool | None
    headline_delta_allowed: bool
    headline_verdict: Literal[
        "beats_reference",
        "ties_reference",
        "underperforms_reference",
        "non_comparable",
    ]
    generated_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _headline_is_derived_only_when_comparable(self) -> "SOTAComparison":
        if (
            self.candidate_protocol_sha256 != self.comparability.candidate_protocol_sha256
            or self.reference_protocol_sha256 != self.comparability.reference_protocol_sha256
        ):
            raise ValueError("SOTA comparison is bound to different protocol identities")
        if self.generated_at < self.comparability.assessed_at:
            raise ValueError("SOTA comparison cannot predate comparability assessment")
        if not math.isfinite(self.candidate_score) or not math.isfinite(self.reference_score):
            raise ValueError("SOTA scores must be finite")
        for label, score in (
            ("candidate", self.candidate_score),
            ("reference", self.reference_score),
        ):
            if self.metric.valid_minimum is not None and score < self.metric.valid_minimum:
                raise ValueError(f"{label} score is below the metric range")
            if self.metric.valid_maximum is not None and score > self.metric.valid_maximum:
                raise ValueError(f"{label} score is above the metric range")

        if self.comparability.status is ComparabilityStatus.NON_COMPARABLE:
            if (
                self.raw_delta is not None
                or self.favorable_delta is not None
                or self.candidate_outperforms is not None
                or self.headline_delta_allowed
                or self.headline_verdict != "non_comparable"
            ):
                raise ValueError("non-comparable protocols cannot emit a SOTA delta or headline")
            return self

        expected_raw = self.candidate_score - self.reference_score
        expected_favorable = (
            expected_raw
            if self.metric.direction is MetricDirection.HIGHER_IS_BETTER
            else -expected_raw
        )
        expected_outperforms = expected_favorable > 0.0
        expected_verdict = (
            "beats_reference"
            if expected_favorable > 0.0
            else "underperforms_reference"
            if expected_favorable < 0.0
            else "ties_reference"
        )
        if self.raw_delta is None or not math.isclose(
            self.raw_delta, expected_raw, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError("SOTA raw delta is not derived from the scores")
        if self.favorable_delta is None or not math.isclose(
            self.favorable_delta, expected_favorable, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError("SOTA favorable delta is not direction-normalized")
        if (
            self.candidate_outperforms != expected_outperforms
            or not self.headline_delta_allowed
            or self.headline_verdict != expected_verdict
        ):
            raise ValueError("SOTA headline fields do not match comparable scores")
        return self

    @property
    def comparison_sha256(self) -> str:
        return content_sha256(self)


def build_sota_comparison(
    *,
    comparison_id: str,
    candidate: ProtocolSignature,
    reference: ProtocolSignature,
    candidate_score: float,
    reference_score: float,
    assessed_at: datetime,
    generated_at: datetime,
) -> SOTAComparison:
    comparability = assess_protocol_comparability(candidate, reference, assessed_at=assessed_at)
    comparable = comparability.status is ComparabilityStatus.COMPATIBLE
    raw_delta = candidate_score - reference_score if comparable else None
    favorable_delta = (
        raw_delta
        if comparable and candidate.metric.direction is MetricDirection.HIGHER_IS_BETTER
        else -raw_delta
        if comparable and raw_delta is not None
        else None
    )
    outperforms = favorable_delta > 0.0 if favorable_delta is not None else None
    verdict: Literal[
        "beats_reference",
        "ties_reference",
        "underperforms_reference",
        "non_comparable",
    ] = (
        "non_comparable"
        if favorable_delta is None
        else "beats_reference"
        if favorable_delta > 0.0
        else "underperforms_reference"
        if favorable_delta < 0.0
        else "ties_reference"
    )
    return SOTAComparison(
        comparison_id=comparison_id,
        candidate_protocol_sha256=candidate.protocol_sha256,
        reference_protocol_sha256=reference.protocol_sha256,
        comparability=comparability,
        metric=candidate.metric,
        candidate_score=candidate_score,
        reference_score=reference_score,
        raw_delta=raw_delta,
        favorable_delta=favorable_delta,
        candidate_outperforms=outperforms,
        headline_delta_allowed=comparable,
        headline_verdict=verdict,
        generated_at=generated_at,
    )


class ContradictionCorrectionReport(KnowledgeModel):
    schema_version: Literal[1] = 1
    report_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    corpus_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    claim_graph_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checked_paper_snapshot_sha256s: tuple[str, ...] = Field(min_length=1)
    publication_update_sha256s: tuple[str, ...] = ()
    contradictory_evidence_edge_sha256s: tuple[str, ...] = ()
    contradictory_prior_art_relation_sha256s: tuple[str, ...] = ()
    affected_claim_sha256s: tuple[str, ...] = ()
    unresolved_claim_sha256s: tuple[str, ...] = ()
    correction_retraction_check_complete: bool
    generated_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _report_sets_are_consistent(self) -> "ContradictionCorrectionReport":
        identity_groups = (
            self.checked_paper_snapshot_sha256s,
            self.publication_update_sha256s,
            self.contradictory_evidence_edge_sha256s,
            self.contradictory_prior_art_relation_sha256s,
            self.affected_claim_sha256s,
            self.unresolved_claim_sha256s,
        )
        if any(len(group) != len(set(group)) for group in identity_groups):
            raise ValueError("contradiction/correction report identities must be unique")
        if not set(self.unresolved_claim_sha256s).issubset(self.affected_claim_sha256s):
            raise ValueError("unresolved claims must be a subset of affected claims")
        return self

    @property
    def report_sha256(self) -> str:
        return content_sha256(self)


class KnowledgeBoundarySnapshot(KnowledgeModel):
    """Self-validating issue-12 bundle; no persistence or driver behavior is implied."""

    schema_version: Literal[1] = 1
    snapshot_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    corpus: CorpusSnapshot
    search_protocol: SearchProtocol
    search_session: SearchSession
    coverage_report: CoverageReport
    claim_graph: AtomicClaimGraph
    prior_art_relations: tuple[PriorArtRelation, ...] = Field(min_length=1)
    novelty_assessment: NoveltyAssessment
    protocol_signatures: tuple[ProtocolSignature, ...] = Field(min_length=2)
    sota_comparisons: tuple[SOTAComparison, ...] = Field(min_length=1)
    contradiction_correction_report: ContradictionCorrectionReport
    parent_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _bundle_references_are_closed(self) -> "KnowledgeBoundarySnapshot":
        corpus_sha = self.corpus.snapshot_sha256
        if (
            self.search_protocol.corpus_snapshot_sha256 != corpus_sha
            or self.search_protocol.cutoff_time != self.corpus.cutoff_time
        ):
            raise ValueError("search protocol is bound to another corpus/cutoff")
        if self.search_protocol.frozen_at < self.corpus.frozen_at:
            raise ValueError("search protocol cannot freeze before its corpus")
        if self.search_session.protocol_sha256 != self.search_protocol.protocol_sha256:
            raise ValueError("search session is bound to another protocol")
        if self.search_session.corpus_snapshot_sha256 != corpus_sha:
            raise ValueError("search session is bound to another corpus")
        if self.search_session.started_at < self.search_protocol.frozen_at:
            raise ValueError("search session began before protocol freeze")

        paper_hashes = {paper.snapshot_sha256 for paper in self.corpus.papers}
        source_ids = {source.source_id for source in self.corpus.sources}
        span_hashes = {span.span_sha256 for span in self.corpus.spans}
        if not set(self.search_protocol.planned_source_ids).issubset(source_ids):
            raise ValueError("search protocol includes a source outside the corpus manifest")
        if not set(self.search_protocol.seed_paper_snapshot_sha256s).issubset(paper_hashes):
            raise ValueError("search seed paper is outside the corpus")
        if len(self.search_session.queries) > self.search_protocol.max_queries:
            raise ValueError("search session exceeded its frozen query budget")
        query_families = {query.family for query in self.search_session.queries}
        required_families = set(self.search_protocol.required_query_families) | {
            QueryFamily.CITATION_BACKWARD,
            QueryFamily.CITATION_FORWARD,
        }
        if not required_families.issubset(query_families):
            raise ValueError("search session did not execute every required query family")
        for query in self.search_session.queries:
            if query.source_id not in self.search_protocol.planned_source_ids:
                raise ValueError("search query used an unplanned source")
            if query.round_index >= self.search_protocol.saturation_rule.maximum_rounds:
                raise ValueError("search query exceeded the frozen round budget")
            if any(hit.paper_snapshot_sha256 not in paper_hashes for hit in query.hits):
                raise ValueError("search hit lies outside the frozen corpus")

        if (
            self.coverage_report.corpus_snapshot_sha256 != corpus_sha
            or self.coverage_report.search_session_sha256 != self.search_session.session_sha256
        ):
            raise ValueError("coverage report is bound to another corpus/search session")
        if self.coverage_report.generated_at < self.search_session.ended_at:
            raise ValueError("coverage report predates search completion")
        has_query_failure = (
            any(query.outcome is QueryOutcome.ERROR for query in self.search_session.queries)
            or self.search_session.stopping_reason is SearchStoppingReason.HARD_FAILURE
        )
        if has_query_failure and self.coverage_report.verdict is CoverageVerdict.SUFFICIENT:
            raise ValueError("retrieval failure cannot coexist with sufficient hard coverage")

        if self.claim_graph.corpus_snapshot_sha256 != corpus_sha:
            raise ValueError("claim graph is bound to another corpus")
        claims = {claim.claim_sha256: claim for claim in self.claim_graph.claims}
        candidate_claims = {
            claim_hash
            for claim_hash, claim in claims.items()
            if claim.origin is ClaimOrigin.CANDIDATE
        }
        prior_claims = {
            claim_hash
            for claim_hash, claim in claims.items()
            if claim.origin is ClaimOrigin.PRIOR_ART
        }
        if set(self.search_protocol.candidate_claim_sha256s) != candidate_claims:
            raise ValueError("search protocol does not exactly cover candidate claims")
        for claim in self.claim_graph.claims:
            if claim.asserted_at > self.claim_graph.frozen_at:
                raise ValueError("claim was asserted after graph freeze")
            if (
                claim.origin is ClaimOrigin.PRIOR_ART
                and claim.source_paper_snapshot_sha256 not in paper_hashes
            ):
                raise ValueError("prior claim cites a paper outside the corpus")
        edges = {edge.edge_sha256: edge for edge in self.claim_graph.evidence_edges}
        for edge in edges.values():
            if edge.source_span_sha256 not in span_hashes:
                raise ValueError("claim evidence edge cites a span outside the corpus")
            if edge.reviewed_at is not None and edge.reviewed_at > self.claim_graph.frozen_at:
                raise ValueError("claim evidence was reviewed after graph freeze")

        relation_hashes = [relation.relation_sha256 for relation in self.prior_art_relations]
        if len(relation_hashes) != len(set(relation_hashes)):
            raise ValueError("knowledge snapshot prior-art relations must be unique")
        relation_differences: set[str] = set()
        for relation in self.prior_art_relations:
            if relation.candidate_claim_sha256 not in candidate_claims:
                raise ValueError("prior-art relation has an unknown candidate claim")
            if relation.prior_claim_sha256 not in prior_claims:
                raise ValueError("prior-art relation has an unknown prior claim")
            if not set(relation.evidence_span_sha256s).issubset(span_hashes):
                raise ValueError("prior-art relation cites a span outside the corpus")
            for difference in relation.differences:
                if not set(difference.evidence_span_sha256s).issubset(span_hashes):
                    raise ValueError("prior-art difference cites a span outside the corpus")
                relation_differences.add(content_sha256(difference))

        novelty = self.novelty_assessment
        if (
            novelty.corpus_snapshot_sha256 != corpus_sha
            or novelty.search_session_sha256 != self.search_session.session_sha256
            or novelty.coverage_report_sha256 != self.coverage_report.report_sha256
            or novelty.coverage_verdict is not self.coverage_report.verdict
            or novelty.claim_graph_sha256 != self.claim_graph.graph_sha256
            or novelty.temporal_cutoff != self.corpus.cutoff_time
            or set(novelty.candidate_claim_sha256s) != candidate_claims
            or [item.relation_sha256 for item in novelty.nearest_prior_art] != relation_hashes
        ):
            raise ValueError("novelty assessment is not closed over this evidence bundle")
        if any(
            content_sha256(difference) not in relation_differences
            for difference in novelty.exact_differences
        ):
            raise ValueError("novelty exact difference lacks a prior-art relation")
        if (
            novelty.policy.minimum_nearest_prior_art
            != self.coverage_report.policy.minimum_nearest_prior_art
            or novelty.policy.minimum_independent_reviewers
            != self.coverage_report.policy.minimum_independent_reviewers
        ):
            raise ValueError("coverage and novelty policies disagree on frozen minima")

        correction = self.contradiction_correction_report
        if (
            correction.corpus_snapshot_sha256 != corpus_sha
            or correction.claim_graph_sha256 != self.claim_graph.graph_sha256
        ):
            raise ValueError("contradiction/correction report is bound elsewhere")
        if not set(correction.checked_paper_snapshot_sha256s).issubset(paper_hashes):
            raise ValueError("correction report checked a paper outside the corpus")
        update_hashes = {update.update_sha256 for update in self.corpus.updates}
        if not set(correction.publication_update_sha256s).issubset(update_hashes):
            raise ValueError("correction report cites an update outside the corpus")
        if not set(correction.contradictory_evidence_edge_sha256s).issubset(edges):
            raise ValueError("contradiction report cites an unknown evidence edge")
        if not set(correction.contradictory_prior_art_relation_sha256s).issubset(relation_hashes):
            raise ValueError("contradiction report cites an unknown prior-art relation")
        if not set(correction.affected_claim_sha256s).issubset(claims):
            raise ValueError("contradiction report cites an unknown affected claim")
        correction_signal = next(
            signal
            for signal in self.coverage_report.signals
            if signal.signal is CoverageSignalName.CORRECTION_RETRACTION_CHECK
        )
        if (
            correction_signal.status is CoverageSignalStatus.PASS
            and not correction.correction_retraction_check_complete
        ):
            raise ValueError("passed correction coverage requires a complete correction report")

        protocols = {protocol.protocol_sha256: protocol for protocol in self.protocol_signatures}
        if len(protocols) != len(self.protocol_signatures):
            raise ValueError("protocol signatures must be unique")
        comparison_ids = [comparison.comparison_id for comparison in self.sota_comparisons]
        comparison_hashes = [comparison.comparison_sha256 for comparison in self.sota_comparisons]
        if len(comparison_ids) != len(set(comparison_ids)) or len(comparison_hashes) != len(
            set(comparison_hashes)
        ):
            raise ValueError("SOTA comparisons must have unique IDs and contents")
        for comparison in self.sota_comparisons:
            candidate = protocols.get(comparison.candidate_protocol_sha256)
            reference = protocols.get(comparison.reference_protocol_sha256)
            if candidate is None or reference is None:
                raise ValueError("SOTA comparison references an unknown protocol")
            expected = assess_protocol_comparability(
                candidate,
                reference,
                assessed_at=comparison.comparability.assessed_at,
            )
            if comparison.comparability != expected:
                raise ValueError("SOTA comparability does not match frozen protocols")
            if comparison.metric.comparability_sha256 != candidate.metric.comparability_sha256:
                raise ValueError("SOTA comparison uses another candidate metric")

        artifact_times = [
            self.corpus.frozen_at,
            self.search_protocol.frozen_at,
            self.search_session.ended_at,
            self.coverage_report.generated_at,
            self.claim_graph.frozen_at,
            novelty.assessed_at,
            correction.generated_at,
            *(protocol.evaluation_date for protocol in self.protocol_signatures),
            *(comparison.generated_at for comparison in self.sota_comparisons),
        ]
        if any(timestamp > self.frozen_at for timestamp in artifact_times):
            raise ValueError("knowledge boundary froze before one of its artifacts")
        return self

    @property
    def snapshot_sha256(self) -> str:
        return content_sha256(self)
