"""Enforce append-only and receipt completeness for scientific memory.

Revision ID: 20260817_0017
Revises: 20260817_0016
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260817_0017"
down_revision: str | None = "20260817_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION aletheia_research_memory_append_only()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'research memory ledger rows are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in (
        "research_memory_facts",
        "research_memory_task_bindings",
        "research_memory_compactions",
        "research_memory_compaction_members",
        "research_memory_context_receipts",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION aletheia_research_memory_append_only()
            """
        )

    op.execute(
        """
        CREATE FUNCTION aletheia_validate_research_memory_fact()
        RETURNS trigger AS $$
        DECLARE
          graph_quest text;
          command_status text;
          command_type text;
          aggregate_type text;
          aggregate_id text;
          operation text;
          input_fact_id text;
          input_fact_sha text;
        BEGIN
          SELECT quest_id INTO graph_quest
          FROM research_graph_nodes WHERE node_id = NEW.scope_node_id;
          IF NOT FOUND OR graph_quest IS DISTINCT FROM NEW.quest_id THEN
            RAISE EXCEPTION 'research memory fact scope/quest is invalid';
          END IF;
          SELECT status, scientific_commands.command_type,
                 scientific_commands.aggregate_type, scientific_commands.aggregate_id,
                 input_json->>'operation', input_json->>'fact_id',
                 input_json->>'fact_sha256'
            INTO command_status, command_type, aggregate_type, aggregate_id,
                 operation, input_fact_id, input_fact_sha
          FROM scientific_commands WHERE command_id = NEW.command_id;
          IF NOT FOUND OR command_status <> 'applying'
             OR command_type <> 'research_memory.mutation'
             OR aggregate_type <> 'research_memory'
             OR aggregate_id IS DISTINCT FROM NEW.fact_id
             OR operation <> 'register_fact'
             OR input_fact_id IS DISTINCT FROM NEW.fact_id
             OR input_fact_sha IS DISTINCT FROM NEW.fact_sha256 THEN
            RAISE EXCEPTION 'research memory fact is outside its applying command';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_research_memory_fact_guard
        BEFORE INSERT ON research_memory_facts
        FOR EACH ROW EXECUTE FUNCTION aletheia_validate_research_memory_fact()
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_validate_research_memory_task_binding()
        RETURNS trigger AS $$
        DECLARE
          fact_command text;
          command_status text;
          command_input jsonb;
        BEGIN
          IF NEW.task_key <> '*'
             AND NEW.task_key !~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$' THEN
            RAISE EXCEPTION 'research memory task key is invalid';
          END IF;
          SELECT command_id INTO fact_command
          FROM research_memory_facts WHERE fact_id = NEW.fact_id;
          SELECT status, input_json INTO command_status, command_input
          FROM scientific_commands WHERE command_id = NEW.command_id;
          IF fact_command IS DISTINCT FROM NEW.command_id
             OR command_status IS DISTINCT FROM 'applying'
             OR command_input->>'operation' <> 'register_fact'
             OR NOT EXISTS (
               SELECT 1
               FROM jsonb_array_elements(command_input->'fact'->'task_bindings') binding
               WHERE binding->>'task_key' = NEW.task_key
                 AND binding->>'context_role' = NEW.context_role
             ) THEN
            RAISE EXCEPTION 'research memory task binding is outside its fact command';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_research_memory_task_binding_guard
        BEFORE INSERT ON research_memory_task_bindings
        FOR EACH ROW EXECUTE FUNCTION aletheia_validate_research_memory_task_binding()
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_validate_research_memory_compaction()
        RETURNS trigger AS $$
        DECLARE
          graph_quest text;
          parent_scope text;
          parent_task text;
          command_status text;
          command_type text;
          aggregate_type text;
          aggregate_id text;
          operation text;
        BEGIN
          IF NEW.task_key <> '*'
             AND NEW.task_key !~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$' THEN
            RAISE EXCEPTION 'research memory compaction task key is invalid';
          END IF;
          SELECT quest_id INTO graph_quest
          FROM research_graph_nodes WHERE node_id = NEW.scope_node_id;
          IF NOT FOUND OR graph_quest IS DISTINCT FROM NEW.quest_id THEN
            RAISE EXCEPTION 'research memory compaction scope/quest is invalid';
          END IF;
          IF NEW.parent_compaction_id IS NOT NULL THEN
            SELECT scope_node_id, task_key INTO parent_scope, parent_task
            FROM research_memory_compactions
            WHERE compaction_id = NEW.parent_compaction_id;
            IF NOT FOUND OR parent_scope IS DISTINCT FROM NEW.scope_node_id
               OR parent_task IS DISTINCT FROM NEW.task_key THEN
              RAISE EXCEPTION 'research memory compaction parent has another scope/task';
            END IF;
          END IF;
          SELECT status, scientific_commands.command_type,
                 scientific_commands.aggregate_type, scientific_commands.aggregate_id,
                 input_json->>'operation'
            INTO command_status, command_type, aggregate_type, aggregate_id, operation
          FROM scientific_commands WHERE command_id = NEW.command_id;
          IF NOT FOUND OR command_status <> 'applying'
             OR command_type <> 'research_memory.mutation'
             OR aggregate_type <> 'research_memory'
             OR aggregate_id IS DISTINCT FROM NEW.compaction_id
             OR operation <> 'compact' THEN
            RAISE EXCEPTION 'research memory compaction is outside its applying command';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_research_memory_compaction_guard
        BEFORE INSERT ON research_memory_compactions
        FOR EACH ROW EXECUTE FUNCTION aletheia_validate_research_memory_compaction()
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_validate_research_memory_member()
        RETURNS trigger AS $$
        DECLARE
          compaction_task text;
          compaction_scope text;
          compaction_command text;
          command_status text;
          scope_type text;
          scope_parent text;
          scope_quest text;
          fact_scope text;
          stored_fact_sha text;
          stored_fact_kind text;
          has_binding boolean;
          is_required boolean;
          is_non_droppable boolean;
          expected_disposition text;
        BEGIN
          SELECT task_key, scope_node_id, command_id
            INTO compaction_task, compaction_scope, compaction_command
          FROM research_memory_compactions WHERE compaction_id = NEW.compaction_id;
          SELECT status INTO command_status
          FROM scientific_commands WHERE command_id = compaction_command;
          IF NOT FOUND OR command_status <> 'applying' THEN
            RAISE EXCEPTION 'research memory member cannot be added after compaction commit';
          END IF;
          SELECT scope_node_id, fact_sha256, kind
            INTO fact_scope, stored_fact_sha, stored_fact_kind
          FROM research_memory_facts WHERE fact_id = NEW.fact_id;
          IF NOT FOUND OR NEW.fact_sha256 IS DISTINCT FROM stored_fact_sha
             OR NEW.fact_kind IS DISTINCT FROM stored_fact_kind THEN
            RAISE EXCEPTION 'research memory member fact identity is invalid';
          END IF;
          SELECT node_type, parent_node_id, quest_id
            INTO scope_type, scope_parent, scope_quest
          FROM research_graph_nodes WHERE node_id = compaction_scope;
          IF fact_scope IS DISTINCT FROM compaction_scope
             AND fact_scope IS DISTINCT FROM scope_parent
             AND fact_scope IS DISTINCT FROM scope_quest THEN
            RAISE EXCEPTION 'research memory member is outside compaction ancestry';
          END IF;
          SELECT EXISTS (
                   SELECT 1 FROM research_memory_task_bindings
                   WHERE fact_id = NEW.fact_id AND task_key IN (compaction_task, '*')
                 ),
                 EXISTS (
                   SELECT 1 FROM research_memory_task_bindings
                   WHERE fact_id = NEW.fact_id AND task_key IN (compaction_task, '*')
                     AND context_role = 'required'
                 )
            INTO has_binding, is_required;
          IF NOT has_binding THEN
            RAISE EXCEPTION 'research memory member is not bound to the compaction task';
          END IF;
          is_non_droppable := stored_fact_kind IN (
            'negative_result','contradiction','limitation','failed_hypothesis','safety_boundary'
          );
          IF is_non_droppable THEN
            expected_disposition := 'exact_non_droppable';
          ELSIF is_required THEN
            expected_disposition := 'exact_required';
          ELSE
            expected_disposition := 'summary';
          END IF;
          IF NEW.disposition IS DISTINCT FROM expected_disposition THEN
            RAISE EXCEPTION 'research memory member has an invalid coverage disposition';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_research_memory_member_guard
        BEFORE INSERT ON research_memory_compaction_members
        FOR EACH ROW EXECUTE FUNCTION aletheia_validate_research_memory_member()
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_validate_research_memory_compaction_complete()
        RETURNS trigger AS $$
        DECLARE
          observed_count integer;
          observed_exact integer;
        BEGIN
          SELECT COUNT(*), COUNT(*) FILTER (WHERE disposition <> 'summary')
            INTO observed_count, observed_exact
          FROM research_memory_compaction_members
          WHERE compaction_id = NEW.compaction_id;
          IF observed_count <> NEW.source_count OR observed_exact <> NEW.exact_count THEN
            RAISE EXCEPTION 'research memory compaction member receipt is incomplete';
          END IF;
          RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_research_memory_compaction_complete
        AFTER INSERT ON research_memory_compactions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION aletheia_validate_research_memory_compaction_complete()
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_validate_research_memory_context()
        RETURNS trigger AS $$
        DECLARE
          compaction_quest text;
          compaction_scope text;
          compaction_task text;
          command_status text;
          command_type text;
          aggregate_type text;
          aggregate_id text;
          operation text;
        BEGIN
          IF NEW.task_key <> '*'
             AND NEW.task_key !~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$' THEN
            RAISE EXCEPTION 'research memory context task key is invalid';
          END IF;
          SELECT quest_id, scope_node_id, task_key
            INTO compaction_quest, compaction_scope, compaction_task
          FROM research_memory_compactions WHERE compaction_id = NEW.compaction_id;
          IF NOT FOUND OR compaction_quest IS DISTINCT FROM NEW.quest_id
             OR compaction_scope IS DISTINCT FROM NEW.scope_node_id
             OR compaction_task IS DISTINCT FROM NEW.task_key THEN
            RAISE EXCEPTION 'research memory context is rebound from its compaction';
          END IF;
          SELECT status, scientific_commands.command_type,
                 scientific_commands.aggregate_type, scientific_commands.aggregate_id,
                 input_json->>'operation'
            INTO command_status, command_type, aggregate_type, aggregate_id, operation
          FROM scientific_commands WHERE command_id = NEW.command_id;
          IF NOT FOUND OR command_status <> 'applying'
             OR command_type <> 'research_memory.context'
             OR aggregate_type <> 'research_memory_context'
             OR aggregate_id IS DISTINCT FROM NEW.context_receipt_id
             OR operation <> 'build_context' THEN
            RAISE EXCEPTION 'research memory context is outside its applying command';
          END IF;
          IF NEW.payload_json->>'context_sha256' IS DISTINCT FROM NEW.context_sha256
             OR char_length(NEW.payload_json->>'prompt_text') <> NEW.prompt_chars THEN
            RAISE EXCEPTION 'research memory context payload receipt is invalid';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_research_memory_context_guard
        BEFORE INSERT ON research_memory_context_receipts
        FOR EACH ROW EXECUTE FUNCTION aletheia_validate_research_memory_context()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_research_memory_context_guard ON research_memory_context_receipts")
    op.execute("DROP FUNCTION aletheia_validate_research_memory_context()")
    op.execute(
        "DROP TRIGGER trg_research_memory_compaction_complete ON research_memory_compactions"
    )
    op.execute("DROP FUNCTION aletheia_validate_research_memory_compaction_complete()")
    op.execute(
        "DROP TRIGGER trg_research_memory_member_guard ON research_memory_compaction_members"
    )
    op.execute("DROP FUNCTION aletheia_validate_research_memory_member()")
    op.execute("DROP TRIGGER trg_research_memory_compaction_guard ON research_memory_compactions")
    op.execute("DROP FUNCTION aletheia_validate_research_memory_compaction()")
    op.execute(
        "DROP TRIGGER trg_research_memory_task_binding_guard ON research_memory_task_bindings"
    )
    op.execute("DROP FUNCTION aletheia_validate_research_memory_task_binding()")
    op.execute("DROP TRIGGER trg_research_memory_fact_guard ON research_memory_facts")
    op.execute("DROP FUNCTION aletheia_validate_research_memory_fact()")
    for table in reversed(
        (
            "research_memory_facts",
            "research_memory_task_bindings",
            "research_memory_compactions",
            "research_memory_compaction_members",
            "research_memory_context_receipts",
        )
    ):
        op.execute(f"DROP TRIGGER trg_{table}_append_only ON {table}")
    op.execute("DROP FUNCTION aletheia_research_memory_append_only()")
