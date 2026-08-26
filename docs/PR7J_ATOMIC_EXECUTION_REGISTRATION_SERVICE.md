# PR-7j atomic execution-registration service

- Status: source composition complete; target-host commissioning pending
- Date: 2026-08-26

## What is now runnable

The PR-7e server can load
`aletheia.research_controller_execution_registration_runtime.build_execution_registration_rpc_service`
for the single `register_execution` endpoint. It accepts only a closed
`ScientificExecutionRegistrationRPCPayload`, verifies the existing signed SEA against deployment-
pinned public authorities, and invokes the existing PR-5
`PostgreSQLAtomicScientificExecutionRegistrar`.

For a new scientific slot, the registrar first takes the slot advisory lock and then uses the same
caller-owned PostgreSQL transaction to lock and fully replay the Quest stream/CAS. The action must
still be `AUTHORIZED`, and its adjacent authorization event and verified snapshot must be the
current Kernel head. Only then may the transaction append the exact SEA registration and call the
PR-4 allocator's `admit_and_reserve_in_session`. A failure at any boundary rolls back both rows.

An exact retry reloads the existing SEA and exact immutable reservation lineage, including after the
attempt advances beyond `reserved`. Its public receipt records stable committed facts rather than
first-call `created` booleans, so the typed result and receipt hash are byte-stable across a lost
response or process restart. Historical retries may succeed after the action advances, but a new
registration can never use an already-advanced Kernel head. A one-sided historical SEA or
reservation is rejected as corruption rather than adopted. The former standalone SEA writer was
removed from the observation service, so the atomic registrar is the only production call site.

The transaction also resamples database time after allocation and rechecks SEA liveness and public
custody under the still-held Quest lock. A lock wait therefore cannot carry an expired authorization
past the reservation linearization point.

## Authority boundary

The service loads only public verification material for execution authorization, validation,
admission, qualification, pricing, budget, terminal, runtime-control, node enrollment/signing,
assignment transport, and Kernel commissioning. It has no SEA, runtime-control, node, validation,
admission, terminal, or Kernel private key. Its operation-closed handler exposes only atomic SEA
registration plus qualification admission/reservation; it cannot launch an attempt, mutate node
inventory, commit a terminal result, validate/admit an observation, or write the Kernel.

The configuration pins the database URL hash and schema revision, read-only Kernel CAS
device/inode/owner/group/mode, immutable authority registry and artifact roots, canonical enrolled
nodes, allowed rate cards/currencies, registrar implementation bytes, and pairwise-separated
principals, public keys, and policies. The RPC transport-receipt key must not reuse any domain,
node, Kernel, or X25519 assignment public key.

This is a source-level authority boundary. Production PostgreSQL grants must still restrict the
service account to the exact append/reservation transaction; the presence of an allocator object in
the process is not itself ACL evidence.

## Local verification

Focused tests cover same-transaction Kernel locking, rejection of a later head, post-allocation
liveness revalidation, one-sided-state rejection, atomic rollback, SEA/PR-4 exact binding,
byte-stable retry after attempt progress, public-only allocator composition, role/key/policy
separation, canonical configuration, operation closure, guarded factory loading, implementation
byte pins, and transport/domain key non-reuse.

## Remaining release gates

Six PR-7e concrete factories remain: raw-run source, database observation attestation, independent
F9-v2 validation, committed-validation source, independent admission, and atomic admission/Kernel
incorporation. The next ordered source slice is the `LOAD_RAW_RUN` factory, which must reconstruct
only an exact preregistered and terminally verified PR-4 run without acquiring execution mutation
or scientific signing authority.

No target host is commissioned. Linux account/socket/PostgreSQL/CAS/registry ACLs, systemd
supervision, alerts, receipt-key custody/rotation, and fresh PostgreSQL process-kill/restart tests
remain mandatory. These source and test receipts are engineering evidence, not deployment proof or
a scientific result.

See [ADR 0067](architecture/0067-atomic-execution-registration-rpc-service.md), the
[PR-7i execution-authorization guide](PR7I_EXECUTION_AUTHORIZATION_SERVICE.md), the
[PR-7e server guide](PR7E_EXTERNAL_RPC_SERVICE_RUNTIME.md), and the
[PR-5 controller guide](PR5_DURABLE_SCIENTIFIC_CONTROLLER.md).
