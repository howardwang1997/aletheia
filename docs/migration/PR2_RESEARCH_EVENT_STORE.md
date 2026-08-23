# PR-2 authoritative research event store operator guide

PR-2 adds the first durable authority path for a new research Quest. It does not run a model or an
experiment: an operator or fixture stages typed objects, signs commands under a root-certified
Quest policy, and commits/replays them without invoking the legacy driver.

## Required custody

Retain these independently versioned inputs:

1. the deployment-approved `ResearchAuthorizationTrustRootV1` JSON;
2. the root-certified `ResearchAuthorizationPolicyV1` used to commission the Quest;
3. the filesystem CAS root containing immutable object and snapshot bytes;
4. the PostgreSQL event-store tables at migration `20260824_0023`.

The stream persists the complete policy JSON and its root/policy digests. The trust-root JSON stays
external because it is the deployment trust decision, not data that a command is allowed to
self-assert.

The production HTTP composition additionally requires all five fail-closed settings below. There
are intentionally no development defaults:

```text
ALETHEIA_RESEARCH_KERNEL_TRUST_ROOT_PATH=/absolute/path/to/trust-root.json
ALETHEIA_RESEARCH_KERNEL_TRUST_ROOT_FILE_SHA256=<sha256-of-exact-file-bytes>
ALETHEIA_RESEARCH_KERNEL_GENESIS_POLICY_REGISTRY_PATH=/absolute/path/to/genesis-policies.json
ALETHEIA_RESEARCH_KERNEL_GENESIS_POLICY_REGISTRY_FILE_SHA256=<sha256-of-exact-file-bytes>
ALETHEIA_RESEARCH_KERNEL_CAS_ROOT=/absolute/path/to/existing-cas-directory
```

Both control files must be pre-existing regular files reached without symbolic links, within the
custody size bound, and byte-for-byte equal to their configured digest. The registry contains one
unique, canonically ordered, root-certified genesis policy per configured Quest. The CAS root must
already be a directory. Missing, unsafe, invalid, or mismatched custody returns HTTP `503`; the API
does not create a permissive root, policy, registry, or CAS location.

## HTTP authority cutover

The authoritative write endpoint is:

```text
POST /research-kernel/programs/{program_id}/quests/{quest_id}/commands
```

Its body must be a complete `AuthorizedResearchCommand`. An unsigned
`ResearchCommandProposal`, a legacy request shape, or an authenticated HTTP user's identity cannot
be converted into scientific authority. The signed command's Quest and immutable Program binding
must exactly match the path. Audited readback is available at the corresponding `GET .../audit`
and `GET .../replay` endpoints, which enforce the same frozen scope.

The old `/research-graph/...` URLs are gone. Compatibility operations remain at the explicitly
deprecated `/legacy/research-graph/...` prefix and can mutate only a `legacy_program_graph` Quest.
They never dual-write a kernel stream.

## CLI

Stage a typed Charter, problem, question, or action object before submitting its signed command:

```bash
conda run -n aletheia python scripts/research_kernel_store.py archive-object \
  --input /path/to/object.json \
  --cas-root /path/to/research-cas
```

Audit the complete command/event/object/snapshot/outbox/head chain:

```bash
conda run -n aletheia python scripts/research_kernel_store.py audit \
  --quest-id qst_0123456789abcdef0123456789abcdef \
  --trust-root /path/to/trusted-root.json \
  --cas-root /path/to/research-cas
```

Return the canonical replayed graph only after the same full audit:

```bash
conda run -n aletheia python scripts/research_kernel_store.py replay \
  --quest-id qst_0123456789abcdef0123456789abcdef \
  --trust-root /path/to/trusted-root.json \
  --cas-root /path/to/research-cas
```

The CLI has no flag for bypassing signatures, changing the authorization linearization time,
allowing dirty policy bytes, or accepting a missing trust root.

## Database deployment

Apply the complete Alembic chain and verify ORM drift:

```bash
conda run -n aletheia alembic upgrade head
conda run -n aletheia alembic check
```

Use separate database roles in production:

- the migration owner may apply reviewed Alembic revisions;
- the application role may execute the store transaction but cannot own/truncate the tables,
  disable triggers, or run DDL.

The seven durable tables are:

1. `research_quest_authorities` — the only shared cross-store state, an immutable identity claim;
2. `research_quest_streams` — the kernel Quest head, frozen scope, root, and policy binding;
3. `research_kernel_objects` — admitted CAS object metadata, never a second payload copy;
4. `research_kernel_command_receipts` — exact signed-command and idempotency receipts;
5. `research_kernel_events` — the append-only authoritative event chain;
6. `research_kernel_snapshots` — canonical replay snapshot custody metadata;
7. `research_kernel_outbox` — the transactionally complete delivery projection.

Existing legacy Quest roots are backfilled into the namespace while legacy Quest insertion is
locked until its claim trigger is installed. Creation triggers then atomically claim
`legacy_program_graph` or `research_kernel_v1` and reject any attempt to give one `qst_*` identity
both authorities. A deferred constraint rejects an orphan or wrong-kind claim. Event/object/
snapshot/outbox/head completeness and authority-claim binding are checked at transaction commit.
Claims, objects, receipts, events, and snapshots cannot be updated or deleted. The stream head may
advance only one compare-and-swap version at a time without changing its scope/authority identity;
outbox delivery fields are the only intentionally mutable operational projection.

## Charter and key lifetime

Fixed-policy v1 deliberately rejects an unbounded Charter. `expires_at` must be finite and later
than the PostgreSQL authorization linearization time. Every delegated amendment and emergency
principal must have continuous same-role key coverage through that expiry (with `revoked_at`
cutting coverage short), and at least one ordinary principal must be continuously covered. An
emergency halt ignores Charter expiry only while the emergency signing key itself is active.

Emergency halt is global and actionless. The signed transition must use the deterministic virtual
authority marker derived from the Quest and current Charter; that marker is not an admitted Action
and has no CAS payload. It cannot contain rejected action alternatives. One accepted emergency
event atomically stops every `admitted`, `active`, or `paused` branch, makes the graph terminal, and
causes every later event to fail.

## Failure and recovery semantics

- A stale expected version or parent hash fails before mutation.
- Concurrent commands against one head serialize; only one compare-and-swap mutation commits.
- An exact retry returns the original receipt, including after a crash immediately after commit.
- A failure before database commit rolls back every database row. A staged CAS snapshot may remain
  unreachable and is safe to garbage-collect later.
- Full audit holds the Quest-head lock, preventing a mixed-generation read while another append is
  committing.
- Stream authorization timestamps are monotonic. A PostgreSQL/host wall-clock rollback fails
  closed and pauses new commits until the trusted clock catches up; production must monitor NTP
  and database clock health as an availability prerequisite.
- Cross-Quest object references, policy/root changes, scope changes, signature or receipt tampering,
  missing CAS bytes, snapshot changes, and incomplete outbox bundles fail closed.

PR-2 performs a full locked audit before and after every append. This favors immediate detection
over throughput, but total lifecycle work is `O(N^2)` in the number of Quest events and historical
CAS snapshots are read repeatedly. Do not treat this version as the long-campaign scaling design;
verified incremental tail/checkpoint proofs, catalog reuse, periodic full audits, and an explicit
snapshot-retention policy are required before that use.

## Policy-epoch limitation

PR-2 has one immutable policy epoch per Quest. Use short-lived, predeclared keys and cap the
Charter at their continuous coverage. If a key is suspected of compromise, commit an emergency
stop with an active separately delegated emergency key and commission a new Quest. Do not replace
the configured root when auditing the old stream: that correctly causes trust verification to
fail. Append-only policy rotation is future work and must preserve historical roots and per-event
policy epochs.

The database timestamp stored under the v1 name `committed_at` is sampled after the exclusive
Quest-head lock and exact-retry check. It is the serialized authorization decision point, not a
measurement of the later physical PostgreSQL COMMIT after CAS staging and full audit.

## Acceptance evidence

The final clean acceptance on 2026-08-24 used fresh isolated PostgreSQL databases:

- research-kernel/store focused suite: `158 passed`;
- write-inventory, dependency-boundary, and schema focused suite: `116 passed`;
- complete PR-0 compatibility gate: `166 passed`;
- full non-Docker partition: `1643 passed, 3 skipped, 29 deselected`;
- real Docker partition: `29 passed, 1646 deselected`;
- fresh store integration: `14 passed`; schema migration suite: `11 passed`;
- empty upgrade through `0023`, legacy Quest backfill, `0023 → 0022 → 0023`, bidirectional
  authority-collision rejection, and `alembic check`: passed.

During acceptance the host/PostgreSQL clock stepped backward by about 4.2 seconds once. The
monotonic stream guard rejected the write as designed; after clock recovery the complete store
suite passed `14/14`. No caller clock or permissive fallback was added.
