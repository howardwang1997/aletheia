"""Permit one authorized action to preregister an exhaustive replicate campaign.

Revision ID: 20260828_0028
Revises: 20260828_0027
Create Date: 2026-08-28

PR-5 originally made ``source_event_sha256`` unique on scientific execution
authorizations.  That accidentally limited every authorized scientific action to one replicate,
while the protocol and ARL-1 contracts require two or more exact preregistered slots.  Slot,
execution, attempt, bundle, grant, and authorization identities remain individually unique; the
source event is instead indexed as the shared campaign authority.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260828_0028"
down_revision: str | None = "20260828_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_rsea_source_event",
        "research_scientific_execution_authorizations",
        type_="unique",
    )
    op.create_index(
        "ix_rsea_quest_source_event",
        "research_scientific_execution_authorizations",
        ["quest_id", "source_event_sequence", "source_event_sha256"],
        unique=False,
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM research_scientific_execution_authorizations
            GROUP BY source_event_sha256
            HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION
              'cannot downgrade 0028 while one action source authorizes multiple SEA slots'
              USING ERRCODE = '55000';
          END IF;
        END;
        $$
        """
    )
    op.drop_index(
        "ix_rsea_quest_source_event",
        table_name="research_scientific_execution_authorizations",
    )
    op.create_unique_constraint(
        "uq_rsea_source_event",
        "research_scientific_execution_authorizations",
        ["source_event_sha256"],
    )
