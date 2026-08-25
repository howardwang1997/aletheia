# ADR 0033: Postgres durable queue and database event cursor

Date: 2026-08-16

## Status

Accepted for the F11-S1 engineering boundary. This decision does not complete transactional
scientific transitions (F11-S2), the quest/program graph, portfolio planning, or the endurance gate.

## Context

The original scheduler launched `ExperimentDriver` as an `asyncio.Task` inside the FastAPI process.
Its event stream was persisted, but the SSE endpoint subscribed only to that process's in-memory
queue. An API restart therefore lost execution ownership and live delivery; a second API process
could not observe the first; and `ComputeJob` had no lease, heartbeat, attempt, retry, dependency,
or idempotency contract.

The main threats are:

- two workers perform the same non-idempotent task concurrently;
- a killed worker leaves work permanently stuck or falsely records a scientific failure;
- a late callback from an expired attempt overwrites its replacement;
- a repeated API request creates another logical task with changed inputs;
- partial output from a killed attempt is treated as validated evidence;
- queue state commits while its observable event is lost;
- an SSE reconnect silently misses events; and
- queue history becomes a second, competing source of scientific truth.

PostgreSQL is already the project's ledger and migration boundary. Its official `SELECT`
documentation explicitly describes `SKIP LOCKED` as unsuitable for a general consistent view but
useful for multiple consumers of a queue-like table. The WHATWG SSE standard defines
`Last-Event-ID` as the browser reconnection cursor. Those are the narrow primitives used here:

- [PostgreSQL locking clause and `SKIP LOCKED`](https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE)
- [WHATWG Server-Sent Events and `Last-Event-ID`](https://html.spec.whatwg.org/multipage/server-sent-events.html#last-event-id)

## Decision

### 1. Postgres remains the single durable coordination store

F11-S1 adds `durable_tasks`, `durable_task_dependencies`, `durable_task_attempts`, and
`durable_queue_audits` through Alembic revision `20260816_0006`. No Redis/Celery/Temporal service is
required for this slice. Queue records coordinate engineering delivery; scientific predictions,
observations, belief updates, claims, and artifacts remain authoritative only in their typed
ledgers.

### 2. Delivery is explicitly at-least-once

One claim transaction selects an eligible row with deterministic priority/order and
`FOR UPDATE SKIP LOCKED`, increments its attempt number, generates an opaque lease token, and stores
only the token's SHA-256. The raw token is returned to that worker once. A heartbeat extends the
lease from the current time.

An expired attempt is closed as `lease_expired`. It becomes `retry_wait` while its finite policy has
capacity, or `infrastructure_exhausted` after the last attempt. Neither category is a scientific
failure. A new attempt gets a new ID and token. Completion/failure callbacks bind task, attempt
number, worker, worker-manifest hash, and token; exact callback replay is idempotent, while changed or
stale content cannot mutate the active attempt.

### 3. Logical task identity is content-bound

The caller supplies a stable task ID and idempotency key. The stored request hash covers task type,
canonical inputs, dependencies, owner/run scope, retry policy, priority, and requested availability.
Exact replay returns the original row. Reuse of either identity for different content fails closed.
Dependencies must already exist, preventing forward-edge cycles in this slice.

An optional `concurrency_key` has a database partial unique index over active states. It closes the
race between distinct request IDs that represent mutually exclusive work (the driver uses one key
per run), while terminalization releases the scope for a genuine successor.

### 4. Partial outputs are retained but are not results or evidence

Attempts may point to log and partial-artifact IDs. Only successful completion sets the task's
`result_artifact_id`; every task event explicitly labels partials `partials_are_evidence=false`.
Scientific validators must separately admit an artifact to an evidence ledger.

### 5. Queue transition and event commit together

The `events` table gains nullable `event_key` and `event_sha256`. Keyed events are immutable:
exact replay returns the existing event ID, key rebinding fails, PostgreSQL rejects direct update/
delete, and reads re-hash the projection. Each task state version commits one deterministic keyed
event inside the same SQLAlchemy transaction. A failed event insert rolls back the task transition.

The SSE endpoint no longer subscribes to process-local queues. A fresh connection tails from the
current database cursor; `after_id` or `Last-Event-ID` replays ascending persisted rows. Each frame
carries the database event ID. The in-memory `EventBus` remains only a local low-latency fan-out for
legacy subscribers and tests.

### 6. API and worker lifecycles are separate

`/runs/{run_id}/launch` and `/resume` enqueue `research.experiment_driver.v1` and return its task ID.
They do not start a long-lived coroutine in the API process. `DurableWorker` claims only explicitly
registered task types, heartbeats while the handler runs, and converts typed handler failures into
queue outcomes. Cancellation or process death deliberately leaves the lease for recovery.

## Consequences

- API and worker processes can restart independently without losing queued work.
- Concurrent workers can claim different rows without assigning one attempt twice.
- Database polling adds up to 250 ms SSE latency and more read traffic, but removes process affinity.
- `SKIP LOCKED` intentionally relaxes a globally consistent/fair view while rows are locked; queue
  ordering is deterministic only among rows visible to a claimant.
- At-least-once delivery still requires idempotency keys or approval receipts around outward actions.
- A worker-manifest hash is provenance binding, not authentication by itself; deployments must
  retain and verify the referenced manifest artifact.
- The queue does not yet make every existing scientific transition transactional. F11-S2 must add
  exact transition/outbox boundaries and one-time external-action receipts before broad unattended
  execution.

## Rejected alternatives

### Keep the in-process task and event buses

Rejected because neither survives process death nor supports multiple API/worker processes.

### Claim exactly-once execution

Rejected because a worker can finish an external effect and die before recording completion. The
transport is at-least-once; idempotent scientific commits and external action receipts are the safe
boundary.

### Redis/Celery as the source of truth

Rejected for this slice because it adds an operational store while scientific state already lives
in PostgreSQL. A broker may later accelerate delivery, but it cannot own the only durable truth.

### Adopt Temporal immediately

Deferred. Temporal offers a stronger workflow runtime but introduces another service, workflow-code
versioning discipline, and migration/operations surface before Aletheia's scientific transaction
boundaries are frozen. The queue contracts are deliberately portable enough to revisit that choice.

### Treat lease expiry as task or scientific failure

Rejected. Expiry establishes loss of engineering ownership only; it says nothing about the tested
hypothesis or any partial scientific output.
