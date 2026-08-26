# ADR 0078: retain qualification terminal authority in a private write-once spool

- Status: Accepted
- Date: 2026-08-27

## Context

The five-process qualification manifest reserved a least-privilege outbox role, but no factory
implemented it. The repository already has two terminal sources with intentionally different
semantics: legacy v1 has a mutable pending/published projection, while qualification v2 is an
immutable authority consumed independently by the Research Controller. Treating a database read,
an in-memory callback or an enqueue attempt as publication would lose the terminal wakeup across
crashes or collapse the execution and scientific-controller authorities.

## Decision

Compose the outbox role as a bounded database-to-private-spool mirror:

1. bind canonical config bytes to the exact guarded process, live PostgreSQL role/revision and a
   pre-created owner-only spool root;
2. revalidate every v1 and v2 row into a typed canonical envelope with its original delivery key;
3. publish bytes through fsynced, sealed, same-inode hard-link steps and recover only the four
   exact crash residues;
4. retain every final file and verify the complete spool inventory on every tick;
5. mark only legacy v1 published, only after durable publication, through its existing row CAS;
6. leave v2 immutable and leave external delivery/reconciliation to a separately commissioned
   consumer; and
7. load no signing key and expose no allocation, task-enqueue, Kernel or observation authority.

## Consequences

The checked-in qualification process can now survive a crash at either side of the filesystem/DB
boundary without dropping or rewriting terminal evidence. It intentionally performs a full scan
bounded by deployment config, so history above the bound and missing historical spool files are
operator-visible blockers rather than best-effort delivery.

This is source composition, not host evidence. Exact principal/config/spool/ACL commissioning,
external consumer acknowledgment, concrete Linux observation and the root/systemd/Docker/
PostgreSQL process-kill campaign remain mandatory gates.
