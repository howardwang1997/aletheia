# ADR 0069: Database-observation RPC service

- Status: accepted for the PR-7l source composition
- Date: 2026-08-27

## Context

PR-5 defined DB-time validation/admission challenges and committed-validation receipts, while PR-7d
routed their three database operations through one external authority port. There was no guarded
factory for that port. The service responses also carried a transient `created` bit, validation
commit accepted a correctly signed challenge without proving that the database had issued and
stored it, and operations on one scientific slot were not explicitly serialized before sampling
database time.

The concrete raw-run adapter could prove full terminal and artifact lineage but could not implement
the qualification-custody ports used by the bridge from its public projection. The F9-v2 campaign
archive also lacked a read-only mode suitable for a database authority that must verify but never
publish validator evidence.

## Decision

- Version `VerifiedQualificationRunLineage` to v3 and embed the exact stable
  `VerifiedEngineeringQualification` admitted by PR-4. Expose it only through a narrow run-lineage
  reader and use one concrete adapter for qualification-admission and raw-run verification.
- Lock the immutable SEA row with `FOR UPDATE` before every database-attestation operation and
  sample PostgreSQL time only after that lock is held.
- Require validation commit to load the exact historical challenge by hash and reconstruct the same
  append-only row. Recheck database time after full verification and signing, before persistence.
- Reverify the complete committed-validation chain before issuing an admission challenge.
- Make all three RPC results closed Pydantic contracts and omit first-attempt `created` state so
  exact retries are byte-identical.
- Add read-only F9-v2 archive behavior and a guarded three-operation factory. It loads only the
  database-attestation domain key; Kernel, PR-4, execution, validator, and admitter inputs are
  public/read-only and role-separated.

## Consequences

- A signed but never registered challenge cannot authorize a validation commit, and concurrent
  operations for one slot share one PostgreSQL serialization point.
- The database authority can attest durable timing and committed validation but cannot produce the
  independent validation, decide admission, or mutate the Research Kernel.
- Python operation closure is not a deployed ACL. Target PostgreSQL grants, source-external key
  custody, Linux supervision, and live crash/restart evidence remain release gates.
- PR-7m subsequently closes independent F9-v2 validation. Three concrete factories remain; the
  next is the committed-validation source at that checkpoint. PR-7n subsequently closes that
  source, and PR-7o closes independent admission, leaving only atomic Kernel incorporation.
