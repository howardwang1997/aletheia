# PR-8g qualification authority commissioning

- Status: generation h commissioned and independently qualified; post-qualification UTC hardening
  requires a new freeze before reuse
- Date: 2026-09-05

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
either pristine or already equal to the final `TimeZone=UTC` plus
`search_path=pg_catalog, public` tuple. The role-local UTC default prevents a host-cluster timezone
name from becoming an ambient Python timezone-data dependency and makes timestamp display and
interpretation deterministic for every new application session. The transaction normalizes the
target ACL, and the committed state then requires the exact role config and direct
database/schema/table/column/sequence/routine privilege projection. Exact retry runs the exhaustive
catalog block inside a read-only transaction and reconstructs those direct privilege hashes, so
later ACL drift cannot be mistaken for the original receipt. The PostgreSQL system identifier,
version number, database OID/name and encoding are checked before mutation and retained in the
receipt.

The deployment target pins the repository's unique Alembic head `20260903_0032`. Revision `0027`
adds the PR-5 scientific-controller persistence, `0028` permits the preregistered replicate
campaigns required by ARL-1, `0029` closes real-time endurance transaction-clock custody, and
`0030` gives the runtime-v2 deferred validator exact no-login owner authority without granting
application roles direct routine execution. Revision `0031` binds the one permitted pre-launch
lease contraction to the exact append-only runtime launch authority and matching resource lease;
revision `0032` adds the separately keyed, attempt-scoped pre-runtime cleanup authority.
Commissioning rejects any older or newer database revision. A repository regression test requires
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

## Target result and remaining gate

Generation `20260904h` applied this stage to the fresh database
`aletheia_qualification_20260904h` and later passed the complete PR-8h target campaign. Its
commissioning request had SHA-256
`c36aea116a4010c51d75376e993ce207f785731576509c63096319f3ae590e24`; this is exact target
evidence for frozen merge `e0dc06ce23796aa9fc49d598c57bde6bbe7256fb`, not a transferable
claim for later source.

That run also exposed repeated Psycopg warnings when the target cluster reported `Etc/UTC` to the
minimal service runtime. Psycopg safely fell back to UTC and the independently observed campaign
still qualified, but relying on the fallback is unnecessary ambient behavior. The current ACL and
commissioning projection therefore require both application roles to start new sessions with the
exact `TimeZone=UTC` default. The companion runtime repair binds `PYTHONTZPATH` to the frozen
runtime's own reviewed `share/zoneinfo` directory and probes both UTC names. Because these changes
alter the rendered ACL, unit bytes and commissioned-state digest, they must be frozen and
independently qualified in a later generation before that updated deployment can inherit
generation h's claim. Scientific admission remains false in every PR-8g/PR-8h receipt; the next
capability gate is the production ARL-1 given-protocol campaign and independent qualification flow.

See [ADR 0080](architecture/0080-qualification-authority-commissioning.md), the
[PR-8h target campaign guide](PR8H_QUALIFICATION_TARGET_CAMPAIGN.md), the
[PR-8f bootstrap guide](PR8F_QUALIFICATION_HOST_BOOTSTRAP.md), and the
[PR-8b disabled installer guide](PR8B_DISABLED_QUALIFICATION_INSTALLER.md).
