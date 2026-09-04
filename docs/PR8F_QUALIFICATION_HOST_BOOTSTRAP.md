# PR-8f disabled qualification host bootstrap

- Status: exact generation-h bootstrap executed and later qualified on target
- Date: 2026-09-05

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

## Target result and remaining boundary

At the original PR-8f checkpoint no target command had run, and the bootstrap receipt correctly
kept configs/keys unpublished, PostgreSQL unchanged, units absent and both authority flags false.
Generation `20260904h` later applied bootstrap request SHA-256
`a0fcc6471e5ee35ccc88d7a258d845fcabad8ec298308739dc9ec0341d006fbb`, continued through PR-8g
commissioning and PR-8b installation, and passed the exact PR-8h campaign. That later receipt is
the deployment evidence; bootstrap alone still cannot activate services or grant scientific
admission. A source or deployment change requires a fresh generation and cannot reuse h's
principal/root observations as current evidence.

See [ADR 0079](architecture/0079-disabled-qualification-host-bootstrap.md), the
[PR-8g authority commissioning guide](PR8G_QUALIFICATION_AUTHORITY_COMMISSIONING.md), the
[PR-8h target campaign guide](PR8H_QUALIFICATION_TARGET_CAMPAIGN.md), the
[PR-8e terminal-outbox guide](PR8E_QUALIFICATION_TERMINAL_OUTBOX_FACTORY.md), and the
[PR-4b deployment guide](PR4B_LOCAL_EXECUTION_COMPOSITION.md).
