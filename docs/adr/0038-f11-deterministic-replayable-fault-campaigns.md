# ADR 0038: Deterministic, replayable fault campaigns

- Status: Accepted for the F11-S6 engineering boundary
- Date: 2026-08-18

## Context

F11-S1 through F11-S5 introduced a durable queue, transactional scientific commands, one-time
outward-action reconciliation, a reconstructible research graph, receipt-backed memory, and
shadow-only portfolio planning. Individual tests already exercised selected crashes. F11-S6 needs
one auditable campaign contract that spans those components and can answer a stronger question:
after a confirmed failure, did scientific state, cost state, event receipts, or outward authority
become lost, duplicated, or unknowably active?

Three failure semantics must not be collapsed:

1. a local database transaction can roll back and replay atomically;
2. a durable task is delivered at least once and may need lease recovery; and
3. an arbitrary remote effect cannot be made globally exactly-once by the local database.

A test that catches an exception proves only that an exception occurred. A mutable dashboard row or
an executor-authored `passed=true` would allow the component under test to grade itself.

## Decision

### 1. Freeze a complete ten-boundary manifest

Each campaign contains API process, worker process, database connection, evaluator, provider,
duplicate delivery, stale lease, archive storage, runtime identity, and outward-action scenarios.
The manifest binds harness code, environment manifest, seed, Quest scope, injection point, expected
outcome, required recovery actions, metric expectations, timeout, and scenario IDs. Its SHA-256
derives `fic_<32-hex>`.

### 2. Use deterministic seeded order, not ambient randomness

Execution order is sorted by the content hash of a versioned order schema, seed, and scenario ID.
It is stable across processes and does not rely on Python's randomized hash or hidden mutable RNG
state. The same campaign can therefore be rerun after the harness itself exits.

### 3. Executors return observations, never verdicts

One executor exercises each real boundary and returns typed observations, metrics, recovery actions,
timestamps, and evidence hashes. Executor exceptions abort the campaign. The independent evaluator
checks exact scenario coverage, outcome, recovery, evidence closure, timeout, and every comparator,
then derives scenario and campaign dispositions.

### 4. Require six exact-zero invariants everywhere

Every scenario, including infrastructure-only scenarios, must require exact zero scientific-state
loss, duplicate scientific state, duplicate budget charges, duplicate outward authorization,
unblocked remote ambiguity, and state/event mismatch. A scenario cannot omit or weaken them.
Boundary-specific metrics supplement these invariants.

### 5. Persist success and failure through the scientific outbox

The complete report is stored append-only in `fault_injection_campaigns` through
`resilience_fault_campaign.commit`. The report row, command result, and keyed event are one local
transaction. Database guards bind the insert to the applying command and verify principal, identity,
hashes, counts, Quest type, report fields, and timestamps. Every read re-evaluates embedded evidence
and verifies the command/event receipt. Failed and blocked reports remain evidence.

### 6. Retry local transactions completely; reconcile unknown remote outcomes

PostgreSQL documents that serialization failures require retrying the complete transaction,
including decision logic:
[PostgreSQL transaction failure handling](https://www.postgresql.org/docs/16/mvcc-serialization-failure-handling.html).
The harness therefore proves rollback followed by exact logical replay, not statement-only retry.

For remote calls, a timeout may hide a successful side effect. Safe automatic retry requires a
stable request identity honored by the provider; otherwise the one-time action remains
`reconciliation_required`. This matches the identity-based retry contract in
[AWS Builders' Library](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/).

### 7. Keep network toxic adapters optional

Deterministic CI network manipulation is useful; the official
[Toxiproxy](https://github.com/Shopify/toxiproxy) project provides latency, timeout, reset, and link
failure modes. F11-S6 leaves that as an executor adapter rather than adding a production dependency.
The accepted local suite directly exercises the actual repository boundaries.

### 8. A passing campaign is not activation authority

The audit can only state eligibility for F11-S7 endurance-gate review. It always returns
`autonomous_allocation_enabled=false`. No campaign result may enqueue portfolio actions, reserve or
charge budget, transition the graph, reveal a holdout, or mint an outward-action token.

## Consequences

- Campaigns are reproducible, content-bound, and independently regraded.
- Missing injection, recovery, metric, or evidence becomes blocked/failed rather than silently
  passing.
- An interrupted harness creates no partial mutable campaign receipt; the same manifest can rerun.
- Operators retain negative reliability evidence and can compare successive campaigns.
- The campaign report is larger because it embeds complete observations and invariant results.
- Real executors must measure their boundary state; a synthetic observation is suitable for contract
  testing but not production resilience evidence.
- F11-S7 can consume an immutable prerequisite instead of relying on logs or CI status alone.

## Rejected alternatives

### Treat any injected exception as a pass

Rejected because it does not prove recovery, exact replay, or absence of duplicate effects.

### Let scenario executors submit pass/fail

Rejected because the component under test would author its own verdict. Executors submit only
observations.

### Persist only passing campaigns

Rejected because it hides the reliability failure history and makes improvement claims
selection-biased.

### Keep a mutable running campaign row

Rejected because a process kill could leave ambiguous status and because later edits could rewrite
evidence. A complete immutable report is appended only after all observations exist.

### Claim exactly-once remote execution

Rejected because a provider can perform an effect while its response is lost. The safe state is
explicit reconciliation unless the provider honors the same idempotency identity.

### Automatically activate the F11-S5 portfolio after a passing campaign

Rejected because engineering fault recovery is neither a signed authorization nor 72-hour
scientific endurance evidence.
