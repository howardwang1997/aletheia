# ADR 0081: require independent live observation and destructive target evidence

- Status: Accepted
- Date: 2026-08-27

## Context

PR-8f, PR-8g and PR-8b can create principals/roots, commission keys and PostgreSQL ACL, and install
five disabled units. Their receipts intentionally cannot prove that systemd loaded the intended
bytes, the effective processes retained the intended identities, rootful Docker and shared mounts
work, or recovery preserves one exact execution and terminal envelope.

A synthetic observer or a runner that merely echoes the desired spec would convert configuration
success into a deployment claim. Conversely, letting the execution or scientific-admission process
certify its own host would collapse the authority boundary.

## Decision

1. One root-only Ed25519 observer, distinct from every execution and scientific authority, signs a
   closed fresh projection of files, process state, namespaces, Docker/image confinement and
   PostgreSQL state.
2. Loaded unit bytes plus no drop-ins/no pending reload are necessary but insufficient: each live
   process must also match `/proc` identity, argv, Python inode, cwd, environment and capability
   bitmaps.
3. Docker and authority-bundle fields already frozen before installation remain externally reviewed
   opaque pins. Post-installation typed projections are separately pinned; they do not feed back
   into the original deployment hash.
4. Campaign apply requires an explicit literal acknowledgement, one root-owned canonical request,
   a bounded deadline and one pre-registered qualification-only scientific execution. Its
   explicitly frozen initial-assignment lease may cover deterministic request/plan/apply startup;
   the first exact runtime launch authorization contracts it atomically to a bounded launch lease
   covering the greater of the ordinary heartbeat interval and the complete signed launch window.
   The ordinary heartbeat protocol takes over after launch and may extend, but never shorten, that
   retained lease. Before every node tick, database-clock expiry is persisted only for that
   worker's exact deployment-pinned and registered `(node_id, node_manifest_sha256)`; the worker
   cannot run the allocator's unscoped maintenance sweep over sibling nodes.
5. Before campaign mutation, the pinned systemd boundary must reject every live sibling
   `aletheia-qualification-*` or `aletheia-arl1-*` service outside the request's exact five units.
   It must also reject every sibling unit file whose state is not persistently `disabled` or
   `masked`; an inactive but enabled, static, indirect, linked, generated, transient or
   runtime-masked service can regain authority on activation or reboot. Both checks surround
   activation and installed-manifest observation and are repeated when a completed receipt is
   revalidated. The campaign has no stop/disable authority: older generations must be explicitly
   retired or the campaign must move to a fresh disposable target.
6. The outbox is stopped before the selected attempt becomes terminal. The node process is killed,
   one immutable successful v2 terminal row must commit, the outbox is restarted to publish a
   canonical spool envelope, then its process is killed and the same inode/bytes must survive.
7. Quota and watchdog processes are independently killed after a real loop/ext4 generation and
   must replay the same root-service receipt. A real node peer transaction is terminated by the
   separate admin backend and must lose its connection and advisory lock before peer reconnect.
8. The campaign journal is append-only and crash-replayable. It retains the full post-kill signed
   observation beside a mechanically recomputable preflight; a final verdict is derived only after
   that evidence and fresh DB/spool revalidation.
9. Success grants deployment qualification for that exact evidence chain only. It never grants
   scientific observation admission.

## Consequences

Source completion can now be reviewed and tested on non-Linux development machines without being
misreported as host evidence. Actual qualification is deliberately expensive and destructive, so
it runs only on a disposable exact target with a purpose-built long-running fixture.

There is still no campaign receipt at this checkpoint. Until an operator runs the command and
retains the complete evidence bundle, the deployment remains nondeployable.
