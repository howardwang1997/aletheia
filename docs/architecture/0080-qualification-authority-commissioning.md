# ADR 0080: finalize qualification authority before installing disabled units

- Status: Accepted
- Date: 2026-08-27

## Context

The service manifest cannot be frozen before PR-8f because node and outbox configs must bind live
directory inodes created by bootstrap. Conversely, bootstrap cannot bind the final manifest digest
without creating a hash cycle: config bytes determine process entries, process entries determine
the manifest, and the portable deployment spec pins that manifest.

Keys and database ACLs also cross distinct trust boundaries. Secret bytes must not enter a request
or journal, and successful engineering file publication must not become service activation or
scientific authority.

## Decision

1. A bootstrap request carries one domain-separated unfinalized-manifest SHA-256 sentinel.
2. Commissioning reconstructs the complete bootstrap receipt and permits the final spec to differ
   only in `deployment_manifest_sha256` and the matching reviewed-file digest.
3. The final PR-8b installation request is embedded directly instead of duplicating its manifest,
   process, source and systemd validation.
4. Three externally stored root-owned raw keys are referenced only by
   path/custody/parent-chain/SHA. Their public identities are derived after a fresh read and checked
   before atomic node-owned publish.
5. Five configs bind the live bootstrap inodes, exact process projections, peer URL digests,
   current Alembic head and common OCI/quota/watchdog policy.
6. Every file mutation is preceded by a durable intent and followed by a fresh-file completion.
7. Exact cluster/database identity and unshadowed local-peer HBA rules are immutable preconditions.
   A separate pinned superuser creates/adopts the three exact application roles, transfers database
   ownership, removes the former admin's now-unneeded explicit database grant and applies the
   rendered ACL in one transaction. The post-transaction state freezes
   exact role config and direct target privilege hashes; retry recomputes them while executing the
   exhaustive catalog validation block in a read-only transaction.
8. Systemd units must be absent both before and after this stage. Installation, enablement, start,
   deployment qualification and scientific admission stay mechanically false.

## Consequences

The final manifest can now be derived without self-reference, secrets remain out of canonical
control artifacts, and a crash cannot leave a journal claiming more authority than fresh host/DB
state. A role-identity or HBA variant fails closed; intended target ACL drift is normalized only by
the explicit commissioning transaction and is never silently accepted by observation or retry.

The connected database superuser and target-host root remain explicit commissioning TCB. Source
completion is not target-host evidence. Disabled unit installation, an independent observer, live
peer connections, real service/process-kill recovery and the full qualification campaign remain
later ordered gates.
