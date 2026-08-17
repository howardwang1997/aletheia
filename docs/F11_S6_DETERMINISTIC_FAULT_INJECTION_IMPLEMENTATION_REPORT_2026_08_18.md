# F11-S6 deterministic fault-injection implementation report

Date: 2026-08-18

## Outcome

F11-S6 is engineering-complete. Aletheia can now freeze a complete failure matrix, execute real
durable boundaries in deterministic seeded order, independently derive recovery invariants, retain
passing/failed/blocked evidence in an append-only PostgreSQL ledger, and audit the newest
Quest-scoped campaign before F11-S7.

The accepted real campaign confirms all ten injections and finishes with exact zero for:

- lost scientific state;
- duplicate scientific state;
- duplicate budget charge;
- duplicate outward authorization;
- unresolved remote ambiguity without a blocking state; and
- scientific state/event mismatch.

This does not activate autonomous allocation. Every audit reports
`autonomous_allocation_enabled=false`.

## Related-work decisions

The implementation follows three primary operational sources:

- PostgreSQL requires serialization-related retry around the complete transaction and its decision
  logic, not only the last statement:
  [PostgreSQL transaction failure handling](https://www.postgresql.org/docs/16/mvcc-serialization-failure-handling.html).
- Unknown remote outcomes are retry-safe only when the remote contract honors a stable request
  identity; otherwise reconciliation is required:
  [Making retries safe with idempotent APIs](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/).
- Deterministic network manipulation belongs in test/CI adapters; Toxiproxy supplies relevant
  latency, timeout, reset, and down modes without needing to become an Aletheia production runtime
  dependency: [Shopify Toxiproxy](https://github.com/Shopify/toxiproxy).

These sources informed the split between complete local transaction replay, lease-based task
recovery, and explicit outward reconciliation.

## Frozen contracts and independent evaluation

`aletheia/jobs/fault_schemas.py` adds frozen models for:

- ten `FaultBoundary` values and their typed injection outcomes;
- required recovery actions;
- comparator-bound invariant expectations and evidence-backed metric observations;
- complete campaign manifests and content-derived `fic_<32-hex>` identities;
- scenario results and campaign reports with derived dispositions/counts/self-hash;
- commit receipts, reconstructed snapshots, and Quest audit; and
- the hard-coded non-activation field.

Every scenario must contain all six core metrics with comparator `exact` and expected value zero.
The model rejects an incomplete ten-boundary manifest, repeated identities/metrics, a changed
campaign/report hash, noncanonical members, impossible timestamps, and caller-authored aggregate
counts.

`aletheia/jobs/fault_injection.py` supplies the independent harness:

1. `fault_campaign_order` creates a stable hash permutation from the frozen seed and IDs;
2. `run_fault_campaign` requires an exact executor matrix and lets executor errors escape;
3. `evaluate_fault_scenario` checks confirmed injection, outcome, recovery actions, complete metric
   set, evidence closure, timeout, and all comparators;
4. `evaluate_fault_campaign` derives the complete verdict and six aggregate totals; and
5. `validate_fault_campaign_report` repeats the computation during persistence and every read.

An executor cannot submit a verdict. A caught exception is insufficient: the executor must return
measured post-recovery state and evidence hashes.

## Real ten-boundary acceptance campaign

`tests/jobs/test_fault_injection.py` runs one complete campaign against the real repository
boundaries:

| Boundary | Injection | Measured recovery |
| --- | --- | --- |
| API process | child enqueues, prints receipt, then `os._exit(51)` | parent replays exact request; one task and one keyed enqueue event |
| worker process | child claims lease, then `os._exit(52)` | expired attempt recovered, replacement succeeds, killed owner's callback rejected |
| database connection | failure after event write but before command receipt | transaction leaves no state/command/event, reconnect then exact replay yields one of each |
| evaluator | handler raises `TimeoutError` | first attempt is infrastructure failure; second succeeds |
| provider | handler raises `InfrastructureTaskFailure` | frozen retry policy creates one retry and one success |
| duplicate delivery | same scientific command delivered twice | callback runs once; one state row, command, event, and replayed receipt |
| stale lease | active lease is expired deliberately | replacement attempt succeeds; stale completion is rejected |
| archive storage | archive store raises `OSError(ENOSPC)` | source negative-result fact remains, with zero compactions and zero orphan archive files |
| runtime identity | completion presents a changed worker manifest hash | forged callback is rejected without mutation; original owner completes |
| outward action | claimed provider request expires with unknown remote result | one authorization, no receipt, no second token, explicit `reconciliation_required` |

The campaign order is seed-dependent, so the test cannot rely on a convenient fixed sequence. Every
executor uses a unique durable identity and returns the exact metric set frozen in its scenario.

## Durable evidence ledger

Alembic revision `20260818_0021` adds `fault_injection_campaigns` on top of the single
`20260818_0020` head. The table stores the full report and queryable:

- campaign/Quest identity;
- manifest and report hashes;
- campaign disposition and scenario counts;
- all six core aggregates;
- completion and commit timestamps; and
- scientific command/principal binding.

The ORM and migration schemas compare with zero differences. PostgreSQL checks enforce count and
verdict consistency. A before-insert trigger accepts rows only inside an applying
`resilience_fault_campaign.commit` command and binds command input, aggregate, principal, hashes,
counts, report JSON, timestamps, and Quest node type. Another trigger rejects update/delete.

`FaultCampaignStore.commit` writes the report, command result, and keyed event in one transaction.
Exact command replay returns the original receipt without inserting a second campaign. A changed
logical identity conflicts. `get`, `list`, and `audit` fully reconstruct and regrade each report,
then verify its relational columns and command/event receipt.

Failed and blocked reports are deliberately retained. The Quest audit is eligible for endurance
review only when the latest campaign passes; it never grants execution authority.

## Operator interface

`scripts/fault_campaign.py` provides:

- `evaluate MANIFEST OBSERVATIONS [--output REPORT]`;
- `commit REPORT --idempotency-key ... --principal ...`;
- `show fic_<32-hex>`;
- `list [--quest-id qst_<32-hex>]`; and
- `audit qst_<32-hex>`.

Evaluation is read-only. Persistence occurs only under the explicit `commit` command.

## Acceptance evidence

The focused F11-S6 suite passes:

~~~text
7 passed in 3.26s
~~~

It covers contract rejection, deterministic order, independent regrading, exact persistence replay,
failed-report retention, append-only triggers, migration/ORM parity, and the complete real
ten-boundary campaign above.

The final validation matrix also passes:

~~~text
F11-S6 + queue/outbox/memory/portfolio/migration regression:
63 passed in 8.46s

full non-Docker repository regression:
1300 passed, 2 skipped, 29 deselected, 2611 warnings in 772.97s

Alembic current/head: 20260818_0021
ORM schema diff count: 0
git diff --check: passed
F11-S6 changed-file Ruff: passed
fault_campaign.py CLI smoke: passed
~~~

The 2,611 warnings are the existing `spglib` deprecation warnings in the materials-domain tests.
Repository-wide Ruff also exposes 20 pre-existing violations in unrelated exploratory probe scripts;
those clean tracked files were not changed as part of F11-S6. Every Python file changed or added by
this slice is Ruff-clean.

## Changed implementation surface

- `aletheia/jobs/{fault_schemas,fault_injection,fault_campaign}.py`;
- `aletheia/jobs/{__init__,outbox,persistence}.py`;
- `aletheia/schema_migrations.py`;
- `migrations/versions/20260818_0021_f11_fault_injection_campaigns.py`;
- `scripts/fault_campaign.py`;
- `tests/jobs/test_fault_injection.py`;
- `docs/jobs/FAULT_INJECTION_CAMPAIGNS.md`;
- `docs/adr/0038-f11-deterministic-replayable-fault-campaigns.md`; and
- master plan, operator docs, and documentation index status updates.

## Honest boundary and next work

F11-S6 demonstrates the frozen engineering invariants for the accepted PostgreSQL/local-process
environment. It does not prove resilience against every kernel, distributed-database, network,
provider, or laboratory failure. It does not verify the scientific correctness of portfolio scores,
prove a structural pivot, or establish a 72-hour run.

F11-S7 is next: run one frozen Quest for 72 hours with at least two research questions, three
campaign branches, one preserved negative result, one reproduction, injected process/provider
interruptions, an auditable structural pivot where warranted, and a final reconstructible portfolio
report. Activation remains a separate signed/IAM-authorized decision after that evidence.
