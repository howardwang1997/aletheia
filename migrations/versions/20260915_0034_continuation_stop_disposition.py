"""Widen the continuation-receipt disposition CHECK for the stop-required ground.

Revision ID: 20260915_0034
Revises: 20260909_0033
Create Date: 2026-09-15

ARL-2 roadmap step 5 adds the preregistered stop policy to graph-scoped F9-v2
continuation: ``ContinuationDisposition.STOP_REQUIRED`` with hash-frozen
``StopPolicyPin`` grounds.  The kernel enum already carried ``ActionKind.STOP`` and
the 13-value ``StopReason``; the only schema change is this CHECK widening so a
stop-required receipt row can persist.  No data changes: the table is
append-only custody and no historical row carries the new value.  The runtime ORM
CHECK in ``aletheia/observations/persistence.py`` widens identically in the same
change.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260915_0034"
down_revision: str | None = "20260909_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NARROW = "disposition IN ('ready','redesign_observable','hypothesis_set_fork_required')"
_WIDE = (
    "disposition IN "
    "('ready','redesign_observable','hypothesis_set_fork_required','stop_required')"
)


def upgrade() -> None:
    op.execute("ALTER TABLE research_continuation_receipts DROP CONSTRAINT ck_rcr_disposition")
    op.execute(
        "ALTER TABLE research_continuation_receipts"
        f" ADD CONSTRAINT ck_rcr_disposition CHECK ({_WIDE})"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE research_continuation_receipts DROP CONSTRAINT ck_rcr_disposition")
    op.execute(
        "ALTER TABLE research_continuation_receipts"
        f" ADD CONSTRAINT ck_rcr_disposition CHECK ({_NARROW})"
    )
