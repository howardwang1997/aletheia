"""External acceptance chain: nodeless attempts gain mirrored custody tables.

Revision ID: 20260923_0036
Revises: 20260921_0035
Create Date: 2026-09-23

Contradiction #20: external admission (0035) reserved nodelessly and stopped
there — every acceptance-chain verb refused nodeless attempts, so a
bridge-dispatched execution could never land terminal authority.  This
migration adds the mirrored custody ladder for nodeless attempts:

seven append-only tables twinning the node chain records
(``execution_external_runtime_preparations``, launch authorizations, launch
receipts, termination challenges, termination acceptances, qualification
terminal acceptances, and terminal deadline expirations), keyed to the same
placement-generic attempt columns (``runtime_preparation_sha256``,
``latest_runtime_launch_authorization_sha256``,
``runtime_termination_challenge_sha256``/count,
``accepted_runtime_termination_sha256``,
``accepted_terminal_submission_sha256``,
``terminal_deadline_expiration_sha256``, ``runtime_identity_*``); the node
launch-receipt column stays NULL for external attempts and the external
receipt row is per-attempt unique instead.

The frozen 0026 guards are kept verbatim for node rows and gain external
delegation: ``aletheia_execution_guard_runtime_v2_attempt_head`` routes
nodeless rows to a new mirror function (pointer immutability plus
head-monotonicity against the external tables, including the
challenge-sequence and deadline-activation arms), and
``aletheia_execution_check_runtime_v2_attempt`` routes nodeless rows to a new
external completeness function that re-derives the same field-exact lineage
(preparation authority, launch authorization lineage, launch receipt proof
windows, challenge binding, termination acceptance and conditional
expiration, terminal manifest/receipt canonicality, outbox payload equality
against ``a.updated_at``, and the compute-release/settle state machine).
Two anchored string edits under frozen-form assertions follow the 0035
idiom; downgrade applies the exact reverse.

The single ``execution_qualification_terminal_outbox`` table stays the only
terminal publication stream (the 0027 controller-delivery foreign key keeps
pointing at it).  Its two node-table column foreign keys are replaced by a
placement-aware authority trigger that requires the terminal authority row
to exist in the placement-correct acceptance/expiration table.

New closed JSON schema arms for the external contract payloads live in a new
``aletheia_execution_external_v2_json_valid`` function; shared schemas
(``aletheia.runtime_launch_authorization_request``,
``aletheia.runtime_control_authority_pin``, ``aletheia.artifact_manifest``,
``aletheia.artifact_manifest_entry``, ``aletheia.artifact_verified_receipt``)
keep validating through the frozen 0026 catalog.  No recovery grant columns:
historical recovery authority exists so a node can re-derive custody after
allocator state loss (adopt/absence); the external executor is a
commissioned process whose crash resume is an idempotent dispatch replay
against the stored receipt.  No data changes: existing rows are all
node-mode and no external rows exist yet.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260923_0036"
down_revision: str | None = "20260921_0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EXTERNAL_TABLES = (
    "execution_external_runtime_preparations",
    "execution_external_launch_authorizations",
    "execution_external_runtime_launch_receipts",
    "execution_external_termination_challenges",
    "execution_external_runtime_termination_acceptances",
    "execution_external_qualification_terminal_acceptances",
    "execution_external_qualification_deadline_expirations",
)

# Anchored frozen-form edits (0035 idiom): every node-mode guard below the
# insertion point stays byte-identical; downgrade applies the reverse edit.
_HEAD_GUARD_ANCHOR = (
    "BEGIN\n"
    "          IF OLD.runtime_preparation_sha256 IS NOT NULL AND"
)
_HEAD_GUARD_DELEGATION = (
    "BEGIN\n"
    "          IF NEW.node_id IS NULL AND OLD.node_id IS NULL THEN\n"
    "            RETURN aletheia_execution_guard_external_v2_attempt_head(OLD, NEW);\n"
    "          END IF;\n"
    "          IF OLD.runtime_preparation_sha256 IS NOT NULL AND"
)
_COMPLETE_GUARD_ANCHOR = (
    "SELECT * INTO a FROM execution_attempts WHERE attempt_id = target_attempt;\n"
    "          IF NOT FOUND THEN RETURN NULL; END IF;"
)
_COMPLETE_GUARD_DELEGATION = (
    "SELECT * INTO a FROM execution_attempts WHERE attempt_id = target_attempt;\n"
    "          IF NOT FOUND THEN RETURN NULL; END IF;\n"
    "          IF a.node_id IS NULL THEN\n"
    "            PERFORM aletheia_execution_check_external_v2_attempt(target_attempt);\n"
    "            RETURN NULL;\n"
    "          END IF;"
)
# The frozen guard_attempt (below the v2 head guard in the same BEFORE-UPDATE
# chain) binds runtime identity and the terminalizing/accepting_termination
# booleans against node custody tables only; each arm gains an external-twin
# disjunct so a nodeless attempt can launch, terminate and finalize.
_GUARD_ATTEMPT_IDENTITY_ANCHOR = (
    "                  SELECT 1 FROM execution_runtime_launch_receipts l\n"
    "                   WHERE l.attempt_id = NEW.attempt_id\n"
    "                     AND l.launch_receipt_sha256 ="
    " NEW.node_runtime_launch_receipt_sha256\n"
    "                ))\n"
    "             ) THEN"
)
_GUARD_ATTEMPT_IDENTITY_DELEGATION = (
    "                  SELECT 1 FROM execution_runtime_launch_receipts l\n"
    "                   WHERE l.attempt_id = NEW.attempt_id\n"
    "                     AND l.launch_receipt_sha256 ="
    " NEW.node_runtime_launch_receipt_sha256\n"
    "                )) OR\n"
    "               (OLD.node_id IS NULL AND\n"
    "                OLD.status = 'starting' AND NEW.status = 'running' AND EXISTS (\n"
    "                  SELECT 1 FROM execution_external_runtime_launch_receipts l\n"
    "                   WHERE l.attempt_id = NEW.attempt_id\n"
    "                     AND l.executor_identity_sha256 =\n"
    "                       NEW.runtime_identity_sha256\n"
    "                     AND l.launch_receipt_json->'launch_evidence'\n"
    "                         ->'executor_identity' IS NOT DISTINCT FROM\n"
    "                       NEW.runtime_identity_json\n"
    "                ))\n"
    "             ) THEN"
)
_GUARD_ATTEMPT_ACCEPTING_ANCHOR = (
    "          accepting_termination := NEW.accepted_runtime_termination_sha256"
    " IS NOT NULL\n"
    "            AND OLD.accepted_runtime_termination_sha256 IS NULL\n"
    "            AND EXISTS (\n"
    "              SELECT 1 FROM execution_runtime_termination_acceptances t\n"
    "               WHERE t.attempt_id = NEW.attempt_id\n"
    "                 AND t.accepted_termination_sha256 =\n"
    "                   NEW.accepted_runtime_termination_sha256\n"
    "            );"
)
_GUARD_ATTEMPT_ACCEPTING_DELEGATION = (
    "          accepting_termination := NEW.accepted_runtime_termination_sha256"
    " IS NOT NULL\n"
    "            AND OLD.accepted_runtime_termination_sha256 IS NULL\n"
    "            AND (\n"
    "              EXISTS (\n"
    "                SELECT 1 FROM execution_runtime_termination_acceptances t\n"
    "                 WHERE t.attempt_id = NEW.attempt_id\n"
    "                   AND t.accepted_termination_sha256 =\n"
    "                     NEW.accepted_runtime_termination_sha256\n"
    "              ) OR (\n"
    "                NEW.node_id IS NULL AND EXISTS (\n"
    "                  SELECT 1\n"
    "                    FROM execution_external_runtime_termination_acceptances t\n"
    "                   WHERE t.attempt_id = NEW.attempt_id\n"
    "                     AND t.accepted_termination_sha256 =\n"
    "                       NEW.accepted_runtime_termination_sha256\n"
    "                ))\n"
    "            );"
)
_GUARD_ATTEMPT_TERMINAL_ANCHOR = (
    "            (NEW.terminal_deadline_expiration_sha256 IS NOT NULL AND EXISTS (\n"
    "              SELECT 1\n"
    "                FROM execution_qualification_terminal_deadline_expirations x\n"
    "               WHERE x.terminal_deadline_expiration_sha256 =\n"
    "                       NEW.terminal_deadline_expiration_sha256\n"
    "                 AND x.attempt_id = NEW.attempt_id\n"
    "                 AND NEW.accepted_terminal_submission_sha256 IS NULL\n"
    "            )) OR absence_releasing"
)
_GUARD_ATTEMPT_TERMINAL_DELEGATION = (
    "            (NEW.terminal_deadline_expiration_sha256 IS NOT NULL AND EXISTS (\n"
    "              SELECT 1\n"
    "                FROM execution_qualification_terminal_deadline_expirations x\n"
    "               WHERE x.terminal_deadline_expiration_sha256 =\n"
    "                       NEW.terminal_deadline_expiration_sha256\n"
    "                 AND x.attempt_id = NEW.attempt_id\n"
    "                 AND NEW.accepted_terminal_submission_sha256 IS NULL\n"
    "            )) OR\n"
    "            (NEW.accepted_terminal_submission_sha256 IS NOT NULL AND EXISTS (\n"
    "              SELECT 1 FROM execution_external_qualification_terminal_acceptances q\n"
    "               WHERE q.accepted_terminal_submission_sha256 =\n"
    "                       NEW.accepted_terminal_submission_sha256\n"
    "                 AND q.attempt_id = NEW.attempt_id\n"
    "            )) OR\n"
    "            (NEW.terminal_deadline_expiration_sha256 IS NOT NULL AND EXISTS (\n"
    "              SELECT 1\n"
    "                FROM execution_external_qualification_deadline_expirations x\n"
    "               WHERE x.terminal_deadline_expiration_sha256 =\n"
    "                       NEW.terminal_deadline_expiration_sha256\n"
    "                 AND x.attempt_id = NEW.attempt_id\n"
    "                 AND NEW.accepted_terminal_submission_sha256 IS NULL\n"
    "            )) OR absence_releasing"
)
# The frozen attempt-bundle guard proves an accepted termination against the
# node custody table only; nodeless attempts prove it against the external
# twin instead (node attempts keep byte-identical behavior).
_BUNDLE_TERMINATION_ANCHOR = (
    "               NOT EXISTS (\n"
    "                 SELECT 1 FROM execution_runtime_termination_acceptances t\n"
    "                  WHERE t.attempt_id = attempt_row.attempt_id\n"
    "                    AND t.accepted_termination_sha256 =\n"
    "                      attempt_row.accepted_runtime_termination_sha256\n"
    "               ) OR"
)
_BUNDLE_TERMINATION_DELEGATION = (
    "               NOT EXISTS (\n"
    "                 SELECT 1 FROM execution_runtime_termination_acceptances t\n"
    "                  WHERE t.attempt_id = attempt_row.attempt_id\n"
    "                    AND t.accepted_termination_sha256 =\n"
    "                      attempt_row.accepted_runtime_termination_sha256\n"
    "               ) AND (\n"
    "                 attempt_row.node_id IS NOT NULL OR NOT EXISTS (\n"
    "                   SELECT 1\n"
    "                     FROM execution_external_runtime_termination_acceptances t\n"
    "                    WHERE t.attempt_id = attempt_row.attempt_id\n"
    "                      AND t.accepted_termination_sha256 =\n"
    "                        attempt_row.accepted_runtime_termination_sha256\n"
    "                 )\n"
    "               ) OR"
)


def _create_external_chain_tables() -> None:
    op.execute(
        r"""
        CREATE TABLE execution_external_runtime_preparations (
          preparation_sha256 varchar(64) PRIMARY KEY,
          attempt_id varchar(36) NOT NULL,
          execution_id varchar(36) NOT NULL,
          intent_sha256 varchar(64) NOT NULL,
          bridge_manifest_sha256 varchar(64) NOT NULL,
          fencing_epoch bigint NOT NULL,
          lease_token_sha256 varchar(64) NOT NULL,
          payload_sha256 varchar(64) NOT NULL,
          payload_json jsonb NOT NULL,
          prepared_at timestamptz NOT NULL,
          prepared_monotonic_ns bigint NOT NULL,
          recorded_at timestamptz NOT NULL,
          CONSTRAINT ck_execution_external_runtime_preparations_order CHECK (
            fencing_epoch >= 1 AND prepared_monotonic_ns >= 0),
          CONSTRAINT ck_execution_external_runtime_preparations_hashes CHECK (
            preparation_sha256 ~ '^[0-9a-f]{64}$' AND
            intent_sha256 ~ '^[0-9a-f]{64}$' AND
            bridge_manifest_sha256 ~ '^[0-9a-f]{64}$' AND
            lease_token_sha256 ~ '^[0-9a-f]{64}$' AND
            payload_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT fk_execution_external_runtime_preparations_attempt
            FOREIGN KEY (attempt_id, execution_id)
            REFERENCES execution_attempts (attempt_id, execution_id),
          CONSTRAINT uq_execution_external_runtime_preparations_attempt
            UNIQUE (attempt_id)
        );
        CREATE INDEX ix_execution_external_runtime_preparations_execution_id
          ON execution_external_runtime_preparations (execution_id);
        CREATE INDEX ix_execution_external_runtime_preparations_recorded_at
          ON execution_external_runtime_preparations (recorded_at);

        CREATE TABLE execution_external_launch_authorizations (
          authorization_sha256 varchar(64) PRIMARY KEY,
          attempt_id varchar(36) NOT NULL
            REFERENCES execution_attempts (attempt_id),
          preparation_sha256 varchar(64) NOT NULL
            REFERENCES execution_external_runtime_preparations (
              preparation_sha256),
          sequence integer NOT NULL,
          request_sha256 varchar(64) NOT NULL,
          request_payload_sha256 varchar(64) NOT NULL,
          request_json jsonb NOT NULL,
          authorization_payload_sha256 varchar(64) NOT NULL,
          authorization_json jsonb NOT NULL,
          runtime_control_pin_sha256 varchar(64) NOT NULL,
          runtime_control_pin_json jsonb NOT NULL,
          issued_at timestamptz NOT NULL,
          expires_at timestamptz NOT NULL,
          recorded_at timestamptz NOT NULL,
          CONSTRAINT ck_execution_external_launch_authorizations_order CHECK (
            sequence >= 1 AND issued_at < expires_at),
          CONSTRAINT ck_execution_external_launch_authorizations_hashes CHECK (
            authorization_sha256 ~ '^[0-9a-f]{64}$' AND
            request_sha256 ~ '^[0-9a-f]{64}$' AND
            preparation_sha256 ~ '^[0-9a-f]{64}$' AND
            request_payload_sha256 ~ '^[0-9a-f]{64}$' AND
            authorization_payload_sha256 ~ '^[0-9a-f]{64}$' AND
            runtime_control_pin_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT uq_execution_external_launch_authorizations_sequence
            UNIQUE (attempt_id, sequence),
          CONSTRAINT uq_execution_external_launch_authorizations_request
            UNIQUE (request_sha256)
        );
        CREATE INDEX ix_execution_external_launch_authorizations_preparation_sha256
          ON execution_external_launch_authorizations (preparation_sha256);
        CREATE INDEX ix_execution_external_launch_authorizations_expires_at
          ON execution_external_launch_authorizations (expires_at);
        CREATE INDEX ix_execution_external_launch_authorizations_recorded_at
          ON execution_external_launch_authorizations (recorded_at);

        CREATE TABLE execution_external_runtime_launch_receipts (
          launch_receipt_sha256 varchar(64) PRIMARY KEY,
          attempt_id varchar(36) NOT NULL
            REFERENCES execution_attempts (attempt_id),
          preparation_sha256 varchar(64) NOT NULL
            REFERENCES execution_external_runtime_preparations (
              preparation_sha256),
          authorization_request_sha256 varchar(64) NOT NULL,
          authorization_sha256 varchar(64) NOT NULL
            REFERENCES execution_external_launch_authorizations (
              authorization_sha256),
          executor_identity_sha256 varchar(64) NOT NULL,
          launch_evidence_sha256 varchar(64) NOT NULL,
          launch_payload_sha256 varchar(64) NOT NULL,
          launch_receipt_json jsonb NOT NULL,
          bridge_pin_sha256 varchar(64) NOT NULL,
          bridge_pin_json jsonb NOT NULL,
          signed_at timestamptz NOT NULL,
          accepted_at timestamptz NOT NULL,
          CONSTRAINT ck_execution_external_runtime_launch_receipts_hashes CHECK (
            launch_receipt_sha256 ~ '^[0-9a-f]{64}$' AND
            preparation_sha256 ~ '^[0-9a-f]{64}$' AND
            authorization_request_sha256 ~ '^[0-9a-f]{64}$' AND
            authorization_sha256 ~ '^[0-9a-f]{64}$' AND
            executor_identity_sha256 ~ '^[0-9a-f]{64}$' AND
            launch_evidence_sha256 ~ '^[0-9a-f]{64}$' AND
            launch_payload_sha256 ~ '^[0-9a-f]{64}$' AND
            bridge_pin_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT uq_execution_external_runtime_launch_receipts_attempt
            UNIQUE (attempt_id),
          CONSTRAINT uq_execution_external_runtime_launch_receipts_identity
            UNIQUE (executor_identity_sha256)
        );
        CREATE INDEX ix_execution_external_runtime_launch_receipts_accepted_at
          ON execution_external_runtime_launch_receipts (accepted_at);

        CREATE TABLE execution_external_termination_challenges (
          challenge_sha256 varchar(64) PRIMARY KEY,
          challenge_id varchar(64) NOT NULL,
          attempt_id varchar(36) NOT NULL
            REFERENCES execution_attempts (attempt_id),
          challenge_sequence integer NOT NULL,
          preparation_sha256 varchar(64) NOT NULL
            REFERENCES execution_external_runtime_preparations (
              preparation_sha256),
          launch_receipt_sha256 varchar(64) NOT NULL
            REFERENCES execution_external_runtime_launch_receipts (
              launch_receipt_sha256),
          executor_identity_sha256 varchar(64) NOT NULL,
          termination_evidence_sha256 varchar(64) NOT NULL,
          termination_evidence_json jsonb NOT NULL,
          challenge_payload_sha256 varchar(64) NOT NULL,
          challenge_json jsonb NOT NULL,
          runtime_control_pin_sha256 varchar(64) NOT NULL,
          runtime_control_pin_json jsonb NOT NULL,
          challenged_at timestamptz NOT NULL,
          expires_at timestamptz NOT NULL,
          CONSTRAINT ck_execution_external_termination_challenges_order CHECK (
            challenge_sequence >= 1 AND challenged_at < expires_at),
          CONSTRAINT ck_execution_external_termination_challenges_hashes CHECK (
            challenge_sha256 ~ '^[0-9a-f]{64}$' AND
            challenge_id ~ '^[0-9a-f]{64}$' AND
            preparation_sha256 ~ '^[0-9a-f]{64}$' AND
            launch_receipt_sha256 ~ '^[0-9a-f]{64}$' AND
            executor_identity_sha256 ~ '^[0-9a-f]{64}$' AND
            termination_evidence_sha256 ~ '^[0-9a-f]{64}$' AND
            challenge_payload_sha256 ~ '^[0-9a-f]{64}$' AND
            runtime_control_pin_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT uq_execution_external_termination_challenge_sequence
            UNIQUE (attempt_id, challenge_sequence),
          CONSTRAINT uq_execution_external_termination_challenge_id
            UNIQUE (challenge_id)
        );
        CREATE INDEX ix_execution_external_termination_challenges_expires_at
          ON execution_external_termination_challenges (expires_at);

        CREATE TABLE execution_external_runtime_termination_acceptances (
          accepted_termination_sha256 varchar(64) PRIMARY KEY,
          attempt_id varchar(36) NOT NULL
            REFERENCES execution_attempts (attempt_id),
          challenge_sha256 varchar(64) NOT NULL
            REFERENCES execution_external_termination_challenges (
              challenge_sha256),
          bridge_termination_receipt_sha256 varchar(64) NOT NULL,
          preparation_sha256 varchar(64) NOT NULL
            REFERENCES execution_external_runtime_preparations (
              preparation_sha256),
          launch_receipt_sha256 varchar(64) NOT NULL
            REFERENCES execution_external_runtime_launch_receipts (
              launch_receipt_sha256),
          authorization_request_sha256 varchar(64) NOT NULL,
          authorization_sha256 varchar(64) NOT NULL,
          executor_identity_sha256 varchar(64) NOT NULL,
          termination_evidence_sha256 varchar(64) NOT NULL,
          result_content_sha256 varchar(64) NOT NULL,
          exit_code integer NOT NULL,
          runtime_ended_at timestamptz NOT NULL,
          receipt_payload_sha256 varchar(64) NOT NULL,
          bridge_termination_receipt_json jsonb NOT NULL,
          acceptance_payload_sha256 varchar(64) NOT NULL,
          accepted_termination_json jsonb NOT NULL,
          conditional_terminal_expiration_sha256 varchar(64) NOT NULL,
          conditional_terminal_expiration_payload_sha256 varchar(64) NOT NULL,
          conditional_terminal_expiration_json jsonb NOT NULL,
          conditional_terminal_expiration_authorized_at timestamptz NOT NULL,
          conditional_terminal_expiration_expires_at timestamptz NOT NULL,
          runtime_control_pin_sha256 varchar(64) NOT NULL,
          runtime_control_pin_json jsonb NOT NULL,
          accepted_at timestamptz NOT NULL,
          CONSTRAINT ck_execution_external_runtime_termination_acceptances_order
            CHECK (exit_code BETWEEN 0 AND 255 AND
                   runtime_ended_at <= accepted_at),
          CONSTRAINT
            ck_execution_external_runtime_termination_acceptances_hashes CHECK (
            accepted_termination_sha256 ~ '^[0-9a-f]{64}$' AND
            challenge_sha256 ~ '^[0-9a-f]{64}$' AND
            bridge_termination_receipt_sha256 ~ '^[0-9a-f]{64}$' AND
            preparation_sha256 ~ '^[0-9a-f]{64}$' AND
            launch_receipt_sha256 ~ '^[0-9a-f]{64}$' AND
            authorization_request_sha256 ~ '^[0-9a-f]{64}$' AND
            authorization_sha256 ~ '^[0-9a-f]{64}$' AND
            executor_identity_sha256 ~ '^[0-9a-f]{64}$' AND
            termination_evidence_sha256 ~ '^[0-9a-f]{64}$' AND
            result_content_sha256 ~ '^[0-9a-f]{64}$' AND
            receipt_payload_sha256 ~ '^[0-9a-f]{64}$' AND
            acceptance_payload_sha256 ~ '^[0-9a-f]{64}$' AND
            conditional_terminal_expiration_sha256 ~ '^[0-9a-f]{64}$' AND
            conditional_terminal_expiration_payload_sha256 ~ '^[0-9a-f]{64}$' AND
            runtime_control_pin_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT
            uq_execution_external_runtime_termination_acceptance_attempt
            UNIQUE (attempt_id),
          CONSTRAINT uq_execution_external_runtime_termination_challenge
            UNIQUE (challenge_sha256),
          CONSTRAINT uq_execution_external_runtime_termination_receipt
            UNIQUE (bridge_termination_receipt_sha256),
          CONSTRAINT uq_execution_external_runtime_termination_conditional
            UNIQUE (conditional_terminal_expiration_sha256)
        );
        CREATE INDEX ix_exec_ext_term_acceptances_conditional_expires_at
          ON execution_external_runtime_termination_acceptances (
            conditional_terminal_expiration_expires_at);
        CREATE INDEX ix_exec_ext_term_acceptances_accepted_at
          ON execution_external_runtime_termination_acceptances (accepted_at);

        CREATE TABLE execution_external_qualification_terminal_acceptances (
          accepted_terminal_submission_sha256 varchar(64) PRIMARY KEY,
          attempt_id varchar(36) NOT NULL
            REFERENCES execution_attempts (attempt_id),
          accepted_runtime_termination_sha256 varchar(64) NOT NULL
            REFERENCES execution_external_runtime_termination_acceptances (
              accepted_termination_sha256),
          bridge_manifest_sha256 varchar(64) NOT NULL,
          terminal_submission_sha256 varchar(64) NOT NULL,
          artifact_manifest_sha256 varchar(64) NOT NULL,
          output_tree_sha256 varchar(64) NOT NULL,
          disposition varchar(32) NOT NULL,
          submission_payload_sha256 varchar(64) NOT NULL,
          terminal_submission_json jsonb NOT NULL,
          manifest_payload_sha256 varchar(64) NOT NULL,
          artifact_manifest_json jsonb NOT NULL,
          artifact_verified_receipt_sha256s_json jsonb NOT NULL,
          artifact_verified_receipts_json jsonb NOT NULL,
          acceptance_payload_sha256 varchar(64) NOT NULL,
          accepted_terminal_submission_json jsonb NOT NULL,
          runtime_control_pin_sha256 varchar(64) NOT NULL,
          runtime_control_pin_json jsonb NOT NULL,
          accepted_at timestamptz NOT NULL,
          CONSTRAINT
            ck_execution_external_qualification_terminal_acceptances_disposition
            CHECK (disposition IN ('process_succeeded','process_failed',
                                   'invalid_output','timeout')),
          CONSTRAINT
            ck_execution_external_qualification_terminal_acceptances_hashes CHECK (
            accepted_terminal_submission_sha256 ~ '^[0-9a-f]{64}$' AND
            accepted_runtime_termination_sha256 ~ '^[0-9a-f]{64}$' AND
            bridge_manifest_sha256 ~ '^[0-9a-f]{64}$' AND
            terminal_submission_sha256 ~ '^[0-9a-f]{64}$' AND
            artifact_manifest_sha256 ~ '^[0-9a-f]{64}$' AND
            output_tree_sha256 ~ '^[0-9a-f]{64}$' AND
            submission_payload_sha256 ~ '^[0-9a-f]{64}$' AND
            manifest_payload_sha256 ~ '^[0-9a-f]{64}$' AND
            acceptance_payload_sha256 ~ '^[0-9a-f]{64}$' AND
            runtime_control_pin_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT
            uq_execution_external_qualification_terminal_acceptance_attempt
            UNIQUE (attempt_id),
          CONSTRAINT
            uq_execution_external_qualification_terminal_termination
            UNIQUE (accepted_runtime_termination_sha256),
          CONSTRAINT
            uq_execution_external_qualification_terminal_submission
            UNIQUE (terminal_submission_sha256)
        );
        CREATE INDEX ix_exec_ext_qual_term_acceptances_disposition
          ON execution_external_qualification_terminal_acceptances (
            disposition);
        CREATE INDEX ix_exec_ext_qual_term_acceptances_accepted_at
          ON execution_external_qualification_terminal_acceptances (accepted_at);

        CREATE TABLE
          execution_external_qualification_deadline_expirations (
          terminal_deadline_expiration_sha256 varchar(64) PRIMARY KEY
            REFERENCES execution_external_runtime_termination_acceptances (
              conditional_terminal_expiration_sha256),
          attempt_id varchar(36) NOT NULL
            REFERENCES execution_attempts (attempt_id),
          accepted_runtime_termination_sha256 varchar(64) NOT NULL
            REFERENCES execution_external_runtime_termination_acceptances (
              accepted_termination_sha256),
          payload_sha256 varchar(64) NOT NULL,
          payload_json jsonb NOT NULL,
          runtime_control_pin_sha256 varchar(64) NOT NULL,
          runtime_control_pin_json jsonb NOT NULL,
          authorized_at timestamptz NOT NULL,
          expired_at timestamptz NOT NULL,
          activated_at timestamptz NOT NULL,
          CONSTRAINT
            ck_execution_external_qualification_deadline_order CHECK (
            authorized_at < expired_at AND expired_at <= activated_at),
          CONSTRAINT
            ck_execution_external_qualification_deadline_hashes CHECK (
            terminal_deadline_expiration_sha256 ~ '^[0-9a-f]{64}$' AND
            accepted_runtime_termination_sha256 ~ '^[0-9a-f]{64}$' AND
            payload_sha256 ~ '^[0-9a-f]{64}$' AND
            runtime_control_pin_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT
            uq_execution_external_qualification_deadline_attempt
            UNIQUE (attempt_id),
          CONSTRAINT
            uq_execution_external_qualification_deadline_termination
            UNIQUE (accepted_runtime_termination_sha256)
        );
        CREATE INDEX ix_exec_ext_qual_deadline_expirations_expired_at
          ON execution_external_qualification_deadline_expirations (
            expired_at);
        """
    )


def _install_external_json_shape_catalog() -> None:
    op.execute(
        r"""
        CREATE FUNCTION aletheia_execution_external_v2_json_valid(
          value jsonb, expected_schema text
        ) RETURNS boolean LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
        DECLARE shape jsonb;
        BEGIN
          shape := CASE expected_schema
            WHEN 'aletheia.external_executor_identity' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"execution_id":"string","infrastructure_attempt_id":"string",' ||
              '"runtime_id":"string","executor_ref":"string",' ||
              '"executor_implementation_sha256":"string",' ||
              '"invocation_payload_sha256":"string","started_at":"string",' ||
              '"started_monotonic_ns":"number"}'
            WHEN 'aletheia.external_runtime_preparation' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"bridge_manifest_sha256":"string","execution_id":"string",' ||
              '"infrastructure_attempt_id":"string","intent_sha256":"string",' ||
              '"runtime_id":"string","runtime_engine":"string",' ||
              '"launch_spec_sha256":"string",' ||
              '"workload_executable_sha256":"string",' ||
              '"workload_argv":"array","runtime_request_sha256":"string",' ||
              '"enforced_placement_sha256":"string",' ||
              '"input_materialization_receipt_sha256":"string",' ||
              '"fencing_epoch":"number","lease_token_sha256":"string",' ||
              '"prepared_dispatch_locator_sha256":"string",' ||
              '"prepared_at":"string","prepared_monotonic_ns":"number",' ||
              '"qualification_only":"boolean",' ||
              '"scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.external_launch_authorization' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"admission_sha256":"string",' ||
              '"qualification_grant_sha256":"string",' ||
              '"bridge_manifest_sha256":"string","execution_id":"string",' ||
              '"infrastructure_attempt_id":"string","intent_sha256":"string",' ||
              '"runtime_preparation_sha256":"string",' ||
              '"authorization_request_sha256":"string",' ||
              '"launch_spec_sha256":"string",' ||
              '"workload_executable_sha256":"string",' ||
              '"workload_argv":"array",' ||
              '"enforced_placement_sha256":"string",' ||
              '"input_materialization_receipt_sha256":"string",' ||
              '"fencing_epoch":"number","lease_token_sha256":"string",' ||
              '"lease_expires_at":"string","hard_deadline":"string",' ||
              '"issued_at":"string","expires_at":"string",' ||
              '"max_launch_delay_ns":"number",' ||
              '"runtime_control_policy_sha256":"string",' ||
              '"authorized_by_principal_id":"string",' ||
              '"authorization_key_id":"string",' ||
              '"signature_ed25519_hex":"string",' ||
              '"qualification_only":"boolean",' ||
              '"scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.external_launch_evidence' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"preparation_sha256":"string",' ||
              '"external_launch_authorization_sha256":"string",' ||
              '"executor_identity":"object",' ||
              '"executor_identity_sha256":"string",' ||
              '"executor_start_monotonic_lower_bound_ns":"number",' ||
              '"executor_start_monotonic_upper_bound_exclusive_ns":"number",' ||
              '"enforced_placement_sha256":"string",' ||
              '"input_materialization_receipt_sha256":"string",' ||
              '"enforced_fencing_epoch":"number",' ||
              '"enforced_lease_token_sha256":"string",' ||
              '"launch_evidence_journal_sha256":"string",' ||
              '"observed_at":"string","observed_monotonic_ns":"number",' ||
              '"qualification_only":"boolean",' ||
              '"scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.external_runtime_launch_receipt' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"bridge_manifest_sha256":"string",' ||
              '"launch_evidence":"object",' ||
              '"launch_evidence_sha256":"string","signed_at":"string",' ||
              '"signing_key_id":"string","signature_ed25519_hex":"string",' ||
              '"qualification_only":"boolean",' ||
              '"scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.external_termination_evidence' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"preparation_sha256":"string",' ||
              '"external_launch_receipt_sha256":"string",' ||
              '"executor_identity_sha256":"string","exit_code":"number",' ||
              '"ended_at":"string","ended_monotonic_ns":"number",' ||
              '"result_content_sha256":"string",' ||
              '"termination_journal_sha256":"string",' ||
              '"qualification_only":"boolean",' ||
              '"scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.external_termination_acceptance_challenge' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"challenge_id":"string","attempt_id":"string",' ||
              '"execution_id":"string","intent_sha256":"string",' ||
              '"bridge_manifest_sha256":"string",' ||
              '"runtime_preparation_sha256":"string",' ||
              '"external_runtime_launch_receipt_sha256":"string",' ||
              '"executor_identity_sha256":"string",' ||
              '"termination_evidence_sha256":"string",' ||
              '"result_content_sha256":"string",' ||
              '"resource_lease_sha256":"string",' ||
              '"fencing_epoch":"number","lease_token_sha256":"string",' ||
              '"hard_deadline":"string",' ||
              '"artifact_submission_deadline":"string",' ||
              '"challenged_at":"string","expires_at":"string",' ||
              '"runtime_control_policy_sha256":"string",' ||
              '"challenged_by_principal_id":"string",' ||
              '"challenge_key_id":"string",' ||
              '"signature_ed25519_hex":"string",' ||
              '"qualification_only":"boolean",' ||
              '"scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.external_runtime_termination_receipt' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"bridge_manifest_sha256":"string","challenge_sha256":"string",' ||
              '"runtime_preparation_sha256":"string",' ||
              '"external_runtime_launch_receipt_sha256":"string",' ||
              '"runtime_launch_authorization_request_sha256":"string",' ||
              '"external_launch_authorization_sha256":"string",' ||
              '"termination_evidence":"object",' ||
              '"termination_evidence_sha256":"string",' ||
              '"signed_at":"string","expires_at":"string",' ||
              '"signing_key_id":"string","signature_ed25519_hex":"string",' ||
              '"qualification_only":"boolean",' ||
              '"scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.accepted_external_runtime_termination' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"challenge_sha256":"string","attempt_id":"string",' ||
              '"runtime_preparation_sha256":"string",' ||
              '"external_runtime_launch_receipt_sha256":"string",' ||
              '"runtime_launch_authorization_request_sha256":"string",' ||
              '"external_launch_authorization_sha256":"string",' ||
              '"external_runtime_termination_receipt_sha256":"string",' ||
              '"executor_identity_sha256":"string",' ||
              '"termination_evidence_sha256":"string",' ||
              '"result_content_sha256":"string",' ||
              '"fencing_epoch":"number","lease_token_sha256":"string",' ||
              '"runtime_ended_at":"string","exit_code":"number",' ||
              '"hard_deadline":"string",' ||
              '"artifact_submission_deadline":"string",' ||
              '"proof_signed_at":"string","proof_expires_at":"string",' ||
              '"accepted_at":"string","billable_ended_at":"string",' ||
              '"runtime_control_policy_sha256":"string",' ||
              '"accepted_by_principal_id":"string",' ||
              '"acceptance_key_id":"string",' ||
              '"signature_ed25519_hex":"string",' ||
              '"proof_was_fresh":"boolean",' ||
              '"compute_release_allowed":"boolean",' ||
              '"scientific_admission_allowed":"boolean",' ||
              '"qualification_only":"boolean"}'
            WHEN 'aletheia.external_qualification_terminal_submission' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"bridge_manifest_sha256":"string","intent_sha256":"string",' ||
              '"execution_id":"string","attempt_id":"string",' ||
              '"resource_lease_sha256":"string",' ||
              '"fencing_epoch":"number","lease_token_sha256":"string",' ||
              '"accepted_external_runtime_termination_sha256":"string",' ||
              '"artifact_manifest_sha256":"string",' ||
              '"output_tree_sha256":"string",' ||
              '"artifact_verified_receipt_sha256s":"array",' ||
              '"disposition":"string","submitted_at":"string",' ||
              '"signing_key_id":"string","signature_ed25519_hex":"string",' ||
              '"qualification_only":"boolean",' ||
              '"scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.accepted_external_qualification_terminal_submission'
            THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"attempt_id":"string","bridge_manifest_sha256":"string",' ||
              '"terminal_submission_sha256":"string",' ||
              '"accepted_external_runtime_termination_sha256":"string",' ||
              '"artifact_manifest_sha256":"string",' ||
              '"output_tree_sha256":"string",' ||
              '"artifact_verified_receipt_sha256s":"array",' ||
              '"disposition":"string","bridge_submitted_at":"string",' ||
              '"artifact_submission_deadline":"string",' ||
              '"accepted_at":"string",' ||
              '"runtime_control_policy_sha256":"string",' ||
              '"accepted_by_principal_id":"string",' ||
              '"acceptance_key_id":"string",' ||
              '"signature_ed25519_hex":"string",' ||
              '"qualification_only":"boolean",' ||
              '"scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.external_qualification_terminal_deadline_expiration'
            THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"attempt_id":"string","execution_id":"string",' ||
              '"intent_sha256":"string","bridge_manifest_sha256":"string",' ||
              '"resource_lease_sha256":"string",' ||
              '"runtime_preparation_sha256":"string",' ||
              '"runtime_launch_authorization_request_sha256":"string",' ||
              '"external_launch_authorization_sha256":"string",' ||
              '"external_runtime_launch_receipt_sha256":"string",' ||
              '"external_termination_challenge_sha256":"string",' ||
              '"external_runtime_termination_receipt_sha256":"string",' ||
              '"accepted_external_runtime_termination_sha256":"string",' ||
              '"executor_identity_sha256":"string",' ||
              '"termination_evidence_sha256":"string",' ||
              '"result_content_sha256":"string",' ||
              '"fencing_epoch":"number","lease_token_sha256":"string",' ||
              '"runtime_ended_at":"string","exit_code":"number",' ||
              '"hard_deadline":"string",' ||
              '"artifact_submission_deadline":"string",' ||
              '"accepted_runtime_termination_at":"string",' ||
              '"authorized_at":"string","expired_at":"string",' ||
              '"reason":"string","disposition":"string",' ||
              '"retryable":"boolean",' ||
              '"conditional_on_terminal_submission_absence":"boolean",' ||
              '"database_time_activation_required":"boolean",' ||
              '"runtime_control_policy_sha256":"string",' ||
              '"adjudicated_by_principal_id":"string",' ||
              '"adjudication_key_id":"string",' ||
              '"signature_ed25519_hex":"string",' ||
              '"qualification_only":"boolean",' ||
              '"scientific_admission_allowed":"boolean"}'
            ELSE NULL
          END;
          IF shape IS NULL THEN
            RAISE EXCEPTION 'unknown external v2 schema %', expected_schema;
          END IF;
          RETURN aletheia_execution_json_shape(value, shape);
        END;
        $$;
        """
    )


def _install_external_head_guard() -> None:
    op.execute(
        r"""
        CREATE FUNCTION aletheia_execution_guard_external_v2_attempt_head(
          old_attempt execution_attempts,
          new_attempt execution_attempts
        ) RETURNS execution_attempts LANGUAGE plpgsql AS $$
        BEGIN
          IF old_attempt.runtime_preparation_sha256 IS NOT NULL AND
             new_attempt.runtime_preparation_sha256 IS DISTINCT FROM
               old_attempt.runtime_preparation_sha256 THEN
            RAISE EXCEPTION 'runtime preparation pointer is immutable'
              USING ERRCODE = '55000';
          END IF;
          IF new_attempt.runtime_preparation_sha256 IS DISTINCT FROM
             old_attempt.runtime_preparation_sha256 AND NOT EXISTS (
               SELECT 1 FROM execution_external_runtime_preparations p
                WHERE p.attempt_id = new_attempt.attempt_id
                  AND p.preparation_sha256 =
                    new_attempt.runtime_preparation_sha256
             ) THEN
            RAISE EXCEPTION 'runtime preparation pointer lacks exact row'
              USING ERRCODE = '55000';
          END IF;
          IF (new_attempt.runtime_launch_authorization_count,
              new_attempt.latest_runtime_launch_authorization_sha256)
                IS DISTINCT FROM
             (old_attempt.runtime_launch_authorization_count,
              old_attempt.latest_runtime_launch_authorization_sha256) AND NOT (
                old_attempt.status IN ('reserved', 'starting') AND
                new_attempt.status = 'starting' AND
                old_attempt.node_runtime_launch_receipt_sha256 IS NULL AND
                new_attempt.node_runtime_launch_receipt_sha256 IS NULL AND
                old_attempt.accepted_runtime_termination_sha256 IS NULL AND
                new_attempt.accepted_runtime_termination_sha256 IS NULL AND
                old_attempt.accepted_terminal_submission_sha256 IS NULL AND
                new_attempt.accepted_terminal_submission_sha256 IS NULL AND
                old_attempt.terminal_deadline_expiration_sha256 IS NULL AND
                new_attempt.terminal_deadline_expiration_sha256 IS NULL AND
                new_attempt.runtime_launch_authorization_count =
                  old_attempt.runtime_launch_authorization_count + 1 AND
                EXISTS (
                  SELECT 1
                    FROM execution_external_launch_authorizations a
                   WHERE a.attempt_id = new_attempt.attempt_id
                     AND a.sequence =
                       new_attempt.runtime_launch_authorization_count
                     AND a.authorization_sha256 =
                       new_attempt.latest_runtime_launch_authorization_sha256
                )
             ) THEN
            RAISE EXCEPTION 'runtime launch authorization head is non-monotonic'
              USING ERRCODE = '55000';
          END IF;
          IF old_attempt.node_runtime_launch_receipt_sha256 IS NOT NULL OR
             new_attempt.node_runtime_launch_receipt_sha256 IS NOT NULL OR
             (new_attempt.runtime_identity_sha256,
              new_attempt.runtime_identity_json) IS DISTINCT FROM
             (old_attempt.runtime_identity_sha256,
              old_attempt.runtime_identity_json) AND NOT (
               old_attempt.status = 'starting' AND
               new_attempt.status = 'running' AND
               new_attempt.runtime_identity_sha256 IS NOT NULL AND
               EXISTS (
                 SELECT 1
                   FROM execution_external_runtime_launch_receipts l
                  WHERE l.attempt_id = new_attempt.attempt_id
                    AND l.executor_identity_sha256 =
                      new_attempt.runtime_identity_sha256
                    AND l.launch_receipt_json->'launch_evidence'
                        ->'executor_identity' IS NOT DISTINCT FROM
                        new_attempt.runtime_identity_json
               )
             ) THEN
            RAISE EXCEPTION 'external runtime identity is not the exact launch'
              USING ERRCODE = '55000';
          END IF;
          IF (new_attempt.runtime_termination_challenge_count,
              new_attempt.runtime_termination_challenge_sha256) IS DISTINCT FROM
             (old_attempt.runtime_termination_challenge_count,
              old_attempt.runtime_termination_challenge_sha256) AND NOT (
                old_attempt.accepted_runtime_termination_sha256 IS NULL AND
                new_attempt.runtime_termination_challenge_count =
                  old_attempt.runtime_termination_challenge_count + 1 AND
                EXISTS (
                  SELECT 1 FROM execution_external_termination_challenges c
                   WHERE c.attempt_id = new_attempt.attempt_id
                     AND c.challenge_sha256 =
                       new_attempt.runtime_termination_challenge_sha256
                     AND c.challenge_sequence =
                       new_attempt.runtime_termination_challenge_count
                     AND (
                       old_attempt.runtime_termination_challenge_sha256
                         IS NULL OR
                       new_attempt.updated_at >= (
                         SELECT prior.expires_at
                           FROM execution_external_termination_challenges prior
                          WHERE prior.challenge_sha256 =
                            old_attempt.runtime_termination_challenge_sha256
                       )
                     )
                )
             ) THEN
            RAISE EXCEPTION 'runtime termination challenge head is non-monotonic'
              USING ERRCODE = '55000';
          END IF;
          IF old_attempt.accepted_runtime_termination_sha256 IS NOT NULL AND
             new_attempt.accepted_runtime_termination_sha256 IS DISTINCT FROM
               old_attempt.accepted_runtime_termination_sha256 THEN
            RAISE EXCEPTION 'accepted runtime termination pointer is immutable'
              USING ERRCODE = '55000';
          END IF;
          IF old_attempt.accepted_terminal_submission_sha256 IS NOT NULL AND
             new_attempt.accepted_terminal_submission_sha256 IS DISTINCT FROM
               old_attempt.accepted_terminal_submission_sha256 THEN
            RAISE EXCEPTION 'accepted terminal submission pointer is immutable'
              USING ERRCODE = '55000';
          END IF;
          IF old_attempt.terminal_deadline_expiration_sha256 IS NOT NULL AND
             new_attempt.terminal_deadline_expiration_sha256 IS DISTINCT FROM
               old_attempt.terminal_deadline_expiration_sha256 THEN
            RAISE EXCEPTION 'terminal deadline expiration pointer is immutable'
              USING ERRCODE = '55000';
          END IF;
          IF new_attempt.terminal_deadline_expiration_sha256 IS DISTINCT FROM
             old_attempt.terminal_deadline_expiration_sha256 AND NOT (
               old_attempt.status = 'verifying' AND
               new_attempt.status = 'failed' AND
               old_attempt.terminal_deadline_expiration_sha256 IS NULL AND
               old_attempt.accepted_terminal_submission_sha256 IS NULL AND
               new_attempt.accepted_terminal_submission_sha256 IS NULL AND
               new_attempt.accepted_runtime_termination_sha256 IS NOT NULL AND
               EXISTS (
                 SELECT 1
                   FROM
                     execution_external_qualification_deadline_expirations x
                  WHERE x.attempt_id = new_attempt.attempt_id
                    AND x.terminal_deadline_expiration_sha256 =
                      new_attempt.terminal_deadline_expiration_sha256
                    AND x.accepted_runtime_termination_sha256 =
                      new_attempt.accepted_runtime_termination_sha256
                    AND x.activated_at = new_attempt.updated_at
               )
             ) THEN
            RAISE EXCEPTION 'terminal deadline expiration pointer lacks exact activation'
              USING ERRCODE = '55000';
          END IF;
          RETURN new_attempt;
        END;
        $$;
        """
    )


def _install_external_completeness_guard() -> None:
    op.execute(
        r"""
        CREATE FUNCTION aletheia_execution_check_external_v2_attempt(
          target_attempt text
        ) RETURNS void LANGUAGE plpgsql AS $$
        DECLARE
          a execution_attempts%ROWTYPE;
          p execution_external_runtime_preparations%ROWTYPE;
          latest_auth execution_external_launch_authorizations%ROWTYPE;
          launch execution_external_runtime_launch_receipts%ROWTYPE;
          challenge execution_external_termination_challenges%ROWTYPE;
          termination
            execution_external_runtime_termination_acceptances%ROWTYPE;
          expiration
            execution_external_qualification_deadline_expirations%ROWTYPE;
          terminal
            execution_external_qualification_terminal_acceptances%ROWTYPE;
          auth_count bigint;
          challenge_count bigint;
          head_attempt text;
          resource_state text;
          resource_lease_sha text;
          budget_state text;
        BEGIN
          SELECT * INTO a FROM execution_attempts
           WHERE attempt_id = target_attempt;
          IF NOT FOUND OR a.node_id IS NOT NULL THEN RETURN; END IF;
          SELECT * INTO p FROM execution_external_runtime_preparations
           WHERE attempt_id = target_attempt;
          SELECT count(*) INTO auth_count
            FROM execution_external_launch_authorizations
           WHERE attempt_id = target_attempt;
          SELECT * INTO latest_auth
            FROM execution_external_launch_authorizations
           WHERE attempt_id = target_attempt
           ORDER BY sequence DESC LIMIT 1;
          SELECT * INTO launch FROM execution_external_runtime_launch_receipts
           WHERE attempt_id = target_attempt;
          SELECT count(*) INTO challenge_count
            FROM execution_external_termination_challenges
           WHERE attempt_id = target_attempt;
          SELECT * INTO challenge FROM execution_external_termination_challenges
           WHERE attempt_id = target_attempt
           ORDER BY challenge_sequence DESC LIMIT 1;
          SELECT * INTO termination
            FROM execution_external_runtime_termination_acceptances
           WHERE attempt_id = target_attempt;
          SELECT * INTO expiration
            FROM execution_external_qualification_deadline_expirations
           WHERE attempt_id = target_attempt;
          SELECT * INTO terminal
            FROM execution_external_qualification_terminal_acceptances
           WHERE attempt_id = target_attempt;
          SELECT state, lease_sha256 INTO resource_state, resource_lease_sha
            FROM execution_resource_leases
           WHERE attempt_id = target_attempt;
          SELECT state INTO budget_state FROM execution_budget_reservations
           WHERE attempt_id = target_attempt;
          SELECT active_attempt_id INTO head_attempt FROM execution_heads
           WHERE execution_id = a.execution_id;

          -- A node-mode attempt must never carry external custody rows.
          IF EXISTS (
            SELECT 1 FROM execution_attempts n
             WHERE n.attempt_id = target_attempt AND n.node_id IS NOT NULL
          ) THEN
            RAISE EXCEPTION 'external custody rows on a node attempt'
              USING ERRCODE = '23514';
          END IF;
          IF a.node_runtime_launch_receipt_sha256 IS NOT NULL OR
             a.pre_runtime_absence_count <> 0 OR
             a.last_runtime_inspection_sequence <> 0 OR
             a.terminal_receipt_sha256 IS NOT NULL THEN
            RAISE EXCEPTION 'node custody columns set on external attempt'
              USING ERRCODE = '23514';
          END IF;

          IF p.preparation_sha256 IS NOT NULL AND NOT
             aletheia_execution_external_v2_json_valid(
               p.payload_json, 'aletheia.external_runtime_preparation') THEN
            RAISE EXCEPTION 'external preparation JSON is not the closed schema'
              USING ERRCODE = '23514';
          END IF;
          IF p.preparation_sha256 IS NOT NULL AND (
               NOT aletheia_execution_json_string_array(
                 p.payload_json->'workload_argv', false, false) OR
               jsonb_array_length(p.payload_json->'workload_argv')
                 NOT BETWEEN 1 AND 256
             ) THEN
            RAISE EXCEPTION 'external preparation argv is not a closed string array'
              USING ERRCODE = '23514';
          END IF;
          IF EXISTS (
            SELECT 1 FROM execution_external_launch_authorizations x
             WHERE x.attempt_id = target_attempt AND (
               NOT aletheia_execution_runtime_v2_json_valid(
                 x.request_json,
                 'aletheia.runtime_launch_authorization_request') OR
               NOT aletheia_execution_external_v2_json_valid(
                 x.authorization_json, 'aletheia.external_launch_authorization') OR
               NOT aletheia_execution_json_string_array(
                 x.authorization_json->'workload_argv', false, false) OR
               jsonb_array_length(x.authorization_json->'workload_argv')
                 NOT BETWEEN 1 AND 256 OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 x.runtime_control_pin_json,
                 'aletheia.runtime_control_authority_pin'))
          ) THEN
            RAISE EXCEPTION 'external launch authority JSON is not the closed schema'
              USING ERRCODE = '23514';
          END IF;
          IF launch.launch_receipt_sha256 IS NOT NULL AND (
               NOT aletheia_execution_external_v2_json_valid(
                 launch.launch_receipt_json,
                 'aletheia.external_runtime_launch_receipt') OR
               NOT aletheia_execution_external_v2_json_valid(
                 launch.launch_receipt_json->'launch_evidence',
                 'aletheia.external_launch_evidence') OR
               NOT aletheia_execution_external_v2_json_valid(
                 launch.launch_receipt_json->'launch_evidence'
                   ->'executor_identity',
                 'aletheia.external_executor_identity') OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 launch.bridge_pin_json,
                 'aletheia.runtime_control_authority_pin')
             ) THEN
            RAISE EXCEPTION 'external launch JSON is not the closed schema'
              USING ERRCODE = '23514';
          END IF;
          IF EXISTS (
            SELECT 1 FROM execution_external_termination_challenges c
             WHERE c.attempt_id = target_attempt AND (
               NOT aletheia_execution_external_v2_json_valid(
                 c.termination_evidence_json,
                 'aletheia.external_termination_evidence') OR
               NOT aletheia_execution_external_v2_json_valid(
                 c.challenge_json,
                 'aletheia.external_termination_acceptance_challenge') OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 c.runtime_control_pin_json,
                 'aletheia.runtime_control_authority_pin'))
          ) THEN
            RAISE EXCEPTION 'external termination challenge JSON is not the closed schema'
              USING ERRCODE = '23514';
          END IF;
          IF termination.accepted_termination_sha256 IS NOT NULL AND (
               NOT aletheia_execution_external_v2_json_valid(
                 termination.bridge_termination_receipt_json,
                 'aletheia.external_runtime_termination_receipt') OR
               NOT aletheia_execution_external_v2_json_valid(
                 termination.bridge_termination_receipt_json
                   ->'termination_evidence',
                 'aletheia.external_termination_evidence') OR
               NOT aletheia_execution_external_v2_json_valid(
                 termination.accepted_termination_json,
                 'aletheia.accepted_external_runtime_termination') OR
               NOT aletheia_execution_external_v2_json_valid(
                 termination.conditional_terminal_expiration_json,
                 'aletheia.external_qualification_terminal_deadline_expiration') OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 termination.runtime_control_pin_json,
                 'aletheia.runtime_control_authority_pin')
             ) THEN
            RAISE EXCEPTION 'external termination acceptance JSON is not the closed schema'
              USING ERRCODE = '23514';
          END IF;
          IF expiration.terminal_deadline_expiration_sha256 IS NOT NULL AND (
               NOT aletheia_execution_external_v2_json_valid(
                 expiration.payload_json,
                 'aletheia.external_qualification_terminal_deadline_expiration') OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 expiration.runtime_control_pin_json,
                 'aletheia.runtime_control_authority_pin')
             ) THEN
            RAISE EXCEPTION 'external deadline activation JSON is not the closed schema'
              USING ERRCODE = '23514';
          END IF;
          IF terminal.accepted_terminal_submission_sha256 IS NOT NULL AND (
               NOT aletheia_execution_external_v2_json_valid(
                 terminal.terminal_submission_json,
                 'aletheia.external_qualification_terminal_submission') OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 terminal.artifact_manifest_json, 'aletheia.artifact_manifest') OR
               jsonb_typeof(terminal.artifact_verified_receipt_sha256s_json)
                 IS DISTINCT FROM 'array' OR
               jsonb_typeof(terminal.artifact_verified_receipts_json)
                 IS DISTINCT FROM 'array' OR
               NOT aletheia_execution_external_v2_json_valid(
                 terminal.accepted_terminal_submission_json,
                 'aletheia.accepted_external_qualification_terminal_submission') OR
               NOT aletheia_execution_json_string_array(
                 terminal.artifact_verified_receipt_sha256s_json, true, true) OR
               NOT aletheia_execution_json_string_array(
                 terminal.terminal_submission_json
                   ->'artifact_verified_receipt_sha256s', true, true) OR
               NOT aletheia_execution_json_string_array(
                 terminal.accepted_terminal_submission_json
                   ->'artifact_verified_receipt_sha256s', true, true) OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 terminal.runtime_control_pin_json,
                 'aletheia.runtime_control_authority_pin')
             ) THEN
            RAISE EXCEPTION 'external terminal artifact JSON is not the closed schema'
              USING ERRCODE = '23514';
          END IF;
          IF terminal.accepted_terminal_submission_sha256 IS NOT NULL AND EXISTS (
            SELECT 1
              FROM jsonb_array_elements(terminal.artifact_manifest_json->'entries')
                    entry
             WHERE NOT aletheia_execution_runtime_v2_json_valid(
               entry, 'aletheia.artifact_manifest_entry')
          ) THEN
            RAISE EXCEPTION 'external artifact manifest entry JSON is not the closed schema'
              USING ERRCODE = '23514';
          END IF;
          IF EXISTS (
            SELECT 1 FROM execution_qualification_terminal_outbox o
             WHERE o.attempt_id = target_attempt AND (
               (o.terminal_authority_kind = 'accepted_terminal_submission' AND
                NOT aletheia_execution_external_v2_json_valid(
                  o.payload_json,
                  'aletheia.accepted_external_qualification_terminal_submission')
               ) OR
               (o.terminal_authority_kind = 'terminal_deadline_expiration' AND
                NOT aletheia_execution_external_v2_json_valid(
                  o.payload_json,
                  'aletheia.external_qualification_terminal_deadline_expiration')
               ) OR
               o.terminal_authority_kind NOT IN (
                 'accepted_terminal_submission',
                 'terminal_deadline_expiration')
             )
          ) THEN
            RAISE EXCEPTION 'external terminal outbox JSON is not the closed schema'
              USING ERRCODE = '23514';
          END IF;

          IF (p.preparation_sha256 IS NULL) IS DISTINCT FROM
               (a.runtime_preparation_sha256 IS NULL) OR
             p.preparation_sha256 IS DISTINCT FROM a.runtime_preparation_sha256 OR
             auth_count IS DISTINCT FROM a.runtime_launch_authorization_count OR
             (auth_count > 0 AND (
               latest_auth.sequence IS DISTINCT FROM auth_count OR
               latest_auth.authorization_sha256 IS DISTINCT FROM
                 a.latest_runtime_launch_authorization_sha256)) OR
             (launch.launch_receipt_sha256 IS NULL) IS DISTINCT FROM
               (a.runtime_identity_sha256 IS NULL) OR
             (launch.launch_receipt_sha256 IS NOT NULL AND (
               launch.executor_identity_sha256 IS DISTINCT FROM
                 a.runtime_identity_sha256 OR
               launch.launch_receipt_json->'launch_evidence'
                 ->'executor_identity' IS DISTINCT FROM
                 a.runtime_identity_json)) OR
             challenge_count IS DISTINCT FROM
               a.runtime_termination_challenge_count OR
             (challenge_count > 0 AND (
               challenge.challenge_sha256 IS DISTINCT FROM
                 a.runtime_termination_challenge_sha256 OR
               (SELECT min(c.challenge_sequence)
                  FROM execution_external_termination_challenges c
                 WHERE c.attempt_id = target_attempt) IS DISTINCT FROM 1 OR
               (SELECT max(c.challenge_sequence)
                  FROM execution_external_termination_challenges c
                 WHERE c.attempt_id = target_attempt)
                 IS DISTINCT FROM challenge_count)) OR
             termination.accepted_termination_sha256 IS DISTINCT FROM
               a.accepted_runtime_termination_sha256 OR
             terminal.accepted_terminal_submission_sha256 IS DISTINCT FROM
               a.accepted_terminal_submission_sha256 OR
             expiration.terminal_deadline_expiration_sha256 IS DISTINCT FROM
               a.terminal_deadline_expiration_sha256 THEN
            RAISE EXCEPTION 'external child rows differ from attempt heads'
              USING ERRCODE = '23514';
          END IF;

          IF p.preparation_sha256 IS NOT NULL AND (
               p.execution_id IS DISTINCT FROM a.execution_id OR
               p.intent_sha256 IS DISTINCT FROM a.intent_sha256 OR
               p.fencing_epoch > a.fencing_epoch OR
               p.payload_sha256 IS DISTINCT FROM p.preparation_sha256 OR
               p.payload_json->>'infrastructure_attempt_id' IS DISTINCT FROM
                 a.attempt_id OR
               p.payload_json->>'execution_id' IS DISTINCT FROM a.execution_id OR
               p.payload_json->>'intent_sha256' IS DISTINCT FROM a.intent_sha256 OR
               p.payload_json->>'bridge_manifest_sha256' IS DISTINCT FROM
                 p.bridge_manifest_sha256 OR
               (p.payload_json->>'fencing_epoch')::bigint
                 IS DISTINCT FROM p.fencing_epoch OR
               p.payload_json->>'lease_token_sha256' IS DISTINCT FROM
                 p.lease_token_sha256 OR
               (p.payload_json->>'prepared_at')::timestamptz
                 IS DISTINCT FROM p.prepared_at OR
               (p.payload_json->>'prepared_monotonic_ns')::bigint
                 IS DISTINCT FROM p.prepared_monotonic_ns OR
               p.recorded_at < p.prepared_at OR
               p.payload_json->>'qualification_only' IS DISTINCT FROM 'true' OR
               p.payload_json->>'scientific_admission_allowed'
                 IS DISTINCT FROM 'false'
             ) THEN
            RAISE EXCEPTION 'external preparation differs from exact attempt authority'
              USING ERRCODE = '23514';
          END IF;

          IF EXISTS (
            SELECT 1 FROM execution_external_launch_authorizations x
             WHERE x.attempt_id = target_attempt AND (
               x.preparation_sha256 IS DISTINCT FROM p.preparation_sha256 OR
               x.request_payload_sha256 IS DISTINCT FROM x.request_sha256 OR
               x.authorization_payload_sha256 IS DISTINCT FROM
                 x.authorization_sha256 OR
               x.request_json->>'runtime_preparation_sha256' IS DISTINCT FROM
                 p.preparation_sha256 OR
               x.request_json->>'infrastructure_attempt_id' IS DISTINCT FROM
                 target_attempt OR
               (x.request_json->>'fencing_epoch')::bigint
                 IS DISTINCT FROM p.fencing_epoch OR
               x.request_json->>'lease_token_sha256' IS DISTINCT FROM
                 p.lease_token_sha256 OR
               (x.request_json->>'requested_at')::timestamptz < p.prepared_at OR
               (x.request_json->>'requested_at')::timestamptz > x.issued_at OR
               (x.request_json->>'requested_monotonic_ns')::bigint <
                 p.prepared_monotonic_ns OR
               x.authorization_json->>'authorization_request_sha256'
                 IS DISTINCT FROM x.request_sha256 OR
               x.authorization_json->>'runtime_preparation_sha256'
                 IS DISTINCT FROM p.preparation_sha256 OR
               x.authorization_json->>'infrastructure_attempt_id' IS DISTINCT FROM
                 target_attempt OR
               x.authorization_json->>'execution_id' IS DISTINCT FROM
                 a.execution_id OR
               x.authorization_json->>'intent_sha256' IS DISTINCT FROM
                 a.intent_sha256 OR
               x.authorization_json->>'admission_sha256' IS DISTINCT FROM
                 a.admission_sha256 OR
               x.authorization_json->>'qualification_grant_sha256' IS DISTINCT FROM
                 a.grant_sha256 OR
               x.authorization_json->>'bridge_manifest_sha256' IS DISTINCT FROM
                 p.bridge_manifest_sha256 OR
               x.authorization_json->>'launch_spec_sha256' IS DISTINCT FROM
                 p.payload_json->>'launch_spec_sha256' OR
               x.authorization_json->>'workload_executable_sha256' IS DISTINCT FROM
                 p.payload_json->>'workload_executable_sha256' OR
               x.authorization_json->'workload_argv' IS DISTINCT FROM
                 p.payload_json->'workload_argv' OR
               x.authorization_json->>'enforced_placement_sha256' IS DISTINCT FROM
                 p.payload_json->>'enforced_placement_sha256' OR
               x.authorization_json->>'input_materialization_receipt_sha256'
                 IS DISTINCT FROM
                   p.payload_json->>'input_materialization_receipt_sha256' OR
               (x.authorization_json->>'fencing_epoch')::bigint
                 IS DISTINCT FROM p.fencing_epoch OR
               x.authorization_json->>'lease_token_sha256' IS DISTINCT FROM
                 p.lease_token_sha256 OR
               (x.authorization_json->>'lease_expires_at')::timestamptz >
                 a.lease_expires_at OR
               (x.authorization_json->>'hard_deadline')::timestamptz
                 IS DISTINCT FROM a.hard_deadline OR
               (x.authorization_json->>'issued_at')::timestamptz
                 IS DISTINCT FROM x.issued_at OR
               (x.authorization_json->>'expires_at')::timestamptz
                 IS DISTINCT FROM x.expires_at OR
               (x.authorization_json->>'expires_at')::timestamptz >
                 (x.authorization_json->>'lease_expires_at')::timestamptz OR
               (x.authorization_json->>'lease_expires_at')::timestamptz >
                 (x.authorization_json->>'hard_deadline')::timestamptz OR
               (x.authorization_json->>'max_launch_delay_ns')::bigint
                 NOT BETWEEN 1 AND 60000000000 OR
               x.recorded_at < x.issued_at OR
               x.recorded_at >= x.expires_at OR
               x.issued_at <
                 (x.runtime_control_pin_json->>'valid_from')::timestamptz OR
               x.issued_at >= LEAST(
                 (x.runtime_control_pin_json->>'expires_at')::timestamptz,
                 COALESCE(
                   (x.runtime_control_pin_json->>'revoked_at')::timestamptz,
                   (x.runtime_control_pin_json->>'expires_at')::timestamptz)) OR
               x.expires_at > LEAST(
                 (x.runtime_control_pin_json->>'expires_at')::timestamptz,
                 COALESCE(
                   (x.runtime_control_pin_json->>'revoked_at')::timestamptz,
                   (x.runtime_control_pin_json->>'expires_at')::timestamptz)) OR
               x.runtime_control_pin_json->>'policy_sha256' IS DISTINCT FROM
                 x.authorization_json->>'runtime_control_policy_sha256' OR
               x.runtime_control_pin_json->>'principal_id' IS DISTINCT FROM
                 x.authorization_json->>'authorized_by_principal_id' OR
               x.runtime_control_pin_json->>'key_id' IS DISTINCT FROM
                 x.authorization_json->>'authorization_key_id' OR
               x.authorization_json->>'qualification_only'
                 IS DISTINCT FROM 'true' OR
               x.authorization_json->>'scientific_admission_allowed'
                 IS DISTINCT FROM 'false'
             )
          ) THEN
            RAISE EXCEPTION 'external launch authorization lineage is rebound'
              USING ERRCODE = '23514';
          END IF;

          IF launch.launch_receipt_sha256 IS NOT NULL AND (
               launch.preparation_sha256 IS DISTINCT FROM p.preparation_sha256 OR
               launch.authorization_request_sha256 IS DISTINCT FROM
                 latest_auth.request_sha256 OR
               launch.authorization_sha256 IS DISTINCT FROM
                 latest_auth.authorization_sha256 OR
               launch.launch_payload_sha256 IS DISTINCT FROM
                 launch.launch_receipt_sha256 OR
               launch.launch_receipt_json->>'bridge_manifest_sha256'
                 IS DISTINCT FROM p.bridge_manifest_sha256 OR
               launch.launch_receipt_json->'launch_evidence'
                 ->>'preparation_sha256' IS DISTINCT FROM p.preparation_sha256 OR
               launch.launch_receipt_json->'launch_evidence'
                 ->>'external_launch_authorization_sha256' IS DISTINCT FROM
                 latest_auth.authorization_sha256 OR
               launch.launch_receipt_json->'launch_evidence'
                 ->>'executor_identity_sha256' IS DISTINCT FROM
                 launch.executor_identity_sha256 OR
               launch.launch_receipt_json->'launch_evidence'
                 ->'executor_identity' IS DISTINCT FROM a.runtime_identity_json OR
               launch.launch_receipt_json->'launch_evidence'
                 ->'executor_identity'->>'execution_id' IS DISTINCT FROM
                 a.execution_id OR
               launch.launch_receipt_json->'launch_evidence'
                 ->'executor_identity'->>'infrastructure_attempt_id'
                 IS DISTINCT FROM target_attempt OR
               launch.launch_receipt_json->'launch_evidence'
                 ->'executor_identity'->>'runtime_id' IS DISTINCT FROM
                 p.payload_json->>'runtime_id' OR
               (launch.launch_receipt_json->'launch_evidence'
                 ->'executor_identity'->>'started_at')::timestamptz
                 < p.prepared_at OR
               (launch.launch_receipt_json->'launch_evidence'
                 ->'executor_identity'->>'started_monotonic_ns')::bigint
                 < p.prepared_monotonic_ns OR
               launch.launch_receipt_json->'launch_evidence'
                 ->>'enforced_placement_sha256' IS DISTINCT FROM
                 p.payload_json->>'enforced_placement_sha256' OR
               launch.launch_receipt_json->'launch_evidence'
                 ->>'input_materialization_receipt_sha256' IS DISTINCT FROM
                 p.payload_json->>'input_materialization_receipt_sha256' OR
               (launch.launch_receipt_json->'launch_evidence'
                 ->>'enforced_fencing_epoch')::bigint
                 IS DISTINCT FROM p.fencing_epoch OR
               launch.launch_receipt_json->'launch_evidence'
                 ->>'enforced_lease_token_sha256' IS DISTINCT FROM
                 p.lease_token_sha256 OR
               (launch.launch_receipt_json->'launch_evidence'
                 ->>'executor_start_monotonic_lower_bound_ns')::bigint
                 IS DISTINCT FROM
                 (launch.launch_receipt_json->'launch_evidence'
                   ->'executor_identity'->>'started_monotonic_ns')::bigint OR
               (launch.launch_receipt_json->'launch_evidence'
                 ->'executor_identity'->>'started_monotonic_ns')::bigint >=
                 (launch.launch_receipt_json->'launch_evidence'->>
                   'executor_start_monotonic_upper_bound_exclusive_ns')::bigint OR
               (launch.launch_receipt_json->'launch_evidence'
                 ->>'observed_monotonic_ns')::bigint <
                 (launch.launch_receipt_json->'launch_evidence'->>
                   'executor_start_monotonic_upper_bound_exclusive_ns')::bigint OR
               (launch.launch_receipt_json->'launch_evidence'->>
                   'executor_start_monotonic_lower_bound_ns')::bigint <
                 (latest_auth.request_json->>'requested_monotonic_ns')::bigint OR
               (launch.launch_receipt_json->'launch_evidence'->>
                   'executor_start_monotonic_upper_bound_exclusive_ns')::bigint >
                 (latest_auth.request_json->>'requested_monotonic_ns')::bigint +
                 (latest_auth.authorization_json->>'max_launch_delay_ns')::bigint OR
               (launch.launch_receipt_json->'launch_evidence'
                 ->'executor_identity'->>'started_at')::timestamptz
                 < latest_auth.issued_at OR
               (launch.launch_receipt_json->'launch_evidence'
                 ->'executor_identity'->>'started_at')::timestamptz
                 >= latest_auth.expires_at OR
               (launch.launch_receipt_json->'launch_evidence'
                 ->>'observed_at')::timestamptz <
                 (launch.launch_receipt_json->'launch_evidence'
                   ->'executor_identity'->>'started_at')::timestamptz OR
               (launch.launch_receipt_json->>'signed_at')::timestamptz
                 IS DISTINCT FROM launch.signed_at OR
               launch.signed_at <
                 (launch.launch_receipt_json->'launch_evidence'
                   ->>'observed_at')::timestamptz OR
               launch.signed_at -
                 (launch.launch_receipt_json->'launch_evidence'
                   ->>'observed_at')::timestamptz > interval '60 seconds' OR
               launch.accepted_at < launch.signed_at OR
               launch.bridge_pin_json->>'key_id' IS DISTINCT FROM
                 launch.launch_receipt_json->>'signing_key_id' OR
               launch.signed_at <
                 (launch.bridge_pin_json->>'valid_from')::timestamptz OR
               launch.signed_at >= LEAST(
                 (launch.bridge_pin_json->>'expires_at')::timestamptz,
                 COALESCE(
                   (launch.bridge_pin_json->>'revoked_at')::timestamptz,
                   (launch.bridge_pin_json->>'expires_at')::timestamptz))
             ) THEN
            RAISE EXCEPTION 'external launch authority is incomplete'
              USING ERRCODE = '23514';
          END IF;

          IF EXISTS (
            SELECT 1 FROM execution_external_termination_challenges c
             WHERE c.attempt_id = target_attempt AND (
               c.preparation_sha256 IS DISTINCT FROM p.preparation_sha256 OR
               c.launch_receipt_sha256 IS DISTINCT FROM
                 launch.launch_receipt_sha256 OR
               c.executor_identity_sha256 IS DISTINCT FROM
                 a.runtime_identity_sha256 OR
               c.challenge_payload_sha256 IS DISTINCT FROM c.challenge_sha256 OR
               c.challenge_json->>'challenge_id' IS DISTINCT FROM c.challenge_id OR
               c.challenge_json->>'attempt_id' IS DISTINCT FROM target_attempt OR
               c.challenge_json->>'execution_id' IS DISTINCT FROM
                 a.execution_id OR
               c.challenge_json->>'intent_sha256' IS DISTINCT FROM
                 a.intent_sha256 OR
               c.challenge_json->>'bridge_manifest_sha256' IS DISTINCT FROM
                 p.bridge_manifest_sha256 OR
               c.challenge_json->>'runtime_preparation_sha256' IS DISTINCT FROM
                 p.preparation_sha256 OR
               c.challenge_json->>'external_runtime_launch_receipt_sha256'
                 IS DISTINCT FROM launch.launch_receipt_sha256 OR
               c.challenge_json->>'executor_identity_sha256' IS DISTINCT FROM
                 a.runtime_identity_sha256 OR
               c.challenge_json->>'termination_evidence_sha256' IS DISTINCT FROM
                 c.termination_evidence_sha256 OR
               c.challenge_json->>'result_content_sha256' IS DISTINCT FROM
                 c.termination_evidence_json->>'result_content_sha256' OR
               c.challenge_json->>'resource_lease_sha256' IS DISTINCT FROM
                 resource_lease_sha OR
               (c.challenge_json->>'fencing_epoch')::bigint
                 IS DISTINCT FROM p.fencing_epoch OR
               c.challenge_json->>'lease_token_sha256' IS DISTINCT FROM
                 p.lease_token_sha256 OR
               (c.challenge_json->>'hard_deadline')::timestamptz
                 IS DISTINCT FROM a.hard_deadline OR
               (c.challenge_json->>'artifact_submission_deadline')::timestamptz
                 < c.challenged_at OR
               (c.challenge_json->>'challenged_at')::timestamptz
                 IS DISTINCT FROM c.challenged_at OR
               (c.challenge_json->>'expires_at')::timestamptz
                 IS DISTINCT FROM c.expires_at OR
               c.termination_evidence_json->>'preparation_sha256' IS DISTINCT FROM
                 p.preparation_sha256 OR
               c.termination_evidence_json->>'external_launch_receipt_sha256'
                 IS DISTINCT FROM launch.launch_receipt_sha256 OR
               c.termination_evidence_json->>'executor_identity_sha256'
                 IS DISTINCT FROM a.runtime_identity_sha256 OR
               (c.termination_evidence_json->>'exit_code')::int NOT BETWEEN 0
                 AND 255 OR
               (c.termination_evidence_json->>'ended_at')::timestamptz <
                 (launch.launch_receipt_json->'launch_evidence'
                   ->'executor_identity'->>'started_at')::timestamptz OR
               (c.termination_evidence_json->>'ended_monotonic_ns')::bigint <
                 (launch.launch_receipt_json->'launch_evidence'
                   ->'executor_identity'->>'started_monotonic_ns')::bigint OR
               (c.termination_evidence_json->>'ended_at')::timestamptz >
                 c.challenged_at OR
               c.runtime_control_pin_json->>'policy_sha256' IS DISTINCT FROM
                 c.challenge_json->>'runtime_control_policy_sha256' OR
               c.runtime_control_pin_json->>'principal_id' IS DISTINCT FROM
                 c.challenge_json->>'challenged_by_principal_id' OR
               c.runtime_control_pin_json->>'key_id' IS DISTINCT FROM
                 c.challenge_json->>'challenge_key_id' OR
               c.challenged_at <
                 (c.runtime_control_pin_json->>'valid_from')::timestamptz OR
               c.challenged_at >= LEAST(
                 (c.runtime_control_pin_json->>'expires_at')::timestamptz,
                 COALESCE(
                   (c.runtime_control_pin_json->>'revoked_at')::timestamptz,
                   (c.runtime_control_pin_json->>'expires_at')::timestamptz))
             )
          ) THEN
            RAISE EXCEPTION 'external termination challenge lineage is rebound'
              USING ERRCODE = '23514';
          END IF;

          IF termination.accepted_termination_sha256 IS NOT NULL AND (
               termination.challenge_sha256 IS DISTINCT FROM
                 challenge.challenge_sha256 OR
               termination.preparation_sha256 IS DISTINCT FROM
                 p.preparation_sha256 OR
               termination.launch_receipt_sha256 IS DISTINCT FROM
                 launch.launch_receipt_sha256 OR
               termination.authorization_request_sha256 IS DISTINCT FROM
                 latest_auth.request_sha256 OR
               termination.authorization_sha256 IS DISTINCT FROM
                 latest_auth.authorization_sha256 OR
               termination.receipt_payload_sha256 IS DISTINCT FROM
                 termination.bridge_termination_receipt_sha256 OR
               termination.acceptance_payload_sha256 IS DISTINCT FROM
                 termination.accepted_termination_sha256 OR
               termination.bridge_termination_receipt_json->>'challenge_sha256'
                 IS DISTINCT FROM termination.challenge_sha256 OR
               termination.bridge_termination_receipt_json
                 ->>'runtime_preparation_sha256' IS DISTINCT FROM
                 p.preparation_sha256 OR
               termination.bridge_termination_receipt_json
                 ->>'external_runtime_launch_receipt_sha256' IS DISTINCT FROM
                 launch.launch_receipt_sha256 OR
               termination.bridge_termination_receipt_json
                 ->>'runtime_launch_authorization_request_sha256' IS DISTINCT FROM
                 latest_auth.request_sha256 OR
               termination.bridge_termination_receipt_json
                 ->>'external_launch_authorization_sha256' IS DISTINCT FROM
                 latest_auth.authorization_sha256 OR
               termination.bridge_termination_receipt_json
                 ->>'termination_evidence_sha256' IS DISTINCT FROM
                 termination.termination_evidence_sha256 OR
               termination.bridge_termination_receipt_json
                 ->'termination_evidence'->>'preparation_sha256' IS DISTINCT FROM
                 p.preparation_sha256 OR
               termination.bridge_termination_receipt_json
                 ->'termination_evidence'->>'external_launch_receipt_sha256'
                 IS DISTINCT FROM launch.launch_receipt_sha256 OR
               termination.bridge_termination_receipt_json
                 ->'termination_evidence'->>'executor_identity_sha256'
                 IS DISTINCT FROM a.runtime_identity_sha256 OR
               (termination.bridge_termination_receipt_json
                 ->'termination_evidence'->>'exit_code')::int IS DISTINCT FROM
                 termination.exit_code OR
               (termination.bridge_termination_receipt_json
                 ->'termination_evidence'->>'ended_at')::timestamptz
                 IS DISTINCT FROM termination.runtime_ended_at OR
               (termination.bridge_termination_receipt_json
                 ->'termination_evidence'->>'result_content_sha256')
                 IS DISTINCT FROM termination.result_content_sha256 OR
               (termination.bridge_termination_receipt_json->>'signed_at')
                 ::timestamptz IS DISTINCT FROM
                 (termination.accepted_termination_json
                   ->>'proof_signed_at')::timestamptz OR
               (termination.bridge_termination_receipt_json->>'expires_at')
                 ::timestamptz IS DISTINCT FROM
                 (termination.accepted_termination_json
                   ->>'proof_expires_at')::timestamptz OR
               termination.accepted_termination_json->>'challenge_sha256'
                 IS DISTINCT FROM termination.challenge_sha256 OR
               termination.accepted_termination_json->>'attempt_id' IS DISTINCT FROM
                 target_attempt OR
               termination.accepted_termination_json
                 ->>'runtime_preparation_sha256' IS DISTINCT FROM
                 p.preparation_sha256 OR
               termination.accepted_termination_json
                 ->>'external_runtime_launch_receipt_sha256' IS DISTINCT FROM
                 launch.launch_receipt_sha256 OR
               termination.accepted_termination_json
                 ->>'runtime_launch_authorization_request_sha256' IS DISTINCT FROM
                 latest_auth.request_sha256 OR
               termination.accepted_termination_json
                 ->>'external_launch_authorization_sha256' IS DISTINCT FROM
                 latest_auth.authorization_sha256 OR
               termination.accepted_termination_json
                 ->>'external_runtime_termination_receipt_sha256' IS DISTINCT FROM
                 termination.bridge_termination_receipt_sha256 OR
               termination.accepted_termination_json->>'executor_identity_sha256'
                 IS DISTINCT FROM a.runtime_identity_sha256 OR
               termination.accepted_termination_json
                 ->>'termination_evidence_sha256' IS DISTINCT FROM
                 termination.termination_evidence_sha256 OR
               termination.accepted_termination_json->>'result_content_sha256'
                 IS DISTINCT FROM termination.result_content_sha256 OR
               (termination.accepted_termination_json->>'fencing_epoch')::bigint
                 IS DISTINCT FROM p.fencing_epoch OR
               termination.accepted_termination_json->>'lease_token_sha256'
                 IS DISTINCT FROM p.lease_token_sha256 OR
               (termination.accepted_termination_json->>'runtime_ended_at')
                 ::timestamptz IS DISTINCT FROM termination.runtime_ended_at OR
               (termination.accepted_termination_json->>'exit_code')::int
                 IS DISTINCT FROM termination.exit_code OR
               (termination.accepted_termination_json->>'hard_deadline')
                 ::timestamptz IS DISTINCT FROM a.hard_deadline OR
               (termination.accepted_termination_json
                 ->>'artifact_submission_deadline')::timestamptz
                 IS DISTINCT FROM
                 (challenge.challenge_json->>'artifact_submission_deadline')
                 ::timestamptz OR
               (termination.accepted_termination_json->>'accepted_at')
                 ::timestamptz IS DISTINCT FROM termination.accepted_at OR
               (termination.accepted_termination_json->>'billable_ended_at')
                 ::timestamptz < termination.runtime_ended_at OR
               termination.accepted_termination_json
                 ->>'proof_was_fresh' IS DISTINCT FROM 'true' OR
               termination.accepted_termination_json
                 ->>'compute_release_allowed' IS DISTINCT FROM 'true' OR
               termination.conditional_terminal_expiration_payload_sha256
                 IS DISTINCT FROM
                 termination.conditional_terminal_expiration_sha256 OR
               termination.conditional_terminal_expiration_json
                 ->>'accepted_external_runtime_termination_sha256'
                 IS DISTINCT FROM termination.accepted_termination_sha256 OR
               (termination.conditional_terminal_expiration_json
                 ->>'authorized_at')::timestamptz IS DISTINCT FROM
                 termination.conditional_terminal_expiration_authorized_at OR
               (termination.conditional_terminal_expiration_json
                 ->>'expired_at')::timestamptz IS DISTINCT FROM
                 termination.conditional_terminal_expiration_expires_at OR
               (termination.conditional_terminal_expiration_json
                 ->>'expired_at')::timestamptz IS DISTINCT FROM
                 (termination.accepted_termination_json
                   ->>'artifact_submission_deadline')::timestamptz OR
               (termination.conditional_terminal_expiration_json
                 ->>'authorized_at')::timestamptz IS DISTINCT FROM
                 termination.accepted_at OR
               termination.runtime_control_pin_json->>'policy_sha256'
                 IS DISTINCT FROM
                 termination.accepted_termination_json
                   ->>'runtime_control_policy_sha256' OR
               termination.runtime_control_pin_json->>'principal_id'
                 IS DISTINCT FROM
                 termination.accepted_termination_json
                   ->>'accepted_by_principal_id' OR
               termination.runtime_control_pin_json->>'key_id' IS DISTINCT FROM
                 termination.accepted_termination_json->>'acceptance_key_id' OR
               termination.accepted_at <
                 (termination.accepted_termination_json
                   ->>'proof_signed_at')::timestamptz OR
               termination.accepted_at >=
                 (termination.accepted_termination_json
                   ->>'proof_expires_at')::timestamptz OR
               termination.accepted_at >=
                 (termination.accepted_termination_json
                   ->>'artifact_submission_deadline')::timestamptz OR
               termination.accepted_at <
                 (termination.runtime_control_pin_json->>'valid_from')
                 ::timestamptz OR
               termination.accepted_at >= LEAST(
                 (termination.runtime_control_pin_json->>'expires_at')
                   ::timestamptz,
                 COALESCE(
                   (termination.runtime_control_pin_json->>'revoked_at')
                     ::timestamptz,
                   (termination.runtime_control_pin_json->>'expires_at')
                     ::timestamptz)) OR
               (termination.accepted_termination_json
                 ->>'artifact_submission_deadline')::timestamptz > LEAST(
                 (termination.runtime_control_pin_json->>'expires_at')
                   ::timestamptz,
                 COALESCE(
                   (termination.runtime_control_pin_json->>'revoked_at')
                     ::timestamptz,
                   (termination.runtime_control_pin_json->>'expires_at')
                     ::timestamptz))
             ) THEN
            RAISE EXCEPTION 'external termination acceptance differs from full proof'
              USING ERRCODE = '23514';
          END IF;

          IF terminal.accepted_terminal_submission_sha256 IS NOT NULL AND (
               terminal.accepted_runtime_termination_sha256 IS DISTINCT FROM
                 termination.accepted_termination_sha256 OR
               terminal.bridge_manifest_sha256 IS DISTINCT FROM
                 p.bridge_manifest_sha256 OR
               terminal.submission_payload_sha256 IS DISTINCT FROM
                 terminal.terminal_submission_sha256 OR
               terminal.manifest_payload_sha256 IS DISTINCT FROM
                 terminal.artifact_manifest_sha256 OR
               terminal.acceptance_payload_sha256 IS DISTINCT FROM
                 terminal.accepted_terminal_submission_sha256 OR
               terminal.terminal_submission_json->>'bridge_manifest_sha256'
                 IS DISTINCT FROM p.bridge_manifest_sha256 OR
               terminal.terminal_submission_json->>'intent_sha256' IS DISTINCT FROM
                 a.intent_sha256 OR
               terminal.terminal_submission_json->>'execution_id' IS DISTINCT FROM
                 a.execution_id OR
               terminal.terminal_submission_json->>'attempt_id' IS DISTINCT FROM
                 target_attempt OR
               terminal.terminal_submission_json->>'resource_lease_sha256'
                 IS DISTINCT FROM resource_lease_sha OR
               (terminal.terminal_submission_json->>'fencing_epoch')::bigint
                 IS DISTINCT FROM p.fencing_epoch OR
               terminal.terminal_submission_json->>'lease_token_sha256'
                 IS DISTINCT FROM p.lease_token_sha256 OR
               terminal.terminal_submission_json
                 ->>'accepted_external_runtime_termination_sha256' IS DISTINCT FROM
                 termination.accepted_termination_sha256 OR
               terminal.terminal_submission_json->>'artifact_manifest_sha256'
                 IS DISTINCT FROM terminal.artifact_manifest_sha256 OR
               terminal.terminal_submission_json->>'output_tree_sha256'
                 IS DISTINCT FROM terminal.output_tree_sha256 OR
               terminal.terminal_submission_json->'artifact_verified_receipt_sha256s'
                 IS DISTINCT FROM
                 terminal.artifact_verified_receipt_sha256s_json OR
               (terminal.terminal_submission_json->>'submitted_at')::timestamptz
                 < termination.accepted_at OR
               (terminal.terminal_submission_json->>'submitted_at')::timestamptz >=
                 (termination.accepted_termination_json
                   ->>'artifact_submission_deadline')::timestamptz OR
               terminal.terminal_submission_json->>'disposition' IS DISTINCT FROM
                 terminal.disposition OR
               terminal.terminal_submission_json->>'qualification_only'
                 IS DISTINCT FROM 'true' OR
               terminal.terminal_submission_json->>'scientific_admission_allowed'
                 IS DISTINCT FROM 'false' OR
               terminal.artifact_manifest_json->>'intent_sha256' IS DISTINCT FROM
                 a.intent_sha256 OR
               terminal.artifact_manifest_json->>'execution_id' IS DISTINCT FROM
                 a.execution_id OR
               terminal.artifact_manifest_json->>'infrastructure_attempt_id'
                 IS DISTINCT FROM target_attempt OR
               terminal.artifact_manifest_json->>'replicate_slot_id'
                 IS DISTINCT FROM a.intent_json->'infrastructure_attempt'->>
                   'replicate_slot_id' OR
               (terminal.artifact_manifest_json->>'produced_at')::timestamptz
                 IS DISTINCT FROM termination.runtime_ended_at OR
               (SELECT count(*)
                  FROM jsonb_array_elements(
                    terminal.artifact_manifest_json->'entries') entry)
                 IS DISTINCT FROM
               (SELECT count(DISTINCT entry->>'artifact_key')
                  FROM jsonb_array_elements(
                    terminal.artifact_manifest_json->'entries') entry) OR
               terminal.artifact_manifest_json->'entries' IS DISTINCT FROM
                 COALESCE(
                   (SELECT jsonb_agg(entry ORDER BY entry->>'artifact_key')
                      FROM jsonb_array_elements(
                        terminal.artifact_manifest_json->'entries') entry),
                   '[]'::jsonb) OR
               EXISTS (
                 SELECT 1
                   FROM jsonb_array_elements(
                     terminal.artifact_verified_receipts_json) receipt
                  WHERE NOT aletheia_execution_runtime_v2_json_valid(
                     receipt, 'aletheia.artifact_verified_receipt') OR
                    NOT aletheia_execution_runtime_v2_json_valid(
                     receipt->'artifact', 'aletheia.artifact_manifest_entry') OR
                    NOT aletheia_execution_json_string_array(
                     receipt->'custody_receipt_sha256s', true, true) OR
                    (receipt->>'custody_mode' IS NOT DISTINCT FROM
                       'site_local_attested' AND
                     jsonb_array_length(receipt->'custody_receipt_sha256s') = 0) OR
                    receipt->>'artifact_manifest_sha256' IS DISTINCT FROM
                      terminal.artifact_manifest_sha256 OR
                    receipt->>'producer_attempt_id' IS DISTINCT FROM
                      target_attempt OR
                    NOT EXISTS (
                      SELECT 1
                        FROM jsonb_array_elements(
                          terminal.artifact_manifest_json->'entries') entry
                       WHERE entry IS NOT DISTINCT FROM receipt->'artifact'
                    )
               ) OR
               jsonb_array_length(terminal.artifact_verified_receipts_json)
                 IS DISTINCT FROM
                   jsonb_array_length(
                     terminal.artifact_manifest_json->'entries') OR
               (SELECT jsonb_agg(receipt->'artifact'->'artifact_key' ORDER BY ordinal)
                  FROM jsonb_array_elements(
                    terminal.artifact_verified_receipts_json)
                         WITH ORDINALITY AS receipts(receipt, ordinal))
                 IS DISTINCT FROM
               (SELECT jsonb_agg(entry->'artifact_key' ORDER BY ordinal)
                  FROM jsonb_array_elements(
                    terminal.artifact_manifest_json->'entries')
                         WITH ORDINALITY AS entries(entry, ordinal)) OR
               terminal.artifact_verified_receipt_sha256s_json IS DISTINCT FROM
                 COALESCE(
                   (SELECT jsonb_agg(sha ORDER BY sha)
                      FROM jsonb_array_elements_text(
                        terminal.artifact_verified_receipt_sha256s_json)
                            AS items(sha)),
                   '[]'::jsonb) OR
               terminal.accepted_terminal_submission_json
                 ->>'terminal_submission_sha256' IS DISTINCT FROM
                 terminal.terminal_submission_sha256 OR
               terminal.accepted_terminal_submission_json
                 ->>'accepted_external_runtime_termination_sha256' IS DISTINCT FROM
                 termination.accepted_termination_sha256 OR
               terminal.accepted_terminal_submission_json
                 ->>'bridge_manifest_sha256' IS DISTINCT FROM
                 p.bridge_manifest_sha256 OR
               terminal.accepted_terminal_submission_json
                 ->>'artifact_manifest_sha256' IS DISTINCT FROM
                 terminal.artifact_manifest_sha256 OR
               terminal.accepted_terminal_submission_json
                 ->>'output_tree_sha256' IS DISTINCT FROM
                 terminal.output_tree_sha256 OR
               terminal.accepted_terminal_submission_json
                 ->'artifact_verified_receipt_sha256s' IS DISTINCT FROM
                 terminal.artifact_verified_receipt_sha256s_json OR
               terminal.accepted_terminal_submission_json->>'disposition'
                 IS DISTINCT FROM terminal.disposition OR
               (terminal.accepted_terminal_submission_json
                 ->>'bridge_submitted_at')::timestamptz IS DISTINCT FROM
                 (terminal.terminal_submission_json->>'submitted_at')
                 ::timestamptz OR
               (terminal.accepted_terminal_submission_json
                 ->>'artifact_submission_deadline')::timestamptz IS DISTINCT FROM
                 (termination.accepted_termination_json
                   ->>'artifact_submission_deadline')::timestamptz OR
               (terminal.accepted_terminal_submission_json->>'accepted_at')
                 ::timestamptz IS DISTINCT FROM terminal.accepted_at OR
               terminal.accepted_at <
                 (terminal.terminal_submission_json->>'submitted_at')
                 ::timestamptz OR
               terminal.accepted_at >=
                 (terminal.accepted_terminal_submission_json
                   ->>'artifact_submission_deadline')::timestamptz OR
               terminal.accepted_terminal_submission_json
                 ->>'qualification_only' IS DISTINCT FROM 'true' OR
               terminal.accepted_terminal_submission_json
                 ->>'scientific_admission_allowed' IS DISTINCT FROM 'false' OR
               terminal.runtime_control_pin_json->>'policy_sha256' IS DISTINCT FROM
                 terminal.accepted_terminal_submission_json
                   ->>'runtime_control_policy_sha256' OR
               terminal.runtime_control_pin_json->>'principal_id' IS DISTINCT FROM
                 terminal.accepted_terminal_submission_json
                   ->>'accepted_by_principal_id' OR
               terminal.runtime_control_pin_json->>'key_id' IS DISTINCT FROM
                 terminal.accepted_terminal_submission_json->>'acceptance_key_id' OR
               terminal.accepted_at <
                 (terminal.runtime_control_pin_json->>'valid_from')::timestamptz OR
               terminal.accepted_at >= LEAST(
                 (terminal.runtime_control_pin_json->>'expires_at')::timestamptz,
                 COALESCE(
                   (terminal.runtime_control_pin_json->>'revoked_at')::timestamptz,
                   (terminal.runtime_control_pin_json->>'expires_at')
                     ::timestamptz)) OR
               (terminal.accepted_terminal_submission_json
                 ->>'artifact_submission_deadline')::timestamptz > LEAST(
                 (terminal.runtime_control_pin_json->>'expires_at')::timestamptz,
                 COALESCE(
                   (terminal.runtime_control_pin_json->>'revoked_at')::timestamptz,
                   (terminal.runtime_control_pin_json->>'expires_at')
                     ::timestamptz)) OR
               terminal.runtime_control_pin_sha256 IS DISTINCT FROM
                 termination.runtime_control_pin_sha256 OR
               terminal.runtime_control_pin_json IS DISTINCT FROM
                 termination.runtime_control_pin_json
             ) THEN
            RAISE EXCEPTION 'external terminal artifact acceptance differs from full proof'
              USING ERRCODE = '23514';
          END IF;

          IF EXISTS (
            SELECT 1 FROM execution_qualification_terminal_outbox o
             WHERE o.attempt_id = target_attempt AND (
               o.execution_id IS DISTINCT FROM a.execution_id OR
               o.topic IS DISTINCT FROM
                 'execution.qualification_terminal.v2' OR
               o.delivery_key IS DISTINCT FROM
                 'execution-v2:' || a.execution_id || ':' || target_attempt OR
               o.outbox_id IS DISTINCT FROM
                 'qto_' || o.terminal_authority_sha256 OR
               o.payload_sha256 IS DISTINCT FROM o.terminal_authority_sha256 OR
               o.created_at IS DISTINCT FROM a.updated_at
               OR (
                 o.terminal_authority_kind = 'accepted_terminal_submission' AND (
                   terminal.accepted_terminal_submission_sha256 IS NULL OR
                   expiration.terminal_deadline_expiration_sha256 IS NOT NULL OR
                   o.terminal_authority_sha256 IS DISTINCT FROM
                     terminal.accepted_terminal_submission_sha256 OR
                   o.accepted_terminal_submission_sha256 IS DISTINCT FROM
                     terminal.accepted_terminal_submission_sha256 OR
                   o.terminal_deadline_expiration_sha256 IS NOT NULL OR
                   o.payload_json IS DISTINCT FROM
                     terminal.accepted_terminal_submission_json
                 )
               ) OR (
                 o.terminal_authority_kind = 'terminal_deadline_expiration' AND (
                   expiration.terminal_deadline_expiration_sha256 IS NULL OR
                   terminal.accepted_terminal_submission_sha256 IS NOT NULL OR
                   o.terminal_authority_sha256 IS DISTINCT FROM
                     expiration.terminal_deadline_expiration_sha256 OR
                   o.terminal_deadline_expiration_sha256 IS DISTINCT FROM
                     expiration.terminal_deadline_expiration_sha256 OR
                   o.accepted_terminal_submission_sha256 IS NOT NULL OR
                   o.payload_json IS DISTINCT FROM expiration.payload_json
                 )
               )
             )
          ) THEN
            RAISE EXCEPTION 'external terminal outbox differs from exact authority'
              USING ERRCODE = '23514';
          END IF;

          IF termination.accepted_termination_sha256 IS NOT NULL THEN
            IF a.status NOT IN ('verifying','succeeded','failed') OR
               resource_state IS DISTINCT FROM 'released' OR
               budget_state IS DISTINCT FROM 'settled' OR
               a.terminal_receipt_sha256 IS NOT NULL OR EXISTS (
                 SELECT 1 FROM execution_device_leases d
                  WHERE d.attempt_id = target_attempt AND
                    d.state IS DISTINCT FROM 'released'
               ) OR
               (a.status = 'verifying' AND
                  head_attempt IS DISTINCT FROM target_attempt) OR
               (a.status IN ('succeeded','failed') AND
                  head_attempt IS NOT NULL) THEN
              RAISE EXCEPTION
                'external termination did not atomically release compute'
                USING ERRCODE = '23514';
            END IF;
            IF a.status IN ('succeeded','failed') AND (
                 ((terminal.accepted_terminal_submission_sha256 IS NULL)::integer +
                  (expiration.terminal_deadline_expiration_sha256 IS NULL)::integer)
                    IS DISTINCT FROM 1 OR
                 NOT EXISTS (
                   SELECT 1 FROM execution_qualification_terminal_outbox o
                    WHERE o.attempt_id = target_attempt
                      AND o.execution_id = a.execution_id
                 ) OR
                 (terminal.accepted_terminal_submission_sha256 IS NOT NULL AND
                  a.status IS DISTINCT FROM
                    CASE WHEN terminal.disposition = 'process_succeeded'
                         THEN 'succeeded' ELSE 'failed' END) OR
                 (expiration.terminal_deadline_expiration_sha256 IS NOT NULL AND
                  a.status IS DISTINCT FROM 'failed')
               ) THEN
              RAISE EXCEPTION
                'external terminal attempt lacks exact acceptance/outbox'
                USING ERRCODE = '23514';
            END IF;
          END IF;
          IF a.status = 'verifying' AND (
               expiration.terminal_deadline_expiration_sha256 IS NOT NULL OR
               EXISTS (
                 SELECT 1 FROM execution_qualification_terminal_outbox o
                  WHERE o.attempt_id = target_attempt
               )
             ) THEN
            RAISE EXCEPTION
              'verifying external attempt cannot expose terminal final authority'
              USING ERRCODE = '23514';
          END IF;
          RETURN;
        END;
        $$;
        """
    )


def _install_outbox_authority_trigger() -> None:
    # The 0026 inline column foreign keys are unnamed, so PostgreSQL truncated
    # their auto-generated constraint names to 63 bytes; drop them by catalog
    # lookup instead of guessing the truncated spellings.
    op.execute(
        r"""
        DO $drop$
        DECLARE constraint_name text;
        BEGIN
          FOR constraint_name IN
            SELECT c.conname FROM pg_constraint c
             WHERE c.conrelid =
               'execution_qualification_terminal_outbox'::regclass
               AND c.contype = 'f'
               AND c.conkey = ARRAY[
                 (SELECT attnum FROM pg_attribute
                   WHERE attrelid = c.conrelid
                     AND attname = 'accepted_terminal_submission_sha256')]
            OR (
                 c.conrelid =
                   'execution_qualification_terminal_outbox'::regclass
               AND c.contype = 'f'
               AND c.conkey = ARRAY[
                 (SELECT attnum FROM pg_attribute
                   WHERE attrelid = c.conrelid
                     AND attname = 'terminal_deadline_expiration_sha256')]
            )
          LOOP
            EXECUTE format(
              'ALTER TABLE execution_qualification_terminal_outbox'
              || ' DROP CONSTRAINT %I', constraint_name);
          END LOOP;
        END;
        $drop$;
        """
    )
    op.execute(
        r"""
        CREATE FUNCTION aletheia_execution_check_terminal_outbox_authority()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          attempt_node_id text;
        BEGIN
          SELECT node_id INTO attempt_node_id FROM execution_attempts
           WHERE attempt_id = NEW.attempt_id;
          IF attempt_node_id IS NULL THEN
            IF NEW.terminal_authority_kind = 'accepted_terminal_submission'
               AND NOT EXISTS (
                 SELECT 1
                   FROM execution_external_qualification_terminal_acceptances t
                  WHERE t.accepted_terminal_submission_sha256 =
                    NEW.accepted_terminal_submission_sha256
                    AND t.attempt_id = NEW.attempt_id
               ) THEN
              RAISE EXCEPTION
                'outbox external terminal authority row is absent'
                USING ERRCODE = '23514';
            END IF;
            IF NEW.terminal_authority_kind = 'terminal_deadline_expiration'
               AND NOT EXISTS (
                 SELECT 1
                   FROM
                     execution_external_qualification_deadline_expirations x
                  WHERE x.terminal_deadline_expiration_sha256 =
                    NEW.terminal_deadline_expiration_sha256
                    AND x.attempt_id = NEW.attempt_id
               ) THEN
              RAISE EXCEPTION
                'outbox external expiration authority row is absent'
                USING ERRCODE = '23514';
            END IF;
          ELSE
            IF NEW.terminal_authority_kind = 'accepted_terminal_submission'
               AND NOT EXISTS (
                 SELECT 1
                   FROM execution_qualification_terminal_acceptances t
                  WHERE t.accepted_terminal_submission_sha256 =
                    NEW.accepted_terminal_submission_sha256
               ) THEN
              RAISE EXCEPTION
                'outbox terminal authority row is absent'
                USING ERRCODE = '23514';
            END IF;
            IF NEW.terminal_authority_kind = 'terminal_deadline_expiration'
               AND NOT EXISTS (
                 SELECT 1
                   FROM execution_qualification_terminal_deadline_expirations x
                  WHERE x.terminal_deadline_expiration_sha256 =
                    NEW.terminal_deadline_expiration_sha256
               ) THEN
              RAISE EXCEPTION
                'outbox expiration authority row is absent'
                USING ERRCODE = '23514';
            END IF;
          END IF;
          RETURN NULL;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_execution_terminal_outbox_authority
          AFTER INSERT ON execution_qualification_terminal_outbox
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW
          EXECUTE FUNCTION aletheia_execution_check_terminal_outbox_authority()
        """
    )


def _delegate_frozen_guards_to_external() -> None:
    plans = (
        (
            "aletheia_execution_guard_runtime_v2_attempt_head()",
            "0026 head guard function head",
            (_HEAD_GUARD_ANCHOR, _HEAD_GUARD_DELEGATION),
        ),
        (
            "aletheia_execution_check_runtime_v2_attempt()",
            "0026 completeness guard attempt fetch",
            (_COMPLETE_GUARD_ANCHOR, _COMPLETE_GUARD_DELEGATION),
        ),
        (
            "aletheia_execution_guard_attempt()",
            "0026 guard_attempt identity arm",
            (_GUARD_ATTEMPT_IDENTITY_ANCHOR, _GUARD_ATTEMPT_IDENTITY_DELEGATION),
        ),
        (
            "aletheia_execution_guard_attempt()",
            "0026 guard_attempt accepting_termination arm",
            (_GUARD_ATTEMPT_ACCEPTING_ANCHOR, _GUARD_ATTEMPT_ACCEPTING_DELEGATION),
        ),
        (
            "aletheia_execution_guard_attempt()",
            "0026 guard_attempt terminalizing arm",
            (_GUARD_ATTEMPT_TERMINAL_ANCHOR, _GUARD_ATTEMPT_TERMINAL_DELEGATION),
        ),
        (
            "aletheia_execution_check_attempt_bundle()",
            "0026 attempt bundle termination proof",
            (_BUNDLE_TERMINATION_ANCHOR, _BUNDLE_TERMINATION_DELEGATION),
        ),
    )
    for function_name, label, (needle, replacement) in plans:
        op.execute(
            f"""
            DO $migration$
            DECLARE
              definition text;
            BEGIN
              SELECT pg_get_functiondef('{function_name}'::regprocedure)
                INTO definition;
              IF position($needle${needle}$needle$ IN definition) = 0 THEN
                RAISE EXCEPTION '{label} is not the expected frozen form';
              END IF;
              EXECUTE replace(
                definition, $needle${needle}$needle$, $needle${replacement}$needle$
              );
            END;
            $migration$;
            """
        )


def _install_external_table_triggers() -> None:
    for table in _EXTERNAL_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION aletheia_execution_reject_mutation()
            """
        )
    for table in _EXTERNAL_TABLES:
        op.execute(
            f"""
            CREATE CONSTRAINT TRIGGER trg_{table}_runtime_v2_complete
            AFTER INSERT OR UPDATE ON {table}
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION
              aletheia_execution_check_runtime_v2_attempt()
            """
        )


def upgrade() -> None:
    _create_external_chain_tables()
    _install_external_json_shape_catalog()
    _install_external_head_guard()
    _install_external_completeness_guard()
    _delegate_frozen_guards_to_external()
    _install_outbox_authority_trigger()
    _install_external_table_triggers()


def downgrade() -> None:
    for table in _EXTERNAL_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_runtime_v2_complete ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_execution_terminal_outbox_authority"
        " ON execution_qualification_terminal_outbox"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS"
        " aletheia_execution_check_terminal_outbox_authority()"
    )
    plans = (
        (
            "aletheia_execution_check_attempt_bundle()",
            "0036 attempt bundle termination delegation",
            (_BUNDLE_TERMINATION_ANCHOR, _BUNDLE_TERMINATION_DELEGATION),
        ),
        (
            "aletheia_execution_guard_attempt()",
            "0036 guard_attempt terminalizing delegation",
            (_GUARD_ATTEMPT_TERMINAL_ANCHOR, _GUARD_ATTEMPT_TERMINAL_DELEGATION),
        ),
        (
            "aletheia_execution_guard_attempt()",
            "0036 guard_attempt accepting_termination delegation",
            (_GUARD_ATTEMPT_ACCEPTING_ANCHOR, _GUARD_ATTEMPT_ACCEPTING_DELEGATION),
        ),
        (
            "aletheia_execution_guard_attempt()",
            "0036 guard_attempt identity delegation",
            (_GUARD_ATTEMPT_IDENTITY_ANCHOR, _GUARD_ATTEMPT_IDENTITY_DELEGATION),
        ),
        (
            "aletheia_execution_check_runtime_v2_attempt()",
            "0036 completeness delegation",
            (_COMPLETE_GUARD_ANCHOR, _COMPLETE_GUARD_DELEGATION),
        ),
        (
            "aletheia_execution_guard_runtime_v2_attempt_head()",
            "0036 head guard delegation",
            (_HEAD_GUARD_ANCHOR, _HEAD_GUARD_DELEGATION),
        ),
    )
    for function_name, label, (needle, replacement) in plans:
        old, new = replacement, needle
        op.execute(
            f"""
            DO $migration$
            DECLARE
              definition text;
            BEGIN
              SELECT pg_get_functiondef('{function_name}'::regprocedure)
                INTO definition;
              IF position($needle${old}$needle$ IN definition) = 0 THEN
                RAISE EXCEPTION '{label} is not the expected rewritten form';
              END IF;
              EXECUTE replace(
                definition, $needle${old}$needle$, $needle${new}$needle$
              );
            END;
            $migration$;
            """
        )
    op.execute(
        "DROP FUNCTION IF EXISTS aletheia_execution_check_external_v2_attempt(text)"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS"
        " aletheia_execution_guard_external_v2_attempt_head("
        "execution_attempts, execution_attempts)"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS"
        " aletheia_execution_external_v2_json_valid(jsonb, text)"
    )
    op.execute(
        "ALTER TABLE execution_qualification_terminal_outbox"
        " ADD CONSTRAINT fk_terminal_outbox_accepted_terminal_submission"
        " FOREIGN KEY (accepted_terminal_submission_sha256)"
        " REFERENCES execution_qualification_terminal_acceptances"
        " (accepted_terminal_submission_sha256)"
    )
    op.execute(
        "ALTER TABLE execution_qualification_terminal_outbox"
        " ADD CONSTRAINT fk_terminal_outbox_terminal_deadline_expiration"
        " FOREIGN KEY (terminal_deadline_expiration_sha256)"
        " REFERENCES execution_qualification_terminal_deadline_expirations"
        " (terminal_deadline_expiration_sha256)"
    )
    for table in reversed(_EXTERNAL_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table}")
