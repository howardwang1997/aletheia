# PR-7n committed-validation source service

- Status: source composition complete; target-host commissioning pending
- Date: 2026-08-27

## What is now runnable

The PR-7e server can load
`aletheia.research_controller_committed_validation_runtime.build_committed_validation_source_rpc_service`
for exactly `load_committed_validation`. The request contains only Quest, action and scientific-slot
identity. Caller-supplied receipts, campaigns, terminal material and timestamps are not accepted.
The source process is authority-neutral and keyless: its RPC transport key is its only private key,
and it cannot issue or commit validation, publish a campaign, decide admission or mutate the Kernel.

The adapter reads the one append-only validation row for the exact slot, reconstructs its closed
committed receipt and demands byte-for-byte equality with the canonical row projection. It samples
PostgreSQL time and then historically verifies the database commitment and nested validator receipt,
including the exact DB challenge, F9-v2 campaign winner, Research Kernel action/snapshot, signed SEA,
complete PR-4 qualification/run lineage and freshly rehashed artifact CAS. A row with valid-looking
JSON but broken custody is rejected.

## Read-only composition

The service pin carries both the database-attestation and independent-validation authority bindings
because those are the two signatures being reverified; the source principal, manifest and policy
must be distinct from both. The guarded factory also freezes the database URL/schema revision,
read-only Kernel CAS, complete PR-4 public authority set, artifact store, authority registry,
read-only F9-v2 campaign archive and exact adapter source bytes. All custody roots are canonical,
inode-pinned, non-overlapping and non-writable from this source facade.

Focused tests cover full nested custody verification, rejection after raw-run custody failure,
canonical row/action binding, the single-operation RPC partition, direct typed RPC round-trip,
authority-neutral service identity, absence of domain keys/mutations, guarded factory loading,
duplicate/rebound configuration, source-byte drift and archive inode replacement.

## Remaining release gates

Two PR-7e concrete factories remain: independent admission and atomic admission/Kernel
incorporation. The next ordered source slice is the independent admission-decision signer. It must
own only the admission key, reverify the committed validation and DB admission challenge, and remain
unable to fill a slot or mutate the Kernel.

No target host is commissioned. Exact PostgreSQL read ACLs, Linux accounts/socket ownership,
systemd supervision, alerts and a fresh multi-process PostgreSQL kill/restart campaign remain
mandatory. This source and its tests are engineering evidence, not deployment proof or a scientific
result.

See [ADR 0071](architecture/0071-committed-validation-source-rpc-service.md), the
[PR-7m validator guide](PR7M_INDEPENDENT_F9_V2_VALIDATION_SERVICE.md), the
[PR-7e server guide](PR7E_EXTERNAL_RPC_SERVICE_RUNTIME.md), and the
[PR-5 controller guide](PR5_DURABLE_SCIENTIFIC_CONTROLLER.md).
