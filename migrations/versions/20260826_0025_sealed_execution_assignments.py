"""Add node-encrypted, replayable qualification assignment delivery.

Revision ID: 20260826_0025
Revises: 20260825_0024
Create Date: 2026-08-26

PR-4a was deliberately non-composed and had no durable raw-token delivery.  This revision refuses
to reinterpret any pre-existing attempt: an operator must drain/rebuild the qualification-only
foundation before upgrading.  Every attempt created after this revision has exactly one immutable
X25519/AEAD envelope bound to its admission, initial fence, resource lease, and enrolled node.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260826_0025"
down_revision: str | None = "20260825_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM execution_attempts) THEN
            RAISE EXCEPTION
              '0025 requires an empty PR-4a attempt store; drain and rebuild before upgrade'
              USING ERRCODE = '23514';
          END IF;
        END;
        $$;
        """
    )
    op.create_table(
        "execution_assignment_envelopes",
        sa.Column("assignment_envelope_sha256", sa.String(64), nullable=False),
        sa.Column("assignment_secret_sha256", sa.String(64), nullable=False),
        sa.Column("attempt_id", sa.String(36), nullable=False),
        sa.Column("admission_sha256", sa.String(64), nullable=False),
        sa.Column("grant_sha256", sa.String(64), nullable=False),
        sa.Column("bundle_sha256", sa.String(64), nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("node_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("resource_lease_sha256", sa.String(64), nullable=False),
        sa.Column("initial_fencing_epoch", sa.BigInteger(), nullable=False),
        sa.Column("lease_token_sha256", sa.String(64), nullable=False),
        sa.Column("transport_pin_sha256", sa.String(64), nullable=False),
        sa.Column("transport_key_id", sa.String(64), nullable=False),
        sa.Column("transport_pin_json", JSONB, nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("payload_json", JSONB, nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "initial_fencing_epoch >= 1 AND issued_at < expires_at",
            name="ck_execution_assignment_envelopes_fence_time",
        ),
        sa.CheckConstraint(
            "assignment_envelope_sha256 ~ '^[0-9a-f]{64}$' AND "
            "assignment_secret_sha256 ~ '^[0-9a-f]{64}$' AND "
            "admission_sha256 ~ '^[0-9a-f]{64}$' AND "
            "grant_sha256 ~ '^[0-9a-f]{64}$' AND "
            "bundle_sha256 ~ '^[0-9a-f]{64}$' AND "
            "node_manifest_sha256 ~ '^[0-9a-f]{64}$' AND "
            "resource_lease_sha256 ~ '^[0-9a-f]{64}$' AND "
            "lease_token_sha256 ~ '^[0-9a-f]{64}$' AND "
            "transport_pin_sha256 ~ '^[0-9a-f]{64}$' AND "
            "transport_key_id ~ '^[0-9a-f]{64}$' AND "
            "payload_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_execution_assignment_envelopes_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["execution_attempts.attempt_id"],
            name="fk_execution_assignment_envelopes_attempt",
        ),
        sa.ForeignKeyConstraint(
            ["admission_sha256", "attempt_id"],
            [
                "execution_qualification_admissions.admission_sha256",
                "execution_qualification_admissions.infrastructure_attempt_id",
            ],
            name="fk_execution_assignment_envelopes_admission_attempt",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["execution_nodes.node_id"],
            name="fk_execution_assignment_envelopes_node",
        ),
        sa.PrimaryKeyConstraint("assignment_envelope_sha256"),
        sa.UniqueConstraint("assignment_secret_sha256"),
        sa.UniqueConstraint("attempt_id"),
        sa.UniqueConstraint(
            "admission_sha256",
            name="uq_execution_assignment_envelopes_admission",
        ),
    )
    op.create_index(
        "ix_execution_assignment_envelopes_attempt_id",
        "execution_assignment_envelopes",
        ["attempt_id"],
    )
    op.create_index(
        "ix_execution_assignment_envelopes_admission_sha256",
        "execution_assignment_envelopes",
        ["admission_sha256"],
    )
    op.create_index(
        "ix_execution_assignment_envelopes_node_id",
        "execution_assignment_envelopes",
        ["node_id"],
    )
    op.create_index(
        "ix_execution_assignment_envelopes_transport_key_id",
        "execution_assignment_envelopes",
        ["transport_key_id"],
    )
    op.create_index(
        "ix_execution_assignment_envelopes_expires_at",
        "execution_assignment_envelopes",
        ["expires_at"],
    )
    op.create_index(
        "ix_execution_assignment_envelopes_created_at",
        "execution_assignment_envelopes",
        ["created_at"],
    )
    op.execute(
        """
        CREATE FUNCTION aletheia_execution_check_assignment_envelope()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          target_attempt text;
          attempt_row execution_attempts%ROWTYPE;
          envelope_row execution_assignment_envelopes%ROWTYPE;
          admission_row execution_qualification_admissions%ROWTYPE;
          resource_row execution_resource_leases%ROWTYPE;
          node_row execution_nodes%ROWTYPE;
          first_adoption execution_attempt_adoptions%ROWTYPE;
        BEGIN
          target_attempt := COALESCE(NEW.attempt_id, OLD.attempt_id);
          SELECT * INTO attempt_row FROM execution_attempts WHERE attempt_id = target_attempt;
          IF NOT FOUND THEN
            RETURN NULL;
          END IF;
          SELECT * INTO envelope_row FROM execution_assignment_envelopes
           WHERE attempt_id = target_attempt;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'execution attempt lacks an exact sealed assignment envelope'
              USING ERRCODE = '23514';
          END IF;
          SELECT * INTO admission_row FROM execution_qualification_admissions
           WHERE admission_sha256 = attempt_row.admission_sha256;
          SELECT * INTO resource_row FROM execution_resource_leases
           WHERE attempt_id = target_attempt;
          SELECT * INTO node_row FROM execution_nodes WHERE node_id = attempt_row.node_id;
          SELECT * INTO first_adoption FROM execution_attempt_adoptions
           WHERE attempt_id = target_attempt AND sequence = 1;

          IF admission_row.admission_sha256 IS NULL OR resource_row.lease_id IS NULL OR
             node_row.node_id IS NULL OR
             jsonb_typeof(envelope_row.payload_json) IS DISTINCT FROM 'object' OR
             NOT envelope_row.payload_json ?& ARRAY[
               'schema_name', 'schema_version', 'algorithm',
               'infrastructure_attempt_id', 'admission_sha256', 'grant_sha256',
               'bundle_sha256', 'node_id', 'node_manifest_sha256',
               'resource_lease_sha256', 'fencing_epoch', 'lease_token_sha256',
               'assignment_secret_sha256', 'transport_pin_sha256', 'transport_key_id',
               'ephemeral_public_key_x25519_hex', 'nonce_hex', 'aad_sha256',
               'ciphertext_base64', 'issued_at', 'expires_at', 'qualification_only',
               'scientific_admission_allowed'
             ] OR
             (SELECT count(*) FROM jsonb_object_keys(envelope_row.payload_json))
               IS DISTINCT FROM 23 OR
             jsonb_typeof(envelope_row.payload_json->'schema_name') IS DISTINCT FROM 'string' OR
             jsonb_typeof(envelope_row.payload_json->'schema_version') IS DISTINCT FROM
               'number' OR
             jsonb_typeof(envelope_row.payload_json->'algorithm') IS DISTINCT FROM 'string' OR
             jsonb_typeof(envelope_row.payload_json->'infrastructure_attempt_id')
               IS DISTINCT FROM 'string' OR
             jsonb_typeof(envelope_row.payload_json->'admission_sha256') IS DISTINCT FROM
               'string' OR
             jsonb_typeof(envelope_row.payload_json->'grant_sha256') IS DISTINCT FROM 'string' OR
             jsonb_typeof(envelope_row.payload_json->'bundle_sha256') IS DISTINCT FROM 'string' OR
             jsonb_typeof(envelope_row.payload_json->'node_id') IS DISTINCT FROM 'string' OR
             jsonb_typeof(envelope_row.payload_json->'node_manifest_sha256')
               IS DISTINCT FROM 'string' OR
             jsonb_typeof(envelope_row.payload_json->'resource_lease_sha256')
               IS DISTINCT FROM 'string' OR
             jsonb_typeof(envelope_row.payload_json->'fencing_epoch') IS DISTINCT FROM
               'number' OR
             jsonb_typeof(envelope_row.payload_json->'lease_token_sha256') IS DISTINCT FROM
               'string' OR
             jsonb_typeof(envelope_row.payload_json->'assignment_secret_sha256')
               IS DISTINCT FROM 'string' OR
             jsonb_typeof(envelope_row.payload_json->'transport_pin_sha256')
               IS DISTINCT FROM 'string' OR
             jsonb_typeof(envelope_row.payload_json->'transport_key_id') IS DISTINCT FROM
               'string' OR
             jsonb_typeof(envelope_row.payload_json->'ephemeral_public_key_x25519_hex')
               IS DISTINCT FROM 'string' OR
             jsonb_typeof(envelope_row.payload_json->'nonce_hex') IS DISTINCT FROM 'string' OR
             jsonb_typeof(envelope_row.payload_json->'aad_sha256') IS DISTINCT FROM 'string' OR
             jsonb_typeof(envelope_row.payload_json->'ciphertext_base64') IS DISTINCT FROM
               'string' OR
             jsonb_typeof(envelope_row.payload_json->'issued_at') IS DISTINCT FROM 'string' OR
             jsonb_typeof(envelope_row.payload_json->'expires_at') IS DISTINCT FROM 'string' OR
             jsonb_typeof(envelope_row.payload_json->'qualification_only') IS DISTINCT FROM
               'boolean' OR
             jsonb_typeof(envelope_row.payload_json->'scientific_admission_allowed')
               IS DISTINCT FROM 'boolean' OR
             jsonb_typeof(envelope_row.transport_pin_json) IS DISTINCT FROM 'object' OR
             NOT envelope_row.transport_pin_json ?& ARRAY[
               'schema_name', 'schema_version', 'transport_domain', 'node_id',
               'node_manifest_sha256', 'transport_policy_sha256',
               'transport_principal_id', 'transport_key_id', 'public_key_x25519_hex',
               'valid_from', 'expires_at', 'revoked_at', 'qualification_only',
               'scientific_admission_allowed'
             ] OR
             (SELECT count(*) FROM jsonb_object_keys(envelope_row.transport_pin_json))
               IS DISTINCT FROM 14 OR
             jsonb_typeof(envelope_row.transport_pin_json->'schema_name') IS DISTINCT FROM
               'string' OR
             jsonb_typeof(envelope_row.transport_pin_json->'schema_version') IS DISTINCT FROM
               'number' OR
             jsonb_typeof(envelope_row.transport_pin_json->'transport_domain') IS DISTINCT FROM
               'string' OR
             jsonb_typeof(envelope_row.transport_pin_json->'node_id') IS DISTINCT FROM 'string' OR
             jsonb_typeof(envelope_row.transport_pin_json->'node_manifest_sha256')
               IS DISTINCT FROM 'string' OR
             jsonb_typeof(envelope_row.transport_pin_json->'transport_policy_sha256')
               IS DISTINCT FROM 'string' OR
             jsonb_typeof(envelope_row.transport_pin_json->'transport_principal_id')
               IS DISTINCT FROM 'string' OR
             jsonb_typeof(envelope_row.transport_pin_json->'transport_key_id')
               IS DISTINCT FROM 'string' OR
             jsonb_typeof(envelope_row.transport_pin_json->'public_key_x25519_hex')
               IS DISTINCT FROM 'string' OR
             jsonb_typeof(envelope_row.transport_pin_json->'valid_from') IS DISTINCT FROM
               'string' OR
             jsonb_typeof(envelope_row.transport_pin_json->'expires_at') IS DISTINCT FROM
               'string' OR
             jsonb_typeof(envelope_row.transport_pin_json->'revoked_at') NOT IN
               ('null', 'string') OR
             jsonb_typeof(envelope_row.transport_pin_json->'qualification_only')
               IS DISTINCT FROM 'boolean' OR
             jsonb_typeof(envelope_row.transport_pin_json->'scientific_admission_allowed')
               IS DISTINCT FROM 'boolean' OR
             envelope_row.assignment_envelope_sha256 IS DISTINCT FROM
               envelope_row.payload_sha256 OR
             envelope_row.admission_sha256 IS DISTINCT FROM attempt_row.admission_sha256 OR
             envelope_row.admission_sha256 IS DISTINCT FROM admission_row.admission_sha256 OR
             envelope_row.grant_sha256 IS DISTINCT FROM attempt_row.grant_sha256 OR
             envelope_row.grant_sha256 IS DISTINCT FROM admission_row.grant_sha256 OR
             envelope_row.bundle_sha256 IS DISTINCT FROM attempt_row.bundle_sha256 OR
             envelope_row.bundle_sha256 IS DISTINCT FROM admission_row.bundle_sha256 OR
             envelope_row.node_id IS DISTINCT FROM attempt_row.node_id OR
             envelope_row.node_manifest_sha256 IS DISTINCT FROM node_row.node_manifest_sha256 OR
             envelope_row.resource_lease_sha256 IS DISTINCT FROM resource_row.lease_sha256 OR
             envelope_row.payload_json->>'schema_name' IS DISTINCT FROM
               'aletheia.sealed_qualification_assignment' OR
             envelope_row.payload_json->>'schema_version' IS DISTINCT FROM '1' OR
             envelope_row.payload_json->>'algorithm' IS DISTINCT FROM
               'x25519-hkdf-sha256-chacha20poly1305-v1' OR
             envelope_row.payload_json->>'infrastructure_attempt_id' IS DISTINCT FROM
               target_attempt OR
             envelope_row.payload_json->>'admission_sha256' IS DISTINCT FROM
               envelope_row.admission_sha256 OR
             envelope_row.payload_json->>'grant_sha256' IS DISTINCT FROM
               envelope_row.grant_sha256 OR
             envelope_row.payload_json->>'bundle_sha256' IS DISTINCT FROM
               envelope_row.bundle_sha256 OR
             envelope_row.payload_json->>'node_id' IS DISTINCT FROM envelope_row.node_id OR
             envelope_row.payload_json->>'node_manifest_sha256' IS DISTINCT FROM
               envelope_row.node_manifest_sha256 OR
             envelope_row.payload_json->>'resource_lease_sha256' IS DISTINCT FROM
               envelope_row.resource_lease_sha256 OR
             (envelope_row.payload_json->>'fencing_epoch')::bigint IS DISTINCT FROM
               envelope_row.initial_fencing_epoch OR
             envelope_row.payload_json->>'lease_token_sha256' IS DISTINCT FROM
               envelope_row.lease_token_sha256 OR
             envelope_row.payload_json->>'assignment_secret_sha256' IS DISTINCT FROM
               envelope_row.assignment_secret_sha256 OR
             envelope_row.payload_json->>'transport_pin_sha256' IS DISTINCT FROM
               envelope_row.transport_pin_sha256 OR
             envelope_row.payload_json->>'transport_key_id' IS DISTINCT FROM
               envelope_row.transport_key_id OR
             (envelope_row.payload_json->>'issued_at')::timestamptz IS DISTINCT FROM
               envelope_row.issued_at OR
             (envelope_row.payload_json->>'expires_at')::timestamptz IS DISTINCT FROM
               envelope_row.expires_at OR
             (envelope_row.payload_json->>'ephemeral_public_key_x25519_hex' ~
               '^[0-9a-f]{64}$') IS DISTINCT FROM TRUE OR
             (envelope_row.payload_json->>'nonce_hex' ~ '^[0-9a-f]{24}$')
               IS DISTINCT FROM TRUE OR
             (envelope_row.payload_json->>'aad_sha256' ~ '^[0-9a-f]{64}$')
               IS DISTINCT FROM TRUE OR
             (envelope_row.payload_json->>'ciphertext_base64' ~
               '^[A-Za-z0-9+/]+={0,2}$') IS DISTINCT FROM TRUE OR
             length(envelope_row.payload_json->>'ciphertext_base64') < 24 OR
             envelope_row.transport_pin_json->>'schema_name' IS DISTINCT FROM
               'aletheia.node_assignment_transport_pin' OR
             envelope_row.transport_pin_json->>'schema_version' IS DISTINCT FROM '1' OR
             envelope_row.transport_pin_json->>'transport_domain' IS DISTINCT FROM
               'ALETHEIA_QUALIFICATION_ASSIGNMENT_AEAD_V1' OR
             envelope_row.transport_pin_json->>'node_id' IS DISTINCT FROM
               envelope_row.node_id OR
             envelope_row.transport_pin_json->>'node_manifest_sha256' IS DISTINCT FROM
               envelope_row.node_manifest_sha256 OR
             envelope_row.transport_pin_json->>'transport_key_id' IS DISTINCT FROM
               envelope_row.transport_key_id OR
             (envelope_row.transport_pin_json->>'transport_policy_sha256' ~
               '^[0-9a-f]{64}$') IS DISTINCT FROM TRUE OR
             (envelope_row.transport_pin_json->>'transport_principal_id' ~
               '^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$') IS DISTINCT FROM TRUE OR
             (envelope_row.transport_pin_json->>'public_key_x25519_hex' ~
               '^[0-9a-f]{64}$') IS DISTINCT FROM TRUE OR
             (envelope_row.transport_pin_json->>'valid_from')::timestamptz IS NULL OR
             (envelope_row.transport_pin_json->>'expires_at')::timestamptz IS NULL OR
             (envelope_row.transport_pin_json->>'valid_from')::timestamptz >
               envelope_row.issued_at OR
             envelope_row.expires_at >
               (envelope_row.transport_pin_json->>'expires_at')::timestamptz OR
             (
               jsonb_typeof(envelope_row.transport_pin_json->'revoked_at')
                 IS DISTINCT FROM 'null' AND
               (
                 jsonb_typeof(envelope_row.transport_pin_json->'revoked_at')
                   IS DISTINCT FROM 'string' OR
                 (envelope_row.transport_pin_json->>'revoked_at')::timestamptz <
                   (envelope_row.transport_pin_json->>'valid_from')::timestamptz OR
                 (envelope_row.transport_pin_json->>'revoked_at')::timestamptz >
                   (envelope_row.transport_pin_json->>'expires_at')::timestamptz OR
                 envelope_row.expires_at >
                   (envelope_row.transport_pin_json->>'revoked_at')::timestamptz
               )
             ) OR
             envelope_row.transport_pin_json->>'qualification_only' IS DISTINCT FROM 'true' OR
             envelope_row.transport_pin_json->>'scientific_admission_allowed' IS DISTINCT FROM
               'false' OR
             envelope_row.payload_json->>'qualification_only' IS DISTINCT FROM 'true' OR
             envelope_row.payload_json->>'scientific_admission_allowed' IS DISTINCT FROM
               'false' THEN
            RAISE EXCEPTION 'sealed assignment envelope differs from its authority bundle'
              USING ERRCODE = '23514';
          END IF;

          IF attempt_row.adoption_count = 0 THEN
            IF envelope_row.initial_fencing_epoch <> attempt_row.fencing_epoch OR
               envelope_row.lease_token_sha256 <> attempt_row.lease_token_sha256 THEN
              RAISE EXCEPTION 'initial assignment differs from the active attempt fence/token'
                USING ERRCODE = '23514';
            END IF;
          ELSIF first_adoption.adoption_sha256 IS NULL OR
                envelope_row.initial_fencing_epoch <> first_adoption.previous_fencing_epoch OR
                envelope_row.lease_token_sha256 <>
                  first_adoption.previous_lease_token_sha256 THEN
            RAISE EXCEPTION 'initial assignment differs from first adoption lineage'
              USING ERRCODE = '23514';
          END IF;
          RETURN NULL;
        END;
        $$;

        CREATE CONSTRAINT TRIGGER trg_execution_attempt_assignment_complete
          AFTER INSERT OR UPDATE ON execution_attempts
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_assignment_envelope();
        CREATE CONSTRAINT TRIGGER trg_execution_assignment_attempt_complete
          AFTER INSERT OR UPDATE ON execution_assignment_envelopes
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_assignment_envelope();
        CREATE CONSTRAINT TRIGGER trg_execution_assignment_resource_complete
          AFTER INSERT OR UPDATE ON execution_resource_leases
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_assignment_envelope();
        CREATE CONSTRAINT TRIGGER trg_execution_assignment_adoption_complete
          AFTER INSERT OR UPDATE ON execution_attempt_adoptions
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_check_assignment_envelope();
        CREATE TRIGGER trg_execution_assignment_envelopes_immutable
          BEFORE UPDATE OR DELETE ON execution_assignment_envelopes
          FOR EACH ROW EXECUTE FUNCTION aletheia_execution_reject_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS aletheia_execution_check_assignment_envelope() CASCADE")
    op.drop_table("execution_assignment_envelopes")
