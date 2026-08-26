# ADR 0068: Verified raw-run source RPC service

- Status: accepted for the PR-7k source composition
- Date: 2026-08-26

## Context

PR-5 had a deterministic raw-run assembler and a PR-4 allocator read method that replayed terminal
lineage and freshly rehashed artifacts. PR-7d routed `LOAD_RAW_RUN` through an external port, but no
guarded concrete factory composed that path. The assembler also trusted a canonical SEA row without
rechecking its signature and did not carry qualification-admission chronology in its terminal
projection, so it could not itself prove preregistration.

## Decision

- Version the ephemeral `VerifiedQualificationRawRunMaterial` projection to v2 and add exact
  qualification-admission, reservation, and runtime-launch times with closed historical ordering.
- Reverify the canonical SEA and its execution/validator/admitter/qualification signatures under
  deployment-pinned public keys, then require `registered_at < qualification_admitted_at`.
- Reuse the complete PR-4 lineage verifier and fresh artifact CAS rehash behind a new
  `VerifiedQualificationRawRunMaterialReader`. The factory and RPC handler receive only this narrow
  facade, never the allocator mutation surface.
- Add a guarded, keyless, single-operation factory that pins database/schema, bridge and PR-4 public
  authorities, artifact/registry custody, source bytes, endpoint identity, and role separation.
- Keep `assembled_at` derived from immutable terminal/artifact receipts rather than the current DB
  observation time, preserving byte-stable retries.

## Consequences

- `LOAD_RAW_RUN` can reconstruct only a preregistered, terminally verified PR-4 execution and cannot
  fabricate or admit a scientific outcome.
- The source process has no signing key and exposes no execution mutation method. Read-only database
  grants and live host evidence remain required because Python operation closure is not an ACL.
- PR-7l subsequently closes database observation attestation and PR-7m closes independent F9-v2
  validation. Three concrete factories remain; the next is the committed-validation source.
  PR-7n subsequently closes that source, leaving independent admission next and two factories.
