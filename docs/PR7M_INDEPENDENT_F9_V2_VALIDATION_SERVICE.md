# PR-7m independent F9-v2 validation service

- Status: source composition complete; target-host commissioning pending
- Date: 2026-08-27

## What is now runnable

The PR-7e server can load
`aletheia.research_controller_f9_v2_validation_runtime.build_f9_v2_validation_rpc_service`
for exactly two operations: `prepare_validation_campaign` and `issue_validation_receipt`. The
process owns one observation-validator Ed25519 domain key in a source-external, single-link `0400`
file. Its RPC receipt key is separate. Database-attestation, admission, execution and Kernel private
keys are rejected by the configuration and never enter the service.

Campaign preparation replays the exact Kernel action, complete PR-4 qualification/run lineage and
fresh artifact CAS custody before assessment. The signed request remains graph-scoped: it binds the
authorized snapshot, Protocol IR, F9-v2 world model and predictions, scientific slot, artifact,
namespace and pre-observation commitments. Publication has one immutable first-writer-wins file per
raw run. Receipt issuance then reopens and rehashes that file and verifies the DB-signed issuance
challenge using only the database public pin.

## Conservative assessment baseline

The checked-in `ExactContentF9V2ObservationAssessor` is intentionally narrower than a general
scientific validator. A frozen catalog maps one complete action/protocol/world-model/slot/schema and
freshly rehashed content digest to one pre-reviewed outcome bin or scientific rejection. Unknown or
rebound content becomes `f9-v2:unrecognized-exact-content` with `blocked_execution`; it is never
guessed into positive, negative or inconclusive evidence. The catalog and assessor source bytes are
both pinned, and only outcome bins already present in the signed admission policy are accepted.

This baseline is useful for qualified deterministic fixtures and known-answer campaigns. It is not
a claim that arbitrary domain observations can be interpreted by hash lookup. A general
domain-specific assessor remains a separate reviewed implementation and deployment concern.

## Custody and verification

The guarded factory pins the database URL/schema revision, read-only Kernel CAS, all public bridge
and PR-4 authorities, exact run-lineage/artifact readers, service and assessor source bytes,
validator key bytes, and a process-owned `0700` write-once campaign root. Those roots, the socket,
configuration, receipt key and reviewed source must be disjoint. The factory reopens both source
files and the validator key after composition and exposes no execution, database, admission or
Kernel mutation method.

Focused tests cover exact-content matching and unknown-content blocking, scientific-rejection
preservation, catalog/source rebinding, the two-operation RPC partition, end-to-end typed RPC
round-trips, private-key separation, public-only Kernel/PR-4/artifact composition, writable archive
identity and guarded factory loading.

## Remaining release gates

Three PR-7e concrete factories remain: committed-validation source, independent admission, and
atomic admission/Kernel incorporation. The next ordered source slice is the committed-validation
source. It must be keyless and return only the fully reverified durable validation for the exact
Quest/action/scientific slot.

No target host is commissioned. Exact PostgreSQL read ACLs, Linux accounts/socket/key ownership,
systemd supervision, alerts, key rotation/revocation, a general reviewed assessor and a fresh
multi-process PostgreSQL kill/restart campaign remain mandatory. These source and test receipts are
engineering evidence, not deployment proof or a scientific result.

See [ADR 0070](architecture/0070-independent-f9-v2-validation-rpc-service.md), the
[PR-7l database-observation guide](PR7L_DATABASE_OBSERVATION_SERVICE.md), the
[PR-7e server guide](PR7E_EXTERNAL_RPC_SERVICE_RUNTIME.md), and the
[PR-5 controller guide](PR5_DURABLE_SCIENTIFIC_CONTROLLER.md).
