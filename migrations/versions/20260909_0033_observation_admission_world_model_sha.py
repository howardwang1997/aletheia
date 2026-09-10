"""Promote the admission's source world-model digest to a real column.

Revision ID: 20260909_0033
Revises: 20260903_0032
Create Date: 2026-09-09

The incorporation-complete trigger cross-checked the Kernel event payload's
``source_world_model_sha256`` against an ``admission_json`` navigation to
``...protocol.world_model.world_model_sha256``.  That key is a computed
property (``canonical_sha256`` of the snapshot), never a serialized field, so
pydantic never emitted it and the JSON side of the comparison was always NULL
while the payload side is a required 64-hex string.  Every admission commit
therefore raised ``observation_incorporated event lacks its exact admission
row`` at COMMIT; the first live ARL-1 admission (generation 20260908o, release
52095d4, 2026-09-08T13:49:44Z) failed closed on exactly this.  The table has
never accepted a row in any deployment, so the NOT NULL column needs no
default.  Both trigger branches now compare the column exactly like
``admitted_observation_sha256``; ``ObservationAdmissionWrite.from_contract``
derives it from the same property the event payload uses.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260909_0033"
down_revision: str | None = "20260903_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BROKEN_PATH = (
    "'{message,decision,message,committed_validation_receipt,message,receipt,"
    "message,raw_run,scientific_authorization,message,action_protocol_binding,"
    "compilation_request,protocol,world_model,world_model_sha256}'"
)

_FIXED_FUNCTION = """
        CREATE OR REPLACE FUNCTION aletheia_observation_incorporation_complete()
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
               event_row.event_json #>> '{payload,source_world_model_sha256}'
                 IS DISTINCT FROM NEW.source_world_model_sha256 OR
               event_row.event_json #>> '{payload,action_id}' IS DISTINCT FROM
                 NEW.admission_json #>>
                   '{message,decision,message,committed_validation_receipt,message,receipt,message,raw_run,scientific_authorization,message,action_protocol_binding,action,action_id}' OR
               event_row.event_json #>> '{payload,branch_id}' IS DISTINCT FROM
                 NEW.admission_json #>>
                   '{message,decision,message,committed_validation_receipt,message,receipt,message,raw_run,scientific_authorization,message,action_protocol_binding,compilation_request,protocol,graph_scope,branch_id}' OR
               event_row.event_json #>> '{payload,outcome}' IS DISTINCT FROM
                 NEW.admission_json #>>
                   '{message,decision,message,committed_validation_receipt,message,receipt,message,outcome}' THEN
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
             NEW.event_json #>> '{payload,source_world_model_sha256}'
               IS DISTINCT FROM admission_row.source_world_model_sha256 OR
             NEW.event_json #>> '{payload,action_id}' IS DISTINCT FROM
               admission_row.admission_json #>>
                 '{message,decision,message,committed_validation_receipt,message,receipt,message,raw_run,scientific_authorization,message,action_protocol_binding,action,action_id}' OR
             NEW.event_json #>> '{payload,branch_id}' IS DISTINCT FROM
               admission_row.admission_json #>>
                 '{message,decision,message,committed_validation_receipt,message,receipt,message,raw_run,scientific_authorization,message,action_protocol_binding,compilation_request,protocol,graph_scope,branch_id}' OR
             NEW.event_json #>> '{payload,outcome}' IS DISTINCT FROM
               admission_row.admission_json #>>
                 '{message,decision,message,committed_validation_receipt,message,receipt,message,outcome}' THEN
            RAISE EXCEPTION
              'observation_incorporated event lacks its exact admission row'
              USING ERRCODE = '23514';
          END IF;
          RETURN NULL;
        END;
        $$
"""

_ORIGINAL_FUNCTION = """
        CREATE OR REPLACE FUNCTION aletheia_observation_incorporation_complete()
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
        $$
"""


def _require_original_trigger_shape() -> None:
    op.execute(
        f"""
        DO $migration$
        DECLARE
          definition text;
          broken_count integer;
          column_count integer;
        BEGIN
          SELECT pg_get_functiondef(
            to_regprocedure('public.aletheia_observation_incorporation_complete()')
          ) INTO definition;
          IF definition IS NULL THEN
            RAISE EXCEPTION 'required incorporation guard function is absent'
              USING ERRCODE = '55000';
          END IF;
          broken_count :=
            (length(definition) - length(replace(definition, {_BROKEN_PATH}, '')))
            / length({_BROKEN_PATH});
          column_count :=
            (length(definition)
             - length(replace(definition, '.source_world_model_sha256', '')))
            / length('.source_world_model_sha256');
          IF broken_count <> 2 OR column_count <> 0 THEN
            RAISE EXCEPTION
              'unexpected incorporation guard definition: broken path count %, column references %',
              broken_count, column_count
              USING ERRCODE = '55000';
          END IF;
        END;
        $migration$;
        """
    )


def upgrade() -> None:
    _require_original_trigger_shape()
    op.execute(
        "ALTER TABLE research_observation_admissions "
        "ADD COLUMN source_world_model_sha256 varchar(64) NOT NULL"
    )
    op.create_check_constraint(
        "ck_roa_world_model",
        "research_observation_admissions",
        "length(source_world_model_sha256) = 64 "
        "AND lower(source_world_model_sha256) = source_world_model_sha256",
    )
    op.execute(_FIXED_FUNCTION)


def downgrade() -> None:
    op.execute(_ORIGINAL_FUNCTION)
    op.drop_constraint(
        "ck_roa_world_model", "research_observation_admissions", type_="check"
    )
    op.execute(
        "ALTER TABLE research_observation_admissions "
        "DROP COLUMN source_world_model_sha256"
    )
