# PR-5 durable scientific controller and observation admission

- Status: local source/test vertical cut complete; production composition and target-host
  deployment unqualified
- Date: 2026-08-25
- Scope: signed Research Kernel action-to-execution bridge, independent observation admission,
  durable controller delivery, restart recovery, and typed continuation

## What this slice closes

PR-5 connects the PR-2 Research Kernel, PR-3 graph-scoped protocol compiler, and PR-4
qualification-only execution substrate without giving any one component end-to-end scientific
authority. The controller remains an operational coordinator: it can reconstruct a next step and
submit a proposal to a dedicated authority, but it cannot sign its own Research Kernel command,
admit its own observation, or turn engineering success into scientific evidence.

The local vertical cut now exercises this sequence:

1. an authorized Research Kernel action is compiled against graph-scoped F9-v2 world-model and
   capability bindings;
2. a missing observable produces a canonical blocked compilation, which is retained as durable
   redesign evidence;
3. a typed refinement commits the redesigned branch and a revised protocol compiles;
4. a separately signed scientific execution authorization is durably preregistered before PR-4
   admission, reservation, and process start;
5. a raw successful run is independently checked against the exact Kernel action, PR-4 lineage,
   fresh artifact bytes, and an immutable F9 validation campaign;
6. validation and admission use separate signed challenges and authorities;
7. one PostgreSQL transaction commits the admitted row together with the signed
   `observation_incorporated` event, canonical snapshot, Kernel outbox row, and stream head;
8. a valid negative or inconclusive observation can mechanically require a hypothesis-set fork;
9. the signed fork and selected-child activation produce a graph-scoped discriminating follow-up;
10. that follow-up is compiled, executed, independently admitted into a second distinct scientific
    slot, and incorporated without invoking the legacy `ExperimentDriver` or synthesizing a legacy
    Run.

This is deterministic engineering evidence for the control path. It is not a scientific result,
proof of novelty, or evidence that a target host is safe to run unattended.

## Authority and custody chain

The bridge keeps five duties separate:

| Boundary | Authority or evidence | What it cannot do |
|---|---|---|
| Research action | root-certified Research Kernel command/event/CAS replay | execute work or admit an observation |
| Engineering qualification | PR-4 bundle, grant, admission, reservation, node/runtime terminal lineage | report a scientific positive, negative, or inconclusive outcome |
| Validation | independent validation campaign and DB-issued validation challenge | fill an empty scientific slot |
| Admission | distinct admission decision and DB-issued admission challenge | mutate the Research Kernel alone |
| Incorporation | ordinary external Kernel command authority plus atomic store participant | reinterpret validation or admit a second observation for the slot |

`PostgreSQLRawRunCustodyVerificationAdapter` replays the public PR-4 lineage and requires the SEA
registration time to precede qualification admission, resource reservation, and actual process
start. It then reopens and rehashes the exact local CAS objects. The protected PR-5 adapter module
contains only this raw-run adapter and the Kernel action-authority adapter; neither it nor the
controller imports legacy F9, events, memory, programs, or `ExperimentDriver`.

The frozen F9-v1 campaign bridge is now explicitly isolated at
`aletheia.migration.f9_v1_observation_compatibility`. It fresh-rehashes legacy campaign bytes and
writes one immutable graph/raw-run binding CAS, but it is not a PR-5 validation/admission authority
and is never imported by the observation/controller authority graphs. A deployment-owned F9-v2
validation adapter remains a production gate.

Existing PR-4 v1 quote and artifact-receipt schemas do not carry independent signer-key signatures.
Those two edges therefore remain trusted-local, deployment-pinned evidence; the bridge must not
describe them as host-root-independent cryptographic attestation.

## Persistence and exactly-once boundary

Alembic revision `20260828_0027` adds ten append-only tables:

- `research_controller_registrations`, `research_controller_deliveries`,
  `research_controller_delivery_attempts`, and `research_controller_delivery_resolutions`;
- `research_protocol_compilations` and `research_continuation_receipts`;
- `research_scientific_execution_authorizations`;
- `research_observation_issuance_challenges`;
- `research_observation_validation_receipts`;
- `research_observation_admissions`.

The controller tables are delivery/recovery projections, not a second scientific ledger. The
Research Kernel event stream and canonical snapshot remain the sole scientific state authority.
SEA execution and attempt identities are preregistered before a PR-4 attempt row exists, so the SEA
table deliberately has no premature foreign key to `execution_attempts`; the concrete custody
adapter later requires an exact immutable attempt lineage.

The scientific slot is unique in the admission table. Deferred PostgreSQL constraint triggers
require every admitted row to name its exact `observation_incorporated` event and require every such
event to have the matching admitted row. The coordinator writes that row through
`ResearchKernelStore.commit_in_session`, so event, snapshot, outbox, head, and admission either all
commit or all roll back. Exact retries return the original receipt; a different decision for the
same slot fails closed.

Challenges use database time and immutable nonces. A live challenge for the same purpose and row
scope is serialized under the stable authorization lock. Once it expires, a new challenge may be
appended with a fresh nonce; an old row is never mutated or treated as a permanent reservation.

The frozen write-owner inventory classifies all ten PR-5 tables individually, names each concrete
append surface and its exact production callers, and keeps controller delivery, compilation,
challenge, and continuation projections separate from scientific execution, validation, and
admission authority. The protected controller package and authority-neutral durable-task contracts
are excluded from the legacy-source digest as new boundaries; the top-level legacy-worker
compatibility composition remains inside that digest and therefore cannot change silently.

## Durable controller and recovery

`POST /research-kernel/programs/{program_id}/quests/{quest_id}/launch` accepts only an idempotency
key and the caller's expected Kernel head. Controller code, policies, capability catalog, bridge
policy, worker identity, and retry policy come from a deployment-owned manifest whose bytes and
SHA-256 are pinned in server configuration. The launcher repeats the complete Kernel audit after
locking the Quest inside the registration transaction, closing the read/write race between the
HTTP check and durable subscription.

Kernel and PR-4 terminal outboxes create deterministic `research.controller.v1` tasks. Each task
has `run_id=None`, a per-Quest concurrency key, a bounded retry policy, and exactly one controller
tick. Enqueue, delivery receipt, and outbox publish happen in one caller-owned transaction. A busy
Quest leaves the source pending for a later pass.

Each immutable delivery owns an append-only generation chain. A failed task is redriven as a new
generation; a successful internal step that committed no authoritative Kernel command or
observation admission likewise receives a deterministic successor generation. Awaiting and
blocked steps, authoritative commits, cancellations, invalid results, and exhausted generation
budgets are frozen as typed delivery resolutions. Reconciliation locks and rechecks the delivery
before appending, so concurrent reconcilers converge, settled old deliveries cannot starve newer
work, and no terminal task is silently made runnable again.

Every tick reconstructs state from a full Kernel audit and append-only receipts. Recovery verifies
the exact registration, delivery source, compilation request and recomputed result, SEA,
validation, admission, incorporation event, continuation receipt, and terminal execution pair.
There is no mutable controller scientific checkpoint. A lease loss before commit retries the same
task; a crash after a Kernel commit is recovered from the authoritative event and the next outbox
wakeup.

The generic controller service exposes a narrow typed step-execution port. Durable-task values and
the caller-owned queue seam live in an authority-neutral package; the protected controller never
imports the legacy `jobs` package or its event emitter. The existing v1 worker composition is an
explicit outer compatibility module, `aletheia.research_controller_runtime`, rather than an inward
controller dependency. A deployment must bind
each step to its dedicated signing, compiler, PR-4, validation, admission, or continuation adapter;
there is intentionally no catch-all model callback and no legacy `ExperimentDriver` fallback.

## Remaining gates

PR-5 does not make PR-4 deployable. Before unattended or remote execution, the exact target host
still needs the opt-in Linux/root/systemd/loop/ext4/rootful-Docker campaign described in
[the PR-4b guide](PR4B_LOCAL_EXECUTION_COMPOSITION.md). The repository still has no target-host
installer, concrete Linux observer, frozen installed-manifest instance, or campaign runner.

The local vertical fixture is synthetic and uses reviewed deterministic capability adapters. The
PR-5 source/test slice separately exercises the concrete PostgreSQL/CAS custody adapters and the
atomic coordinator contract, but it is not a live multi-process PostgreSQL kill/restart campaign.
A production launch additionally needs deployment composition for the controller step handlers,
signing-key custody, terminal-dispatcher/worker processes, an independent F9-v2 validator
adapter/service, monitoring, and process-kill PostgreSQL fault campaigns. PR-7a now supplies
byte-pinned process loops and concrete authority-minimal PostgreSQL composition for Kernel dispatch
and periodic delivery reconciliation; it does not supply terminal or scientific-step authority.
The F9-v1 migration adapter does not satisfy that production gate.
Remote/GPU execution, external-effect actions,
checkpointing, autonomous spending, claim admission, and publication remain closed.

PR-6's explicitly limited legacy-evaluation compatibility source/test slice is now complete; see
[the PR-6 guide](PR6_LEGACY_EVALUATION_COMPATIBILITY.md). Its production step handler and qualified
image remain deployment work. No remote canary should run until both the PR-4 target-host campaign
and the PR-5 production composition are independently accepted.
