# ADR 0067: Atomic execution-registration RPC service

- Status: accepted for the PR-7j source composition
- Date: 2026-08-26

## Context

PR-5 already had a transaction-owning registrar that coupled a signed SEA row to PR-4
qualification admission and reservation. PR-7d routed it through an external `REGISTER_EXECUTION`
port, but no checked-in guarded factory composed its Kernel/CAS verifier, public engineering
authorities, enrolled nodes, and allocator. The old receipt also exposed first-call `created` flags,
so a response-loss retry could return a different result hash.

Online historical action verification was intentionally permissive enough to replay an action that
had later become `APPLIED`. That is correct for validation custody but too weak for creating a new
execution registration: the authorization must still be the current Kernel head at the reservation
linearization point.

## Decision

- Add a caller-transaction action-authority method backed by
  `ResearchKernelStore.audit_in_session`; it locks the Quest stream and requires the exact adjacent
  authorization event/snapshot to be the current `AUTHORIZED` head.
- Use an exact read-only allocator seam to distinguish an absent pair from a complete historical
  SEA/reservation pair. Reject either one-sided state as corruption, and remove the observation
  service's former standalone SEA writer. Exact complete retries use the historical verifier and
  reload immutable reservation lineage without requiring the action to remain current.
- For a new pair, retain the Quest lock through allocation, resample database time afterwards, and
  recheck SEA liveness and public custody so a lock wait cannot cross authorization expiry.
- Replace first-call creation flags in the public atomic receipt with stable committed-state
  assertions, making exact retries byte-identical.
- Add a frozen qualification-registration composition with read-only artifacts and public authority
  registries, canonical node/enrollment/transport pins, and a PR-4 allocator configured with
  runtime-control verification but no runtime-control issuer.
- Add a guarded, keyless, single-operation RPC factory. It pins database/schema, read-only Kernel
  CAS, registrar bytes, complete public authority closure, custody roots, and transport-key
  separation; it exposes only `REGISTER_EXECUTION`.

## Consequences

- A new SEA registration and PR-4 admission/reservation either commit together against a locked
  current Kernel authorization or leave no registration.
- A lost-response retry returns the same domain receipt identity and does not require the action to
  remain current after the original transaction committed, even if the attempt lifecycle advanced.
- Historical one-sided state cannot be silently repaired or mistaken for an exact retry.
- The process has execution-database mutation capability but no scientific or runtime signing key.
  Target PostgreSQL ACLs and live process-kill tests remain required to prove the deployed boundary.
- PR-7k subsequently closes the exact public `LOAD_RAW_RUN` source, PR-7l closes database
  observation attestation, and PR-7m closes independent F9-v2 validation. Three concrete RPC
  factories remain at that checkpoint; PR-7n subsequently closes the committed-validation source,
  PR-7o closes independent admission, and PR-7p closes atomic Kernel incorporation.
