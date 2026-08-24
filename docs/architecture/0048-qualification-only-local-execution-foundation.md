# Architecture decision 0048: A qualification-only local execution foundation

- Status: Accepted (PR-4a qualification foundation; production composition deferred to PR-4b)
- Date: 2026-08-24
- Scope: local execution authority, resource/budget leases, node proofs, and artifact custody

## Decision

PR-4 starts with an operational engineering-qualification boundary. It does not create a second
scientific controller and it does not infer launch authority from a PR-3 `ExecutionIntent`.
`ExecutionIntent.authorized_at`, a compilation receipt, a budget hash, or successful Pydantic model
construction are declarations, not permission to execute.

The only admitted PR-4a bundle is signed by a deployment-pinned qualification key and is permanently
marked `qualification_only=true` and `scientific_admission_allowed=false`. Verification reloads and
revalidates the exact compilation request/result, WorkOrder, intent, input-artifact custody, budget
authorization, cost quote, and—on a retry—the prior immutable terminal receipt. It reruns the PR-3
compilation, intent-binding, and confirmed-failure retry verifiers. PR-4a accepts only replay-safe,
network-none work and only `NEVER` or direct idempotent-new-attempt retry. External/physical actions,
checkpoint resume, reconciliation-driven retry, and scientific Quest launch fail closed.

This restriction is deliberate. PR-2's current action authorization binds an action identity but
does not yet bind the exact protocol, compilation, WorkOrder, intent, input custody, quote, and
budget material. PR-5 must add that research-kernel launch authorization before production Quest
work can use this substrate.

## Independent trust inputs

No request may bootstrap its own authority:

- a qualification verifier is constructed from an exact deployment pin, including revocation;
- each `WorkerNodeManifest` is enrolled by a separate deployment-root signature before its node key
  may sign inventory or runtime evidence;
- the allocator receives a fixed registry of enrolled node verifiers rather than a caller-selected
  manifest;
- budget authorizations, pricing quotes/rate-card identities, and historical execution receipts are
  reloaded from constructor-pinned authority archives and compared with the inline bundle;
- input artifact receipts and manifests are reloaded from immutable custody, their final CAS bytes
  are streamed and rehashed at allocator database time, and WorkOrder outputs trace to one exact
  successful producer receipt;
- a separate terminal-verification key signs the exact final central `ExecutionReceipt`, node
  receipt, artifact closure, terminal state, and failure identity before settlement.

The qualification signer, node enrollment root, node runtime key, pricing/budget sources, artifact
verifier, terminal verifier, and allocator are distinct declared roles. The contracts mechanically
establish exact key and content identities; deployment policy must still protect those keys,
archives, and database roles.

This repository intentionally supplies no permissive in-memory production replacement for the
quote/rate-card or source-budget authority archives. It also does not yet compose the allocator,
node facade, concrete runtime, artifact verifier, and terminal committer. Until independently
pinned adapters exist, PR-4a is a qualification foundation rather than a deployable service.

## Linearized admission and reservation

`PostgreSQLExecutionAllocator` is the only application writer for execution authority tables. Its
`admit_and_reserve` entrypoint uses one PostgreSQL transaction that:

1. obtains a trusted `clock_timestamp()` after the relevant row locks;
2. revalidates the signed qualification and all constructor-pinned custody sources;
3. locks the execution head, budget head, node capacity head, and sorted device heads;
4. verifies current signed inventory, exact static resource compatibility, device identity and
   external occupancy, the quote placement, all validity windows, and available bigint budget;
5. inserts one qualification admission, attempt, resource/device lease, and budget reservation;
6. advances capacity, device-fence, budget, and execution heads atomically; and
7. returns a random raw lease token only on the first successful reservation. PostgreSQL stores only
   its hash; an idempotent replay never reveals the token again.

The queue is transport only. A durable-queue redelivery may wake the allocator for the same exact
attempt, but queue lease expiry never creates a new infrastructure attempt, releases resources, or
confers launch authority.

## Fences, runtime identity, and loss of contact

Ordinary worker-side launch, heartbeat, transition, and terminal callbacks are bound to the exact
`(attempt_id, fencing_epoch, lease_token)` tuple. Database-clock expiry may enter retained
reconciliation without a token; same-node adoption instead proves the previous token hash/fence in
fresh signed node evidence before rotating to a new raw token. Artifact custody is exact-intent/
attempt-bound and the final signed receipts close it back to the active fence. A stable execution
and scientific-replicate identity is not an infrastructure retry identity.

A node signs a PID-reuse-safe runtime identity plus bounded inventory, inspection, adoption, and
execution receipts. Lease expiry or agent loss moves a live attempt to
`reconciliation_required`; it does not release the device/budget or launch a duplicate. Same-node
adoption requires a fresh signed inspection of the exact running runtime, a node-local singleton
lock, and an atomic one-step fence/token rotation. A terminated/absent inspection may authorize
resource release or a later retry; `unknown` may not. Cross-node adoption is impossible.

## Artifact custody and terminal commit

The frozen launch contract requires untrusted work to receive read-only staged inputs, a
quota-bounded writable output directory, no database/object-store/signing credential, no Docker
socket, and no network. PR-4a does not include the concrete runtime adapter that enforces that
sandbox, so production launch remains disabled. The implemented trusted collector rejects path
escape, symlinks, hardlinks, non-regular files, tree changes during reading, undeclared outputs,
quota overflow, and custody metadata changes. It streams each file through a quarantine boundary
and publishes immutable content-addressed objects with fsync and conditional hard links. Existing
objects and all later reads are reopened and rehashed.

An engineering success is committed only after the node-signed terminated runtime/output material,
the complete artifact manifest, every central `ArtifactVerifiedReceipt`, and the final
`ExecutionReceipt` agree. The terminal attempt update, immutable receipt, budget settlement,
resource release, and execution outbox row share one transaction. Crash windows may leave an
unreachable immutable CAS orphan; they may not leave a database success without verified bytes.
An execution receipt remains engineering evidence and can never itself admit an observation.

The trusted central composer signs a `TerminalVerificationAttestation` over the exact central and
node receipt hashes, execution scope, artifact closure, terminal state, failure identity, and its
deployment policy/key. The allocator verifies that signature at the locked PostgreSQL observation
time and persists the exact attestation with the terminal row. A caller-supplied verifier-principal
string cannot manufacture a failure disposition or retry authority.
Historical resolution uses the same constructor-pinned terminal authority. The pin serialized in a
database row is compared with that deployment input and is never promoted into a trust root merely
because its self-signature is internally consistent.

Terminal state is mechanically constrained by the signed exit, PostgreSQL hard deadline, and exact
required-artifact closure. A zero exit maps to success only when in-window and complete, otherwise
to the exact timeout or invalid-output failure. A nonzero exit is never success; the independently
pinned terminal verifier signs its exact policy-supported failure identity and retry disposition. A
caller cannot relabel a clean run as a retryable infrastructure failure.

Before signing a qualification, the issuer freshly resolves the complete exact input custody
closure; admission repeats that resolution at PostgreSQL time. Artifact and producer
`verified_at` timestamps remain host/verifier evidence and do not order authorization. Retry order
uses the immutable terminal row's PostgreSQL `committed_at`.

## Consequences and limits

- PostgreSQL time is the authorization, freshness, deadline, and lease linearization clock. Host
  clock rollback fails closed; production requires monitored clock health.
- PR-4a is local and qualification-only. It does not expose an HTTP launch endpoint or route the
  research kernel/controller to execution.
- Each PR-4a attempt may reserve at most one accelerator. Multi-accelerator fencing and placement
  remain disabled until a later contract can preserve liveness across independently advanced
  device fence counters.
- The included node agent is an injected fault-test facade, not a concrete OCI runtime or a composed
  adapter for the PostgreSQL allocator and terminal artifact workflow. In particular, a future
  trusted runtime must enforce and freshly inspect the exact fence and reserved resource/device
  identities; the protocol-fake campaign does not prove host/container enforcement.
- It does not implement provider actions, distributed placement, checkpoint resume, ambiguous
  external-result reconciliation, observation admission, or claim updates.
- The first implementation deliberately favors full revalidation and simple locked transactions
  over throughput.
- Static source guards make the allocator the only application importer/writer of private execution
  ORM records. Schema registration is the sole read-only exception. A separately pinned read-only
  receipt archive exposes typed receipts, never ORM records or a live session.
- A database owner can disable triggers or change protected bytes. Production therefore needs a
  migrator/owner credential distinct from the allocator application role, least privilege, backups,
  and audit monitoring; Python and SQL invariants do not claim protection from a compromised DB
  owner.
- A lost pre-runtime token has no honest signed absence proof in PR-4a, and expiration or revocation
  of node or terminal-verification authority before terminal verification can also prevent safe
  release. Admission requires those pins to cover the quoted runtime, but no post-runtime grace is
  inferred. These cases retain the hold until a future signed launch-absence or historical/grace
  verification policy exists.

## Deferred work

PR-4b must add deployment-pinned quote/source-budget adapters, a hardened local runtime,
allocator-to-agent/artifact/terminal composition, signed pre-runtime absence and terminal-proof
recovery, and real isolation/adoption/fault campaigns. PR-5 adds a signed research-kernel
action-to-execution admission message, durable controller, and independent observation-admission
bridge. Checkpoint and external-action paths require their own typed receipt and reconciliation
state machines before they can be enabled.
