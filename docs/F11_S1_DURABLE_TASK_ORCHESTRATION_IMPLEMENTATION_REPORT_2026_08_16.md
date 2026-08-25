# F11-S1 Durable Task Orchestration Implementation Report

Date: 2026-08-16

## Outcome

F11-S1 is engineering-complete. Long-running research execution no longer depends on an
`asyncio.Task` owned by the FastAPI process. A Postgres-backed queue now owns content-bound task
requests, dependencies, attempts, leases, heartbeats, retry decisions, active mutual-exclusion
scopes, recovery audits, and successful result pointers. Launch/resume is a control-plane enqueue;
an independently deployed worker runs the existing experiment driver.

The observable path is durable as well. Each task state version and one immutable keyed event
commit in the same database transaction, and SSE replays the database event ID through
`after_id`/`Last-Event-ID`. An API process restart or a second API process is no longer an event
truth boundary.

This does not complete F11. It establishes durable engineering delivery only. F11-S2 must still
make all scientific transitions and one-time outward actions transactionally replay-safe; later
slices still own the Quest/Program/Campaign graph, memory compaction, portfolio policy, broad fault
injection, and the 72-hour gate.

## Related-work basis

The implementation uses narrowly documented platform semantics:

- [PostgreSQL `SELECT` locking](https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE)
  describes `SKIP LOCKED` as an inconsistent general-purpose view that is useful for multiple
  consumers of a queue-like table. Aletheia uses it only for claim/recovery coordination, not for a
  scientific read model.
- [WHATWG Server-Sent Events](https://html.spec.whatwg.org/multipage/server-sent-events.html#last-event-id)
  defines `Last-Event-ID` as the reconnection cursor. Aletheia maps it to the monotonic database
  event ID.
- [Temporal's durable-execution model](https://docs.temporal.io/) was considered as a future
  stronger workflow runtime. It was deferred because it adds an operational service and workflow
  versioning surface before Aletheia's F11-S2 scientific transaction boundary is frozen.

The design does not claim exactly-once execution. A worker can complete an external effect and die
before acknowledging it; delivery is intentionally at-least-once, with exact callback identities
and a requirement for downstream idempotency/receipts.

## Durable schema

Four forward-only revisions extend the single Alembic chain:

1. `20260816_0006` adds `durable_tasks`, `durable_task_dependencies`,
   `durable_task_attempts`, `durable_queue_audits`, and immutable event identity columns;
2. `20260816_0007` binds requested versus actually scheduled retry decisions to each attempt, so a
   callback replay cannot change retry intent; and
3. `20260816_0008` adds active `concurrency_key` mutual exclusion through a partial unique index;
   and
4. `20260816_0009` rejects direct SQL update/delete of every keyed durable event.

The live verification after the full regression reports one head, `20260816_0009`, and zero
SQLAlchemy/Alembic schema differences. Legacy-baseline adoption explicitly excludes post-baseline
event columns/constraints while retaining exact comparison for the original table.

## Delivery contract

### Content-bound enqueue

`TaskSpec.request_sha256` covers task ID/type, canonical inputs, dependency IDs, owner/run scope,
idempotency and concurrency keys, retry policy, priority, and requested availability. Exact replay
returns the original row. Reusing task ID or idempotency key for changed content fails closed.

Dependencies must already exist, which prevents forward-edge cycles in this slice. A child remains
blocked until every parent succeeds; a failed/cancelled parent propagates a content-hashed
`dependency_failed` terminal state.

The optional concurrency key is distinct from idempotency. A partial unique index permits only one
blocked/queued/leased/retry-wait task in a mutual-exclusion scope, even if two different request IDs
race. Terminalization releases the key for a genuine successor. Research-driver tasks use
`driver:<run_id>`, closing double-click and HTTP-retry races.

### Lease, heartbeat, and recovery

Claim uses deterministic priority/order plus `FOR UPDATE SKIP LOCKED`. It creates a new attempt and
opaque 32-byte-class token, stores only SHA-256(token), and binds the worker ID and worker-manifest
SHA-256. Default runtime timestamps come from PostgreSQL `now()`; tests may inject an aware time.

Heartbeat extends the current lease. A lost lease closes the attempt as `lease_expired`; it enters
retry wait while policy capacity remains and otherwise becomes `infrastructure_exhausted`. Neither
state is a scientific refutation. Recovery writes an operator-visible audit.

Completion/failure verifies task, attempt, attempt number, worker, worker-manifest hash, and token.
Exact terminal callback replay is accepted. Changed output, changed retry intent, or a stale callback
cannot mutate the replacement attempt.

### Partial output boundary

Attempt records retain log, partial-artifact, failure-detail, and result pointers. Only success sets
the task result. Events carrying partial IDs explicitly state `partials_are_evidence=false`; a typed
scientific validator/ledger must independently admit them.

## Transactional durable events and SSE

Keyed events hash the exact run/agent/parent/type/payload projection. Exact event replay returns its
existing ID; rebinding a key to changed content fails. PostgreSQL rejects keyed-row mutation and
every read recomputes the stored SHA-256. Queue transition version `N` uses
`durable-task:<task-id>:N`, written with the task update in one SQLAlchemy transaction. A forced
event-store failure proves that the task insert rolls back.

`/events` now polls ascending database pages. A new connection tails from the current cursor to
preserve the dashboard's live-only default; `after_id` or `Last-Event-ID` requests replay. Every SSE
frame includes the persisted ID. The in-process bus remains only a legacy low-latency local fan-out.

## API and worker separation

The control plane exposes enqueue/list/get/attempt/recovery endpoints. Run launch/resume enqueues
`research.experiment_driver.v1`, returns `task_id`, and does not create the driver coroutine.

`DurableWorker`:

- claims only explicitly registered task types;
- passes a frozen task snapshot, never the raw lease token, to the handler;
- automatically heartbeats while synchronous or asynchronous handlers run;
- maps typed infrastructure/scientific/invalid failures to queue categories;
- leaves cancellation/process death for lease recovery; and
- refuses stale ownership rather than attempting a late write.

`scripts/durable_worker.py` loads explicit trusted `TASK_TYPE=MODULE:CALLABLE` registrations.
`scripts/durable_tasks.py` supports strict JSON enqueue, inspection, attempt history, listing, and
recovery. Both require the exact current schema.

## Fault and adversarial coverage

The 13 focused F11 tests cover:

1. exact enqueue replay and content rebinding rejection;
2. task/event atomic rollback;
3. raw-token non-persistence and forged token rejection;
4. heartbeat extension and exact/conflicting completion replay;
5. retryable infrastructure versus terminal scientific outcomes;
6. changed retry-intent rejection;
7. stale callback isolation after a replacement claim;
8. dependency release and failed-parent propagation;
9. two-worker `SKIP LOCKED` claim uniqueness;
10. concurrent active-concurrency-key enqueue and terminal release;
11. a real child process terminated with `os._exit(23)`, restart recovery, and late-owner rejection;
12. final lease exhaustion kept distinct from scientific failure;
13. cross-session durable event/SSE replay and independent worker retry/success.

The API/RBAC/launch focused matrix adds control-plane route and data-readiness coverage.

## Validation

Final closeout:

- F11-focused queue/SSE/API suite: 13 passed in 1.26 s;
- expanded migration/API/launch/F11 suite: 25 passed;
- full non-Docker regression: 1247 passed, 1 skipped, 29 deselected in 1329.93 s;
- warnings: 2611 existing spglib deprecation warnings only;
- Alembic head: `20260816_0009`;
- ORM/schema differences: 0;
- scoped Ruff: passed for every changed Python file;
- scoped Ruff format check: 20 files passed; and
- `git diff --check`: passed.

Repository-wide Ruff is not yet a clean historical gate: it reports 20 pre-existing findings in
unrelated exploratory probe scripts and one existing test import. The F11 slice introduces none.

Final implementation identities:

- contracts: `25d083ff9c6ca5938159be45ce243bea575cee6eebec3d0470d3b78db37a950a`;
- persistence: `add6930a03fd663dde8f312578992eba598c53d2c09e2c290bf8c8fcef8fad43`;
- queue: `759ef31ab5cc6d5227ef725d23d28560b59315ceeb3a07edcab9cad1608f2854`;
- worker: `68d9678d10b663ba1242f450ef97d6917f482734b41ee36f56892dad814dbe4d`;
- durable event store: `9bac8ad9c184f38d35aec899de56625372d7007d1cbb1e80c31fce19a02b824f`;
- SSE endpoint: `121279101b2cdec6f5bfad87ce55dad4aa7c5a87115169f0e4c64ac697192095`;
- driver adapter: `2b27b56df3cc9db922acacb7188080da808b9bf66ea69412755541515a53d268`;
- keyed-event mutation migration:
  `2ecad6d25cfe9e23463e55f57fdf9ab56d3414cb3143e63d2c9c8d9b1aefd73d`;
- core queue tests: `aa818f0892ac2df5ac3f6c5dddb2adeded112e126fbebaaf401d05cd1eb3f7fd`;
- SSE tests: `760d7f2ac2985db0d809b1e514f4946af959c0527f509147f5cc6d4ebbb03706`;
- API tests: `ce8417c1453cf27e44eb9ad4c10644e11d00aa5ede2cd3eea71d1f5dccf7b770`.

## Files

- `aletheia/jobs/{contracts,persistence,queue,worker}.py`
- `aletheia/api/tasks.py`
- `aletheia/api/events_sse.py`
- `aletheia/events/store.py`
- `aletheia/scheduler/durable.py`
- `migrations/versions/20260816_0006_f11_durable_queue.py`
- `migrations/versions/20260816_0007_f11_attempt_retry_identity.py`
- `migrations/versions/20260816_0008_f11_active_concurrency_keys.py`
- `migrations/versions/20260816_0009_f11_immutable_keyed_events.py`
- `scripts/durable_tasks.py`
- `scripts/durable_worker.py`
- `tests/jobs/test_durable_queue.py`
- `tests/test_durable_events_sse.py`
- `tests/test_durable_task_api.py`
- `docs/jobs/DURABLE_TASK_ORCHESTRATION.md`
- `docs/adr/0033-f11-postgres-durable-queue-and-event-cursor.md`

## Honest boundary and next work

F11-S1 establishes that work and telemetry survive process loss. It does not prove that every
legacy scientific operation is replay-safe. In particular, some scheduler paths still write a
scientific row and publish an event in separate transactions; one-time holdout/external actions
need durable authorization/execution receipts; and existing external side effects need exact
idempotency bindings.

The next implementation slice is F11-S2: enumerate prediction, observation validation, belief
update, artifact commit, holdout/external access, and outward-action transitions; place each behind
one transactional command/outbox boundary; add duplicate-message and crash-point fault injection;
and prove replay creates neither a second scientific update nor a second outward effect.
