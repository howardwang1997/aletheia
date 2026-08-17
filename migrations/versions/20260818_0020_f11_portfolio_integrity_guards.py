"""Enforce append-only and command-bound shadow portfolio receipts.

Revision ID: 20260818_0020
Revises: 20260818_0019
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260818_0020"
down_revision: str | None = "20260818_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION aletheia_research_portfolio_append_only()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'research portfolio ledger rows are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in (
        "research_portfolio_slates",
        "research_portfolio_candidates",
        "research_portfolio_human_plans",
        "research_portfolio_epochs",
        "research_portfolio_scores",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION aletheia_research_portfolio_append_only()
            """
        )

    op.execute(
        """
        CREATE FUNCTION aletheia_validate_research_portfolio_slate()
        RETURNS trigger AS $$
        DECLARE
          memory_quest text;
          memory_scope text;
          memory_task text;
          memory_provider text;
          memory_model text;
          command_status text;
          command_type text;
          aggregate_type text;
          aggregate_id text;
          command_input jsonb;
        BEGIN
          SELECT quest_id, scope_node_id, task_key, consumer_provider, consumer_model
            INTO memory_quest, memory_scope, memory_task, memory_provider, memory_model
          FROM research_memory_context_receipts
          WHERE context_receipt_id = NEW.memory_context_receipt_id;
          IF NOT FOUND OR memory_quest IS DISTINCT FROM NEW.quest_id
             OR memory_scope IS DISTINCT FROM NEW.quest_id
             OR memory_task IS DISTINCT FROM NEW.spec_json #>> '{policy,memory_task_key}'
             OR memory_provider IS DISTINCT FROM NEW.spec_json #>> '{proposal,proposer_provider}'
             OR memory_model IS DISTINCT FROM NEW.spec_json #>> '{proposal,proposer_model}' THEN
            RAISE EXCEPTION 'research portfolio slate has an invalid memory boundary';
          END IF;
          SELECT status, scientific_commands.command_type,
                 scientific_commands.aggregate_type, scientific_commands.aggregate_id,
                 input_json
            INTO command_status, command_type, aggregate_type, aggregate_id, command_input
          FROM scientific_commands WHERE command_id = NEW.command_id;
          IF NOT FOUND OR command_status <> 'applying'
             OR command_type <> 'research_portfolio.mutation'
             OR aggregate_type <> 'research_portfolio'
             OR aggregate_id IS DISTINCT FROM NEW.slate_id
             OR command_input->>'operation' <> 'register_slate'
             OR command_input->>'slate_id' IS DISTINCT FROM NEW.slate_id
             OR command_input->>'spec_sha256' IS DISTINCT FROM NEW.spec_sha256
             OR command_input->>'graph_sha256' IS DISTINCT FROM NEW.graph_sha256
             OR command_input->>'budget_state_sha256' IS DISTINCT FROM NEW.budget_state_sha256 THEN
            RAISE EXCEPTION 'research portfolio slate is outside its applying command';
          END IF;
          IF NEW.spec_json #>> '{policy,quest_id}' IS DISTINCT FROM NEW.quest_id
             OR NEW.spec_json #>> '{proposal,quest_id}' IS DISTINCT FROM NEW.quest_id
             OR NEW.spec_json #>> '{proposal,graph_sha256}' IS DISTINCT FROM NEW.graph_sha256
             OR NEW.spec_json #>> '{proposal,memory_context_receipt_id}'
                  IS DISTINCT FROM NEW.memory_context_receipt_id
             OR NEW.spec_json #>> '{proposal,proposer_principal}' IS NOT DISTINCT FROM
                  NEW.spec_json #>> '{assessment_batch,manifest,assessor_principal}'
             OR NEW.graph_snapshot_json->>'graph_sha256' IS DISTINCT FROM NEW.graph_sha256
             OR NEW.spec_json #>> '{policy,mode}' <> 'shadow'
             OR jsonb_typeof(NEW.budget_state_json) <> 'array' THEN
            RAISE EXCEPTION 'research portfolio slate bindings are inconsistent';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_research_portfolio_slate_guard
        BEFORE INSERT ON research_portfolio_slates
        FOR EACH ROW EXECUTE FUNCTION aletheia_validate_research_portfolio_slate()
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_validate_research_portfolio_candidate()
        RETURNS trigger AS $$
        DECLARE
          slate_quest text;
          slate_command text;
          command_status text;
          command_input jsonb;
          target_quest text;
          target_type text;
          target_parent text;
          family_program text;
        BEGIN
          SELECT quest_id, command_id INTO slate_quest, slate_command
          FROM research_portfolio_slates WHERE slate_id = NEW.slate_id;
          SELECT status, input_json INTO command_status, command_input
          FROM scientific_commands WHERE command_id = slate_command;
          IF NOT FOUND OR command_status <> 'applying'
             OR NOT EXISTS (
               SELECT 1
               FROM jsonb_array_elements(command_input->'candidate_bindings') binding
               WHERE binding->>'candidate_id' = NEW.candidate_id
                 AND binding->>'action_sha256' = NEW.action_sha256
                 AND binding->>'assessment_sha256' = NEW.assessment_sha256
                 AND binding->>'program_id' = NEW.program_id
                 AND binding->>'family_id' IS NOT DISTINCT FROM NEW.family_id
             ) THEN
            RAISE EXCEPTION 'research portfolio candidate is outside its slate command';
          END IF;
          IF NEW.action_json->>'action_type' IS DISTINCT FROM NEW.action_type
             OR NEW.action_json->>'target_node_id' IS DISTINCT FROM NEW.target_node_id
             OR NEW.assessment_json->>'candidate_id' IS DISTINCT FROM NEW.candidate_id
             OR NEW.assessment_json->>'action_sha256' IS DISTINCT FROM NEW.action_sha256 THEN
            RAISE EXCEPTION 'research portfolio candidate payload is rebound';
          END IF;
          SELECT quest_id, node_type, parent_node_id
            INTO target_quest, target_type, target_parent
          FROM research_graph_nodes WHERE node_id = NEW.target_node_id;
          IF NOT FOUND OR target_quest IS DISTINCT FROM slate_quest
             OR NEW.program_id IS DISTINCT FROM (
                CASE WHEN target_type = 'program' THEN NEW.target_node_id ELSE target_parent END
             ) THEN
            RAISE EXCEPTION 'research portfolio candidate target/program is invalid';
          END IF;
          IF NEW.action_type IN (
               'advance_campaign','discriminating_experiment','replication','mechanism_test'
             ) AND target_type <> 'campaign' THEN
            RAISE EXCEPTION 'research portfolio campaign action has another target type';
          END IF;
          IF NEW.action_type IN (
               'repair_capability','start_campaign','pause_program','stop_program'
             ) AND target_type <> 'program' THEN
            RAISE EXCEPTION 'research portfolio program action has another target type';
          END IF;
          IF NEW.action_type = 'acquire_data' AND target_type NOT IN ('program','campaign') THEN
            RAISE EXCEPTION 'research portfolio data action has another target type';
          END IF;
          IF NEW.family_id IS NOT NULL THEN
            SELECT program_node_id INTO family_program
            FROM research_scientific_families WHERE family_id = NEW.family_id;
            IF NOT FOUND OR family_program IS DISTINCT FROM NEW.program_id THEN
              RAISE EXCEPTION 'research portfolio candidate family belongs to another Program';
            END IF;
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_research_portfolio_candidate_guard
        BEFORE INSERT ON research_portfolio_candidates
        FOR EACH ROW EXECUTE FUNCTION aletheia_validate_research_portfolio_candidate()
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_validate_research_portfolio_slate_complete()
        RETURNS trigger AS $$
        DECLARE
          expected_count integer;
          observed_count integer;
        BEGIN
          SELECT jsonb_array_length(input_json->'candidate_bindings')
            INTO expected_count
          FROM scientific_commands WHERE command_id = NEW.command_id;
          SELECT COUNT(*) INTO observed_count
          FROM research_portfolio_candidates WHERE slate_id = NEW.slate_id;
          IF expected_count IS NULL OR expected_count <> observed_count THEN
            RAISE EXCEPTION 'research portfolio slate candidate receipt is incomplete';
          END IF;
          RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_research_portfolio_slate_complete
        AFTER INSERT ON research_portfolio_slates
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION aletheia_validate_research_portfolio_slate_complete()
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_validate_research_portfolio_human_plan()
        RETURNS trigger AS $$
        DECLARE
          command_status text;
          command_type text;
          aggregate_type text;
          aggregate_id text;
          command_principal text;
          command_input jsonb;
          proposer_principal text;
          assessor_principal text;
        BEGIN
          SELECT status, scientific_commands.command_type,
                 scientific_commands.aggregate_type, scientific_commands.aggregate_id,
                 principal, input_json
            INTO command_status, command_type, aggregate_type, aggregate_id,
                 command_principal, command_input
          FROM scientific_commands WHERE command_id = NEW.command_id;
          IF NOT FOUND OR command_status <> 'applying'
             OR command_type <> 'research_portfolio.mutation'
             OR aggregate_type <> 'research_portfolio'
             OR aggregate_id IS DISTINCT FROM NEW.human_plan_id
             OR command_input->>'operation' <> 'commit_human_plan'
             OR command_input->>'slate_id' IS DISTINCT FROM NEW.slate_id
             OR command_input->>'human_plan_id' IS DISTINCT FROM NEW.human_plan_id
             OR command_input->>'plan_sha256' IS DISTINCT FROM NEW.plan_sha256
             OR command_input->'plan' IS DISTINCT FROM NEW.plan_json THEN
            RAISE EXCEPTION 'research portfolio human plan is outside its applying command';
          END IF;
          SELECT spec_json #>> '{proposal,proposer_principal}',
                 spec_json #>> '{assessment_batch,manifest,assessor_principal}'
            INTO proposer_principal, assessor_principal
          FROM research_portfolio_slates WHERE slate_id = NEW.slate_id;
          IF NOT FOUND OR proposer_principal IS NOT DISTINCT FROM assessor_principal
             OR command_principal IN (proposer_principal, assessor_principal)
             OR NEW.created_by IS DISTINCT FROM command_principal
             OR NEW.plan_json->>'planner_output_access' <> 'none' THEN
            RAISE EXCEPTION 'research portfolio human plan is not independently blinded';
          END IF;
          IF EXISTS (
               SELECT 1 FROM jsonb_array_elements_text(
                 NEW.plan_json->'selected_candidate_ids'
               ) selected(candidate_id)
               WHERE NOT EXISTS (
                 SELECT 1 FROM research_portfolio_candidates candidate
                 WHERE candidate.slate_id = NEW.slate_id
                   AND candidate.candidate_id = selected.candidate_id
               )
             ) THEN
            RAISE EXCEPTION 'research portfolio human plan selects another slate';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_research_portfolio_human_plan_guard
        BEFORE INSERT ON research_portfolio_human_plans
        FOR EACH ROW EXECUTE FUNCTION aletheia_validate_research_portfolio_human_plan()
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_validate_research_portfolio_epoch()
        RETURNS trigger AS $$
        DECLARE
          command_status text;
          command_type text;
          aggregate_type text;
          aggregate_id text;
          command_input jsonb;
          plan_slate text;
          slate_quest text;
        BEGIN
          SELECT status, scientific_commands.command_type,
                 scientific_commands.aggregate_type, scientific_commands.aggregate_id,
                 input_json
            INTO command_status, command_type, aggregate_type, aggregate_id, command_input
          FROM scientific_commands WHERE command_id = NEW.command_id;
          IF NOT FOUND OR command_status <> 'applying'
             OR command_type <> 'research_portfolio.mutation'
             OR aggregate_type <> 'research_portfolio'
             OR aggregate_id IS DISTINCT FROM NEW.epoch_id
             OR command_input->>'operation' <> 'evaluate_slate'
             OR command_input->>'slate_id' IS DISTINCT FROM NEW.slate_id
             OR command_input->>'epoch_id' IS DISTINCT FROM NEW.epoch_id
             OR command_input->>'human_plan_id' IS DISTINCT FROM NEW.human_plan_id
             OR command_input->>'decision_sha256' IS DISTINCT FROM NEW.decision_sha256
             OR command_input->>'comparison_sha256' IS DISTINCT FROM NEW.comparison_sha256
             OR command_input->>'epoch_sha256' IS DISTINCT FROM NEW.epoch_sha256 THEN
            RAISE EXCEPTION 'research portfolio epoch is outside its applying command';
          END IF;
          SELECT slate_id INTO plan_slate FROM research_portfolio_human_plans
          WHERE human_plan_id = NEW.human_plan_id;
          SELECT quest_id INTO slate_quest FROM research_portfolio_slates
          WHERE slate_id = NEW.slate_id;
          IF plan_slate IS DISTINCT FROM NEW.slate_id
             OR slate_quest IS DISTINCT FROM NEW.quest_id
             OR NEW.decision_json->>'shadow_only' <> 'true'
             OR NEW.decision_json->>'actions_enqueued' <> 'false'
             OR NEW.shadow_only IS NOT TRUE OR NEW.actions_enqueued IS NOT FALSE THEN
            RAISE EXCEPTION 'research portfolio epoch is not a bound shadow decision';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_research_portfolio_epoch_guard
        BEFORE INSERT ON research_portfolio_epochs
        FOR EACH ROW EXECUTE FUNCTION aletheia_validate_research_portfolio_epoch()
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_validate_research_portfolio_score()
        RETURNS trigger AS $$
        DECLARE
          epoch_slate text;
          epoch_command text;
          command_status text;
          command_input jsonb;
        BEGIN
          SELECT slate_id, command_id INTO epoch_slate, epoch_command
          FROM research_portfolio_epochs WHERE epoch_id = NEW.epoch_id;
          SELECT status, input_json INTO command_status, command_input
          FROM scientific_commands WHERE command_id = epoch_command;
          IF NOT FOUND OR command_status <> 'applying'
             OR epoch_slate IS DISTINCT FROM NEW.slate_id
             OR NOT EXISTS (
               SELECT 1
               FROM jsonb_array_elements(command_input->'score_bindings') binding
               WHERE binding->>'candidate_id' = NEW.candidate_id
                 AND binding->>'score_sha256' = NEW.score_sha256
                 AND (binding->>'rank')::integer = NEW.rank
                 AND (binding->>'selected')::boolean = NEW.selected
             ) THEN
            RAISE EXCEPTION 'research portfolio score is outside its epoch command';
          END IF;
          IF NEW.score_json->>'candidate_id' IS DISTINCT FROM NEW.candidate_id
             OR (NEW.score_json->>'feasible')::boolean IS DISTINCT FROM NEW.feasible
             OR (NEW.score_json->>'base_utility_microscore')::bigint
                  IS DISTINCT FROM NEW.base_utility_microscore THEN
            RAISE EXCEPTION 'research portfolio score payload is rebound';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_research_portfolio_score_guard
        BEFORE INSERT ON research_portfolio_scores
        FOR EACH ROW EXECUTE FUNCTION aletheia_validate_research_portfolio_score()
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_validate_research_portfolio_epoch_complete()
        RETURNS trigger AS $$
        DECLARE
          observed_count integer;
          observed_rank_count integer;
        BEGIN
          SELECT COUNT(*), COUNT(DISTINCT rank)
            INTO observed_count, observed_rank_count
          FROM research_portfolio_scores WHERE epoch_id = NEW.epoch_id;
          IF observed_count <> NEW.score_count
             OR observed_rank_count <> NEW.score_count THEN
            RAISE EXCEPTION 'research portfolio epoch score receipt is incomplete';
          END IF;
          RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_research_portfolio_epoch_complete
        AFTER INSERT ON research_portfolio_epochs
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION aletheia_validate_research_portfolio_epoch_complete()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_research_portfolio_epoch_complete ON research_portfolio_epochs")
    op.execute("DROP FUNCTION aletheia_validate_research_portfolio_epoch_complete()")
    op.execute("DROP TRIGGER trg_research_portfolio_score_guard ON research_portfolio_scores")
    op.execute("DROP FUNCTION aletheia_validate_research_portfolio_score()")
    op.execute("DROP TRIGGER trg_research_portfolio_epoch_guard ON research_portfolio_epochs")
    op.execute("DROP FUNCTION aletheia_validate_research_portfolio_epoch()")
    op.execute(
        "DROP TRIGGER trg_research_portfolio_human_plan_guard ON research_portfolio_human_plans"
    )
    op.execute("DROP FUNCTION aletheia_validate_research_portfolio_human_plan()")
    op.execute("DROP TRIGGER trg_research_portfolio_slate_complete ON research_portfolio_slates")
    op.execute("DROP FUNCTION aletheia_validate_research_portfolio_slate_complete()")
    op.execute(
        "DROP TRIGGER trg_research_portfolio_candidate_guard ON research_portfolio_candidates"
    )
    op.execute("DROP FUNCTION aletheia_validate_research_portfolio_candidate()")
    op.execute("DROP TRIGGER trg_research_portfolio_slate_guard ON research_portfolio_slates")
    op.execute("DROP FUNCTION aletheia_validate_research_portfolio_slate()")
    for table in reversed(
        (
            "research_portfolio_slates",
            "research_portfolio_candidates",
            "research_portfolio_human_plans",
            "research_portfolio_epochs",
            "research_portfolio_scores",
        )
    ):
        op.execute(f"DROP TRIGGER trg_{table}_append_only ON {table}")
    op.execute("DROP FUNCTION aletheia_research_portfolio_append_only()")
