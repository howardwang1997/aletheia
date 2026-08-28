"""Bind real-time endurance timestamps to the exact PostgreSQL transaction clock.

Revision ID: 20260828_0029
Revises: 20260828_0028
Create Date: 2026-08-28

The original guards compared a database timestamp captured near the beginning of a transaction
with ``clock_timestamp()`` when the deferred work reached the row trigger.  Legitimate observation
and report construction can take longer than that fixed tolerance.  PostgreSQL's transaction
timestamp is stable for the whole transaction, so exact equality is both stricter and independent
of transaction duration.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260828_0029"
down_revision: str | None = "20260828_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUARDS = (
    ("aletheia_validate_research_endurance_gate", "started_at"),
    ("aletheia_validate_research_endurance_checkpoint", "observed_at"),
    ("aletheia_validate_research_endurance_report", "completed_at"),
)


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _replace_guard(function_name: str, source_guard: str, target_guard: str) -> None:
    function_identity = f"public.{function_name}()"
    op.execute(
        f"""
        DO $migration$
        DECLARE
          definition text;
          source_guard text := {_sql_literal(source_guard)};
          target_guard text := {_sql_literal(target_guard)};
          source_count integer;
          target_count integer;
        BEGIN
          SELECT pg_get_functiondef(to_regprocedure({_sql_literal(function_identity)}))
          INTO definition;
          IF definition IS NULL THEN
            RAISE EXCEPTION 'required endurance guard function is absent: {function_name}'
              USING ERRCODE = '55000';
          END IF;
          source_count :=
            (length(definition) - length(replace(definition, source_guard, '')))
            / length(source_guard);
          target_count :=
            (length(definition) - length(replace(definition, target_guard, '')))
            / length(target_guard);
          IF source_count = 1 AND target_count = 0 THEN
            EXECUTE replace(definition, source_guard, target_guard);
          ELSIF source_count = 0 AND target_count = 1 THEN
            NULL;
          ELSE
            RAISE EXCEPTION 'unexpected endurance guard definition: {function_name}'
              USING ERRCODE = '55000';
          END IF;
        END;
        $migration$;
        """
    )


def _old_guard(timestamp_field: str) -> str:
    return f"abs(extract(epoch FROM (clock_timestamp() - NEW.{timestamp_field}))) > 5"


def _transaction_guard(timestamp_field: str) -> str:
    return f"NEW.{timestamp_field} IS DISTINCT FROM transaction_timestamp()"


def upgrade() -> None:
    for function_name, timestamp_field in _GUARDS:
        _replace_guard(
            function_name,
            _old_guard(timestamp_field),
            _transaction_guard(timestamp_field),
        )


def downgrade() -> None:
    for function_name, timestamp_field in reversed(_GUARDS):
        _replace_guard(
            function_name,
            _transaction_guard(timestamp_field),
            _old_guard(timestamp_field),
        )
