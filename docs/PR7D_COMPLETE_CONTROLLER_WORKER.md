# PR-7d complete keyless controller worker factory

- Status: source composition complete; external services and target host uncommissioned
- Date: 2026-08-26

## What is now wired

`aletheia.research_controller_worker_runtime.build_worker_runtime` is a checked-in factory accepted
by the byte-pinned PR-7a loader. It constructs one `worker` role with:

- the existing PostgreSQL recovery adapter;
- the public-key-only PR-4 terminal lineage reader;
- a read-only Research Kernel CAS/audit store;
- the exhaustive PR-7b adapter set for the three proposal steps, protocol compilation, execution
  registration, independent validation, atomic admission/incorporation, and continuation; and
- eleven operation-closed Unix RPC clients whose signed transport receipts are independently
  verified before a typed result reaches an adapter.

No connection is opened while configuration is parsed or composed. A service is contacted only
when the deterministic recovery plan selects its exact operation. Repeated requests have the same
request identity, allowing the external service to return its durable first-writer result. Signed
provider/compiler/assessor blockers preserve the controller's existing non-retryable blocked
disposition; transport faults do not masquerade as those blockers.

## Deployment configuration

The outer runtime manifest continues to pin the controller manifest, factory source, and complete
configuration bytes. The worker configuration additionally freezes:

- controller/worker/process identities and schema/database pins;
- the exact eight adapter manifests and per-step source/config hashes;
- an exhaustive partition of fourteen RPC operations across eleven service pins;
- socket owner/group/mode and required Linux peer credentials;
- service principal, policy, manifest, authority closure, receipt public key, validity interval,
  timeout, and request/response bounds; and
- the Research Kernel public trust root/read-only CAS plus all public terminal-verification roots.

The worker configuration contains no scientific private-key field. Unknown JSON fields, duplicate
keys, operation overlap, missing operations, authority rebinding, endpoint/key reuse, writable CAS
mode, stale source bytes, or deployment identity drift fail before the worker begins a lease.

## Verification performed locally

Focused tests cover deterministic request replay, canonical response enforcement, request/service
rebinding, signature tamper, unknown private-key fields, endpoint operation partitioning, authority
closure, all eight composed adapters, source hash drift, duplicate configuration keys, guarded
factory loading, reusable terminal-reader composition, and read-only CAS behavior. The broader
controller suite continues to exercise every step's domain-level receipt and recovery checks.

These are source and local engineering tests. They do not prove that a target socket belongs to an
independent production process, that a private key is absent from worker memory, that PostgreSQL
ACLs are least privilege, or that the system survives real process/host failure.

## Remaining release gates

PR-7e now supplies the common byte-pinned Linux server runtime for these endpoints, including
closed payload/result dispatch, service/worker `SO_PEERCRED`, distinct UIDs, shared socket GID, and
service-owned receipt-key custody. Before deployment, compose and freeze the eleven concrete
authority factories and start them under reviewed OS principals. Then run the PR-4
Linux/rootful-Docker/systemd/loop/ext4/cgroup-v2 campaign and a fresh-PostgreSQL multi-process
campaign covering dispatcher, worker, service, database, and reconciler kill points. The campaign
must demonstrate exact retry, lease takeover, source redrive, one scientific-slot admission,
read-only CAS/terminal access, socket peer/ACL enforcement, and recovery from authoritative bytes.

See [ADR 0061](architecture/0061-keyless-controller-worker-composition.md), the
[PR-7e external-service guide](PR7E_EXTERNAL_RPC_SERVICE_RUNTIME.md), the
[PR-7 runtime guide](PR7_CONTROLLER_PRODUCTION_RUNTIME.md), the
[PR-7b authority guide](PR7B_CONTROLLER_STEP_AUTHORITY_BOUNDARY.md), and the
[PR-7c terminal guide](PR7C_VERIFIED_TERMINAL_DISPATCHER.md).
