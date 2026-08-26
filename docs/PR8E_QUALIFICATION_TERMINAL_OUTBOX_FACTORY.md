# PR-8e qualification terminal-outbox factory

- Status: outbox-side source composition complete; not commissioned on a target host
- Date: 2026-08-27

## Closed source surface

PR-8e supplies the checked-in factory named by the non-root `outbox` entry in the five-process
qualification manifest. It is a deliberately narrow database-to-filesystem handoff, not a
scientific controller or a network publisher.

- One canonical config binds the guarded process projection, database URL digest, exact
  PostgreSQL role and Alembic revision, a pre-created private spool inode, the poll interval and a
  hard maximum row count for each source generation.
- The process reads both terminal generations: legacy `execution.terminal.v1` receipts and the
  immutable `execution.qualification_terminal.v2` accepted/deadline authorities. Every database
  column, payload, authority hash, execution/attempt identity, topic, delivery key and creation
  time is revalidated into one closed canonical envelope.
- A deployment-scoped nonblocking lock permits one spool owner. Publication uses an owner-only
  pending file, file fsync, read-only sealing, a same-inode hard link, directory fsync and exact
  residue recovery. A retry accepts only the same bytes and custody; foreign entries, missing
  retained files, symlinks, changed metadata and variant payloads fail closed.
- Legacy v1 changes from `pending` to `published` only after the exact envelope is durable, using
  the existing row-state CAS in the same database transaction. A rollback leaves a harmless exact
  file that the next tick replays before retrying the CAS. Immutable v2 rows receive no invented
  delivery status.
- Startup verifies the live database principal and sole schema head. The process has no private
  signing key, allocation path, durable-task enqueue capability, Kernel mutation or observation
  admission authority.

The spool is a retained local handoff. This service never deletes an envelope and requires its
inventory to equal the bounded authoritative source set. Commissioning must therefore begin with
an empty source or import every exact historical envelope; exceeding the configured bound stops
the process for explicit operator action rather than silently paging past authority.

## Explicit remaining gates

This PR does not create the outbox principal or spool, install its config, apply PostgreSQL ACLs,
enable/start the unit, deliver files to an external consumer, or reconcile consumer acknowledgments.
It also does not implement the concrete Linux observer or campaign runner and does not prove a
real terminal row under process-kill.

With PR-8c, PR-8d and PR-8e, all five manifest entries now have checked-in factories. PR-8f now
supplies the first disabled commissioning stage for exact principals, empty directories and local
PostgreSQL peer URLs, but it has not been run on a target host. Config/key publication and
PostgreSQL role/ACL commissioning remain next while units stay disabled. Only after that may the real
Linux/root/systemd/loop/ext4/rootful-Docker/PostgreSQL process-kill campaign enable the bounded
services and produce deployment evidence.

See [ADR 0078](architecture/0078-qualification-terminal-outbox-factory.md), the
[PR-8d node-factory guide](PR8D_QUALIFICATION_NODE_FACTORY.md), and the
[PR-4b deployment guide](PR4B_LOCAL_EXECUTION_COMPOSITION.md).
