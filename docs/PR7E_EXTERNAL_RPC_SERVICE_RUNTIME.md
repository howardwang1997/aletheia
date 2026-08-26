# PR-7e external RPC service runtime

- Status: server process boundary complete; concrete authority services and target host pending
- Date: 2026-08-26

## What is now runnable

`scripts/run_research_controller_rpc_service.py` starts one byte-pinned external service process.
Its deployment manifest fixes the exact PR-7d worker-side service pin, controller and worker
identity, service and worker UID/GID, socket parent inode/owner/group/mode, receipt-key file,
reviewed factory source, factory configuration, and key-validity interval. The factory returns an
exhaustive `ControllerWorkerRPCHandlerSet`; a missing, duplicate, or additional operation is a
startup failure.

The listener is deliberately Linux-only. It refuses an existing socket path, creates a private
socket before changing it to the exact `0660` shared-group mode, checks server and client
`SO_PEERCRED`, rechecks socket identity on every cycle, and only removes its own unchanged socket at
shutdown. One request is bounded and newline-framed. Its JSON must be canonical and match the exact
controller, worker, service pin, operation, and closed typed payload.

Every operation has one fixed result type. Successful responses are signed with the service's
separate Ed25519 transport-receipt key and bounded before transmission. Proposal, compiler, and
continuation services may return their existing canonical blocker sets; no other operation may
turn a failure into a signed blocker. An invalid request or unexpected domain failure gets no
signed response. Startup and cycle receipts contain operational hashes only and explicitly grant
no scientific authority.

Example invocation on a provisioned Linux service account:

```bash
conda run -n aletheia python scripts/run_research_controller_rpc_service.py \
  --deployment-manifest /etc/aletheia/controller/compiler-rpc-deployment.json \
  --deployment-manifest-sha256 <externally-pinned-sha256>
```

`--once` opens one bounded accept window and is intended for deployment smoke tests. It does not
bypass Linux, UID/GID, socket, key, or source checks.

## Verification performed locally

Focused tests exercise a real worker client against the server request handler, Ed25519 receipt
verification, exact typed result custody, canonical/unknown/rebound request rejection, the limited
signed-blocker vocabulary, wrong result and key rejection, exact handler partitioning, guarded
factory loading, raw `0400` key custody, source drift, unsafe UID overlap, and Darwin fail-closed
startup. Dependency and legacy-inventory gates keep the server core out of the legacy scientific
control plane while retaining the outer process loader under the normalized-AST freeze.

## Remaining release gates

PR-7f supplies the deterministic continuation factory and its fresh assessment-artifact custody;
PR-7g adds a conservative deterministic action-proposal factory with reconstructable powerless
cost/risk receipts and pinned spool custody. This PR still does not supply the other nine concrete
production factories. In particular, knowledge-grounded provider selection, Kernel command
signing, execution/allocator authority, database attestation, F9-v2 validation, admission and
atomic Kernel incorporation still need service-specific
configuration, key custody, PostgreSQL ACLs, and health/alert policy. A source-level handler or a
test key is not commissioned authority.

After those factories exist, an exact Linux target must run the PR-4
root/systemd/rootful-Docker/loop/ext4/cgroup-v2 qualification campaign and the fresh-PostgreSQL
multi-process kill/restart campaign. Until then no host is deployable and these receipts are not a
scientific result or deployment proof.

See [ADR 0062](architecture/0062-operation-closed-external-rpc-service-runtime.md), the
[PR-7d worker guide](PR7D_COMPLETE_CONTROLLER_WORKER.md), and the
[PR-7 runtime guide](PR7_CONTROLLER_PRODUCTION_RUNTIME.md).
