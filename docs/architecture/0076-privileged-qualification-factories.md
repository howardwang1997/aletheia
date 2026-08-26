# Architecture decision 0076: Privileged qualification service factories

- Status: accepted for the PR-8c source-composition slice
- Date: 2026-08-27
- Scope: workspace, loopback quota and independent watchdog processes

## Context

PR-8a created a guarded one-role process boundary and PR-8b could install its manifest and disabled
units. The three privileged unit entries still named factory modules that did not exist. The quota
and watchdog implementations were already closed Linux services; the shared workspace setup had no
equivalent exact, replayable implementation.

Embedding the final process identity in its own config is not possible: that identity contains the
config file digest. Treating a pre-bind render or a successful `mount` exit code as live mount
evidence would also create false authority after a crash or pathname substitution.

## Decision

- Define one config-binding projection that excludes only the derived process id and the config
  digest. Freeze the resulting canonical config bytes back into the final process identity.
- Parse every root-service config as duplicate-free, canonical JSON and bind it to the exact
  deployment, factory source, role, operation, principal and config custody projection.
- Reuse the existing quota and watchdog services without adding a generic callback or dispatch
  surface.
- Add a root/systemd-only shared workspace one-shot. Require exact pre-created source and empty
  underlay pins; invoke only the pinned mount executable; then independently re-read mountinfo and
  inode custody.
- Treat an exact bind without a shared propagation marker as the sole recoverable intermediate
  state. Promote it and re-observe; never unmount, replace a foreign mount or create a directory.
- Keep all three services qualification-only and incapable of loading database credentials,
  private keys or scientific-admission authority.

## Consequences

The installed manifest can now name real workspace, quota and watchdog factories, and the
workspace operation can resume across the bind/propagation crash boundary without guessing. This
does not prove those bytes, configs or Linux states exist on any host. Node and outbox factories,
commissioned principals/config/key custody, PostgreSQL ACL application, the concrete observer and
the full target-host campaign remain mandatory follow-on gates.
