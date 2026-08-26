# Architecture decision 0071: Committed-validation source RPC service

- Status: accepted for the source-composition slice
- Date: 2026-08-27
- Scope: keyless exact-slot read and complete historical validation verification

## Context

The admission step already used a typed `LOAD_COMMITTED_VALIDATION` RPC port. Its local PostgreSQL
adapter, however, only reparsed the stored JSON and checked the action hash. No checked-in process
factory composed full Kernel, PR-4, artifact, F9-v2, validator and database-signature verification.
A syntactically canonical row therefore was not sufficient production-source evidence.

## Decision

- Keep the request closed to Quest, action and scientific-slot identity. Resolve the immutable row
  from PostgreSQL rather than accepting caller material.
- Reconstruct the exact `ObservationValidationReceiptWrite` and reject any row whose JSON or hashes
  differ from that canonical projection.
- Sample PostgreSQL time and call the full historical committed-validation verifier. This replays the
  nested DB challenge/commit, independent validator receipt, F9-v2 campaign, Kernel action, SEA,
  PR-4 run lineage and artifact CAS.
- Deploy the source as an authority-neutral service with no domain signing key. Its service identity
  is separate from the database and validator bindings whose public signatures it verifies.
- Pin the database/schema, adapter bytes and every read-only CAS/registry/archive root. Reject root,
  authority, service identity, policy, manifest or receipt-key reuse.

## Consequences

- Admission can now load only a fully verified durable validation from an operation-closed external
  source; a canonical-looking database row alone is no longer sufficient.
- The process cannot create validation or scientific evidence and carries no database, validator,
  admitter, execution or Kernel private key.
- Two concrete factories remain. Independent admission is next, followed by atomic
  admission/Kernel incorporation.
- Target-host PostgreSQL ACLs, filesystem custody, supervision and live crash/restart evidence remain
  release gates.
