"""Bind one pre-launch lease contraction to exact runtime launch authority.

Revision ID: 20260831_0031
Revises: 20260829_0030
Create Date: 2026-08-31

A separately deployed registration service may reserve an assignment long enough for a frozen
target campaign to render and apply its plan. Once the node proves possession of the lease token
and requests its first runtime launch authorization, that pre-launch window must contract to the
ordinary heartbeat interval. The existing runtime-v2 guards intentionally reject every lease
rollback, so this migration admits only the exact ``reserved -> starting`` transition backed by
the append-only launch-authorization row and keeps the resource lease identical to the attempt.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260831_0031"
down_revision: str | None = "20260829_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ATTEMPT_FUNCTION = "aletheia_execution_guard_attempt"
_RESOURCE_FUNCTION = "aletheia_execution_guard_lease_state"

_ATTEMPT_MONOTONIC_GUARD = """          IF NEW.updated_at < OLD.updated_at OR NEW.heartbeat_at < OLD.heartbeat_at OR
             NEW.lease_expires_at < OLD.lease_expires_at OR
             NEW.lease_expires_at > NEW.hard_deadline THEN
            RAISE EXCEPTION 'attempt clock/heartbeat/lease expiry is non-monotonic'
              USING ERRCODE = '55000';
          END IF;"""

_ATTEMPT_CONTRACTION_GUARD = """          IF NEW.updated_at < OLD.updated_at OR NEW.heartbeat_at < OLD.heartbeat_at OR
             (NEW.lease_expires_at < OLD.lease_expires_at AND NOT (
               OLD.status = 'reserved'
               AND NEW.status = 'starting'
               AND NEW.heartbeat_at = NEW.updated_at
               AND NEW.runtime_preparation_sha256 IS NOT NULL
               AND NEW.runtime_launch_authorization_count =
                   OLD.runtime_launch_authorization_count + 1
               AND NEW.latest_runtime_launch_authorization_sha256 IS NOT NULL
               AND EXISTS (
                 SELECT 1
                   FROM execution_runtime_launch_authorizations launch_row
                  WHERE launch_row.authorization_sha256 =
                        NEW.latest_runtime_launch_authorization_sha256
                    AND launch_row.attempt_id = NEW.attempt_id
                    AND launch_row.sequence = NEW.runtime_launch_authorization_count
                    AND launch_row.preparation_sha256 = NEW.runtime_preparation_sha256
                    AND (launch_row.authorization_json->>'lease_expires_at')::timestamptz =
                        NEW.lease_expires_at
                    AND (launch_row.authorization_json->>'hard_deadline')::timestamptz =
                        NEW.hard_deadline
                    AND launch_row.issued_at = NEW.updated_at
                    AND launch_row.recorded_at = NEW.updated_at
               )
             )) OR NEW.lease_expires_at > NEW.hard_deadline THEN
            RAISE EXCEPTION 'attempt clock/heartbeat/lease expiry is non-monotonic'
              USING ERRCODE = '55000';
          END IF;"""

_RESOURCE_MONOTONIC_GUARD = """            IF NEW.heartbeat_at < OLD.heartbeat_at OR
               NEW.lease_expires_at < OLD.lease_expires_at OR
               NEW.lease_expires_at > (SELECT hard_deadline FROM execution_attempts
                                        WHERE attempt_id = NEW.attempt_id) THEN
              RAISE EXCEPTION 'resource lease heartbeat/expiry is non-monotonic or past deadline'
                USING ERRCODE = '55000';
            END IF;"""

_RESOURCE_CONTRACTION_GUARD = """            IF NEW.heartbeat_at < OLD.heartbeat_at OR
               (NEW.lease_expires_at < OLD.lease_expires_at AND NOT EXISTS (
                 SELECT 1
                   FROM execution_attempts attempt_row
                   JOIN execution_runtime_launch_authorizations launch_row
                     ON launch_row.authorization_sha256 =
                        attempt_row.latest_runtime_launch_authorization_sha256
                    AND launch_row.attempt_id = attempt_row.attempt_id
                    AND launch_row.sequence = attempt_row.runtime_launch_authorization_count
                  WHERE attempt_row.attempt_id = NEW.attempt_id
                    AND attempt_row.status = 'starting'
                    AND attempt_row.heartbeat_at = NEW.heartbeat_at
                    AND attempt_row.lease_expires_at = NEW.lease_expires_at
                    AND launch_row.preparation_sha256 =
                        attempt_row.runtime_preparation_sha256
                    AND (launch_row.authorization_json->>'lease_expires_at')::timestamptz =
                        NEW.lease_expires_at
                    AND launch_row.authorization_sha256 =
                        attempt_row.latest_runtime_launch_authorization_sha256
               )) OR
               NEW.lease_expires_at > (SELECT hard_deadline FROM execution_attempts
                                        WHERE attempt_id = NEW.attempt_id) THEN
              RAISE EXCEPTION 'resource lease heartbeat/expiry is non-monotonic or past deadline'
                USING ERRCODE = '55000';
            END IF;"""


def _replace_guard(*, function_name: str, old: str, new: str) -> None:
    op.execute(
        f"""
        DO $migration$
        DECLARE
          definition text;
          rewritten text;
        BEGIN
          SELECT pg_get_functiondef(to_regprocedure('public.{function_name}()'))
            INTO definition;
          IF definition IS NULL OR position($old${old}$old$ in definition) = 0 THEN
            RAISE EXCEPTION '0031 found unexpected definition for {function_name}';
          END IF;
          rewritten := replace(definition, $old${old}$old$, $new${new}$new$);
          IF rewritten = definition OR position($old${old}$old$ in rewritten) <> 0 THEN
            RAISE EXCEPTION '0031 did not replace exactly the expected {function_name} guard';
          END IF;
          EXECUTE rewritten;
        END;
        $migration$;
        """
    )


def upgrade() -> None:
    _replace_guard(
        function_name=_ATTEMPT_FUNCTION,
        old=_ATTEMPT_MONOTONIC_GUARD,
        new=_ATTEMPT_CONTRACTION_GUARD,
    )
    _replace_guard(
        function_name=_RESOURCE_FUNCTION,
        old=_RESOURCE_MONOTONIC_GUARD,
        new=_RESOURCE_CONTRACTION_GUARD,
    )


def downgrade() -> None:
    _replace_guard(
        function_name=_RESOURCE_FUNCTION,
        old=_RESOURCE_CONTRACTION_GUARD,
        new=_RESOURCE_MONOTONIC_GUARD,
    )
    _replace_guard(
        function_name=_ATTEMPT_FUNCTION,
        old=_ATTEMPT_CONTRACTION_GUARD,
        new=_ATTEMPT_MONOTONIC_GUARD,
    )
