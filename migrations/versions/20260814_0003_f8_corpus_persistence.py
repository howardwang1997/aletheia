"""Add immutable F8 corpus/source-span persistence.

Revision ID: 20260814_0003
Revises: 20260813_0002
Create Date: 2026-08-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260814_0003"
down_revision: str | None = "20260813_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


IMMUTABLE_TABLES = (
    "knowledge_access_policies",
    "knowledge_corpus_sources",
    "knowledge_paper_snapshots",
    "knowledge_paper_text_identities",
    "knowledge_source_spans",
    "knowledge_publication_updates",
    "knowledge_content_access_grants",
    "knowledge_provider_receipts",
    "knowledge_corpus_snapshots",
    "knowledge_ingestion_bundles",
    "knowledge_corpus_source_members",
    "knowledge_corpus_paper_members",
    "knowledge_corpus_span_members",
    "knowledge_corpus_update_members",
    "knowledge_bundle_grant_members",
    "knowledge_bundle_receipt_members",
)


def upgrade() -> None:
    op.create_table(
        "knowledge_access_policies",
        sa.Column("policy_sha256", sa.String(length=64), nullable=False),
        sa.Column("policy_id", sa.String(length=192), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("policy_sha256"),
    )
    op.create_index(
        op.f("ix_knowledge_access_policies_policy_id"),
        "knowledge_access_policies",
        ["policy_id"],
        unique=False,
    )

    op.create_table(
        "knowledge_corpus_sources",
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=80), nullable=False),
        sa.Column("snapshot_id", sa.String(length=256), nullable=False),
        sa.Column("snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column("updated_through", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("license_id", sa.String(length=256), nullable=False),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("manifest_sha256"),
        sa.UniqueConstraint(
            "source_id", "snapshot_id", name="uq_knowledge_source_snapshot_identity"
        ),
    )
    op.create_index(
        op.f("ix_knowledge_corpus_sources_source_id"),
        "knowledge_corpus_sources",
        ["source_id"],
        unique=False,
    )

    op.create_table(
        "knowledge_paper_snapshots",
        sa.Column("paper_sha256", sa.String(length=64), nullable=False),
        sa.Column("canonical_id", sa.String(length=512), nullable=False),
        sa.Column("version_id", sa.String(length=256), nullable=False),
        sa.Column("version_public_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("text_availability", sa.String(length=32), nullable=False),
        sa.Column("text_content_sha256", sa.String(length=64), nullable=True),
        sa.Column("license_id", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("paper_sha256"),
    )
    op.create_index(
        op.f("ix_knowledge_paper_snapshots_canonical_id"),
        "knowledge_paper_snapshots",
        ["canonical_id"],
        unique=False,
    )
    op.create_index(
        "ix_knowledge_paper_version",
        "knowledge_paper_snapshots",
        ["canonical_id", "version_id", "text_availability"],
        unique=False,
    )

    op.create_table(
        "knowledge_paper_text_identities",
        sa.Column("canonical_id", sa.String(length=512), nullable=False),
        sa.Column("version_id", sa.String(length=256), nullable=False),
        sa.Column("text_availability", sa.String(length=32), nullable=False),
        sa.Column("text_content_sha256", sa.String(length=64), nullable=False),
        sa.Column("first_paper_sha256", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["first_paper_sha256"], ["knowledge_paper_snapshots.paper_sha256"]),
        sa.PrimaryKeyConstraint("canonical_id", "version_id", "text_availability"),
    )

    op.create_table(
        "knowledge_source_spans",
        sa.Column("span_sha256", sa.String(length=64), nullable=False),
        sa.Column("span_id", sa.String(length=256), nullable=False),
        sa.Column("paper_sha256", sa.String(length=64), nullable=False),
        sa.Column("text_scope", sa.String(length=32), nullable=False),
        sa.Column("exact_text_sha256", sa.String(length=64), nullable=False),
        sa.Column("normalized_text_sha256", sa.String(length=64), nullable=False),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["paper_sha256"], ["knowledge_paper_snapshots.paper_sha256"]),
        sa.PrimaryKeyConstraint("span_sha256"),
    )
    op.create_index(
        op.f("ix_knowledge_source_spans_paper_sha256"),
        "knowledge_source_spans",
        ["paper_sha256"],
        unique=False,
    )
    op.create_index(
        op.f("ix_knowledge_source_spans_span_id"),
        "knowledge_source_spans",
        ["span_id"],
        unique=False,
    )
    op.create_index(
        "ix_knowledge_span_paper_span_id",
        "knowledge_source_spans",
        ["paper_sha256", "span_id"],
        unique=False,
    )

    op.create_table(
        "knowledge_publication_updates",
        sa.Column("update_sha256", sa.String(length=64), nullable=False),
        sa.Column("update_id", sa.String(length=256), nullable=False),
        sa.Column("update_type", sa.String(length=32), nullable=False),
        sa.Column("target_canonical_id", sa.String(length=512), nullable=False),
        sa.Column("notice_paper_sha256", sa.String(length=64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["notice_paper_sha256"], ["knowledge_paper_snapshots.paper_sha256"]
        ),
        sa.PrimaryKeyConstraint("update_sha256"),
        sa.UniqueConstraint("update_id", name="uq_knowledge_update_id"),
    )
    op.create_index(
        op.f("ix_knowledge_publication_updates_target_canonical_id"),
        "knowledge_publication_updates",
        ["target_canonical_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_knowledge_publication_updates_update_id"),
        "knowledge_publication_updates",
        ["update_id"],
        unique=False,
    )

    op.create_table(
        "knowledge_content_access_grants",
        sa.Column("grant_sha256", sa.String(length=64), nullable=False),
        sa.Column("grant_id", sa.String(length=192), nullable=False),
        sa.Column("policy_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("paper_sha256", sa.String(length=64), nullable=False),
        sa.Column("access_class", sa.String(length=32), nullable=False),
        sa.Column("text_capability", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["paper_sha256"], ["knowledge_paper_snapshots.paper_sha256"]),
        sa.ForeignKeyConstraint(["policy_sha256"], ["knowledge_access_policies.policy_sha256"]),
        sa.ForeignKeyConstraint(
            ["source_manifest_sha256"], ["knowledge_corpus_sources.manifest_sha256"]
        ),
        sa.PrimaryKeyConstraint("grant_sha256"),
        sa.UniqueConstraint("grant_id", name="uq_knowledge_access_grant_id"),
    )
    for column in (
        "grant_id",
        "paper_sha256",
        "policy_sha256",
        "source_manifest_sha256",
    ):
        op.create_index(
            op.f(f"ix_knowledge_content_access_grants_{column}"),
            "knowledge_content_access_grants",
            [column],
            unique=False,
        )

    op.create_table(
        "knowledge_provider_receipts",
        sa.Column("receipt_sha256", sa.String(length=64), nullable=False),
        sa.Column("receipt_id", sa.String(length=192), nullable=False),
        sa.Column("source_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("paper_sha256", sa.String(length=64), nullable=False),
        sa.Column("grant_sha256", sa.String(length=64), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["grant_sha256"], ["knowledge_content_access_grants.grant_sha256"]),
        sa.ForeignKeyConstraint(["paper_sha256"], ["knowledge_paper_snapshots.paper_sha256"]),
        sa.ForeignKeyConstraint(
            ["source_manifest_sha256"], ["knowledge_corpus_sources.manifest_sha256"]
        ),
        sa.PrimaryKeyConstraint("receipt_sha256"),
        sa.UniqueConstraint("receipt_id", name="uq_knowledge_provider_receipt_id"),
    )
    for column in ("grant_sha256", "paper_sha256", "receipt_id", "source_manifest_sha256"):
        op.create_index(
            op.f(f"ix_knowledge_provider_receipts_{column}"),
            "knowledge_provider_receipts",
            [column],
            unique=False,
        )

    op.create_table(
        "knowledge_corpus_snapshots",
        sa.Column("corpus_sha256", sa.String(length=64), nullable=False),
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("cutoff_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("temporal_mode", sa.String(length=32), nullable=False),
        sa.Column("license_policy_sha256", sa.String(length=64), nullable=False),
        sa.Column("parent_snapshot_sha256", sa.String(length=64), nullable=True),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["license_policy_sha256"], ["knowledge_access_policies.policy_sha256"]
        ),
        sa.PrimaryKeyConstraint("corpus_sha256"),
        sa.UniqueConstraint("snapshot_id", "version", name="uq_knowledge_corpus_snapshot_version"),
    )
    for column in (
        "cutoff_time",
        "license_policy_sha256",
        "parent_snapshot_sha256",
        "snapshot_id",
    ):
        op.create_index(
            op.f(f"ix_knowledge_corpus_snapshots_{column}"),
            "knowledge_corpus_snapshots",
            [column],
            unique=False,
        )

    op.create_table(
        "knowledge_ingestion_bundles",
        sa.Column("bundle_sha256", sa.String(length=64), nullable=False),
        sa.Column("bundle_id", sa.String(length=192), nullable=False),
        sa.Column("corpus_sha256", sa.String(length=64), nullable=False),
        sa.Column("policy_sha256", sa.String(length=64), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["corpus_sha256"], ["knowledge_corpus_snapshots.corpus_sha256"]),
        sa.ForeignKeyConstraint(["policy_sha256"], ["knowledge_access_policies.policy_sha256"]),
        sa.PrimaryKeyConstraint("bundle_sha256"),
        sa.UniqueConstraint("bundle_id", name="uq_knowledge_ingestion_bundle_id"),
    )
    for column in ("bundle_id", "corpus_sha256", "policy_sha256"):
        op.create_index(
            op.f(f"ix_knowledge_ingestion_bundles_{column}"),
            "knowledge_ingestion_bundles",
            [column],
            unique=False,
        )

    _create_membership_tables()

    op.execute(
        """
        CREATE FUNCTION aletheia_reject_knowledge_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'immutable F8 knowledge row cannot be updated or deleted'
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    for table_name in IMMUTABLE_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_immutable
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION aletheia_reject_knowledge_mutation()
            """
        )


def _create_membership_tables() -> None:
    specifications = (
        (
            "knowledge_corpus_source_members",
            "corpus_sha256",
            "knowledge_corpus_snapshots.corpus_sha256",
            "member_sha256",
            "knowledge_corpus_sources.manifest_sha256",
            "uq_knowledge_corpus_source_order",
        ),
        (
            "knowledge_corpus_paper_members",
            "corpus_sha256",
            "knowledge_corpus_snapshots.corpus_sha256",
            "member_sha256",
            "knowledge_paper_snapshots.paper_sha256",
            "uq_knowledge_corpus_paper_order",
        ),
        (
            "knowledge_corpus_span_members",
            "corpus_sha256",
            "knowledge_corpus_snapshots.corpus_sha256",
            "member_sha256",
            "knowledge_source_spans.span_sha256",
            "uq_knowledge_corpus_span_order",
        ),
        (
            "knowledge_corpus_update_members",
            "corpus_sha256",
            "knowledge_corpus_snapshots.corpus_sha256",
            "member_sha256",
            "knowledge_publication_updates.update_sha256",
            "uq_knowledge_corpus_update_order",
        ),
        (
            "knowledge_bundle_grant_members",
            "bundle_sha256",
            "knowledge_ingestion_bundles.bundle_sha256",
            "member_sha256",
            "knowledge_content_access_grants.grant_sha256",
            "uq_knowledge_bundle_grant_order",
        ),
        (
            "knowledge_bundle_receipt_members",
            "bundle_sha256",
            "knowledge_ingestion_bundles.bundle_sha256",
            "member_sha256",
            "knowledge_provider_receipts.receipt_sha256",
            "uq_knowledge_bundle_receipt_order",
        ),
    )
    for table_name, parent_name, parent_ref, member_name, member_ref, order_name in specifications:
        op.create_table(
            table_name,
            sa.Column(parent_name, sa.String(length=64), nullable=False),
            sa.Column(member_name, sa.String(length=64), nullable=False),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint([member_name], [member_ref]),
            sa.ForeignKeyConstraint([parent_name], [parent_ref]),
            sa.PrimaryKeyConstraint(parent_name, member_name),
            sa.UniqueConstraint(parent_name, "ordinal", name=order_name),
        )


def downgrade() -> None:
    for table_name in reversed(IMMUTABLE_TABLES):
        op.drop_table(table_name)
    op.execute("DROP FUNCTION aletheia_reject_knowledge_mutation()")
