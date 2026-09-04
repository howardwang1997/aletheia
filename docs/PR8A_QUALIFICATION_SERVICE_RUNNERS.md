# PR-8a guarded qualification service runners

- Status: source process boundary complete; exact generation h later qualified on target
- Date: 2026-09-05

## What changed

The five service programs rendered by `QualificationDeploymentSpecV1` now exist in the repository:

- `scripts/run-workspace.py` exposes only `ensure-shared-workspace`;
- `scripts/run-quota.py` and `scripts/run-watchdog.py` expose only `serve`;
- `scripts/run-node.py` exposes only `run` with the exact manifest-pinned poll interval; and
- `scripts/run-outbox.py` exposes only `run`.

Each thin entrypoint compiles in one role and delegates to
`aletheia.qualification_service_runtime`. The common outer runtime accepts one canonical
five-process manifest. Before importing any composition factory it verifies an out-of-band
manifest SHA-256, rejects duplicate or non-canonical JSON, fresh-reads regular single-link source
and configuration files, checks their byte/owner/group/mode pins, rejects symlink traversal, and
checks the live Linux effective UID/GID. A factory may return only the exact
`QualificationServiceHandlerSet` for that role and operation. Returned values are rejected so a
runner receipt cannot be mistaken for scientific evidence.

The systemd renderer now includes `--manifest-sha256` in every `ExecStart`. This binds the bytes
consumed by a runner to `QualificationDeploymentSpecV1.deployment_manifest_sha256` instead of
trusting a pathname alone. Startup and normal-return receipts remain unsigned operational
diagnostics with `qualification_only=true`, `scientific_admission_allowed=false`, and
`deployment_qualified=false`.

## Deliberate boundary

This slice did not supply the five production composition factories. PR-8c subsequently supplies
the workspace, quota and watchdog factories; PR-8d and PR-8e subsequently supplied the node and
outbox factories. It does not
invent an outbox publisher, load node or PostgreSQL credentials, install accounts or files, write
systemd units, apply the PostgreSQL ACL, start a daemon, or observe a host. The next installer must
mechanically prove that the manifest deployment ID, service UID/GID, node poll interval, factory
paths and source digests agree with the exact portable deployment spec and its exhaustive reviewed
code tree before installing anything.

PR-8b now supplies that narrowly scoped, explicit installer for the manifest and disabled unit
files. It still does not supply all factories, principals/configs/keys, PostgreSQL ACL mutation,
service activation, observer or campaign.

Generation `20260904h` later commissioned these five guarded runners and passed the concrete
root/systemd/loop/ext4/rootful-Docker/PostgreSQL PR-8h campaign on frozen merge `e0dc06ce`. That
target receipt—not the source tests on Darwin or a mocked Linux identity—is the deployment
evidence, and it still grants no scientific admission.

See [architecture decision 0074](architecture/0074-guarded-qualification-service-runners.md) and
the [PR-4b deployment guide](PR4B_LOCAL_EXECUTION_COMPOSITION.md).
