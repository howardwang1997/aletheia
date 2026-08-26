# Architecture decision 0062: Operation-closed external RPC service runtime

- Status: accepted for the external-service process boundary
- Date: 2026-08-26
- Scope: server side of the PR-7d controller-worker RPC protocol

## Decision

Each external controller service runs as one independently supervised Linux process. A process may
serve the small related operation set in one `ControllerWorkerRPCServicePin`, but it cannot add an
operation at runtime or accept a generic method name. The server validates canonical JSON and a
closed Pydantic payload for every one of the fourteen protocol operations before invoking its
operation-specific handler. The handler result must have the one fixed result type for that
operation. Unknown fields, duplicate/non-canonical bytes, controller/worker/service rebinding,
operation drift, asynchronous callbacks, and result-type substitution fail before a response is
signed.

The runtime manifest pins the worker principal and Linux peer UID/GID, service UID/GID, worker-side
service pin, shared socket group, socket-parent inode/custody, reviewed code root, exact factory and
configuration bytes, and one raw Ed25519 transport key file. The key file must be a regular,
single-link, service-owned `0400` file outside reviewed source. Its public key must derive to the
worker pin. The listener is Linux-only, requires `SO_PEERCRED` in both directions, creates a `0660`
socket initially inaccessible under a restrictive umask, and refuses to replace a pre-existing
path. Shutdown removes the path only when its device/inode still identify the socket created by
that process.

A successful result or an explicitly supported blocker receives a canonical signed transport
receipt. Only proposal, protocol-compilation, and continuation operations may convert their
existing typed non-retryable domain blockers into the signed blocker variant. Malformed requests
receive no signed response. Unexpected handler/store failures also receive no signed scientific
interpretation and terminate the cycle so the supervisor can restart the process.

The transport receipt does not authorize execution, validate an observation, admit a scientific
slot, or sign a Research Kernel command. Domain handlers must still verify and persist their own
signed objects and implement durable first-writer replay. The generic server deliberately provides
no in-memory idempotency cache and no second authority ledger.

## Consequences

- The eleven PR-7d client endpoints now have a checked-in, byte-pinned server and CLI process
  boundary that can be supervised and targeted by later kill/restart campaigns.
- A worker process never receives the transport private key or a domain private key. Separate
  service UIDs and a shared socket GID are mechanically required by the manifest.
- PR-7f through PR-7p subsequently provide byte-pinned composition factories for all eleven
  operation families. This decision and those source slices do not commission their keys,
  PostgreSQL roles, service accounts, ACLs, or provider credentials.
- Darwin tests exercise request/response, signature, loader, and fail-closed contracts only. A real
  Linux target must still prove socket ACLs/peer credentials, systemd restart, PostgreSQL recovery,
  and process-kill behavior.
