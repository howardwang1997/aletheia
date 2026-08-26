# PR-7o independent admission service

- Status: signer composition complete; target-host commissioning pending
- Date: 2026-08-27

## What is now runnable

The PR-7e server can load
`aletheia.research_controller_independent_admission_runtime.build_independent_admission_rpc_service`
for exactly `issue_admission_decision`. The process owns the observation-admitter private key and
its separate RPC receipt key. It does not own database, execution, validator or Research Kernel
private keys, and it cannot write a scientific slot, publish validation material or mutate the
Kernel.

The service accepts only one committed validation and its exact database-signed admission
challenge. It independently replays the complete validation chain through Research Kernel state,
the signed SEA, PR-4 run lineage and fresh artifact hashes, and the immutable graph-scoped F9-v2
campaign. A validated confirmation deterministically yields an `ADMITTED` proposal; a verified
scientific rejection or engineering blocker yields `REJECTED` with the validation blocker codes.
Neither result itself confers scientific authority.

## Live challenge semantics

Admission decisions are no longer backdated to challenge issuance. The signer samples PostgreSQL
time before complete custody replay and again immediately before signing, rejects clock rollback,
and requires `challenge.issued_at <= decided_at < challenge.expires_at`; the signed decision carries
that post-verification time. The later atomic commit must satisfy
`decided_at <= registered_at <= committed_at < challenge.expires_at`. An expired challenge therefore
cannot be replayed to obtain a fresh decision.

## Deployment closure

The guarded factory freezes the single admission authority binding, the admission key file,
database URL/schema, read-only Kernel CAS, complete PR-4 public authority set, artifact store,
authority registry, read-only F9-v2 archive and exact service bytes. All key/custody roots are
canonical, inode-pinned where applicable and non-overlapping. The RPC receipt key is mechanically
separate from every domain and node key.

Focused tests cover admitted and rejected derivation, expired-challenge rejection, nested-custody
failure, wrong-key startup, direct typed RPC transport, operation/authority closure, guarded loading,
duplicate configuration, key-mode drift, archive replacement and factory-byte drift.

## Subsequent closure and remaining release gates

PR-7p subsequently closes the final source-level factory: atomic admission plus Research Kernel
incorporation. It holds the database-attestation and exact ordinary Kernel-command keys, reverifies
this independent proposal, enforces the empty-slot CAS, and commits the admission row together with
the Kernel event/snapshot/outbox/head in one PostgreSQL transaction.

No target host is commissioned. Exact PostgreSQL ACLs, Linux accounts/socket ownership, systemd
supervision, alerts and a fresh multi-process PostgreSQL kill/restart campaign remain mandatory.
This service and its tests are engineering evidence, not deployment proof or a scientific result.

See [ADR 0072](architecture/0072-independent-admission-rpc-service.md), the
[PR-7p atomic-admission guide](PR7P_ATOMIC_ADMISSION_SERVICE.md), the
[PR-7n committed-validation source guide](PR7N_COMMITTED_VALIDATION_SOURCE_SERVICE.md), the
[PR-7e server guide](PR7E_EXTERNAL_RPC_SERVICE_RUNTIME.md), and the
[PR-5 controller guide](PR5_DURABLE_SCIENTIFIC_CONTROLLER.md).
