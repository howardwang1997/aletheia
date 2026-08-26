# Architecture decision 0075: Disabled qualification file installer

- Status: accepted for the PR-8b installation slice
- Date: 2026-08-27
- Scope: manifest/unit publication and systemd reload only

## Context

PR-8a supplied guarded process entrypoints but intentionally had no host mutation path. Installing
five units with ordinary file copies would permit partial state after a crash, pathname substitution,
variant overwrite, or accidental activation before PostgreSQL and custody prerequisites were
qualified.

## Decision

- Make planning the default CLI behavior. Require an externally SHA-pinned canonical request,
  `--apply`, root/Linux execution and an exact acknowledgement for mutation.
- Bind the service manifest back to the portable spec: deployment identity, manifest digest,
  factory membership in the exhaustive reviewed tree, role principals and node poll interval must
  match mechanically.
- Require factory, config and systemctl bytes/owner/group/mode before any journal or target write.
- Serialize by deployment lock and freeze one active request. Variant requests cannot replace an
  installed generation.
- Record append-only request, plan, per-artifact intent/completion, daemon-reload and final receipt
  objects. Resume only the same canonical request and compare full target inode custody.
- Publish one manifest and five units through same-directory staging and fsync. Never overwrite an
  existing variant.
- Observe all units inactive and disabled/absent before mutation. Invoke only `daemon-reload`, then
  require all five exact units loaded, disabled and inactive.
- Keep principal creation, PostgreSQL ACL application, enable/start, independent observation and
  campaign execution mechanically false in every plan and receipt.

## Consequences

Loss of the installer process at any artifact boundary is recoverable without guessing whether a
file committed, and a successful file installation cannot activate execution. The journal and
receipt are operational evidence only; they are not an independent observation of the host.

The target must still pre-stage reviewed code, factories, configurations, identities, directories,
keys, PostgreSQL and Docker/systemd prerequisites. A later commissioning stage must close those
inputs, apply the exact ACL under a separately pinned database authority, and then let an
independent observer freeze live state before any opt-in campaign starts services.
