# Durable task orchestration

F11-S1 moves long-running work out of the API process and onto a Postgres-backed at-least-once task
queue. It provides restart recovery and a durable event cursor. Queue completion is still not
scientific evidence. F11-S2 now supplies the separate exact scientific-command and one-time-action
boundary described in `TRANSACTIONAL_SCIENTIFIC_TRANSITIONS.md`.

## State model

~~~text
                       dependency succeeds
blocked ------------------------------------------------> queued
   |                                                        |
   | dependency fails                                       | claim + new attempt/token
   v                                                        v
failed(dependency_failed)                                leased
                                                           |  \
                                  heartbeat extends lease  |   \ success
                                                           |    v
                                    infra failure/expiry    |  succeeded
                                                           v
                                                      retry_wait
                                                           |
                                               backoff elapsed + claim
                                                           |
                                                           +----> leased

scientific/invalid/cancelled failure -> failed/cancelled
last expired attempt                 -> failed(infrastructure_exhausted)
~~~

The raw lease token is never stored or sent to a handler. `DurableWorker` owns it; handlers receive
only a frozen `TaskSnapshot`. The database stores SHA-256(token) in the task and attempt rows.

## Durable objects

- `durable_tasks`: immutable request identity plus current coordination projection;
- `durable_task_dependencies`: pre-existing task edges;
- `durable_task_attempts`: worker/manifest identity, heartbeat, terminal category, logs, partials,
  and successful result pointer;
- `durable_queue_audits`: explicit restart/recovery summaries; and
- `events.event_key/event_sha256`: immutable observable transition identity.

Task rows are mutable coordination projections. Attempt rows record one delivery lifecycle. Typed
scientific objects remain immutable in their existing F8/F9/F10 ledgers; the queue does not replace
them.

`idempotency_key` binds retries of one logical request. Optional `concurrency_key` is different: a
partial unique index permits at most one blocked/queued/leased/retry-wait task in that mutual-
exclusion scope, even when two distinct requests race. The key is released only by a terminal state.
Research-driver tasks use `driver:<run-id>` so a double click cannot run two drivers for one run.

## Deploy

Apply the reviewed migration before starting either process:

~~~bash
conda run -n aletheia alembic upgrade head
~~~

Start the API control plane:

~~~bash
conda run -n aletheia uvicorn aletheia.api.main:app --port 8000
~~~

Start at least one separate research-driver worker. Replace the example manifest digest with the
SHA-256 of the retained worker manifest used by the deployment:

~~~bash
conda run -n aletheia python scripts/durable_worker.py \
  --worker-id research-worker-01 \
  --worker-manifest-sha256 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --handler research.experiment_driver.v1=aletheia.scheduler.durable:run_driver_task
~~~

`SIGINT`/`SIGTERM` stops claiming after the current call boundary. A hard kill does not write a
scientific failure; the lease becomes reclaimable after expiry.

## API control plane

- `POST /tasks`: validate and enqueue a frozen `TaskSpec`;
- `GET /tasks`: list by run/status;
- `GET /tasks/{task_id}`: current projection;
- `GET /tasks/{task_id}/attempts`: complete delivery history;
- `POST /tasks/operations/recover-expired`: operator recovery scan;
- `POST /runs/{run_id}/launch`: data-gated durable driver enqueue; and
- `POST /runs/{run_id}/resume`: durable successor operation enqueue.

Launch/resume accepts an optional `operation_id`. Reusing it with identical run/mode returns the
same task; changing bound content conflicts. Clients that need HTTP retry idempotency should generate
one operation ID before the first request and retain it until a response is known.

## Operator CLI

Inspect and recover:

~~~bash
conda run -n aletheia python scripts/durable_tasks.py list --status leased
conda run -n aletheia python scripts/durable_tasks.py get <task-id>
conda run -n aletheia python scripts/durable_tasks.py attempts <task-id>
conda run -n aletheia python scripts/durable_tasks.py recover
~~~

Enqueue a strict JSON `TaskSpec`:

~~~bash
conda run -n aletheia python scripts/durable_tasks.py enqueue --spec task.json
~~~

Example:

~~~json
{
  "task_id": "task-example-001",
  "task_type": "example.echo.v1",
  "inputs": {"artifact_id": "artifact:input:001"},
  "dependency_ids": [],
  "owner": "operator",
  "run_id": null,
  "idempotency_key": "example:echo:001",
  "concurrency_key": null,
  "retry_policy": {
    "max_attempts": 3,
    "lease_seconds": 300,
    "heartbeat_interval_seconds": 60,
    "initial_backoff_seconds": 5,
    "backoff_multiplier": 2,
    "max_backoff_seconds": 300,
    "retryable_categories": ["infrastructure", "lease_expired"]
  },
  "priority": 0,
  "available_at": null
}
~~~

## SSE resume

A new `/events` connection starts at the current durable tail. To replay, pass either:

~~~text
GET /events?run_id=<run-id>&after_id=<last-seen-database-id>
Last-Event-ID: <last-seen-database-id>
~~~

Frames remain unnamed so the existing browser `onmessage` handler works, and now include the SSE
`id` field. Event IDs are global and monotonically increasing; filtering by run does not renumber
them.

## Failure interpretation

- `infrastructure`: worker-declared retryable execution failure;
- `lease_expired`: ownership vanished; not scientific evidence;
- `infrastructure_exhausted`: retry budget ended; not scientific refutation;
- `scientific`: a handler reached a genuine typed negative outcome boundary;
- `invalid_output`: handler output did not satisfy its contract;
- `dependency_failed`: a prerequisite ended unsuccessfully; and
- `cancelled`: explicit cancellation category.

Log and partial artifact IDs are retained on attempts, but never copied into `result_artifact_id`.
Their event projection says `partials_are_evidence=false`. A separate validator and typed ledger
commit are required before any output can support a claim.

These are delivery categories, not self-authenticating scientific verdicts. In particular, merely
raising `ScientificTaskFailure` cannot refute a hypothesis or authorize a claim; only the relevant
typed validator/transition can do that.

## Recovery and debugging

1. Run `durable_tasks.py recover` or start a worker (startup performs a recovery scan).
2. Inspect task and attempt history.
3. Resume SSE from the last database event ID.
4. For a stuck lease, check clock synchronization, `lease_expires_at`, worker liveness, and database
   connectivity. Do not edit the row manually.
5. For an idempotency conflict, compare the stored `request_sha256`; create a new explicit operation
   only when the logical work genuinely changed.

The focused acceptance suite is `tests/jobs/test_durable_queue.py` plus
`tests/test_durable_events_sse.py`. It includes an actual child worker process that exits via
`os._exit`, replacement recovery, concurrent `SKIP LOCKED` claims, stale callbacks, event rollback,
SSE replay, dependencies, finite retry, and automatic worker handling.

## Honest boundary

F11-S1 prevents process-local orchestration from being the only execution truth. Later F11 slices
now couple scientific transitions to command/event receipts, provide one-time-action
reconciliation, reconstruct the research graph and memory, plan shadow portfolios, and run the
deterministic F11-S6 fault campaign described in `FAULT_INJECTION_CAMPAIGNS.md`. None claims globally
atomic effects across an arbitrary provider. The 72-hour F11-S7 endurance gate and separately
authorized production activation remain.
