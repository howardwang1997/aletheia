"""File-backed knowledge input for bounded campaigns (ARL-2 roadmap step 1).

F8's typed ingestion chain is offline-green but quarantined: nothing produces a
:class:`CorpusIngestionBundle` from real campaign material, and the control plane imports none
of it.  This module is the bridge for campaigns whose knowledge input is a fixed, PI-reviewed
corpus directory rather than a live provider: every paper is an operator-supplied copy of a
published record, imported in ``MANUAL_IMPORT`` mode with user attestation, no automated
retrieval, and no redistribution.  Live provider calibration stays a separate exit by design.

The corpus directory layout::

    corpus/
      manifest.json        campaign-level manifest (timestamps explicit, no clock reads)
      papers/<id>.txt      the operator-supplied text of each paper version

``manifest.json`` carries one entry per paper: publication metadata, an HTTPS source URL,
license terms text with an evidence status, the text file reference, and the claim spans with
character ranges into the text file.  Construction is deterministic: every timestamp comes from
the manifest, iteration order is sorted, and span text must equal the referenced file slice or
construction fails closed.  The output bundle is closed and license-explicit by the F8
contracts themselves; nothing here relaxes them.

Deliberately absent: claim extraction (the F8 extraction protocol rides the authoring PR that
consumes claims) and coverage reporting (rides question admission).  This module builds and
verifies the bundle only.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from aletheia.knowledge.ingestion import (
    ContentAccessClass,
    ContentAccessGrant,
    ContentAccessPolicy,
    ContentRetention,
    ContentUse,
    CorpusIngestionBundle,
    LicenseEvidenceStatus,
    ProviderIngestReceipt,
    ProviderRetrievalMode,
)
from aletheia.knowledge.schemas import (
    CorpusSnapshot,
    CorpusSourceVersion,
    ExtractionMethod,
    PaperSnapshot,
    PeerReviewStatus,
    PublicationType,
    SourceSpan,
    SpanLocator,
    TemporalSnapshotMode,
    TextAvailability,
    TextScope,
)

NORMALIZER_ID = "aletheia.research_controller.file_backed_corpus.v1"
NORMALIZER_SHA256 = hashlib.sha256(NORMALIZER_ID.encode("utf-8")).hexdigest()

_HTTPS = re.compile(r"^https://")


class CorpusDirectoryError(ValueError):
    """The corpus directory is not a valid fixed knowledge input."""


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _parse_time(value: str, label: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise CorpusDirectoryError(f"{label} must be timezone-aware UTC")
    return parsed


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CorpusDirectoryError(message)


def build_file_backed_bundle(corpus_dir: Path) -> CorpusIngestionBundle:
    """Build one closed ingestion bundle from a fixed corpus directory; fail closed on drift."""

    manifest_path = corpus_dir / "manifest.json"
    _require(manifest_path.is_file(), f"corpus directory lacks manifest.json: {corpus_dir}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    cutoff = _parse_time(manifest["cutoff_time"], "cutoff_time")
    frozen_at = _parse_time(manifest["frozen_at"], "frozen_at")
    policy_frozen_at = _parse_time(manifest["policy_frozen_at"], "policy_frozen_at")
    retrieved_at = _parse_time(manifest["source"]["retrieved_at"], "source retrieved_at")
    updated_through = _parse_time(manifest["source"]["updated_through"], "source updated_through")
    _require(frozen_at >= cutoff, "corpus cannot freeze before its cutoff")
    _require(retrieved_at >= updated_through, "source cannot be retrieved before its boundary")

    terms_text = manifest["source"]["terms"]
    _require(bool(terms_text.strip()), "corpus source must record license terms text")
    source = CorpusSourceVersion(
        source_id=manifest["source"]["source_id"],
        snapshot_id=manifest["source"]["snapshot_id"],
        snapshot_sha256=_sha256_text(json.dumps(manifest["source"], sort_keys=True)),
        updated_through=updated_through,
        retrieved_at=retrieved_at,
        license_id=manifest["source"]["license_id"],
        terms_sha256=_sha256_text(terms_text),
        as_of_evidence_sha256=_sha256_text(
            manifest_path.read_text(encoding="utf-8")
        ),
    )

    policy = ContentAccessPolicy(
        policy_id=manifest["policy_id"],
        allowed_access_classes=(ContentAccessClass.USER_PROVIDED,),
        allowed_uses=(
            ContentUse.METADATA_INDEX,
            ContentUse.ABSTRACT_PROCESSING,
            ContentUse.SPAN_EXTRACTION,
        ),
        frozen_at=policy_frozen_at,
    )

    papers: list[PaperSnapshot] = []
    spans: list[SourceSpan] = []
    grants: list[ContentAccessGrant] = []
    receipts: list[ProviderIngestReceipt] = []

    for entry in sorted(manifest["papers"], key=lambda item: item["paper_id"]):
        paper_id = entry["paper_id"]
        text_path = corpus_dir / entry["text_file"]
        _require(text_path.is_file(), f"paper {paper_id} text file missing: {entry['text_file']}")
        text = text_path.read_text(encoding="utf-8")
        version_public_at = _parse_time(
            entry["version_public_at"], f"paper {paper_id} version_public_at"
        )
        observed_at = _parse_time(entry["observed_at"], f"paper {paper_id} observed_at")
        first_public_at = _parse_time(
            entry["first_public_at"], f"paper {paper_id} first_public_at"
        )
        _require(
            bool(entry["source_urls"]),
            f"paper {paper_id} must record at least one source URL",
        )
        _require(
            _HTTPS.match(entry["source_urls"][0]) is not None,
            f"paper {paper_id} source URL must use HTTPS",
        )
        paper_terms = entry["license_terms"]
        _require(bool(paper_terms.strip()), f"paper {paper_id} must record license terms text")

        paper = PaperSnapshot(
            canonical_id=entry["canonical_id"],
            version_id=entry["version_id"],
            title=entry["title"],
            authors=tuple(entry["authors"]),
            venue=entry.get("venue"),
            publication_type=PublicationType(entry["publication_type"]),
            first_public_at=first_public_at,
            version_public_at=version_public_at,
            observed_at=observed_at,
            doi=entry.get("doi"),
            source_urls=tuple(entry["source_urls"]),
            metadata_sha256=_sha256_text(json.dumps(entry, sort_keys=True)),
            text_availability=TextAvailability.ABSTRACT,
            text_content_sha256=_sha256_text(text),
            license_id=entry["license_id"],
            license_terms_sha256=_sha256_text(paper_terms),
            peer_review_status=PeerReviewStatus(entry.get("peer_review_status", "unknown")),
            as_of_evidence_sha256=_sha256_text(text),
        )
        papers.append(paper)

        paper_spans: list[str] = []
        for span_entry in sorted(entry.get("spans", []), key=lambda item: item["span_id"]):
            span_text = span_entry["text"]
            start = span_entry["char_start"]
            end = span_entry["char_end"]
            _require(
                0 <= start < end <= len(text),
                f"paper {paper_id} span {span_entry['span_id']} character range out of bounds",
            )
            _require(
                text[start:end] == span_text,
                f"paper {paper_id} span {span_entry['span_id']} text differs from its file slice",
            )
            span = SourceSpan(
                span_id=span_entry["span_id"],
                paper_snapshot_sha256=paper.snapshot_sha256,
                text_scope=TextScope.ABSTRACT,
                locator=SpanLocator(
                    section=span_entry.get("section"),
                    char_start=start,
                    char_end=end,
                    normalized_span_sha256=_sha256_text(_normalized(span_text)),
                ),
                exact_text_sha256=_sha256_text(span_text),
                normalized_text_sha256=_sha256_text(_normalized(span_text)),
                text_bytes=len(span_text.encode("utf-8")),
                extraction_method=ExtractionMethod.MANUAL,
                extraction_confidence=1.0,
                extracted_at=observed_at,
            )
            spans.append(span)
            paper_spans.append(span.span_sha256)

        grant = ContentAccessGrant(
            grant_id=f"grant:{paper_id}",
            policy_sha256=policy.policy_sha256,
            source_manifest_sha256=source.manifest_sha256,
            paper_snapshot_sha256=paper.snapshot_sha256,
            text_capability=TextAvailability.ABSTRACT,
            content_sha256=paper.text_content_sha256,
            access_class=ContentAccessClass.USER_PROVIDED,
            license_id=paper.license_id,
            license_terms_sha256=paper.license_terms_sha256,
            license_evidence_status=LicenseEvidenceStatus(
                entry.get("license_evidence_status", "user_attestation")
            ),
            terms_evidence_sha256=_sha256_text(paper_terms),
            source_url=entry["source_urls"][0],
            permitted_uses=(
                ContentUse.METADATA_INDEX,
                ContentUse.ABSTRACT_PROCESSING,
                ContentUse.SPAN_EXTRACTION,
            ),
            retention=ContentRetention.HASH_ONLY,
            automated_retrieval_permitted=False,
            redistributable=False,
            observed_at=observed_at,
        )
        grants.append(grant)

        receipts.append(
            ProviderIngestReceipt(
                receipt_id=f"receipt:{paper_id}",
                source_manifest_sha256=source.manifest_sha256,
                provider_record_id=f"file:{entry['text_file']}",
                raw_response_sha256=_sha256_text(text),
                normalizer_sha256=NORMALIZER_SHA256,
                paper_snapshot_sha256=paper.snapshot_sha256,
                source_span_sha256s=tuple(sorted(paper_spans)),
                access_grant_sha256=grant.grant_sha256,
                retrieval_mode=ProviderRetrievalMode.MANUAL_IMPORT,
                fetched_at=retrieved_at,
            )
        )
        _require(
            version_public_at <= retrieved_at <= frozen_at,
            f"paper {paper_id} retrieval time must fall between publication and freeze",
        )
        _require(
            observed_at <= frozen_at,
            f"paper {paper_id} cannot be observed after the corpus freeze",
        )

    corpus = CorpusSnapshot(
        snapshot_id=manifest["snapshot_id"],
        version=manifest["snapshot_version"],
        cutoff_time=cutoff,
        temporal_mode=TemporalSnapshotMode.RECONSTRUCTED,
        sources=(source,),
        papers=tuple(papers),
        spans=tuple(spans),
        license_policy_sha256=policy.policy_sha256,
        frozen_at=frozen_at,
    )
    return CorpusIngestionBundle(
        bundle_id=manifest["bundle_id"],
        access_policy=policy,
        corpus=corpus,
        access_grants=tuple(grants),
        provider_receipts=tuple(receipts),
        frozen_at=frozen_at,
    )


def register_campaign_corpus(corpus_dir: Path) -> CorpusIngestionBundle:
    """Build, re-validate, and return the campaign knowledge bundle.

    Persistence into knowledge custody (``persist_ingestion_bundle``) is the campaign runtime's
    call: this function stays pure so tests and replay can rebuild the identical bundle from the
    directory alone.
    """

    bundle = build_file_backed_bundle(corpus_dir)
    rebuilt = build_file_backed_bundle(corpus_dir)
    if rebuilt.bundle_sha256 != bundle.bundle_sha256:
        raise CorpusDirectoryError("corpus directory does not rebuild deterministically")
    return bundle


__all__ = [
    "CorpusDirectoryError",
    "NORMALIZER_ID",
    "NORMALIZER_SHA256",
    "build_file_backed_bundle",
    "register_campaign_corpus",
]
