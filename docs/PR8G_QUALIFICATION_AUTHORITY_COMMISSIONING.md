# PR-8g qualification authority commissioning

- Status: config/key/PostgreSQL commissioning source complete; not executed on a target host
- Date: 2026-08-27

## Closed commissioning stage

PR-8g connects the disabled PR-8f bootstrap receipt to the final PR-8b installation request without
starting a service. The bootstrap spec must carry the explicit unfinalized-manifest sentinel. After
the live UID/GID and directory inodes exist, one canonical commissioning request may change exactly
the two manifest-digest fields and nothing else in the deployment spec.

The request freezes:

- the complete bootstrap request and its independently reconstructable receipt chain;
- the final five-process manifest and disabled installation request;
- three external root-owned, mode `0400`, parent-chain/SHA-pinned raw private-key sources, while
  keeping all key bytes out of the canonical request and journals;
- five canonical process configs whose file digests and non-self-referential process bindings match
  the final manifest;
- every live workspace/quota/watchdog/node/outbox root inode from the bootstrap receipt;
- distinct passwordless local-peer database URL digests for the node and outbox; and
- an out-of-band admin database URL digest, separate superuser role, current schema revision,
  exact cluster/database identity and rendered ACL digest.

The three privileged service configs freeze their deployment-scoped unit names, but do not carry
an inode or content pin for a unit that is required to remain absent during commissioning. Unit
content includes the final manifest digest, so placing a future unit pin back inside a config
whose digest is itself part of that manifest would create an unsatisfiable hash/inode cycle. The
PR-8b receipt freezes the installed files after publication and the independent PR-8h observer
then reopens the exact loaded fragments, inode custody and daemon-reload state before qualification.
The quota config similarly freezes the exact source inode that must appear at the shared output
path, but not a Linux mount ID that does not exist until the workspace one-shot performs the bind.
Quota and node composition independently resolve that live shared-mount ID after startup; the node
then pins it for its process lifetime and PR-8h freezes it in the signed host observation.

The crash-replayable execution order is fixed: node Ed25519 key, assignment X25519 key, runtime-
control Ed25519 key, workspace/quota/watchdog/node/outbox configs, then PostgreSQL. Each file has an
append-only intent and completion and is freshly reopened and rehashed on retry. Before a private
key can be published, its derived public-key identity must equal the already frozen node,
assignment-transport or runtime-control authority pin.

PostgreSQL commissioning requires two unshadowed, option-free `local`/`peer` HBA rules—one for each
application role. The connected admin must be the separately pinned superuser. Creation of the
NOLOGIN owner plus two passwordless LOGIN roles, database ownership transfer and the existing
exhaustive ACL run in one database transaction; after ownership transfer, any explicit database
grant retained by the separately pinned former admin is revoked. Pre-existing roles are accepted only when every
login, membership, password, validity and connection-limit field is safe; their config must be
either pristine or already equal to the final `search_path`. The transaction normalizes the target
ACL, and the committed state then requires the exact role config and direct database/schema/table/
column/sequence/routine privilege projection. Exact retry runs the exhaustive catalog block inside
a read-only transaction and reconstructs those direct privilege hashes, so later ACL drift cannot
be mistaken for the original receipt. The PostgreSQL system identifier, version number, database
OID/name and encoding are checked before mutation and retained in the receipt.

The deployment target pins the repository's unique Alembic head `20260829_0030`. Revision `0027`
adds the PR-5 scientific-controller persistence, `0028` permits the preregistered replicate
campaigns required by ARL-1, `0029` closes real-time endurance transaction-clock custody, and
`0030` gives the runtime-v2 deferred validator exact no-login owner authority without granting
application roles direct routine execution. Commissioning rejects any older or newer database
revision. A repository regression test requires
this deployment pin to remain equal to the unique Alembic head.

Plan and apply use the checked-in wrapper. Apply additionally requires the exact acknowledgement
and `ALETHEIA_QUALIFICATION_ADMIN_DATABASE_URL`; the URL itself is never written to the request or
receipt.

```console
conda run -n aletheia python scripts/commission-qualification-authority.py plan \
  /root/qualification-authority-commissioning.json

ALETHEIA_QUALIFICATION_ADMIN_DATABASE_URL='<secret-url>' \
conda run -n aletheia python scripts/commission-qualification-authority.py apply \
  /root/qualification-authority-commissioning.json \
  --acknowledge COMMISSION_QUALIFICATION_AUTHORITY_DISABLED
```

Both commands require a canonical root-owned mode-`0400` request file. Apply additionally requires
Linux, effective `root:root`, the unchanged PR-8f bootstrap state, the three source key files, the
pinned `systemctl` executable, the exact HBA projection, and a current migrated PostgreSQL database.

## Explicit remaining gates

No target host or database was mutated at this checkpoint. The final receipt always records that
all five units remain absent, uninstalled, disabled and inactive; deployment qualification and
scientific admission remain false. PR-8b must still install the final manifest and disabled units.

PR-8h now provides the independent live observer and campaign runner, but it has not executed the
exact Linux/rootful-Docker/systemd/shared-mount/loop/ext4/cgroup-v2/AppArmor/PostgreSQL
peer/process-kill campaign. In particular, unit tests and a synthetic host port do not prove OS
peer login, real transaction rollback, key custody on the target filesystem or service restart
recovery.

See [ADR 0080](architecture/0080-qualification-authority-commissioning.md), the
[PR-8h target campaign guide](PR8H_QUALIFICATION_TARGET_CAMPAIGN.md), the
[PR-8f bootstrap guide](PR8F_QUALIFICATION_HOST_BOOTSTRAP.md), and the
[PR-8b disabled installer guide](PR8B_DISABLED_QUALIFICATION_INSTALLER.md).
