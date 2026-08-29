"""Run the runtime-v2 deferred validator under its frozen execution owner.

Revision ID: 20260829_0030
Revises: 20260828_0029
Create Date: 2026-08-29

The qualification ACL intentionally revokes direct ``EXECUTE`` on every execution routine from
the allocator and outbox roles.  PostgreSQL deferred row triggers still run as the mutating role
unless their trigger function is ``SECURITY DEFINER``.  The runtime-v2 completeness trigger calls
three separately revoked, pure validation helpers, so legitimate allocator updates otherwise fail
at commit with ``permission denied``.  Pinning the trigger function to the no-login execution
owner, with an exact safe search path, preserves the zero-routine-grant role boundary while
allowing only the already-installed trigger to perform its relational validation.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260829_0030"
down_revision: str | None = "20260828_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FUNCTION_IDENTITY = "public.aletheia_execution_check_runtime_v2_attempt()"
_SAFE_SEARCH_PATH = "search_path=pg_catalog, public"


def _assert_function_state(*, security_definer: bool, safe_search_path: bool) -> None:
    expected_security = "true" if security_definer else "false"
    expected_config = (
        f"ARRAY['{_SAFE_SEARCH_PATH}']::text[]" if safe_search_path else "ARRAY[]::text[]"
    )
    op.execute(
        f"""
        DO $migration$
        DECLARE
          routine oid := to_regprocedure('{_FUNCTION_IDENTITY}');
          observed_security_definer boolean;
          observed_configuration text[];
          observed_return_type text;
        BEGIN
          IF routine IS NULL THEN
            RAISE EXCEPTION 'runtime-v2 deferred validator is absent'
              USING ERRCODE = '55000';
          END IF;
          SELECT routine_row.prosecdef,
                 COALESCE(routine_row.proconfig, ARRAY[]::text[]),
                 pg_catalog.pg_get_function_result(routine_row.oid)
            INTO observed_security_definer, observed_configuration, observed_return_type
            FROM pg_catalog.pg_proc AS routine_row
           WHERE routine_row.oid = routine;
          IF observed_return_type IS DISTINCT FROM 'trigger'
             OR observed_security_definer IS DISTINCT FROM {expected_security}
             OR observed_configuration IS DISTINCT FROM {expected_config} THEN
            RAISE EXCEPTION 'runtime-v2 deferred validator authority is unexpected'
              USING ERRCODE = '55000';
          END IF;
        END;
        $migration$;
        """
    )


def upgrade() -> None:
    _assert_function_state(security_definer=False, safe_search_path=False)
    op.execute(
        f"ALTER FUNCTION {_FUNCTION_IDENTITY} SECURITY DEFINER SET search_path = pg_catalog, public"
    )
    _assert_function_state(security_definer=True, safe_search_path=True)


def downgrade() -> None:
    _assert_function_state(security_definer=True, safe_search_path=True)
    op.execute(f"ALTER FUNCTION {_FUNCTION_IDENTITY} SECURITY INVOKER")
    op.execute(f"ALTER FUNCTION {_FUNCTION_IDENTITY} RESET search_path")
    _assert_function_state(security_definer=False, safe_search_path=False)
