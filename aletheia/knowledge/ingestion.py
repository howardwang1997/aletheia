"""Provider-neutral, license-explicit corpus ingestion contracts for F8-S1.

No provider or network implementation lives here. Adapters must normalize their records into these
objects, and persistence stores only identities/locators/policy evidence by default—not licensed
paper text.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.knowledge.schemas import (
    CorpusSnapshot,
    KnowledgeModel,
    TextAvailability,
)
from aletheia.reproducibility.manifest import content_sha256


class ContentAccessClass(str, Enum):
    OPEN_ACCESS = "open_access"
    INSTITUTIONAL = "institutional"
    USER_PROVIDED = "user_provided"
    METADATA_ONLY = "metadata_only"


class ContentUse(str, Enum):
    METADATA_INDEX = "metadata_index"
    ABSTRACT_PROCESSING = "abstract_processing"
    FULL_TEXT_PROCESSING = "full_text_processing"
    SPAN_EXTRACTION = "span_extraction"
    MODEL_INPUT = "model_input"
    RETAIN_CONTENT = "retain_content"
    REDISTRIBUTE_CONTENT = "redistribute_content"


class ContentRetention(str, Enum):
    HASH_ONLY = "hash_only"
    ENCRYPTED_CONTENT = "encrypted_content"


class ProviderRetrievalMode(str, Enum):
    AUTOMATED = "automated"
    MANUAL_IMPORT = "manual_import"


class LicenseEvidenceStatus(str, Enum):
    ARTICLE_LEVEL_TERMS = "article_level_terms"
    INSTITUTIONAL_CONTRACT = "institutional_contract"
    USER_ATTESTATION = "user_attestation"
    UNKNOWN = "unknown"


class ContentAccessPolicy(KnowledgeModel):
    schema_version: Literal[1] = 1
    policy_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    allowed_access_classes: tuple[ContentAccessClass, ...] = Field(min_length=1)
    allowed_uses: tuple[ContentUse, ...] = Field(min_length=1)
    default_retention: ContentRetention = ContentRetention.HASH_ONLY
    full_text_requires_article_terms: Literal[True] = True
    model_input_requires_explicit_use: Literal[True] = True
    unknown_license_maximum: Literal["metadata_only"] = "metadata_only"
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _policy_sets_are_unique(self) -> "ContentAccessPolicy":
        if len(self.allowed_access_classes) != len(set(self.allowed_access_classes)):
            raise ValueError("content access classes must be unique")
        if len(self.allowed_uses) != len(set(self.allowed_uses)):
            raise ValueError("content uses must be unique")
        if ContentUse.METADATA_INDEX not in self.allowed_uses:
            raise ValueError("content policy must allow metadata indexing")
        if (
            self.default_retention is ContentRetention.ENCRYPTED_CONTENT
            and ContentUse.RETAIN_CONTENT not in self.allowed_uses
        ):
            raise ValueError("encrypted retention requires retained-content permission")
        return self

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class ContentAccessGrant(KnowledgeModel):
    schema_version: Literal[1] = 1
    grant_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    paper_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text_capability: TextAvailability
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    access_class: ContentAccessClass
    license_id: str = Field(min_length=1, max_length=256)
    license_terms_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    license_evidence_status: LicenseEvidenceStatus
    terms_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_url: str = Field(min_length=9, max_length=4096)
    permitted_uses: tuple[ContentUse, ...] = Field(min_length=1)
    retention: ContentRetention = ContentRetention.HASH_ONLY
    automated_retrieval_permitted: bool
    redistributable: bool
    observed_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _grant_does_not_infer_rights(self) -> "ContentAccessGrant":
        uses = set(self.permitted_uses)
        if len(uses) != len(self.permitted_uses):
            raise ValueError("content grant uses must be unique")
        if ContentUse.METADATA_INDEX not in uses:
            raise ValueError("every content grant requires metadata-index permission")
        if not self.source_url.startswith("https://"):
            raise ValueError("content grant source URL must use HTTPS")
        if self.expires_at is not None and self.expires_at <= self.observed_at:
            raise ValueError("content grant expiry must follow observation")
        if self.redistributable != (ContentUse.REDISTRIBUTE_CONTENT in uses):
            raise ValueError("redistributable flag must match explicit redistribution permission")
        if self.retention is ContentRetention.HASH_ONLY:
            if ContentUse.RETAIN_CONTENT in uses:
                raise ValueError("hash-only grant cannot claim retained-content permission")
        elif ContentUse.RETAIN_CONTENT not in uses:
            raise ValueError("encrypted content retention requires explicit permission")

        if self.text_capability is TextAvailability.METADATA_ONLY:
            content_uses = {
                ContentUse.ABSTRACT_PROCESSING,
                ContentUse.FULL_TEXT_PROCESSING,
                ContentUse.SPAN_EXTRACTION,
                ContentUse.MODEL_INPUT,
                ContentUse.RETAIN_CONTENT,
                ContentUse.REDISTRIBUTE_CONTENT,
            }
            if self.content_sha256 is not None or uses.intersection(content_uses):
                raise ValueError("metadata-only grant cannot claim text content or text uses")
            if self.access_class is not ContentAccessClass.METADATA_ONLY:
                raise ValueError("metadata-only capability requires metadata-only access class")
        elif self.content_sha256 is None:
            raise ValueError("abstract/full-text grant requires exact content identity")
        elif self.license_evidence_status is LicenseEvidenceStatus.UNKNOWN:
            raise ValueError("unknown license evidence cannot grant abstract/full-text processing")
        elif self.text_capability is TextAvailability.ABSTRACT:
            if ContentUse.ABSTRACT_PROCESSING not in uses:
                raise ValueError("abstract capability requires abstract-processing permission")
            if ContentUse.FULL_TEXT_PROCESSING in uses:
                raise ValueError("abstract capability cannot claim full-text processing")
        else:
            required = {ContentUse.FULL_TEXT_PROCESSING, ContentUse.SPAN_EXTRACTION}
            if not required.issubset(uses):
                raise ValueError("full-text capability requires processing and span extraction")
        if self.access_class is ContentAccessClass.OPEN_ACCESS and (
            self.license_evidence_status is not LicenseEvidenceStatus.ARTICLE_LEVEL_TERMS
        ):
            raise ValueError("open-access text requires article-level license evidence")
        if self.access_class is ContentAccessClass.INSTITUTIONAL and (
            self.license_evidence_status
            not in {
                LicenseEvidenceStatus.ARTICLE_LEVEL_TERMS,
                LicenseEvidenceStatus.INSTITUTIONAL_CONTRACT,
            }
        ):
            raise ValueError("institutional text requires article terms or contract evidence")
        if self.access_class is ContentAccessClass.USER_PROVIDED and (
            self.license_evidence_status
            not in {
                LicenseEvidenceStatus.ARTICLE_LEVEL_TERMS,
                LicenseEvidenceStatus.USER_ATTESTATION,
            }
        ):
            raise ValueError("user-provided text requires article terms or user attestation")
        return self

    @property
    def grant_sha256(self) -> str:
        return content_sha256(self)


class ProviderIngestReceipt(KnowledgeModel):
    schema_version: Literal[1] = 1
    receipt_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_record_id: str = Field(min_length=1, max_length=1024)
    raw_response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalizer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    paper_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_span_sha256s: tuple[str, ...] = ()
    access_grant_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    retrieval_mode: ProviderRetrievalMode
    fetched_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _span_identities_are_unique(self) -> "ProviderIngestReceipt":
        if len(self.source_span_sha256s) != len(set(self.source_span_sha256s)):
            raise ValueError("provider receipt source spans must be unique")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class CorpusIngestionBundle(KnowledgeModel):
    schema_version: Literal[1] = 1
    bundle_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    access_policy: ContentAccessPolicy
    corpus: CorpusSnapshot
    access_grants: tuple[ContentAccessGrant, ...] = Field(min_length=1)
    provider_receipts: tuple[ProviderIngestReceipt, ...] = Field(min_length=1)
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _bundle_is_closed_and_license_explicit(self) -> "CorpusIngestionBundle":
        if self.corpus.license_policy_sha256 != self.access_policy.policy_sha256:
            raise ValueError("corpus is bound to another content-access policy")
        if self.access_policy.frozen_at > self.corpus.frozen_at:
            raise ValueError("content-access policy froze after the corpus")
        if self.corpus.frozen_at > self.frozen_at:
            raise ValueError("ingestion bundle froze before its corpus")

        papers = {paper.snapshot_sha256: paper for paper in self.corpus.papers}
        sources = {source.manifest_sha256 for source in self.corpus.sources}
        spans_by_paper: dict[str, list[str]] = {paper_sha: [] for paper_sha in papers}
        for span in self.corpus.spans:
            spans_by_paper[span.paper_snapshot_sha256].append(span.span_sha256)

        grants = {grant.paper_snapshot_sha256: grant for grant in self.access_grants}
        grant_hashes = {grant.grant_sha256 for grant in self.access_grants}
        if len(grants) != len(self.access_grants) or set(grants) != set(papers):
            raise ValueError("ingestion bundle requires exactly one access grant per paper")
        allowed_classes = set(self.access_policy.allowed_access_classes)
        allowed_uses = set(self.access_policy.allowed_uses)
        for paper_sha, grant in grants.items():
            paper = papers[paper_sha]
            if grant.policy_sha256 != self.access_policy.policy_sha256:
                raise ValueError("content grant is bound to another access policy")
            if grant.source_manifest_sha256 not in sources:
                raise ValueError("content grant cites a source outside the corpus")
            if grant.access_class not in allowed_classes or not set(grant.permitted_uses).issubset(
                allowed_uses
            ):
                raise ValueError("content grant exceeds the frozen access policy")
            if (
                grant.text_capability is not paper.text_availability
                or grant.content_sha256 != paper.text_content_sha256
                or grant.license_id != paper.license_id
                or grant.license_terms_sha256 != paper.license_terms_sha256
            ):
                raise ValueError("content grant does not match its paper snapshot")
            if grant.observed_at > self.corpus.frozen_at:
                raise ValueError("content grant was observed after corpus freeze")

        receipts = {receipt.paper_snapshot_sha256: receipt for receipt in self.provider_receipts}
        if len(receipts) != len(self.provider_receipts) or set(receipts) != set(papers):
            raise ValueError("ingestion bundle requires exactly one provider receipt per paper")
        for paper_sha, receipt in receipts.items():
            paper = papers[paper_sha]
            grant = grants[paper_sha]
            if receipt.source_manifest_sha256 not in sources:
                raise ValueError("provider receipt cites a source outside the corpus")
            if receipt.access_grant_sha256 not in grant_hashes or (
                receipt.access_grant_sha256 != grant.grant_sha256
            ):
                raise ValueError("provider receipt is bound to another content grant")
            if receipt.source_manifest_sha256 != grant.source_manifest_sha256:
                raise ValueError("provider receipt and content grant cite different sources")
            if (
                receipt.retrieval_mode is ProviderRetrievalMode.AUTOMATED
                and not grant.automated_retrieval_permitted
            ):
                raise ValueError("automated provider receipt lacks retrieval permission")
            if set(receipt.source_span_sha256s) != set(spans_by_paper[paper_sha]):
                raise ValueError("provider receipt does not exactly cover its paper spans")
            if receipt.fetched_at < paper.version_public_at:
                raise ValueError("provider receipt predates the paper version")
            if receipt.fetched_at > self.corpus.frozen_at:
                raise ValueError("provider receipt was fetched after corpus freeze")
            if grant.expires_at is not None and receipt.fetched_at >= grant.expires_at:
                raise ValueError("provider receipt was fetched after access-grant expiry")
        return self

    @property
    def bundle_sha256(self) -> str:
        return content_sha256(self)
