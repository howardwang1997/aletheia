"""Persist the qualification runtime-v2 authority lifecycle.

Revision ID: 20260827_0026
Revises: 20260826_0025
Create Date: 2026-08-27

The v2 records are a separate append-only authority chain.  In particular, neither accepted
runtime termination nor accepted artifact provenance is represented as a legacy ExecutionReceipt.
Compute and budget holds are released at fresh termination acceptance, while the execution head
remains active through the independently bounded artifact-verification grace window.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260827_0026"
down_revision: str | None = "20260826_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_APPEND_ONLY_TABLES = (
    "execution_runtime_preparations",
    "execution_runtime_launch_authorizations",
    "execution_runtime_launch_receipts",
    "execution_pre_runtime_absence_decisions",
    "execution_runtime_fence_rebinds",
    "execution_runtime_termination_challenges",
    "execution_runtime_termination_acceptances",
    "execution_qualification_terminal_deadline_expirations",
    "execution_qualification_terminal_acceptances",
    "execution_qualification_terminal_outbox",
)


def upgrade() -> None:
    _backup_v1_guard_functions()
    op.execute(
        """
        ALTER TABLE execution_attempts
          DROP CONSTRAINT ck_execution_attempts_versions,
          DROP CONSTRAINT ck_execution_attempts_hashes;
        ALTER TABLE execution_attempts
          ADD COLUMN runtime_preparation_sha256 varchar(64),
          ADD COLUMN runtime_launch_authorization_count integer NOT NULL DEFAULT 0,
          ADD COLUMN latest_runtime_launch_authorization_sha256 varchar(64),
          ADD COLUMN pre_runtime_absence_count integer NOT NULL DEFAULT 0,
          ADD COLUMN latest_pre_runtime_absence_receipt_sha256 varchar(64),
          ADD COLUMN node_runtime_launch_receipt_sha256 varchar(64),
          ADD COLUMN runtime_termination_challenge_count integer NOT NULL DEFAULT 0,
          ADD COLUMN runtime_termination_challenge_sha256 varchar(64),
          ADD COLUMN accepted_runtime_termination_sha256 varchar(64),
          ADD COLUMN accepted_terminal_submission_sha256 varchar(64),
          ADD COLUMN terminal_deadline_expiration_sha256 varchar(64),
          ADD CONSTRAINT ck_execution_attempts_versions CHECK (
            attempt_number >= 1 AND adoption_count >= 0 AND
            last_runtime_inspection_sequence >= 0 AND
            runtime_launch_authorization_count >= 0 AND
            pre_runtime_absence_count >= 0 AND
            runtime_termination_challenge_count >= 0 AND
            state_version >= 1 AND fencing_epoch >= 1
          ),
          ADD CONSTRAINT ck_execution_attempts_hashes CHECK (
            intent_sha256 ~ '^[0-9a-f]{64}$' AND
            admission_sha256 ~ '^[0-9a-f]{64}$' AND
            grant_sha256 ~ '^[0-9a-f]{64}$' AND
            bundle_sha256 ~ '^[0-9a-f]{64}$' AND
            cost_quote_sha256 ~ '^[0-9a-f]{64}$' AND
            lease_token_sha256 ~ '^[0-9a-f]{64}$' AND
            node_inventory_sha256 ~ '^[0-9a-f]{64}$' AND
            (latest_adoption_sha256 IS NULL OR
              latest_adoption_sha256 ~ '^[0-9a-f]{64}$') AND
            (last_runtime_inspection_sha256 IS NULL OR
              last_runtime_inspection_sha256 ~ '^[0-9a-f]{64}$') AND
            (runtime_identity_sha256 IS NULL OR
              runtime_identity_sha256 ~ '^[0-9a-f]{64}$') AND
            (terminal_receipt_sha256 IS NULL OR
              terminal_receipt_sha256 ~ '^[0-9a-f]{64}$') AND
            (runtime_preparation_sha256 IS NULL OR
              runtime_preparation_sha256 ~ '^[0-9a-f]{64}$') AND
            (latest_runtime_launch_authorization_sha256 IS NULL OR
              latest_runtime_launch_authorization_sha256 ~ '^[0-9a-f]{64}$') AND
            (latest_pre_runtime_absence_receipt_sha256 IS NULL OR
              latest_pre_runtime_absence_receipt_sha256 ~ '^[0-9a-f]{64}$') AND
            (node_runtime_launch_receipt_sha256 IS NULL OR
              node_runtime_launch_receipt_sha256 ~ '^[0-9a-f]{64}$') AND
            (runtime_termination_challenge_sha256 IS NULL OR
              runtime_termination_challenge_sha256 ~ '^[0-9a-f]{64}$') AND
            (accepted_runtime_termination_sha256 IS NULL OR
              accepted_runtime_termination_sha256 ~ '^[0-9a-f]{64}$') AND
            (accepted_terminal_submission_sha256 IS NULL OR
              accepted_terminal_submission_sha256 ~ '^[0-9a-f]{64}$') AND
            (terminal_deadline_expiration_sha256 IS NULL OR
              terminal_deadline_expiration_sha256 ~ '^[0-9a-f]{64}$')
          ),
          ADD CONSTRAINT ck_execution_attempts_v2_counts CHECK (
            (runtime_launch_authorization_count = 0) =
              (latest_runtime_launch_authorization_sha256 IS NULL) AND
            (pre_runtime_absence_count = 0) =
              (latest_pre_runtime_absence_receipt_sha256 IS NULL) AND
            (runtime_termination_challenge_count = 0) =
              (runtime_termination_challenge_sha256 IS NULL)
          ),
          ADD CONSTRAINT ck_execution_attempts_v2_terminal_authority CHECK (
            accepted_terminal_submission_sha256 IS NULL OR
            terminal_deadline_expiration_sha256 IS NULL
          ),
          ADD CONSTRAINT uq_execution_attempts_runtime_preparation
            UNIQUE (runtime_preparation_sha256),
          ADD CONSTRAINT uq_execution_attempts_latest_runtime_launch_authorization
            UNIQUE (latest_runtime_launch_authorization_sha256),
          ADD CONSTRAINT uq_execution_attempts_latest_pre_runtime_absence
            UNIQUE (latest_pre_runtime_absence_receipt_sha256),
          ADD CONSTRAINT uq_execution_attempts_node_runtime_launch_receipt
            UNIQUE (node_runtime_launch_receipt_sha256),
          ADD CONSTRAINT uq_execution_attempts_runtime_termination_challenge
            UNIQUE (runtime_termination_challenge_sha256),
          ADD CONSTRAINT uq_execution_attempts_accepted_runtime_termination
            UNIQUE (accepted_runtime_termination_sha256),
          ADD CONSTRAINT uq_execution_attempts_accepted_terminal_submission
            UNIQUE (accepted_terminal_submission_sha256),
          ADD CONSTRAINT uq_execution_attempts_terminal_deadline_expiration
            UNIQUE (terminal_deadline_expiration_sha256);

        CREATE TABLE execution_runtime_preparations (
          preparation_sha256 varchar(64) PRIMARY KEY,
          attempt_id varchar(36) NOT NULL,
          execution_id varchar(36) NOT NULL,
          intent_sha256 varchar(64) NOT NULL,
          node_id varchar(128) NOT NULL REFERENCES execution_nodes(node_id),
          node_manifest_sha256 varchar(64) NOT NULL,
          boot_id varchar(192) NOT NULL,
          fencing_epoch bigint NOT NULL,
          lease_token_sha256 varchar(64) NOT NULL,
          payload_sha256 varchar(64) NOT NULL,
          payload_json jsonb NOT NULL,
          prepared_at timestamptz NOT NULL,
          prepared_monotonic_ns bigint NOT NULL,
          recorded_at timestamptz NOT NULL,
          CONSTRAINT fk_execution_runtime_preparations_attempt
            FOREIGN KEY (attempt_id, execution_id)
            REFERENCES execution_attempts(attempt_id, execution_id),
          CONSTRAINT uq_execution_runtime_preparations_attempt UNIQUE (attempt_id),
          CONSTRAINT ck_execution_runtime_preparations_order CHECK (
            fencing_epoch >= 1 AND prepared_monotonic_ns >= 0),
          CONSTRAINT ck_execution_runtime_preparations_hashes CHECK (
            preparation_sha256 ~ '^[0-9a-f]{64}$' AND
            intent_sha256 ~ '^[0-9a-f]{64}$' AND
            node_manifest_sha256 ~ '^[0-9a-f]{64}$' AND
            lease_token_sha256 ~ '^[0-9a-f]{64}$' AND
            payload_sha256 ~ '^[0-9a-f]{64}$')
        );
        CREATE INDEX ix_execution_runtime_preparations_attempt_id
          ON execution_runtime_preparations (attempt_id);
        CREATE INDEX ix_execution_runtime_preparations_execution_id
          ON execution_runtime_preparations (execution_id);
        CREATE INDEX ix_execution_runtime_preparations_node_id
          ON execution_runtime_preparations (node_id);
        CREATE INDEX ix_execution_runtime_preparations_recorded_at
          ON execution_runtime_preparations (recorded_at);

        CREATE TABLE execution_runtime_launch_authorizations (
          authorization_sha256 varchar(64) PRIMARY KEY,
          attempt_id varchar(36) NOT NULL REFERENCES execution_attempts(attempt_id),
          preparation_sha256 varchar(64) NOT NULL
            REFERENCES execution_runtime_preparations(preparation_sha256),
          sequence integer NOT NULL,
          request_sha256 varchar(64) NOT NULL UNIQUE,
          pre_runtime_absence_epoch integer NOT NULL,
          pre_runtime_absence_receipt_sha256 varchar(64),
          request_payload_sha256 varchar(64) NOT NULL,
          request_json jsonb NOT NULL,
          authorization_payload_sha256 varchar(64) NOT NULL,
          authorization_json jsonb NOT NULL,
          runtime_control_pin_sha256 varchar(64) NOT NULL,
          runtime_control_pin_json jsonb NOT NULL,
          issued_at timestamptz NOT NULL,
          expires_at timestamptz NOT NULL,
          recorded_at timestamptz NOT NULL,
          CONSTRAINT uq_execution_runtime_launch_authorizations_sequence
            UNIQUE (attempt_id, sequence),
          CONSTRAINT ck_execution_runtime_launch_authorizations_order CHECK (
            sequence >= 1 AND pre_runtime_absence_epoch >= 0 AND issued_at < expires_at),
          CONSTRAINT ck_execution_runtime_launch_authorizations_absence CHECK (
            (pre_runtime_absence_epoch = 0) =
              (pre_runtime_absence_receipt_sha256 IS NULL)),
          CONSTRAINT ck_execution_runtime_launch_authorizations_hashes CHECK (
            authorization_sha256 ~ '^[0-9a-f]{64}$' AND
            request_sha256 ~ '^[0-9a-f]{64}$' AND
            preparation_sha256 ~ '^[0-9a-f]{64}$' AND
            (pre_runtime_absence_receipt_sha256 IS NULL OR
              pre_runtime_absence_receipt_sha256 ~ '^[0-9a-f]{64}$') AND
            request_payload_sha256 ~ '^[0-9a-f]{64}$' AND
            authorization_payload_sha256 ~ '^[0-9a-f]{64}$' AND
            runtime_control_pin_sha256 ~ '^[0-9a-f]{64}$')
        );
        CREATE INDEX ix_execution_runtime_launch_authorizations_attempt_id
          ON execution_runtime_launch_authorizations (attempt_id);
        CREATE INDEX ix_execution_runtime_launch_authorizations_preparation_sha256
          ON execution_runtime_launch_authorizations (preparation_sha256);
        CREATE INDEX ix_execution_runtime_launch_authorizations_expires_at
          ON execution_runtime_launch_authorizations (expires_at);
        CREATE INDEX ix_execution_runtime_launch_authorizations_recorded_at
          ON execution_runtime_launch_authorizations (recorded_at);

        CREATE TABLE execution_runtime_launch_receipts (
          launch_receipt_sha256 varchar(64) PRIMARY KEY,
          attempt_id varchar(36) NOT NULL REFERENCES execution_attempts(attempt_id),
          preparation_sha256 varchar(64) NOT NULL
            REFERENCES execution_runtime_preparations(preparation_sha256),
          authorization_request_sha256 varchar(64) NOT NULL,
          authorization_sha256 varchar(64) NOT NULL
            REFERENCES execution_runtime_launch_authorizations(authorization_sha256),
          runtime_identity_sha256 varchar(64) NOT NULL UNIQUE,
          launch_payload_sha256 varchar(64) NOT NULL,
          launch_receipt_json jsonb NOT NULL,
          recovery_grant_sha256 varchar(64) NOT NULL UNIQUE,
          recovery_payload_sha256 varchar(64) NOT NULL,
          recovery_grant_json jsonb NOT NULL,
          recovery_expires_at timestamptz NOT NULL,
          runtime_control_pin_sha256 varchar(64) NOT NULL,
          runtime_control_pin_json jsonb NOT NULL,
          signed_at timestamptz NOT NULL,
          accepted_at timestamptz NOT NULL,
          CONSTRAINT uq_execution_runtime_launch_receipts_attempt
            UNIQUE (attempt_id),
          CONSTRAINT ck_execution_runtime_launch_receipts_hashes CHECK (
            launch_receipt_sha256 ~ '^[0-9a-f]{64}$' AND
            preparation_sha256 ~ '^[0-9a-f]{64}$' AND
            authorization_request_sha256 ~ '^[0-9a-f]{64}$' AND
            authorization_sha256 ~ '^[0-9a-f]{64}$' AND
            runtime_identity_sha256 ~ '^[0-9a-f]{64}$' AND
            launch_payload_sha256 ~ '^[0-9a-f]{64}$' AND
            recovery_grant_sha256 ~ '^[0-9a-f]{64}$' AND
            recovery_payload_sha256 ~ '^[0-9a-f]{64}$' AND
            runtime_control_pin_sha256 ~ '^[0-9a-f]{64}$')
        );
        CREATE INDEX ix_execution_runtime_launch_receipts_attempt_id
          ON execution_runtime_launch_receipts (attempt_id);
        CREATE INDEX ix_execution_runtime_launch_receipts_accepted_at
          ON execution_runtime_launch_receipts (accepted_at);
        CREATE INDEX ix_execution_runtime_launch_receipts_recovery_expires_at
          ON execution_runtime_launch_receipts (recovery_expires_at);

        CREATE TABLE execution_pre_runtime_absence_decisions (
          decision_sha256 varchar(64) PRIMARY KEY,
          attempt_id varchar(36) NOT NULL REFERENCES execution_attempts(attempt_id),
          absence_epoch integer NOT NULL,
          absence_receipt_sha256 varchar(64) NOT NULL UNIQUE,
          preparation_sha256 varchar(64) NOT NULL
            REFERENCES execution_runtime_preparations(preparation_sha256),
          prior_authorization_request_sha256 varchar(64),
          prior_authorization_sha256 varchar(64),
          absence_payload_sha256 varchar(64) NOT NULL,
          absence_receipt_json jsonb NOT NULL,
          disposition varchar(16) NOT NULL,
          replacement_request_sha256 varchar(64),
          replacement_authorization_sha256 varchar(64),
          decision_json jsonb NOT NULL,
          runtime_control_pin_sha256 varchar(64) NOT NULL,
          runtime_control_pin_json jsonb NOT NULL,
          decided_at timestamptz NOT NULL,
          CONSTRAINT uq_execution_pre_runtime_absence_epoch UNIQUE (attempt_id, absence_epoch),
          CONSTRAINT ck_execution_pre_runtime_absence_decisions_shape CHECK (
            absence_epoch >= 1 AND disposition IN ('released','reauthorized')),
          CONSTRAINT ck_execution_pre_runtime_absence_decisions_replacement CHECK (
            (disposition = 'released' AND replacement_request_sha256 IS NULL AND
              replacement_authorization_sha256 IS NULL) OR
            (disposition = 'reauthorized' AND replacement_request_sha256 IS NOT NULL AND
              replacement_authorization_sha256 IS NOT NULL)),
          CONSTRAINT ck_execution_pre_runtime_absence_decisions_prior_pair CHECK (
            (prior_authorization_request_sha256 IS NULL) =
              (prior_authorization_sha256 IS NULL)),
          CONSTRAINT ck_execution_pre_runtime_absence_decisions_hashes CHECK (
            decision_sha256 ~ '^[0-9a-f]{64}$' AND
            absence_receipt_sha256 ~ '^[0-9a-f]{64}$' AND
            preparation_sha256 ~ '^[0-9a-f]{64}$' AND
            (prior_authorization_request_sha256 IS NULL OR
              prior_authorization_request_sha256 ~ '^[0-9a-f]{64}$') AND
            (prior_authorization_sha256 IS NULL OR
              prior_authorization_sha256 ~ '^[0-9a-f]{64}$') AND
            absence_payload_sha256 ~ '^[0-9a-f]{64}$' AND
            (replacement_request_sha256 IS NULL OR
              replacement_request_sha256 ~ '^[0-9a-f]{64}$') AND
            (replacement_authorization_sha256 IS NULL OR
              replacement_authorization_sha256 ~ '^[0-9a-f]{64}$') AND
            runtime_control_pin_sha256 ~ '^[0-9a-f]{64}$')
        );
        CREATE INDEX ix_execution_pre_runtime_absence_decisions_attempt_id
          ON execution_pre_runtime_absence_decisions (attempt_id);
        CREATE INDEX ix_execution_pre_runtime_absence_decisions_disposition
          ON execution_pre_runtime_absence_decisions (disposition);
        CREATE INDEX ix_execution_pre_runtime_absence_decisions_decided_at
          ON execution_pre_runtime_absence_decisions (decided_at);

        CREATE TABLE execution_runtime_fence_rebinds (
          rebind_receipt_sha256 varchar(64) PRIMARY KEY,
          attempt_id varchar(36) NOT NULL REFERENCES execution_attempts(attempt_id),
          adoption_sha256 varchar(64) NOT NULL UNIQUE
            REFERENCES execution_attempt_adoptions(adoption_sha256),
          sequence integer NOT NULL,
          request_sha256 varchar(64) NOT NULL UNIQUE,
          evidence_sha256 varchar(64) NOT NULL UNIQUE,
          preparation_sha256 varchar(64) NOT NULL
            REFERENCES execution_runtime_preparations(preparation_sha256),
          runtime_identity_sha256 varchar(64) NOT NULL,
          previous_fencing_epoch bigint NOT NULL,
          new_fencing_epoch bigint NOT NULL,
          previous_lease_token_sha256 varchar(64) NOT NULL,
          new_lease_token_sha256 varchar(64) NOT NULL,
          request_payload_sha256 varchar(64) NOT NULL,
          request_json jsonb NOT NULL,
          receipt_payload_sha256 varchar(64) NOT NULL,
          receipt_json jsonb NOT NULL,
          rebound_at timestamptz NOT NULL,
          accepted_at timestamptz NOT NULL,
          CONSTRAINT uq_execution_runtime_rebind_sequence UNIQUE (attempt_id, sequence),
          CONSTRAINT ck_execution_runtime_fence_rebinds_sequence CHECK (
            sequence >= 1 AND new_fencing_epoch = previous_fencing_epoch + 1),
          CONSTRAINT ck_execution_runtime_fence_rebinds_hashes CHECK (
            rebind_receipt_sha256 ~ '^[0-9a-f]{64}$' AND
            request_sha256 ~ '^[0-9a-f]{64}$' AND
            evidence_sha256 ~ '^[0-9a-f]{64}$' AND
            adoption_sha256 ~ '^[0-9a-f]{64}$' AND
            preparation_sha256 ~ '^[0-9a-f]{64}$' AND
            runtime_identity_sha256 ~ '^[0-9a-f]{64}$' AND
            previous_lease_token_sha256 ~ '^[0-9a-f]{64}$' AND
            new_lease_token_sha256 ~ '^[0-9a-f]{64}$' AND
            request_payload_sha256 ~ '^[0-9a-f]{64}$' AND
            receipt_payload_sha256 ~ '^[0-9a-f]{64}$')
        );
        CREATE INDEX ix_execution_runtime_fence_rebinds_attempt_id
          ON execution_runtime_fence_rebinds (attempt_id);
        CREATE INDEX ix_execution_runtime_fence_rebinds_adoption_sha256
          ON execution_runtime_fence_rebinds (adoption_sha256);
        CREATE INDEX ix_execution_runtime_fence_rebinds_accepted_at
          ON execution_runtime_fence_rebinds (accepted_at);

        CREATE TABLE execution_runtime_termination_challenges (
          challenge_sha256 varchar(64) PRIMARY KEY,
          challenge_id varchar(64) NOT NULL UNIQUE,
          attempt_id varchar(36) NOT NULL REFERENCES execution_attempts(attempt_id),
          preparation_sha256 varchar(64) NOT NULL
            REFERENCES execution_runtime_preparations(preparation_sha256),
          launch_receipt_sha256 varchar(64) NOT NULL
            REFERENCES execution_runtime_launch_receipts(launch_receipt_sha256),
          runtime_identity_sha256 varchar(64) NOT NULL,
          inspection_evidence_sha256 varchar(64) NOT NULL,
          inspection_evidence_json jsonb NOT NULL,
          inspection_sequence bigint NOT NULL,
          challenge_payload_sha256 varchar(64) NOT NULL,
          challenge_json jsonb NOT NULL,
          runtime_control_pin_sha256 varchar(64) NOT NULL,
          runtime_control_pin_json jsonb NOT NULL,
          challenged_at timestamptz NOT NULL,
          expires_at timestamptz NOT NULL,
          CONSTRAINT ck_execution_runtime_termination_challenges_order CHECK (
            inspection_sequence >= 1 AND challenged_at < expires_at),
          CONSTRAINT uq_execution_runtime_termination_challenge_sequence
            UNIQUE (attempt_id, inspection_sequence),
          CONSTRAINT ck_execution_runtime_termination_challenges_hashes CHECK (
            challenge_sha256 ~ '^[0-9a-f]{64}$' AND challenge_id ~ '^[0-9a-f]{64}$' AND
            preparation_sha256 ~ '^[0-9a-f]{64}$' AND
            launch_receipt_sha256 ~ '^[0-9a-f]{64}$' AND
            runtime_identity_sha256 ~ '^[0-9a-f]{64}$' AND
            inspection_evidence_sha256 ~ '^[0-9a-f]{64}$' AND
            challenge_payload_sha256 ~ '^[0-9a-f]{64}$' AND
            runtime_control_pin_sha256 ~ '^[0-9a-f]{64}$')
        );
        CREATE INDEX ix_execution_runtime_termination_challenges_attempt_id
          ON execution_runtime_termination_challenges (attempt_id);
        CREATE INDEX ix_execution_runtime_termination_challenges_expires_at
          ON execution_runtime_termination_challenges (expires_at);

        CREATE TABLE execution_runtime_termination_acceptances (
          accepted_termination_sha256 varchar(64) PRIMARY KEY,
          attempt_id varchar(36) NOT NULL REFERENCES execution_attempts(attempt_id),
          challenge_sha256 varchar(64) NOT NULL UNIQUE
            REFERENCES execution_runtime_termination_challenges(challenge_sha256),
          node_termination_receipt_sha256 varchar(64) NOT NULL UNIQUE,
          preparation_sha256 varchar(64) NOT NULL
            REFERENCES execution_runtime_preparations(preparation_sha256),
          launch_receipt_sha256 varchar(64) NOT NULL
            REFERENCES execution_runtime_launch_receipts(launch_receipt_sha256),
          runtime_identity_sha256 varchar(64) NOT NULL,
          termination_evidence_sha256 varchar(64) NOT NULL,
          inspection_sequence bigint NOT NULL,
          node_receipt_payload_sha256 varchar(64) NOT NULL,
          node_termination_receipt_json jsonb NOT NULL,
          acceptance_payload_sha256 varchar(64) NOT NULL,
          accepted_termination_json jsonb NOT NULL,
          recovery_grant_sha256 varchar(64) NOT NULL UNIQUE,
          recovery_payload_sha256 varchar(64) NOT NULL,
          recovery_grant_json jsonb NOT NULL,
          recovery_expires_at timestamptz NOT NULL,
          conditional_terminal_expiration_sha256 varchar(64) NOT NULL UNIQUE,
          conditional_terminal_expiration_payload_sha256 varchar(64) NOT NULL,
          conditional_terminal_expiration_json jsonb NOT NULL,
          conditional_terminal_expiration_authorized_at timestamptz NOT NULL,
          conditional_terminal_expiration_expires_at timestamptz NOT NULL,
          runtime_control_pin_sha256 varchar(64) NOT NULL,
          runtime_control_pin_json jsonb NOT NULL,
          runtime_ended_at timestamptz NOT NULL,
          accepted_at timestamptz NOT NULL,
          CONSTRAINT uq_execution_runtime_termination_acceptance_attempt
            UNIQUE (attempt_id),
          CONSTRAINT ck_execution_runtime_termination_acceptances_order CHECK (
            inspection_sequence >= 1 AND runtime_ended_at <= accepted_at),
          CONSTRAINT ck_execution_runtime_termination_acceptances_hashes CHECK (
            accepted_termination_sha256 ~ '^[0-9a-f]{64}$' AND
            challenge_sha256 ~ '^[0-9a-f]{64}$' AND
            node_termination_receipt_sha256 ~ '^[0-9a-f]{64}$' AND
            preparation_sha256 ~ '^[0-9a-f]{64}$' AND
            launch_receipt_sha256 ~ '^[0-9a-f]{64}$' AND
            runtime_identity_sha256 ~ '^[0-9a-f]{64}$' AND
            termination_evidence_sha256 ~ '^[0-9a-f]{64}$' AND
            node_receipt_payload_sha256 ~ '^[0-9a-f]{64}$' AND
            acceptance_payload_sha256 ~ '^[0-9a-f]{64}$' AND
            recovery_grant_sha256 ~ '^[0-9a-f]{64}$' AND
            recovery_payload_sha256 ~ '^[0-9a-f]{64}$' AND
            conditional_terminal_expiration_sha256 ~ '^[0-9a-f]{64}$' AND
            conditional_terminal_expiration_payload_sha256 ~ '^[0-9a-f]{64}$' AND
            runtime_control_pin_sha256 ~ '^[0-9a-f]{64}$')
        );
        CREATE INDEX ix_execution_runtime_termination_acceptances_attempt_id
          ON execution_runtime_termination_acceptances (attempt_id);
        CREATE INDEX ix_execution_runtime_termination_acceptances_accepted_at
          ON execution_runtime_termination_acceptances (accepted_at);
        CREATE INDEX ix_execution_runtime_termination_acceptances_recovery_e_44fc
          ON execution_runtime_termination_acceptances (recovery_expires_at);
        CREATE INDEX ix_execution_runtime_termination_acceptances_conditiona_76be
          ON execution_runtime_termination_acceptances (
            conditional_terminal_expiration_expires_at);

        CREATE TABLE execution_qualification_terminal_deadline_expirations (
          terminal_deadline_expiration_sha256 varchar(64) PRIMARY KEY
            REFERENCES execution_runtime_termination_acceptances(
              conditional_terminal_expiration_sha256),
          attempt_id varchar(36) NOT NULL REFERENCES execution_attempts(attempt_id),
          accepted_runtime_termination_sha256 varchar(64) NOT NULL UNIQUE
            REFERENCES execution_runtime_termination_acceptances(accepted_termination_sha256),
          payload_sha256 varchar(64) NOT NULL,
          payload_json jsonb NOT NULL,
          runtime_control_pin_sha256 varchar(64) NOT NULL,
          runtime_control_pin_json jsonb NOT NULL,
          authorized_at timestamptz NOT NULL,
          expired_at timestamptz NOT NULL,
          activated_at timestamptz NOT NULL,
          CONSTRAINT uq_execution_qualification_terminal_deadline_expiration_attempt
            UNIQUE (attempt_id),
          CONSTRAINT ck_execution_qualification_terminal_deadline_expirations_order CHECK (
            authorized_at < expired_at AND expired_at <= activated_at),
          CONSTRAINT ck_execution_qualification_terminal_deadline_expirations_hashes CHECK (
            terminal_deadline_expiration_sha256 ~ '^[0-9a-f]{64}$' AND
            accepted_runtime_termination_sha256 ~ '^[0-9a-f]{64}$' AND
            payload_sha256 ~ '^[0-9a-f]{64}$' AND
            runtime_control_pin_sha256 ~ '^[0-9a-f]{64}$')
        );
        CREATE INDEX ix_execution_qualification_terminal_deadline_expiration_e983
          ON execution_qualification_terminal_deadline_expirations (attempt_id);
        CREATE INDEX ix_execution_qualification_terminal_deadline_expiration_0cd2
          ON execution_qualification_terminal_deadline_expirations (expired_at);
        CREATE INDEX ix_execution_qualification_terminal_deadline_expiration_d29a
          ON execution_qualification_terminal_deadline_expirations (activated_at);

        CREATE TABLE execution_qualification_terminal_acceptances (
          accepted_terminal_submission_sha256 varchar(64) PRIMARY KEY,
          attempt_id varchar(36) NOT NULL REFERENCES execution_attempts(attempt_id),
          accepted_runtime_termination_sha256 varchar(64) NOT NULL UNIQUE
            REFERENCES execution_runtime_termination_acceptances(accepted_termination_sha256),
          terminal_submission_sha256 varchar(64) NOT NULL UNIQUE,
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
          CONSTRAINT uq_execution_qualification_terminal_acceptance_attempt
            UNIQUE (attempt_id),
          CONSTRAINT ck_execution_qualification_terminal_acceptances_disposition CHECK (
            disposition IN ('process_succeeded','process_failed','invalid_output','timeout')),
          CONSTRAINT ck_execution_qualification_terminal_acceptances_hashes CHECK (
            accepted_terminal_submission_sha256 ~ '^[0-9a-f]{64}$' AND
            accepted_runtime_termination_sha256 ~ '^[0-9a-f]{64}$' AND
            terminal_submission_sha256 ~ '^[0-9a-f]{64}$' AND
            artifact_manifest_sha256 ~ '^[0-9a-f]{64}$' AND
            output_tree_sha256 ~ '^[0-9a-f]{64}$' AND
            submission_payload_sha256 ~ '^[0-9a-f]{64}$' AND
            manifest_payload_sha256 ~ '^[0-9a-f]{64}$' AND
            acceptance_payload_sha256 ~ '^[0-9a-f]{64}$' AND
            runtime_control_pin_sha256 ~ '^[0-9a-f]{64}$')
        );
        CREATE INDEX ix_execution_qualification_terminal_acceptances_attempt_id
          ON execution_qualification_terminal_acceptances (attempt_id);
        CREATE INDEX ix_execution_qualification_terminal_acceptances_disposition
          ON execution_qualification_terminal_acceptances (disposition);
        CREATE INDEX ix_execution_qualification_terminal_acceptances_accepted_at
          ON execution_qualification_terminal_acceptances (accepted_at);

        CREATE TABLE execution_qualification_terminal_outbox (
          outbox_id varchar(96) PRIMARY KEY,
          terminal_authority_kind varchar(48) NOT NULL,
          terminal_authority_sha256 varchar(64) NOT NULL UNIQUE,
          accepted_terminal_submission_sha256 varchar(64) UNIQUE
            REFERENCES execution_qualification_terminal_acceptances(
              accepted_terminal_submission_sha256),
          terminal_deadline_expiration_sha256 varchar(64) UNIQUE
            REFERENCES execution_qualification_terminal_deadline_expirations(
              terminal_deadline_expiration_sha256),
          execution_id varchar(36) NOT NULL,
          attempt_id varchar(36) NOT NULL UNIQUE REFERENCES execution_attempts(attempt_id),
          topic varchar(96) NOT NULL,
          delivery_key varchar(192) NOT NULL UNIQUE,
          payload_sha256 varchar(64) NOT NULL,
          payload_json jsonb NOT NULL,
          created_at timestamptz NOT NULL,
          CONSTRAINT ck_execution_qualification_terminal_outbox_hashes CHECK (
            outbox_id = 'qto_' || terminal_authority_sha256 AND
            terminal_authority_sha256 ~ '^[0-9a-f]{64}$' AND
            payload_sha256 ~ '^[0-9a-f]{64}$' AND
            payload_sha256 = terminal_authority_sha256 AND
            ((terminal_authority_kind = 'accepted_terminal_submission' AND
              accepted_terminal_submission_sha256 IS NOT NULL AND
              accepted_terminal_submission_sha256 = terminal_authority_sha256 AND
              terminal_deadline_expiration_sha256 IS NULL) OR
             (terminal_authority_kind = 'terminal_deadline_expiration' AND
              terminal_deadline_expiration_sha256 IS NOT NULL AND
              terminal_deadline_expiration_sha256 = terminal_authority_sha256 AND
              accepted_terminal_submission_sha256 IS NULL)) AND
            topic = 'execution.qualification_terminal.v2' AND
            delivery_key = 'execution-v2:' || execution_id || ':' || attempt_id)
        );
        CREATE INDEX ix_execution_qualification_terminal_outbox_terminal_aut_19b6
          ON execution_qualification_terminal_outbox (terminal_authority_sha256);
        CREATE INDEX ix_execution_qualification_terminal_outbox_accepted_ter_efc6
          ON execution_qualification_terminal_outbox (
            accepted_terminal_submission_sha256);
        CREATE INDEX ix_execution_qualification_terminal_outbox_terminal_dea_54bd
          ON execution_qualification_terminal_outbox (
            terminal_deadline_expiration_sha256);
        CREATE INDEX ix_execution_qualification_terminal_outbox_execution_id
          ON execution_qualification_terminal_outbox (execution_id);
        CREATE INDEX ix_execution_qualification_terminal_outbox_attempt_id
          ON execution_qualification_terminal_outbox (attempt_id);
        CREATE INDEX ix_execution_qualification_terminal_outbox_created_at
          ON execution_qualification_terminal_outbox (created_at);
        """
    )

    op.execute(
        r"""
        CREATE FUNCTION aletheia_execution_json_shape(value jsonb, shape jsonb)
        RETURNS boolean LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
        DECLARE
          field_name text;
          allowed_types text;
          observed_type text;
          value_count bigint;
          shape_count bigint;
        BEGIN
          IF jsonb_typeof(value) IS DISTINCT FROM 'object' OR
             jsonb_typeof(shape) IS DISTINCT FROM 'object' THEN
            RETURN false;
          END IF;
          SELECT count(*) INTO value_count FROM jsonb_object_keys(value);
          SELECT count(*) INTO shape_count FROM jsonb_object_keys(shape);
          IF value_count IS DISTINCT FROM shape_count THEN RETURN false; END IF;
          FOR field_name, allowed_types IN SELECT * FROM jsonb_each_text(shape) LOOP
            IF NOT value ? field_name THEN RETURN false; END IF;
            observed_type := jsonb_typeof(value->field_name);
            IF observed_type IS NULL OR
               NOT observed_type = ANY(string_to_array(allowed_types, '|')) THEN
              RETURN false;
            END IF;
          END LOOP;
          RETURN true;
        END;
        $$;

        CREATE FUNCTION aletheia_execution_json_string_array(
          value jsonb, require_sha256 boolean, require_canonical boolean
        ) RETURNS boolean LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
        DECLARE
          item jsonb;
          current_value text;
          previous_value text := NULL;
        BEGIN
          IF jsonb_typeof(value) IS DISTINCT FROM 'array' THEN RETURN false; END IF;
          FOR item IN SELECT element FROM jsonb_array_elements(value) AS values(element)
          LOOP
            IF jsonb_typeof(item) IS DISTINCT FROM 'string' THEN RETURN false; END IF;
            current_value := item #>> '{}';
            IF require_sha256 AND current_value !~ '^[0-9a-f]{64}$' THEN
              RETURN false;
            END IF;
            IF require_canonical AND previous_value IS NOT NULL AND
               current_value <= previous_value THEN
              RETURN false;
            END IF;
            previous_value := current_value;
          END LOOP;
          RETURN true;
        END;
        $$;

        CREATE FUNCTION aletheia_execution_runtime_v2_json_valid(
          value jsonb, expected_schema text
        ) RETURNS boolean LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
        DECLARE shape jsonb;
        BEGIN
          shape := CASE expected_schema
            WHEN 'aletheia.runtime_control_authority_pin' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"policy_sha256":"string","principal_id":"string","key_id":"string",' ||
              '"public_key_ed25519_hex":"string","valid_from":"string",' ||
              '"expires_at":"string","revoked_at":"null|string",' ||
              '"qualification_only":"boolean",' ||
              '"scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.runtime_preparation' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"node_manifest_sha256":"string","node_id":"string","boot_id":"string",' ||
              '"execution_id":"string","infrastructure_attempt_id":"string",' ||
              '"intent_sha256":"string","runtime_id":"string","runtime_engine":"string",' ||
              '"launch_spec_sha256":"string","workload_executable_sha256":"string",' ||
              '"workload_argv":"array","runtime_request_sha256":"string",' ||
              '"enforced_placement_sha256":"string",' ||
              '"input_materialization_receipt_sha256":"string",' ||
              '"output_quota_provisioning_receipt_sha256":"string",' ||
              '"fencing_epoch":"number",' ||
              '"lease_token_sha256":"string","prepared_runtime_locator_sha256":"string",' ||
              '"oci_config_sha256":"string","prepared_at":"string",' ||
              '"prepared_monotonic_ns":"number","qualification_only":"boolean",' ||
              '"scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.runtime_launch_authorization_request' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"request_nonce_sha256":"string","runtime_preparation_sha256":"string",' ||
              '"infrastructure_attempt_id":"string","fencing_epoch":"number",' ||
              '"lease_token_sha256":"string","pre_runtime_absence_epoch":"number",' ||
              '"pre_runtime_absence_receipt_sha256":"null|string",' ||
              '"requested_at":"string","requested_monotonic_ns":"number",' ||
              '"qualification_only":"boolean","scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.runtime_launch_authorization' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"admission_sha256":"string","qualification_grant_sha256":"string",' ||
              '"node_manifest_sha256":"string","node_id":"string","boot_id":"string",' ||
              '"execution_id":"string","infrastructure_attempt_id":"string",' ||
              '"intent_sha256":"string","runtime_preparation_sha256":"string",' ||
              '"authorization_request_sha256":"string","launch_spec_sha256":"string",' ||
              '"oci_config_sha256":"string","workload_executable_sha256":"string",' ||
              '"workload_argv":"array","enforced_placement_sha256":"string",' ||
              '"input_materialization_receipt_sha256":"string","fencing_epoch":"number",' ||
              '"lease_token_sha256":"string","lease_expires_at":"string",' ||
              '"hard_deadline":"string","issued_at":"string","expires_at":"string",' ||
              '"max_launch_delay_ns":"number","runtime_control_policy_sha256":"string",' ||
              '"authorized_by_principal_id":"string","authorization_key_id":"string",' ||
              '"signature_ed25519_hex":"string","qualification_only":"boolean",' ||
              '"scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.node_runtime_identity' THEN
              '{"schema_name":"string","schema_version":"number","node_id":"string",' ||
              '"boot_id":"string","execution_id":"string",' ||
              '"infrastructure_attempt_id":"string","runtime_id":"string",' ||
              '"runtime_engine":"string","launch_spec_sha256":"string",' ||
              '"sandbox_instance_sha256":"string","process_identity_sha256":"string",' ||
              '"started_at":"string","started_monotonic_ns":"number"}'
            WHEN 'aletheia.runtime_launch_evidence' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"preparation_sha256":"string",' ||
              '"runtime_launch_authorization_sha256":"string",' ||
              '"runtime_identity":"object","runtime_identity_sha256":"string",' ||
              '"engine_start_monotonic_lower_bound_ns":"number",' ||
              '"engine_start_monotonic_upper_bound_exclusive_ns":"number",' ||
              '"enforced_placement_sha256":"string",' ||
              '"input_materialization_receipt_sha256":"string",' ||
              '"enforced_fencing_epoch":"number",' ||
              '"enforced_lease_token_sha256":"string",' ||
              '"engine_launch_journal_sha256":"string",' ||
              '"launch_evidence_sha256":"string","observed_at":"string",' ||
              '"observed_monotonic_ns":"number","qualification_only":"boolean",' ||
              '"scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.node_runtime_launch_receipt' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"node_manifest_sha256":"string","launch_evidence":"object",' ||
              '"launch_evidence_sha256":"string","signed_at":"string",' ||
              '"signing_key_id":"string","signature_ed25519_hex":"string",' ||
              '"qualification_only":"boolean","scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.historical_runtime_recovery_grant' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"admission_sha256":"string","qualification_grant_sha256":"string",' ||
              '"intent_sha256":"string","execution_id":"string",' ||
              '"infrastructure_attempt_id":"string","runtime_preparation_sha256":"string",' ||
              '"node_runtime_launch_receipt_sha256":"string",' ||
              '"accepted_runtime_termination_sha256":"null|string",' ||
              '"admitted_at":"string","hard_deadline":"string","issued_at":"string",' ||
              '"recovery_expires_at":"string","runtime_control_policy_sha256":"string",' ||
              '"authorized_by_principal_id":"string","authorization_key_id":"string",' ||
              '"signature_ed25519_hex":"string","recovery_only":"boolean",' ||
              '"launch_allowed":"boolean","qualification_only":"boolean",' ||
              '"scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.runtime_inspection_evidence' THEN
              '{"schema_name":"string","schema_version":"number","state":"string",' ||
              '"preparation_sha256":"string","runtime_identity":"null|object",' ||
              '"runtime_identity_sha256":"null|string",' ||
              '"enforced_placement_sha256":"string",' ||
              '"input_materialization_receipt_sha256":"string",' ||
              '"enforced_fencing_epoch":"number",' ||
              '"enforced_lease_token_sha256":"string",' ||
              '"inspection_evidence_sha256":"string",' ||
              '"runtime_control_journal_sha256":"string",' ||
              '"prelaunch_absence_journal_sha256":"null|string",' ||
              '"prelaunch_absence_epoch":"null|number",' ||
              '"prelaunch_authorization_request_sha256":"null|string",' ||
              '"prelaunch_authorization_sha256":"null|string",' ||
              '"engine_terminal_journal_sha256":"null|string","inspected_at":"string",' ||
              '"inspected_monotonic_ns":"number","exit_code":"null|number",' ||
              '"ended_at":"null|string","ended_monotonic_ns":"null|number",' ||
              '"qualification_only":"boolean","scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.pre_runtime_absence_receipt' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"node_manifest_sha256":"string","preparation":"object",' ||
              '"preparation_sha256":"string","absence_evidence":"object",' ||
              '"absence_evidence_sha256":"string","signed_at":"string",' ||
              '"expires_at":"string","signing_key_id":"string",' ||
              '"signature_ed25519_hex":"string","qualification_only":"boolean",' ||
              '"scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.pre_runtime_absence_decision_record' THEN
              '{"schema_name":"string","schema_version":"number","attempt_id":"string",' ||
              '"absence_epoch":"number","absence_receipt_sha256":"string",' ||
              '"preparation_sha256":"string",' ||
              '"prior_authorization_request_sha256":"null|string",' ||
              '"prior_authorization_sha256":"null|string","disposition":"string",' ||
              '"replacement_request_sha256":"null|string",' ||
              '"replacement_authorization_sha256":"null|string","decided_at":"string",' ||
              '"runtime_control_pin_sha256":"string","qualification_only":"boolean",' ||
              '"scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.runtime_fence_rebind_request' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"preparation_sha256":"string","runtime_identity_sha256":"string",' ||
              '"previous_fencing_epoch":"number",' ||
              '"previous_lease_token_sha256":"string","new_fencing_epoch":"number",' ||
              '"new_lease_token_sha256":"string","rebind_sequence":"number",' ||
              '"expected_runtime_control_journal_sha256":"string",' ||
              '"requested_at":"string","requested_monotonic_ns":"number",' ||
              '"qualification_only":"boolean","scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.runtime_fence_rebind_evidence' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"request_sha256":"string","preparation_sha256":"string",' ||
              '"runtime_identity_sha256":"string","previous_fencing_epoch":"number",' ||
              '"previous_lease_token_sha256":"string","new_fencing_epoch":"number",' ||
              '"new_lease_token_sha256":"string","rebind_sequence":"number",' ||
              '"previous_runtime_control_journal_sha256":"string",' ||
              '"new_runtime_control_journal_sha256":"string",' ||
              '"rebind_evidence_sha256":"string","rebound_at":"string",' ||
              '"rebound_monotonic_ns":"number","qualification_only":"boolean",' ||
              '"scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.runtime_fence_rebind_receipt' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"node_manifest_sha256":"string","evidence":"object",' ||
              '"evidence_sha256":"string","signed_at":"string",' ||
              '"signing_key_id":"string","signature_ed25519_hex":"string",' ||
              '"qualification_only":"boolean","scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.runtime_termination_acceptance_challenge' THEN
              '{"schema_name":"string","schema_version":"number","challenge_id":"string",' ||
              '"attempt_id":"string","execution_id":"string","intent_sha256":"string",' ||
              '"node_manifest_sha256":"string","runtime_preparation_sha256":"string",' ||
              '"node_runtime_launch_receipt_sha256":"string",' ||
              '"runtime_identity_sha256":"string",' ||
              '"runtime_inspection_evidence_sha256":"string",' ||
              '"inspection_sequence":"number","node_inventory_sha256":"string",' ||
              '"resource_lease_sha256":"string","fencing_epoch":"number",' ||
              '"lease_token_sha256":"string","hard_deadline":"string",' ||
              '"artifact_submission_deadline":"string","challenged_at":"string",' ||
              '"expires_at":"string","runtime_control_policy_sha256":"string",' ||
              '"challenged_by_principal_id":"string","challenge_key_id":"string",' ||
              '"signature_ed25519_hex":"string","qualification_only":"boolean",' ||
              '"scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.node_runtime_termination_receipt' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"node_manifest_sha256":"string","challenge_sha256":"string",' ||
              '"runtime_preparation_sha256":"string",' ||
              '"node_runtime_launch_receipt_sha256":"string",' ||
              '"runtime_launch_authorization_request_sha256":"string",' ||
              '"runtime_launch_authorization_sha256":"string",' ||
              '"inspection_sequence":"number","termination_evidence":"object",' ||
              '"termination_evidence_sha256":"string","signed_at":"string",' ||
              '"expires_at":"string","signing_key_id":"string",' ||
              '"signature_ed25519_hex":"string","qualification_only":"boolean",' ||
              '"scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.accepted_runtime_termination' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"challenge_sha256":"string","attempt_id":"string",' ||
              '"runtime_preparation_sha256":"string",' ||
              '"node_runtime_launch_receipt_sha256":"string",' ||
              '"runtime_launch_authorization_request_sha256":"string",' ||
              '"runtime_launch_authorization_sha256":"string",' ||
              '"node_runtime_termination_receipt_sha256":"string",' ||
              '"inspection_sequence":"number","runtime_identity_sha256":"string",' ||
              '"runtime_inspection_evidence_sha256":"string",' ||
              '"engine_terminal_journal_sha256":"string","fencing_epoch":"number",' ||
              '"lease_token_sha256":"string","runtime_ended_at":"string",' ||
              '"exit_code":"number","hard_deadline":"string",' ||
              '"artifact_submission_deadline":"string","proof_signed_at":"string",' ||
              '"proof_expires_at":"string","accepted_at":"string",' ||
              '"billable_ended_at":"string","runtime_control_policy_sha256":"string",' ||
              '"accepted_by_principal_id":"string","acceptance_key_id":"string",' ||
              '"signature_ed25519_hex":"string","proof_was_fresh":"boolean",' ||
              '"compute_release_allowed":"boolean",' ||
              '"scientific_admission_allowed":"boolean","qualification_only":"boolean"}'
            WHEN 'aletheia.qualification_terminal_deadline_expiration' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"attempt_id":"string","execution_id":"string","intent_sha256":"string",' ||
              '"node_id":"string","node_manifest_sha256":"string",' ||
              '"node_inventory_sha256":"string","resource_lease_sha256":"string",' ||
              '"runtime_preparation_sha256":"string",' ||
              '"runtime_launch_authorization_request_sha256":"string",' ||
              '"runtime_launch_authorization_sha256":"string",' ||
              '"node_runtime_launch_receipt_sha256":"string",' ||
              '"runtime_termination_challenge_sha256":"string",' ||
              '"node_runtime_termination_receipt_sha256":"string",' ||
              '"accepted_runtime_termination_sha256":"string",' ||
              '"runtime_identity_sha256":"string",' ||
              '"runtime_inspection_evidence_sha256":"string",' ||
              '"engine_terminal_journal_sha256":"string",' ||
              '"inspection_sequence":"number","fencing_epoch":"number",' ||
              '"lease_token_sha256":"string","runtime_ended_at":"string",' ||
              '"exit_code":"number","hard_deadline":"string",' ||
              '"artifact_submission_deadline":"string",' ||
              '"accepted_runtime_termination_at":"string","authorized_at":"string",' ||
              '"expired_at":"string","reason":"string","disposition":"string",' ||
              '"retryable":"boolean",' ||
              '"conditional_on_terminal_submission_absence":"boolean",' ||
              '"database_time_activation_required":"boolean",' ||
              '"runtime_control_policy_sha256":"string",' ||
              '"adjudicated_by_principal_id":"string","adjudication_key_id":"string",' ||
              '"signature_ed25519_hex":"string","qualification_only":"boolean",' ||
              '"scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.qualification_terminal_submission' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"node_manifest_sha256":"string","intent_sha256":"string",' ||
              '"execution_id":"string","attempt_id":"string",' ||
              '"node_inventory_sha256":"string","resource_lease_sha256":"string",' ||
              '"fencing_epoch":"number","lease_token_sha256":"string",' ||
              '"accepted_runtime_termination_sha256":"string",' ||
              '"artifact_manifest_sha256":"string","output_tree_sha256":"string",' ||
              '"artifact_verified_receipt_sha256s":"array","disposition":"string",' ||
              '"submitted_at":"string","signing_key_id":"string",' ||
              '"signature_ed25519_hex":"string","qualification_only":"boolean",' ||
              '"scientific_admission_allowed":"boolean"}'
            WHEN 'aletheia.artifact_manifest' THEN
              '{"schema_name":"string","schema_version":"number","intent_sha256":"string",' ||
              '"execution_id":"string","replicate_slot_id":"string",' ||
              '"infrastructure_attempt_id":"string","entries":"array",' ||
              '"produced_at":"string"}'
            WHEN 'aletheia.artifact_manifest_entry' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"expected_artifact_id":"string","artifact_key":"string","role":"string",' ||
              '"content_sha256":"string","bytes":"number","media_type":"string",' ||
              '"schema_sha256":"null|string","quarantine_ref":"string"}'
            WHEN 'aletheia.artifact_verified_receipt' THEN
              '{"schema_name":"string","schema_version":"number",' ||
              '"artifact_manifest_sha256":"string","producer_attempt_id":"string",' ||
              '"artifact":"object","custody_mode":"string",' ||
              '"verifier_principal_id":"string","object_store_id":"string",' ||
              '"final_object_ref":"string","final_object_version":"string",' ||
              '"custody_receipt_sha256s":"array","verified_at":"string"}'
            WHEN 'aletheia.accepted_qualification_terminal_submission' THEN
              '{"schema_name":"string","schema_version":"number","attempt_id":"string",' ||
              '"node_manifest_sha256":"string","terminal_submission_sha256":"string",' ||
              '"accepted_runtime_termination_sha256":"string",' ||
              '"artifact_manifest_sha256":"string","output_tree_sha256":"string",' ||
              '"artifact_verified_receipt_sha256s":"array","disposition":"string",' ||
              '"node_submitted_at":"string","artifact_submission_deadline":"string",' ||
              '"accepted_at":"string","runtime_control_policy_sha256":"string",' ||
              '"accepted_by_principal_id":"string","acceptance_key_id":"string",' ||
              '"signature_ed25519_hex":"string","qualification_only":"boolean",' ||
              '"scientific_admission_allowed":"boolean"}'
            ELSE NULL
          END;
          IF NOT (
            shape IS NOT NULL AND aletheia_execution_json_shape(value, shape)
            AND value->>'schema_name' IS NOT DISTINCT FROM expected_schema
            AND value->>'schema_version' IS NOT DISTINCT FROM
              CASE WHEN expected_schema IN (
                'aletheia.node_runtime_identity',
                'aletheia.artifact_manifest',
                'aletheia.artifact_manifest_entry',
                'aletheia.artifact_verified_receipt'
              ) THEN '1' ELSE '2' END
            AND (NOT value ? 'qualification_only' OR
                 value->>'qualification_only' IS NOT DISTINCT FROM 'true')
            AND (NOT value ? 'scientific_admission_allowed' OR
                 value->>'scientific_admission_allowed' IS NOT DISTINCT FROM 'false')
          ) THEN
            RETURN false;
          END IF;
          IF expected_schema = 'aletheia.runtime_control_authority_pin' AND NOT (
               (value->>'valid_from')::timestamptz <
                 (value->>'expires_at')::timestamptz AND
               (
                 jsonb_typeof(value->'revoked_at') = 'null' OR
                 (value->>'revoked_at')::timestamptz BETWEEN
                   (value->>'valid_from')::timestamptz AND
                   (value->>'expires_at')::timestamptz
               )
             ) THEN
            RETURN false;
          END IF;
          RETURN true;
        END;
        $$;
        """
    )

    for table in _APPEND_ONLY_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION aletheia_execution_reject_mutation()
            """
        )

    _replace_v1_guards_for_runtime_v2()
    _extend_attempt_bundle_for_runtime_v2()
    _install_runtime_v2_guards()


def _backup_v1_guard_functions() -> None:
    """Retain exact 0024 guards so a one-step downgrade remains a valid 0025 schema."""

    op.execute(
        r"""
        DO $migration$
        DECLARE
          source_name text;
          backup_name text;
          definition text;
        BEGIN
          FOR source_name, backup_name IN
            SELECT * FROM (VALUES
              ('aletheia_execution_guard_attempt',
               'aletheia_execution_guard_attempt_v1_0026_backup'),
              ('aletheia_execution_guard_lease_state',
               'aletheia_execution_guard_lease_state_v1_0026_backup'),
              ('aletheia_execution_check_attempt_bundle',
               'aletheia_execution_check_attempt_bundle_v1_0026_backup')
            ) names(source_name, backup_name)
          LOOP
            SELECT pg_get_functiondef(to_regprocedure(source_name || '()')) INTO definition;
            IF definition IS NULL THEN
              RAISE EXCEPTION '0026 requires frozen 0024 function %', source_name;
            END IF;
            definition := replace(
              definition,
              'FUNCTION public.' || source_name || '()',
              'FUNCTION public.' || backup_name || '()'
            );
            EXECUTE definition;
          END LOOP;
        END;
        $migration$;
        """
    )


def _replace_v1_guards_for_runtime_v2() -> None:
    """Replace only transition guards whose v1 assumptions predate inert preparation."""

    op.execute("DROP TRIGGER trg_execution_attempts_guard ON execution_attempts")
    op.execute("DROP FUNCTION aletheia_execution_guard_attempt()")
    op.execute(
        r"""
        CREATE FUNCTION aletheia_execution_guard_attempt() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          allowed boolean;
          adopting boolean;
          finalizing boolean;
          terminalizing boolean;
          absence_releasing boolean;
          accepting_termination boolean;
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'execution attempt is not deletable' USING ERRCODE = '55000';
          END IF;
          IF (NEW.attempt_id, NEW.execution_id, NEW.attempt_number, NEW.intent_sha256,
              NEW.intent_json, NEW.admission_sha256, NEW.grant_sha256, NEW.bundle_sha256,
              NEW.cost_quote_sha256, NEW.node_id, NEW.node_inventory_sha256,
              NEW.authorized_at, NEW.reserved_at, NEW.hard_deadline)
             IS DISTINCT FROM
             (OLD.attempt_id, OLD.execution_id, OLD.attempt_number, OLD.intent_sha256,
              OLD.intent_json, OLD.admission_sha256, OLD.grant_sha256, OLD.bundle_sha256,
              OLD.cost_quote_sha256, OLD.node_id, OLD.node_inventory_sha256,
              OLD.authorized_at, OLD.reserved_at, OLD.hard_deadline) THEN
            RAISE EXCEPTION 'execution attempt authority is immutable' USING ERRCODE = '55000';
          END IF;
          IF NEW.state_version <> OLD.state_version + 1 THEN
            RAISE EXCEPTION 'execution attempt state_version must advance exactly once'
              USING ERRCODE = '40001';
          END IF;
          IF NEW.updated_at < OLD.updated_at OR NEW.heartbeat_at < OLD.heartbeat_at OR
             NEW.lease_expires_at < OLD.lease_expires_at OR
             NEW.lease_expires_at > NEW.hard_deadline THEN
            RAISE EXCEPTION 'attempt clock/heartbeat/lease expiry is non-monotonic'
              USING ERRCODE = '55000';
          END IF;

          adopting := NEW.fencing_epoch = OLD.fencing_epoch + 1
            AND NEW.adoption_count = OLD.adoption_count + 1
            AND NEW.latest_adoption_sha256 IS NOT NULL
            AND NEW.lease_token_sha256 <> OLD.lease_token_sha256
            AND NEW.status = 'running'
            AND EXISTS (
              SELECT 1 FROM execution_attempt_adoptions d
               WHERE d.adoption_sha256 = NEW.latest_adoption_sha256
                 AND d.attempt_id = OLD.attempt_id
                 AND d.sequence = NEW.adoption_count
                 AND d.previous_fencing_epoch = OLD.fencing_epoch
                 AND d.new_fencing_epoch = NEW.fencing_epoch
                 AND d.previous_lease_token_sha256 = OLD.lease_token_sha256
                 AND d.new_lease_token_sha256 = NEW.lease_token_sha256
                 AND d.runtime_identity_sha256 = NEW.runtime_identity_sha256
            );
          absence_releasing := NEW.status = 'cancelled'
            AND NEW.accepted_runtime_termination_sha256 IS NULL
            AND NEW.latest_pre_runtime_absence_receipt_sha256 IS NOT NULL
            AND EXISTS (
              SELECT 1 FROM execution_pre_runtime_absence_decisions d
               WHERE d.attempt_id = NEW.attempt_id
                 AND d.absence_receipt_sha256 =
                   NEW.latest_pre_runtime_absence_receipt_sha256
                 AND d.disposition = 'released'
            );
          accepting_termination := NEW.accepted_runtime_termination_sha256 IS NOT NULL
            AND OLD.accepted_runtime_termination_sha256 IS NULL
            AND EXISTS (
              SELECT 1 FROM execution_runtime_termination_acceptances t
               WHERE t.attempt_id = NEW.attempt_id
                 AND t.accepted_termination_sha256 =
                   NEW.accepted_runtime_termination_sha256
            );
          terminalizing := NEW.status IN ('succeeded','failed','cancelled') AND (
            (NEW.terminal_receipt_sha256 IS NOT NULL AND EXISTS (
              SELECT 1 FROM execution_terminal_receipts r
               WHERE r.receipt_sha256 = NEW.terminal_receipt_sha256
                 AND r.attempt_id = NEW.attempt_id
            )) OR
            (NEW.accepted_terminal_submission_sha256 IS NOT NULL AND EXISTS (
              SELECT 1 FROM execution_qualification_terminal_acceptances q
               WHERE q.accepted_terminal_submission_sha256 =
                       NEW.accepted_terminal_submission_sha256
                 AND q.attempt_id = NEW.attempt_id
            )) OR
            (NEW.terminal_deadline_expiration_sha256 IS NOT NULL AND EXISTS (
              SELECT 1
                FROM execution_qualification_terminal_deadline_expirations x
               WHERE x.terminal_deadline_expiration_sha256 =
                       NEW.terminal_deadline_expiration_sha256
                 AND x.attempt_id = NEW.attempt_id
                 AND NEW.accepted_terminal_submission_sha256 IS NULL
            )) OR absence_releasing
          );
          finalizing := OLD.status = 'reconciliation_required' AND terminalizing;

          IF NOT adopting AND
             (NEW.fencing_epoch, NEW.lease_token_sha256, NEW.adoption_count,
              NEW.latest_adoption_sha256) IS DISTINCT FROM
             (OLD.fencing_epoch, OLD.lease_token_sha256, OLD.adoption_count,
              OLD.latest_adoption_sha256) THEN
            RAISE EXCEPTION 'fence/token rotation requires an exact adoption receipt'
              USING ERRCODE = '55000';
          END IF;
          IF OLD.runtime_identity_sha256 IS NOT NULL AND
             (NEW.runtime_identity_sha256, NEW.runtime_identity_json) IS DISTINCT FROM
             (OLD.runtime_identity_sha256, OLD.runtime_identity_json) THEN
            RAISE EXCEPTION 'runtime identity is immutable once bound' USING ERRCODE = '55000';
          END IF;
          IF OLD.runtime_identity_sha256 IS NULL AND NEW.runtime_identity_sha256 IS NOT NULL AND
             NOT (
               (OLD.status = 'reserved' AND NEW.status = 'starting') OR
               (OLD.status IN ('starting','reconciliation_required') AND
                NEW.status IN ('running','reconciliation_required') AND
                NEW.node_runtime_launch_receipt_sha256 IS NOT NULL AND EXISTS (
                  SELECT 1 FROM execution_runtime_launch_receipts l
                   WHERE l.attempt_id = NEW.attempt_id
                     AND l.launch_receipt_sha256 = NEW.node_runtime_launch_receipt_sha256
                ))
             ) THEN
            RAISE EXCEPTION 'runtime identity may bind only from exact launch evidence'
              USING ERRCODE = '55000';
          END IF;
          IF OLD.runtime_identity_sha256 IS NULL AND NEW.runtime_identity_sha256 IS NOT NULL AND
             (NEW.runtime_identity_json->>'started_at')::timestamptz NOT BETWEEN
               NEW.reserved_at AND NEW.updated_at THEN
            RAISE EXCEPTION 'runtime start is outside its reservation/DB observation order'
              USING ERRCODE = '55000';
          END IF;
          IF (NEW.last_runtime_inspection_sequence, NEW.last_runtime_inspection_sha256,
              NEW.last_runtime_inspected_at, NEW.last_runtime_inspected_monotonic_ns)
             IS DISTINCT FROM
             (OLD.last_runtime_inspection_sequence, OLD.last_runtime_inspection_sha256,
              OLD.last_runtime_inspected_at, OLD.last_runtime_inspected_monotonic_ns) AND
             (NEW.last_runtime_inspection_sequence <= OLD.last_runtime_inspection_sequence OR
              NEW.last_runtime_inspected_at IS NULL OR
              NEW.last_runtime_inspected_monotonic_ns IS NULL OR
              (OLD.last_runtime_inspected_at IS NOT NULL AND
               NEW.last_runtime_inspected_at <= OLD.last_runtime_inspected_at) OR
              (OLD.last_runtime_inspected_monotonic_ns IS NOT NULL AND
               NEW.last_runtime_inspected_monotonic_ns <=
                 OLD.last_runtime_inspected_monotonic_ns) OR
              NOT (adopting OR terminalizing OR accepting_termination)) THEN
            RAISE EXCEPTION 'runtime inspection order may advance only by signed adoption/exit'
              USING ERRCODE = '55000';
          END IF;
          IF NEW.status IN ('starting','running','terminated','verifying',
                            'succeeded','failed','cancelled') AND
             NEW.runtime_identity_sha256 IS NULL AND
             NOT (NEW.status = 'starting' AND NEW.runtime_preparation_sha256 IS NOT NULL) AND
             NOT absence_releasing THEN
            RAISE EXCEPTION 'launched/terminal attempt requires exact runtime identity'
              USING ERRCODE = '55000';
          END IF;

          allowed := NEW.status = OLD.status OR
            (OLD.status = 'reserved' AND NEW.status IN
              ('starting','reconciliation_required')) OR
            (OLD.status = 'starting' AND NEW.status IN
              ('running','reconciliation_required')) OR
            (OLD.status = 'running' AND NEW.status IN
              ('terminated','verifying','reconciliation_required')) OR
            (OLD.status = 'terminated' AND NEW.status IN
              ('verifying','reconciliation_required')) OR
            (OLD.status = 'verifying' AND NEW.status IN
              ('reconciliation_required')) OR
            (OLD.status IN ('running','terminated','verifying') AND terminalizing) OR
            (OLD.status IN ('reserved','starting') AND absence_releasing) OR
            (OLD.status = 'reconciliation_required' AND (adopting OR finalizing OR
                                                          accepting_termination));
          IF NOT allowed THEN
            RAISE EXCEPTION 'invalid execution attempt transition % -> %', OLD.status, NEW.status
              USING ERRCODE = '55000';
          END IF;
          IF OLD.terminal_receipt_sha256 IS NOT NULL AND
             NEW.terminal_receipt_sha256 IS DISTINCT FROM OLD.terminal_receipt_sha256 THEN
            RAISE EXCEPTION 'terminal receipt identity is immutable' USING ERRCODE = '55000';
          END IF;
          RETURN NEW;
        END;
        $$;
        CREATE TRIGGER trg_execution_attempts_guard
          BEFORE UPDATE OR DELETE ON execution_attempts
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_guard_attempt();
        """
    )

    # A retained reconciliation lease may be resolved only by a v1 receipt, a fresh v2 runtime
    # termination acceptance, or a signed never-started proof.  Held leases already permit a
    # release transition; the deferred bundle guard below proves its matching authority.
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION aletheia_execution_guard_lease_state() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          adopting boolean := false;
          finalizing boolean := false;
          new_fence bigint := (to_jsonb(NEW)->>'fencing_epoch')::bigint;
          old_fence bigint := (to_jsonb(OLD)->>'fencing_epoch')::bigint;
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION '% is not deletable', TG_TABLE_NAME USING ERRCODE = '55000';
          END IF;
          IF TG_TABLE_NAME = 'execution_resource_leases' THEN
            IF (to_jsonb(NEW) - ARRAY['state','fencing_epoch','heartbeat_at',
                                      'lease_expires_at','released_at'])
               IS DISTINCT FROM
               (to_jsonb(OLD) - ARRAY['state','fencing_epoch','heartbeat_at',
                                      'lease_expires_at','released_at']) THEN
              RAISE EXCEPTION 'resource lease identity is immutable' USING ERRCODE = '55000';
            END IF;
          ELSIF TG_TABLE_NAME = 'execution_device_leases' THEN
            IF (to_jsonb(NEW) - ARRAY['state','fencing_epoch','released_at']) IS DISTINCT FROM
               (to_jsonb(OLD) - ARRAY['state','fencing_epoch','released_at']) THEN
              RAISE EXCEPTION 'device lease identity is immutable' USING ERRCODE = '55000';
            END IF;
          ELSE
            IF (to_jsonb(NEW) - ARRAY['state','actual_lease_seconds',
                                      'settled_microunits','settled_at'])
               IS DISTINCT FROM
               (to_jsonb(OLD) - ARRAY['state','actual_lease_seconds',
                                      'settled_microunits','settled_at']) THEN
              RAISE EXCEPTION 'budget reservation identity is immutable' USING ERRCODE = '55000';
            END IF;
            adopting := OLD.state = 'reconciliation_required' AND NEW.state = 'held'
              AND EXISTS (
                SELECT 1 FROM execution_attempts a
                 JOIN execution_attempt_adoptions d
                   ON d.adoption_sha256 = a.latest_adoption_sha256
                 WHERE a.attempt_id = NEW.attempt_id AND a.status = 'running'
              );
          END IF;
          IF new_fence IS DISTINCT FROM old_fence THEN
            adopting := new_fence = old_fence + 1 AND EXISTS (
              SELECT 1 FROM execution_attempt_adoptions d
               WHERE d.attempt_id = (to_jsonb(NEW)->>'attempt_id')
                 AND d.previous_fencing_epoch = old_fence
                 AND d.new_fencing_epoch = new_fence
            );
            IF NOT adopting THEN
              RAISE EXCEPTION 'lease fence rotation lacks exact adoption receipt'
                USING ERRCODE = '55000';
            END IF;
          END IF;
          finalizing := OLD.state = 'reconciliation_required'
            AND NEW.state IN ('released','settled') AND (
              EXISTS (SELECT 1 FROM execution_terminal_receipts r
                       WHERE r.attempt_id = (to_jsonb(NEW)->>'attempt_id')) OR
              EXISTS (SELECT 1 FROM execution_runtime_termination_acceptances r
                       WHERE r.attempt_id = (to_jsonb(NEW)->>'attempt_id')) OR
              EXISTS (SELECT 1 FROM execution_pre_runtime_absence_decisions d
                       WHERE d.attempt_id = (to_jsonb(NEW)->>'attempt_id')
                         AND d.disposition = 'released')
            );
          IF NOT (
            NEW.state = OLD.state OR
            (OLD.state = 'held' AND NEW.state IN ('reconciliation_required','released','settled')) OR
            (OLD.state = 'reconciliation_required' AND NEW.state = 'held' AND adopting) OR
            finalizing
          ) THEN
            RAISE EXCEPTION 'invalid % state transition % -> %', TG_TABLE_NAME,
              OLD.state, NEW.state USING ERRCODE = '55000';
          END IF;
          IF OLD.state IN ('released','settled') AND NEW.state <> OLD.state THEN
            RAISE EXCEPTION '% terminal/reconciliation state is sticky', TG_TABLE_NAME
              USING ERRCODE = '55000';
          END IF;
          IF TG_TABLE_NAME = 'execution_budget_reservations' AND
             OLD.state IN ('released','settled') AND
             (to_jsonb(NEW)->'actual_lease_seconds', to_jsonb(NEW)->'settled_microunits',
              to_jsonb(NEW)->'settled_at') IS DISTINCT FROM
             (to_jsonb(OLD)->'actual_lease_seconds', to_jsonb(OLD)->'settled_microunits',
              to_jsonb(OLD)->'settled_at') THEN
            RAISE EXCEPTION 'terminal budget settlement is immutable'
              USING ERRCODE = '55000';
          END IF;
          IF TG_TABLE_NAME = 'execution_resource_leases' THEN
            IF NEW.heartbeat_at < OLD.heartbeat_at OR
               NEW.lease_expires_at < OLD.lease_expires_at OR
               NEW.lease_expires_at > (SELECT hard_deadline FROM execution_attempts
                                        WHERE attempt_id = NEW.attempt_id) THEN
              RAISE EXCEPTION 'resource lease heartbeat/expiry is non-monotonic or past deadline'
                USING ERRCODE = '55000';
            END IF;
          END IF;
          RETURN NEW;
        END;
        $$;
        """
    )


def _extend_attempt_bundle_for_runtime_v2() -> None:
    """Keep all 0024 placement checks while extending only its hold-state decision tree."""

    op.execute(
        r"""
        DO $migration$
        DECLARE
          definition text;
          needle text;
          replacement text;
        BEGIN
          SELECT pg_get_functiondef(
            'aletheia_execution_check_attempt_bundle()'::regprocedure
          ) INTO definition;
          needle := $needle$IF attempt_row.status = 'reconciliation_required' THEN$needle$;
          replacement := $replacement$
          IF attempt_row.accepted_runtime_termination_sha256 IS NOT NULL THEN
            IF attempt_row.status NOT IN ('verifying','succeeded','failed') OR
               lease_row.state <> 'released' OR reservation_row.state <> 'settled' OR
               EXISTS (SELECT 1 FROM execution_device_leases d
                        WHERE d.attempt_id = attempt_row.attempt_id
                          AND d.state <> 'released') OR
               attempt_row.terminal_receipt_sha256 IS NOT NULL OR
               NOT EXISTS (
                 SELECT 1 FROM execution_runtime_termination_acceptances t
                  WHERE t.attempt_id = attempt_row.attempt_id
                    AND t.accepted_termination_sha256 =
                      attempt_row.accepted_runtime_termination_sha256
               ) OR
               (attempt_row.status = 'verifying' AND
                head_attempt IS DISTINCT FROM attempt_row.attempt_id) OR
               (attempt_row.status IN ('succeeded','failed') AND head_attempt IS NOT NULL) THEN
              RAISE EXCEPTION
                'accepted runtime termination must release compute while preserving artifact grace'
                USING ERRCODE = '23514';
            END IF;
          ELSIF attempt_row.status = 'reconciliation_required' THEN$replacement$;
          IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0024 attempt-bundle state branch is not the expected frozen form';
          END IF;
          definition := replace(definition, needle, replacement);

          needle := $needle$
          ELSIF attempt_row.status IN ('succeeded','failed','cancelled') THEN
          $needle$;
          replacement := $replacement$
          ELSIF attempt_row.status = 'cancelled' AND
                attempt_row.accepted_runtime_termination_sha256 IS NULL AND
                EXISTS (
                  SELECT 1 FROM execution_pre_runtime_absence_decisions d
                   WHERE d.attempt_id = attempt_row.attempt_id
                     AND d.absence_receipt_sha256 =
                       attempt_row.latest_pre_runtime_absence_receipt_sha256
                     AND d.disposition = 'released'
                ) THEN
            IF lease_row.state <> 'released' OR reservation_row.state <> 'released' OR
               EXISTS (SELECT 1 FROM execution_device_leases d
                        WHERE d.attempt_id = attempt_row.attempt_id
                          AND d.state <> 'released') OR
               head_attempt IS NOT NULL OR attempt_row.terminal_receipt_sha256 IS NOT NULL OR
               attempt_row.runtime_identity_sha256 IS NOT NULL THEN
              RAISE EXCEPTION
                'pre-runtime cancellation must release holds only with exact absence proof'
                USING ERRCODE = '23514';
            END IF;
          ELSIF attempt_row.status IN ('succeeded','failed','cancelled') THEN
          $replacement$;
          IF position(needle IN definition) = 0 THEN
            RAISE EXCEPTION '0024 terminal attempt-bundle branch is not the expected frozen form';
          END IF;
          definition := replace(definition, needle, replacement);
          EXECUTE definition;
        END;
        $migration$;
        """
    )


def _install_runtime_v2_guards() -> None:
    # Implemented below in a separate block to keep physical DDL reviewable.
    _install_runtime_v2_attempt_head_guard()
    _install_runtime_v2_completeness_guard()


def _install_runtime_v2_attempt_head_guard() -> None:
    op.execute(
        r"""
        CREATE FUNCTION aletheia_execution_guard_runtime_v2_attempt_head() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.runtime_preparation_sha256 IS NOT NULL AND
             NEW.runtime_preparation_sha256 IS DISTINCT FROM OLD.runtime_preparation_sha256 THEN
            RAISE EXCEPTION 'runtime preparation pointer is immutable' USING ERRCODE = '55000';
          END IF;
          IF NEW.runtime_preparation_sha256 IS DISTINCT FROM
             OLD.runtime_preparation_sha256 AND NOT EXISTS (
               SELECT 1 FROM execution_runtime_preparations p
                WHERE p.attempt_id = NEW.attempt_id
                  AND p.preparation_sha256 = NEW.runtime_preparation_sha256
             ) THEN
            RAISE EXCEPTION 'runtime preparation pointer lacks exact row' USING ERRCODE = '55000';
          END IF;
          IF (NEW.runtime_launch_authorization_count,
              NEW.latest_runtime_launch_authorization_sha256) IS DISTINCT FROM
             (OLD.runtime_launch_authorization_count,
              OLD.latest_runtime_launch_authorization_sha256) AND NOT (
                OLD.status IN ('reserved', 'starting') AND
                NEW.status = 'starting' AND
                OLD.node_runtime_launch_receipt_sha256 IS NULL AND
                NEW.node_runtime_launch_receipt_sha256 IS NULL AND
                OLD.accepted_runtime_termination_sha256 IS NULL AND
                NEW.accepted_runtime_termination_sha256 IS NULL AND
                OLD.accepted_terminal_submission_sha256 IS NULL AND
                NEW.accepted_terminal_submission_sha256 IS NULL AND
                OLD.terminal_deadline_expiration_sha256 IS NULL AND
                NEW.terminal_deadline_expiration_sha256 IS NULL AND
                NEW.runtime_launch_authorization_count =
                  OLD.runtime_launch_authorization_count + 1 AND
                EXISTS (
                  SELECT 1 FROM execution_runtime_launch_authorizations a
                   WHERE a.attempt_id = NEW.attempt_id
                     AND a.sequence = NEW.runtime_launch_authorization_count
                     AND a.authorization_sha256 =
                       NEW.latest_runtime_launch_authorization_sha256
                )
             ) THEN
            RAISE EXCEPTION 'runtime launch authorization head is non-monotonic'
              USING ERRCODE = '55000';
          END IF;
          IF (NEW.pre_runtime_absence_count,
              NEW.latest_pre_runtime_absence_receipt_sha256) IS DISTINCT FROM
             (OLD.pre_runtime_absence_count,
              OLD.latest_pre_runtime_absence_receipt_sha256) AND NOT (
                OLD.status IN ('reserved', 'starting', 'reconciliation_required') AND
                NEW.status IN ('starting', 'cancelled') AND
                OLD.node_runtime_launch_receipt_sha256 IS NULL AND
                NEW.node_runtime_launch_receipt_sha256 IS NULL AND
                OLD.accepted_runtime_termination_sha256 IS NULL AND
                NEW.accepted_runtime_termination_sha256 IS NULL AND
                OLD.accepted_terminal_submission_sha256 IS NULL AND
                NEW.accepted_terminal_submission_sha256 IS NULL AND
                OLD.terminal_deadline_expiration_sha256 IS NULL AND
                NEW.terminal_deadline_expiration_sha256 IS NULL AND
                NEW.pre_runtime_absence_count = OLD.pre_runtime_absence_count + 1 AND
                EXISTS (
                  SELECT 1 FROM execution_pre_runtime_absence_decisions d
                   WHERE d.attempt_id = NEW.attempt_id
                     AND d.absence_epoch = NEW.pre_runtime_absence_count
                     AND d.absence_receipt_sha256 =
                       NEW.latest_pre_runtime_absence_receipt_sha256
                )
             ) THEN
            RAISE EXCEPTION 'pre-runtime absence head is non-monotonic'
              USING ERRCODE = '55000';
          END IF;
          IF OLD.node_runtime_launch_receipt_sha256 IS NOT NULL AND
             NEW.node_runtime_launch_receipt_sha256 IS DISTINCT FROM
               OLD.node_runtime_launch_receipt_sha256 THEN
            RAISE EXCEPTION 'runtime launch receipt pointer is immutable'
              USING ERRCODE = '55000';
          END IF;
          IF (NEW.runtime_termination_challenge_count,
              NEW.runtime_termination_challenge_sha256) IS DISTINCT FROM
             (OLD.runtime_termination_challenge_count,
              OLD.runtime_termination_challenge_sha256) AND NOT (
                OLD.accepted_runtime_termination_sha256 IS NULL AND
                NEW.runtime_termination_challenge_count =
                  OLD.runtime_termination_challenge_count + 1 AND
                EXISTS (
                  SELECT 1 FROM execution_runtime_termination_challenges c
                   WHERE c.attempt_id = NEW.attempt_id
                     AND c.challenge_sha256 = NEW.runtime_termination_challenge_sha256
                     AND c.inspection_sequence =
                       CASE WHEN OLD.runtime_termination_challenge_sha256 IS NULL
                         THEN OLD.last_runtime_inspection_sequence + 1
                         ELSE 1 + (
                           SELECT prior.inspection_sequence
                             FROM execution_runtime_termination_challenges prior
                            WHERE prior.challenge_sha256 =
                              OLD.runtime_termination_challenge_sha256
                         )
                       END
                     AND (
                       OLD.runtime_termination_challenge_sha256 IS NULL OR
                       NEW.updated_at >= (
                         SELECT prior.expires_at
                           FROM execution_runtime_termination_challenges prior
                          WHERE prior.challenge_sha256 =
                            OLD.runtime_termination_challenge_sha256
                       )
                     )
                )
             ) THEN
            RAISE EXCEPTION 'runtime termination challenge head is non-monotonic'
              USING ERRCODE = '55000';
          END IF;
          IF OLD.accepted_runtime_termination_sha256 IS NOT NULL AND
             NEW.accepted_runtime_termination_sha256 IS DISTINCT FROM
               OLD.accepted_runtime_termination_sha256 THEN
            RAISE EXCEPTION 'accepted runtime termination pointer is immutable'
              USING ERRCODE = '55000';
          END IF;
          IF OLD.accepted_terminal_submission_sha256 IS NOT NULL AND
             NEW.accepted_terminal_submission_sha256 IS DISTINCT FROM
               OLD.accepted_terminal_submission_sha256 THEN
            RAISE EXCEPTION 'accepted terminal submission pointer is immutable'
              USING ERRCODE = '55000';
          END IF;
          IF OLD.terminal_deadline_expiration_sha256 IS NOT NULL AND
             NEW.terminal_deadline_expiration_sha256 IS DISTINCT FROM
               OLD.terminal_deadline_expiration_sha256 THEN
            RAISE EXCEPTION 'terminal deadline expiration pointer is immutable'
              USING ERRCODE = '55000';
          END IF;
          IF NEW.terminal_deadline_expiration_sha256 IS DISTINCT FROM
             OLD.terminal_deadline_expiration_sha256 AND NOT (
               OLD.status = 'verifying' AND NEW.status = 'failed' AND
               OLD.terminal_deadline_expiration_sha256 IS NULL AND
               OLD.accepted_terminal_submission_sha256 IS NULL AND
               NEW.accepted_terminal_submission_sha256 IS NULL AND
               NEW.accepted_runtime_termination_sha256 IS NOT NULL AND
               EXISTS (
                 SELECT 1
                   FROM execution_qualification_terminal_deadline_expirations x
                  WHERE x.attempt_id = NEW.attempt_id
                    AND x.terminal_deadline_expiration_sha256 =
                      NEW.terminal_deadline_expiration_sha256
                    AND x.accepted_runtime_termination_sha256 =
                      NEW.accepted_runtime_termination_sha256
                    AND x.activated_at = NEW.updated_at
               )
             ) THEN
            RAISE EXCEPTION 'terminal deadline expiration pointer lacks exact activation'
              USING ERRCODE = '55000';
          END IF;
          RETURN NEW;
        END;
        $$;
        CREATE TRIGGER trg_execution_attempts_runtime_v2_head_guard
          BEFORE UPDATE ON execution_attempts
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_guard_runtime_v2_attempt_head();
        """
    )


def _install_runtime_v2_completeness_guard() -> None:
    op.execute(
        r"""
        CREATE FUNCTION aletheia_execution_check_runtime_v2_attempt() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          target_attempt text := COALESCE(to_jsonb(NEW)->>'attempt_id',
                                          to_jsonb(OLD)->>'attempt_id');
          a execution_attempts%ROWTYPE;
          p execution_runtime_preparations%ROWTYPE;
          latest_auth execution_runtime_launch_authorizations%ROWTYPE;
          latest_absence execution_pre_runtime_absence_decisions%ROWTYPE;
          launch execution_runtime_launch_receipts%ROWTYPE;
          challenge execution_runtime_termination_challenges%ROWTYPE;
          termination execution_runtime_termination_acceptances%ROWTYPE;
          expiration execution_qualification_terminal_deadline_expirations%ROWTYPE;
          terminal execution_qualification_terminal_acceptances%ROWTYPE;
          auth_count bigint;
          absence_count bigint;
          challenge_count bigint;
          head_attempt text;
          resource_state text;
          resource_lease_sha text;
          budget_state text;
        BEGIN
          IF target_attempt IS NULL THEN RETURN NULL; END IF;
          SELECT * INTO a FROM execution_attempts WHERE attempt_id = target_attempt;
          IF NOT FOUND THEN RETURN NULL; END IF;
          SELECT * INTO p FROM execution_runtime_preparations WHERE attempt_id = target_attempt;
          SELECT count(*) INTO auth_count FROM execution_runtime_launch_authorizations
           WHERE attempt_id = target_attempt;
          SELECT * INTO latest_auth FROM execution_runtime_launch_authorizations
           WHERE attempt_id = target_attempt ORDER BY sequence DESC LIMIT 1;
          SELECT count(*) INTO absence_count FROM execution_pre_runtime_absence_decisions
           WHERE attempt_id = target_attempt;
          SELECT * INTO latest_absence FROM execution_pre_runtime_absence_decisions
           WHERE attempt_id = target_attempt ORDER BY absence_epoch DESC LIMIT 1;
          SELECT * INTO launch FROM execution_runtime_launch_receipts
           WHERE attempt_id = target_attempt;
          SELECT count(*) INTO challenge_count FROM execution_runtime_termination_challenges
           WHERE attempt_id = target_attempt;
          SELECT * INTO challenge FROM execution_runtime_termination_challenges
           WHERE attempt_id = target_attempt ORDER BY inspection_sequence DESC LIMIT 1;
          SELECT * INTO termination FROM execution_runtime_termination_acceptances
           WHERE attempt_id = target_attempt;
          SELECT * INTO expiration
            FROM execution_qualification_terminal_deadline_expirations
           WHERE attempt_id = target_attempt;
          SELECT * INTO terminal FROM execution_qualification_terminal_acceptances
           WHERE attempt_id = target_attempt;
          SELECT active_attempt_id INTO head_attempt FROM execution_heads
           WHERE execution_id = a.execution_id;
          SELECT state, lease_sha256 INTO resource_state, resource_lease_sha
            FROM execution_resource_leases
           WHERE attempt_id = target_attempt;
          SELECT state INTO budget_state FROM execution_budget_reservations
           WHERE attempt_id = target_attempt;

          -- JSONB NOT NULL does not reject JSON null, scalars, missing keys, or extra keys.
          -- Reject every malformed authority object before any field extraction or cast below.
          IF p.preparation_sha256 IS NOT NULL AND NOT
             aletheia_execution_runtime_v2_json_valid(
               p.payload_json, 'aletheia.runtime_preparation') THEN
            RAISE EXCEPTION 'runtime preparation JSON is not the closed schema'
              USING ERRCODE = '23514';
          END IF;
          IF p.preparation_sha256 IS NOT NULL AND (
               NOT aletheia_execution_json_string_array(
                 p.payload_json->'workload_argv', false, false) OR
               jsonb_array_length(p.payload_json->'workload_argv') NOT BETWEEN 1 AND 256
             ) THEN
            RAISE EXCEPTION 'runtime preparation argv is not a closed string array'
              USING ERRCODE = '23514';
          END IF;
          IF EXISTS (
            SELECT 1 FROM execution_runtime_launch_authorizations x
             WHERE x.attempt_id = target_attempt AND (
               NOT aletheia_execution_runtime_v2_json_valid(
                 x.request_json, 'aletheia.runtime_launch_authorization_request') OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 x.authorization_json, 'aletheia.runtime_launch_authorization') OR
               NOT aletheia_execution_json_string_array(
                 x.authorization_json->'workload_argv', false, false) OR
               jsonb_array_length(x.authorization_json->'workload_argv') NOT BETWEEN 1 AND 256 OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 x.runtime_control_pin_json, 'aletheia.runtime_control_authority_pin'))
          ) THEN
            RAISE EXCEPTION 'runtime launch authority JSON is not the closed schema'
              USING ERRCODE = '23514';
          END IF;
          IF launch.launch_receipt_sha256 IS NOT NULL AND (
               NOT aletheia_execution_runtime_v2_json_valid(
                 launch.launch_receipt_json, 'aletheia.node_runtime_launch_receipt') OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 launch.launch_receipt_json->'launch_evidence',
                 'aletheia.runtime_launch_evidence') OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 launch.launch_receipt_json->'launch_evidence'->'runtime_identity',
                 'aletheia.node_runtime_identity') OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 launch.recovery_grant_json, 'aletheia.historical_runtime_recovery_grant') OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 launch.runtime_control_pin_json, 'aletheia.runtime_control_authority_pin')
             ) THEN
            RAISE EXCEPTION 'runtime launch/recovery JSON is not the closed schema'
              USING ERRCODE = '23514';
          END IF;
          IF EXISTS (
            SELECT 1 FROM execution_pre_runtime_absence_decisions d
             WHERE d.attempt_id = target_attempt AND (
               NOT aletheia_execution_runtime_v2_json_valid(
                 d.absence_receipt_json, 'aletheia.pre_runtime_absence_receipt') OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 d.absence_receipt_json->'preparation', 'aletheia.runtime_preparation') OR
               NOT aletheia_execution_json_string_array(
                 d.absence_receipt_json->'preparation'->'workload_argv', false, false) OR
               jsonb_array_length(
                 d.absence_receipt_json->'preparation'->'workload_argv')
                   NOT BETWEEN 1 AND 256 OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 d.absence_receipt_json->'absence_evidence',
                 'aletheia.runtime_inspection_evidence') OR
               d.absence_receipt_json->'absence_evidence'->>'state'
                 IS DISTINCT FROM 'absent' OR
               jsonb_typeof(d.absence_receipt_json->'absence_evidence'->'runtime_identity')
                 IS DISTINCT FROM 'null' OR
               jsonb_typeof(d.absence_receipt_json->'absence_evidence'->
                 'runtime_identity_sha256') IS DISTINCT FROM 'null' OR
               jsonb_typeof(d.absence_receipt_json->'absence_evidence'->'exit_code')
                 IS DISTINCT FROM 'null' OR
               jsonb_typeof(d.absence_receipt_json->'absence_evidence'->'ended_at')
                 IS DISTINCT FROM 'null' OR
               jsonb_typeof(d.absence_receipt_json->'absence_evidence'->
                 'ended_monotonic_ns') IS DISTINCT FROM 'null' OR
               jsonb_typeof(d.absence_receipt_json->'absence_evidence'->
                 'engine_terminal_journal_sha256') IS DISTINCT FROM 'null' OR
               jsonb_typeof(d.absence_receipt_json->'absence_evidence'->
                 'prelaunch_absence_journal_sha256') IS DISTINCT FROM 'string' OR
               jsonb_typeof(d.absence_receipt_json->'absence_evidence'->
                 'prelaunch_absence_epoch') IS DISTINCT FROM 'number' OR
               ((jsonb_typeof(d.absence_receipt_json->'absence_evidence'->
                   'prelaunch_authorization_request_sha256') = 'null') IS DISTINCT FROM
                (jsonb_typeof(d.absence_receipt_json->'absence_evidence'->
                   'prelaunch_authorization_sha256') = 'null')) OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 d.decision_json, 'aletheia.pre_runtime_absence_decision_record') OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 d.runtime_control_pin_json, 'aletheia.runtime_control_authority_pin'))
          ) THEN
            RAISE EXCEPTION 'pre-runtime absence JSON is not the closed schema'
              USING ERRCODE = '23514';
          END IF;
          IF EXISTS (
            SELECT 1 FROM execution_runtime_fence_rebinds r
             WHERE r.attempt_id = target_attempt AND (
               NOT aletheia_execution_runtime_v2_json_valid(
                 r.request_json, 'aletheia.runtime_fence_rebind_request') OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 r.receipt_json, 'aletheia.runtime_fence_rebind_receipt') OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 r.receipt_json->'evidence', 'aletheia.runtime_fence_rebind_evidence'))
          ) THEN
            RAISE EXCEPTION 'runtime fence rebind JSON is not the closed schema'
              USING ERRCODE = '23514';
          END IF;
          IF EXISTS (
            SELECT 1 FROM execution_runtime_termination_challenges c
             WHERE c.attempt_id = target_attempt AND (
               NOT aletheia_execution_runtime_v2_json_valid(
                 c.inspection_evidence_json, 'aletheia.runtime_inspection_evidence') OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 c.inspection_evidence_json->'runtime_identity',
                 'aletheia.node_runtime_identity') OR
               c.inspection_evidence_json->>'state' IS DISTINCT FROM 'terminated' OR
               c.inspection_evidence_json->'runtime_identity'
                 IS DISTINCT FROM a.runtime_identity_json OR
               jsonb_typeof(c.inspection_evidence_json->'runtime_identity_sha256')
                 IS DISTINCT FROM 'string' OR
               jsonb_typeof(c.inspection_evidence_json->'exit_code')
                 IS DISTINCT FROM 'number' OR
               jsonb_typeof(c.inspection_evidence_json->'ended_at')
                 IS DISTINCT FROM 'string' OR
               jsonb_typeof(c.inspection_evidence_json->'ended_monotonic_ns')
                 IS DISTINCT FROM 'number' OR
               jsonb_typeof(c.inspection_evidence_json->'engine_terminal_journal_sha256')
                 IS DISTINCT FROM 'string' OR
               jsonb_typeof(c.inspection_evidence_json->
                 'prelaunch_absence_journal_sha256') IS DISTINCT FROM 'null' OR
               jsonb_typeof(c.inspection_evidence_json->'prelaunch_absence_epoch')
                 IS DISTINCT FROM 'null' OR
               jsonb_typeof(c.inspection_evidence_json->
                 'prelaunch_authorization_request_sha256') IS DISTINCT FROM 'null' OR
               jsonb_typeof(c.inspection_evidence_json->
                 'prelaunch_authorization_sha256') IS DISTINCT FROM 'null' OR
               (c.inspection_evidence_json->>'ended_at')::timestamptz >
                 (c.inspection_evidence_json->>'inspected_at')::timestamptz OR
               (c.inspection_evidence_json->>'ended_at')::timestamptz <
                 (c.inspection_evidence_json->'runtime_identity'->>'started_at')::timestamptz OR
               (c.inspection_evidence_json->>'ended_monotonic_ns')::bigint >
                 (c.inspection_evidence_json->>'inspected_monotonic_ns')::bigint OR
               (c.inspection_evidence_json->>'ended_monotonic_ns')::bigint <
                 (c.inspection_evidence_json->'runtime_identity'->>
                   'started_monotonic_ns')::bigint OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 c.challenge_json, 'aletheia.runtime_termination_acceptance_challenge') OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 c.runtime_control_pin_json, 'aletheia.runtime_control_authority_pin'))
          ) THEN
            RAISE EXCEPTION 'runtime termination challenge JSON is not the closed schema'
              USING ERRCODE = '23514';
          END IF;
          IF termination.accepted_termination_sha256 IS NOT NULL AND (
               NOT aletheia_execution_runtime_v2_json_valid(
                 termination.node_termination_receipt_json,
                 'aletheia.node_runtime_termination_receipt') OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 termination.node_termination_receipt_json->'termination_evidence',
                 'aletheia.runtime_inspection_evidence') OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 termination.node_termination_receipt_json->'termination_evidence'
                   ->'runtime_identity', 'aletheia.node_runtime_identity') OR
               termination.node_termination_receipt_json->'termination_evidence'
                   ->>'state' IS DISTINCT FROM 'terminated' OR
               termination.node_termination_receipt_json->'termination_evidence'
                   ->'runtime_identity' IS DISTINCT FROM a.runtime_identity_json OR
               jsonb_typeof(termination.node_termination_receipt_json->
                   'termination_evidence'->'runtime_identity_sha256')
                 IS DISTINCT FROM 'string' OR
               jsonb_typeof(termination.node_termination_receipt_json->
                   'termination_evidence'->'exit_code') IS DISTINCT FROM 'number' OR
               jsonb_typeof(termination.node_termination_receipt_json->
                   'termination_evidence'->'ended_at') IS DISTINCT FROM 'string' OR
               jsonb_typeof(termination.node_termination_receipt_json->
                   'termination_evidence'->'ended_monotonic_ns')
                 IS DISTINCT FROM 'number' OR
               jsonb_typeof(termination.node_termination_receipt_json->
                   'termination_evidence'->'engine_terminal_journal_sha256')
                 IS DISTINCT FROM 'string' OR
               jsonb_typeof(termination.node_termination_receipt_json->
                   'termination_evidence'->'prelaunch_absence_journal_sha256')
                 IS DISTINCT FROM 'null' OR
               jsonb_typeof(termination.node_termination_receipt_json->
                   'termination_evidence'->'prelaunch_absence_epoch')
                 IS DISTINCT FROM 'null' OR
               jsonb_typeof(termination.node_termination_receipt_json->
                   'termination_evidence'->
                   'prelaunch_authorization_request_sha256')
                 IS DISTINCT FROM 'null' OR
               jsonb_typeof(termination.node_termination_receipt_json->
                   'termination_evidence'->'prelaunch_authorization_sha256')
                 IS DISTINCT FROM 'null' OR
               (termination.node_termination_receipt_json->'termination_evidence'
                   ->>'ended_at')::timestamptz >
                 (termination.node_termination_receipt_json->'termination_evidence'
                   ->>'inspected_at')::timestamptz OR
               (termination.node_termination_receipt_json->'termination_evidence'
                   ->>'ended_at')::timestamptz <
                 (termination.node_termination_receipt_json->'termination_evidence'
                   ->'runtime_identity'->>'started_at')::timestamptz OR
               (termination.node_termination_receipt_json->'termination_evidence'
                   ->>'ended_monotonic_ns')::bigint >
                 (termination.node_termination_receipt_json->'termination_evidence'
                   ->>'inspected_monotonic_ns')::bigint OR
               (termination.node_termination_receipt_json->'termination_evidence'
                   ->>'ended_monotonic_ns')::bigint <
                 (termination.node_termination_receipt_json->'termination_evidence'
                   ->'runtime_identity'->>'started_monotonic_ns')::bigint OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 termination.accepted_termination_json,
                 'aletheia.accepted_runtime_termination') OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 termination.recovery_grant_json,
                 'aletheia.historical_runtime_recovery_grant') OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 termination.conditional_terminal_expiration_json,
                 'aletheia.qualification_terminal_deadline_expiration') OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 termination.runtime_control_pin_json,
                 'aletheia.runtime_control_authority_pin')
             ) THEN
            RAISE EXCEPTION 'runtime termination acceptance JSON is not the closed schema'
              USING ERRCODE = '23514';
          END IF;
          IF expiration.terminal_deadline_expiration_sha256 IS NOT NULL AND (
               NOT aletheia_execution_runtime_v2_json_valid(
                 expiration.payload_json,
                 'aletheia.qualification_terminal_deadline_expiration') OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 expiration.runtime_control_pin_json,
                 'aletheia.runtime_control_authority_pin')
             ) THEN
            RAISE EXCEPTION 'terminal deadline activation JSON is not the closed schema'
              USING ERRCODE = '23514';
          END IF;
          IF terminal.accepted_terminal_submission_sha256 IS NOT NULL AND (
               NOT aletheia_execution_runtime_v2_json_valid(
                 terminal.terminal_submission_json,
                 'aletheia.qualification_terminal_submission') OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 terminal.artifact_manifest_json, 'aletheia.artifact_manifest') OR
               jsonb_typeof(terminal.artifact_verified_receipt_sha256s_json)
                 IS DISTINCT FROM 'array' OR
               jsonb_typeof(terminal.artifact_verified_receipts_json)
                 IS DISTINCT FROM 'array' OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 terminal.accepted_terminal_submission_json,
                 'aletheia.accepted_qualification_terminal_submission') OR
               NOT aletheia_execution_json_string_array(
                 terminal.artifact_verified_receipt_sha256s_json, true, true) OR
               NOT aletheia_execution_json_string_array(
                 terminal.terminal_submission_json->'artifact_verified_receipt_sha256s',
                 true, true) OR
               NOT aletheia_execution_json_string_array(
                 terminal.accepted_terminal_submission_json->
                   'artifact_verified_receipt_sha256s', true, true) OR
               NOT aletheia_execution_runtime_v2_json_valid(
                 terminal.runtime_control_pin_json, 'aletheia.runtime_control_authority_pin')
             ) THEN
            RAISE EXCEPTION 'terminal artifact JSON is not the closed schema'
              USING ERRCODE = '23514';
          END IF;
          IF terminal.accepted_terminal_submission_sha256 IS NOT NULL AND EXISTS (
            SELECT 1
              FROM jsonb_array_elements(terminal.artifact_manifest_json->'entries') entry
             WHERE NOT aletheia_execution_runtime_v2_json_valid(
               entry, 'aletheia.artifact_manifest_entry')
          ) THEN
            RAISE EXCEPTION 'artifact manifest entry JSON is not the closed schema'
              USING ERRCODE = '23514';
          END IF;
          IF terminal.accepted_terminal_submission_sha256 IS NOT NULL AND (
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
                   '[]'::jsonb)
             ) THEN
            RAISE EXCEPTION 'artifact manifest projection is not exact and canonical'
              USING ERRCODE = '23514';
          END IF;
          IF terminal.accepted_terminal_submission_sha256 IS NOT NULL AND EXISTS (
            SELECT 1
              FROM jsonb_array_elements(terminal.artifact_verified_receipts_json) receipt
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
                   receipt->>'producer_attempt_id' IS DISTINCT FROM target_attempt OR
                   NOT EXISTS (
                     SELECT 1
                       FROM jsonb_array_elements(
                         terminal.artifact_manifest_json->'entries') entry
                      WHERE entry IS NOT DISTINCT FROM receipt->'artifact'
                   )
          ) THEN
            RAISE EXCEPTION 'artifact verification receipt JSON is not the closed schema'
              USING ERRCODE = '23514';
          END IF;
          IF terminal.accepted_terminal_submission_sha256 IS NOT NULL AND (
               jsonb_array_length(terminal.artifact_verified_receipts_json)
                 IS DISTINCT FROM
                   jsonb_array_length(terminal.artifact_manifest_json->'entries') OR
               (SELECT jsonb_agg(receipt->'artifact'->'artifact_key' ORDER BY ordinal)
                  FROM jsonb_array_elements(terminal.artifact_verified_receipts_json)
                         WITH ORDINALITY AS receipts(receipt, ordinal))
                 IS DISTINCT FROM
               (SELECT jsonb_agg(entry->'artifact_key' ORDER BY ordinal)
                  FROM jsonb_array_elements(terminal.artifact_manifest_json->'entries')
                         WITH ORDINALITY AS entries(entry, ordinal)) OR
               terminal.artifact_verified_receipts_json IS DISTINCT FROM
                 COALESCE(
                   (SELECT jsonb_agg(receipt ORDER BY
                                      receipt->'artifact'->>'artifact_key')
                      FROM jsonb_array_elements(
                        terminal.artifact_verified_receipts_json) receipt),
                   '[]'::jsonb) OR
               (SELECT count(*)
                  FROM jsonb_array_elements(
                    terminal.artifact_verified_receipts_json) receipt)
                 IS DISTINCT FROM
               (SELECT count(DISTINCT receipt->'artifact'->>'artifact_key')
                  FROM jsonb_array_elements(
                    terminal.artifact_verified_receipts_json) receipt)
             ) THEN
            RAISE EXCEPTION 'artifact verification receipt projection is not canonical'
              USING ERRCODE = '23514';
          END IF;
          IF EXISTS (
            SELECT 1 FROM execution_qualification_terminal_outbox o
             WHERE o.attempt_id = target_attempt AND (
               (o.terminal_authority_kind = 'accepted_terminal_submission' AND
                NOT aletheia_execution_runtime_v2_json_valid(
                  o.payload_json,
                  'aletheia.accepted_qualification_terminal_submission')) OR
               (o.terminal_authority_kind = 'terminal_deadline_expiration' AND
                NOT aletheia_execution_runtime_v2_json_valid(
                  o.payload_json,
                  'aletheia.qualification_terminal_deadline_expiration')) OR
               o.terminal_authority_kind NOT IN (
                 'accepted_terminal_submission',
                 'terminal_deadline_expiration')
             )
          ) THEN
            RAISE EXCEPTION 'qualification terminal outbox JSON is not the closed schema'
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
             absence_count IS DISTINCT FROM a.pre_runtime_absence_count OR
             (absence_count > 0 AND (
               latest_absence.absence_epoch IS DISTINCT FROM absence_count OR
               latest_absence.absence_receipt_sha256 IS DISTINCT FROM
                 a.latest_pre_runtime_absence_receipt_sha256)) OR
             launch.launch_receipt_sha256 IS DISTINCT FROM
               a.node_runtime_launch_receipt_sha256 OR
             challenge_count IS DISTINCT FROM a.runtime_termination_challenge_count OR
             (challenge_count > 0 AND (
               challenge.challenge_sha256 IS DISTINCT FROM
                 a.runtime_termination_challenge_sha256 OR
               challenge.inspection_sequence IS DISTINCT FROM
                 (SELECT min(c.inspection_sequence) + challenge_count - 1
                    FROM execution_runtime_termination_challenges c
                   WHERE c.attempt_id = target_attempt) OR
               (termination.accepted_termination_sha256 IS NULL AND
                (SELECT min(c.inspection_sequence)
                   FROM execution_runtime_termination_challenges c
                  WHERE c.attempt_id = target_attempt) IS DISTINCT FROM
                    a.last_runtime_inspection_sequence + 1) OR
               (termination.accepted_termination_sha256 IS NOT NULL AND
                challenge.inspection_sequence IS DISTINCT FROM
                  a.last_runtime_inspection_sequence))) OR
             termination.accepted_termination_sha256 IS DISTINCT FROM
               a.accepted_runtime_termination_sha256 OR
             terminal.accepted_terminal_submission_sha256 IS DISTINCT FROM
               a.accepted_terminal_submission_sha256 OR
             expiration.terminal_deadline_expiration_sha256 IS DISTINCT FROM
               a.terminal_deadline_expiration_sha256 THEN
            RAISE EXCEPTION 'runtime-v2 child rows differ from attempt heads'
              USING ERRCODE = '23514';
          END IF;

          IF p.preparation_sha256 IS NOT NULL AND (
               p.execution_id IS DISTINCT FROM a.execution_id OR
               p.intent_sha256 IS DISTINCT FROM a.intent_sha256 OR
               p.node_id IS DISTINCT FROM a.node_id OR p.fencing_epoch > a.fencing_epoch OR
               p.payload_sha256 IS DISTINCT FROM p.preparation_sha256 OR
               p.payload_json->>'infrastructure_attempt_id' IS DISTINCT FROM a.attempt_id OR
               p.payload_json->>'execution_id' IS DISTINCT FROM a.execution_id OR
               p.payload_json->>'intent_sha256' IS DISTINCT FROM a.intent_sha256 OR
               p.payload_json->>'node_id' IS DISTINCT FROM a.node_id OR
               p.payload_json->>'node_manifest_sha256' IS DISTINCT FROM p.node_manifest_sha256 OR
               p.payload_json->>'boot_id' IS DISTINCT FROM p.boot_id OR
               (p.payload_json->>'fencing_epoch')::bigint IS DISTINCT FROM p.fencing_epoch OR
               p.payload_json->>'lease_token_sha256' IS DISTINCT FROM p.lease_token_sha256 OR
               p.payload_json->>'output_quota_provisioning_receipt_sha256'
                 !~ '^[0-9a-f]{64}$' OR
               (p.payload_json->>'prepared_at')::timestamptz IS DISTINCT FROM p.prepared_at OR
               (p.payload_json->>'prepared_monotonic_ns')::bigint
                 IS DISTINCT FROM p.prepared_monotonic_ns OR
               p.recorded_at < p.prepared_at OR
               p.payload_json->>'qualification_only' IS DISTINCT FROM 'true' OR
               p.payload_json->>'scientific_admission_allowed' IS DISTINCT FROM 'false'
             ) THEN
            RAISE EXCEPTION 'runtime preparation differs from exact attempt authority'
              USING ERRCODE = '23514';
          END IF;

          IF EXISTS (
            SELECT 1 FROM execution_runtime_launch_authorizations x
             WHERE x.attempt_id = target_attempt AND (
               x.preparation_sha256 IS DISTINCT FROM p.preparation_sha256 OR
               x.request_payload_sha256 IS DISTINCT FROM x.request_sha256 OR
               x.authorization_payload_sha256 IS DISTINCT FROM x.authorization_sha256 OR
               x.request_json->>'runtime_preparation_sha256' IS DISTINCT FROM
                 p.preparation_sha256 OR
               x.request_json->>'infrastructure_attempt_id' IS DISTINCT FROM target_attempt OR
               (x.request_json->>'fencing_epoch')::bigint IS DISTINCT FROM p.fencing_epoch OR
               x.request_json->>'lease_token_sha256' IS DISTINCT FROM p.lease_token_sha256 OR
               (x.request_json->>'pre_runtime_absence_epoch')::integer
                 IS DISTINCT FROM x.pre_runtime_absence_epoch OR
               x.request_json->>'pre_runtime_absence_receipt_sha256'
                 IS DISTINCT FROM x.pre_runtime_absence_receipt_sha256 OR
               (x.request_json->>'requested_at')::timestamptz < p.prepared_at OR
               (x.request_json->>'requested_at')::timestamptz > x.issued_at OR
               (x.request_json->>'requested_monotonic_ns')::bigint <
                 p.prepared_monotonic_ns OR
               x.authorization_json->>'authorization_request_sha256' IS DISTINCT FROM
                 x.request_sha256 OR
               x.authorization_json->>'runtime_preparation_sha256' IS DISTINCT FROM
                 p.preparation_sha256 OR
               x.authorization_json->>'infrastructure_attempt_id' IS DISTINCT FROM
                 target_attempt OR
               x.authorization_json->>'execution_id' IS DISTINCT FROM a.execution_id OR
               x.authorization_json->>'intent_sha256' IS DISTINCT FROM a.intent_sha256 OR
               x.authorization_json->>'admission_sha256' IS DISTINCT FROM a.admission_sha256 OR
               x.authorization_json->>'qualification_grant_sha256' IS DISTINCT FROM
                 a.grant_sha256 OR
               x.authorization_json->>'node_manifest_sha256' IS DISTINCT FROM
                 p.node_manifest_sha256 OR
               x.authorization_json->>'node_id' IS DISTINCT FROM p.node_id OR
               x.authorization_json->>'boot_id' IS DISTINCT FROM p.boot_id OR
               x.authorization_json->>'launch_spec_sha256' IS DISTINCT FROM
                 p.payload_json->>'launch_spec_sha256' OR
               x.authorization_json->>'oci_config_sha256' IS DISTINCT FROM
                 p.payload_json->>'oci_config_sha256' OR
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
               x.authorization_json->>'qualification_only' IS DISTINCT FROM 'true' OR
               x.authorization_json->>'scientific_admission_allowed' IS DISTINCT FROM 'false'
             )
          ) THEN
            RAISE EXCEPTION 'runtime launch authorization lineage is rebound'
              USING ERRCODE = '23514';
          END IF;

          IF launch.launch_receipt_sha256 IS NOT NULL AND (
               launch.preparation_sha256 IS DISTINCT FROM p.preparation_sha256 OR
               launch.authorization_request_sha256 IS DISTINCT FROM
                 latest_auth.request_sha256 OR
               launch.authorization_sha256 IS DISTINCT FROM
                 latest_auth.authorization_sha256 OR
               launch.authorization_sha256 IS DISTINCT FROM
                 (launch.launch_receipt_json->'launch_evidence'
                   ->>'runtime_launch_authorization_sha256') OR
               launch.launch_payload_sha256 IS DISTINCT FROM launch.launch_receipt_sha256 OR
               launch.launch_receipt_json->>'node_manifest_sha256' IS DISTINCT FROM
                 p.node_manifest_sha256 OR
               launch.launch_receipt_json->'launch_evidence'->>'preparation_sha256'
                 IS DISTINCT FROM p.preparation_sha256 OR
               launch.launch_receipt_json->'launch_evidence'
                   ->>'runtime_identity_sha256' IS DISTINCT FROM launch.runtime_identity_sha256 OR
               launch.launch_receipt_json->'launch_evidence'->'runtime_identity'
                 IS DISTINCT FROM a.runtime_identity_json OR
               launch.launch_receipt_json->'launch_evidence'->'runtime_identity'
                   ->>'node_id' IS DISTINCT FROM p.node_id OR
               launch.launch_receipt_json->'launch_evidence'->'runtime_identity'
                   ->>'boot_id' IS DISTINCT FROM p.boot_id OR
               launch.launch_receipt_json->'launch_evidence'->'runtime_identity'
                   ->>'execution_id' IS DISTINCT FROM a.execution_id OR
               launch.launch_receipt_json->'launch_evidence'->'runtime_identity'
                   ->>'infrastructure_attempt_id' IS DISTINCT FROM target_attempt OR
               launch.launch_receipt_json->'launch_evidence'->'runtime_identity'
                   ->>'runtime_id' IS DISTINCT FROM p.payload_json->>'runtime_id' OR
               launch.launch_receipt_json->'launch_evidence'->'runtime_identity'
                   ->>'runtime_engine' IS DISTINCT FROM p.payload_json->>'runtime_engine' OR
               launch.launch_receipt_json->'launch_evidence'->'runtime_identity'
                   ->>'launch_spec_sha256' IS DISTINCT FROM
                     p.payload_json->>'launch_spec_sha256' OR
               (launch.launch_receipt_json->'launch_evidence'->'runtime_identity'
                   ->>'started_at')::timestamptz < p.prepared_at OR
               (launch.launch_receipt_json->'launch_evidence'->'runtime_identity'
                   ->>'started_monotonic_ns')::bigint < p.prepared_monotonic_ns OR
               launch.launch_receipt_json->'launch_evidence'->>'enforced_placement_sha256'
                 IS DISTINCT FROM p.payload_json->>'enforced_placement_sha256' OR
               launch.launch_receipt_json->'launch_evidence'->>
                   'input_materialization_receipt_sha256'
                 IS DISTINCT FROM p.payload_json->>
                   'input_materialization_receipt_sha256' OR
               (launch.launch_receipt_json->'launch_evidence'->>
                   'enforced_fencing_epoch')::bigint IS DISTINCT FROM p.fencing_epoch OR
               launch.launch_receipt_json->'launch_evidence'->>
                   'enforced_lease_token_sha256' IS DISTINCT FROM p.lease_token_sha256 OR
               (launch.launch_receipt_json->'launch_evidence'->>
                   'engine_start_monotonic_lower_bound_ns')::bigint
                 IS DISTINCT FROM
                   (launch.launch_receipt_json->'launch_evidence'->'runtime_identity'
                     ->>'started_monotonic_ns')::bigint OR
               (launch.launch_receipt_json->'launch_evidence'->'runtime_identity'
                   ->>'started_monotonic_ns')::bigint >=
                 (launch.launch_receipt_json->'launch_evidence'->>
                   'engine_start_monotonic_upper_bound_exclusive_ns')::bigint OR
               (launch.launch_receipt_json->'launch_evidence'->>
                   'observed_monotonic_ns')::bigint <
                 (launch.launch_receipt_json->'launch_evidence'->>
                   'engine_start_monotonic_upper_bound_exclusive_ns')::bigint OR
               (launch.launch_receipt_json->'launch_evidence'->>
                   'engine_start_monotonic_lower_bound_ns')::bigint <
                 (latest_auth.request_json->>'requested_monotonic_ns')::bigint OR
               (launch.launch_receipt_json->'launch_evidence'->>
                   'engine_start_monotonic_upper_bound_exclusive_ns')::bigint >
                 (latest_auth.request_json->>'requested_monotonic_ns')::bigint +
                 (latest_auth.authorization_json->>'max_launch_delay_ns')::bigint OR
               (launch.launch_receipt_json->'launch_evidence'->'runtime_identity'
                   ->>'started_at')::timestamptz < latest_auth.issued_at OR
               (launch.launch_receipt_json->'launch_evidence'->'runtime_identity'
                   ->>'started_at')::timestamptz >= latest_auth.expires_at OR
               (launch.launch_receipt_json->'launch_evidence'->>'observed_at')::timestamptz <
                 (launch.launch_receipt_json->'launch_evidence'->'runtime_identity'
                   ->>'started_at')::timestamptz OR
               (launch.launch_receipt_json->>'signed_at')::timestamptz
                 IS DISTINCT FROM launch.signed_at OR
               launch.signed_at <
                 (launch.launch_receipt_json->'launch_evidence'->>'observed_at')::timestamptz OR
               launch.signed_at -
                 (launch.launch_receipt_json->'launch_evidence'->>'observed_at')::timestamptz
                   > interval '60 seconds' OR
               launch.accepted_at < launch.signed_at OR
               launch.recovery_payload_sha256 IS DISTINCT FROM launch.recovery_grant_sha256 OR
               launch.recovery_grant_json->>'admission_sha256' IS DISTINCT FROM
                 a.admission_sha256 OR
               launch.recovery_grant_json->>'qualification_grant_sha256' IS DISTINCT FROM
                 a.grant_sha256 OR
               launch.recovery_grant_json->>'intent_sha256' IS DISTINCT FROM
                 a.intent_sha256 OR
               launch.recovery_grant_json->>'execution_id' IS DISTINCT FROM
                 a.execution_id OR
               launch.recovery_grant_json->>'runtime_preparation_sha256' IS DISTINCT FROM
                 p.preparation_sha256 OR
               launch.recovery_grant_json->>'node_runtime_launch_receipt_sha256'
                 IS DISTINCT FROM
                 launch.launch_receipt_sha256 OR
               launch.recovery_grant_json->>'infrastructure_attempt_id' IS DISTINCT FROM
                 target_attempt OR
               jsonb_typeof(launch.recovery_grant_json->
                 'accepted_runtime_termination_sha256') IS DISTINCT FROM 'null' OR
               (launch.recovery_grant_json->>'admitted_at')::timestamptz
                 IS DISTINCT FROM a.authorized_at OR
               (launch.recovery_grant_json->>'hard_deadline')::timestamptz
                 IS DISTINCT FROM a.hard_deadline OR
               (launch.recovery_grant_json->>'issued_at')::timestamptz
                 IS DISTINCT FROM launch.accepted_at OR
               launch.recovery_grant_json->>'launch_allowed' IS DISTINCT FROM 'false' OR
               launch.recovery_grant_json->>'recovery_only' IS DISTINCT FROM 'true' OR
               launch.runtime_control_pin_json->>'policy_sha256' IS DISTINCT FROM
                 launch.recovery_grant_json->>'runtime_control_policy_sha256' OR
               launch.runtime_control_pin_json->>'principal_id' IS DISTINCT FROM
                 launch.recovery_grant_json->>'authorized_by_principal_id' OR
               launch.runtime_control_pin_json->>'key_id' IS DISTINCT FROM
                 launch.recovery_grant_json->>'authorization_key_id' OR
               launch.runtime_control_pin_sha256 IS DISTINCT FROM
                 latest_auth.runtime_control_pin_sha256 OR
               launch.runtime_control_pin_json IS DISTINCT FROM
                 latest_auth.runtime_control_pin_json OR
               (launch.recovery_grant_json->>'recovery_expires_at')::timestamptz
                 IS DISTINCT FROM launch.recovery_expires_at OR
               (launch.recovery_grant_json->>'hard_deadline')::timestamptz >=
                 launch.recovery_expires_at OR
               (launch.recovery_grant_json->>'issued_at')::timestamptz >=
                 launch.recovery_expires_at OR
               (launch.recovery_grant_json->>'issued_at')::timestamptz <
                 (launch.runtime_control_pin_json->>'valid_from')::timestamptz OR
               (launch.recovery_grant_json->>'issued_at')::timestamptz >= LEAST(
                 (launch.runtime_control_pin_json->>'expires_at')::timestamptz,
                 COALESCE(
                   (launch.runtime_control_pin_json->>'revoked_at')::timestamptz,
                   (launch.runtime_control_pin_json->>'expires_at')::timestamptz)) OR
               launch.recovery_expires_at > LEAST(
                 (launch.runtime_control_pin_json->>'expires_at')::timestamptz,
                 COALESCE(
                   (launch.runtime_control_pin_json->>'revoked_at')::timestamptz,
                   (launch.runtime_control_pin_json->>'expires_at')::timestamptz))
             ) THEN
            RAISE EXCEPTION 'runtime launch/recovery authority is incomplete'
              USING ERRCODE = '23514';
          END IF;

          IF EXISTS (
            SELECT 1 FROM execution_pre_runtime_absence_decisions d
             WHERE d.attempt_id = target_attempt AND (
               d.preparation_sha256 IS DISTINCT FROM p.preparation_sha256 OR
               d.absence_payload_sha256 IS DISTINCT FROM d.absence_receipt_sha256 OR
               d.absence_receipt_json->>'node_manifest_sha256' IS DISTINCT FROM
                 p.node_manifest_sha256 OR
               d.absence_receipt_json->'preparation' IS DISTINCT FROM p.payload_json OR
               d.absence_receipt_json->>'preparation_sha256' IS DISTINCT FROM
                 p.preparation_sha256 OR
               d.absence_receipt_json->'absence_evidence'->>'preparation_sha256'
                 IS DISTINCT FROM p.preparation_sha256 OR
               (d.absence_receipt_json->'absence_evidence'->>
                   'prelaunch_absence_epoch')::integer
                 IS DISTINCT FROM d.absence_epoch OR
               d.absence_receipt_json->'absence_evidence'->>
                   'enforced_placement_sha256'
                 IS DISTINCT FROM p.payload_json->>'enforced_placement_sha256' OR
               d.absence_receipt_json->'absence_evidence'->>
                   'input_materialization_receipt_sha256'
                 IS DISTINCT FROM p.payload_json->>
                   'input_materialization_receipt_sha256' OR
               (d.absence_receipt_json->'absence_evidence'->>
                   'enforced_fencing_epoch')::bigint IS DISTINCT FROM p.fencing_epoch OR
               d.absence_receipt_json->'absence_evidence'->>
                   'enforced_lease_token_sha256' IS DISTINCT FROM p.lease_token_sha256 OR
               (d.absence_receipt_json->>'signed_at')::timestamptz <
                 (d.absence_receipt_json->'absence_evidence'->>
                   'inspected_at')::timestamptz OR
               (d.absence_receipt_json->>'signed_at')::timestamptz -
                 (d.absence_receipt_json->'absence_evidence'->>
                   'inspected_at')::timestamptz > interval '60 seconds' OR
               (d.absence_receipt_json->>'expires_at')::timestamptz <=
                 (d.absence_receipt_json->>'signed_at')::timestamptz OR
               d.decided_at <
                 (d.absence_receipt_json->>'signed_at')::timestamptz OR
               d.decided_at >=
                 (d.absence_receipt_json->>'expires_at')::timestamptz OR
               d.prior_authorization_request_sha256 IS DISTINCT FROM
                 d.absence_receipt_json->'absence_evidence'
                   ->>'prelaunch_authorization_request_sha256' OR
               d.prior_authorization_sha256 IS DISTINCT FROM
                 d.absence_receipt_json->'absence_evidence'
                   ->>'prelaunch_authorization_sha256' OR
               d.decision_json->>'attempt_id' IS DISTINCT FROM d.attempt_id OR
               (d.decision_json->>'absence_epoch')::integer IS DISTINCT FROM
                 d.absence_epoch OR
               d.decision_json->>'absence_receipt_sha256' IS DISTINCT FROM
                 d.absence_receipt_sha256 OR
               d.decision_json->>'prior_authorization_request_sha256' IS DISTINCT FROM
                 d.prior_authorization_request_sha256 OR
               d.decision_json->>'prior_authorization_sha256' IS DISTINCT FROM
                 d.prior_authorization_sha256 OR
               d.decision_json->>'runtime_control_pin_sha256' IS DISTINCT FROM
                 d.runtime_control_pin_sha256 OR
               d.decision_json->>'preparation_sha256' IS DISTINCT FROM
                 p.preparation_sha256 OR
               d.decision_json->>'disposition' IS DISTINCT FROM d.disposition OR
               d.decision_json->>'replacement_request_sha256' IS DISTINCT FROM
                 d.replacement_request_sha256 OR
               d.decision_json->>'replacement_authorization_sha256' IS DISTINCT FROM
                 d.replacement_authorization_sha256 OR
               (d.decision_json->>'decided_at')::timestamptz IS DISTINCT FROM
                 d.decided_at OR
               d.runtime_control_pin_json->>'policy_sha256' IS NULL OR
               d.decided_at <
                 (d.runtime_control_pin_json->>'valid_from')::timestamptz OR
               d.decided_at >= LEAST(
                 (d.runtime_control_pin_json->>'expires_at')::timestamptz,
                 COALESCE(
                   (d.runtime_control_pin_json->>'revoked_at')::timestamptz,
                   (d.runtime_control_pin_json->>'expires_at')::timestamptz)) OR
               ((d.prior_authorization_sha256 IS NULL) AND d.absence_epoch IS DISTINCT FROM 1) OR
               (d.prior_authorization_sha256 IS NOT NULL AND NOT EXISTS (
                 SELECT 1 FROM execution_runtime_launch_authorizations prior
                  WHERE prior.attempt_id = target_attempt
                    AND prior.request_sha256 = d.prior_authorization_request_sha256
                    AND prior.authorization_sha256 = d.prior_authorization_sha256
                    AND d.absence_epoch = prior.pre_runtime_absence_epoch + 1
               )) OR
               (d.disposition = 'reauthorized' AND NOT EXISTS (
                 SELECT 1 FROM execution_runtime_launch_authorizations x
                  WHERE x.attempt_id = target_attempt
                    AND x.request_sha256 = d.replacement_request_sha256
                    AND x.authorization_sha256 = d.replacement_authorization_sha256
                    AND x.pre_runtime_absence_receipt_sha256 = d.absence_receipt_sha256
                    AND x.pre_runtime_absence_epoch = d.absence_epoch
               ))
             )
          ) THEN
            RAISE EXCEPTION 'pre-runtime absence decision lacks exact proof/replacement'
              USING ERRCODE = '23514';
          END IF;

          IF EXISTS (
            SELECT 1 FROM execution_runtime_fence_rebinds r
            JOIN execution_attempt_adoptions d ON d.adoption_sha256 = r.adoption_sha256
             WHERE r.attempt_id = target_attempt AND (
               d.attempt_id IS DISTINCT FROM target_attempt OR
               d.sequence IS DISTINCT FROM r.sequence OR
               r.preparation_sha256 IS DISTINCT FROM p.preparation_sha256 OR
               r.runtime_identity_sha256 IS DISTINCT FROM d.runtime_identity_sha256 OR
               r.previous_fencing_epoch IS DISTINCT FROM d.previous_fencing_epoch OR
               r.new_fencing_epoch IS DISTINCT FROM d.new_fencing_epoch OR
               r.previous_lease_token_sha256 IS DISTINCT FROM
                 d.previous_lease_token_sha256 OR
               r.new_lease_token_sha256 IS DISTINCT FROM d.new_lease_token_sha256 OR
               r.request_payload_sha256 IS DISTINCT FROM r.request_sha256 OR
               r.receipt_payload_sha256 IS DISTINCT FROM r.rebind_receipt_sha256 OR
               r.request_json->>'preparation_sha256' IS DISTINCT FROM
                 r.preparation_sha256 OR
               r.request_json->>'runtime_identity_sha256' IS DISTINCT FROM
                 r.runtime_identity_sha256 OR
               (r.request_json->>'previous_fencing_epoch')::bigint
                 IS DISTINCT FROM r.previous_fencing_epoch OR
               r.request_json->>'previous_lease_token_sha256' IS DISTINCT FROM
                 r.previous_lease_token_sha256 OR
               (r.request_json->>'new_fencing_epoch')::bigint
                 IS DISTINCT FROM r.new_fencing_epoch OR
               r.request_json->>'new_lease_token_sha256' IS DISTINCT FROM
                 r.new_lease_token_sha256 OR
               (r.request_json->>'rebind_sequence')::integer IS DISTINCT FROM
                 r.sequence OR
               r.request_json->>'qualification_only' IS DISTINCT FROM 'true' OR
               r.request_json->>'scientific_admission_allowed' IS DISTINCT FROM
                 'false' OR
               r.receipt_json->>'node_manifest_sha256' IS DISTINCT FROM
                 p.node_manifest_sha256 OR
               r.receipt_json->>'evidence_sha256' IS DISTINCT FROM
                 r.evidence_sha256 OR
               r.receipt_json->'evidence'->>'request_sha256' IS DISTINCT FROM
                 r.request_sha256 OR
               r.receipt_json->'evidence'->>'preparation_sha256' IS DISTINCT FROM
                 r.preparation_sha256 OR
               r.receipt_json->'evidence'->>'runtime_identity_sha256'
                 IS DISTINCT FROM r.runtime_identity_sha256 OR
               (r.receipt_json->'evidence'->>'previous_fencing_epoch')::bigint
                 IS DISTINCT FROM r.previous_fencing_epoch OR
               r.receipt_json->'evidence'->>'previous_lease_token_sha256'
                 IS DISTINCT FROM r.previous_lease_token_sha256 OR
               (r.receipt_json->'evidence'->>'new_fencing_epoch')::bigint
                 IS DISTINCT FROM r.new_fencing_epoch OR
               r.receipt_json->'evidence'->>'new_lease_token_sha256'
                 IS DISTINCT FROM r.new_lease_token_sha256 OR
               (r.receipt_json->'evidence'->>'rebind_sequence')::integer
                 IS DISTINCT FROM r.sequence OR
               r.receipt_json->'evidence'->>'previous_runtime_control_journal_sha256'
                 IS DISTINCT FROM
                   r.request_json->>'expected_runtime_control_journal_sha256' OR
               r.receipt_json->'evidence'->>'new_runtime_control_journal_sha256'
                 IS NOT DISTINCT FROM
                   r.receipt_json->'evidence'->>
                     'previous_runtime_control_journal_sha256' OR
               r.receipt_json->'evidence'->>'rebind_evidence_sha256'
                 IS NULL OR
               (r.receipt_json->'evidence'->>'rebound_at')::timestamptz
                 IS DISTINCT FROM r.rebound_at OR
               (r.receipt_json->'evidence'->>'rebound_at')::timestamptz <
                 (r.request_json->>'requested_at')::timestamptz OR
               (r.receipt_json->'evidence'->>'rebound_monotonic_ns')::bigint <
                 (r.request_json->>'requested_monotonic_ns')::bigint OR
               (r.receipt_json->>'signed_at')::timestamptz < r.rebound_at OR
               (r.receipt_json->>'signed_at')::timestamptz - r.rebound_at >
                 interval '60 seconds' OR
               r.accepted_at < (r.receipt_json->>'signed_at')::timestamptz OR
               r.receipt_json->'evidence'->>'qualification_only'
                 IS DISTINCT FROM 'true' OR
               r.receipt_json->'evidence'->>'scientific_admission_allowed'
                 IS DISTINCT FROM 'false' OR
               r.receipt_json->>'qualification_only' IS DISTINCT FROM 'true' OR
               r.receipt_json->>'scientific_admission_allowed' IS DISTINCT FROM
                 'false'
             )
          ) OR (
            p.preparation_sha256 IS NOT NULL AND a.adoption_count > 0 AND
            (SELECT count(*) FROM execution_runtime_fence_rebinds r
              WHERE r.attempt_id = target_attempt) IS DISTINCT FROM a.adoption_count
          ) THEN
            RAISE EXCEPTION 'runtime fence rebind and adoption are not one-to-one'
              USING ERRCODE = '23514';
          END IF;

          IF challenge.challenge_sha256 IS NOT NULL AND (
               challenge.preparation_sha256 IS DISTINCT FROM p.preparation_sha256 OR
               challenge.launch_receipt_sha256 IS DISTINCT FROM launch.launch_receipt_sha256 OR
               challenge.runtime_identity_sha256 IS DISTINCT FROM launch.runtime_identity_sha256 OR
               challenge.inspection_evidence_json->>'preparation_sha256' IS DISTINCT FROM
                 p.preparation_sha256 OR
               challenge.inspection_evidence_json->>'runtime_identity_sha256' IS DISTINCT FROM
                 launch.runtime_identity_sha256 OR
               challenge.inspection_evidence_json->'runtime_identity'
                 IS DISTINCT FROM a.runtime_identity_json OR
               challenge.inspection_evidence_json->>'state' IS DISTINCT FROM 'terminated' OR
               challenge.inspection_evidence_json->>'enforced_placement_sha256'
                 IS DISTINCT FROM p.payload_json->>'enforced_placement_sha256' OR
               challenge.inspection_evidence_json->>
                   'input_materialization_receipt_sha256'
                 IS DISTINCT FROM p.payload_json->>
                   'input_materialization_receipt_sha256' OR
               challenge.inspection_evidence_json->>'inspection_evidence_sha256'
                 IS NULL OR
               challenge.inspection_evidence_json->>'engine_terminal_journal_sha256'
                 IS NULL OR
               (challenge.inspection_evidence_json->>'enforced_fencing_epoch')::bigint
                 IS DISTINCT FROM a.fencing_epoch OR
               challenge.inspection_evidence_json->>'enforced_lease_token_sha256'
                 IS DISTINCT FROM a.lease_token_sha256 OR
               challenge.challenge_payload_sha256 IS DISTINCT FROM challenge.challenge_sha256 OR
               challenge.challenge_json->>'challenge_id' IS DISTINCT FROM
                 challenge.challenge_id OR
               challenge.challenge_json->>'attempt_id' IS DISTINCT FROM target_attempt OR
               challenge.challenge_json->>'execution_id' IS DISTINCT FROM a.execution_id OR
               challenge.challenge_json->>'intent_sha256' IS DISTINCT FROM a.intent_sha256 OR
               challenge.challenge_json->>'node_manifest_sha256' IS DISTINCT FROM
                 p.node_manifest_sha256 OR
               challenge.challenge_json->>'runtime_preparation_sha256' IS DISTINCT FROM
                 p.preparation_sha256 OR
               challenge.challenge_json->>'node_runtime_launch_receipt_sha256'
                 IS DISTINCT FROM
                 launch.launch_receipt_sha256 OR
               challenge.challenge_json->>'runtime_identity_sha256'
                 IS DISTINCT FROM challenge.runtime_identity_sha256 OR
               challenge.challenge_json->>'runtime_inspection_evidence_sha256'
                 IS DISTINCT FROM challenge.inspection_evidence_sha256 OR
               (challenge.challenge_json->>'inspection_sequence')::bigint IS DISTINCT FROM
                 challenge.inspection_sequence OR
               challenge.challenge_json->>'node_inventory_sha256' IS DISTINCT FROM
                 a.node_inventory_sha256 OR
               challenge.challenge_json->>'resource_lease_sha256' IS DISTINCT FROM
                 resource_lease_sha OR
               (challenge.challenge_json->>'fencing_epoch')::bigint IS DISTINCT FROM
                 a.fencing_epoch OR
               challenge.challenge_json->>'lease_token_sha256' IS DISTINCT FROM
                 a.lease_token_sha256 OR
               (challenge.challenge_json->>'hard_deadline')::timestamptz
                 IS DISTINCT FROM a.hard_deadline OR
               (challenge.challenge_json->>'artifact_submission_deadline')::timestamptz <=
                 a.hard_deadline OR
               (challenge.challenge_json->>'challenged_at')::timestamptz IS DISTINCT FROM
                 challenge.challenged_at OR
               (challenge.challenge_json->>'expires_at')::timestamptz IS DISTINCT FROM
                 challenge.expires_at OR
               challenge.challenged_at <
                 (challenge.inspection_evidence_json->>'inspected_at')::timestamptz OR
               challenge.challenged_at <
                 (challenge.runtime_control_pin_json->>'valid_from')::timestamptz OR
               challenge.challenged_at >= LEAST(
                 (challenge.runtime_control_pin_json->>'expires_at')::timestamptz,
                 COALESCE(
                   (challenge.runtime_control_pin_json->>'revoked_at')::timestamptz,
                   (challenge.runtime_control_pin_json->>'expires_at')::timestamptz)) OR
               challenge.expires_at > LEAST(
                 (challenge.runtime_control_pin_json->>'expires_at')::timestamptz,
                 COALESCE(
                   (challenge.runtime_control_pin_json->>'revoked_at')::timestamptz,
                   (challenge.runtime_control_pin_json->>'expires_at')::timestamptz)) OR
               (challenge.challenge_json->>'artifact_submission_deadline')::timestamptz >
                 LEAST(
                   (challenge.runtime_control_pin_json->>'expires_at')::timestamptz,
                   COALESCE(
                     (challenge.runtime_control_pin_json->>'revoked_at')::timestamptz,
                     (challenge.runtime_control_pin_json->>'expires_at')::timestamptz)) OR
               challenge.runtime_control_pin_json->>'policy_sha256' IS DISTINCT FROM
                 challenge.challenge_json->>'runtime_control_policy_sha256' OR
               challenge.runtime_control_pin_json->>'principal_id' IS DISTINCT FROM
                 challenge.challenge_json->>'challenged_by_principal_id' OR
               challenge.runtime_control_pin_json->>'key_id' IS DISTINCT FROM
                 challenge.challenge_json->>'challenge_key_id' OR
               challenge.runtime_control_pin_sha256 IS DISTINCT FROM
                 latest_auth.runtime_control_pin_sha256 OR
               challenge.runtime_control_pin_json IS DISTINCT FROM
                 latest_auth.runtime_control_pin_json
             ) THEN
            RAISE EXCEPTION 'runtime termination challenge differs from launch lineage'
              USING ERRCODE = '23514';
          END IF;

          IF EXISTS (
            SELECT 1
              FROM (
                SELECT expires_at,
                       lead(challenged_at) OVER (ORDER BY inspection_sequence)
                         AS next_challenged_at
                  FROM execution_runtime_termination_challenges
                 WHERE attempt_id = target_attempt
              ) generations
             WHERE next_challenged_at IS NOT NULL
               AND expires_at > next_challenged_at
          ) THEN
            RAISE EXCEPTION 'runtime termination challenge generation overlaps its successor'
              USING ERRCODE = '23514';
          END IF;

          IF EXISTS (
            SELECT 1
              FROM execution_runtime_termination_challenges current_generation
              JOIN execution_runtime_termination_challenges previous_generation
                ON previous_generation.attempt_id = current_generation.attempt_id
               AND previous_generation.inspection_sequence + 1 =
                     current_generation.inspection_sequence
             WHERE current_generation.attempt_id = target_attempt AND (
               current_generation.inspection_evidence_json
                   - 'inspection_evidence_sha256' - 'inspected_at'
                   - 'inspected_monotonic_ns'
                 IS DISTINCT FROM
               previous_generation.inspection_evidence_json
                   - 'inspection_evidence_sha256' - 'inspected_at'
                   - 'inspected_monotonic_ns' OR
               current_generation.inspection_evidence_json->>'inspection_evidence_sha256'
                 IS NOT DISTINCT FROM
                   previous_generation.inspection_evidence_json->>
                     'inspection_evidence_sha256' OR
               (current_generation.inspection_evidence_json->>'inspected_at')::timestamptz
                 <= (previous_generation.inspection_evidence_json->>
                       'inspected_at')::timestamptz OR
               (current_generation.inspection_evidence_json->>
                   'inspected_monotonic_ns')::bigint
                 <= (previous_generation.inspection_evidence_json->>
                       'inspected_monotonic_ns')::bigint
             )
          ) THEN
            RAISE EXCEPTION 'runtime termination evidence refresh changed immutable facts'
              USING ERRCODE = '23514';
          END IF;

          IF termination.accepted_termination_sha256 IS NOT NULL AND (
               termination.challenge_sha256 IS DISTINCT FROM challenge.challenge_sha256 OR
               termination.preparation_sha256 IS DISTINCT FROM p.preparation_sha256 OR
               termination.launch_receipt_sha256 IS DISTINCT FROM launch.launch_receipt_sha256 OR
               termination.runtime_identity_sha256 IS DISTINCT FROM launch.runtime_identity_sha256 OR
               termination.node_receipt_payload_sha256 IS DISTINCT FROM
                 termination.node_termination_receipt_sha256 OR
               termination.node_termination_receipt_json->>'node_manifest_sha256'
                 IS DISTINCT FROM p.node_manifest_sha256 OR
               termination.node_termination_receipt_json->>'challenge_sha256'
                 IS DISTINCT FROM challenge.challenge_sha256 OR
               termination.node_termination_receipt_json->>'runtime_preparation_sha256'
                 IS DISTINCT FROM p.preparation_sha256 OR
               termination.node_termination_receipt_json->>
                 'node_runtime_launch_receipt_sha256' IS DISTINCT FROM
                   launch.launch_receipt_sha256 OR
               termination.node_termination_receipt_json->>
                   'runtime_launch_authorization_request_sha256'
                 IS DISTINCT FROM latest_auth.request_sha256 OR
               termination.node_termination_receipt_json->>
                   'runtime_launch_authorization_sha256'
                 IS DISTINCT FROM latest_auth.authorization_sha256 OR
               (termination.node_termination_receipt_json->>'inspection_sequence')::bigint
                 IS DISTINCT FROM termination.inspection_sequence OR
               termination.node_termination_receipt_json->>'termination_evidence_sha256'
                 IS DISTINCT FROM termination.termination_evidence_sha256 OR
               termination.node_termination_receipt_json->'termination_evidence'
                 IS DISTINCT FROM challenge.inspection_evidence_json OR
               (termination.node_termination_receipt_json->>'signed_at')::timestamptz <
                 (termination.node_termination_receipt_json->'termination_evidence'->>
                   'inspected_at')::timestamptz OR
               (termination.node_termination_receipt_json->>'signed_at')::timestamptz -
                 (termination.node_termination_receipt_json->'termination_evidence'->>
                   'inspected_at')::timestamptz > interval '60 seconds' OR
               (termination.node_termination_receipt_json->>'signed_at')::timestamptz >=
                 (termination.node_termination_receipt_json->>'expires_at')::timestamptz OR
               (termination.node_termination_receipt_json->>'expires_at')::timestamptz >
                 challenge.expires_at OR
               (termination.node_termination_receipt_json->>'signed_at')::timestamptz >
                 termination.accepted_at OR
               termination.acceptance_payload_sha256 IS DISTINCT FROM
                 termination.accepted_termination_sha256 OR
               termination.accepted_termination_json->>'challenge_sha256' IS DISTINCT FROM
                 challenge.challenge_sha256 OR
               termination.accepted_termination_json->>'attempt_id' IS DISTINCT FROM
                 target_attempt OR
               termination.accepted_termination_json->>'runtime_preparation_sha256'
                 IS DISTINCT FROM p.preparation_sha256 OR
               termination.accepted_termination_json->>
                   'node_runtime_launch_receipt_sha256'
                 IS DISTINCT FROM launch.launch_receipt_sha256 OR
               termination.accepted_termination_json->>
                   'runtime_launch_authorization_request_sha256'
                 IS DISTINCT FROM latest_auth.request_sha256 OR
               termination.accepted_termination_json->>
                   'runtime_launch_authorization_sha256'
                 IS DISTINCT FROM latest_auth.authorization_sha256 OR
               termination.accepted_termination_json->>
                   'node_runtime_termination_receipt_sha256' IS DISTINCT FROM
                 termination.node_termination_receipt_sha256 OR
               (termination.accepted_termination_json->>'inspection_sequence')::bigint
                 IS DISTINCT FROM termination.inspection_sequence OR
               termination.accepted_termination_json->>'runtime_identity_sha256'
                 IS DISTINCT FROM termination.runtime_identity_sha256 OR
               termination.accepted_termination_json->>
                 'runtime_inspection_evidence_sha256' IS DISTINCT FROM
                   termination.termination_evidence_sha256 OR
               termination.accepted_termination_json->>'engine_terminal_journal_sha256'
                 IS DISTINCT FROM challenge.inspection_evidence_json->>
                   'engine_terminal_journal_sha256' OR
               (termination.accepted_termination_json->>'fencing_epoch')::bigint
                 IS DISTINCT FROM a.fencing_epoch OR
               termination.accepted_termination_json->>'lease_token_sha256'
                 IS DISTINCT FROM a.lease_token_sha256 OR
               (termination.accepted_termination_json->>'runtime_ended_at')::timestamptz
                 IS DISTINCT FROM termination.runtime_ended_at OR
               (termination.accepted_termination_json->>'exit_code')::integer
                 IS DISTINCT FROM
                   (challenge.inspection_evidence_json->>'exit_code')::integer OR
               (termination.accepted_termination_json->>'accepted_at')::timestamptz
                 IS DISTINCT FROM termination.accepted_at OR
               (termination.accepted_termination_json->>'proof_signed_at')::timestamptz
                 IS DISTINCT FROM
                   (termination.node_termination_receipt_json->>'signed_at')::timestamptz OR
               (termination.accepted_termination_json->>'proof_expires_at')::timestamptz
                 IS DISTINCT FROM
                   (termination.node_termination_receipt_json->>'expires_at')::timestamptz OR
               (termination.accepted_termination_json->>'hard_deadline')::timestamptz
                 IS DISTINCT FROM a.hard_deadline OR
               (termination.accepted_termination_json->>
                   'artifact_submission_deadline')::timestamptz
                 IS DISTINCT FROM
                   (challenge.challenge_json->>'artifact_submission_deadline')::timestamptz OR
               (termination.accepted_termination_json->>'runtime_ended_at')::timestamptz >
                 (termination.accepted_termination_json->>'proof_signed_at')::timestamptz OR
               (termination.accepted_termination_json->>'proof_signed_at')::timestamptz >
                 termination.accepted_at OR
               termination.accepted_at >=
                 (termination.accepted_termination_json->>'proof_expires_at')::timestamptz OR
               (termination.accepted_termination_json->>'runtime_ended_at')::timestamptz >
                 (termination.accepted_termination_json->>'billable_ended_at')::timestamptz OR
               (termination.accepted_termination_json->>'billable_ended_at')::timestamptz >
                 termination.accepted_at OR
               termination.accepted_at >=
                 (termination.accepted_termination_json->>
                   'artifact_submission_deadline')::timestamptz OR
               termination.accepted_termination_json->>'proof_was_fresh'
                 IS DISTINCT FROM 'true' OR
               termination.accepted_termination_json->>'compute_release_allowed'
                 IS DISTINCT FROM 'true' OR
               termination.accepted_termination_json->>'qualification_only'
                 IS DISTINCT FROM 'true' OR
               termination.accepted_termination_json->>'scientific_admission_allowed'
                 IS DISTINCT FROM 'false' OR
               termination.runtime_control_pin_json->>'policy_sha256' IS DISTINCT FROM
                 termination.accepted_termination_json->>'runtime_control_policy_sha256' OR
               termination.runtime_control_pin_json->>'principal_id' IS DISTINCT FROM
                 termination.accepted_termination_json->>'accepted_by_principal_id' OR
               termination.runtime_control_pin_json->>'key_id' IS DISTINCT FROM
                 termination.accepted_termination_json->>'acceptance_key_id' OR
               termination.runtime_control_pin_sha256 IS DISTINCT FROM
                 challenge.runtime_control_pin_sha256 OR
               termination.runtime_control_pin_json IS DISTINCT FROM
                 challenge.runtime_control_pin_json OR
               termination.recovery_payload_sha256 IS DISTINCT FROM
                 termination.recovery_grant_sha256 OR
               termination.recovery_grant_json->>'admission_sha256' IS DISTINCT FROM
                 a.admission_sha256 OR
               termination.recovery_grant_json->>'qualification_grant_sha256'
                 IS DISTINCT FROM a.grant_sha256 OR
               termination.recovery_grant_json->>'intent_sha256' IS DISTINCT FROM
                 a.intent_sha256 OR
               termination.recovery_grant_json->>'execution_id' IS DISTINCT FROM
                 a.execution_id OR
               termination.recovery_grant_json->>'accepted_runtime_termination_sha256'
                 IS DISTINCT FROM termination.accepted_termination_sha256 OR
               termination.recovery_grant_json->>'infrastructure_attempt_id'
                 IS DISTINCT FROM target_attempt OR
               termination.recovery_grant_json->>'runtime_preparation_sha256'
                 IS DISTINCT FROM p.preparation_sha256 OR
               termination.recovery_grant_json->>'node_runtime_launch_receipt_sha256'
                 IS DISTINCT FROM launch.launch_receipt_sha256 OR
               (termination.recovery_grant_json->>'admitted_at')::timestamptz
                 IS DISTINCT FROM a.authorized_at OR
               (termination.recovery_grant_json->>'hard_deadline')::timestamptz
                 IS DISTINCT FROM a.hard_deadline OR
               (termination.recovery_grant_json->>'issued_at')::timestamptz
                 IS DISTINCT FROM termination.accepted_at OR
               termination.recovery_grant_json->>'recovery_only'
                 IS DISTINCT FROM 'true' OR
               termination.recovery_grant_json->>'launch_allowed'
                 IS DISTINCT FROM 'false' OR
               termination.recovery_grant_json->>'qualification_only'
                 IS DISTINCT FROM 'true' OR
               termination.recovery_grant_json->>'scientific_admission_allowed'
                 IS DISTINCT FROM 'false' OR
               termination.runtime_control_pin_json->>'policy_sha256' IS DISTINCT FROM
                 termination.recovery_grant_json->>'runtime_control_policy_sha256' OR
               termination.runtime_control_pin_json->>'principal_id' IS DISTINCT FROM
                 termination.recovery_grant_json->>'authorized_by_principal_id' OR
               termination.runtime_control_pin_json->>'key_id' IS DISTINCT FROM
                 termination.recovery_grant_json->>'authorization_key_id' OR
               (termination.recovery_grant_json->>'recovery_expires_at')::timestamptz
                 IS DISTINCT FROM termination.recovery_expires_at OR
               a.hard_deadline >= termination.recovery_expires_at OR
               termination.accepted_at >= termination.recovery_expires_at OR
               termination.accepted_at <
                 (termination.runtime_control_pin_json->>'valid_from')::timestamptz OR
               termination.accepted_at >= LEAST(
                 (termination.runtime_control_pin_json->>'expires_at')::timestamptz,
                 COALESCE(
                   (termination.runtime_control_pin_json->>'revoked_at')::timestamptz,
                   (termination.runtime_control_pin_json->>'expires_at')::timestamptz)) OR
               termination.recovery_expires_at > LEAST(
                 (termination.runtime_control_pin_json->>'expires_at')::timestamptz,
                 COALESCE(
                   (termination.runtime_control_pin_json->>'revoked_at')::timestamptz,
                   (termination.runtime_control_pin_json->>'expires_at')::timestamptz)) OR
               (termination.accepted_termination_json->>
                   'artifact_submission_deadline')::timestamptz > LEAST(
                 (termination.runtime_control_pin_json->>'expires_at')::timestamptz,
                 COALESCE(
                   (termination.runtime_control_pin_json->>'revoked_at')::timestamptz,
                   (termination.runtime_control_pin_json->>'expires_at')::timestamptz)) OR
               termination.conditional_terminal_expiration_payload_sha256
                 IS DISTINCT FROM
                   termination.conditional_terminal_expiration_sha256 OR
               termination.conditional_terminal_expiration_json->>'attempt_id'
                 IS DISTINCT FROM target_attempt OR
               termination.conditional_terminal_expiration_json->>'execution_id'
                 IS DISTINCT FROM a.execution_id OR
               termination.conditional_terminal_expiration_json->>'intent_sha256'
                 IS DISTINCT FROM a.intent_sha256 OR
               termination.conditional_terminal_expiration_json->>'node_id'
                 IS DISTINCT FROM a.node_id OR
               termination.conditional_terminal_expiration_json->>'node_manifest_sha256'
                 IS DISTINCT FROM p.node_manifest_sha256 OR
               termination.conditional_terminal_expiration_json->>'node_inventory_sha256'
                 IS DISTINCT FROM a.node_inventory_sha256 OR
               termination.conditional_terminal_expiration_json->>'resource_lease_sha256'
                 IS DISTINCT FROM resource_lease_sha OR
               termination.conditional_terminal_expiration_json->>'runtime_preparation_sha256'
                 IS DISTINCT FROM p.preparation_sha256 OR
               termination.conditional_terminal_expiration_json->>
                   'runtime_launch_authorization_request_sha256'
                 IS DISTINCT FROM latest_auth.request_sha256 OR
               termination.conditional_terminal_expiration_json->>
                   'runtime_launch_authorization_sha256'
                 IS DISTINCT FROM latest_auth.authorization_sha256 OR
               termination.conditional_terminal_expiration_json->>
                   'node_runtime_launch_receipt_sha256'
                 IS DISTINCT FROM launch.launch_receipt_sha256 OR
               termination.conditional_terminal_expiration_json->>
                   'runtime_termination_challenge_sha256'
                 IS DISTINCT FROM challenge.challenge_sha256 OR
               termination.conditional_terminal_expiration_json->>
                   'node_runtime_termination_receipt_sha256'
                 IS DISTINCT FROM termination.node_termination_receipt_sha256 OR
               termination.conditional_terminal_expiration_json->>
                   'accepted_runtime_termination_sha256'
                 IS DISTINCT FROM termination.accepted_termination_sha256 OR
               termination.conditional_terminal_expiration_json->>'runtime_identity_sha256'
                 IS DISTINCT FROM termination.runtime_identity_sha256 OR
               termination.conditional_terminal_expiration_json->>
                   'runtime_inspection_evidence_sha256'
                 IS DISTINCT FROM termination.termination_evidence_sha256 OR
               termination.conditional_terminal_expiration_json->>
                   'engine_terminal_journal_sha256'
                 IS DISTINCT FROM challenge.inspection_evidence_json->>
                   'engine_terminal_journal_sha256' OR
               (termination.conditional_terminal_expiration_json->>'inspection_sequence')::bigint
                 IS DISTINCT FROM termination.inspection_sequence OR
               (termination.conditional_terminal_expiration_json->>'fencing_epoch')::bigint
                 IS DISTINCT FROM a.fencing_epoch OR
               termination.conditional_terminal_expiration_json->>'lease_token_sha256'
                 IS DISTINCT FROM a.lease_token_sha256 OR
               (termination.conditional_terminal_expiration_json->>'runtime_ended_at')::timestamptz
                 IS DISTINCT FROM termination.runtime_ended_at OR
               (termination.conditional_terminal_expiration_json->>'exit_code')::integer
                 IS DISTINCT FROM
                   (challenge.inspection_evidence_json->>'exit_code')::integer OR
               (termination.conditional_terminal_expiration_json->>'hard_deadline')::timestamptz
                 IS DISTINCT FROM a.hard_deadline OR
               (termination.conditional_terminal_expiration_json->>
                   'artifact_submission_deadline')::timestamptz
                 IS DISTINCT FROM
                   (termination.accepted_termination_json->>
                     'artifact_submission_deadline')::timestamptz OR
               (termination.conditional_terminal_expiration_json->>
                   'accepted_runtime_termination_at')::timestamptz
                 IS DISTINCT FROM termination.accepted_at OR
               (termination.conditional_terminal_expiration_json->>'authorized_at')::timestamptz
                 IS DISTINCT FROM termination.conditional_terminal_expiration_authorized_at OR
               termination.conditional_terminal_expiration_authorized_at
                 IS DISTINCT FROM termination.accepted_at OR
               (termination.conditional_terminal_expiration_json->>'expired_at')::timestamptz
                 IS DISTINCT FROM termination.conditional_terminal_expiration_expires_at OR
               termination.conditional_terminal_expiration_expires_at
                 IS DISTINCT FROM
                   (termination.accepted_termination_json->>
                     'artifact_submission_deadline')::timestamptz OR
               termination.conditional_terminal_expiration_json->>'reason'
                 IS DISTINCT FROM 'artifact_submission_deadline_expired' OR
               termination.conditional_terminal_expiration_json->>'disposition'
                 IS DISTINCT FROM 'invalid_output' OR
               termination.conditional_terminal_expiration_json->>'retryable'
                 IS DISTINCT FROM 'false' OR
               termination.conditional_terminal_expiration_json->>
                   'conditional_on_terminal_submission_absence'
                 IS DISTINCT FROM 'true' OR
               termination.conditional_terminal_expiration_json->>
                   'database_time_activation_required'
                 IS DISTINCT FROM 'true' OR
               termination.conditional_terminal_expiration_json->>'qualification_only'
                 IS DISTINCT FROM 'true' OR
               termination.conditional_terminal_expiration_json->>
                   'scientific_admission_allowed'
                 IS DISTINCT FROM 'false' OR
               termination.runtime_control_pin_json->>'policy_sha256' IS DISTINCT FROM
                 termination.conditional_terminal_expiration_json->>
                   'runtime_control_policy_sha256' OR
               termination.runtime_control_pin_json->>'principal_id' IS DISTINCT FROM
                 termination.conditional_terminal_expiration_json->>
                   'adjudicated_by_principal_id' OR
               termination.runtime_control_pin_json->>'key_id' IS DISTINCT FROM
                 termination.conditional_terminal_expiration_json->>'adjudication_key_id'
             ) THEN
            RAISE EXCEPTION 'accepted runtime termination differs from full proof'
              USING ERRCODE = '23514';
          END IF;

          IF expiration.terminal_deadline_expiration_sha256 IS NOT NULL AND (
               terminal.accepted_terminal_submission_sha256 IS NOT NULL OR
               expiration.attempt_id IS DISTINCT FROM target_attempt OR
               expiration.accepted_runtime_termination_sha256 IS DISTINCT FROM
                 termination.accepted_termination_sha256 OR
               expiration.terminal_deadline_expiration_sha256 IS DISTINCT FROM
                 termination.conditional_terminal_expiration_sha256 OR
               expiration.payload_sha256 IS DISTINCT FROM
                 expiration.terminal_deadline_expiration_sha256 OR
               expiration.payload_json IS DISTINCT FROM
                 termination.conditional_terminal_expiration_json OR
               expiration.runtime_control_pin_sha256 IS DISTINCT FROM
                 termination.runtime_control_pin_sha256 OR
               expiration.runtime_control_pin_json IS DISTINCT FROM
                 termination.runtime_control_pin_json OR
               expiration.authorized_at IS DISTINCT FROM
                 termination.conditional_terminal_expiration_authorized_at OR
               expiration.expired_at IS DISTINCT FROM
                 termination.conditional_terminal_expiration_expires_at OR
               expiration.activated_at < expiration.expired_at OR
               expiration.activated_at IS DISTINCT FROM a.updated_at
             ) THEN
            RAISE EXCEPTION 'terminal deadline activation differs from conditional authority'
              USING ERRCODE = '23514';
          END IF;

          IF terminal.accepted_terminal_submission_sha256 IS NOT NULL AND (
               expiration.terminal_deadline_expiration_sha256 IS NOT NULL OR
               terminal.accepted_runtime_termination_sha256 IS DISTINCT FROM
                 termination.accepted_termination_sha256 OR
               terminal.submission_payload_sha256 IS DISTINCT FROM
                 terminal.terminal_submission_sha256 OR
               terminal.manifest_payload_sha256 IS DISTINCT FROM
                 terminal.artifact_manifest_sha256 OR
               terminal.acceptance_payload_sha256 IS DISTINCT FROM
                 terminal.accepted_terminal_submission_sha256 OR
               terminal.terminal_submission_json->>'node_manifest_sha256'
                 IS DISTINCT FROM p.node_manifest_sha256 OR
               terminal.terminal_submission_json->>'intent_sha256'
                 IS DISTINCT FROM a.intent_sha256 OR
               terminal.terminal_submission_json->>'execution_id'
                 IS DISTINCT FROM a.execution_id OR
               terminal.terminal_submission_json->>'attempt_id'
                 IS DISTINCT FROM target_attempt OR
               terminal.terminal_submission_json->>'node_inventory_sha256'
                 IS DISTINCT FROM a.node_inventory_sha256 OR
               terminal.terminal_submission_json->>'resource_lease_sha256'
                 IS DISTINCT FROM resource_lease_sha OR
               (terminal.terminal_submission_json->>'fencing_epoch')::bigint
                 IS DISTINCT FROM a.fencing_epoch OR
               terminal.terminal_submission_json->>'lease_token_sha256'
                 IS DISTINCT FROM a.lease_token_sha256 OR
               terminal.terminal_submission_json->>'accepted_runtime_termination_sha256'
                 IS DISTINCT FROM
                 termination.accepted_termination_sha256 OR
               terminal.accepted_terminal_submission_json->>'terminal_submission_sha256'
                 IS DISTINCT FROM
                 terminal.terminal_submission_sha256 OR
               terminal.accepted_terminal_submission_json->>'attempt_id'
                 IS DISTINCT FROM target_attempt OR
               terminal.accepted_terminal_submission_json->>'node_manifest_sha256'
                 IS DISTINCT FROM p.node_manifest_sha256 OR
               terminal.accepted_terminal_submission_json->>
                   'accepted_runtime_termination_sha256'
                 IS DISTINCT FROM termination.accepted_termination_sha256 OR
               terminal.accepted_terminal_submission_json->>'artifact_manifest_sha256'
                 IS DISTINCT FROM
                 terminal.artifact_manifest_sha256 OR
               terminal.output_tree_sha256 IS DISTINCT FROM
                 terminal.terminal_submission_json->>'output_tree_sha256' OR
               terminal.output_tree_sha256 IS DISTINCT FROM
                 terminal.accepted_terminal_submission_json->>'output_tree_sha256' OR
               terminal.disposition IS DISTINCT FROM
                 terminal.terminal_submission_json->>'disposition' OR
               terminal.disposition IS DISTINCT FROM
                 terminal.accepted_terminal_submission_json->>'disposition' OR
               terminal.artifact_verified_receipt_sha256s_json IS DISTINCT FROM
                 terminal.terminal_submission_json->'artifact_verified_receipt_sha256s' OR
               terminal.artifact_verified_receipt_sha256s_json IS DISTINCT FROM
                 terminal.accepted_terminal_submission_json->
                   'artifact_verified_receipt_sha256s' OR
               jsonb_array_length(terminal.artifact_verified_receipts_json)
                 IS DISTINCT FROM
                   jsonb_array_length(
                     terminal.artifact_verified_receipt_sha256s_json) OR
               (terminal.accepted_terminal_submission_json->>'accepted_at')::timestamptz
                 IS DISTINCT FROM terminal.accepted_at OR
               terminal.accepted_terminal_submission_json->>'node_submitted_at'
                 IS DISTINCT FROM terminal.terminal_submission_json->>'submitted_at' OR
               terminal.accepted_at <
                 (terminal.accepted_terminal_submission_json->>
                   'node_submitted_at')::timestamptz OR
               terminal.accepted_at >=
                 (terminal.accepted_terminal_submission_json->>
                   'artifact_submission_deadline')::timestamptz OR
               (terminal.terminal_submission_json->>'submitted_at')::timestamptz <
                 termination.accepted_at OR
               (terminal.terminal_submission_json->>'submitted_at')::timestamptz >=
                 (termination.accepted_termination_json->>
                   'artifact_submission_deadline')::timestamptz OR
               (terminal.accepted_terminal_submission_json->>
                   'artifact_submission_deadline')::timestamptz
                 IS DISTINCT FROM
                   (termination.accepted_termination_json->>
                     'artifact_submission_deadline')::timestamptz OR
               terminal.terminal_submission_json->>'qualification_only'
                 IS DISTINCT FROM 'true' OR
               terminal.terminal_submission_json->>'scientific_admission_allowed'
                 IS DISTINCT FROM 'false' OR
               terminal.accepted_terminal_submission_json->>'qualification_only'
                 IS DISTINCT FROM 'true' OR
               terminal.accepted_terminal_submission_json->>
                   'scientific_admission_allowed' IS DISTINCT FROM 'false' OR
               terminal.runtime_control_pin_json->>'policy_sha256' IS DISTINCT FROM
                 terminal.accepted_terminal_submission_json->>'runtime_control_policy_sha256' OR
               terminal.runtime_control_pin_json->>'principal_id' IS DISTINCT FROM
                 terminal.accepted_terminal_submission_json->>'accepted_by_principal_id' OR
               terminal.runtime_control_pin_json->>'key_id' IS DISTINCT FROM
                 terminal.accepted_terminal_submission_json->>'acceptance_key_id' OR
               terminal.accepted_at <
                 (terminal.runtime_control_pin_json->>'valid_from')::timestamptz OR
               terminal.accepted_at >= LEAST(
                 (terminal.runtime_control_pin_json->>'expires_at')::timestamptz,
                 COALESCE(
                   (terminal.runtime_control_pin_json->>'revoked_at')::timestamptz,
                   (terminal.runtime_control_pin_json->>'expires_at')::timestamptz)) OR
               (terminal.accepted_terminal_submission_json->>
                   'artifact_submission_deadline')::timestamptz > LEAST(
                 (terminal.runtime_control_pin_json->>'expires_at')::timestamptz,
                 COALESCE(
                   (terminal.runtime_control_pin_json->>'revoked_at')::timestamptz,
                   (terminal.runtime_control_pin_json->>'expires_at')::timestamptz)) OR
               terminal.runtime_control_pin_sha256 IS DISTINCT FROM
                 termination.runtime_control_pin_sha256 OR
               terminal.runtime_control_pin_json IS DISTINCT FROM
                 termination.runtime_control_pin_json
             ) THEN
            RAISE EXCEPTION 'terminal artifact acceptance differs from full proof'
              USING ERRCODE = '23514';
          END IF;

          IF EXISTS (
            SELECT 1 FROM execution_qualification_terminal_outbox o
             WHERE o.attempt_id = target_attempt AND (
               o.execution_id IS DISTINCT FROM a.execution_id OR
               o.topic IS DISTINCT FROM 'execution.qualification_terminal.v2' OR
               o.delivery_key IS DISTINCT FROM
                 'execution-v2:' || a.execution_id || ':' || target_attempt OR
               o.outbox_id IS DISTINCT FROM 'qto_' || o.terminal_authority_sha256 OR
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
               ) OR o.terminal_authority_kind NOT IN (
                 'accepted_terminal_submission', 'terminal_deadline_expiration'
               )
             )
          ) THEN
            RAISE EXCEPTION 'qualification terminal outbox differs from exact authority'
              USING ERRCODE = '23514';
          END IF;

          IF latest_absence.disposition = 'released' THEN
            IF a.status IS DISTINCT FROM 'cancelled' OR
               resource_state IS DISTINCT FROM 'released' OR
               budget_state IS DISTINCT FROM 'released' OR head_attempt IS NOT NULL OR
               a.node_runtime_launch_receipt_sha256 IS NOT NULL OR EXISTS (
                 SELECT 1 FROM execution_device_leases d
                  WHERE d.attempt_id = target_attempt AND
                    d.state IS DISTINCT FROM 'released'
               ) THEN
              RAISE EXCEPTION 'pre-runtime release lacks proof or retained holds'
                USING ERRCODE = '23514';
            END IF;
          ELSIF termination.accepted_termination_sha256 IS NOT NULL THEN
            IF a.status NOT IN ('verifying','succeeded','failed') OR
               resource_state IS DISTINCT FROM 'released' OR
               budget_state IS DISTINCT FROM 'settled' OR
               a.terminal_receipt_sha256 IS NOT NULL OR EXISTS (
                 SELECT 1 FROM execution_device_leases d
                  WHERE d.attempt_id = target_attempt AND
                    d.state IS DISTINCT FROM 'released'
               ) OR
               (a.status = 'verifying' AND head_attempt IS DISTINCT FROM target_attempt) OR
               (a.status IN ('succeeded','failed') AND head_attempt IS NOT NULL) THEN
              RAISE EXCEPTION 'accepted runtime termination did not atomically release compute'
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
              RAISE EXCEPTION 'terminal v2 attempt lacks exact acceptance/outbox'
                USING ERRCODE = '23514';
            END IF;
            IF a.status = 'verifying' AND (
                 expiration.terminal_deadline_expiration_sha256 IS NOT NULL OR
                 EXISTS (
                   SELECT 1 FROM execution_qualification_terminal_outbox o
                    WHERE o.attempt_id = target_attempt
                 )
               ) THEN
              RAISE EXCEPTION 'verifying attempt cannot expose terminal final authority'
                USING ERRCODE = '23514';
            END IF;
          END IF;
          RETURN NULL;
        END;
        $$;
        """
    )

    for table in ("execution_attempts", *_APPEND_ONLY_TABLES):
        op.execute(
            f"""
            CREATE CONSTRAINT TRIGGER trg_{table}_runtime_v2_complete
            AFTER INSERT OR UPDATE ON {table}
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_runtime_v2_attempt()
            """
        )
    for table in (
        "execution_resource_leases",
        "execution_device_leases",
        "execution_budget_reservations",
        "execution_heads",
        "execution_attempt_adoptions",
    ):
        op.execute(
            f"""
            CREATE CONSTRAINT TRIGGER trg_{table}_runtime_v2_complete
            AFTER INSERT OR UPDATE ON {table}
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_runtime_v2_attempt()
            """
        )


def downgrade() -> None:
    # Downgrades are supported only structurally; 0024/0025 will remove their own remaining
    # functions on a full downgrade.  Refuse to erase an accepted v2 authority chain.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM execution_runtime_preparations) THEN
            RAISE EXCEPTION '0026 downgrade requires an empty runtime-v2 authority store'
              USING ERRCODE = '23514';
          END IF;
        END;
        $$;
        """
    )
    _restore_v1_guard_functions()
    op.execute("DROP FUNCTION IF EXISTS aletheia_execution_check_runtime_v2_attempt() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS aletheia_execution_guard_runtime_v2_attempt_head() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS aletheia_execution_runtime_v2_json_valid(jsonb,text)")
    op.execute(
        "DROP FUNCTION IF EXISTS aletheia_execution_json_string_array(jsonb,boolean,boolean)"
    )
    op.execute("DROP FUNCTION IF EXISTS aletheia_execution_json_shape(jsonb,jsonb)")
    for table in reversed(_APPEND_ONLY_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute(
        """
        ALTER TABLE execution_attempts
          DROP CONSTRAINT ck_execution_attempts_v2_counts,
          DROP CONSTRAINT uq_execution_attempts_runtime_preparation,
          DROP CONSTRAINT uq_execution_attempts_latest_runtime_launch_authorization,
          DROP CONSTRAINT uq_execution_attempts_latest_pre_runtime_absence,
          DROP CONSTRAINT uq_execution_attempts_node_runtime_launch_receipt,
          DROP CONSTRAINT uq_execution_attempts_runtime_termination_challenge,
          DROP CONSTRAINT uq_execution_attempts_accepted_runtime_termination,
          DROP CONSTRAINT uq_execution_attempts_accepted_terminal_submission,
          DROP CONSTRAINT IF EXISTS uq_execution_attempts_terminal_deadline_expiration,
          DROP CONSTRAINT IF EXISTS ck_execution_attempts_v2_terminal_authority,
          DROP CONSTRAINT ck_execution_attempts_versions,
          DROP CONSTRAINT ck_execution_attempts_hashes,
          DROP COLUMN runtime_preparation_sha256,
          DROP COLUMN runtime_launch_authorization_count,
          DROP COLUMN latest_runtime_launch_authorization_sha256,
          DROP COLUMN pre_runtime_absence_count,
          DROP COLUMN latest_pre_runtime_absence_receipt_sha256,
          DROP COLUMN node_runtime_launch_receipt_sha256,
          DROP COLUMN IF EXISTS runtime_termination_challenge_count,
          DROP COLUMN runtime_termination_challenge_sha256,
          DROP COLUMN accepted_runtime_termination_sha256,
          DROP COLUMN accepted_terminal_submission_sha256,
          DROP COLUMN IF EXISTS terminal_deadline_expiration_sha256,
          ADD CONSTRAINT ck_execution_attempts_versions CHECK (
            attempt_number >= 1 AND adoption_count >= 0 AND
            last_runtime_inspection_sequence >= 0 AND state_version >= 1 AND fencing_epoch >= 1),
          ADD CONSTRAINT ck_execution_attempts_hashes CHECK (
            intent_sha256 ~ '^[0-9a-f]{64}$' AND
            admission_sha256 ~ '^[0-9a-f]{64}$' AND
            grant_sha256 ~ '^[0-9a-f]{64}$' AND bundle_sha256 ~ '^[0-9a-f]{64}$' AND
            cost_quote_sha256 ~ '^[0-9a-f]{64}$' AND
            lease_token_sha256 ~ '^[0-9a-f]{64}$' AND
            node_inventory_sha256 ~ '^[0-9a-f]{64}$' AND
            (latest_adoption_sha256 IS NULL OR
              latest_adoption_sha256 ~ '^[0-9a-f]{64}$') AND
            (last_runtime_inspection_sha256 IS NULL OR
              last_runtime_inspection_sha256 ~ '^[0-9a-f]{64}$') AND
            (runtime_identity_sha256 IS NULL OR
              runtime_identity_sha256 ~ '^[0-9a-f]{64}$') AND
            (terminal_receipt_sha256 IS NULL OR
              terminal_receipt_sha256 ~ '^[0-9a-f]{64}$'));
        """
    )


def _restore_v1_guard_functions() -> None:
    op.execute(
        r"""
        DO $migration$
        DECLARE
          source_name text;
          backup_name text;
          definition text;
        BEGIN
          FOR source_name, backup_name IN
            SELECT * FROM (VALUES
              ('aletheia_execution_guard_attempt',
               'aletheia_execution_guard_attempt_v1_0026_backup'),
              ('aletheia_execution_guard_lease_state',
               'aletheia_execution_guard_lease_state_v1_0026_backup'),
              ('aletheia_execution_check_attempt_bundle',
               'aletheia_execution_check_attempt_bundle_v1_0026_backup')
            ) names(source_name, backup_name)
          LOOP
            SELECT pg_get_functiondef(to_regprocedure(backup_name || '()')) INTO definition;
            IF definition IS NULL THEN
              RAISE EXCEPTION '0026 downgrade lacks frozen backup function %', backup_name;
            END IF;
            definition := replace(
              definition,
              'FUNCTION public.' || backup_name || '()',
              'FUNCTION public.' || source_name || '()'
            );
            EXECUTE definition;
            EXECUTE format('DROP FUNCTION %I()', backup_name);
          END LOOP;
        END;
        $migration$;
        """
    )
