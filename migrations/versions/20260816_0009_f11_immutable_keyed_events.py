"""Reject direct SQL mutation of content-bound durable events.

Revision ID: 20260816_0009
Revises: 20260816_0008
Create Date: 2026-08-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260816_0009"
down_revision: str | None = "20260816_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION aletheia_reject_keyed_event_mutation()
        RETURNS trigger AS $$
        BEGIN
          IF OLD.event_key IS NOT NULL THEN
            RAISE EXCEPTION 'keyed durable events are immutable';
          END IF;
          IF TG_OP = 'DELETE' THEN
            RETURN OLD;
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_events_keyed_immutable
        BEFORE UPDATE OR DELETE ON events
        FOR EACH ROW EXECUTE FUNCTION aletheia_reject_keyed_event_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_events_keyed_immutable ON events")
    op.execute("DROP FUNCTION aletheia_reject_keyed_event_mutation()")
