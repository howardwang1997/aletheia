# PR-8j attempt-scoped pre-runtime cleanup recovery

- Status: available only for a live never-started attempt satisfying its exact cleanup contract
- Scope: release one retained never-started qualification attempt after its source node key expired
- Scientific authority: none

## Contract

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

## Applicability

Use this operation only for an existing, never-started attempt whose pinned runtime preparation,
launch authorization, custody roots and watchdog deployment are still present and independently
verified. Historical retired attempts are not prerequisites for a new deployment. Each new
qualification generation must satisfy its own commissioning and cleanup contracts.
