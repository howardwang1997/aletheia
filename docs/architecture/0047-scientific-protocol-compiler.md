# Architecture decision 0047: A pure scientific-protocol compiler boundary

- Status: accepted
- Date: 2026-08-24
- Scope: PR-3 protocol, capability, and execution contracts

## Decision

Aletheia represents a proposed scientific action as an immutable, domain-independent
`ProtocolIR` before any execution authority can act on it. The PR-3 compiler is a pure projection:
it consumes exact protocol, capability-catalog, static-resource-catalog, and compiler identities
and, for closed well-formed contracts, returns either a canonical `WorkOrderDAG` or a canonical set
of typed blockers. Malformed or scope-escaped values fail model revalidation before compilation.
It does not
read configuration, a database, a filesystem, the network, a process table, a GPU inventory, or a
live provider. It does not authorize an action, reserve money or equipment, execute a node, admit
an observation, update a belief, or strengthen a claim.

The protocol reuses PR-2 authority rather than creating a second scientific namespace.
`ProtocolScope` contains the exact `ResearchScopeBinding`, its mechanically checked most-specific
scope node, a branch identity, a `ResearchQuestionVersion` object reference, and the committed
research-graph snapshot hash. Objective, design-space, method, observable, epistemic, claim, and
optional world-model objects all bind the derived graph-scope hash.

Scientific intent is not forced into one hypothesis-testing shape. `EpistemicContract` is a tagged
union for hypothesis discrimination, characterization, estimation, constraint testing, capability
qualification, formal derivation, and evidence synthesis. A `WorldModelSnapshotV2` is optional for
the other kinds. Hypothesis discrimination alone requires an exact world-model snapshot with at
least two selected alternatives and bidirectional, same-observable, same-measurement-protocol,
same-outcome-space distinguishing predictions. This preserves F9's useful falsifiability contract
without making every research action invent null, primary, and alternative hypotheses.

`CapabilityManifestV2` describes one atomic operation. A manifest closes its typed ports,
applicability, principal and independence requirements, frozen runtime, calibration, failure and
retry modes, safety audits, license/egress rules, qualification evidence, and claim ceiling.
Composition happens only in the protocol DAG. The compiler resolves an exact manifest pin or one
unique static-catalog match; zero or multiple matches fail closed. A full planner, adaptive
workflow, or legacy experiment driver cannot be registered as one capability and thereby become a
second controller.

The type checker enforces the structural gates from the target architecture, including:

- epistemic/world-model/observable consistency and graph-scope closure;
- specimen and entity identity lineage, controls, hidden confirmation/private/replication data,
  and pre-observation analysis/stopping/exclusion/multiplicity commitments;
- acyclic step dependencies and single-producer typed ports;
- exact or explicitly audited directional compatibility for schemas, units/ontologies, data
  classification, licenses, and egress;
- executor/parser/validator/approver independence;
- capability qualification; typed applicability, failure-mode, sample-floor, runtime, safety, and
  license/egress audit bindings; a calibration audit binding when calibration applies; sample
  bounds, retry semantics, and a structurally compatible static resource class;
- a claim ceiling and replication structure no stronger than the selected capabilities permit;
- content hashes for every caller-mutable design parameter.

PR-3 v1 can mechanically establish only preregistered exact reexecution. Because v1 has no
claim-to-step assignment that can exclude a branch, every `SCIENTIFIC_EXECUTOR` step/branch in the
protocol must declare at least two scientific slots. Multiplicity on parser, validator, control,
or analysis steps is not replication. PR-3 rejects
`INDEPENDENT_IMPLEMENTATION` and `EXTERNAL_INDEPENDENT` claim ceilings even when a protocol declares
many slots, because slot count cannot prove different implementations, principals, or sites. A
future explicit replication-assignment contract must bind those identities before either tier can
compile.

`CapabilityAuditBinding` makes that audit dependency mechanical. Each binding names one typed audit,
the exact capability-manifest and receipt hashes, audit-policy hash, auditor, passed conclusion,
and validity interval. The checker requires applicability, failure-mode, sample-floor, runtime,
safety, and license/egress bindings for every selected manifest, plus calibration when the manifest
requires it. The outer binding statically associates the exact final manifest with a receipt hash,
and that receipt hash must occur in the manifest's frozen qualification evidence. The compiler
does not claim that opaque receipt bytes recursively bind the final manifest: a future authenticated
receipt must instead attest a non-circular audit subject/material slice and policy identity. Audit
validity must begin no later than
qualification, qualification must occur no later than the complete manifest's `frozen_at`, and the
manifest must be frozen no later than protocol `authored_at`; an audit expiry must remain later than
protocol `authored_at`.

These checks do not make the domain-independent compiler the auditor: it does not prove
identifiability, statistical power, calibration quality, biosafety, or physical safety. PR-3 has no
trusted receipt registry or authorization clock, so it does not load the receipt bytes or establish
their signature, custody, revocation state, or freshness beyond the declared interval at protocol
freeze. Those checks belong at later kernel authorization and observation-admission boundaries.
The qualifier's declared principal ID must differ from the capability freezer and executor, and an
audit's declared principal ID must differ from the relevant protocol/capability authorities. The
implemented predicates establish only ID inequality: they do not authenticate those principals or
prove distinct groups, sites, organizations, credentials, or implementations.

An accepted compilation contains deterministic node identities and exact transitive bindings to
the protocol, selected manifests, static resource catalog, logical `command_sha256`, execution
parameters, environment, dependencies, resource envelopes, expected raw artifacts, contract,
observable and caller-parameter bindings, and preregistered replicate kind, count, seed hashes, and
site requirement. A `CompilationReceipt` binds the type-check report and either the work-order hash
or all blocker hashes. `verify_compilation` revalidates the closed models, recomputes the result,
and compares canonical bytes. The receipt is content-addressed compilation evidence, not a
signature or grant of execution authority.

PR-3 also freezes the minimum execution-side value boundary. A scientific replicate slot is
preregistered scientific identity; an infrastructure attempt is a recoverable engineering lineage
under that slot. Static resource classes describe shape, never current capacity. Execution
success, a raw artifact manifest, or an executor-reported positive/negative/inconclusive value is
not an admitted scientific observation. In PR-3, artifact verification and execution-receipt
interfaces are ports only. PR-4a later supplied qualification-only durable implementations without
turning them into scientific admission or a deployable runtime.

The PR-3 `WorkOrderDAG` to `ExecutionIntent` bridge is an explicit pure verification boundary.
Before any PR-4a reservation or later launch, `verify_execution_intent_binding` must prove exact node,
command, capability, resource, environment, expected-artifact, effect, and replicate bindings and
exactly one typed input-artifact receipt binding for every input port. Intermediate inputs also
bind their producer node and producer replicate slot. In v1 every intermediate edge has equal
producer/consumer slot counts and uses the preregistered ordinal mapping `i -> i`; `1 -> N`, `N -> 1`,
aggregation, and runtime slot selection fail closed pending an explicit assignment contract. The
verifier checks declared identities; it
does not fetch the receipt, rehash input bytes, or validate custody. Successful model construction
alone is not launch authority.

Similarly, `verify_execution_retry_binding` is mandatory before deriving a direct idempotent new
infrastructure attempt. The preceding receipt must contain the exact prior intent and a retryable engineering
failure after confirmed termination; the new attempt must bind that receipt, attempt, and failure
category, while every other intent field stays byte-identical. PR-4a later added retained
reconciliation and signed same-node adoption outside this helper. Checkpoint resume and external-
action reconciliation still require later specialized custody/state transitions and remain rejected
by the generic helper.
`READ_ONLY_EXTERNAL` uses an
external runtime, explicit action kind, and matching static external resource but is replay-safe
and declares no mutation provider receipt. Mutating idempotent external work needs provider
idempotency/reconciliation identity and one required provider receipt; a one-time external effect
permits exactly one infrastructure attempt and cannot be retried.

## Heterogeneous acceptance fixtures

One compiler must accept three structurally different fixture families:

1. a grouped regression protocol with explicit grouping, identity lineage, and preregistered
   analysis;
2. a structural intervention or simulation protocol with distinguishing predictions and explicit
   identity lineage;
3. an external-measurement protocol with hidden confirmation data, calibration, one-time
   physical/external side-effect semantics, and expected raw artifacts.

These fixtures exercise different epistemic and execution shapes. They are not aliases for a
materials benchmark and do not add `X`, `y`, groups, MAE, Materials Project, MatBench, or phonon
concepts to the kernel or compiler.

## Frozen v1 compatibility

F9/F10 compatibility is an isolated migration leaf, not an import dependency of the protocol
package. `F9V1WholeObjectBinding` hashes exact legacy bytes and preserves the legacy `run_id`; it
cannot refresh the source, create a v2 identity, or grant observation admission. An
`F10V1AtomicBundleBinding` preserves one complete legacy capability bundle as indivisible opaque
bytes; it cannot split the bundle into v2 operations or grant execution authority. Neither adapter
reads a live legacy row or registers a legacy executor.

## Consequences

- Model-generated free-form designs can be retained as proposals, but cannot silently become an
  executable or claim-bearing protocol.
- Canonical compilation is reproducible without a database or hardware and can be independently
  verified from frozen inputs.
- Busy nodes, current provider availability, leases, quotas, and budget balances are intentionally
  absent. A static match means “this shape is known,” not “this run can start.”
- No PR-3 object is persisted by the compiler. Authoritative protocol admission remains a later
  signed research-kernel command. PR-4a subsequently added qualification-only execution/artifact
  persistence, not Research Kernel launch authority or observation admission.
- An engineering-success receipt proves only that an executor produced and verified expected raw
  artifacts. Independent observation admission and claim gates remain mandatory.
- Repeated slots are not independent replication. PR-3 can compile exact reexecution only when
  every scientific-executor branch has at least two preregistered slots; stronger replication tiers
  remain blocked pending implementation/principal/site assignment contracts.
- The intent and retry verifiers are prerequisites for PR-4a qualification reservation and any
  future launch/retry path, but neither authorizes or performs an action or verifies input-receipt
  bytes and custody by itself.

## Deferred work

PR-4a now supplies a qualification-only slice of that boundary: signed single-node inventory,
atomic PostgreSQL budget/resource reservation, quarantine and central artifact rehash, durable
attempt/receipt persistence, fencing, retained reconciliation, signed same-node adoption, and an
injected node fault facade. It is not a deployable execution service. PR-4b must compose the
deployment-pinned quote/source-budget adapters, concrete runtime, allocator, artifact workflow, and
terminal committer and prove real isolation/recovery. Checkpoint resume and external-action
reconciliation require later dedicated contracts. PR-3 must not itself be used to launch the local
machine, the two V100 servers, the 2060 server, a paid external service, or a physical instrument;
remote and multi-site scheduling follows only after the composed local receipt path is proven.
