"""Persist the PR-5 controller and independent observation authority chain.

Revision ID: 20260828_0027
Revises: 20260827_0026
Create Date: 2026-08-28

The controller rows are recoverable delivery receipts, not a second research ledger.  Scientific
authorization, validation, and admission rows are append-only.  A Phase-1 admitted observation is
valid only when the same transaction also creates its exact ``observation_incorporated`` Research
Kernel event; rejected admission decisions deliberately have no incorporation event.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260828_0027"
down_revision: str | None = "20260827_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_APPEND_ONLY_TABLES = (
    "research_controller_registrations",
    "research_controller_deliveries",
    "research_controller_delivery_attempts",
    "research_controller_delivery_resolutions",
    "research_protocol_compilations",
    "research_scientific_execution_authorizations",
    "research_observation_issuance_challenges",
    "research_observation_validation_receipts",
    "research_observation_admissions",
    "research_continuation_receipts",
)

_V1_EVENT_TYPES = (
    "'charter_activated','charter_revised','opportunity_recorded',"
    "'problem_admitted','question_admitted','action_proposed','action_authorized',"
    "'action_rejected','action_superseded','continue_committed','activate_committed',"
    "'refine_committed','fork_committed','backtrack_committed','pause_committed',"
    "'stop_committed'"
)


def upgrade() -> None:
    # The new admission participant uses the normal Kernel transaction/event/outbox authority.
    # The typed composite uniqueness makes both action-source and incorporation FKs exact.
    op.drop_constraint(
        "ck_research_kernel_events_type",
        "research_kernel_events",
        type_="check",
    )
    op.create_check_constraint(
        "ck_research_kernel_events_type",
        "research_kernel_events",
        f"event_type IN ({_V1_EVENT_TYPES},'observation_incorporated')",
    )
    op.create_unique_constraint(
        "uq_rke_scoped_typed_event",
        "research_kernel_events",
        ["quest_id", "sequence", "event_sha256", "event_type"],
    )
    op.create_unique_constraint(
        "uq_rko_exact_controller_source",
        "research_kernel_outbox",
        ["outbox_id", "quest_id", "sequence", "event_sha256"],
    )
    op.create_unique_constraint(
        "uq_exec_qto_exact_controller_source",
        "execution_qualification_terminal_outbox",
        ["outbox_id", "execution_id", "attempt_id", "terminal_authority_sha256"],
    )

    op.execute(
        r"""
        CREATE TABLE research_controller_registrations (
          registration_sha256 varchar(64) PRIMARY KEY,
          registration_id varchar(36) NOT NULL,
          quest_id varchar(36) NOT NULL,
          controller_id varchar(128) NOT NULL,
          controller_kind varchar(64) NOT NULL,
          controller_manifest_sha256 varchar(64) NOT NULL,
          controller_principal_id varchar(128) NOT NULL,
          registered_by_principal_id varchar(128) NOT NULL,
          launch_request_sha256 varchar(64) NOT NULL,
          registration_json jsonb NOT NULL,
          registered_at timestamptz NOT NULL,
          CONSTRAINT ck_rc_reg_hashes CHECK (
            registration_sha256 ~ '^[0-9a-f]{64}$' AND
            controller_manifest_sha256 ~ '^[0-9a-f]{64}$' AND
            launch_request_sha256 ~ '^[0-9a-f]{64}$'
          ),
          CONSTRAINT ck_rc_reg_quest CHECK (quest_id ~ '^qst_[0-9a-f]{32}$'),
          CONSTRAINT ck_rc_reg_id CHECK (registration_id ~ '^rcr_[0-9a-f]{32}$'),
          CONSTRAINT ck_rc_reg_kind CHECK (controller_kind = 'research.controller.v1'),
          CONSTRAINT ck_rc_reg_json CHECK (
            jsonb_typeof(registration_json) = 'object' AND
            registration_json->>'schema_name' = 'aletheia.research_controller_registration' AND
            registration_json->>'registration_id' = registration_id AND
            registration_json->>'controller_id' = controller_id AND
            registration_json->>'controller_manifest_sha256' = controller_manifest_sha256 AND
            registration_json->>'controller_principal_id' = controller_principal_id AND
            registration_json->>'registered_by_principal_id' = registered_by_principal_id AND
            registration_json #>> '{launch_request,quest_id}' = quest_id AND
            (registration_json->>'registered_at')::timestamptz = registered_at
          ),
          CONSTRAINT uq_rc_reg_quest UNIQUE (quest_id),
          CONSTRAINT uq_rc_reg_id UNIQUE (registration_id),
          CONSTRAINT uq_rc_reg_launch_request UNIQUE (launch_request_sha256),
          CONSTRAINT uq_rc_reg_exact_quest
            UNIQUE (registration_sha256, registration_id, quest_id),
          CONSTRAINT uq_rc_reg_exact_launch
            UNIQUE (registration_sha256, registration_id, quest_id, launch_request_sha256),
          CONSTRAINT fk_rc_reg_quest FOREIGN KEY (quest_id)
            REFERENCES research_quest_streams (quest_id)
        );
        CREATE INDEX ix_rc_reg_registered_at
          ON research_controller_registrations (registered_at);
        CREATE INDEX ix_rc_reg_controller
          ON research_controller_registrations (controller_id);

        CREATE TABLE research_controller_deliveries (
          delivery_sha256 varchar(64) PRIMARY KEY,
          registration_sha256 varchar(64) NOT NULL,
          registration_id varchar(36) NOT NULL,
          quest_id varchar(36) NOT NULL,
          source_kind varchar(32) NOT NULL,
          source_key varchar(192) NOT NULL,
          source_sha256 varchar(64) NOT NULL,
          source_stream_version bigint,
          launch_request_sha256 varchar(64),
          execution_id varchar(36),
          attempt_id varchar(36),
          task_id varchar(96) NOT NULL,
          delivery_json jsonb NOT NULL,
          delivered_at timestamptz NOT NULL,
          CONSTRAINT ck_rc_delivery_hashes CHECK (
            delivery_sha256 ~ '^[0-9a-f]{64}$' AND
            source_sha256 ~ '^[0-9a-f]{64}$' AND
            (launch_request_sha256 IS NULL OR
              launch_request_sha256 ~ '^[0-9a-f]{64}$')
          ),
          CONSTRAINT ck_rc_delivery_quest CHECK (quest_id ~ '^qst_[0-9a-f]{32}$'),
          CONSTRAINT ck_rc_delivery_source_kind CHECK (
            source_kind IN ('launch','kernel_outbox','execution_terminal_outbox')
          ),
          CONSTRAINT ck_rc_delivery_source_shape CHECK (
            (source_kind = 'launch' AND source_key = registration_id AND
              launch_request_sha256 = source_sha256 AND source_stream_version IS NULL AND
              execution_id IS NULL AND attempt_id IS NULL) OR
            (source_kind = 'kernel_outbox' AND launch_request_sha256 IS NULL AND
              source_stream_version >= 1 AND execution_id IS NULL AND attempt_id IS NULL) OR
            (source_kind = 'execution_terminal_outbox' AND
              launch_request_sha256 IS NULL AND source_stream_version IS NULL AND
              execution_id IS NOT NULL AND attempt_id IS NOT NULL)
          ),
          CONSTRAINT ck_rc_delivery_json CHECK (
            jsonb_typeof(delivery_json) = 'object' AND
            delivery_json->>'schema_name' = 'aletheia.research_controller_delivery' AND
            delivery_json->>'registration_sha256' = registration_sha256 AND
            delivery_json #>> '{wakeup,registration_id}' = registration_id AND
            delivery_json #>> '{wakeup,quest_id}' = quest_id AND
            delivery_json #>> '{wakeup,source_kind}' = source_kind AND
            delivery_json #>> '{wakeup,source_key}' = source_key AND
            delivery_json #>> '{wakeup,source_sha256}' = source_sha256 AND
            delivery_json->>'task_id' = task_id
          ),
          CONSTRAINT uq_rc_delivery_source_key UNIQUE (source_kind, source_key),
          CONSTRAINT uq_rc_delivery_source_hash UNIQUE (source_kind, source_sha256),
          CONSTRAINT uq_rc_delivery_task UNIQUE (task_id),
          CONSTRAINT uq_rc_delivery_exact_quest UNIQUE (delivery_sha256, quest_id),
          CONSTRAINT fk_rc_delivery_registration FOREIGN KEY
            (registration_sha256, registration_id, quest_id)
            REFERENCES research_controller_registrations
              (registration_sha256, registration_id, quest_id)
            DEFERRABLE INITIALLY DEFERRED,
          CONSTRAINT fk_rc_delivery_launch FOREIGN KEY
            (registration_sha256, registration_id, quest_id, launch_request_sha256)
            REFERENCES research_controller_registrations
              (registration_sha256, registration_id, quest_id, launch_request_sha256)
            DEFERRABLE INITIALLY DEFERRED,
          CONSTRAINT fk_rc_delivery_exact_source FOREIGN KEY
            (source_key, quest_id, source_stream_version, source_sha256)
            REFERENCES research_kernel_outbox
              (outbox_id, quest_id, sequence, event_sha256)
            DEFERRABLE INITIALLY DEFERRED,
          CONSTRAINT fk_rc_delivery_execution_terminal FOREIGN KEY
            (source_key, execution_id, attempt_id, source_sha256)
            REFERENCES execution_qualification_terminal_outbox
              (outbox_id, execution_id, attempt_id, terminal_authority_sha256)
            DEFERRABLE INITIALLY DEFERRED,
          CONSTRAINT fk_rc_delivery_task FOREIGN KEY (task_id)
            REFERENCES durable_tasks (task_id)
        );
        CREATE INDEX ix_rc_delivery_registration
          ON research_controller_deliveries (registration_sha256, delivered_at);

        CREATE TABLE research_controller_delivery_attempts (
          attempt_sha256 varchar(64) PRIMARY KEY,
          delivery_sha256 varchar(64) NOT NULL,
          quest_id varchar(36) NOT NULL,
          wakeup_sha256 varchar(64) NOT NULL,
          controller_manifest_sha256 varchar(64) NOT NULL,
          generation bigint NOT NULL,
          kind varchar(32) NOT NULL,
          task_id varchar(96) NOT NULL,
          task_request_sha256 varchar(64) NOT NULL,
          supersedes_task_id varchar(96),
          predecessor_status varchar(16),
          predecessor_terminal_category varchar(40),
          predecessor_terminal_detail_sha256 varchar(64),
          predecessor_result_sha256 varchar(64),
          predecessor_tick_receipt_sha256 varchar(64),
          attempt_json jsonb NOT NULL,
          recorded_at timestamptz NOT NULL,
          CONSTRAINT ck_rcda_hashes CHECK (
            attempt_sha256 ~ '^[0-9a-f]{64}$' AND
            delivery_sha256 ~ '^[0-9a-f]{64}$' AND
            wakeup_sha256 ~ '^[0-9a-f]{64}$' AND
            controller_manifest_sha256 ~ '^[0-9a-f]{64}$' AND
            task_request_sha256 ~ '^[0-9a-f]{64}$' AND
            (predecessor_terminal_detail_sha256 IS NULL OR
              predecessor_terminal_detail_sha256 ~ '^[0-9a-f]{64}$') AND
            (predecessor_result_sha256 IS NULL OR
              predecessor_result_sha256 ~ '^[0-9a-f]{64}$') AND
            (predecessor_tick_receipt_sha256 IS NULL OR
              predecessor_tick_receipt_sha256 ~ '^[0-9a-f]{64}$')
          ),
          CONSTRAINT ck_rcda_quest CHECK (quest_id ~ '^qst_[0-9a-f]{32}$'),
          CONSTRAINT ck_rcda_generation CHECK (generation >= 0 AND generation <= 1024),
          CONSTRAINT ck_rcda_kind CHECK (
            kind IN ('initial','failure_redrive','completed_successor')
          ),
          CONSTRAINT ck_rcda_distinct_tasks CHECK (task_id <> supersedes_task_id),
          CONSTRAINT ck_rcda_predecessor_shape CHECK (
            (generation = 0 AND kind = 'initial' AND supersedes_task_id IS NULL AND
              predecessor_status IS NULL AND predecessor_terminal_category IS NULL AND
              predecessor_terminal_detail_sha256 IS NULL AND
              predecessor_result_sha256 IS NULL AND
              predecessor_tick_receipt_sha256 IS NULL) OR
            (generation > 0 AND kind = 'failure_redrive' AND
              supersedes_task_id IS NOT NULL AND predecessor_status = 'failed' AND
              predecessor_terminal_category IS NOT NULL AND
              predecessor_terminal_detail_sha256 IS NOT NULL AND
              predecessor_result_sha256 IS NULL AND
              predecessor_tick_receipt_sha256 IS NULL) OR
            (generation > 0 AND kind = 'completed_successor' AND
              supersedes_task_id IS NOT NULL AND predecessor_status = 'succeeded' AND
              predecessor_terminal_category = 'success' AND
              predecessor_terminal_detail_sha256 IS NULL AND
              predecessor_result_sha256 IS NOT NULL AND
              predecessor_tick_receipt_sha256 IS NOT NULL)
          ),
          CONSTRAINT ck_rcda_json CHECK (
            jsonb_typeof(attempt_json) = 'object' AND
            attempt_json->>'schema_name' =
              'aletheia.research_controller_delivery_attempt' AND
            attempt_json->>'delivery_sha256' = delivery_sha256 AND
            attempt_json->>'quest_id' = quest_id AND
            attempt_json->>'wakeup_sha256' = wakeup_sha256 AND
            attempt_json->>'controller_manifest_sha256' = controller_manifest_sha256 AND
            (attempt_json->>'generation')::bigint = generation AND
            attempt_json->>'kind' = kind AND
            attempt_json->>'task_id' = task_id AND
            attempt_json->>'task_request_sha256' = task_request_sha256 AND
            attempt_json->>'supersedes_task_id' IS NOT DISTINCT FROM supersedes_task_id AND
            attempt_json->>'predecessor_status' IS NOT DISTINCT FROM predecessor_status AND
            attempt_json->>'predecessor_terminal_category'
              IS NOT DISTINCT FROM predecessor_terminal_category AND
            attempt_json->>'predecessor_terminal_detail_sha256'
              IS NOT DISTINCT FROM predecessor_terminal_detail_sha256 AND
            attempt_json->>'predecessor_result_sha256'
              IS NOT DISTINCT FROM predecessor_result_sha256 AND
            attempt_json->>'predecessor_tick_receipt_sha256'
              IS NOT DISTINCT FROM predecessor_tick_receipt_sha256 AND
            (attempt_json->>'recorded_at')::timestamptz = recorded_at
          ),
          CONSTRAINT uq_rcda_delivery_generation UNIQUE (delivery_sha256, generation),
          CONSTRAINT uq_rcda_task UNIQUE (task_id),
          CONSTRAINT uq_rcda_supersedes_task UNIQUE (supersedes_task_id),
          CONSTRAINT uq_rcda_exact_attempt
            UNIQUE (attempt_sha256, delivery_sha256, generation, task_id),
          CONSTRAINT fk_rcda_delivery FOREIGN KEY (delivery_sha256, quest_id)
            REFERENCES research_controller_deliveries (delivery_sha256, quest_id)
            DEFERRABLE INITIALLY DEFERRED,
          CONSTRAINT fk_rcda_task FOREIGN KEY (task_id)
            REFERENCES durable_tasks (task_id),
          CONSTRAINT fk_rcda_supersedes_task FOREIGN KEY (supersedes_task_id)
            REFERENCES durable_tasks (task_id)
        );
        CREATE INDEX ix_rcda_delivery_generation
          ON research_controller_delivery_attempts (delivery_sha256, generation);
        CREATE INDEX ix_rcda_quest_recorded
          ON research_controller_delivery_attempts (quest_id, recorded_at);

        CREATE TABLE research_controller_delivery_resolutions (
          resolution_sha256 varchar(64) PRIMARY KEY,
          delivery_sha256 varchar(64) NOT NULL,
          quest_id varchar(36) NOT NULL,
          latest_attempt_sha256 varchar(64) NOT NULL,
          exhausted_generation bigint NOT NULL,
          max_delivery_generation bigint NOT NULL,
          terminal_task_id varchar(96) NOT NULL,
          terminal_task_status varchar(16) NOT NULL,
          terminal_category varchar(40) NOT NULL,
          terminal_detail_sha256 varchar(64),
          terminal_result_sha256 varchar(64),
          tick_receipt_sha256 varchar(64),
          step_disposition varchar(32),
          signed_kernel_command_committed boolean,
          independent_observation_admission_committed boolean,
          controller_manifest_sha256 varchar(64) NOT NULL,
          disposition varchar(40) NOT NULL,
          dead_letter_reason varchar(40),
          resolution_json jsonb NOT NULL,
          resolved_at timestamptz NOT NULL,
          CONSTRAINT ck_rcdr_hashes CHECK (
            resolution_sha256 ~ '^[0-9a-f]{64}$' AND
            delivery_sha256 ~ '^[0-9a-f]{64}$' AND
            latest_attempt_sha256 ~ '^[0-9a-f]{64}$' AND
            controller_manifest_sha256 ~ '^[0-9a-f]{64}$' AND
            (terminal_detail_sha256 IS NULL OR
              terminal_detail_sha256 ~ '^[0-9a-f]{64}$') AND
            (terminal_result_sha256 IS NULL OR
              terminal_result_sha256 ~ '^[0-9a-f]{64}$') AND
            (tick_receipt_sha256 IS NULL OR tick_receipt_sha256 ~ '^[0-9a-f]{64}$')
          ),
          CONSTRAINT ck_rcdr_quest CHECK (quest_id ~ '^qst_[0-9a-f]{32}$'),
          CONSTRAINT ck_rcdr_generation CHECK (
            exhausted_generation >= 0 AND exhausted_generation <= 1024 AND
            max_delivery_generation >= 0 AND max_delivery_generation <= 1024 AND
            exhausted_generation <= max_delivery_generation
          ),
          CONSTRAINT ck_rcdr_disposition CHECK (
            disposition IN ('awaiting_authority','awaiting_external_result','blocked',
              'authoritative_source_committed','dead_letter')
          ),
          CONSTRAINT ck_rcdr_dead_letter_reason CHECK (
            (disposition = 'dead_letter' AND dead_letter_reason IN
              ('generation_limit_exhausted','invalid_succeeded_result','task_cancelled')) OR
            (disposition <> 'dead_letter' AND dead_letter_reason IS NULL)
          ),
          CONSTRAINT ck_rcdr_terminal_shape CHECK (
            (terminal_task_status IN ('failed','cancelled') AND
              terminal_detail_sha256 IS NOT NULL AND
              terminal_result_sha256 IS NULL) OR
            (terminal_task_status = 'succeeded' AND terminal_category = 'success' AND
              terminal_detail_sha256 IS NULL AND terminal_result_sha256 IS NOT NULL)
          ),
          CONSTRAINT ck_rcdr_verified_success CHECK (
            (disposition = 'dead_letter') OR
            (terminal_task_status = 'succeeded' AND tick_receipt_sha256 IS NOT NULL AND
              step_disposition IS NOT NULL AND signed_kernel_command_committed IS NOT NULL AND
              independent_observation_admission_committed IS NOT NULL)
          ),
          CONSTRAINT ck_rcdr_resolution_shape CHECK (
            (disposition = 'awaiting_authority' AND
              step_disposition = 'awaiting_authority' AND
              signed_kernel_command_committed = false AND
              independent_observation_admission_committed = false) OR
            (disposition = 'awaiting_external_result' AND
              step_disposition = 'awaiting_external_result' AND
              signed_kernel_command_committed = false AND
              independent_observation_admission_committed = false) OR
            (disposition = 'blocked' AND step_disposition = 'blocked' AND
              signed_kernel_command_committed = false AND
              independent_observation_admission_committed = false) OR
            (disposition = 'authoritative_source_committed' AND
              step_disposition = 'completed' AND
              signed_kernel_command_committed = true) OR
            (disposition = 'dead_letter')
          ),
          CONSTRAINT ck_rcdr_reason_shape CHECK (
            (dead_letter_reason = 'generation_limit_exhausted' AND
              exhausted_generation = max_delivery_generation AND
              ((terminal_task_status = 'failed' AND tick_receipt_sha256 IS NULL AND
                step_disposition IS NULL) OR
               (terminal_task_status = 'succeeded' AND tick_receipt_sha256 IS NOT NULL AND
                step_disposition = 'completed' AND signed_kernel_command_committed = false AND
                independent_observation_admission_committed = false))) OR
            (dead_letter_reason = 'invalid_succeeded_result' AND
              terminal_task_status = 'succeeded' AND tick_receipt_sha256 IS NULL AND
              step_disposition IS NULL AND signed_kernel_command_committed IS NULL AND
              independent_observation_admission_committed IS NULL) OR
            (dead_letter_reason = 'task_cancelled' AND
              terminal_task_status = 'cancelled' AND terminal_category = 'cancelled' AND
              tick_receipt_sha256 IS NULL AND step_disposition IS NULL AND
              signed_kernel_command_committed IS NULL AND
              independent_observation_admission_committed IS NULL) OR
            dead_letter_reason IS NULL
          ),
          CONSTRAINT ck_rcdr_json CHECK (
            jsonb_typeof(resolution_json) = 'object' AND
            resolution_json->>'schema_name' =
              'aletheia.research_controller_delivery_resolution' AND
            resolution_json->>'delivery_sha256' = delivery_sha256 AND
            resolution_json->>'quest_id' = quest_id AND
            resolution_json->>'latest_attempt_sha256' = latest_attempt_sha256 AND
            (resolution_json->>'exhausted_generation')::bigint = exhausted_generation AND
            (resolution_json->>'max_delivery_generation')::bigint =
              max_delivery_generation AND
            resolution_json->>'terminal_task_id' = terminal_task_id AND
            resolution_json->>'terminal_task_status' = terminal_task_status AND
            resolution_json->>'terminal_category' = terminal_category AND
            resolution_json->>'terminal_detail_sha256'
              IS NOT DISTINCT FROM terminal_detail_sha256 AND
            resolution_json->>'terminal_result_sha256'
              IS NOT DISTINCT FROM terminal_result_sha256 AND
            resolution_json->>'tick_receipt_sha256'
              IS NOT DISTINCT FROM tick_receipt_sha256 AND
            resolution_json->>'step_disposition' IS NOT DISTINCT FROM step_disposition AND
            (resolution_json->>'signed_kernel_command_committed')::boolean
              IS NOT DISTINCT FROM signed_kernel_command_committed AND
            (resolution_json->>'independent_observation_admission_committed')::boolean
              IS NOT DISTINCT FROM independent_observation_admission_committed AND
            resolution_json->>'controller_manifest_sha256' =
              controller_manifest_sha256 AND
            resolution_json->>'disposition' = disposition AND
            resolution_json->>'dead_letter_reason'
              IS NOT DISTINCT FROM dead_letter_reason AND
            (resolution_json->>'resolved_at')::timestamptz = resolved_at
          ),
          CONSTRAINT uq_rcdr_delivery UNIQUE (delivery_sha256),
          CONSTRAINT uq_rcdr_terminal_task UNIQUE (terminal_task_id),
          CONSTRAINT fk_rcdr_latest_attempt FOREIGN KEY
            (latest_attempt_sha256, delivery_sha256, exhausted_generation, terminal_task_id)
            REFERENCES research_controller_delivery_attempts
              (attempt_sha256, delivery_sha256, generation, task_id)
            DEFERRABLE INITIALLY DEFERRED,
          CONSTRAINT fk_rcdr_terminal_task FOREIGN KEY (terminal_task_id)
            REFERENCES durable_tasks (task_id)
        );
        CREATE INDEX ix_rcdr_quest_resolved
          ON research_controller_delivery_resolutions (quest_id, resolved_at);

        CREATE TABLE research_protocol_compilations (
          compilation_sha256 varchar(64) PRIMARY KEY,
          quest_id varchar(36) NOT NULL,
          action_sha256 varchar(64) NOT NULL,
          protocol_id varchar(128) NOT NULL,
          protocol_version bigint NOT NULL,
          revision_parent_version bigint,
          revision_parent_sha256 varchar(64),
          protocol_sha256 varchar(64) NOT NULL,
          request_sha256 varchar(64) NOT NULL,
          result_sha256 varchar(64) NOT NULL,
          receipt_sha256 varchar(64) NOT NULL,
          request_json jsonb NOT NULL,
          result_json jsonb NOT NULL,
          registered_at timestamptz NOT NULL,
          CONSTRAINT ck_rpc_hashes CHECK (
            compilation_sha256 ~ '^[0-9a-f]{64}$' AND
            action_sha256 ~ '^[0-9a-f]{64}$' AND
            protocol_sha256 ~ '^[0-9a-f]{64}$' AND
            request_sha256 ~ '^[0-9a-f]{64}$' AND
            result_sha256 ~ '^[0-9a-f]{64}$' AND
            receipt_sha256 ~ '^[0-9a-f]{64}$' AND
            (revision_parent_sha256 IS NULL OR
              revision_parent_sha256 ~ '^[0-9a-f]{64}$')
          ),
          CONSTRAINT ck_rpc_quest CHECK (quest_id ~ '^qst_[0-9a-f]{32}$'),
          CONSTRAINT ck_rpc_revision CHECK (
            protocol_version >= 1 AND
            ((protocol_version = 1 AND revision_parent_version IS NULL AND
              revision_parent_sha256 IS NULL) OR
             (protocol_version > 1 AND revision_parent_version = protocol_version - 1 AND
              revision_parent_sha256 IS NOT NULL))
          ),
          CONSTRAINT ck_rpc_json CHECK (
            jsonb_typeof(request_json) = 'object' AND
            jsonb_typeof(result_json) = 'object' AND
            request_json #>> '{protocol,protocol_id}' = protocol_id AND
            (request_json #>> '{protocol,version}')::bigint = protocol_version AND
            request_json #>> '{protocol,revision_parent_sha256}'
              IS NOT DISTINCT FROM revision_parent_sha256 AND
            result_json #>> '{receipt,protocol_sha256}' = protocol_sha256
          ),
          CONSTRAINT uq_rpc_action UNIQUE (action_sha256),
          CONSTRAINT uq_rpc_request UNIQUE (request_sha256),
          CONSTRAINT uq_rpc_result UNIQUE (result_sha256),
          CONSTRAINT uq_rpc_receipt UNIQUE (receipt_sha256),
          CONSTRAINT uq_rpc_protocol_version
            UNIQUE (quest_id, protocol_id, protocol_version),
          CONSTRAINT uq_rpc_protocol_identity
            UNIQUE (quest_id, protocol_id, protocol_version, protocol_sha256),
          CONSTRAINT fk_rpc_quest FOREIGN KEY (quest_id)
            REFERENCES research_quest_streams (quest_id),
          CONSTRAINT fk_rpc_action FOREIGN KEY (action_sha256)
            REFERENCES research_kernel_objects (object_sha256),
          CONSTRAINT fk_rpc_revision_parent FOREIGN KEY
            (quest_id, protocol_id, revision_parent_version, revision_parent_sha256)
            REFERENCES research_protocol_compilations
              (quest_id, protocol_id, protocol_version, protocol_sha256)
            DEFERRABLE INITIALLY DEFERRED
        );
        CREATE INDEX ix_rpc_quest_registered
          ON research_protocol_compilations (quest_id, registered_at);

        -- execution_id/attempt_id are preregistered identities.  They intentionally do not FK to
        -- execution_attempts because the PR-4 attempt is created only after this SEA commits;
        -- completed raw-run custody later resolves and re-verifies the exact pair.
        CREATE TABLE research_scientific_execution_authorizations (
          authorization_sha256 varchar(64) PRIMARY KEY,
          quest_id varchar(36) NOT NULL,
          scientific_slot_id varchar(36) NOT NULL,
          action_sha256 varchar(64) NOT NULL,
          execution_id varchar(36) NOT NULL,
          attempt_id varchar(36) NOT NULL,
          source_event_sequence bigint NOT NULL,
          source_event_sha256 varchar(64) NOT NULL,
          source_event_type varchar(64) NOT NULL,
          qualification_bundle_sha256 varchar(64) NOT NULL,
          qualification_grant_sha256 varchar(64) NOT NULL,
          authorization_json jsonb NOT NULL,
          authorized_at timestamptz NOT NULL,
          expires_at timestamptz NOT NULL,
          observation_admission_deadline timestamptz NOT NULL,
          registered_at timestamptz NOT NULL,
          CONSTRAINT ck_rsea_hashes CHECK (
            authorization_sha256 ~ '^[0-9a-f]{64}$' AND
            action_sha256 ~ '^[0-9a-f]{64}$' AND
            source_event_sha256 ~ '^[0-9a-f]{64}$' AND
            qualification_bundle_sha256 ~ '^[0-9a-f]{64}$' AND
            qualification_grant_sha256 ~ '^[0-9a-f]{64}$'
          ),
          CONSTRAINT ck_rsea_quest CHECK (quest_id ~ '^qst_[0-9a-f]{32}$'),
          CONSTRAINT ck_rsea_slot CHECK (scientific_slot_id ~ '^sos_[0-9a-f]{32}$'),
          CONSTRAINT ck_rsea_source_event CHECK (
            source_event_type = 'action_authorized' AND source_event_sequence >= 1
          ),
          CONSTRAINT ck_rsea_execution CHECK (
            execution_id ~ '^exe_[0-9a-f]{32}$' AND
            attempt_id ~ '^iat_[0-9a-f]{32}$'
          ),
          CONSTRAINT ck_rsea_time CHECK (
            authorized_at <= registered_at AND registered_at < expires_at AND
            expires_at < observation_admission_deadline
          ),
          CONSTRAINT ck_rsea_json CHECK (
            jsonb_typeof(authorization_json) = 'object' AND
            authorization_json->>'schema_name' =
              'aletheia.scientific_execution_authorization' AND
            authorization_json #>> '{message,scientific_slot_id}' = scientific_slot_id AND
            authorization_json #>> '{message,action_protocol_binding,action,quest_id}' = quest_id AND
            authorization_json #>> '{message,qualification_bundle,intent,execution_id}' =
              execution_id AND
            authorization_json #>>
              '{message,qualification_bundle,intent,infrastructure_attempt,infrastructure_attempt_id}' =
              attempt_id
          ),
          CONSTRAINT uq_rsea_slot UNIQUE (scientific_slot_id),
          CONSTRAINT uq_rsea_execution UNIQUE (execution_id),
          CONSTRAINT uq_rsea_attempt UNIQUE (attempt_id),
          CONSTRAINT uq_rsea_source_event UNIQUE (source_event_sha256),
          CONSTRAINT uq_rsea_bundle UNIQUE (qualification_bundle_sha256),
          CONSTRAINT uq_rsea_grant UNIQUE (qualification_grant_sha256),
          CONSTRAINT uq_rsea_exact_scope
            UNIQUE (authorization_sha256, quest_id, scientific_slot_id),
          CONSTRAINT fk_rsea_action FOREIGN KEY (action_sha256)
            REFERENCES research_kernel_objects (object_sha256),
          CONSTRAINT fk_rsea_source_event FOREIGN KEY
            (quest_id, source_event_sequence, source_event_sha256, source_event_type)
            REFERENCES research_kernel_events
              (quest_id, sequence, event_sha256, event_type)
            DEFERRABLE INITIALLY DEFERRED
        );
        CREATE INDEX ix_rsea_quest_registered
          ON research_scientific_execution_authorizations (quest_id, registered_at);

        CREATE TABLE research_observation_issuance_challenges (
          challenge_sha256 varchar(64) PRIMARY KEY,
          purpose varchar(16) NOT NULL,
          quest_id varchar(36) NOT NULL,
          scientific_slot_id varchar(36) NOT NULL,
          authorization_sha256 varchar(64) NOT NULL,
          nonce_sha256 varchar(64) NOT NULL,
          row_scope varchar(128) NOT NULL,
          raw_run_sha256 varchar(64),
          committed_validation_receipt_sha256 varchar(64),
          validation_receipt_sha256 varchar(64),
          database_authority_policy_sha256 varchar(64) NOT NULL,
          issued_by_principal_id varchar(128) NOT NULL,
          issuance_key_id varchar(64) NOT NULL,
          challenge_json jsonb NOT NULL,
          issued_at timestamptz NOT NULL,
          expires_at timestamptz NOT NULL,
          observation_admission_deadline timestamptz NOT NULL,
          recorded_at timestamptz NOT NULL,
          CONSTRAINT ck_roic_hashes CHECK (
            challenge_sha256 ~ '^[0-9a-f]{64}$' AND
            authorization_sha256 ~ '^[0-9a-f]{64}$' AND
            nonce_sha256 ~ '^[0-9a-f]{64}$' AND
            database_authority_policy_sha256 ~ '^[0-9a-f]{64}$' AND
            issuance_key_id ~ '^[0-9a-f]{64}$' AND
            (raw_run_sha256 IS NULL OR raw_run_sha256 ~ '^[0-9a-f]{64}$') AND
            (committed_validation_receipt_sha256 IS NULL OR
              committed_validation_receipt_sha256 ~ '^[0-9a-f]{64}$') AND
            (validation_receipt_sha256 IS NULL OR
              validation_receipt_sha256 ~ '^[0-9a-f]{64}$')
          ),
          CONSTRAINT ck_roic_quest CHECK (quest_id ~ '^qst_[0-9a-f]{32}$'),
          CONSTRAINT ck_roic_slot CHECK (scientific_slot_id ~ '^sos_[0-9a-f]{32}$'),
          CONSTRAINT ck_roic_purpose CHECK (purpose IN ('validation','admission')),
          CONSTRAINT ck_roic_source_shape CHECK (
            (purpose = 'validation' AND raw_run_sha256 IS NOT NULL AND
              committed_validation_receipt_sha256 IS NULL AND
              validation_receipt_sha256 IS NULL) OR
            (purpose = 'admission' AND raw_run_sha256 IS NULL AND
              committed_validation_receipt_sha256 IS NOT NULL AND
              validation_receipt_sha256 IS NOT NULL)
          ),
          CONSTRAINT ck_roic_time CHECK (
            issued_at <= recorded_at AND recorded_at < expires_at AND
            expires_at <= observation_admission_deadline
          ),
          CONSTRAINT ck_roic_json CHECK (
            jsonb_typeof(challenge_json) = 'object' AND
            challenge_json #>> '{message,scientific_slot_id}' = scientific_slot_id AND
            challenge_json #>> '{message,nonce_sha256}' = nonce_sha256 AND
            challenge_json #>> '{message,row_scope}' = row_scope
          ),
          CONSTRAINT uq_roic_nonce UNIQUE (nonce_sha256),
          CONSTRAINT uq_roic_validation_source UNIQUE
            (challenge_sha256, quest_id, scientific_slot_id, authorization_sha256, raw_run_sha256),
          CONSTRAINT uq_roic_admission_source UNIQUE
            (challenge_sha256, quest_id, scientific_slot_id, authorization_sha256,
             committed_validation_receipt_sha256, validation_receipt_sha256),
          CONSTRAINT fk_roic_authorization FOREIGN KEY
            (authorization_sha256, quest_id, scientific_slot_id)
            REFERENCES research_scientific_execution_authorizations
              (authorization_sha256, quest_id, scientific_slot_id)
            DEFERRABLE INITIALLY DEFERRED
        );
        CREATE INDEX ix_roic_expiry
          ON research_observation_issuance_challenges (purpose, expires_at);

        CREATE TABLE research_observation_validation_receipts (
          committed_receipt_sha256 varchar(64) PRIMARY KEY,
          validation_receipt_sha256 varchar(64) NOT NULL,
          quest_id varchar(36) NOT NULL,
          scientific_slot_id varchar(36) NOT NULL,
          authorization_sha256 varchar(64) NOT NULL,
          qualification_admission_sha256 varchar(64) NOT NULL,
          raw_run_sha256 varchar(64) NOT NULL,
          issuance_challenge_sha256 varchar(64) NOT NULL,
          validation_campaign_sha256 varchar(64),
          disposition varchar(32) NOT NULL,
          outcome varchar(16),
          scientific_observation_sha256 varchar(64),
          committed_receipt_json jsonb NOT NULL,
          validated_at timestamptz NOT NULL,
          registered_at timestamptz NOT NULL,
          committed_at timestamptz NOT NULL,
          CONSTRAINT ck_rovr_hashes CHECK (
            committed_receipt_sha256 ~ '^[0-9a-f]{64}$' AND
            validation_receipt_sha256 ~ '^[0-9a-f]{64}$' AND
            authorization_sha256 ~ '^[0-9a-f]{64}$' AND
            qualification_admission_sha256 ~ '^[0-9a-f]{64}$' AND
            raw_run_sha256 ~ '^[0-9a-f]{64}$' AND
            issuance_challenge_sha256 ~ '^[0-9a-f]{64}$' AND
            (validation_campaign_sha256 IS NULL OR
              validation_campaign_sha256 ~ '^[0-9a-f]{64}$') AND
            (scientific_observation_sha256 IS NULL OR
              scientific_observation_sha256 ~ '^[0-9a-f]{64}$')
          ),
          CONSTRAINT ck_rovr_quest CHECK (quest_id ~ '^qst_[0-9a-f]{32}$'),
          CONSTRAINT ck_rovr_slot CHECK (scientific_slot_id ~ '^sos_[0-9a-f]{32}$'),
          CONSTRAINT ck_rovr_disposition CHECK (
            disposition IN ('validated_confirmation','rejected_scientific','blocked_execution')
          ),
          CONSTRAINT ck_rovr_outcome CHECK (
            (disposition = 'validated_confirmation' AND
              outcome IN ('positive','negative','inconclusive') AND
              scientific_observation_sha256 IS NOT NULL) OR
            (disposition <> 'validated_confirmation' AND outcome IS NULL AND
              scientific_observation_sha256 IS NULL)
          ),
          CONSTRAINT ck_rovr_time CHECK (
            validated_at <= registered_at AND registered_at <= committed_at
          ),
          CONSTRAINT ck_rovr_json CHECK (
            jsonb_typeof(committed_receipt_json) = 'object' AND
            committed_receipt_json->>'schema_name' =
              'aletheia.committed_observation_validation_receipt' AND
            committed_receipt_json #>> '{message,validation_receipt_sha256}' =
              validation_receipt_sha256 AND
            committed_receipt_json #>> '{message,issuance_challenge_sha256}' =
              issuance_challenge_sha256
          ),
          CONSTRAINT uq_rovr_receipt UNIQUE (validation_receipt_sha256),
          CONSTRAINT uq_rovr_slot UNIQUE (scientific_slot_id),
          CONSTRAINT uq_rovr_raw_run UNIQUE (raw_run_sha256),
          CONSTRAINT uq_rovr_challenge UNIQUE (issuance_challenge_sha256),
          CONSTRAINT uq_rovr_observation UNIQUE (scientific_observation_sha256),
          CONSTRAINT uq_rovr_exact_receipt UNIQUE
            (committed_receipt_sha256, validation_receipt_sha256, quest_id,
             scientific_slot_id, authorization_sha256),
          CONSTRAINT uq_rovr_exact_observation UNIQUE
            (committed_receipt_sha256, validation_receipt_sha256, quest_id,
             scientific_slot_id, authorization_sha256, scientific_observation_sha256),
          CONSTRAINT fk_rovr_authorization FOREIGN KEY
            (authorization_sha256, quest_id, scientific_slot_id)
            REFERENCES research_scientific_execution_authorizations
              (authorization_sha256, quest_id, scientific_slot_id)
            DEFERRABLE INITIALLY DEFERRED,
          CONSTRAINT fk_rovr_qualification FOREIGN KEY (qualification_admission_sha256)
            REFERENCES execution_qualification_admissions (admission_sha256),
          CONSTRAINT fk_rovr_exact_challenge FOREIGN KEY
            (issuance_challenge_sha256, quest_id, scientific_slot_id,
             authorization_sha256, raw_run_sha256)
            REFERENCES research_observation_issuance_challenges
              (challenge_sha256, quest_id, scientific_slot_id,
               authorization_sha256, raw_run_sha256)
            DEFERRABLE INITIALLY DEFERRED
        );
        CREATE INDEX ix_rovr_quest_committed
          ON research_observation_validation_receipts (quest_id, committed_at);

        ALTER TABLE research_observation_issuance_challenges
          ADD CONSTRAINT fk_roic_admission_validation FOREIGN KEY
            (committed_validation_receipt_sha256, validation_receipt_sha256,
             quest_id, scientific_slot_id, authorization_sha256)
            REFERENCES research_observation_validation_receipts
              (committed_receipt_sha256, validation_receipt_sha256,
               quest_id, scientific_slot_id, authorization_sha256)
            DEFERRABLE INITIALLY DEFERRED;

        CREATE TABLE research_observation_admissions (
          committed_admission_sha256 varchar(64) PRIMARY KEY,
          decision_sha256 varchar(64) NOT NULL,
          quest_id varchar(36) NOT NULL,
          scientific_slot_id varchar(36) NOT NULL,
          authorization_sha256 varchar(64) NOT NULL,
          committed_validation_receipt_sha256 varchar(64) NOT NULL,
          validation_receipt_sha256 varchar(64) NOT NULL,
          issuance_challenge_sha256 varchar(64) NOT NULL,
          disposition varchar(16) NOT NULL,
          admitted_observation_sha256 varchar(64) NOT NULL,
          admission_json jsonb NOT NULL,
          registered_at timestamptz NOT NULL,
          committed_at timestamptz NOT NULL,
          incorporated_event_sequence bigint,
          incorporated_event_sha256 varchar(64),
          incorporated_event_type varchar(64),
          CONSTRAINT ck_roa_hashes CHECK (
            committed_admission_sha256 ~ '^[0-9a-f]{64}$' AND
            decision_sha256 ~ '^[0-9a-f]{64}$' AND
            authorization_sha256 ~ '^[0-9a-f]{64}$' AND
            committed_validation_receipt_sha256 ~ '^[0-9a-f]{64}$' AND
            validation_receipt_sha256 ~ '^[0-9a-f]{64}$' AND
            issuance_challenge_sha256 ~ '^[0-9a-f]{64}$' AND
            (admitted_observation_sha256 IS NULL OR
              admitted_observation_sha256 ~ '^[0-9a-f]{64}$') AND
            (incorporated_event_sha256 IS NULL OR
              incorporated_event_sha256 ~ '^[0-9a-f]{64}$')
          ),
          CONSTRAINT ck_roa_quest CHECK (quest_id ~ '^qst_[0-9a-f]{32}$'),
          CONSTRAINT ck_roa_slot CHECK (scientific_slot_id ~ '^sos_[0-9a-f]{32}$'),
          CONSTRAINT ck_roa_disposition CHECK (disposition = 'admitted'),
          CONSTRAINT ck_roa_incorporation CHECK (
            incorporated_event_sequence >= 1 AND incorporated_event_sha256 IS NOT NULL AND
            incorporated_event_type = 'observation_incorporated'
          ),
          CONSTRAINT ck_roa_time CHECK (registered_at <= committed_at),
          CONSTRAINT ck_roa_json CHECK (
            jsonb_typeof(admission_json) = 'object' AND
            admission_json->>'schema_name' = 'aletheia.committed_observation_admission' AND
            admission_json #>> '{message,decision_sha256}' = decision_sha256 AND
            admission_json #>> '{message,committed_validation_receipt_sha256}' =
              committed_validation_receipt_sha256 AND
            admission_json #>> '{message,exact_registered_validation_receipt_sha256}' =
              validation_receipt_sha256 AND
            admission_json #>> '{message,issuance_challenge_sha256}' =
              issuance_challenge_sha256
          ),
          CONSTRAINT uq_roa_phase1_slot UNIQUE (scientific_slot_id),
          CONSTRAINT uq_roa_decision UNIQUE (decision_sha256),
          CONSTRAINT uq_roa_validation UNIQUE (committed_validation_receipt_sha256),
          CONSTRAINT uq_roa_challenge UNIQUE (issuance_challenge_sha256),
          CONSTRAINT uq_roa_observation UNIQUE (admitted_observation_sha256),
          CONSTRAINT uq_roa_event UNIQUE (incorporated_event_sha256),
          CONSTRAINT uq_roa_exact_observation UNIQUE
            (committed_admission_sha256, quest_id, scientific_slot_id,
             admitted_observation_sha256),
          CONSTRAINT fk_roa_authorization FOREIGN KEY
            (authorization_sha256, quest_id, scientific_slot_id)
            REFERENCES research_scientific_execution_authorizations
              (authorization_sha256, quest_id, scientific_slot_id)
            DEFERRABLE INITIALLY DEFERRED,
          CONSTRAINT fk_roa_exact_validation FOREIGN KEY
            (committed_validation_receipt_sha256, validation_receipt_sha256,
             quest_id, scientific_slot_id, authorization_sha256)
            REFERENCES research_observation_validation_receipts
              (committed_receipt_sha256, validation_receipt_sha256,
               quest_id, scientific_slot_id, authorization_sha256)
            DEFERRABLE INITIALLY DEFERRED,
          CONSTRAINT fk_roa_exact_observation FOREIGN KEY
            (committed_validation_receipt_sha256, validation_receipt_sha256,
             quest_id, scientific_slot_id, authorization_sha256,
             admitted_observation_sha256)
            REFERENCES research_observation_validation_receipts
              (committed_receipt_sha256, validation_receipt_sha256,
               quest_id, scientific_slot_id, authorization_sha256,
               scientific_observation_sha256)
            DEFERRABLE INITIALLY DEFERRED,
          CONSTRAINT fk_roa_exact_challenge FOREIGN KEY
            (issuance_challenge_sha256, quest_id, scientific_slot_id,
             authorization_sha256, committed_validation_receipt_sha256,
             validation_receipt_sha256)
            REFERENCES research_observation_issuance_challenges
              (challenge_sha256, quest_id, scientific_slot_id,
               authorization_sha256, committed_validation_receipt_sha256,
               validation_receipt_sha256)
            DEFERRABLE INITIALLY DEFERRED,
          CONSTRAINT fk_roa_incorporated_event FOREIGN KEY
            (quest_id, incorporated_event_sequence, incorporated_event_sha256,
             incorporated_event_type)
            REFERENCES research_kernel_events
              (quest_id, sequence, event_sha256, event_type)
            DEFERRABLE INITIALLY DEFERRED
        );
        CREATE INDEX ix_roa_quest_committed
          ON research_observation_admissions (quest_id, committed_at);

        CREATE TABLE research_continuation_receipts (
          receipt_sha256 varchar(64) PRIMARY KEY,
          quest_id varchar(36) NOT NULL,
          action_sha256 varchar(64) NOT NULL,
          scientific_slot_id varchar(36) NOT NULL,
          world_model_snapshot_sha256 varchar(64) NOT NULL,
          observation_projection_sha256 varchar(64) NOT NULL,
          scientific_observation_sha256 varchar(64) NOT NULL,
          committed_admission_sha256 varchar(64) NOT NULL,
          disposition varchar(48) NOT NULL,
          receipt_json jsonb NOT NULL,
          recorded_at timestamptz NOT NULL,
          CONSTRAINT ck_rcr_hashes CHECK (
            receipt_sha256 ~ '^[0-9a-f]{64}$' AND
            action_sha256 ~ '^[0-9a-f]{64}$' AND
            world_model_snapshot_sha256 ~ '^[0-9a-f]{64}$' AND
            observation_projection_sha256 ~ '^[0-9a-f]{64}$' AND
            scientific_observation_sha256 ~ '^[0-9a-f]{64}$' AND
            committed_admission_sha256 ~ '^[0-9a-f]{64}$'
          ),
          CONSTRAINT ck_rcr_quest CHECK (quest_id ~ '^qst_[0-9a-f]{32}$'),
          CONSTRAINT ck_rcr_slot CHECK (scientific_slot_id ~ '^sos_[0-9a-f]{32}$'),
          CONSTRAINT ck_rcr_disposition CHECK (
            disposition IN ('ready','redesign_observable','hypothesis_set_fork_required')
          ),
          CONSTRAINT ck_rcr_json CHECK (
            jsonb_typeof(receipt_json) = 'object' AND
            receipt_json->>'schema_name' = 'aletheia.graph_scoped_continuation_receipt' AND
            receipt_json->>'scientific_slot_id' = scientific_slot_id AND
            receipt_json->>'world_model_snapshot_sha256' = world_model_snapshot_sha256 AND
            receipt_json->>'observation_projection_sha256' = observation_projection_sha256 AND
            receipt_json->>'disposition' = disposition
          ),
          CONSTRAINT uq_rcr_slot UNIQUE (scientific_slot_id),
          CONSTRAINT uq_rcr_action UNIQUE (action_sha256),
          CONSTRAINT uq_rcr_projection UNIQUE (observation_projection_sha256),
          CONSTRAINT fk_rcr_action FOREIGN KEY (action_sha256)
            REFERENCES research_kernel_objects (object_sha256),
          CONSTRAINT fk_rcr_exact_admission FOREIGN KEY
            (committed_admission_sha256, quest_id, scientific_slot_id,
             scientific_observation_sha256)
            REFERENCES research_observation_admissions
              (committed_admission_sha256, quest_id, scientific_slot_id,
               admitted_observation_sha256)
            DEFERRABLE INITIALLY DEFERRED
        );
        CREATE INDEX ix_rcr_quest_recorded
          ON research_continuation_receipts (quest_id, recorded_at);
        """
    )

    op.execute(
        """
        CREATE FUNCTION aletheia_controller_delivery_initial_attempt_complete()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1
              FROM research_controller_delivery_attempts attempt
             WHERE attempt.delivery_sha256 = NEW.delivery_sha256
               AND attempt.quest_id = NEW.quest_id
               AND attempt.generation = 0
               AND attempt.kind = 'initial'
               AND attempt.task_id = NEW.task_id
          ) THEN
            RAISE EXCEPTION 'controller delivery lacks its exact initial task generation'
              USING ERRCODE = '23514';
          END IF;
          RETURN NULL;
        END;
        $$;

        CREATE FUNCTION aletheia_controller_delivery_attempt_chain_complete()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          delivery_row research_controller_deliveries%ROWTYPE;
          registration_manifest_sha256 varchar(64);
          predecessor_row research_controller_delivery_attempts%ROWTYPE;
          predecessor_task_row durable_tasks%ROWTYPE;
          task_row durable_tasks%ROWTYPE;
        BEGIN
          SELECT * INTO delivery_row
            FROM research_controller_deliveries
           WHERE delivery_sha256 = NEW.delivery_sha256
             AND quest_id = NEW.quest_id
             FOR UPDATE;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'controller attempt lacks its exact delivery'
              USING ERRCODE = '23514';
          END IF;
          IF EXISTS (
            SELECT 1
              FROM research_controller_delivery_resolutions resolution
             WHERE resolution.delivery_sha256 = NEW.delivery_sha256
          ) THEN
            RAISE EXCEPTION 'resolved controller delivery cannot append another attempt'
              USING ERRCODE = '23514';
          END IF;
          SELECT controller_manifest_sha256 INTO registration_manifest_sha256
            FROM research_controller_registrations
           WHERE registration_sha256 = delivery_row.registration_sha256;
          IF NOT FOUND OR
             registration_manifest_sha256 IS DISTINCT FROM NEW.controller_manifest_sha256 THEN
            RAISE EXCEPTION 'controller attempt differs from its registered manifest'
              USING ERRCODE = '23514';
          END IF;
          SELECT * INTO task_row FROM durable_tasks WHERE task_id = NEW.task_id;
          IF NOT FOUND OR task_row.request_sha256 IS DISTINCT FROM NEW.task_request_sha256 OR
             task_row.inputs_json->>'controller_manifest_sha256'
               IS DISTINCT FROM NEW.controller_manifest_sha256 OR
             task_row.inputs_json->>'wakeup_sha256' IS DISTINCT FROM NEW.wakeup_sha256 OR
             (task_row.inputs_json->>'delivery_generation')::bigint
               IS DISTINCT FROM NEW.generation THEN
            RAISE EXCEPTION 'controller attempt differs from its deterministic task envelope'
              USING ERRCODE = '23514';
          END IF;
          IF NEW.generation = 0 THEN
            IF NEW.task_id IS DISTINCT FROM delivery_row.task_id OR
               task_row.inputs_json->>'delivery_sha256' IS NOT NULL OR
               task_row.inputs_json->>'supersedes_task_id' IS NOT NULL THEN
              RAISE EXCEPTION 'initial controller attempt differs from its delivery task'
                USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
          END IF;
          SELECT * INTO predecessor_row
            FROM research_controller_delivery_attempts
           WHERE delivery_sha256 = NEW.delivery_sha256
             AND generation = NEW.generation - 1
             AND task_id = NEW.supersedes_task_id;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'controller delivery attempt lacks its exact predecessor'
              USING ERRCODE = '23514';
          END IF;
          SELECT * INTO predecessor_task_row
            FROM durable_tasks
           WHERE task_id = NEW.supersedes_task_id;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'controller delivery attempt predecessor task disappeared'
              USING ERRCODE = '23514';
          END IF;
          IF predecessor_row.controller_manifest_sha256
               IS DISTINCT FROM NEW.controller_manifest_sha256 OR
             predecessor_row.wakeup_sha256 IS DISTINCT FROM NEW.wakeup_sha256 OR
             predecessor_task_row.status IS DISTINCT FROM NEW.predecessor_status OR
             predecessor_task_row.terminal_category
               IS DISTINCT FROM NEW.predecessor_terminal_category OR
             predecessor_task_row.terminal_detail_sha256
               IS DISTINCT FROM NEW.predecessor_terminal_detail_sha256 OR
             predecessor_task_row.result_sha256
               IS DISTINCT FROM NEW.predecessor_result_sha256 OR
             task_row.inputs_json->>'delivery_sha256'
               IS DISTINCT FROM NEW.delivery_sha256 OR
             task_row.inputs_json->>'supersedes_task_id'
               IS DISTINCT FROM NEW.supersedes_task_id THEN
            RAISE EXCEPTION 'controller delivery attempt chain is not contiguous and exact'
              USING ERRCODE = '23514';
          END IF;
          IF NEW.kind = 'completed_successor' AND (
               predecessor_task_row.result_json->>'schema_name'
                 IS DISTINCT FROM 'aletheia.research_controller_tick_receipt' OR
               predecessor_task_row.result_json->>'wakeup_sha256'
                 IS DISTINCT FROM NEW.wakeup_sha256 OR
               predecessor_task_row.result_json #>> '{step_receipt,disposition}'
                 IS DISTINCT FROM 'completed' OR
               (predecessor_task_row.result_json #>>
                 '{step_receipt,signed_kernel_command_committed}')::boolean
                 IS DISTINCT FROM false OR
               (predecessor_task_row.result_json #>>
                 '{step_receipt,independent_observation_admission_committed}')::boolean
                 IS DISTINCT FROM false OR
               predecessor_task_row.result_artifact_id IS DISTINCT FROM
                 'research-controller-receipt:' || NEW.predecessor_tick_receipt_sha256
          ) THEN
            RAISE EXCEPTION
              'completed controller successor lacks its exact internal tick receipt'
              USING ERRCODE = '23514';
          END IF;
          RETURN NULL;
        END;
        $$;

        CREATE FUNCTION aletheia_controller_delivery_resolution_exact()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          delivery_row research_controller_deliveries%ROWTYPE;
          attempt_row research_controller_delivery_attempts%ROWTYPE;
          task_row durable_tasks%ROWTYPE;
        BEGIN
          SELECT * INTO delivery_row
            FROM research_controller_deliveries
           WHERE delivery_sha256 = NEW.delivery_sha256
             AND quest_id = NEW.quest_id
             FOR UPDATE;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'controller resolution lacks its exact locked delivery'
              USING ERRCODE = '23514';
          END IF;
          SELECT * INTO attempt_row
            FROM research_controller_delivery_attempts
           WHERE attempt_sha256 = NEW.latest_attempt_sha256
             AND delivery_sha256 = NEW.delivery_sha256
             AND generation = NEW.exhausted_generation
             AND task_id = NEW.terminal_task_id;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'controller resolution lacks its exact delivery attempt'
              USING ERRCODE = '23514';
          END IF;
          IF EXISTS (
            SELECT 1
              FROM research_controller_delivery_attempts later_attempt
             WHERE later_attempt.delivery_sha256 = NEW.delivery_sha256
               AND later_attempt.generation > NEW.exhausted_generation
          ) THEN
            RAISE EXCEPTION 'controller resolution does not target the latest delivery attempt'
              USING ERRCODE = '23514';
          END IF;
          SELECT * INTO task_row
            FROM durable_tasks
           WHERE task_id = NEW.terminal_task_id;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'controller resolution terminal task disappeared'
              USING ERRCODE = '23514';
          END IF;
          IF attempt_row.quest_id IS DISTINCT FROM NEW.quest_id OR
             attempt_row.controller_manifest_sha256
               IS DISTINCT FROM NEW.controller_manifest_sha256 OR
             task_row.status IS DISTINCT FROM NEW.terminal_task_status OR
             task_row.terminal_category IS DISTINCT FROM NEW.terminal_category OR
             task_row.terminal_detail_sha256
               IS DISTINCT FROM NEW.terminal_detail_sha256 OR
             task_row.result_sha256 IS DISTINCT FROM NEW.terminal_result_sha256 THEN
            RAISE EXCEPTION 'controller resolution differs from its exact terminal task'
              USING ERRCODE = '23514';
          END IF;
          RETURN NULL;
        END;
        $$;

        CREATE CONSTRAINT TRIGGER trg_rc_delivery_initial_attempt_complete
        AFTER INSERT ON research_controller_deliveries
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION aletheia_controller_delivery_initial_attempt_complete();

        CREATE CONSTRAINT TRIGGER trg_rc_delivery_attempt_chain_complete
        AFTER INSERT ON research_controller_delivery_attempts
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION aletheia_controller_delivery_attempt_chain_complete();

        CREATE CONSTRAINT TRIGGER trg_rc_delivery_resolution_exact
        AFTER INSERT ON research_controller_delivery_resolutions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION aletheia_controller_delivery_resolution_exact();

        CREATE FUNCTION aletheia_observation_reject_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '55000';
        END;
        $$
        """
    )
    for table in _APPEND_ONLY_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION aletheia_observation_reject_mutation()
            """
        )
    op.execute(
        """
        CREATE FUNCTION aletheia_observation_incorporation_complete()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          event_row research_kernel_events%ROWTYPE;
          admission_row research_observation_admissions%ROWTYPE;
        BEGIN
          IF TG_TABLE_NAME = 'research_observation_admissions' THEN
            IF NEW.disposition <> 'admitted' THEN
              RETURN NULL;
            END IF;
            SELECT * INTO event_row
              FROM research_kernel_events
             WHERE quest_id = NEW.quest_id
               AND sequence = NEW.incorporated_event_sequence
               AND event_sha256 = NEW.incorporated_event_sha256
               AND event_type = 'observation_incorporated';
            IF NOT FOUND OR
               event_row.event_json #>> '{payload,scientific_slot_id}'
                 IS DISTINCT FROM NEW.scientific_slot_id OR
               event_row.event_json #>> '{payload,committed_admission_sha256}'
                 IS DISTINCT FROM NEW.committed_admission_sha256 OR
               event_row.event_json #>> '{payload,scientific_observation_sha256}'
                 IS DISTINCT FROM NEW.admitted_observation_sha256 OR
               event_row.event_json #>> '{payload,action_id}' IS DISTINCT FROM
                 NEW.admission_json #>>
                   '{message,decision,message,committed_validation_receipt,message,receipt,message,raw_run,scientific_authorization,message,action_protocol_binding,action,action_id}' OR
               event_row.event_json #>> '{payload,branch_id}' IS DISTINCT FROM
                 NEW.admission_json #>>
                   '{message,decision,message,committed_validation_receipt,message,receipt,message,raw_run,scientific_authorization,message,action_protocol_binding,compilation_request,protocol,graph_scope,branch_id}' OR
               event_row.event_json #>> '{payload,outcome}' IS DISTINCT FROM
                 NEW.admission_json #>>
                   '{message,decision,message,committed_validation_receipt,message,receipt,message,outcome}' OR
               event_row.event_json #>> '{payload,source_world_model_sha256}' IS DISTINCT FROM
                 NEW.admission_json #>>
                   '{message,decision,message,committed_validation_receipt,message,receipt,message,raw_run,scientific_authorization,message,action_protocol_binding,compilation_request,protocol,world_model,world_model_sha256}' THEN
              RAISE EXCEPTION
                'admission lacks its exact observation_incorporated event payload'
                USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
          END IF;

          IF NEW.event_type <> 'observation_incorporated' THEN
            RETURN NULL;
          END IF;
          SELECT * INTO admission_row
            FROM research_observation_admissions
           WHERE incorporated_event_sha256 = NEW.event_sha256;
          IF NOT FOUND OR admission_row.disposition <> 'admitted' OR
             admission_row.quest_id IS DISTINCT FROM NEW.quest_id OR
             admission_row.incorporated_event_sequence IS DISTINCT FROM NEW.sequence OR
             NEW.event_json #>> '{payload,scientific_slot_id}'
               IS DISTINCT FROM admission_row.scientific_slot_id OR
             NEW.event_json #>> '{payload,committed_admission_sha256}'
               IS DISTINCT FROM admission_row.committed_admission_sha256 OR
             NEW.event_json #>> '{payload,scientific_observation_sha256}'
               IS DISTINCT FROM admission_row.admitted_observation_sha256 OR
             NEW.event_json #>> '{payload,action_id}' IS DISTINCT FROM
               admission_row.admission_json #>>
                 '{message,decision,message,committed_validation_receipt,message,receipt,message,raw_run,scientific_authorization,message,action_protocol_binding,action,action_id}' OR
             NEW.event_json #>> '{payload,branch_id}' IS DISTINCT FROM
               admission_row.admission_json #>>
                 '{message,decision,message,committed_validation_receipt,message,receipt,message,raw_run,scientific_authorization,message,action_protocol_binding,compilation_request,protocol,graph_scope,branch_id}' OR
             NEW.event_json #>> '{payload,outcome}' IS DISTINCT FROM
               admission_row.admission_json #>>
                 '{message,decision,message,committed_validation_receipt,message,receipt,message,outcome}' OR
             NEW.event_json #>> '{payload,source_world_model_sha256}' IS DISTINCT FROM
               admission_row.admission_json #>>
                 '{message,decision,message,committed_validation_receipt,message,receipt,message,raw_run,scientific_authorization,message,action_protocol_binding,compilation_request,protocol,world_model,world_model_sha256}' THEN
            RAISE EXCEPTION
              'observation_incorporated event lacks its exact admission row'
              USING ERRCODE = '23514';
          END IF;
          RETURN NULL;
        END;
        $$;

        CREATE CONSTRAINT TRIGGER trg_roa_incorporation_complete
        AFTER INSERT OR UPDATE ON research_observation_admissions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION aletheia_observation_incorporation_complete();

        CREATE CONSTRAINT TRIGGER trg_rke_observation_incorporation_complete
        AFTER INSERT OR UPDATE ON research_kernel_events
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        WHEN (NEW.event_type = 'observation_incorporated')
        EXECUTE FUNCTION aletheia_observation_incorporation_complete();
        """
    )


def downgrade() -> None:
    # An old kernel cannot replay the new typed event.  Refuse rather than silently orphaning it.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM research_kernel_events
            WHERE event_type = 'observation_incorporated'
          ) THEN
            RAISE EXCEPTION
              'cannot downgrade 0027 while observation_incorporated events exist'
              USING ERRCODE = '55000';
          END IF;
        END;
        $$
        """
    )
    op.execute("DROP TRIGGER trg_rke_observation_incorporation_complete ON research_kernel_events")
    op.drop_constraint(
        "fk_roic_admission_validation",
        "research_observation_issuance_challenges",
        type_="foreignkey",
    )
    for table in reversed(_APPEND_ONLY_TABLES):
        op.drop_table(table)
    op.execute("DROP FUNCTION aletheia_controller_delivery_resolution_exact()")
    op.execute("DROP FUNCTION aletheia_controller_delivery_attempt_chain_complete()")
    op.execute("DROP FUNCTION aletheia_controller_delivery_initial_attempt_complete()")
    op.execute("DROP FUNCTION aletheia_observation_incorporation_complete()")
    op.execute("DROP FUNCTION aletheia_observation_reject_mutation()")

    op.drop_constraint(
        "uq_exec_qto_exact_controller_source",
        "execution_qualification_terminal_outbox",
        type_="unique",
    )
    op.drop_constraint(
        "uq_rko_exact_controller_source",
        "research_kernel_outbox",
        type_="unique",
    )
    op.drop_constraint(
        "uq_rke_scoped_typed_event",
        "research_kernel_events",
        type_="unique",
    )
    op.drop_constraint(
        "ck_research_kernel_events_type",
        "research_kernel_events",
        type_="check",
    )
    op.create_check_constraint(
        "ck_research_kernel_events_type",
        "research_kernel_events",
        f"event_type IN ({_V1_EVENT_TYPES})",
    )
