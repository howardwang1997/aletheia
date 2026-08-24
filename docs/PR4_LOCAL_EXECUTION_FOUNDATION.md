# PR-4a local execution foundation (frozen baseline)

PR-4a turns the PR-3 execution value boundary into a durable, fenced local engineering substrate.
It is intentionally a qualification harness, not a production scientific-launch API.

This guide records the accepted `20260825_0024` PR-4a boundary. PR-4b now extends that baseline with
sealed assignment delivery (`0025`) and a concrete CPU-only runtime-v2 composition (`0026`), without
adding scientific admission. For the current 27-table schema, lifecycle, TCB, and deployment status,
see the [PR-4b composition guide](PR4B_LOCAL_EXECUTION_COMPOSITION.md) and
[architecture decision 0049](architecture/0049-qualification-only-local-execution-composition.md).
Statements below that a
concrete adapter was absent describe the frozen PR-4a cut, not the current source tree.

## Safety boundary

The accepted path is restricted to a deployment-signed `EngineeringQualificationGrant` over one
exact `EngineeringQualificationBundle`. The bundle is closed over:

- the canonical PR-3 compilation request, result, receipt, and WorkOrder;
- one exact `ExecutionIntent` and its preregistered replicate/infrastructure-attempt identity;
- every typed input-artifact receipt and fresh CAS/custody resolution;
- a registered budget authorization and registered exact cost quote; and
- the exact prior confirmed-failure execution receipt when an idempotent retry requires lineage.

The verifier reruns `verify_compilation`, `verify_execution_intent_binding`, and, for direct
idempotent retries, `verify_execution_retry_binding`. It accepts only replay-safe, network-none work
and records literal `qualification_only=true` / `scientific_admission_allowed=false` values.

Do not interpret any of the following as launch authority: an `ExecutionIntent`, its
`authorized_at`, a `CompilationReceipt`, an opaque budget hash, a caller-supplied quote, or an
`ExecutionReceipt`. PR-5 must bind a committed Research Kernel action to the exact execution bundle
before a Quest can launch through this fabric.

## Deployment pins

Production composition must supply all of these independently of the request:

1. the qualification-authority policy/key pin and revocation time;
2. the node-enrollment root policy/key pin;
3. exact enrolled `WorkerNodeManifest` certificates;
4. the registered budget-authorization archive;
5. the registered quote/rate-card/pricing-policy archive;
6. the verified artifact/manifest CAS and terminal-receipt archive;
7. the terminal-verification policy/key pin, distinct from the node and qualification keys; and
8. the PostgreSQL connection using the restricted allocator application role.

Missing, mismatched, expired, or revoked material is a hard failure. There is no allow-all policy,
default key, self-enrolled node, caller-selected archive, or allocator caller-supplied clock. The
pure grant issuer receives explicit signed validity times; allocator authorization uses PostgreSQL.

At PR-4a completion, the repository did not contain a concrete quote/rate-card or source-budget
registry adapter or the allocator/node/runtime/artifact/terminal composition. Tests used explicit
closed fakes. PR-4b now implements those local CPU-only components, but target-host deployment
qualification and the PR-5 scientific bridge remain separate gates.

## Lifecycle

The nominal facade path is:

```text
reserved -> starting -> running -> terminated -> verifying -> succeeded|failed|cancelled
    |          |          |           |            |
    +----------+----------+-----------+------------+-> reconciliation_required
```

Lease, heartbeat, or hard-deadline expiry enters `reconciliation_required` while retaining the
device and budget hold. It never means “safe to retry.” Fresh same-node signed adoption can return
that state to `running` with a rotated fence/token. Exact fresh terminal proof may also settle from
`running`, `terminated`, `verifying`, or `reconciliation_required`; these recovery edges are omitted
from the nominal diagram. A new infrastructure attempt is created only from an exact prior terminal
receipt that proves a retryable engineering failure after confirmed termination.

Terminal disposition is not caller-selected. For a node-signed zero exit, an in-window complete
required-artifact closure is success, a post-deadline exit is timeout, and a safe incomplete or
invalid closure is invalid output. A nonzero exit can never be success; the independent terminal
authority selects and signs the exact permitted typed failure based on its pinned detection policy.
A clean complete run cannot be relabeled as a retryable infrastructure failure to mint another
attempt. The final central `ExecutionReceipt` and exact node receipt are covered by a
deployment-pinned `TerminalVerificationAttestation`; a `verified_by_principal_id` string alone is
never authority to create failure or retry lineage.

Ordinary worker-originated post-reservation mutations require the raw lease token and current
fence. PostgreSQL stores only the token hash. The first successful reservation returns the token
once; idempotent replay returns the same snapshot with no token. A stale token or epoch cannot
start, heartbeat, terminate, verify, or commit an attempt. Two recovery paths deliberately use
different authority: database-clock expiry can move an attempt into retained reconciliation without
a token, and same-node adoption verifies a node-signed proof over the previous token hash/fence
before rotating to a caller-supplied new token. Node registration, signed-inventory ingestion, and
initial admission use their own pinned contracts.

## Node evidence

A deployment-root enrollment binds a node/site/principal, immutable node manifest, and bounded,
expiring node signing key. Node inventory includes boot identity, sequence, monotonic time, resource
classes, health, safety reserve, managed/external occupancy, and allocatable capacity. The allocator
clamps its effective lifetime using PostgreSQL receive time and a deployment maximum TTL.

Runtime inspection and execution receipts bind the exact node, boot, runtime, attempt, inventory,
lease, fence, token hash, monotonic/wall-clock order, exit, termination evidence, and output tree.
Same-node adoption additionally requires fresh running-state inspection and a singleton-lock proof.
`unknown` runtime state never releases authority.

In the PR-4a cut, `QualificationNodeAgent` was an injected facade exercised against protocol fakes;
it was neither an adapter for `PostgreSQLExecutionAllocator` nor a concrete OCI/container runtime.
PR-4b now supplies `PostgreSQLNodeAllocatorAdapter`, `QualificationExecutionWorker`, and
`LocalQualificationOCIRuntime`. The concrete cut is CPU-only and still requires the exact target
Linux/root/systemd/loop/Docker deployment campaign before a deployability claim.

## Artifact workflow

`LocalArtifactStore` requires a non-symlink custody root that is not group/world writable; newly
created roots use mode `0700`. The adapter does not authenticate an existing directory's owner or
reject group/world read bits, so deployment remains responsible for ownership and read-access
privacy. The root contains quarantine, immutable manifest/receipt metadata, and
`sha256/<prefix>/<digest>` objects. The store rejects symlinks, hardlinks, FIFOs/devices, path
escape, renames or mutations during read, undeclared outputs, duplicate keys, and quota violations.
Files are streamed, fsynced, made read-only, and conditionally hard-linked into CAS; existing/final
bytes are reopened and rehashed.

Input resolution reloads the exact AVR and manifest sidecars and rehashes the final CAS object at
each relevant authority observation: first at the issuer's explicit signing time, then again at the
allocator's locked PostgreSQL observation. A WorkOrder-produced input additionally resolves to one
immutable successful producer `ExecutionReceipt` and is checked against its exact producer node and
replicate slot. A standalone receipt object or hash is insufficient.

The qualification issuer performs the same complete resolution before signing the exact receipt
hashes, and the allocator repeats it before reservation. Artifact and producer `verified_at` values
are host/verifier evidence only; they are not used as authorization clocks. PostgreSQL terminal-row
`committed_at` is the retry-ordering authority.

## Database and recovery

Alembic revision `20260825_0024` owns exactly 16 `execution_*` tables: node/inventory/device heads,
qualification admissions, budget authorization/head/reservation/events, execution head/attempt/
adoption state, resource/device leases, immutable terminal receipts, and the transactional outbox.
Application writes occur only through `PostgreSQLExecutionAllocator`; the ORM records are private
and a static boundary rejects a second importer/writer. Admission/reservation and terminal
receipt/outbox settlement are atomic.
The immutable terminal row retains the exact signed terminal-verification attestation and binds it
to the receipt, node evidence, artifacts, policy, principal, and key. The read-only receipt archive
is constructed with the deployment terminal authority and rejects a row-supplied or foreign pin;
stored JSON can never select its own trust root.

After a crash:

- retry the exact admission key; a matching reservation is idempotent and does not reissue its raw
  token;
- if the caller lost the only token after a runtime exists, retain the hold and use exact same-node
  signed inspection/adoption proof rather than minting a second attempt;
- if the token was lost before any runtime identity exists, PR-4a has no honest absence proof and
  deliberately retains the reconciliation hold for a future signed launch/absence contract;
- do not release a timed-out live attempt until a fresh terminated/absent proof exists;
- immutable quarantine/CAS orphans may be garbage-collected only by a separately audited retention
  process; and
- never repair authority tables with ad-hoc SQL. Restore/migration uses the owner credential, and
  the allocator remains disabled until a separately implemented integrity procedure has revalidated
  the restored schema and authority rows; PR-4a does not provide a stream-wide audit API.

Production requires monitored PostgreSQL/host clock health. A backward clock step fails the
monotonic authority checks and pauses commits until time catches up.

If node enrollment/signing authority or the terminal-verification authority expires or is revoked
before terminal verification, PR-4a fails closed and may retain the resource/budget hold. Admission
requires all of those pins to be active and cover the full quoted runtime, but it does not invent
post-runtime grace. A future historical-proof or explicitly bounded terminal-verification grace
policy is required; the allocator does not bypass fresh trust.

## Explicit non-capabilities

PR-4a does not provide:

- Research Kernel action admission or an HTTP execution endpoint;
- scientific observation/claim admission;
- checkpoint resume or ambiguous external-action reconciliation;
- physical/provider actions or network-enabled authored workloads;
- multi-node placement, multi-accelerator attempts, or cross-node runtime adoption;
- a concrete OCI/runtime adapter, allocator-to-agent adapter, or production authority-registry
  composition; or
- protection from a compromised database owner, node agent key, qualification key,
  terminal-verification key, or host root.

See [architecture decision 0048](architecture/0048-qualification-only-local-execution-foundation.md)
for this baseline, [architecture decision
0049](architecture/0049-qualification-only-local-execution-composition.md) and the
[PR-4b composition guide](PR4B_LOCAL_EXECUTION_COMPOSITION.md) for PR-4b, and the
[PR-3 compiler guide](PR3_PROTOCOL_COMPILER.md) for the pure compilation/intent boundary.
