# PR-7c verified qualification-terminal dispatcher

- Status: checked-in public-key-only composition complete; target-host commissioning pending
- Date: 2026-08-26
- Scope: let the durable controller consume PR-4 terminal wakeups without trusting a bare outbox
  row or loading runtime-control signing custody

## What this slice closes

The PR-5 terminal dispatcher previously read an immutable qualification-terminal outbox projection
and turned it into a controller wakeup. Immutability prevents later rewriting, but the row alone does
not prove that the attempt passed qualification, held the recorded resources, launched on the
enrolled node, reached an accepted runtime termination, and produced the exact terminal authority.

`PostgreSQLExecutionAllocator.load_verified_qualification_terminal_source()` now reconstructs that
history before delivery. Accepted submissions reuse the complete verified qualification run
lineage, including freshly rehashed artifacts. Deadline expirations independently replay the stored
qualification, node enrollment, launch, accepted termination, pre-signed expiration, PostgreSQL-time
activation, execution head, and immutable outbox. Both paths return a closed
`VerifiedQualificationTerminalSource`; neither makes a terminal event scientific evidence.

The dispatcher and restart recovery adapter require this verified projection and then re-read the
exact outbox inside the delivery transaction. A missing proof, changed source, different
execution/attempt, changed qualification bundle/grant, late scientific-execution registration, or
changed terminal identity fails before enqueue or durable delivery. The registration must predate
the qualification admission recorded by the verified lineage. The normal delivery/task/source
uniqueness and redrive rules remain unchanged.

## Public-key-only process composition

Runtime-control verification is now a separate port from runtime-control issuance.
`PinnedRuntimeControlVerificationAuthority` carries only an Ed25519 public pin and historical
verifier. `VerifiedQualificationTerminalOutboxReader` exposes only the verified-source loader and
the caller-owned transactional outbox read; allocator mutation methods are not part of the runtime
port.

The checked-in terminal factory freezes:

- controller manifest, process principal, database URL hash, and schema revision;
- the exact artifact-store root with a pre-existing safe layout, plus the authority-registry root
  and its frozen filesystem identity;
- pricing, source-budget, qualification, terminal, runtime-control, node-enrollment, node-signing,
  and assignment-transport public authority pins;
- enrolled node manifests and canonical rate-card/currency allowlists; and
- permanent false flags for signing-key, execution-mutation, Kernel-mutation, scientific-authority,
  and observation-admission access.

All sensitive principals and key IDs must be distinct. The artifact CAS must already contain its
complete safe directory layout and is opened in a fail-closed read-only mode; the terminal process
cannot quarantine or publish outputs. The guarded-loader module is intentionally a small operational
wrapper; the typed composition remains in `aletheia.execution`, and the wrapper imports it only
inside the pinned factory call. This keeps dynamic-origin inspection bounded while preserving
exact-byte deployment loading.

## Verification

The source and controller suites cover:

- accepted-terminal and deadline-expiration lineage reconstruction on isolated PostgreSQL;
- a verifier-only allocator with no runtime-control issuance method;
- dispatcher rejection of raw-without-proof and proof/outbox rebinding;
- exact binding to the preregistered scientific execution authorization and its pre-admission
  chronology;
- restart recovery requiring the same exact verified source;
- authority/principal overlap and duplicate-config rejection;
- direct and guarded-loader construction of the pinned terminal role; and
- the repository dependency, legacy-driver, and normalized-AST governance gates.

The recommended source checkpoint is:

~~~bash
conda run -n aletheia env PYTHONPATH=. pytest -q \
  tests/research_controller/test_dispatcher.py \
  tests/research_controller/test_recovery.py \
  tests/research_controller/test_runtime_process.py \
  tests/research_controller/test_terminal_runtime.py

conda run -n aletheia env PYTHONPATH=. pytest -q \
  tests/migration/test_pr0_dependency_boundary.py \
  tests/migration/test_legacy_write_inventory.py \
  tests/research_controller/test_dependency_boundary.py
~~~

PostgreSQL execution tests must use a freshly migrated, explicit loopback `aletheia_pr4*` database
as required by `tests/execution/postgres_test_safety.py`; they intentionally skip against an
implicit or non-isolated database.

The 2026-08-26 source checkpoint passed 511 execution tests with 68 safely skipped, 112 controller
tests with three safely skipped, 62 observation tests, and 266 migration/schema tests. On a fresh
`20260828_0027` PostgreSQL database, all 28 runtime-v2 allocator tests and all six terminal-runtime
tests passed, and `alembic check` reported no drift. These are repository and isolated-database
results, not target-host commissioning evidence.

## Remaining gates

This slice commissions repository composition, not a host. The deployment still must prove the
terminal process's read-only PostgreSQL ACL, filesystem custody, exact authority-registry contents,
public-key-only secret inventory, supervision/alerts, and kill/restart behavior. PR-4's exact
Linux/root/systemd/loop/ext4/rootful-Docker campaign remains mandatory.

The worker also remains uncommissioned. The PR-6 compatibility handler/image and durable
execution-registration source slice, independent graph-scoped F9-v2 validation source slice, and
atomic admission/Kernel submission adapter now exist. Their exact target-host qualification,
external process/key composition, continuation custody, and the full step-specific worker
composition remain open. No terminal wakeup is an observation, claim, or deployment qualification.

See [ADR 0054](architecture/0054-verified-qualification-terminal-dispatcher.md), the
[PR-7 runtime guide](PR7_CONTROLLER_PRODUCTION_RUNTIME.md), the
[PR-7b authority guide](PR7B_CONTROLLER_STEP_AUTHORITY_BOUNDARY.md), and the
[end-to-end architecture](END_TO_END_AUTONOMOUS_RESEARCH_ARCHITECTURE_2026_08_22.md).
