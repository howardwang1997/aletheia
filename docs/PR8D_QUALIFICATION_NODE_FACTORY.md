# PR-8d qualification node factory

- Status: node-side source composition complete; exact generation h later qualified on target
- Date: 2026-09-05

## Closed source surface

PR-8d supplies the checked-in factory named by the non-root `node` entry in the five-process
qualification manifest.

- A canonical config binds the guarded process projection, database URL digest, exact PostgreSQL
  role and single Alembic revision, one deployment-enrolled node, its public engineering
  authorities, one CPU-only launch specification, the OCI/image policy, and both root-service
  clients.
- Three distinct node-owned `0400` raw private-key files are pinned by path, bytes, owner, group,
  parent chain and derived public identity. The Ed25519 node key signs node evidence, the X25519
  key decrypts only assignments for the exact node manifest, and the independent Ed25519
  runtime-control key can issue only the six typed runtime-control contracts. No generic signing
  callback or raw key is exported.
- Artifact, node-state, input-materialization and OCI-runtime roots are pre-provisioned `0700`
  directories pinned by path, device, inode, UID/GID, mode and parent chain. They are re-opened and
  checked before any private key is loaded.
- Startup fresh-checks authority validity, derives all three public keys again, reads the current
  Linux boot ID, and assembles the existing PostgreSQL allocator, encrypted assignment adapter,
  durable node state, CAS materializer/quarantine, immutable launch gate, quota/watchdog clients,
  OCI runtime, node agent and terminal-settling worker.
- The worker loop performs one independently recoverable tick at a time and uses only the poll
  interval frozen into the service manifest. A process crash therefore resumes from PostgreSQL,
  node-local journals and runtime evidence rather than an in-memory checkpoint.
- The quota daemon protocol now has an exact `verify` operation in addition to `ensure`. The
  unprivileged runtime asks the same root process to freshly validate the live loop device,
  backing file, mount identity and quota evidence. Both request and response are canonical,
  operation-closed and bound to root `SO_PEERCRED`.

The config keeps qualification signing, terminal-verification signing, Kernel signing and
scientific admission unavailable. Workloads still receive no database credential, Docker socket,
artifact-store credential, node key or host environment.

## Explicit remaining gates

This PR does not create the node principal, directories or keys; install configs; grant the
PostgreSQL allocator role; register/refresh node inventory; enable or start systemd units; or prove
one real Docker execution. PR-8e later implemented the terminal-outbox source factory. The
remaining commissioning work is not evidence produced by a Python unit test.

The node factory currently admits exactly one CPU-only launch specification. GPU device discovery,
host scheduler integration and signed live inventory remain outside this local qualification
deployment. Exact generation `20260904h` later passed the
Linux/root/systemd/loop/ext4/rootful-Docker/PostgreSQL process-kill campaign for the frozen CPU-only
service. That result does not extend to GPU, another release, or scientific admission.

See [architecture decision 0077](architecture/0077-qualification-node-factory.md), the
[PR-8c root factory guide](PR8C_PRIVILEGED_QUALIFICATION_FACTORIES.md), and the
[PR-4b deployment guide](PR4B_LOCAL_EXECUTION_COMPOSITION.md).
