"""Immutable PostgreSQL persistence for F8 corpus-ingestion bundles.

This module is intentionally not imported by the scheduler or ``aletheia.knowledge`` package
exports. It provides the F8-S1 storage boundary only; provider adapters and research-driver wiring
remain separate work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, insert as postgresql_insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from aletheia.db import Base, session_scope
from aletheia.knowledge.ingestion import (
    ContentAccessGrant,
    ContentAccessPolicy,
    CorpusIngestionBundle,
    ProviderIngestReceipt,
)
from aletheia.knowledge.schemas import (
    CorpusSnapshot,
    CorpusSourceVersion,
    PaperSnapshot,
    PublicationUpdate,
    SourceSpan,
)


class KnowledgePersistenceError(RuntimeError):
    """Persisted knowledge is absent, conflicting, or no longer self-validating."""


class ImmutableKnowledgeConflict(KnowledgePersistenceError):
    """A stable knowledge identity is already bound to different content."""


class KnowledgeObjectNotFound(KnowledgePersistenceError):
    """A requested immutable knowledge bundle does not exist."""


class KnowledgeAccessPolicyRecord(Base):
    __tablename__ = "knowledge_access_policies"

    policy_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    policy_id: Mapped[str] = mapped_column(String(192), index=True)
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)


class KnowledgeCorpusSourceRecord(Base):
    __tablename__ = "knowledge_corpus_sources"
    __table_args__ = (
        UniqueConstraint("source_id", "snapshot_id", name="uq_knowledge_source_snapshot_identity"),
    )

    manifest_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(80), index=True)
    snapshot_id: Mapped[str] = mapped_column(String(256))
    snapshot_sha256: Mapped[str] = mapped_column(String(64))
    updated_through: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    license_id: Mapped[str] = mapped_column(String(256))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)


class KnowledgePaperSnapshotRecord(Base):
    __tablename__ = "knowledge_paper_snapshots"
    __table_args__ = (
        Index(
            "ix_knowledge_paper_version",
            "canonical_id",
            "version_id",
            "text_availability",
        ),
    )

    paper_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    canonical_id: Mapped[str] = mapped_column(String(512), index=True)
    version_id: Mapped[str] = mapped_column(String(256))
    version_public_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    text_availability: Mapped[str] = mapped_column(String(32))
    text_content_sha256: Mapped[str | None] = mapped_column(String(64))
    license_id: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)


class KnowledgePaperTextIdentityRecord(Base):
    """One content hash per publication version and available-text scope."""

    __tablename__ = "knowledge_paper_text_identities"

    canonical_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    version_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    text_availability: Mapped[str] = mapped_column(String(32), primary_key=True)
    text_content_sha256: Mapped[str] = mapped_column(String(64))
    first_paper_sha256: Mapped[str] = mapped_column(
        ForeignKey("knowledge_paper_snapshots.paper_sha256")
    )


class KnowledgeSourceSpanRecord(Base):
    __tablename__ = "knowledge_source_spans"
    __table_args__ = (Index("ix_knowledge_span_paper_span_id", "paper_sha256", "span_id"),)

    span_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    span_id: Mapped[str] = mapped_column(String(256), index=True)
    paper_sha256: Mapped[str] = mapped_column(
        ForeignKey("knowledge_paper_snapshots.paper_sha256"), index=True
    )
    text_scope: Mapped[str] = mapped_column(String(32))
    exact_text_sha256: Mapped[str] = mapped_column(String(64))
    normalized_text_sha256: Mapped[str] = mapped_column(String(64))
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)


class KnowledgePublicationUpdateRecord(Base):
    __tablename__ = "knowledge_publication_updates"
    __table_args__ = (UniqueConstraint("update_id", name="uq_knowledge_update_id"),)

    update_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    update_id: Mapped[str] = mapped_column(String(256), index=True)
    update_type: Mapped[str] = mapped_column(String(32))
    target_canonical_id: Mapped[str] = mapped_column(String(512), index=True)
    notice_paper_sha256: Mapped[str] = mapped_column(
        ForeignKey("knowledge_paper_snapshots.paper_sha256")
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)


class KnowledgeContentAccessGrantRecord(Base):
    __tablename__ = "knowledge_content_access_grants"
    __table_args__ = (UniqueConstraint("grant_id", name="uq_knowledge_access_grant_id"),)

    grant_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    grant_id: Mapped[str] = mapped_column(String(192), index=True)
    policy_sha256: Mapped[str] = mapped_column(
        ForeignKey("knowledge_access_policies.policy_sha256"), index=True
    )
    source_manifest_sha256: Mapped[str] = mapped_column(
        ForeignKey("knowledge_corpus_sources.manifest_sha256"), index=True
    )
    paper_sha256: Mapped[str] = mapped_column(
        ForeignKey("knowledge_paper_snapshots.paper_sha256"), index=True
    )
    access_class: Mapped[str] = mapped_column(String(32))
    text_capability: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)


class KnowledgeProviderReceiptRecord(Base):
    __tablename__ = "knowledge_provider_receipts"
    __table_args__ = (UniqueConstraint("receipt_id", name="uq_knowledge_provider_receipt_id"),)

    receipt_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    receipt_id: Mapped[str] = mapped_column(String(192), index=True)
    source_manifest_sha256: Mapped[str] = mapped_column(
        ForeignKey("knowledge_corpus_sources.manifest_sha256"), index=True
    )
    paper_sha256: Mapped[str] = mapped_column(
        ForeignKey("knowledge_paper_snapshots.paper_sha256"), index=True
    )
    grant_sha256: Mapped[str] = mapped_column(
        ForeignKey("knowledge_content_access_grants.grant_sha256"), index=True
    )
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)


class KnowledgeCorpusSnapshotRecord(Base):
    __tablename__ = "knowledge_corpus_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "version", name="uq_knowledge_corpus_snapshot_version"),
    )

    corpus_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[str] = mapped_column(String(128))
    cutoff_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    temporal_mode: Mapped[str] = mapped_column(String(32))
    license_policy_sha256: Mapped[str] = mapped_column(
        ForeignKey("knowledge_access_policies.policy_sha256"), index=True
    )
    parent_snapshot_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)


class KnowledgeIngestionBundleRecord(Base):
    __tablename__ = "knowledge_ingestion_bundles"
    __table_args__ = (UniqueConstraint("bundle_id", name="uq_knowledge_ingestion_bundle_id"),)

    bundle_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    bundle_id: Mapped[str] = mapped_column(String(192), index=True)
    corpus_sha256: Mapped[str] = mapped_column(
        ForeignKey("knowledge_corpus_snapshots.corpus_sha256"), index=True
    )
    policy_sha256: Mapped[str] = mapped_column(
        ForeignKey("knowledge_access_policies.policy_sha256"), index=True
    )
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)


class KnowledgeCorpusSourceMember(Base):
    __tablename__ = "knowledge_corpus_source_members"
    __table_args__ = (
        UniqueConstraint("corpus_sha256", "ordinal", name="uq_knowledge_corpus_source_order"),
    )

    corpus_sha256: Mapped[str] = mapped_column(
        ForeignKey("knowledge_corpus_snapshots.corpus_sha256"), primary_key=True
    )
    member_sha256: Mapped[str] = mapped_column(
        ForeignKey("knowledge_corpus_sources.manifest_sha256"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)


class KnowledgeCorpusPaperMember(Base):
    __tablename__ = "knowledge_corpus_paper_members"
    __table_args__ = (
        UniqueConstraint("corpus_sha256", "ordinal", name="uq_knowledge_corpus_paper_order"),
    )

    corpus_sha256: Mapped[str] = mapped_column(
        ForeignKey("knowledge_corpus_snapshots.corpus_sha256"), primary_key=True
    )
    member_sha256: Mapped[str] = mapped_column(
        ForeignKey("knowledge_paper_snapshots.paper_sha256"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)


class KnowledgeCorpusSpanMember(Base):
    __tablename__ = "knowledge_corpus_span_members"
    __table_args__ = (
        UniqueConstraint("corpus_sha256", "ordinal", name="uq_knowledge_corpus_span_order"),
    )

    corpus_sha256: Mapped[str] = mapped_column(
        ForeignKey("knowledge_corpus_snapshots.corpus_sha256"), primary_key=True
    )
    member_sha256: Mapped[str] = mapped_column(
        ForeignKey("knowledge_source_spans.span_sha256"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)


class KnowledgeCorpusUpdateMember(Base):
    __tablename__ = "knowledge_corpus_update_members"
    __table_args__ = (
        UniqueConstraint("corpus_sha256", "ordinal", name="uq_knowledge_corpus_update_order"),
    )

    corpus_sha256: Mapped[str] = mapped_column(
        ForeignKey("knowledge_corpus_snapshots.corpus_sha256"), primary_key=True
    )
    member_sha256: Mapped[str] = mapped_column(
        ForeignKey("knowledge_publication_updates.update_sha256"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)


class KnowledgeBundleGrantMember(Base):
    __tablename__ = "knowledge_bundle_grant_members"
    __table_args__ = (
        UniqueConstraint("bundle_sha256", "ordinal", name="uq_knowledge_bundle_grant_order"),
    )

    bundle_sha256: Mapped[str] = mapped_column(
        ForeignKey("knowledge_ingestion_bundles.bundle_sha256"), primary_key=True
    )
    member_sha256: Mapped[str] = mapped_column(
        ForeignKey("knowledge_content_access_grants.grant_sha256"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)


class KnowledgeBundleReceiptMember(Base):
    __tablename__ = "knowledge_bundle_receipt_members"
    __table_args__ = (
        UniqueConstraint("bundle_sha256", "ordinal", name="uq_knowledge_bundle_receipt_order"),
    )

    bundle_sha256: Mapped[str] = mapped_column(
        ForeignKey("knowledge_ingestion_bundles.bundle_sha256"), primary_key=True
    )
    member_sha256: Mapped[str] = mapped_column(
        ForeignKey("knowledge_provider_receipts.receipt_sha256"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)


@dataclass(frozen=True)
class PersistenceResult:
    bundle_sha256: str
    corpus_sha256: str
    created: bool
    source_count: int
    paper_count: int
    span_count: int
    update_count: int


_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _payload(model: BaseModel, *, exclude: set[str] | None = None) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude=exclude or set())


def _put_record(
    session: Session,
    record_type: type[Base],
    *,
    primary_key_name: str,
    primary_key: str,
    payload_json: dict[str, Any] | None,
    values: dict[str, Any],
    identity_description: str,
) -> bool:
    statement = (
        postgresql_insert(record_type)
        .values(**values)
        .on_conflict_do_nothing()
        .returning(getattr(record_type, primary_key_name))
    )
    created = session.execute(statement).scalar_one_or_none() is not None
    record = session.get(record_type, primary_key)
    if record is None:
        raise ImmutableKnowledgeConflict(
            f"{identity_description} is already bound to another content identity"
        )
    if payload_json is not None and getattr(record, "payload_json") != payload_json:
        raise ImmutableKnowledgeConflict(
            f"{identity_description} payload differs from its content hash"
        )
    return created


def _put_policy(session: Session, policy: ContentAccessPolicy) -> bool:
    payload = _payload(policy)
    return _put_record(
        session,
        KnowledgeAccessPolicyRecord,
        primary_key_name="policy_sha256",
        primary_key=policy.policy_sha256,
        payload_json=payload,
        values={
            "policy_sha256": policy.policy_sha256,
            "policy_id": policy.policy_id,
            "frozen_at": policy.frozen_at,
            "payload_json": payload,
        },
        identity_description=f"content access policy {policy.policy_id!r}",
    )


def _put_source(session: Session, source: CorpusSourceVersion) -> bool:
    payload = _payload(source)
    return _put_record(
        session,
        KnowledgeCorpusSourceRecord,
        primary_key_name="manifest_sha256",
        primary_key=source.manifest_sha256,
        payload_json=payload,
        values={
            "manifest_sha256": source.manifest_sha256,
            "source_id": source.source_id,
            "snapshot_id": source.snapshot_id,
            "snapshot_sha256": source.snapshot_sha256,
            "updated_through": source.updated_through,
            "retrieved_at": source.retrieved_at,
            "license_id": source.license_id,
            "payload_json": payload,
        },
        identity_description=f"source snapshot {source.source_id!r}/{source.snapshot_id!r}",
    )


def _put_paper(session: Session, paper: PaperSnapshot) -> bool:
    payload = _payload(paper)
    created = _put_record(
        session,
        KnowledgePaperSnapshotRecord,
        primary_key_name="paper_sha256",
        primary_key=paper.snapshot_sha256,
        payload_json=payload,
        values={
            "paper_sha256": paper.snapshot_sha256,
            "canonical_id": paper.canonical_id,
            "version_id": paper.version_id,
            "version_public_at": paper.version_public_at,
            "observed_at": paper.observed_at,
            "text_availability": paper.text_availability.value,
            "text_content_sha256": paper.text_content_sha256,
            "license_id": paper.license_id,
            "status": paper.status.value,
            "payload_json": payload,
        },
        identity_description=(
            f"paper snapshot {paper.canonical_id!r}/{paper.version_id!r}/"
            f"{paper.text_availability.value!r}"
        ),
    )
    if paper.text_content_sha256 is None:
        return created

    identity = {
        "canonical_id": paper.canonical_id,
        "version_id": paper.version_id,
        "text_availability": paper.text_availability.value,
        "text_content_sha256": paper.text_content_sha256,
        "first_paper_sha256": paper.snapshot_sha256,
    }
    session.execute(
        postgresql_insert(KnowledgePaperTextIdentityRecord)
        .values(**identity)
        .on_conflict_do_nothing()
    )
    key = (paper.canonical_id, paper.version_id, paper.text_availability.value)
    text_identity = session.get(KnowledgePaperTextIdentityRecord, key)
    if text_identity is None or text_identity.text_content_sha256 != paper.text_content_sha256:
        raise ImmutableKnowledgeConflict(
            "paper text changed without a new publication version: "
            f"{paper.canonical_id!r}/{paper.version_id!r}/"
            f"{paper.text_availability.value!r}"
        )
    return created


def _put_span(session: Session, span: SourceSpan) -> bool:
    payload = _payload(span)
    return _put_record(
        session,
        KnowledgeSourceSpanRecord,
        primary_key_name="span_sha256",
        primary_key=span.span_sha256,
        payload_json=payload,
        values={
            "span_sha256": span.span_sha256,
            "span_id": span.span_id,
            "paper_sha256": span.paper_snapshot_sha256,
            "text_scope": span.text_scope.value,
            "exact_text_sha256": span.exact_text_sha256,
            "normalized_text_sha256": span.normalized_text_sha256,
            "extracted_at": span.extracted_at,
            "payload_json": payload,
        },
        identity_description=f"source span {span.span_id!r}",
    )


def _put_update(session: Session, update: PublicationUpdate) -> bool:
    payload = _payload(update)
    return _put_record(
        session,
        KnowledgePublicationUpdateRecord,
        primary_key_name="update_sha256",
        primary_key=update.update_sha256,
        payload_json=payload,
        values={
            "update_sha256": update.update_sha256,
            "update_id": update.update_id,
            "update_type": update.update_type.value,
            "target_canonical_id": update.target_canonical_id,
            "notice_paper_sha256": update.notice_paper_snapshot_sha256,
            "observed_at": update.observed_at,
            "payload_json": payload,
        },
        identity_description=f"publication update {update.update_id!r}",
    )


def _put_grant(session: Session, grant: ContentAccessGrant) -> bool:
    payload = _payload(grant)
    return _put_record(
        session,
        KnowledgeContentAccessGrantRecord,
        primary_key_name="grant_sha256",
        primary_key=grant.grant_sha256,
        payload_json=payload,
        values={
            "grant_sha256": grant.grant_sha256,
            "grant_id": grant.grant_id,
            "policy_sha256": grant.policy_sha256,
            "source_manifest_sha256": grant.source_manifest_sha256,
            "paper_sha256": grant.paper_snapshot_sha256,
            "access_class": grant.access_class.value,
            "text_capability": grant.text_capability.value,
            "expires_at": grant.expires_at,
            "payload_json": payload,
        },
        identity_description=f"content access grant {grant.grant_id!r}",
    )


def _put_receipt(session: Session, receipt: ProviderIngestReceipt) -> bool:
    payload = _payload(receipt)
    return _put_record(
        session,
        KnowledgeProviderReceiptRecord,
        primary_key_name="receipt_sha256",
        primary_key=receipt.receipt_sha256,
        payload_json=payload,
        values={
            "receipt_sha256": receipt.receipt_sha256,
            "receipt_id": receipt.receipt_id,
            "source_manifest_sha256": receipt.source_manifest_sha256,
            "paper_sha256": receipt.paper_snapshot_sha256,
            "grant_sha256": receipt.access_grant_sha256,
            "fetched_at": receipt.fetched_at,
            "payload_json": payload,
        },
        identity_description=f"provider receipt {receipt.receipt_id!r}",
    )


def _put_corpus(session: Session, corpus: CorpusSnapshot) -> bool:
    payload = _payload(corpus, exclude={"sources", "papers", "spans", "updates"})
    return _put_record(
        session,
        KnowledgeCorpusSnapshotRecord,
        primary_key_name="corpus_sha256",
        primary_key=corpus.snapshot_sha256,
        payload_json=payload,
        values={
            "corpus_sha256": corpus.snapshot_sha256,
            "snapshot_id": corpus.snapshot_id,
            "version": corpus.version,
            "cutoff_time": corpus.cutoff_time,
            "temporal_mode": corpus.temporal_mode.value,
            "license_policy_sha256": corpus.license_policy_sha256,
            "parent_snapshot_sha256": corpus.parent_snapshot_sha256,
            "frozen_at": corpus.frozen_at,
            "payload_json": payload,
        },
        identity_description=f"corpus snapshot {corpus.snapshot_id!r}/{corpus.version!r}",
    )


def _put_bundle(session: Session, bundle: CorpusIngestionBundle) -> bool:
    payload = _payload(
        bundle,
        exclude={"access_policy", "corpus", "access_grants", "provider_receipts"},
    )
    return _put_record(
        session,
        KnowledgeIngestionBundleRecord,
        primary_key_name="bundle_sha256",
        primary_key=bundle.bundle_sha256,
        payload_json=payload,
        values={
            "bundle_sha256": bundle.bundle_sha256,
            "bundle_id": bundle.bundle_id,
            "corpus_sha256": bundle.corpus.snapshot_sha256,
            "policy_sha256": bundle.access_policy.policy_sha256,
            "frozen_at": bundle.frozen_at,
            "payload_json": payload,
        },
        identity_description=f"corpus ingestion bundle {bundle.bundle_id!r}",
    )


def _put_member(
    session: Session,
    member_type: type[Base],
    *,
    parent_key_name: str,
    parent_sha256: str,
    member_sha256: str,
    ordinal: int,
) -> None:
    values = {
        parent_key_name: parent_sha256,
        "member_sha256": member_sha256,
        "ordinal": ordinal,
    }
    session.execute(postgresql_insert(member_type).values(**values).on_conflict_do_nothing())
    record = session.get(member_type, (parent_sha256, member_sha256))
    if record is None or record.ordinal != ordinal:
        raise ImmutableKnowledgeConflict(
            f"immutable membership/order conflict for {member_type.__tablename__} "
            f"at ordinal {ordinal}"
        )


def persist_ingestion_bundle(session: Session, bundle: CorpusIngestionBundle) -> PersistenceResult:
    """Persist a validated bundle atomically in the caller's transaction.

    Inserts use PostgreSQL conflict-safe identity reconciliation. A byte-identical retry is a
    no-op; a stable source/update/grant/receipt/corpus/bundle identity with different content fails
    and must be rolled back by the caller.
    """

    _put_policy(session, bundle.access_policy)
    for source in bundle.corpus.sources:
        _put_source(session, source)
    for paper in bundle.corpus.papers:
        _put_paper(session, paper)
    for span in bundle.corpus.spans:
        _put_span(session, span)
    for update in bundle.corpus.updates:
        _put_update(session, update)
    _put_corpus(session, bundle.corpus)

    for ordinal, source in enumerate(bundle.corpus.sources):
        _put_member(
            session,
            KnowledgeCorpusSourceMember,
            parent_key_name="corpus_sha256",
            parent_sha256=bundle.corpus.snapshot_sha256,
            member_sha256=source.manifest_sha256,
            ordinal=ordinal,
        )
    for ordinal, paper in enumerate(bundle.corpus.papers):
        _put_member(
            session,
            KnowledgeCorpusPaperMember,
            parent_key_name="corpus_sha256",
            parent_sha256=bundle.corpus.snapshot_sha256,
            member_sha256=paper.snapshot_sha256,
            ordinal=ordinal,
        )
    for ordinal, span in enumerate(bundle.corpus.spans):
        _put_member(
            session,
            KnowledgeCorpusSpanMember,
            parent_key_name="corpus_sha256",
            parent_sha256=bundle.corpus.snapshot_sha256,
            member_sha256=span.span_sha256,
            ordinal=ordinal,
        )
    for ordinal, update in enumerate(bundle.corpus.updates):
        _put_member(
            session,
            KnowledgeCorpusUpdateMember,
            parent_key_name="corpus_sha256",
            parent_sha256=bundle.corpus.snapshot_sha256,
            member_sha256=update.update_sha256,
            ordinal=ordinal,
        )

    for grant in bundle.access_grants:
        _put_grant(session, grant)
    for receipt in bundle.provider_receipts:
        _put_receipt(session, receipt)
    created = _put_bundle(session, bundle)
    for ordinal, grant in enumerate(bundle.access_grants):
        _put_member(
            session,
            KnowledgeBundleGrantMember,
            parent_key_name="bundle_sha256",
            parent_sha256=bundle.bundle_sha256,
            member_sha256=grant.grant_sha256,
            ordinal=ordinal,
        )
    for ordinal, receipt in enumerate(bundle.provider_receipts):
        _put_member(
            session,
            KnowledgeBundleReceiptMember,
            parent_key_name="bundle_sha256",
            parent_sha256=bundle.bundle_sha256,
            member_sha256=receipt.receipt_sha256,
            ordinal=ordinal,
        )
    return PersistenceResult(
        bundle_sha256=bundle.bundle_sha256,
        corpus_sha256=bundle.corpus.snapshot_sha256,
        created=created,
        source_count=len(bundle.corpus.sources),
        paper_count=len(bundle.corpus.papers),
        span_count=len(bundle.corpus.spans),
        update_count=len(bundle.corpus.updates),
    )


def _validate_record_payload(
    record: Any,
    model_type: type[_ModelT],
    *,
    identity_property: str,
    expected_sha256: str,
    description: str,
) -> _ModelT:
    try:
        model = model_type.model_validate(record.payload_json)
    except ValidationError as exc:
        raise KnowledgePersistenceError(
            f"persisted {description} payload no longer validates"
        ) from exc
    if getattr(model, identity_property) != expected_sha256:
        raise KnowledgePersistenceError(
            f"persisted {description} content hash does not match its primary key"
        )
    return model


def _require_record(session: Session, record_type: type[Base], key: Any, description: str) -> Any:
    record = session.get(record_type, key)
    if record is None:
        raise KnowledgePersistenceError(f"persisted {description} membership is dangling")
    return record


def _ordered_members(
    session: Session,
    member_type: type[Base],
    *,
    parent_key_name: str,
    parent_sha256: str,
) -> list[str]:
    rows = list(
        session.scalars(
            select(member_type)
            .where(getattr(member_type, parent_key_name) == parent_sha256)
            .order_by(member_type.ordinal)
        )
    )
    if [row.ordinal for row in rows] != list(range(len(rows))):
        raise KnowledgePersistenceError(
            f"persisted {member_type.__tablename__} ordinals are not contiguous from zero"
        )
    return [row.member_sha256 for row in rows]


def _load_policy(session: Session, policy_sha256: str) -> ContentAccessPolicy:
    record = _require_record(
        session,
        KnowledgeAccessPolicyRecord,
        policy_sha256,
        "content-access policy",
    )
    policy = _validate_record_payload(
        record,
        ContentAccessPolicy,
        identity_property="policy_sha256",
        expected_sha256=policy_sha256,
        description="content-access policy",
    )
    if record.policy_id != policy.policy_id or record.frozen_at != policy.frozen_at:
        raise KnowledgePersistenceError("content-access policy index columns differ from payload")
    return policy


def _load_source(session: Session, manifest_sha256: str) -> CorpusSourceVersion:
    record = _require_record(session, KnowledgeCorpusSourceRecord, manifest_sha256, "corpus source")
    source = _validate_record_payload(
        record,
        CorpusSourceVersion,
        identity_property="manifest_sha256",
        expected_sha256=manifest_sha256,
        description="corpus source",
    )
    if (
        record.source_id != source.source_id
        or record.snapshot_id != source.snapshot_id
        or record.snapshot_sha256 != source.snapshot_sha256
    ):
        raise KnowledgePersistenceError("corpus-source index columns differ from payload")
    return source


def _load_paper(session: Session, paper_sha256: str) -> PaperSnapshot:
    record = _require_record(session, KnowledgePaperSnapshotRecord, paper_sha256, "paper snapshot")
    paper = _validate_record_payload(
        record,
        PaperSnapshot,
        identity_property="snapshot_sha256",
        expected_sha256=paper_sha256,
        description="paper snapshot",
    )
    if (
        record.canonical_id != paper.canonical_id
        or record.version_id != paper.version_id
        or record.text_availability != paper.text_availability.value
        or record.text_content_sha256 != paper.text_content_sha256
    ):
        raise KnowledgePersistenceError("paper-snapshot index columns differ from payload")
    return paper


def _load_span(session: Session, span_sha256: str) -> SourceSpan:
    record = _require_record(session, KnowledgeSourceSpanRecord, span_sha256, "source span")
    span = _validate_record_payload(
        record,
        SourceSpan,
        identity_property="span_sha256",
        expected_sha256=span_sha256,
        description="source span",
    )
    if (
        record.span_id != span.span_id
        or record.paper_sha256 != span.paper_snapshot_sha256
        or record.exact_text_sha256 != span.exact_text_sha256
    ):
        raise KnowledgePersistenceError("source-span index columns differ from payload")
    return span


def _load_update(session: Session, update_sha256: str) -> PublicationUpdate:
    record = _require_record(
        session, KnowledgePublicationUpdateRecord, update_sha256, "publication update"
    )
    update = _validate_record_payload(
        record,
        PublicationUpdate,
        identity_property="update_sha256",
        expected_sha256=update_sha256,
        description="publication update",
    )
    if (
        record.update_id != update.update_id
        or record.notice_paper_sha256 != update.notice_paper_snapshot_sha256
    ):
        raise KnowledgePersistenceError("publication-update index columns differ from payload")
    return update


def load_corpus_snapshot(session: Session, corpus_sha256: str) -> CorpusSnapshot:
    record = session.get(KnowledgeCorpusSnapshotRecord, corpus_sha256)
    if record is None:
        raise KnowledgeObjectNotFound(f"knowledge corpus not found: {corpus_sha256}")
    source_ids = _ordered_members(
        session,
        KnowledgeCorpusSourceMember,
        parent_key_name="corpus_sha256",
        parent_sha256=corpus_sha256,
    )
    paper_ids = _ordered_members(
        session,
        KnowledgeCorpusPaperMember,
        parent_key_name="corpus_sha256",
        parent_sha256=corpus_sha256,
    )
    span_ids = _ordered_members(
        session,
        KnowledgeCorpusSpanMember,
        parent_key_name="corpus_sha256",
        parent_sha256=corpus_sha256,
    )
    update_ids = _ordered_members(
        session,
        KnowledgeCorpusUpdateMember,
        parent_key_name="corpus_sha256",
        parent_sha256=corpus_sha256,
    )
    payload = dict(record.payload_json)
    payload.update(
        {
            "sources": [_load_source(session, identity) for identity in source_ids],
            "papers": [_load_paper(session, identity) for identity in paper_ids],
            "spans": [_load_span(session, identity) for identity in span_ids],
            "updates": [_load_update(session, identity) for identity in update_ids],
        }
    )
    try:
        corpus = CorpusSnapshot.model_validate(payload)
    except ValidationError as exc:
        raise KnowledgePersistenceError("persisted corpus closure no longer validates") from exc
    if corpus.snapshot_sha256 != corpus_sha256:
        raise KnowledgePersistenceError("persisted corpus hash does not match its primary key")
    if (
        record.snapshot_id != corpus.snapshot_id
        or record.version != corpus.version
        or record.license_policy_sha256 != corpus.license_policy_sha256
    ):
        raise KnowledgePersistenceError("corpus index columns differ from reconstructed payload")
    return corpus


def _load_grant(session: Session, grant_sha256: str) -> ContentAccessGrant:
    record = _require_record(
        session, KnowledgeContentAccessGrantRecord, grant_sha256, "content access grant"
    )
    grant = _validate_record_payload(
        record,
        ContentAccessGrant,
        identity_property="grant_sha256",
        expected_sha256=grant_sha256,
        description="content access grant",
    )
    if (
        record.grant_id != grant.grant_id
        or record.policy_sha256 != grant.policy_sha256
        or record.paper_sha256 != grant.paper_snapshot_sha256
    ):
        raise KnowledgePersistenceError("content-grant index columns differ from payload")
    return grant


def _load_receipt(session: Session, receipt_sha256: str) -> ProviderIngestReceipt:
    record = _require_record(
        session, KnowledgeProviderReceiptRecord, receipt_sha256, "provider receipt"
    )
    receipt = _validate_record_payload(
        record,
        ProviderIngestReceipt,
        identity_property="receipt_sha256",
        expected_sha256=receipt_sha256,
        description="provider receipt",
    )
    if (
        record.receipt_id != receipt.receipt_id
        or record.paper_sha256 != receipt.paper_snapshot_sha256
        or record.grant_sha256 != receipt.access_grant_sha256
    ):
        raise KnowledgePersistenceError("provider-receipt index columns differ from payload")
    return receipt


def load_ingestion_bundle(session: Session, bundle_sha256: str) -> CorpusIngestionBundle:
    record = session.get(KnowledgeIngestionBundleRecord, bundle_sha256)
    if record is None:
        raise KnowledgeObjectNotFound(f"knowledge ingestion bundle not found: {bundle_sha256}")
    grant_ids = _ordered_members(
        session,
        KnowledgeBundleGrantMember,
        parent_key_name="bundle_sha256",
        parent_sha256=bundle_sha256,
    )
    receipt_ids = _ordered_members(
        session,
        KnowledgeBundleReceiptMember,
        parent_key_name="bundle_sha256",
        parent_sha256=bundle_sha256,
    )
    payload = dict(record.payload_json)
    payload.update(
        {
            "access_policy": _load_policy(session, record.policy_sha256),
            "corpus": load_corpus_snapshot(session, record.corpus_sha256),
            "access_grants": [_load_grant(session, identity) for identity in grant_ids],
            "provider_receipts": [_load_receipt(session, identity) for identity in receipt_ids],
        }
    )
    try:
        bundle = CorpusIngestionBundle.model_validate(payload)
    except ValidationError as exc:
        raise KnowledgePersistenceError(
            "persisted corpus-ingestion closure no longer validates"
        ) from exc
    if bundle.bundle_sha256 != bundle_sha256:
        raise KnowledgePersistenceError(
            "persisted ingestion-bundle hash does not match its primary key"
        )
    if (
        record.bundle_id != bundle.bundle_id
        or record.corpus_sha256 != bundle.corpus.snapshot_sha256
        or record.policy_sha256 != bundle.access_policy.policy_sha256
    ):
        raise KnowledgePersistenceError(
            "ingestion-bundle index columns differ from reconstructed payload"
        )
    return bundle


def store_ingestion_bundle(bundle: CorpusIngestionBundle) -> PersistenceResult:
    """Transactional application wrapper around :func:`persist_ingestion_bundle`."""

    with session_scope() as session:
        return persist_ingestion_bundle(session, bundle)


def get_ingestion_bundle(bundle_sha256: str) -> CorpusIngestionBundle:
    """Load and fully revalidate a persisted bundle in one read transaction."""

    with session_scope() as session:
        return load_ingestion_bundle(session, bundle_sha256)
