# ADR 0077: compose the qualification node from exact custody and typed authorities

- Status: Accepted
- Date: 2026-08-27

## Context

The guarded node runner existed, but its factory did not. A permissive factory would be especially
dangerous because this one process needs PostgreSQL mutation access plus three unrelated private
keys while also controlling a local OCI runtime. Configuration echoes or a generic signer would
collapse the node, assignment-transport and allocator/runtime-control trust domains.

The OCI runtime also required an independent output-quota verifier. The root quota daemon could
create the loop filesystem, but its socket exposed only provisioning; passing the unprivileged
provisioning client as the runtime verifier would fail at the first real launch.

## Decision

Use one canonical, process-bound node config and fail closed unless all of the following are exact:

1. the configured database URL digest matches the process environment, the source tree has one
   expected Alembic head, and the live database reports the pinned allocator role and exactly that
   revision;
2. the enrolled node, qualification/terminal/runtime-control public pins and pricing/budget
   registries are active and pairwise role-separated;
3. node-signing Ed25519, assignment X25519 and runtime-control Ed25519 key files are distinct,
   `0400`, node-owned, parent-chain pinned and re-derived against their public authorities;
4. all four mutable node roots are exact pre-existing `0700` inode pins, and every root/key remains
   separate from reviewed code, config and privileged service custody;
5. one CPU-only launch spec agrees with the enrolled manifest and complete OCI/image policy;
6. quota and watchdog sockets admit only the frozen node UID/GID; and
7. the runtime-control key is reachable only through the typed issuance port.

Extend the existing quota socket with one exact `verify` request. The root daemon runs the existing
live loop/mount verifier and returns only the expected evidence digest. Extra fields, noncanonical
JSON, a non-root server peer, a different deployment digest or any changed evidence are rejected.

## Consequences

The checked-in node factory is restart-safe and can execute already registered, inventoried,
qualification-only attempts without gaining scientific authority. The price is an intentionally
narrow first deployment: one CPU launch spec, four pre-created mutable roots, three separately
commissioned key files and a live PostgreSQL role/revision check are mandatory.

This decision does not claim that a host has those objects. Principal creation, ACL application,
node inventory publication, terminal-outbox composition and the target-host campaign remain
separate gates.
