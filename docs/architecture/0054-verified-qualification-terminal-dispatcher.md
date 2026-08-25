# ADR 0054: Verified qualification-terminal dispatcher

- Status: accepted for the PR-7c source/runtime boundary
- Date: 2026-08-26

## Context

PR-5 made qualification-terminal outbox delivery durable, and PR-7a supplied a supervised terminal
role, but the port exposed only an immutable outbox row. A valid row was not by itself proof of the
PR-4 qualification, reservation, enrolled-node launch, runtime termination, and artifact custody
that preceded it. Reusing the normal allocator composition would also load runtime-control issuance
custody and expose an unnecessary execution-mutation surface to an operational dispatcher.

## Decision

Separate runtime-control historical verification from issuance. Before a terminal source may wake
the controller, reconstruct a closed verified projection over its exact PR-4 history. Accepted
terminal submissions use the full qualification-run lineage; deadline terminals reverify their
pre-signed expiration and PostgreSQL-time activation. Re-read the immutable outbox inside the
delivery transaction and require it to match the verified source byte-for-byte at the typed field
boundary. Also require the proof's qualification bundle and grant to match the preregistered
scientific execution authorization, whose durable registration must strictly predate qualification
admission.

Compose the terminal process from public authority pins only. Open the pre-existing artifact CAS in
read-only mode, expose a narrow reader with no allocator mutation methods, and reject principal/key
overlap. Keep the exact-byte guarded-loader entry point small and put the typed execution
composition inside the new execution-authority package. Retain the outer entry point in the legacy
operational AST freeze because it composes the existing durable queue.

## Consequences

- A bare or rebound terminal outbox cannot create a controller wakeup.
- Restart recovery independently re-establishes the same lineage instead of trusting a prior
  in-memory result.
- The checked-in terminal process does not contain runtime-control private signing material and
  cannot turn qualification evidence into scientific admission.
- Python port narrowing is not a substitute for host ACLs. Target-host database/filesystem custody,
  process supervision, secret inventory, and live kill/restart campaigns remain required.
- The next worker slices must preserve independent execution, validation, admission, and Kernel
  signing authorities rather than folding them into this dispatcher.
