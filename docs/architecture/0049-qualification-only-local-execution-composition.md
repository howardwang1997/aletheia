# Architecture decision 0049: Compose one CPU-only qualification runtime

- Status: Accepted for PR-4b source/test composition; security review A=0; target-host deployment
  unqualified and nondeployable
- Date: 2026-08-24
- Scope: sealed assignment delivery, local OCI isolation, runtime-v2 recovery, and terminal composition

## Decision

PR-4b composes the PR-4a allocator and custody boundary only for one local CPU-only engineering
qualification runtime. PR-4b authority models retain the literal qualification-only boundary;
every authority-bearing contract that exposes the fields fixes `qualification_only=true` and
`scientific_admission_allowed=false`.

The runtime uses a deployment-pinned, digest-verified Docker/OCI image and a minimal in-container
launch gate. It accepts only direct execution of the exact pinned workload, network none, a
read-only root, read-only staged inputs, one loop-backed quota mount as the only writable workload
mount, exact CPU/memory/pids cgroup-v2 limits, dropped capabilities, no-new-privileges, and pinned
seccomp/AppArmor policy. Device/GPU launch fails before any engine mutation in this cut.

Assignment delivery is one attempt-bound X25519/AEAD envelope. The node decrypts the initial raw
lease token directly into durable local custody; later DTOs and recovery delivery expose only its
hash. Runtime preparation is inert and durable. Each Docker create/start generation needs a fresh,
short-lived database-issued authorization bound to the preparation, staged inputs, output quota,
placement, OCI configuration, fence, lease-token hash, and pre-runtime absence epoch.

Runtime-v2 termination is deliberately separate from the legacy `ExecutionReceipt`. Fresh accepted
termination releases compute/budget holds, while the attempt remains active through a bounded
artifact grace window. Exact artifact acceptance or a pre-signed no-artifact deadline expiration
then becomes the one terminal authority and is atomically paired with one v2 outbox row.

## Recovery decision

Local irreversible phases use fsynced intent/pending/completed journals and replay the same
generation. A never-started generation requires exact absence evidence; once an engine mutation may
have started work, unknown is retained and cannot be converted into absence. An actual start is
recovered from historical ticket/start evidence. Same-node adoption requires fresh running
inspection and a singleton lock, then crash-idempotently rotates both allocator and runtime fences.
Cross-node adoption is forbidden.

A deployment-pinned root/systemd watchdog owns hard-deadline enforcement independently of the node
agent. It binds the exact container and durable launch scope, uses cgroup-v2 kill, and requires an
empty-cgroup proof. Caller-authored terminal events, Docker status strings alone, or elapsed host
timers cannot release authority.

Output-mount generation checks bracket create and start with deliberately different outcomes. A
post-create mismatch removes the exact CREATED/PID0 container, and a post-start mismatch kills the
exact running container. A mismatch in the final guard immediately before start fails closed before
that mutation and retains the exact CREATED/PID0 generation for durable never-started cleanup; the
runtime does not claim that branch immediately killed or removed it.

## Schema decision

The execution schema at Alembic head `20260827_0026` contains exactly 27 tables: 16 from the PR-4a
foundation (`0024`), one sealed-assignment table (`0025`), and ten append-only runtime-v2 tables
(`0026`). Upgrade to `0025` requires an empty attempt store; old attempts are never retrofitted with
invented token-delivery evidence. Stored JSON pins are evidence, not self-selected trust roots.

## Trust and deployment boundary

The supported TCB includes the restricted allocator process/role, PostgreSQL and its healthy clock,
the enrolled node and its exclusive uncontainerized UID/dedicated GID, node and runtime-control
keys, pinned authority/CAS registries, root-owned deployment files, the root-only quota provisioner,
rootful Docker daemon, independent root watchdog, and their shared view of the provisioned mount.
The workload receives none of those credentials and cannot access the host workspace parent or
`CAP_SYS_ADMIN`.

`OutputQuotaProvisioningReceipt` is trusted-local evidence from that privileged root service, not
independent remote attestation. The watchdog's due-job recovery is a single-threaded inspect loop
whose `maximum_active_jobs` defaults to 4,096 and rejects a scan above the deployment-pinned value;
hard-real-time enforcement latency at 4,096 jobs is not established by the present tests.

Docker binds a host pathname, not an already-open descriptor. Pre/post mutation generation checks,
root sealing, sticky-parent custody, and emergency kill reduce replacement windows but do not defend
against an adversarial same-UID host process, host root/remount authority, or a compromised root
service/Docker daemon. Exclusive UID/GID and shared mount-namespace configuration are therefore TCB
requirements, not optional hardening.

PostgreSQL constraints cannot verify Ed25519 signatures or recompute every application canonical
hash. The owner/migrator credential must be kept offline; the allocator application role must have
only reviewed `SELECT`/`INSERT`/`UPDATE` access and no ownership, DDL, `DELETE`, `TRUNCATE`, trigger or
function mutation, interactive raw-SQL exposure, or workload access. PR-8e subsequently supplied a
source-level durable private-spool dispatcher. Production still requires a commissioned idempotent
external spool consumer, worker/deadline/outbox supervision, audited CAS GC, backup/restore
procedure, and database/host clock monitoring.

## Consequences

- PR-4b is still a qualification-only execution substrate. It cannot launch a scientific Quest or
  admit an observation/claim.
- CPU-only means no accelerator/device; it does not impose a one-core limit. Multi-node placement,
  GPU execution, checkpoint resume, external/provider actions, and cross-node recovery remain
  disabled.
- A target host is not deployable merely because unit, simulated-kernel, PostgreSQL, or ordinary
  Docker tests pass. The exact opt-in Linux/root/systemd/loop/ext4/rootful-Docker campaign must pass
  on that deployment, including the pinned systemd cgroup-v2 layout and shared mount visibility.
  This repository currently has neither a target-host installer nor a frozen deployment-manifest
  instance, and that exact campaign has not run; PR-4b is nondeployable at this checkpoint.
- The PR-5 local bridge does not change this deployment verdict. Discovery-episode
  projection/assessment remains pure derived, evaluator-only work and cannot become a second
  authority ledger.

See the [PR-4b composition guide](../PR4B_LOCAL_EXECUTION_COMPOSITION.md) for the implementation map,
exact schema inventory, lifecycle, operating requirements, and deployment evidence boundary.
