# PR-7l database-observation attestation service

- Status: source composition complete; target-host commissioning pending
- Date: 2026-08-27

## What is now runnable

The PR-7e server can load
`aletheia.research_controller_database_observation_runtime.build_database_observation_rpc_service`
for exactly three operations: `issue_validation_challenge`, `commit_validation`, and
`issue_admission_challenge`. The process owns one database-attestation Ed25519 domain key in a
source-external, single-link `0400` file. Its RPC receipt key is a separate key and neither key is
accepted as a validator, admitter, execution, PR-4, or Kernel authority.

Every operation first locks the exact preregistered scientific slot through its immutable SEA row,
then samples PostgreSQL time. Validation commitment requires the caller's signed validation receipt
to reference the exact challenge bytes already stored in the append-only challenge table; a valid
but unregistered challenge cannot be committed. After full PR-4, Kernel, raw-run, F9-v2 campaign,
and signature verification, the service samples database time again before appending the committed
validation. Admission-challenge issuance reloads the exact committed-validation row and repeats the
complete custody verification before signing.

First execution and exact restart replay return the same closed Pydantic response bytes. Operational
`created` state is deliberately absent from the RPC contract; durable identity is carried by the
signed challenge or committed-validation hash.

## Read-only custody composition

The service composes a read-only Research Kernel audit, a narrow complete PR-4 run-lineage reader,
fresh artifact CAS rehash, and a read-only F9-v2 validation-campaign archive. The run-lineage
projection now embeds the exact stable `VerifiedEngineeringQualification`, allowing the same
concrete adapter to prove qualification admission, full raw-run custody, and strict
SEA-registration-before-admission chronology without exposing allocator mutation methods.

The F9-v2 archive reader cannot publish a campaign and does not create lock files. Its root and the
Kernel CAS are pinned by canonical path, device, inode, owner, group, and non-writable mode. The
factory also pins database URL/schema revision, all public authorities and policies, exact service
implementation bytes, controller/worker identity, challenge TTL, and disjoint filesystem roots.

## Local verification

Focused tests cover the three-operation partition, guarded factory loading, database-domain/RPC-key
separation, read-only PR-4 and F9-v2 facades, canonical stable retries, exact persisted-challenge
requirements, final DB-time liveness checks, complete committed-validation revalidation, source/key
byte pins, duplicate/rebound configuration, and root inode drift. The existing allocator, raw-run,
scientific-bridge, controller, dependency, schema, and inventory suites remain the broader regression
gate.

## Remaining release gates

PR-7m subsequently closes the independent F9-v2 validation factory with an isolated validator key,
full public-custody replay, a conservative exact-content assessor and write-once campaign archive.
Three PR-7e concrete factories remain: committed-validation source, independent admission, and
atomic admission/Kernel incorporation at that checkpoint. PR-7n subsequently closes the keyless
committed-validation source, leaving independent admission as the next of two factories.

No target host is commissioned. Exact PostgreSQL write/read ACLs, Linux account/socket and key
ownership, systemd supervision, alerts, key rotation/revocation, and a fresh multi-process
PostgreSQL kill/restart campaign remain mandatory. These source and test receipts are engineering
evidence, not deployment proof or a scientific result.

See [ADR 0069](architecture/0069-database-observation-rpc-service.md), the
[PR-7m independent-validation guide](PR7M_INDEPENDENT_F9_V2_VALIDATION_SERVICE.md), the
[PR-7k raw-run source guide](PR7K_VERIFIED_RAW_RUN_SOURCE_SERVICE.md), the
[PR-7e server guide](PR7E_EXTERNAL_RPC_SERVICE_RUNTIME.md), and the
[PR-5 controller guide](PR5_DURABLE_SCIENTIFIC_CONTROLLER.md).
