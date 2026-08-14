from __future__ import annotations

import hashlib

import aletheia.knowledge as k
from .test_schema_spike import _build_bundle, _time


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _revalidate(model_type, model, **updates):
    payload = model.model_dump(mode="python")
    payload.update(updates)
    return model_type.model_validate(payload)


def build_ingestion_bundle(suffix: str = "base") -> k.CorpusIngestionBundle:
    issue12 = _build_bundle()["bundle"]
    policy = k.ContentAccessPolicy(
        policy_id=f"f8s1-access-policy:{suffix}",
        allowed_access_classes=(
            "open_access",
            "institutional",
            "user_provided",
            "metadata_only",
        ),
        allowed_uses=(
            "metadata_index",
            "abstract_processing",
            "full_text_processing",
            "span_extraction",
        ),
        default_retention="hash_only",
        frozen_at=_time("2024-12-01T00:00:00Z"),
    )
    corpus = _revalidate(
        k.CorpusSnapshot,
        issue12.corpus,
        snapshot_id=f"f8s1-corpus-{suffix}",
        version="1",
        license_policy_sha256=policy.policy_sha256,
    )
    source = corpus.sources[0]
    spans_by_paper: dict[str, tuple[str, ...]] = {}
    for paper in corpus.papers:
        spans_by_paper[paper.snapshot_sha256] = tuple(
            span.span_sha256
            for span in corpus.spans
            if span.paper_snapshot_sha256 == paper.snapshot_sha256
        )

    grants: list[k.ContentAccessGrant] = []
    receipts: list[k.ProviderIngestReceipt] = []
    for index, paper in enumerate(corpus.papers):
        uses = (
            ("metadata_index", "full_text_processing", "span_extraction")
            if paper.text_availability is k.TextAvailability.FULL_TEXT
            else ("metadata_index", "abstract_processing")
            if paper.text_availability is k.TextAvailability.ABSTRACT
            else ("metadata_index",)
        )
        grant = k.ContentAccessGrant(
            grant_id=f"grant:{suffix}:{index}",
            policy_sha256=policy.policy_sha256,
            source_manifest_sha256=source.manifest_sha256,
            paper_snapshot_sha256=paper.snapshot_sha256,
            text_capability=paper.text_availability,
            content_sha256=paper.text_content_sha256,
            access_class=(
                "metadata_only"
                if paper.text_availability is k.TextAvailability.METADATA_ONLY
                else "open_access"
            ),
            license_id=paper.license_id,
            license_terms_sha256=paper.license_terms_sha256,
            license_evidence_status="article_level_terms",
            terms_evidence_sha256=_sha(f"article-terms:{paper.snapshot_sha256}"),
            source_url=paper.source_urls[0],
            permitted_uses=uses,
            retention="hash_only",
            automated_retrieval_permitted=True,
            redistributable=False,
            observed_at=paper.observed_at,
        )
        receipt = k.ProviderIngestReceipt(
            receipt_id=f"provider-receipt:{suffix}:{index}",
            source_manifest_sha256=source.manifest_sha256,
            provider_record_id=f"provider-record-{index}",
            raw_response_sha256=_sha(f"raw-provider-response:{suffix}:{index}"),
            normalizer_sha256=_sha("f8s1-normalizer-v1"),
            paper_snapshot_sha256=paper.snapshot_sha256,
            source_span_sha256s=spans_by_paper[paper.snapshot_sha256],
            access_grant_sha256=grant.grant_sha256,
            retrieval_mode="automated",
            fetched_at=paper.observed_at,
        )
        grants.append(grant)
        receipts.append(receipt)
    return k.CorpusIngestionBundle(
        bundle_id=f"f8s1-ingestion-bundle:{suffix}",
        access_policy=policy,
        corpus=corpus,
        access_grants=tuple(grants),
        provider_receipts=tuple(receipts),
        frozen_at=corpus.frozen_at,
    )
