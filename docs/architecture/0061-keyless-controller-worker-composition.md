# Architecture decision 0061: Keyless controller worker composition

- Status: accepted for the complete worker source composition
- Date: 2026-08-26
- Scope: production `research.controller.v1` worker factory

## Decision

The checked-in worker factory now installs the exhaustive eight-adapter set required by ADR 0053.
It does not construct a generic model callback and it does not load execution, validation,
admission, database-attestation, or Research Kernel signing keys. Instead, eleven named Unix-stream
ports expose only the operations needed by proposal materialization, compilation, execution
authorization/registration, raw-run recovery, validation, admission, and continuation assessment.

Every port freezes its socket path and filesystem identity, Linux `SO_PEERCRED` UID/GID, service
principal, manifest, policy, allowed operations, authority-binding hashes, request/response byte bounds, and
an independent Ed25519 transport-receipt public key. Requests are deterministic and content
identified. Responses must bind the exact request and endpoint, be canonical JSON, carry a
canonical typed result hash, fall inside the pinned key interval, and verify under that receipt key.
A signed canonical blocker variant is translated only for proposal, compiler, and continuation
steps that already define typed non-retryable blockers; transport failures remain retryable task
failures rather than fabricated scientific dispositions.
A transport receipt authenticates the service boundary; it never replaces SEA, validation,
admission, database, or Kernel domain signatures and transaction checks.

The worker configuration maps every RPC operation exactly once and requires distinct service
identities, receipt keys, and socket paths. Each adapter config hash is derived only from the
endpoints reachable by that step. The factory fresh-hashes the installed adapter source against the
adapter manifest before composition and binds the aggregate set to the exact controller manifest,
worker manifest, process principal, preparation time, database URL hash, and schema revision.

Recovery remains local and read-only. The factory composes the existing verified PR-4 terminal
reader from public verification material so recovery can re-read the immutable terminal outbox in
the same PostgreSQL transaction. It also composes a Research Kernel audit store from a public trust
root and a filesystem CAS opened in explicit read-only mode. The CAS root is pinned by canonical
path, owner, group, device, inode, and non-writable mode; attempts to stage objects or snapshots fail
before a filesystem write.

## Consequences

- The production worker role now has a checked-in guarded-loader factory rather than a deployment
  placeholder.
- All eight active steps are reachable only through their typed adapters; passive waits remain
  local and no catch-all callback exists.
- A worker compromise does not reveal scientific signing keys from its configuration or composed
  objects. Actual process memory, filesystem ACL, and key absence still require host evidence.
- RPC services must implement the exact envelopes and retain their own durable/domain verification
  behavior. The repository does not yet commission those service processes or their private keys.
- Source-file hashing is deployment provenance, not runtime-memory attestation. An immutable release
  mount, exact Unix socket ACLs, PostgreSQL roles, supervision, alerting, and live kill/restart tests
  remain mandatory before production use.
