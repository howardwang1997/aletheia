"""Make one-time action identity and state progression database-enforced.

Revision ID: 20260817_0012
Revises: 20260817_0011
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260817_0012"
down_revision: str | None = "20260817_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_external_action_receipts_action_receipt",
        "external_action_receipts",
        ["action_id", "receipt_sha256"],
    )
    op.create_foreign_key(
        "fk_campaign_split_ledgers_final_action_receipt",
        "campaign_split_ledgers",
        "external_action_receipts",
        ["final_action_id", "final_action_receipt_sha256"],
        ["action_id", "receipt_sha256"],
    )
    op.create_foreign_key(
        "fk_external_validation_ledgers_action_receipt",
        "external_validation_ledgers",
        "external_action_receipts",
        ["action_id", "action_receipt_sha256"],
        ["action_id", "receipt_sha256"],
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_enforce_one_time_external_action()
        RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'one-time external action intent cannot be deleted';
          END IF;

          IF OLD.action_id IS DISTINCT FROM NEW.action_id
             OR OLD.run_id IS DISTINCT FROM NEW.run_id
             OR OLD.action_type IS DISTINCT FROM NEW.action_type
             OR OLD.scope_key IS DISTINCT FROM NEW.scope_key
             OR OLD.request_sha256 IS DISTINCT FROM NEW.request_sha256
             OR OLD.request_json IS DISTINCT FROM NEW.request_json
             OR OLD.principal IS DISTINCT FROM NEW.principal
             OR OLD.provider_idempotency_key IS DISTINCT FROM NEW.provider_idempotency_key
             OR OLD.claim_ttl_seconds IS DISTINCT FROM NEW.claim_ttl_seconds
             OR OLD.claim_owner IS DISTINCT FROM NEW.claim_owner
             OR OLD.execution_token_sha256 IS DISTINCT FROM NEW.execution_token_sha256
             OR OLD.claimed_at IS DISTINCT FROM NEW.claimed_at
             OR OLD.reconcile_after IS DISTINCT FROM NEW.reconcile_after
             OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
            RAISE EXCEPTION 'one-time external action identity cannot be mutated';
          END IF;

          -- The claim row is inserted before its keyed event ID is known. This one same-transaction
          -- bootstrap update is the only non-state transition the row permits.
          IF OLD.status = 'claimed'
             AND NEW.status = 'claimed'
             AND OLD.state_version = 1
             AND NEW.state_version = 1
             AND OLD.last_event_id IS NULL
             AND NEW.last_event_id IS NOT NULL
             AND OLD.receipt_sha256 IS NULL
             AND NEW.receipt_sha256 IS NULL
             AND OLD.completed_at IS NULL
             AND NEW.completed_at IS NULL
             AND OLD.updated_at IS NOT DISTINCT FROM NEW.updated_at THEN
            RETURN NEW;
          END IF;

          IF NEW.state_version <> OLD.state_version + 1
             OR NEW.last_event_id IS NULL
             OR NEW.last_event_id IS NOT DISTINCT FROM OLD.last_event_id
             OR NEW.updated_at < OLD.updated_at THEN
            RAISE EXCEPTION 'one-time external action state progression is invalid';
          END IF;

          IF OLD.status = 'claimed' AND NEW.status = 'reconciliation_required' THEN
            IF NEW.receipt_sha256 IS NOT NULL OR NEW.completed_at IS NOT NULL THEN
              RAISE EXCEPTION 'reconciliation state cannot contain a completion receipt';
            END IF;
            RETURN NEW;
          END IF;

          IF OLD.status IN ('claimed', 'reconciliation_required')
             AND NEW.status = 'completed' THEN
            IF NEW.receipt_sha256 IS NULL
               OR NEW.completed_at IS NULL
               OR NEW.completed_at IS DISTINCT FROM NEW.updated_at THEN
              RAISE EXCEPTION 'completed external action requires its exact receipt timestamp';
            END IF;
            RETURN NEW;
          END IF;

          RAISE EXCEPTION 'one-time external action state transition is invalid';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_one_time_external_actions_state
        BEFORE UPDATE OR DELETE ON one_time_external_actions
        FOR EACH ROW EXECUTE FUNCTION aletheia_enforce_one_time_external_action()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_one_time_external_actions_state ON one_time_external_actions"
    )
    op.execute("DROP FUNCTION aletheia_enforce_one_time_external_action()")
    op.drop_constraint(
        "fk_external_validation_ledgers_action_receipt",
        "external_validation_ledgers",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_campaign_split_ledgers_final_action_receipt",
        "campaign_split_ledgers",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_external_action_receipts_action_receipt",
        "external_action_receipts",
        type_="unique",
    )
