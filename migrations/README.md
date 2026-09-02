# Aletheia database migrations

The database schema is versioned with Alembic. Application startup never creates or alters tables.
The current repository head is `20260903_0032`.

For a fresh database:

```bash
conda run -n aletheia alembic upgrade head
```

For a pre-Alembic database created by the old `create_all()` path:

```bash
conda run -n aletheia python scripts/adopt_schema_baseline.py
```

The adoption command is deliberately strict: it stamps revision `20260813_0001` only when Alembic
reports no differences from that legacy baseline. It does not repair a partial or unexpected
schema. Then run `alembic upgrade head` to add post-baseline tables. Back up the database before
adoption or upgrade; `scripts/backup_database.py` records a content hash receipt when the local
`pg_dump` client is available.

Tests may still use `aletheia.db.create_all()` as an explicit test fixture. Runtime code must call
`require_schema_exact()` and fail closed when the database is empty, behind, ahead, or structurally
different from the ORM-managed head.

Revision `20260814_0003` adds the F8-S1 immutable corpus store. Its knowledge rows and membership
edges reject SQL `UPDATE` and `DELETE`; corrections, later observations, and new corpus membership
must be inserted as new content-addressed versions. The migration stores hashes, locators, typed
metadata, article-level access grants, and provider receipt identities—not licensed source text.

Revision `20260815_0004` adds F9-S1 immutable research-question, hypothesis-version, assumption,
prediction, competing-belief, and world-model snapshot tables. Stable lineage IDs are separate
from content SHA-256 version identities; database triggers reject mutation. The existing K2
`belief_states` table and historical K2 events are not rewritten. A read-only
`k2_belief_state_compat` projection exposes their Beta mean and labels the representation explicitly
so callers cannot confuse it with an F9 multi-hypothesis posterior.

Revision `20260815_0005` adds immutable F9-S8 world-model transition records. Each record binds one
committed update receipt to its source, posterior, and optional revision-closed next snapshot.
Application code writes those objects and the corresponding typed scheduler event in one
transaction; transition rows reject SQL `UPDATE` and `DELETE`.

Revision `20260825_0024` adds the 16-table PR-4a local execution foundation: enrolled node and
inventory state, qualification admission, resource/device/budget heads and leases, attempts and
adoptions, immutable terminal receipts, and the transactional v1 outbox.

Revision `20260826_0025` adds exactly one `execution_assignment_envelopes` table for node-encrypted,
attempt-bound initial lease-token delivery. It refuses to upgrade a database containing any PR-4a
attempt: drain/rebuild the qualification attempt store rather than invent assignment custody for an
older row.

Revision `20260827_0026` adds ten append-only runtime-v2 tables for inert preparation, short-lived
launch authorization, accepted actual launch, pre-runtime absence, fence rebind, termination
challenge/acceptance, artifact terminal acceptance or deadline expiration, and the transactional v2
outbox. The current execution schema therefore contains exactly 27 `execution_*` tables: 16 from
`0024`, one from `0025`, and ten from `0026`. Runtime-v2 termination is not written back as a legacy
`ExecutionReceipt`.

Revision `20260828_0027` adds the append-only PR-5 durable-controller and scientific-observation
bridge. Controller registrations and three-way source deliveries remain operational projections;
delivery attempts form a bounded append-only generation chain, while typed delivery resolutions
freeze awaiting, blocked, authoritative, cancelled, invalid-result, and exhausted outcomes so a
settled old delivery cannot starve later reconciliation work;
protocol compilation and continuation receipts make crash recovery deterministic. Separately
signed scientific execution authorization, issuance challenge, validation, and Phase-1 admission
rows form an exact relational chain. SEA execution and attempt identities are preregistered before
PR-4 creates the attempt row, so the SEA table deliberately has no premature attempt foreign key;
the concrete custody adapter later requires the exact immutable PR-4 lineage. An admitted row must be committed with its exact
`observation_incorporated` Research Kernel event, and each scientific slot can be admitted at most
once. Issuance challenges are immutable: a live purpose/row-scope window is serialized by locking
its stable authorization, while an expired challenge can be followed by a new row with a fresh
nonce instead of mutating or permanently reserving that row scope.

Revision `20260828_0028` removes the accidental one-SEA-per-authorization-event limit. One
authorized action may now preregister several exact scientific replicate slots; authorization,
slot, execution, attempt, qualification bundle, and qualification grant identities remain unique,
and the shared source event is indexed for campaign replay. Downgrade refuses a database that has
already used this multi-replicate capability.

Revision `20260828_0029` makes real-time endurance evidence use PostgreSQL's exact transaction
timestamp. The three database guards no longer compare a timestamp captured near transaction
start to the wall clock at trigger execution with a fixed five-second tolerance, so valid
long-running finalization work cannot fail only because trigger evaluation occurs later. Caller
clock injection remains forbidden, while accelerated engineering evidence retains its explicit
test clock.

Revision `20260829_0030` makes the runtime-v2 deferred completeness trigger execute under the
no-login execution owner with the exact `pg_catalog, public` search path. Qualification roles keep
zero direct routine grants, while allocator writes can still invoke the installed trigger's pure
validation helpers at commit. This closes the target-host failure where a legitimate attempt state
transition rolled back with `permission denied for function
aletheia_execution_runtime_v2_json_valid`.

Revision `20260831_0031` permits exactly one lease contraction when a still-unlaunched attempt
moves from `reserved` to `starting`. The new expiry must be carried by the append-only runtime
launch authorization written in the same transaction, and the resource lease must match the
contracted attempt exactly. This separates a bounded campaign pre-launch window from the short
runtime heartbeat window without allowing arbitrary lease rollback.

Revision `20260903_0032` adds no table and does not revive an expired node identity. It admits a
second closed JSON shape only for a one-hour-or-shorter, independently keyed authority pinned to
one source node manifest, one already-authorized never-started attempt, one cleanup epoch, and one
watchdog deployment. The deferred runtime trigger requires a release-only decision with no launch
or reauthorization output; legacy absence rows retain their byte shape and validation semantics.
