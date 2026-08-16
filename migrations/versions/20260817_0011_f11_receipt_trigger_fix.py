"""Make the shared F11 immutability trigger safe across heterogeneous rows.

Revision ID: 20260817_0011
Revises: 20260817_0010
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260817_0011"
down_revision: str | None = "20260817_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION aletheia_reject_immutable_f11_row()
        RETURNS trigger AS $$
        BEGIN
          IF TG_TABLE_NAME = 'scientific_commands' THEN
            IF OLD.status <> 'committed' THEN
              IF TG_OP = 'DELETE' THEN
                RETURN OLD;
              END IF;
              RETURN NEW;
            END IF;
          END IF;
          RAISE EXCEPTION 'immutable F11 scientific receipt cannot be mutated';
        END;
        $$ LANGUAGE plpgsql
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION aletheia_reject_immutable_f11_row()
        RETURNS trigger AS $$
        BEGIN
          IF TG_TABLE_NAME = 'scientific_commands' AND OLD.status <> 'committed' THEN
            IF TG_OP = 'DELETE' THEN
              RETURN OLD;
            END IF;
            RETURN NEW;
          END IF;
          RAISE EXCEPTION 'immutable F11 scientific receipt cannot be mutated';
        END;
        $$ LANGUAGE plpgsql
        """
    )
