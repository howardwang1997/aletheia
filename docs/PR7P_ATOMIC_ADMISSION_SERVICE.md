# PR-7p atomic admission and Kernel-incorporation service

- Status: source composition complete; target-host commissioning pending
- Date: 2026-08-27

## What is now runnable

The PR-7e server can load
`aletheia.research_controller_atomic_admission_runtime.build_atomic_admission_rpc_service` for
exactly `commit_and_incorporate`. This final operation-family process owns the database-attestation
private key, one ordinary Research Kernel command key pinned to one exact Quest/scope/policy, and a
separate RPC receipt key. It loads no observation-admitter, validator or execution private key.

The Kernel authority is not a general command signer. It accepts only an
`OBSERVATION_INCORPORATED` proposal mechanically identical to the independently signed admission:
Quest, branch, action, scientific slot, admitted observation, outcome, source world model,
idempotency key and source event key must all match. A caller cannot substitute another event or
make the service sign its own proposal.

## Atomic transaction and retry semantics

The coordinator opens one caller-owned PostgreSQL transaction, requires an empty scientific slot,
replays the complete Kernel action, SEA, PR-4 run lineage, fresh artifact hashes and immutable
F9-v2 campaign, and DB-signs one committed admission. It then re-audits the exact authorized Kernel
action, obtains the constrained ordinary-key signature, and stages the Kernel event, snapshot,
outbox and head together with the admission row. Any failure rolls back every database write.

PostgreSQL time is sampled before custody verification, immediately before the database signature,
and after the Kernel and admission rows are staged. Clock rollback, database-key expiry or crossing
the admission challenge's half-open deadline fails the transaction. An exact retry reconstructs
and compares the complete admission row, replays the signed custody at current database time, and
accepts only the already-created exact Kernel command receipt. It cannot create a second event or a
second slot admission.

## Guarded deployment boundary

The factory pins the database URL and schema revision; exact authority bindings; root-certified
Kernel policy; service, coordinator and constrained-authority source bytes; two distinct
service-owned `0400` domain key files; a service-owned inode-pinned `0700` writable Kernel CAS; the
read-only PR-4 artifact/authority roots; and the read-only F9-v2 archive. Domain, transport, node,
commissioning and worker identities are mechanically separated, and all custody roots are
canonical and non-overlapping.

Focused tests cover exact ordinary-key authorization, rebound event/slot/source rejection,
transaction rollback, challenge expiry after Kernel staging, full exact-retry reconstruction,
direct typed RPC transport, operation closure, guarded loading, duplicate configuration, policy
rebind, key-mode drift, writable-CAS replacement and factory-byte drift.

## Remaining release gates

All eleven external operation-family factories now exist at source level. PR-8a additionally adds
the five guarded qualification service runner entrypoints and pins their manifest bytes in every
rendered systemd `ExecStart`; it deliberately supplies no production service factories. PR-8b can
now install the exact manifest/unit files while leaving them disabled and inactive, but it does not
provision principals/configs/keys or apply PostgreSQL ACLs. No target host is commissioned. The
next gate must instantiate and freeze Linux accounts, socket and PostgreSQL ACLs,
key custody, systemd units, health/alert policy and the exact deployment manifest, then run a fresh
multi-process PostgreSQL campaign covering concurrent empty-slot admission, injected rollback,
service kill/restart, dispatcher/reconciler recovery and a complete second scientific slot.

These contracts and local tests are engineering evidence, not deployment proof or a scientific
result.

See [ADR 0073](architecture/0073-atomic-admission-rpc-service.md), the
[PR-7o independent-admission guide](PR7O_INDEPENDENT_ADMISSION_SERVICE.md), the
[PR-7e server guide](PR7E_EXTERNAL_RPC_SERVICE_RUNTIME.md), and the
[PR-5 controller guide](PR5_DURABLE_SCIENTIFIC_CONTROLLER.md).
