# PR-8j attempt-scoped pre-runtime cleanup recovery

- Status: watchdog replay repair merged and target-replayed; recovery remains paused on the same
  raw-inspection comparison independently exposed in the node cleanup replay
- Scope: release one retained never-started qualification attempt after its source node key expired
- Scientific authority: none

## Why this exists

A qualification node can durably submit an exact Docker start and then prove that the launch gate
rejected it before workload execution. The ordinary absence transaction requires a fresh enrolled
node signature. If that node key expires after local absence evidence is sealed but before the
database commit, refusing the stale signature is correct, yet the attempt and its exclusive
resource holds cannot be released through the ordinary path.

This slice adds a narrower authority instead of reviving the node. It is valid for at most one
hour and freezes all of the following directly into the public pin:

- source node ID and manifest SHA-256;
- one infrastructure attempt and one existing runtime preparation;
- the already-committed runtime launch authorization;
- the exact next pre-runtime absence epoch;
- the installed root-watchdog deployment; and
- one distinct Ed25519 principal/key and policy.

It sets `cleanup_only=true`, `launch_allowed=false`, `qualification_only=true` and
`scientific_admission_allowed=false`. The allocator accepts it only for a never-started attempt
without a node launch receipt, runtime identity or terminal authority. The resulting transaction
must be `released`, with no replacement request or replacement authorization.

## Durable and process boundaries

Alembic `20260903_0032` adds no table and changes no existing row. It extends the closed JSON and
deferred attempt validator with an additive recovery shape while preserving the byte shape of
ordinary node-signed receipts. Application verification still checks the Ed25519 signature and
constructor-pinned key; PostgreSQL independently checks the relational attempt, preparation,
authorization, epoch, time window and release-only decision in the same transaction.

`commission-pre-runtime-cleanup.py` must run as root on the target. It generates the private key
on that target, never prints or returns private bytes or their file digest, and durably publishes
the key/config through a fixed pending inode plus a no-overwrite hard link. Exact retry recovers an
unsealed pending writer residue, a sealed pending inode, or the final-plus-pending two-link crash
window without substituting another finalized key.

`run-pre-runtime-cleanup.py` runs once as the frozen node UID/GID. It loads one config by an
out-of-band SHA-256, exposes no daemon/polling interface, requests only the named attempt, fresh
rehashes all original node/runtime/custody inputs and exits successfully only for
`pre_runtime_released`. A pre-existing node-signed local pending receipt is retained as generation
1; the worker appends a recovery-signed generation 2 with an exact supersession hash before any
allocator call.

## Required order on the retained target

1. Merge the source and freeze an archive from the resulting `main` commit.
2. Verify the archive and reviewed Python tree on the target.
3. Upgrade only the qualification database from `0031` to `0032`; verify the exact schema head.
4. Read the existing attempt/preparation/authorization/absence-epoch identities from their public
   projections and commission the target-local key/config inside the existing deployment custody
   roots. Do not export or print the key.
5. Invoke the one-shot worker as the exact node UID/GID while the recovery window is active.
6. Require one atomic absence decision, attempt `cancelled`, all holds released, the exact stopped
   container absent, and the local append-only generation chain intact.
7. Only then unmount/detach the released old loop/ext4 generation and begin a fresh PR-8h campaign.

The retained target attempt and its loop/ext4 hold are negative engineering evidence until steps
1–6 complete. This recovery is not a PR-8h campaign receipt and cannot qualify ARL-1 by itself.

## 2026-09-03 target checkpoint

PR #139 merged as `61ccd9dddaaef0945b946b68223250159c43e849`; its deterministic source
archive was installed read-only on the qualification target and the qualification database was
upgraded from `0031` to `0032`. The attempt-scoped authority was commissioned entirely on the
target. Its private key was neither exported nor printed, and that expired authority will not be
reused.

Two one-shot cleanup invocations both failed before container removal or database release and are
retained as negative engineering evidence. The second invocation reconstructed the exact immutable
watchdog armed/terminal/pending chain and the same stopped container identity, exit `126`, PID zero,
no restart and exact start/finish timestamps. It then exposed that the watchdog compared the hash
of the *entire current* Docker `ContainerInspect` object with the historical inspection hash.
Docker-maintained, non-security metadata can change while the frozen configuration and process
identity remain unchanged, so this byte-wide replay comparison prevented liveness.

The first follow-up keeps the historical full-inspection hash immutable but does not treat unrelated
current metadata as security authority. Runtime and watchdog share the same exhaustive frozen OCI
enforcement verifier, freshly rehash the runtime-owned seccomp copy, and independently require the
exact container ID/name, closed process-state fields, exit `126`, PID zero, no restart and the
historical start/finish timestamps. Configuration, state or timestamp drift still fails before a
quiescence acknowledgement or container removal.

PR #140 merged as `cf6b00357df4e41d057e7b86c093405e198ae6d7`; deterministic archive
`b041c985c371a536d2c02e33f107aeb3a6f3f8c94647e8f6763bc086db8afb02` was installed frozen on
the same target, its watchdog process was rebound to that release, and a new target-local cleanup
authority was commissioned without exporting private bytes. The third one-shot invocation passed
the watchdog's stable quiescence verification, then failed closed before container removal or the
allocator release transaction. A fixed-string, target-local redaction classified the exception as
`expired launch-gate rejection changed during cleanup`: the node cleanup independently rebuilt the
same fully verified rejection but still compared its fresh full-inspection digest with the original
historical digest. The next repair applies the same rule at that second boundary: preserve the
historical digest, freshly revalidate the complete OCI/process projection, and compare every
security-relevant rejection field while excluding only the newly observed raw digest. A changed
configuration or start/finish timestamp remains rejected in regression tests.

## Focused verification

```bash
conda run -n aletheia pytest -q \
  tests/execution/test_runtime_v2_contracts.py \
  tests/execution/test_node_agent.py \
  tests/execution/test_postgresql_node_adapter.py \
  tests/execution/test_qualification_node_service.py \
  tests/execution/test_pre_runtime_cleanup_commissioning.py \
  tests/execution/test_allocator_v2.py \
  tests/test_schema_migrations.py
```

The allocator test requires the repository's isolated PostgreSQL test database. CI also performs
a fresh PostgreSQL 17 `alembic upgrade head`, `alembic check`, exact schema verification and the
repository-wide suite.
