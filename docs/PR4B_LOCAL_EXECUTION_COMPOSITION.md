# PR-4b qualification-only local execution composition

- Status: source/test composition and deployment-evidence contracts complete; target-host
  installation/campaign is unqualified and nondeployable
- Evidence date: 2026-08-25
- Alembic head: `20260827_0026`

PR-4b composes the PR-4a authority foundation into one local, CPU-only engineering-qualification
path. The implementation remains permanently marked `qualification_only=true` and
`scientific_admission_allowed=false`. It is not a Research Kernel launch path, an observation or
claim admission path, a remote/GPU scheduler, or an autonomous experiment controller.

Repository implementation and deterministic/fault-test coverage are not deployment evidence. The
repository now contains a portable desired-state contract, deterministic systemd/PostgreSQL
rendering, five guarded one-role runner entrypoints, and a derived manifest/preflight contract over
an externally pinned observer signature. The runners verify an out-of-band manifest digest before
loading a byte-pinned factory, but no production role factory is supplied by that process-boundary
slice. PR-8b adds an opt-in crash-replayable installer for only the exact manifest and five disabled
unit files. It cannot create principals, apply the PostgreSQL ACL, enable/start units or qualify a
host. The repository still contains no complete target-host commissioning workflow, concrete Linux
observer, frozen manifest instance, or campaign runner, and the target-host campaign has not run. A host is
deployable only after its exact
Linux, rootful Docker, systemd, loop/ext4, mount-namespace, cgroup-v2 and Docker
systemd-cgroup-driver layout, seccomp, AppArmor, image-layout, UID/GID, filesystem,
PostgreSQL-role, and clock configuration has passed the opt-in production campaign described under
**Deployment status**.

## Implemented composition

| Boundary | Concrete implementation | Exact scope and remaining boundary |
|---|---|---|
| Pricing and source budget | `ExactExecutionCostQuoteRegistry`, `SourceBudgetProjectionRegistry`, and `CompositeExecutionAuthorityResolver` | Read-only, detached-Ed25519-verified, deployment-pinned files. There is deliberately no application signing/publishing API; provisioning and key custody remain deployment work. |
| Assignment delivery | `SealedQualificationAssignment` plus `PostgreSQLNodeAllocatorAdapter` | One X25519/AEAD envelope per attempt. Initial delivery decrypts directly into node-local token custody; public DTOs and historical delivery never reveal the raw lease token. |
| Allocator/agent bridge | `PostgreSQLNodeAllocatorAdapter` and `QualificationExecutionWorker` | Uses only public allocator DTOs, translates exact proof-rejection codes, settles an authenticated collected/pending terminal acceptance or adjudicates its signed deadline expiration, and atomically creates the terminal outbox row. It does not publish that outbox row or schedule worker ticks. |
| Input staging | `LocalCASInputMaterializer` | Freshly reopens verified CAS custody, streams and rehashes every declared input, publishes a sealed `0500` exact tree, and emits a typed materialization receipt. It does not fetch remote or network inputs. |
| Output quota | `LoopbackOutputQuotaProvisioningService` and `LoopbackOutputQuotaProvisionerClient` | A narrow root/systemd service provisions one crash-replayable ext4 loop filesystem before input materialization. The minimum requested size is 16 MiB; the exact sector-aligned block capacity is no larger than the request and is bound by `OutputQuotaProvisioningReceipt`. That receipt is trusted-local evidence emitted by the privileged root service, not independent remote attestation. There is no sparse-directory or best-effort quota fallback. |
| Runtime and launch gate | `LocalQualificationOCIRuntime`, `ImmutableOCIImageLaunchGateVerifier`, and `aletheia.execution.qualification_launch_gate` | Digest-pinned Docker image/layout, direct exec, exact workload digest/argv, read-only root, network none, dropped capabilities, no-new-privileges, pinned seccomp/AppArmor, private cgroup namespace, exact CPU/memory/pids limits, read-only inputs, and the quota mount as the only writable workload mount. This PR-4b cut rejects every accelerator/device launch before engine mutation. |
| Deadline enforcement | `DurableDeadlineWatchdogService` and `SystemdDeadlineWatchdogController` | Independently supervised root/systemd watchdog, exact durable job scope, pinned service/unit/module/binary, `cgroup.kill`, and empty-cgroup evidence. There is no in-process timer fallback. Its due-job recovery is a single-threaded inspect loop; `maximum_active_jobs` defaults to 4,096 and rejects a scan above the deployment-pinned value. Hard-real-time kill latency at 4,096 jobs has not been established. |
| Artifact and terminal path | `LocalArtifactStore`, `QualificationNodeAgent`, `PostgreSQLExecutionAllocator`, `QualificationExecutionWorker`, and `QualificationTerminalOutboxService` | Quarantine/CAS rehash, termination challenge/receipt, independently accepted runtime termination, bounded artifact grace, terminal acceptance or pre-signed deadline expiration, and one transactional v2 outbox row. PR-8e retains exact v1/v2 envelopes in a private crash-replayable spool; external consumer delivery and acknowledgment remain separate. |
| Deployment evidence | `QualificationDeploymentSpecV1`, `render_systemd_units`, `render_postgresql_acl`, `SignedQualificationLinuxDeploymentObservation`, `QualificationInstalledDeploymentManifestV1`, and `verify_installed_manifest` | Portable desired state closes reviewed code/Python/native-dependency trees, service identities, exact PostgreSQL objects/ACL closure, host/runtime pins, and an external observer key. Only a real Linux observation may be frozen; revalidation returns eligibility for a later opt-in campaign, never a deployment or scientific verdict. Installation, observer implementation, and campaign execution remain external. |
| Service process boundary | `QualificationServiceDeploymentManifestV1`, `QualificationServiceRuntime`, and five thin `scripts/run-*.py` entrypoints | Each process exposes one role/operation, verifies canonical manifest/source/config bytes and live Linux UID/GID, and emits only non-authoritative operational diagnostics. PR-8c/PR-8d/PR-8e supply all five source factories; commissioned credentials remain absent. |
| Disabled file installation | `QualificationInstallationRequestV1`, `LinuxQualificationInstallationHost`, and `scripts/install-qualification-deployment.py` | Dry-run by default; explicit root/Linux opt-in atomically publishes the exact manifest and five units with append-only crash recovery, invokes only pinned daemon-reload, and proves every unit remains disabled/inactive. Principals, configs/keys, PostgreSQL ACLs, activation, observer and campaign stay external. |

There is no complete commissioned target-host composition instance or workflow that
provisions identities and registry files, creates/restricts PostgreSQL roles, configures mount
propagation, or starts/supervises the worker. PR-8b can install only the manifest/disabled unit file
subset. There is a closed schema for deriving a manifest from signed live
evidence, but no target-host manifest instance has been frozen.
`QualificationExecutionWorker` closes the database/node/terminal application path and
`LocalQualificationOCIRuntime` accepts the concrete gate/quota/watchdog controllers, but the whole
real root-service + Docker + PostgreSQL path is not established merely by constructing those
objects. Deployment must supply and validate that wiring as one exact manifest/campaign.

The concrete path is one local node and CPU-only. A request may reserve multiple CPU cores within
the signed inventory and exact cgroup limit; “CPU-only” does not mean a one-core limit. Although
the PR-4a allocator contracts can represent at most one accelerator, this composed runtime rejects
device launch and recovery. Multi-node placement, GPU access, checkpoint resume, provider actions,
and cross-node adoption remain out of scope.

## Authoritative schema

The current Alembic head is `20260827_0026` and owns exactly 27 `execution_*` tables:

- `20260825_0024` creates the 16-table PR-4a foundation: `execution_nodes`,
  `execution_inventory_attestations`, `execution_inventory_devices`, `execution_device_heads`,
  `execution_qualification_admissions`, `execution_budget_authorizations`,
  `execution_budget_heads`, `execution_heads`, `execution_attempts`,
  `execution_attempt_adoptions`, `execution_resource_leases`, `execution_device_leases`,
  `execution_budget_reservations`, `execution_budget_events`, `execution_terminal_receipts`, and
  `execution_outbox`;
- `20260826_0025` adds exactly one `execution_assignment_envelopes` table. Upgrade requires an
  empty PR-4a attempt store so an old attempt can never be retrofitted with invented assignment
  custody; and
- `20260827_0026` adds exactly ten append-only runtime-v2 tables:
  `execution_runtime_preparations`, `execution_runtime_launch_authorizations`,
  `execution_runtime_launch_receipts`, `execution_pre_runtime_absence_decisions`,
  `execution_runtime_fence_rebinds`, `execution_runtime_termination_challenges`,
  `execution_runtime_termination_acceptances`,
  `execution_qualification_terminal_deadline_expirations`,
  `execution_qualification_terminal_acceptances`, and
  `execution_qualification_terminal_outbox`.

The v1 terminal receipt/outbox tables remain immutable historical authority for PR-4a attempts;
runtime-v2 termination or artifact evidence is never rewritten into a legacy `ExecutionReceipt`.
The v2 tables form their own exact chain and the attempt head contains only its current bound
hashes/counters.

## Lifecycle and crash semantics

The composed happy path is:

```text
signed qualification + exact registries + fresh inventory
  -> atomic reservation + one sealed assignment
  -> output quota provision -> input materialization -> inert runtime preparation
  -> short DB-clock launch authorization
  -> watchdog arm + in-container launch-gate verification
  -> Docker create/start + node launch receipt + allocator start acceptance
  -> running/heartbeat
  -> termination challenge + node termination receipt
  -> accepted runtime termination (resource and budget holds released)
  -> artifact quarantine/verification during bounded grace
  -> accepted terminal submission OR terminal-deadline expiration
  -> exactly one execution.qualification_terminal.v2 outbox row
```

Before the first execution-head insert, admission takes transaction-scoped PostgreSQL advisory
locks for both the execution ID and scientific replicate-slot ID in one fixed, private order. This
linearizes exact concurrent retries and crossed identity conflicts before PostgreSQL touches the
primary-key and replicate-slot unique indexes; one caller may mint the raw lease token, while every
exact follower reloads only committed hashed/sealed custody. The database clock is sampled after
the wait, so contention cannot carry an expired authority window into a reservation.

The required crash protocol gives each irreversible local step a durable
intent/pending/completed record. A conforming replay must return the same bytes or fail closed; it
cannot mint a replacement generation merely because a process died. The frozen source and focused
tests close the reviewed security-A crash cases and cover the following phases. That source/test
closure is not proof of every kernel/host failure mode or a substitute for target-host validation:

- quota provisioning records backing-file creation, sizing, loop attachment, filesystem format,
  mount identity, ownership seal, and receipt publication. A crash after any phase resumes the same
  generation, including zero-length backing-file creation and mount-before-receipt windows;
- preparation is inert. Docker create/start is impossible until the allocator issues a short-lived
  authorization over the exact preparation, absence epoch, placement, OCI configuration, staged
  input receipt, quota receipt, fence, and token hash;
- output-mount generation checks bracket both engine mutations. A mismatch after create removes
  the exact CREATED/PID0 container; a mismatch after start kills the exact running container. A
  mismatch in the final guard immediately before start instead fails closed before the start
  mutation and deliberately retains the exact CREATED/PID0 generation for durable never-started
  cleanup; it is not reported as immediately killed or removed;
- an exact never-started/created-with-PID-zero proof may authorize cleanup and either release or a
  replacement launch generation. At or after the hard deadline, including after the artifact
  submission grace has elapsed, historical pre-runtime recovery remains deliverable only to
  finish cleanup/release; it cannot prepare or launch work. That liveness rule does not extend the
  bounded recovery window for a runtime that actually launched or for terminal artifacts;
- a submitted Docker start is still eligible for pre-workload cleanup only when its immutable
  historical inspection proves that the pinned launch gate itself began at or after ticket expiry,
  exited with its reserved rejection code `126`, has PID zero and no restart. Before removal the
  root watchdog and node cleanup each revalidate the same container ID/name, exact state/timestamps
  and exhaustive frozen OCI enforcement semantics, including a fresh seccomp-copy hash. Unrelated
  current Docker metadata may drift without replacing the historical full-inspection evidence.
  This does not create a runtime identity, engineering success, or scientific evidence;
- after an engine mutation may have started a process, absence is not inferred. An ambiguous or
  unknown runtime retains reconciliation and its evidence; it never releases authority or starts a
  duplicate;
- an already-started runtime is recovered from the durable actual-start window and historical
  launch proof, not a newly live ticket. Same-node adoption requires fresh running inspection plus
  the singleton lock, rotates the lease token/fence exactly once, and crash-replays the same runtime
  fence rebind. Cross-node adoption is forbidden;
- the independent watchdog remains armed across node-agent death and kills the exact overdue
  container cgroup. A caller-authored terminal event cannot turn a live or ambiguous process into
  accepted termination;
- accepted runtime termination is the point at which compute/budget holds are released. The
  execution head remains active through the independently bounded artifact-submission window; and
- artifact completion and no-artifact deadline expiration are mutually exclusive terminal
  authorities. Settlement is idempotent and creates one outbox row in the same transaction. A
  worker crash after commit replays the existing acceptance/outbox, not a second settlement.

The signed records retain their exact authority pins and content. Historical verification compares
those records with constructor/deployment-pinned trust roots; a key or pin serialized in a database
row never promotes itself into authority. Historical recovery grants are recovery-only and cannot
authorize a new launch. Live launch still requires currently active qualification, node, transport,
runtime-control, registry, and custody material over the required time windows.

## Database role and external services

SQL triggers and closed JSON checks defend invariants, but PostgreSQL does not verify Ed25519
signatures or recompute every Python canonical hash. Deployment therefore must:

- keep the owner/migrator credential offline from the allocator and workload;
- give the dedicated allocator application principal only the exact `SELECT`/`INSERT`/`UPDATE`
  privileges required by the reviewed allocator, with no table ownership, `DELETE`, `TRUNCATE`,
  DDL, trigger/function mutation, or interactive/raw-SQL exposure;
- give no database, object-store, Docker-socket, node-key, quota-service, or watchdog credential to
  the authored workload; and
- disable the allocator after restore until schema and authority rows have been independently
  revalidated. Never repair an authority row with ad-hoc SQL.

PR-8e now supplies the source-level private-spool dispatcher and PR-8d supplies the node loop, but
there is still no commissioned external spool consumer, service manager, CAS/quarantine garbage
collector, backup/restore verifier, or PostgreSQL/host clock monitor. Those are required deployment
components. The external consumer must use `delivery_key` idempotently and must not delete retained
spool authority; supervision must keep node/terminal-deadline/outbox ticks alive; GC must use an
audited retention/reachability policy; and a backward or unhealthy PostgreSQL clock must pause
authority commits rather than be papered over with host time.

## Deployment TCB and pathname-bind limit

The supported threat model requires all of the following:

- one dedicated, uncontainerized and exclusively reserved node-service UID, plus a dedicated GID;
  no unrelated or user-controlled host process may run under that UID/GID;
- root-owned, pinned systemd unit/executable/module files; a root-only quota provisioner; the
  rootful Docker daemon; and an independent root watchdog;
- a mount namespace/propagation arrangement in which the quota service, node/runtime process, and
  Docker daemon observe the same provisioned output mount generation;
- a root-owned sticky `01730` workspace root, a root-sealed attempt parent, fixed-component
  `openat(..., O_NOFOLLOW)` traversal, and an owner-only `0700` output mount; and
- a workload that, of the host output workspace, sees only the bound output root, cannot traverse
  its host parent, and has neither `CAP_SYS_ADMIN` nor a Docker socket or host credential.

The runtime reopens the exact mount/device/inode generation around Docker create and start. A
post-create mismatch removes the exact CREATED/PID0 container; a post-start mismatch kills the exact
running container; and a final pre-start-guard mismatch fails closed while retaining the exact
CREATED/PID0 generation for durable never-started cleanup. Docker accepts a host pathname rather
than an already-open file descriptor. Therefore this design does **not** resist an adversarial peer
with the same host UID, host root, a compromised quota/watchdog service, a compromised Docker
daemon, or root mount/remount authority. The exclusive node UID and root/mount-namespace controls
are required TCB assumptions, not properties proved by a Pydantic contract or unit test.

## Deployment status

The repository has deterministic unit/fault coverage for authority binding, raw-DML guards,
assignment/token custody, input materialization, allocator/runtime lifecycle, mount-generation
rechecks, quota/watchdog crash journals, adapter translation, terminal settlement, and concurrent
PostgreSQL deadline adjudication. The 2026-08-24 independent source/test checkpoint reported
security review A=0. Its combined non-PostgreSQL slice passed 228 tests with five deselected; the
deployment/runtime slice passed 133/133; the race slice passed 10/10; and the full
`tests/execution` suite passed 352 tests with 68 skipped. These overlapping counts are separate
views of the same frozen code, not numbers to add together. They validate contracts and failure
handling, not the host's production configuration.

The 2026-08-25 deployment-evidence closure added 149 focused tests for closed desired state,
deterministic unit/ACL rendering, exhaustive reviewed code and native dependency identity, signed
node/custody-root identity, effective node poll configuration, loaded/enforcing AppArmor policy,
PostgreSQL role/object/grant/routine-owner closure, signed observer provenance and time bounds,
reboot-safe versus same-boot drift, fail-closed freeze, and Darwin refusal. With `PYTHONPATH=.` the
complete `tests/execution` suite passed 501 tests with 68 skipped, and the
migration/dependency/schema slice passed 191 tests. These are source-contract results only; they do
not increase deployment status.

At the earlier 2026-08-24 pre-closure checkpoint, after final formatting and metadata-test
isolation, the repository-wide suite passed 2,267 tests with eight skipped. Two Docker tests that
initially encountered a stale Colima client/container closeout were rerun independently before that
clean pass; no timeout result is counted as acceptance evidence. This historical count does not
include the 149 deployment-evidence tests above; a new repository-wide count is not claimed by this
focused closure checkpoint.

Migration/SQL validation used the dedicated database `aletheia_pr4b_root_dev_8241`: head
`0026 -> 0025 -> 0026` round-tripped successfully, `alembic check` reported no new operations, and
the allocator/runtime-v2/red-team/schema slice passed 77 tests. The PostgreSQL test safety boundary
requires an explicit `ALETHEIA_DATABASE_URL`, a loopback PostgreSQL host, an
`aletheia_pr4*` database name, and an exact configured-engine match before destructive fixtures can
run.

The PR-4b closure run used the dedicated loopback PostgreSQL database
`aletheia_pr4b_adapter_final_20260824_01`, freshly upgraded to `20260827_0026`; it did not use the
default or another agent's database. The adapter slice passed 21 tests, including five real-
PostgreSQL cases. The real database cases cover authenticated start/launch/recovery, terminal
settlement across crash points and the artifact deadline, commit-return deadline recovery,
foreign-node rejection, two concurrent workers producing one exact outbox row, and
post-hard-deadline prelaunch cleanup with zero workload launch. They do not exercise a real root
quota/watchdog service or target-host Docker mount namespace.

Five guarded source runner entrypoints now exist, every rendered `ExecStart` carries the exact
deployment-manifest SHA-256, and an explicit installer can publish only those disabled files.
All five processes now have checked-in factories. PR-8f adds the first disabled-only commissioning
stage for exact Linux principals, PostgreSQL peer URLs and empty custody roots, but that stage has
not run. The complete config/key/ACL workflow, concrete observer, frozen manifest instance and
campaign runner still do not exist at this checkpoint. `QualificationInstalledDeploymentManifestV1` is a derived schema, not evidence
that any installation was observed. Until an opt-in campaign runs as root on the exact target Linux host with its real systemd units,
rootful Docker daemon, shared mount visibility, loop/ext4 tools, cgroup-v2 hierarchy, pinned OCI
layout/image, Docker's pinned systemd cgroup layout, seccomp/AppArmor profiles, dedicated UID/GID,
and restricted PostgreSQL role, PR-4b
must be described as an implemented qualification composition with deployment validation pending,
not as a deployable or production execution service. A Docker daemon reachable from a non-Linux
development host, containerized/mocked systemd, or unit tests that monkeypatch root/kernel evidence
do not satisfy this gate.

At this evidence checkpoint the controlling process is Darwin. A Colima Ubuntu VM exposes a
rootful Linux Docker daemon and cgroup v2, but it does not prove the target host's PID 1 systemd,
real root loop/ext4 services, or shared quota-service/node/Docker mount namespace. Consequently the
full exact target-host campaign was not run, and the reachable VM/daemon is not deployment evidence.
PR-4b is therefore explicitly **nondeployable** at this checkpoint.

## Explicit non-capabilities and next boundary

PR-4b does not provide an HTTP launch endpoint, a Research Kernel action-to-execution grant,
scientific observation/claim admission, a durable scientific controller, distributed scheduling,
GPU/device execution, checkpoint resume, ambiguous provider-action reconciliation, network-enabled
authored work, or protection from the TCB principals above. The subsequent PR-5 slice had to add a
signed Research Kernel action bound to the exact qualification/execution bundle and an independent
observation-admission bridge before this fabric could participate in a scientific Quest. That local
source/test bridge is now present, but it does not upgrade PR-4b's deployment status; target-host
qualification and production controller/validator composition remain separate gates.

The proposed discovery-episode objects in the target architecture remain later evaluation work:
`DiscoveryEpisodeProjection` must be a pure, recomputable, read-only view over authoritative events
and receipts, and `DiscoveryEpisodeAssessment` must remain evaluator-only. Neither object changes
PR-4b authority or becomes a second research ledger. With the PR-5 local bridge present, that slice
is now eligible as later evaluation work, not part of PR-4b or PR-5 authority.

See [architecture decision 0049](architecture/0049-qualification-only-local-execution-composition.md)
for the decision and threat model and the
[PR-4a foundation guide](PR4_LOCAL_EXECUTION_FOUNDATION.md) for the frozen baseline.
