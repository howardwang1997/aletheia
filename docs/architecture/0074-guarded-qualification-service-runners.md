# Architecture decision 0074: Guarded qualification service runners

- Status: accepted for the PR-8a source-runtime slice
- Date: 2026-08-27
- Scope: the five qualification-only service process entrypoints rendered by PR-4b

## Context

The PR-4b desired-state renderer emitted five systemd units whose `ExecStart` values named runner
files that were not present in the repository. The command line passed only a manifest pathname,
even though the deployment spec already carried an exact manifest digest. No target installer
could honestly start those units, and accepting an unpinned pathname at process startup would
create a time-of-check/time-of-use gap.

## Decision

- Add one thin checked-in runner per workspace, quota, watchdog, node and outbox role.
- Compile one role into each entrypoint and accept only its exact operation. Only the node role may
  accept a poll interval, and that interval must equal the frozen process deployment.
- Represent all five processes in one canonical, exhaustive, qualification-only deployment
  manifest with derived identities, distinct node/outbox principals, exact source/config custody,
  and no installation, automatic-start or scientific-admission capability.
- Pass the deployment spec's manifest SHA-256 in every systemd `ExecStart`; verify those exact
  canonical bytes before loading any factory.
- Fresh-read factory and configuration bytes with no symlink traversal, one regular hard link,
  stable inode metadata, bounded size, exact SHA-256, owner, group and read-only mode.
- Require Linux and the exact effective process UID/GID before dynamic loading. Accept only the
  concrete one-operation handler container and reject any handler return value.
- Emit operational startup/normal-return diagnostics that explicitly remain non-deployment and
  non-scientific evidence.

## Consequences

The rendered units now point to real, closed entrypoints and a pathname-only manifest substitution
cannot reach a composition factory. A reviewed deployment can provide role-specific factories
without widening the runner command surface.

This decision intentionally stops before those factories and before host mutation. A later opt-in
installer must bind the service manifest back to the complete portable spec and reviewed code tree,
install identities/files/ACLs/units without partial activation, and leave the services disabled
until an independent observer and campaign qualify the exact host. Until that occurs, the system
remains nondeployable.
