# PR-7 controller production runtime process boundary

- Status: kernel-dispatcher, delivery-reconciler, and verified terminal-dispatcher source
  composition complete; scientific-step composition and target-host commissioning pending
- Date: 2026-08-26
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

PR-7c adds a separate `aletheia.research_controller_terminal_runtime` factory. It loads only
public qualification/runtime/node/terminal authority pins, reconstructs a complete verified PR-4
terminal source, and re-reads the exact immutable outbox in the delivery transaction. It neither
loads a runtime-control private key nor exposes allocator mutations to the dispatcher port. Its
artifact CAS must be pre-created and is opened through a mutation-refusing read-only facade. See the
[PR-7c guide](PR7C_VERIFIED_TERMINAL_DISPATCHER.md) for its configuration and remaining host
ACL/custody gates.

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

This is not a commissioned PR-5 deployment. The terminal role has a checked-in, public-key-only
verified source factory, and PR-7d now supplies the checked-in complete worker factory:

- `worker` binds every active `ControllerStep` to its dedicated proposal, compiler, qualification,
  validation, admission, or continuation adapter. A catch-all model callback remains forbidden;
  signing and independent decision services stay behind receipt-authenticated Unix RPC ports.

PR-6's legacy-evaluation leaf has a launch-gated worker handler and candidate image source, but the
image and host remain unqualified. PR-7e adds the common byte-pinned Linux RPC server/runtime,
closed typed dispatch, peer/socket checks, and transport-key file custody; concrete F9-v2 and other
authority factories are still uncommissioned. The target-host installer/observer/campaign,
domain-key custody, systemd units, health/alert policy, and live
multi-process PostgreSQL kill/restart campaign remain open. No host is called deployable, and no
scientific claim follows from these process receipts.

PR-7b closes the generic worker-callback boundary with an exhaustive, controller/worker-pinned
adapter set and exact recovery-projection forwarding. PR-7d composes the subsequent concrete source
slices without importing their private keys; see
[the PR-7b guide](PR7B_CONTROLLER_STEP_AUTHORITY_BOUNDARY.md) and the
[PR-7d guide](PR7D_COMPLETE_CONTROLLER_WORKER.md), plus the
[PR-7e guide](PR7E_EXTERNAL_RPC_SERVICE_RUNTIME.md).

The three proposal steps now have a proposal-only source implementation with exact Kernel/receipt
re-audit, bounded provider drafts, and write-once unsigned-command custody. PR-7g adds the
conservative deterministic provider and checked-in single-operation endpoint, but it still needs
target-host commissioning; knowledge-grounded proposal intelligence and the independent Kernel
command authority remain open. The protocol compiler now has a two-audit, policy-pinned,
append-only step service; PR-7h adds its exact-action frozen-template provider, restart verifier,
and checked-in RPC factory. General protocol authoring plus receipt custody, ACL and external
process commissioning remain open. PR-7i adds a deterministic exact-template execution-authorizer
whose separate `0400` domain key signs only after fresh Kernel, compilation-registry and PR-4
public-custody verification. PR-7j now supplies that separate keyless process: it locks the current
Kernel authorization and atomically commits SEA registration plus PR-4 admission/reservation.
PR-7k now supplies a separate keyless raw-run source: it re-verifies the historical SEA, strict
pre-admission chronology, complete PR-4 terminal lineage, and freshly rehashed artifacts before
returning a deterministic envelope. PR-7l now supplies the isolated database-attestation process:
it serializes each preregistered slot, issues DB-time challenges, commits only validation tied to an
exact stored challenge, and loads no validator, admitter, execution, or Kernel private key.
PR-7m now supplies the independent F9-v2 validator process: it owns only the validator domain key,
replays public Kernel/PR-4/artifact custody, uses a source-pinned exact-content baseline and
publishes one write-once campaign per raw run. PR-7n now supplies the keyless committed-validation
source, which resolves the durable slot row and replays its complete DB/validator/F9/Kernel/PR-4
custody. PR-7o subsequently supplies the isolated live-challenge admission signer. PR-7p supplies
the final concrete operation factory: it owns only the database-attestation and exact ordinary
Kernel-command keys required to commit one independently decided admission and its Kernel
event/snapshot/outbox/head in the same PostgreSQL transaction. All eleven operation-family
factories therefore exist at source level; none is target-host commissioned by that fact.
Continuation now
has its own two-audit service, mechanically reconstructed observation identity, pinned assessor
policy, and durable provenance; its production service, assessment-artifact byte custody and ACL
remain open. See [ADR 0058](architecture/0058-durable-powerless-action-proposal-steps.md),
[ADR 0059](architecture/0059-durable-protocol-compilation-step.md), and
[ADR 0060](architecture/0060-durable-continuation-assessment-step.md), plus
[ADR 0066](architecture/0066-scientific-execution-authorization-rpc-service.md), and
[ADR 0067](architecture/0067-atomic-execution-registration-rpc-service.md), and
[ADR 0068](architecture/0068-verified-raw-run-source-rpc-service.md), and
[ADR 0069](architecture/0069-database-observation-rpc-service.md), and
[ADR 0070](architecture/0070-independent-f9-v2-validation-rpc-service.md), and
[ADR 0071](architecture/0071-committed-validation-source-rpc-service.md), and
[ADR 0073](architecture/0073-atomic-admission-rpc-service.md).

PR-7c closes the source-code terminal verification/composition gap. The read-only PostgreSQL ACL,
filesystem/key custody, supervisor invocation, and live process-kill behavior must still be proven
on the exact deployment host; see
[the PR-7c guide](PR7C_VERIFIED_TERMINAL_DISPATCHER.md).

See [ADR 0052](architecture/0052-controller-production-runtime-process-boundary.md),
[ADR 0054](architecture/0054-verified-qualification-terminal-dispatcher.md), the
[ADR 0062](architecture/0062-operation-closed-external-rpc-service-runtime.md), the
[PR-5 guide](PR5_DURABLE_SCIENTIFIC_CONTROLLER.md), and the
[end-to-end architecture](END_TO_END_AUTONOMOUS_RESEARCH_ARCHITECTURE_2026_08_22.md).
