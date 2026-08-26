# PR-8f disabled qualification host bootstrap

- Status: principal/root bootstrap source complete; no target-host bootstrap has been executed
- Date: 2026-08-27

## Closed commissioning stage

PR-8f adds the first opt-in target-host commissioning stage without activating a service. One
canonical, externally SHA-pinned request binds the complete qualification deployment spec, two
fixed Linux service identities, the existing Docker group, the local PostgreSQL socket inode,
three root-owned account-management executables, and fifteen empty custody roots.

- The node and outbox Linux user names are exactly their PostgreSQL application-role names. Both
  have a dedicated primary UID/GID, `/nonexistent` home, a pinned `nologin` shell and a locked
  password. Only the node user is an exact member of the pinned Docker group.
- The node and outbox systemd units now receive distinct passwordless
  `postgresql+psycopg` URLs over the fixed `/run/postgresql` Unix socket. All libpq override
  variables are explicitly unset, root-side services receive no database URL, and the effective
  systemd identity projection verifies the exact assignments.
- The plan exhaustively creates fifteen roots: artifact/input/installer/node-state/node-key/
  outbox-spool/runtime journals, workspace underlay/source, quota backing/state/socket parent,
  watchdog state/socket parent, and service configs. Each path has one exact UID/GID/mode policy;
  creation begins from a root-owned empty directory and repairs only the narrow safe intermediate
  state left by an interrupted creation.
- A deployment-scoped flock and append-only request, plan, intent, completion and final-receipt
  files make an exact retry converge. Existing variants, changed NSS identities, extra group
  memberships, inode drift, non-empty incomplete roots, symlinks, writable parent chains, changed
  tool/socket pins and clock rollback fail closed.

The command is a dry run unless both `--apply` and the literal acknowledgement
`BOOTSTRAP_QUALIFICATION_ONLY_DISABLED` are supplied:

```console
conda run -n aletheia python scripts/bootstrap-qualification-host.py \
  --request /root/qualification-bootstrap.json \
  --request-sha256 <out-of-band-sha256>

conda run -n aletheia python scripts/bootstrap-qualification-host.py \
  --request /root/qualification-bootstrap.json \
  --request-sha256 <out-of-band-sha256> \
  --apply \
  --acknowledge BOOTSTRAP_QUALIFICATION_ONLY_DISABLED
```

The concrete adapter requires Linux and effective `root:root`. Its bootstrap journal must already
exist as root-owned mode `0700`; every target parent must already exist, be root-controlled and
contain no symlink traversal. The Docker group and PostgreSQL socket directory are external
preconditions, not objects this stage mutates.

## Explicit remaining gates

No command above has been run on a target host in this repository checkpoint. The receipt always
states that configs and private keys are unpublished, PostgreSQL roles and ACLs are unapplied,
systemd units are uninstalled/disabled/inactive, deployment qualification is false, and scientific
admission is forbidden.

The next ordered stage is PR-8g: publish the exact process configs and private keys, create the two
restricted PostgreSQL peer roles, and apply/revalidate the rendered ACL while all five units remain
disabled. PR-8h must then add the concrete observer/campaign runner and execute the real
Linux/systemd/rootful-Docker/loop/ext4/PostgreSQL process-kill campaign before any host can be
called deployable.

See [ADR 0079](architecture/0079-disabled-qualification-host-bootstrap.md), the
[PR-8e terminal-outbox guide](PR8E_QUALIFICATION_TERMINAL_OUTBOX_FACTORY.md), and the
[PR-4b deployment guide](PR4B_LOCAL_EXECUTION_COMPOSITION.md).
