"""Enforce family and allocated-budget bindings on legacy ledger writes.

Revision ID: 20260817_0015
Revises: 20260817_0014
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260817_0015"
down_revision: str | None = "20260817_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION aletheia_enforce_hypothesis_family_binding()
        RETURNS trigger AS $$
        DECLARE
          expected_family_id text;
          expected_family_key text;
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'hypothesis attempt ledger row cannot be deleted';
          END IF;
          IF TG_OP = 'UPDATE' THEN
            IF OLD.run_id IS DISTINCT FROM NEW.run_id
               OR OLD.experiment_id IS DISTINCT FROM NEW.experiment_id
               OR OLD.research_family_id IS DISTINCT FROM NEW.research_family_id
               OR OLD.family_key IS DISTINCT FROM NEW.family_key
               OR OLD.hypothesis_key IS DISTINCT FROM NEW.hypothesis_key
               OR OLD.hypothesis_text IS DISTINCT FROM NEW.hypothesis_text
               OR OLD.round_index IS DISTINCT FROM NEW.round_index
               OR OLD.phase IS DISTINCT FROM NEW.phase
               OR OLD.confirmation_batch IS DISTINCT FROM NEW.confirmation_batch
               OR OLD.split_hash IS DISTINCT FROM NEW.split_hash
               OR OLD.alpha_allocated IS DISTINCT FROM NEW.alpha_allocated
               OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
              RAISE EXCEPTION 'hypothesis attempt scientific/family identity is immutable';
            END IF;
            RETURN NEW;
          END IF;

          SELECT family.family_id, family.family_key
            INTO expected_family_id, expected_family_key
          FROM research_campaign_runs campaign_run
          JOIN research_campaign_families campaign_family
            ON campaign_family.campaign_node_id = campaign_run.campaign_node_id
          JOIN research_scientific_families family
            ON family.family_id = campaign_family.family_id
          WHERE campaign_run.run_id = NEW.run_id;

          IF FOUND THEN
            IF NEW.research_family_id IS DISTINCT FROM expected_family_id
               OR NEW.family_key IS DISTINCT FROM expected_family_key THEN
              RAISE EXCEPTION 'hypothesis attempt must inherit its campaign scientific family';
            END IF;
          ELSIF NEW.research_family_id IS NOT NULL THEN
            SELECT family_key INTO expected_family_key
            FROM research_scientific_families
            WHERE family_id = NEW.research_family_id;
            IF NOT FOUND OR NEW.family_key IS DISTINCT FROM expected_family_key THEN
              RAISE EXCEPTION 'hypothesis attempt scientific family is invalid';
            END IF;
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_hypothesis_attempt_family_binding
        BEFORE INSERT OR UPDATE OR DELETE ON hypothesis_attempts
        FOR EACH ROW EXECUTE FUNCTION aletheia_enforce_hypothesis_family_binding()
        """
    )
    op.execute(
        """
        CREATE FUNCTION aletheia_enforce_allocated_budget_event()
        RETURNS trigger AS $$
        DECLARE
          allocation_quest text;
          allocation_scope text;
          allocation_kind text;
          allocation_cap bigint;
          scope_type text;
          run_quest text;
          run_campaign text;
          campaign_program text;
          spent_microunits bigint;
          charge_microunits bigint;
        BEGIN
          IF TG_OP IN ('UPDATE', 'DELETE') THEN
            RAISE EXCEPTION 'budget event ledger rows are append-only';
          END IF;
          IF NEW.research_budget_allocation_id IS NULL THEN
            RETURN NEW;
          END IF;
          IF NEW.amount < 0 THEN
            RAISE EXCEPTION 'allocated budget charge cannot be negative';
          END IF;

          SELECT quest_id, scope_node_id, kind, cap_microunits
            INTO allocation_quest, allocation_scope, allocation_kind, allocation_cap
          FROM research_budget_allocations
          WHERE allocation_id = NEW.research_budget_allocation_id
          FOR UPDATE;
          IF NOT FOUND OR NEW.kind IS DISTINCT FROM allocation_kind THEN
            RAISE EXCEPTION 'budget event allocation/kind is invalid';
          END IF;

          SELECT quest_id, campaign_node_id INTO run_quest, run_campaign
          FROM research_campaign_runs WHERE run_id = NEW.run_id;
          IF NOT FOUND OR run_quest IS DISTINCT FROM allocation_quest THEN
            RAISE EXCEPTION 'budget event run is outside its allocation quest';
          END IF;
          SELECT node_type INTO scope_type
          FROM research_graph_nodes WHERE node_id = allocation_scope;
          IF scope_type = 'program' THEN
            SELECT parent_node_id INTO campaign_program
            FROM research_graph_nodes WHERE node_id = run_campaign;
            IF campaign_program IS DISTINCT FROM allocation_scope THEN
              RAISE EXCEPTION 'budget event run is outside its allocation program';
            END IF;
          ELSIF scope_type <> 'quest' THEN
            RAISE EXCEPTION 'budget allocation has an invalid graph scope';
          END IF;

          SELECT COALESCE(SUM(ROUND(amount * 1000000)::bigint), 0)
            INTO spent_microunits
          FROM budget_events
          WHERE research_budget_allocation_id = NEW.research_budget_allocation_id;
          charge_microunits := ROUND(NEW.amount * 1000000)::bigint;
          IF spent_microunits + charge_microunits > allocation_cap THEN
            RAISE EXCEPTION 'research budget allocation cap exceeded';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_allocated_budget_event
        BEFORE INSERT OR UPDATE OR DELETE ON budget_events
        FOR EACH ROW EXECUTE FUNCTION aletheia_enforce_allocated_budget_event()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_allocated_budget_event ON budget_events")
    op.execute("DROP FUNCTION aletheia_enforce_allocated_budget_event()")
    op.execute("DROP TRIGGER trg_hypothesis_attempt_family_binding ON hypothesis_attempts")
    op.execute("DROP FUNCTION aletheia_enforce_hypothesis_family_binding()")
