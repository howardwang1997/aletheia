"""Rename the over-long external terminal-acceptance CHECK to a legal name.

Revision ID: 20260924_0037
Revises: 20260923_0036
Create Date: 2026-09-24

Contradiction #21: 0036 declared the disposition CHECK on
``execution_external_qualification_terminal_acceptances`` as
``ck_execution_external_qualification_terminal_acceptances_disposition``
(68 chars). PostgreSQL NAMEDATALEN is 63, so the DDL applied with the name
silently truncated to ``..._dispos``, while the ORM kept the full 68-char
name. Any client-side identifier formatting (Alembic autogenerate
comparison, deployment ``require_schema_exact``) raises
``sqlalchemy.exc.IdentifierError`` on the metadata name, so activation
cannot run against this schema at all.

The fix renames the stored constraint (the deterministic 63-char
truncation, verified live on the take-5 era database) to the new 55-char
name the ORM now declares. 0036 itself is immutable and untouched; both a
fresh database (0036 truncates deterministically, 0037 renames) and an
already-migrated one converge on the same legal name. Index names >63 are
unaffected: SQLAlchemy truncates those deterministically with a hash
suffix on both sides of the comparison.

No data changes; downgrade restores the exact truncated original.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from collections.abc import Sequence

revision: str = "20260924_0037"
down_revision: str | None = "20260923_0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "execution_external_qualification_terminal_acceptances"
_TRUNCATED = "ck_execution_external_qualification_terminal_acceptances_dispos"
_RENAMED = "ck_execution_external_qual_term_acceptances_disposition"


def _constraint_exists(conname: str) -> bool:
    found = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM pg_constraint "
                "WHERE conrelid = CAST(:table_name AS regclass) AND conname = :conname"
            ).bindparams(table_name=_TABLE, conname=conname)
        )
        .scalar()
    )
    return found == 1


def upgrade() -> None:
    assert _constraint_exists(_TRUNCATED), (
        f"expected {_TABLE}.{_TRUNCATED} (the NAMEDATALEN-truncated 0036 name) to exist"
    )
    op.execute(f"ALTER TABLE {_TABLE} RENAME CONSTRAINT {_TRUNCATED} TO {_RENAMED}")


def downgrade() -> None:
    assert _constraint_exists(_RENAMED), f"expected {_TABLE}.{_RENAMED} to exist"
    op.execute(f"ALTER TABLE {_TABLE} RENAME CONSTRAINT {_RENAMED} TO {_TRUNCATED}")
