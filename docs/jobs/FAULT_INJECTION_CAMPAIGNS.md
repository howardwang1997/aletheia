# Deterministic fault-injection campaigns

F11-S6 exercises the existing durable queue, scientific outbox, memory archive, runtime identity,
and one-time outward-action boundaries under deliberate failure. It records engineering recovery
evidence; it does not infer scientific truth and does not grant autonomous allocation authority.

## Guarantees

Every valid manifest covers all ten boundaries below. Every scenario must require exact zero for
the same six core invariants, even when it also checks boundary-specific counts.

| Boundary | Required observed outcome | Typical safe recovery |
| --- | --- | --- |
| API process | `process_exit` | replay the exact content-bound request |
| worker process | `process_exit` | reclaim expired lease; reject stale callback |
| database connection | `connection_lost` | reconnect; retry the complete logical transaction |
| evaluator | `timeout` | classify as infrastructure and use the frozen retry policy |
| provider | `unavailable` | classify as infrastructure and use the frozen retry policy |
| duplicate delivery | `duplicate_delivered` | return the original command/event receipt |
| stale lease | `lease_expired` | reclaim; reject the old owner |
| archive storage | `storage_exhausted` | preserve source facts; verify there is no partial ledger commit |
| runtime identity | `identity_mismatch` | reject the callback without mutation |
| outward action | `ambiguous_remote_result` | require reconciliation; never issue another raw token |

The mandatory zero metrics are:

1. scientific state lost;
2. scientific state duplicated;
3. budget charges duplicated;
4. outward authorizations duplicated;
5. remote ambiguity left unblocked; and
6. state/event receipts disagreeing.

A campaign cannot weaken or omit these expectations. Boundary-specific metrics may additionally
require one committed command/event, two task attempts, one recovered task, zero orphan archives,
or one reconciliation-required action.

## Execution contract

Create a frozen `FaultCampaignManifest`, then provide one executor per manifested `scenario_id`:

~~~python
from aletheia.jobs import run_fault_campaign

report = run_fault_campaign(
    manifest,
    {
        "f11s6.api_process": exercise_api_process_exit,
        # ...one executor for every other manifested scenario...
    },
)
~~~

The order is a deterministic hash permutation of `seed + scenario_id`. The manifest and scenario
IDs remain content-addressed, so an interrupted harness can rerun the same campaign. Python's
process-randomized `hash()` and version-dependent pseudorandom state are not used.

An executor must exercise the real boundary and return `FaultScenarioObservation` containing:

- the actually observed outcome and confirmation that injection occurred;
- the recovery actions that actually occurred;
- every expected metric exactly once;
- hashes of the evidence used for each metric and a diagnostic hash; and
- aware start/completion timestamps inside the scenario timeout.

Executor exceptions escape the harness. Merely catching an exception is never converted into a
passing or blocked observation. `evaluate_fault_scenario` independently derives invariant results,
missing evidence/recovery blockers, and disposition. `evaluate_fault_campaign` requires exactly one
observation for every manifested scenario and derives all counts, core totals, disposition, and the
self-hashed report. `validate_fault_campaign_report` repeats that computation during every read.

## Transaction and remote-effect semantics

For a PostgreSQL transaction failure, retry the complete logical transaction, including the
application decision that produced its statements. Retrying only the last statement can operate on
a different state than the original decision. This follows PostgreSQL's official guidance for
serialization failures and related full-transaction retry cases:
[PostgreSQL transaction failure handling](https://www.postgresql.org/docs/16/mvcc-serialization-failure-handling.html).

An outward call has a different boundary. A timeout can mean that the remote effect happened and
only the response was lost. Automatic retry is safe only when the remote API honors the same stable
idempotency identity. Otherwise Aletheia retains one local authorization and moves the action to
`reconciliation_required`, consistent with the request-identity contract described in
[Making retries safe with idempotent APIs](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/).

The built-in acceptance suite injects local transaction and process faults directly. Network proxy
faults can later use a deterministic CI adapter such as
[Shopify Toxiproxy](https://github.com/Shopify/toxiproxy); it is not a production runtime dependency
of this slice.

## Persistence

Apply the migration and verify the single head:

~~~bash
conda run -n aletheia alembic upgrade head
conda run -n aletheia alembic current
~~~

The fault-campaign migration is `20260818_0021`; the repository head is now `20260818_0022` after
the F11-S7 endurance ledger. `fault_injection_campaigns` stores the complete report plus queryable
hashes, verdict counts, six core totals, Quest scope, and scientific command binding.

Commit a recomputed report using `FaultCampaignStore.commit`. The report row, scientific command
result, and keyed event commit in one PostgreSQL transaction. A database insert guard requires the
command to be in `applying` state and verifies its identity, principal, hashes, counts, timestamps,
Quest type, and report JSON bindings. Update/delete triggers make reports append-only. Both passing
and failed/blocked reports are retained; editing a historical failure into success is impossible.

Every `get`, `list`, and `audit` operation:

1. validates the frozen schemas;
2. reruns all scenario comparisons from embedded observations;
3. recomputes the campaign report/hash;
4. verifies relational columns; and
5. verifies the scientific command and keyed event receipt.

## CLI

Recompute a report from JSON without writing the database:

~~~bash
conda run -n aletheia python scripts/fault_campaign.py evaluate \
  manifest.json observations.json --output report.json
~~~

Commit only after the report has been inspected:

~~~bash
conda run -n aletheia python scripts/fault_campaign.py commit report.json \
  --idempotency-key fault-campaign:<stable-logical-id> \
  --principal harness:f11s6
~~~

Reconstruct or audit retained evidence:

~~~bash
conda run -n aletheia python scripts/fault_campaign.py show fic_<32-hex>
conda run -n aletheia python scripts/fault_campaign.py list --quest-id qst_<32-hex>
conda run -n aletheia python scripts/fault_campaign.py audit qst_<32-hex>
~~~

`audit` is eligible for F11-S7 review only when the latest Quest-scoped campaign is fully passing.
Its response always includes `autonomous_allocation_enabled=false`.

## Acceptance and incident handling

Run the focused suite:

~~~bash
conda run -n aletheia pytest -q tests/jobs/test_fault_injection.py
~~~

It uses actual child processes that exit with `os._exit`, real PostgreSQL rollback/reconnect, worker
retry and lease recovery, scientific-command redelivery, archive `ENOSPC`, forged worker-manifest
identity, and one-time action ambiguity. The passing campaign proves only the measured engineering
invariants for that exact manifest/environment.

If a campaign fails or blocks:

1. keep and commit the report rather than deleting the evidence;
2. inspect scenario blockers and evidence hashes;
3. repair the boundary without changing the old manifest/report;
4. run a new content-addressed campaign; and
5. require the newest complete campaign to pass before F11-S7 review.

Do not manually change queue, command, action, or campaign rows. Do not treat infrastructure
exhaustion as a scientific negative result. Do not repeat an ambiguous outward effect.

## Honest boundary

F11-S6 establishes a deterministic campaign/evidence contract and demonstrates zero measured loss
or duplication across the ten accepted local boundaries. It does not prove that every production
network, kernel, provider, disk, multi-region database, or laboratory failure is covered. It does
not itself execute a 72-hour gate, demonstrate a scientific pivot, validate portfolio utility, or
enable autonomous spending/task/action authority. F11-S7 now provides the durable gate
implementation, while its real 72-hour run remains pending.
