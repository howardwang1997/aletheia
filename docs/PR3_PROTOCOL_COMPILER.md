# PR-3 scientific protocol compiler

PR-3 freezes the domain-independent contract between a committed research question and a future
execution fabric. It can now represent, structurally check, and canonically compile a scientific
protocol. It cannot yet run that protocol.

## What is implemented

The pure contract surface is split by responsibility:

- `aletheia.protocols.base` binds every protocol to the exact PR-2 Quest/program/campaign scope,
  branch, question reference, and graph snapshot.
- `aletheia.protocols.world_models` provides graph-scoped F9 v2 hypothesis, assumption, prediction,
  belief, and snapshot values. Competing predictions are required only for an explicit hypothesis-
  discrimination contract.
- `aletheia.protocols.claim_contracts` defines operationalized observables, per-kind claim ceilings,
  replication requirements, and the seven-kind `EpistemicContract` union.
- `aletheia.protocols.capabilities` defines atomic `CapabilityManifestV2` values and an immutable
  in-memory catalog.
- `aletheia.protocols.schemas` defines objective, design-space, method, data/control/analysis,
  protocol-step, check-report, work-order, and compilation-receipt values.
- `aletheia.protocols.typecheck` returns all deterministic structural blockers it can establish
  from frozen inputs.
- `aletheia.protocols.compiler` produces and verifies the canonical result.
- `aletheia.execution.schemas` and `aletheia.execution.ports` define static resource, intent,
  replicate/attempt, raw artifact, verification, and receipt boundaries for PR-4 implementations.
- `aletheia.migration.protocol_v1_compatibility` creates opaque, read-only, content-addressed F9/F10
  v1 bindings without importing live legacy models.

All models are frozen and reject unknown fields. Collections that represent sets are canonically
ordered, datetimes must be UTC, and substantive objects expose content identities derived from
canonical JSON.

## Compilation flow

```text
committed PR-2 question + graph snapshot
  -> graph-scoped ProtocolIR
  -> ProtocolCheckRequest(protocol, capability catalog, static resource catalog)
  -> ProtocolCheckReport
       accepted -> exact/unique capability resolution -> canonical WorkOrderDAG
       rejected -> canonical typed blockers -> no WorkOrderDAG
  -> CompilationReceipt
  -> independent verify_compilation(request, result)
```

The public entry points are:

```python
from aletheia.protocols.compiler import (
    ProtocolCompilationRequest,
    compile_protocol,
    verify_compilation,
    verify_execution_intent_binding,
)
from aletheia.execution.schemas import verify_execution_retry_binding
from aletheia.protocols.typecheck import ProtocolCheckRequest, typecheck_protocol
```

Callers must supply exact immutable catalogs. A selector may pin a manifest hash, or it must have
exactly one match for its operation/capability/version constraints. Ambiguity is a blocker, not a
ranking problem delegated to the compiler.

## Reading a result

An accepted report means only that the frozen values are structurally compatible with at least one
declared static resource class. It does not establish any of the following:

- that a CPU, V100, 2060, provider, site, or instrument is presently reachable or free;
- that budget, credentials, safety authorization, or a lease has been reserved;
- that the adapter bytes match a deployed runtime beyond the supplied frozen digest;
- that referenced audit/qualification/calibration evidence is authentic, unrevoked, or currently
  fresh outside the static identities present in the supplied contracts;
- that an input-artifact receipt exists, that its bytes hash to the declared identity, or that its
  custody chain is valid;
- that any work ran, any artifacts arrived, or an observation is valid;
- that a positive, negative, or inconclusive scientific conclusion has been admitted.

A rejected result contains no work order. Its receipt binds the complete sorted blocker set, so a
caller cannot omit an inconvenient error when presenting the compilation. A result should be
accepted by another component only after `verify_compilation` recomputes the canonical bytes. A
`CompilationReceipt` is content-addressed but unsigned and carries no launch authority.

## Structural hard gates

Closed-model invariants fail during Pydantic validation before compilation: examples include an
unknown field, noncanonical collection, non-UTC time, wrong-Quest question reference, or an object
that escapes the protocol graph scope. For a well-formed request, the checker reports canonical
typed blockers rather than editing the protocol. The combined fail-closed coverage includes:

| Boundary | Examples of fail-closed outcomes |
|---|---|
| graph and epistemic scope | wrong question/Quest, world-model mismatch, missing distinguishing prediction |
| observability | missing observable, unqualified measurement manifest, uncovered calibration |
| design integrity | open identity lineage, missing empty/leakage/degeneracy/drift controls |
| pre-observation discipline | visible confirmation/private/replication data, incomplete analysis freeze |
| graph/ports | cycle, unknown dependency, missing or multiply produced port |
| compatibility | schema, unit/ontology, classification, license, or egress mismatch |
| role separation | executor/parser/validator/approver independence conflict |
| capability | unavailable/ambiguous/unqualified/inapplicable manifest; missing, stale, or rebound typed audit |
| execution shape | unsupported resource/checkpoint/retry/sample envelope |
| claims | claim type/strength or replication structure above the allowed ceiling |
| caller mutability | design parameter absent from the exact parameter manifest hash |

Where equality is insufficient, `CompatibilityAuditReceipt` is directional: a receipt for
`source -> target` cannot be reused for `target -> source`. It binds the dimension, both exact
identities, the audit policy, evidence, auditor, and time.

Capability audits use a separate `CapabilityAuditBinding`. Every capability requirement must carry
one canonical binding for each of applicability, failure modes, sample floor, runtime, safety, and
license/egress; a calibrated manifest must additionally carry calibration. Each binding records the
audit kind, exact manifest and receipt hashes, audit-policy hash, auditor, `passed` conclusion, and
validity interval. The outer binding statically associates the exact final manifest and opaque
receipt hash, and the checker requires that receipt hash to be part of the selected manifest's
frozen qualification evidence. PR-3 does not claim that the opaque receipt recursively hashes the
final manifest; a future authenticated receipt must attest a non-circular audit subject/material
slice and policy identity. Both the audit interval and manifest qualification must cover the
protocol's `authored_at`. The complete manifest is frozen only after qualification. The enforced
order requires `audit.valid_from <= qualification.qualified_at` and
`qualification.qualified_at <= manifest.frozen_at <= protocol.authored_at`; an audit expiry, when
present, must remain later than `protocol.authored_at`.

This is static referential and temporal closure, not audit authentication. PR-3 does not retrieve
the referenced receipt, verify a signature or custody chain, consult a revocation registry, or use
the current wall clock. Later kernel authorization/admission must establish those facts; an
accepted compilation proves only that the supplied frozen contracts close at protocol-freeze time.
The qualifier's declared principal ID must differ from the capability freezer and executor, and an
audit's declared principal ID must differ from the relevant protocol/capability authorities. These
checks prove only ID inequality: they do not authenticate the principals or establish different
groups, sites, organizations, credentials, or implementations.

Replication is deliberately conservative. Because v1 has no claim-to-step assignment that can
exclude a branch, PR-3 may establish `EXACT_REEXECUTION` only when at least two slots are
preregistered on every `SCIENTIFIC_EXECUTOR` step/branch in the protocol. Parser, validator,
control, or analysis multiplicity cannot satisfy this rule. PR-3 always blocks
`INDEPENDENT_IMPLEMENTATION` and `EXTERNAL_INDEPENDENT`: summing slots or sequential DAG nodes does
not prove implementation, principal, or site independence. Those tiers require a future explicit
replication-assignment contract.

## Execution contracts and retry identity

Each accepted `WorkOrderNode` projects a deterministic node identity/hash, logical
`command_sha256`, selected capability identity and static-resource constraints, exact environment
and execution-parameter hashes, typed ports, expected artifacts, contract/observable/caller-
parameter bindings, operation batch size, and the preregistered replicate kind, slot count, seed
hashes, and site requirement. The DAG separately binds the static resource-catalog identity. This
command hash describes the logical adapter command; it is not shell code or launch authority.

Before PR-4a may reserve an `ExecutionIntent`, or any later component may launch one, it must call
`verify_execution_intent_binding(work_order, intent)`. This pure fail-closed check requires every
frozen node/command/resource/artifact/replicate field to be exact and requires one typed
`InputArtifactBinding` per input port. A protocol input declares a verification-receipt hash; an
intermediate input additionally binds the exact producer node and producer replicate slot. The v1
graph permits only equal producer/consumer slot counts and the preregistered ordinal mapping
`i -> i`; `1 -> N`, `N -> 1`, aggregation, and runtime selection among producer slots fail closed
until an explicit assignment contract exists. The function checks these identities only: PR-3 does
not load the receipt, rehash the input bytes, or
validate its custody. Constructing a Pydantic `ExecutionIntent` successfully is therefore not
permission to launch it.

Infrastructure retries receive distinct attempt identities and must never masquerade as extra
scientific replicates; multiple slots must never masquerade as independent-implementation or
external-site replication. Before a direct idempotent new attempt,
`verify_execution_retry_binding(previous_intent,
current_intent, previous_receipt)` requires a receipt containing the exact prior intent, a terminal
engineering failure explicitly marked retryable after confirmed termination, and exact next-
attempt lineage. All fields other than `infrastructure_attempt` remain byte-identical. Retry and
reconciliation behavior is also bounded by the selected capability manifest and requested resource
envelope. The generic helper rejects reconciliation and checkpoint-resume modes. PR-4a now adds a
qualification-only retained-reconciliation state plus signed same-node adoption and terminal
recovery outside that helper; checkpoint resume remains disabled until a later dedicated custody
and state-transition contract exists.

External and physical effects are conservative. `READ_ONLY_EXTERNAL` still requires an external
runtime, an explicit action kind, and a matching static external resource, while its effect class is
replay-safe and it declares no mutation provider-receipt artifact. Replay-safe work can be retried
only within its declared failure contract. A mutating idempotent external action requires provider
idempotency and reconciliation identities plus one required provider-receipt artifact. A one-time
or ambiguous external effect permits exactly one infrastructure attempt and cannot be retried or
automatically reissued after a lost response. Its terminal state is reconciliation-required until
external evidence resolves what occurred.

`ExecutionReceipt` describes an engineering terminal state. Even an engineering-success receipt
and verified raw artifacts have no scientific admission authority. Parser/validator/admission
components must later issue an independent observation receipt before the research kernel may
change belief or claims.

## Heterogeneous fixtures

The acceptance suite uses three fixture categories through the same public compiler:

- grouped regression, exercising explicit grouping, preregistered analysis, controlled identity-
  preserving data flow, and deterministic/computational capabilities;
- structural intervention/simulation, exercising a multi-step DAG, discriminating world-model
  predictions, structural identity lineage, and simulation/intervention outputs;
- external measurement, exercising hidden confirmation roles, measurement calibration, a one-time
  external/physical side-effect, and expected raw-artifact structure.

They deliberately differ in epistemic contract, ports, DAG topology, and runtime class. The shared
compiler contains no material, MatBench, phonon, fixed metric, or global `X/y/groups` special case.

## Legacy compatibility

The migration adapter accepts exact bytes already retrieved from frozen legacy custody:

```python
from aletheia.migration.protocol_v1_compatibility import (
    bind_f10_v1_atomic_bundle,
    bind_f9_v1_whole_object,
)
```

The F9 adapter preserves a legacy run scope as one opaque whole object. The F10 adapter preserves
one opaque capability bundle as an indivisible unit. These bindings are evidence identities only:
they cannot read or refresh legacy state, create v2 protocol/capability objects, grant execution or
observation admission, split an F10 bundle, or register the legacy driver with the compiler.

## Verification

Run the PR-3 focused contract and dependency boundary suites with the repository Conda environment:

```bash
conda run -n aletheia pytest -q \
  tests/protocols \
  tests/execution \
  tests/migration/test_protocol_v1_compatibility.py \
  tests/migration/test_pr3_dependency_boundary.py \
  tests/migration/test_legacy_write_inventory.py
```

Run lint on only the PR-3 Python surface; the repository-wide lint command includes historical
probe-script debt outside this slice:

```bash
conda run -n aletheia ruff check \
  aletheia/protocols aletheia/execution \
  aletheia/migration/protocol_v1_compatibility.py \
  tests/protocols tests/execution \
  tests/migration/test_protocol_v1_compatibility.py \
  tests/migration/test_pr3_dependency_boundary.py
```

The authoritative acceptance counts belong in the commit/PR evidence after a clean run. This guide
does not convert an in-progress or partial count into a release claim.

## What follows the PR-3 pure boundary

PR-4a now implements a qualification-only foundation: deployment-signed engineering grants,
enrolled single-node inventory, constructor-supplied authority/custody resolvers, atomic PostgreSQL
resource and budget reservation, fenced attempts and same-node adoption, local quarantine/CAS
rehash, signed node and terminal evidence, transactional terminal receipt/outbox settlement, and an
injected node fault facade.

PR-4b now adds concrete read-only quote/source-budget registries, sealed assignment delivery, the
PostgreSQL allocator-to-node adapter/worker, exact input staging, a CPU-only OCI runtime, loop-backed
output quota, a launch gate, watchdog, and runtime-v2 terminal settlement. It still has no HTTP
launch path or Research Kernel launch authorization, and exact target-host
Linux/root/systemd/loop/Docker qualification remains a deployment gate. Checkpoint/external-action
paths still require separate later contracts. PR-5 must add the signed scientific
action-to-execution bridge. Neither a protocol compilation nor a PR-4 qualification value alone is
permission to start an unregistered process, allocate either V100 server, use the 2060 node, spend
money, call an external measurement provider, or operate a physical instrument.
