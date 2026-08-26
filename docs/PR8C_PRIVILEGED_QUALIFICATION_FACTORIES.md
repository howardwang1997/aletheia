# PR-8c privileged qualification service factories

- Status: root-side source composition complete; not commissioned on a target host
- Date: 2026-08-27

Subsequent PR-8d and PR-8e have now supplied the node and terminal-outbox factories described
below; target-host commissioning remains open.

## Closed source surface

PR-8c supplies the checked-in factories named by the workspace, quota and watchdog entries in the
five-process qualification manifest.

- The workspace factory composes one root-only, systemd-pinned one-shot. It requires pre-created
  source and target directories, rejects a nonempty first-use target, executes an inode- and
  byte-pinned `mount` through its already-open descriptor, and verifies the resulting bind against
  the source inode and `/proc/self/mountinfo`.
- The workspace transition is restart-safe. An exact existing non-shared bind is the only accepted
  partial state and is promoted with `--make-shared`; an exact shared bind is an idempotent replay.
  A missing, duplicate or foreign mount, changed source, changed parent chain, unexpected content,
  changed unit/module/interpreter/tool, or non-root/non-systemd process fails closed.
- The quota and watchdog factories expose only the existing
  `LoopbackOutputQuotaProvisioningService.serve_forever` and
  `DurableDeadlineWatchdogService.serve_forever` operations. Their canonical configs carry the
  complete existing deployment pins and, for the watchdog, the full OCI policy.
- Every config binds a stable process projection containing all process fields except the derived
  process id and its own config digest. This deliberately avoids a hash self-reference while
  preserving source, role, operation, UID/GID, config custody and deployment identity. The final
  config digest remains frozen by the process manifest.
- All three handler sets reject a node poll interval and contain no database credential, private
  signing key or scientific-admission capability.

The config parser rejects duplicate keys and requires byte-for-byte canonical JSON. A focused
guarded-loader test constructs the config first, freezes its digest into the final process, then
loads the exact checked-in factory source through the same runtime boundary used by systemd.

## Explicit remaining gates

This is source composition, not deployment evidence. It does not create directories or principals,
write configs or keys, install/enable/start units, or apply PostgreSQL ACLs. PR-8d and PR-8e later
implemented the non-root node and terminal-outbox services. No concrete observer or campaign runner is
added, and no Linux bind/shared, loop/ext4, Docker, systemd or process-kill campaign ran in this PR.

Commissioning must now pin the configs, principals, keys and ACL before any target-host campaign
can produce a frozen installed manifest.

See [architecture decision 0076](architecture/0076-privileged-qualification-factories.md), the
[PR-8b installer guide](PR8B_DISABLED_QUALIFICATION_INSTALLER.md), and the
[PR-4b deployment guide](PR4B_LOCAL_EXECUTION_COMPOSITION.md).
