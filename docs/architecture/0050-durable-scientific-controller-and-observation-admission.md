# Architecture decision 0050: Durable scientific controller and atomic observation admission

- Status: accepted for the PR-5 local source/test vertical cut
- Date: 2026-08-25
- Scope: Research Kernel observation incorporation, scientific bridge, durable controller, and
  restart recovery

## Decision

The Research Kernel event stream remains the only scientific state authority. PR-5 adds
`observation_incorporated` as an additive v1 event whose reducer can move only the exact authorized
action on the exact branch to `APPLIED`. It retains a positive, negative, or inconclusive
`EvidenceRef` keyed by the scientific slot. A second observation for that slot is rejected both by
the reducer and by PostgreSQL uniqueness.

The new optional observation field on `ActionSnapshot` is omitted from canonical JSON when absent.
Existing PR-2 snapshots therefore retain their bytes and hashes; golden replay tests protect that
compatibility. The event-store verifier resolves the action from the audited state and CAS before
accepting the command.

Scientific execution requires a separately signed authorization that binds the adjacent proposed
and authorized action events, authorized snapshot, graph-scoped protocol, canonical work order,
qualification bundle and grant, node placement, attempt, artifact contract, and one preregistered
scientific slot. This authorization must be durably registered before PR-4 admission, reservation,
or launch. Engineering completion and raw artifact existence are never admission authority.

Validation and admission use distinct principals, keys, policies, database-issued challenges, and
committed receipts. The final admission coordinator asks an external ordinary Kernel authority to
sign the exact incorporation proposal, then commits the admission row and normal Kernel
event/snapshot/outbox/head mutation in one PostgreSQL transaction. Deferred bidirectional triggers
reject orphan admissions and orphan incorporation events.

## Controller decision

Controller registrations and deliveries are operational projections only. A deployment-pinned
manifest fixes code, policy, capability catalog, protocol registry, bridge policy, worker identity,
and retry policy. Callers may choose only an idempotency key and an exact existing Quest head.

The task type is `research.controller.v1`; it never carries a legacy Run identity. Each immutable
wakeup maps to an append-only, generation-bounded task chain with a per-Quest concurrency key.
Kernel and execution outboxes are delivered transactionally with their delivery and generation-zero
attempt receipts. A reconciler appends a successor after a failed task or after a successful
internal step that committed no new authoritative source; typed terminal resolutions prevent old
settled work from starving newer deliveries. The stateless service performs one typed step per
leased task and rebuilds its plan from a full Kernel audit plus append-only compilation,
execution-authorization, validation, admission, and continuation receipts.

There is no controller-owned scientific checkpoint and no generic retry interpretation of an
unknown execution. Terminal recovery must resolve the exact PR-4 execution/attempt authority.
Continuation is a pure graph-scoped F9-v2 projection: it can propose a typed follow-up, refinement,
or fork, but a separately authorized Kernel command must commit the transition.

The controller imports authority-neutral durable-task contracts and a caller-owned queue port,
not the legacy `jobs` package or its event bus. Concrete v1 worker composition stays outside the
protected package. Likewise, the write-once F9-v1 graph-binding adapter is a migration-only outer
compatibility module: it may rehash a frozen legacy campaign and append its immutable binding CAS,
but it is not F9-v2 validation or observation-admission authority.

## Consequences

- A valid negative or inconclusive result can become durable scientific evidence without being
  confused with process failure, timeout, or invalid output.
- A process crash cannot leave a durable admission without its Kernel event, or vice versa.
- Duplicate outbox delivery, lease expiry, terminal task failure, and controller restart are
  recoverable without a second scientific commit or an unbounded in-place retry.
- The controller cannot self-authorize a command, self-admit an observation, invoke legacy
  `ExperimentDriver`, or create a shadow research ledger.
- The local vertical cut demonstrates control-flow correctness, not discovery, production
  readiness, or target-host security.

Deployment remains a separate decision. PR-4's exact target-host campaign and deployment-owned
controller/validator/dispatcher/worker/reconciler composition must pass before remote or unattended
science.
See [the PR-5 operator guide](../PR5_DURABLE_SCIENTIFIC_CONTROLLER.md) for the concrete custody
chain, schema, recovery behavior, and remaining gates.
