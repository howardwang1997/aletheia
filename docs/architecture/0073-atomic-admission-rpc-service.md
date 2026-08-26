# Architecture decision 0073: Atomic admission and Kernel-incorporation RPC service

- Status: accepted for the final PR-7 source-composition slice
- Date: 2026-08-27
- Scope: one scientific-slot admission and its exact Research Kernel incorporation

## Context

The controller already had a caller-owned PostgreSQL transaction that could persist a committed
observation admission and the corresponding Kernel event, snapshot, outbox and stream head. The
remaining production boundary had no checked-in factory for that operation and no constrained
ordinary-key authority. The admission decision signer is intentionally independent and cannot
commit a slot or mutate the Kernel.

## Decision

- Deploy one `COMMIT_AND_INCORPORATE` service outside the keyless controller worker.
- Give it the database-attestation key, one ordinary Kernel key for one exact Quest/scope/policy,
  and a separate transport-receipt key. Do not load admission, validation or execution keys.
- Constrain the Kernel authority to the exact `OBSERVATION_INCORPORATED` payload derived from the
  independently signed admission, including its idempotency and source-event identities.
- Reverify all Kernel, SEA, PR-4, artifact, F9-v2, validator, admitter and database custody before
  committing. Persist the admission row and Kernel mutation through one caller-owned transaction.
- Sample live PostgreSQL time before verification, immediately before the database signature and
  after database staging. Reject rollback or deadline crossing.
- On retry, reconstruct the complete persisted admission, repeat custody verification and require
  the pre-existing exact Kernel command receipt; never infer success from a partial row.
- Guard the process with byte-pinned source and config, distinct `0400` keys, an inode-pinned
  service-owned writable CAS, read-only evidence roots, exact authority separation and one closed
  RPC operation.

## Consequences

- An independently signed admission proposal remains powerless until this transaction commits.
- A compromised caller cannot ask the ordinary key to sign a different Kernel event, slot, action,
  outcome or world model.
- Database or Kernel staging failure leaves neither authoritative database state committed, and an
  exact retry cannot duplicate the scientific slot or event.
- All eleven external operation-family factories now exist at source level.
- Linux identities/ACLs, key installation, systemd supervision and a fresh multi-process
  PostgreSQL kill/restart campaign remain mandatory release gates.
