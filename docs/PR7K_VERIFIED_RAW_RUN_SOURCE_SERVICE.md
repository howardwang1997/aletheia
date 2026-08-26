# PR-7k verified raw-run source service

- Status: source composition complete; target-host commissioning pending
- Date: 2026-08-26

## What is now runnable

The PR-7e server can load
`aletheia.research_controller_raw_run_source_runtime.build_raw_run_source_rpc_service` for the
single `load_raw_run` endpoint. It accepts only a closed scientific-slot lookup and reconstructs a
deterministic `RawRunEnvelope`; it does not accept caller-supplied terminal or artifact material.

The source reloads the exact canonical SEA row, historically verifies its execution, validation,
admission, and qualification signatures under deployment-pinned public keys, and then invokes a
narrow PR-4 read facade. That facade replays qualification admission, resource reservation,
runtime-v2 launch/termination, node enrollment/signatures, terminal acceptance, and fresh artifact
CAS rehash before returning typed terminal material. The exported material now includes admission,
reservation, and launch times, so the source mechanically requires strict SEA preregistration before
qualification admission. A late or invalid SEA, incomplete terminal lineage, rebound action/slot,
or changed artifact fails closed.

Repeated reads may occur at different database times, but `assembled_at` is derived only from the
immutable terminal acceptance and artifact verification receipts. The resulting raw-run bytes and
hash are therefore stable across restart and retry.

## Authority boundary

The service loads public verification keys and read-only artifact/authority-registry custody only.
It has no execution, runtime-control, node, database-attestation, validation, admission, terminal,
or Kernel signing key. Its handler exposes no allocator mutation, challenge issuance, validation,
admission, or Kernel operation. The internal legacy allocator verifier is retained only behind an
operation-closed read facade; production PostgreSQL grants must independently deny every execution
mutation table operation.

Configuration freezes database/schema, exact service/worker/deployment identities, the execution-
authority binding, all bridge and PR-4 public authorities, canonical nodes/rate cards/currencies,
read-only artifact and registry roots, and source implementation bytes. Service, worker, transport-
receipt, and domain principals are separated; the service/domain policies and receipt/domain keys
are also disjoint, including from the execution-node sandbox policies.

## Local verification

Focused tests cover deterministic reconstruction, historical SEA signature rejection, strict
prelaunch chronology, rebound terminal material, the operation-closed raw-material facade,
public-only composition, duplicate/rebound configuration, guarded factory loading, implementation
and factory byte drift, and absence of mutation/signing fields.

## Remaining release gates

PR-7l subsequently closes database observation attestation with DB-time challenges and committed
validation receipts while loading no validator, admitter, execution, or Kernel private key. PR-7m
then closes the independent F9-v2 validator factory. Three PR-7e concrete factories remain:
committed-validation source, independent admission, and atomic admission/Kernel incorporation. The
PR-7n subsequently closes the committed-validation source. The next ordered source slice is
independent admission at that checkpoint; PR-7o now closes it, leaving one atomic-incorporation
factory.

No target host is commissioned. Read-only PostgreSQL/CAS/registry ACLs, Linux account/socket
ownership, systemd supervision, alerts, and fresh multi-process PostgreSQL kill/restart tests remain
mandatory. These source and test receipts are engineering evidence, not deployment proof or a
scientific result.

See [ADR 0068](architecture/0068-verified-raw-run-source-rpc-service.md), the
[PR-7l database-observation guide](PR7L_DATABASE_OBSERVATION_SERVICE.md), the
[PR-7m independent-validation guide](PR7M_INDEPENDENT_F9_V2_VALIDATION_SERVICE.md), the
[PR-7j registration guide](PR7J_ATOMIC_EXECUTION_REGISTRATION_SERVICE.md), the
[PR-7e server guide](PR7E_EXTERNAL_RPC_SERVICE_RUNTIME.md), and the
[PR-5 controller guide](PR5_DURABLE_SCIENTIFIC_CONTROLLER.md).
