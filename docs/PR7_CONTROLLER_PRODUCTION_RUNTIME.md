# PR-7 controller production runtime process boundary

- Status: kernel-dispatcher and delivery-reconciler process composition complete; terminal and
  scientific-step composition uncommissioned
- Date: 2026-08-25
- Scope: turn PR-5's callable controller components into independently supervised, byte-pinned
  process roles without enlarging scientific authority

## What this slice closes

PR-5 implemented durable tasks, transactional outbox delivery, lease recovery, append-only delivery
generations, and a delivery reconciler. Before this slice, those components had no production
invocation boundary: only tests or an embedding application could call `dispatch_once()`,
`run_once()`, or `reconcile_once()`.

`scripts/run_research_controller_runtime.py` now starts exactly one of four closed roles:

- `kernel_dispatcher`;
- `terminal_dispatcher`;
- `worker`; or
- `delivery_reconciler`.

One process cannot combine roles. The runtime fresh-opens and hashes the runtime deployment
manifest, controller manifest, composition factory source, and factory config. All paths must be
canonical absolute paths, may not traverse symlinks, and the factory must remain below the reviewed
code root. The guarded loader executes the exact bytes that were hashed, rather than rereading the
path through Python's import cache. It then audits exported object origins and still rejects the raw
or re-exported legacy `ExperimentDriver`.

The deployment manifest also pins one process principal. The returned durable queue must expose
that exact principal, and a role factory may return only the privileged dependency needed by that
role. A dispatcher therefore cannot silently receive a controller step executor, and a worker
cannot receive an unused Kernel outbox writer.

On worker startup, expired leases are recovered before the first claim. Every successful cycle
emits a canonical hash-bound operational receipt. Idle, dispatch, task, and reconciliation results
are typed separately. These receipts explicitly carry `scientific_authority=false`; they are
monitoring records, not observations or claims. Invariant, pin, or factory failures terminate the
process so a supervisor can alert/restart it rather than hiding corruption in a retry loop.

## Authority-minimal PostgreSQL roles

The checked-in `aletheia.research_controller_postgresql_runtime` factory makes two roles directly
composable:

- `kernel_dispatcher` receives `PostgreSQLResearchKernelOutbox` plus the durable queue;
- `delivery_reconciler` receives only the durable queue.

The PostgreSQL config binds the role, process principal, SHA-256 of the configured database URL,
and exact Alembic revision. Duplicate JSON keys, a changed DSN, a stale schema revision, or a role
rebind fails before the process loop starts.

`PostgreSQLResearchKernelOutbox` is deliberately narrower than `ResearchKernelStore`. It can list
ready rows for registered Quests and compare-and-set one exact row to `published` inside the
dispatch transaction. It has no trust root, policy, CAS archive, command commit, or replay/audit
method. Operational delivery therefore no longer requires loading scientific signing authority.

## Deployment inputs

The outer deployment owns three distinct immutable files:

1. the existing `ResearchControllerManifest`;
2. a role-specific composition config; and
3. a `ResearchControllerRuntimeDeployment` that binds both files and the exact factory source.

For the built-in PostgreSQL roles, the composition config has this closed shape (values are
deployment-specific):

~~~json
{
  "schema_name": "aletheia.research_controller_postgresql_runtime_config",
  "schema_version": 1,
  "role": "delivery_reconciler",
  "process_principal_id": "principal.controller.delivery_reconciler",
  "database_url_sha256": "<sha256-of-exact-configured-dsn>",
  "schema_revision": "20260828_0027",
  "scientific_authority": false,
  "kernel_command_authority": false,
  "observation_admission_authority": false
}
~~~

After independently pinning the runtime-manifest bytes, one supervised process runs as:

~~~bash
conda run -n aletheia python scripts/run_research_controller_runtime.py \
  --deployment-manifest /etc/aletheia/controller/reconciler-runtime.json \
  --deployment-manifest-sha256 <externally-pinned-sha256>
~~~

`--once` performs startup recovery and one cycle for deployment smoke tests. The CLI checks the
database schema before loading any role.

## Evidence and failure semantics

Focused tests cover:

- self-derived runtime identity and closed false-authority flags;
- path, source, config, controller-manifest, DSN, schema, principal, and role rebinding;
- duplicate config keys and exact-byte guarded execution;
- worker startup recovery exactly once;
- typed/hash-bound idle, dispatch, and reconciliation receipts;
- authority-minimal PostgreSQL outbox behavior with no Kernel `commit` or `audit` surface; and
- preservation of the repository-wide legacy-driver/dependency boundary.

The existing PR-5 database invariants remain authoritative for enqueue/delivery/outbox publication
atomicity and redrive generations. Killing a dispatcher or reconciler process is therefore safe at
the transaction boundary; this slice supplies the missing invocation loop, not a new checkpoint.

## Remaining gates

This is not the complete PR-5 production composition. Two roles intentionally have no checked-in
generic factory:

- `terminal_dispatcher` must be built with the exact PR-4 node/runtime/terminal authority and may
  not trust an unverified ORM row;
- `worker` must bind every `ControllerStep` to its dedicated proposal, signing, compiler,
  qualification, validation, admission, or continuation adapter. A catch-all model callback is
  forbidden.

PR-6's legacy-evaluation leaf likewise still needs a qualified image and a real worker step handler
that enters through PR-4 execution custody. The target-host installer/observer/campaign, deployment
key custody, independent F9-v2 validator service, systemd units, health/alert policy, and live
multi-process PostgreSQL kill/restart campaign remain open. No host is called deployable, and no
scientific claim follows from these process receipts.

See [ADR 0052](architecture/0052-controller-production-runtime-process-boundary.md), the
[PR-5 guide](PR5_DURABLE_SCIENTIFIC_CONTROLLER.md), and the
[end-to-end architecture](END_TO_END_AUTONOMOUS_RESEARCH_ARCHITECTURE_2026_08_22.md).
