"""Freeze mutable legacy DataAsset scope into each research allocation.

Revision ID: 20260817_0014
Revises: 20260817_0013
Create Date: 2026-08-17
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260817_0014"
down_revision: str | None = "20260817_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _scope(row: sa.RowMapping) -> dict[str, object]:
    projection = {
        "schema": "aletheia.allocated_data_asset_scope.v1",
        "data_asset_id": row["data_asset_id"],
        "run_id": row["run_id"],
        "source_role": row["source_role"],
        "source": row["source"],
        "ref": row["ref"],
        "content_sha256": row["content_sha256"],
    }
    return {key: value for key, value in projection.items() if value is not None}


def _sha256(value: dict[str, object]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def upgrade() -> None:
    op.add_column(
        "research_data_role_allocations",
        sa.Column("data_asset_run_id", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "research_data_role_allocations",
        sa.Column("source_role", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "research_data_role_allocations",
        sa.Column("data_asset_scope_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "research_data_role_allocations",
        sa.Column(
            "data_asset_scope_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )

    connection = op.get_bind()
    # 0013 makes allocation rows append-only. Temporarily suspend only that trigger while the
    # migration freezes each already-allocated legacy asset's authority-relevant identity.
    op.execute(
        "ALTER TABLE research_data_role_allocations "
        "DISABLE TRIGGER trg_research_data_role_allocations_immutable"
    )
    rows = connection.execute(
        sa.text(
            """
            SELECT allocation.allocation_id,
                   asset.id AS data_asset_id,
                   asset.run_id,
                   asset.role AS source_role,
                   asset.source,
                   asset.ref,
                   asset.content_sha256
            FROM research_data_role_allocations allocation
            JOIN data_assets asset ON asset.id = allocation.data_asset_id
            ORDER BY allocation.allocation_id
            """
        )
    ).mappings()
    for row in rows:
        scope = _scope(row)
        connection.execute(
            sa.text(
                """
                UPDATE research_data_role_allocations
                SET data_asset_run_id = :run_id,
                    source_role = :source_role,
                    data_asset_scope_sha256 = :scope_sha256,
                    data_asset_scope_json = CAST(:scope_json AS jsonb)
                WHERE allocation_id = :allocation_id
                """
            ),
            {
                "allocation_id": row["allocation_id"],
                "run_id": row["run_id"],
                "source_role": row["source_role"],
                "scope_sha256": _sha256(scope),
                "scope_json": json.dumps(
                    scope,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
            },
        )
    op.execute(
        "ALTER TABLE research_data_role_allocations "
        "ENABLE TRIGGER trg_research_data_role_allocations_immutable"
    )

    for column in (
        "data_asset_run_id",
        "source_role",
        "data_asset_scope_sha256",
        "data_asset_scope_json",
    ):
        op.alter_column("research_data_role_allocations", column, nullable=False)
    op.create_foreign_key(
        "fk_research_data_allocations_asset_run",
        "research_data_role_allocations",
        "runs",
        ["data_asset_run_id"],
        ["id"],
    )
    op.create_index(
        op.f("ix_research_data_role_allocations_data_asset_run_id"),
        "research_data_role_allocations",
        ["data_asset_run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_research_data_role_allocations_data_asset_run_id"),
        table_name="research_data_role_allocations",
    )
    op.drop_constraint(
        "fk_research_data_allocations_asset_run",
        "research_data_role_allocations",
        type_="foreignkey",
    )
    op.drop_column("research_data_role_allocations", "data_asset_scope_json")
    op.drop_column("research_data_role_allocations", "data_asset_scope_sha256")
    op.drop_column("research_data_role_allocations", "source_role")
    op.drop_column("research_data_role_allocations", "data_asset_run_id")
