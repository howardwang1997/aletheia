# PR-8b disabled qualification file installer

- Status: source installer complete; no target-host installation has been executed
- Date: 2026-08-27

## Installed surface

`aletheia.qualification_installer` now plans and explicitly installs exactly six immutable files:
the canonical five-process service manifest and the five systemd units rendered by
`QualificationDeploymentSpecV1`. The request binds the complete portable spec, service manifest,
reviewed factory entries, role UID/GID, node poll interval, role-readable configuration custody,
and a byte-pinned root-owned `systemctl` executable.

The command is dry-run by default. Mutation requires both `--apply` and the literal acknowledgement
`INSTALL_QUALIFICATION_ONLY_DISABLED` against one canonical request file and an out-of-band request
SHA-256. The concrete adapter additionally requires Linux, effective `root:root`, a pre-created
root-owned `0700` journal, exact pinned factory/config/systemctl bytes, and root-controlled target
parent chains.

## Crash and retry semantics

One deployment-scoped flock serializes installation. Before each target publication, the installer
durably writes an append-only intent. It publishes through a same-directory staging file, exact
owner/group/mode, file fsync, atomic replacement into an absent target, and parent-directory fsync;
an existing variant is never overwritten. A durable completion records the exact device/inode and
content custody. On restart, an exact request resumes an absent completion, while changed request,
plan, journal, target bytes, metadata or inode fails closed.

After all six completions, the installer invokes only the pinned `systemctl daemon-reload`, records
that marker, and proves the exact five units are loaded, disabled and inactive. A crash after the
marker does not invoke reload again. The final receipt remains
`qualification_only=true`, `scientific_admission_allowed=false`, and
`deployment_qualified=false`.

## Explicit non-capabilities

This slice does not create Linux users/groups or custody roots, install code/Python/native tools,
write composition configs or keys, apply the rendered PostgreSQL ACL, enable/start a unit, observe
the host independently, freeze `QualificationInstalledDeploymentManifestV1`, or execute the
qualification campaign. All factory/config inputs and target parents must already exist with exact
custody. PR-8c/PR-8d/PR-8e have since closed the root, node and terminal-outbox
source-composition gates. Principal/config/key/ACL commissioning remains open.

The Darwin unit/fault tests exercise the pure state machine and a non-root atomic-file primitive;
they do not execute the concrete root/systemd adapter and are not target-host evidence.

See [architecture decision 0075](architecture/0075-disabled-qualification-file-installer.md), the
[PR-8a runner guide](PR8A_QUALIFICATION_SERVICE_RUNNERS.md), and the
[PR-4b deployment guide](PR4B_LOCAL_EXECUTION_COMPOSITION.md).
