# Architecture decision 0072: Independent admission RPC service

- Status: accepted for the signer-composition slice
- Date: 2026-08-27
- Scope: isolated admission proposal signing with live database challenge time

## Context

The durable controller already called a typed `ISSUE_ADMISSION_DECISION` RPC port, but no
checked-in process factory owned only the admission key and recomputed the decision from complete
external custody. The underlying bridge function also stamped a decision with challenge issuance
time, which could not prove that signing occurred before challenge expiry.

## Decision

- Deploy the admission signer outside the controller worker with exactly the
  `ISSUE_ADMISSION_DECISION` operation and independent-admission authority binding.
- Give that process only the admission private key plus a separate transport-receipt key. All
  database, execution, validator and Kernel authority material is public verification input.
- Reverify the committed validation, database challenge, Kernel action, SEA, PR-4 lineage, artifact
  CAS and immutable F9-v2 campaign before signing.
- Derive `ADMITTED` only from a validated confirmation. Preserve a verified non-confirmation as a
  `REJECTED` proposal with its canonical validation blockers.
- Sample PostgreSQL time before full custody replay and again immediately before signing. Reject
  rollback, require the second sample inside the challenge's half-open live window, carry it as
  `decided_at`, and require later registration to follow it.
- Keep the signed result explicitly non-authoritative until the empty-slot CAS and Kernel
  incorporation commit together.

## Consequences

- A worker cannot choose disposition, reasons or signing time, and an expired challenge cannot be
  replayed into a newly signed proposal.
- The signer cannot reserve or fill a slot, attest a database commit or issue a Kernel command.
- PR-7p subsequently closes atomic admission/Kernel incorporation, the final concrete PR-7e
  factory at source level.
- Target-host ACLs, process supervision and live crash/restart evidence remain release gates.
