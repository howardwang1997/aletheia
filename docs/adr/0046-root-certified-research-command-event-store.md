# ADR 0046: Root-certified research commands and an authoritative event store

- Status: accepted
- Date: 2026-08-24
- Scope: PR-2 authoritative research-kernel persistence

## Decision

New-Quest scientific authority is committed only by
`aletheia.research_store.store.ResearchKernelStore.commit`. A model or planner may create a typed
`ResearchCommandProposal`, but that value has no persistence method. A commit requires an
`AuthorizedResearchCommand` carrying an Ed25519 signature over the complete canonical mutation
message, including Quest/scope identity, expected version and tail, idempotency identities, event
type and typed payload, proposal digest, principal, signing key, trust-root and policy digests, and
authorization time. The authorization receipt is mechanically derived from those signed bytes.

Command keys do not bootstrap their own authority. A deployment-pinned
`ResearchAuthorizationTrustRootV1` certifies one immutable, Quest-scoped
`ResearchAuthorizationPolicyV1`. That policy assigns each principal to exactly one of four roles:
commissioning, ordinary command, charter amendment, or emergency stop. Genesis is commissioning
only; charter revision is amendment only; an emergency key can only commit an emergency stop;
all other events require an ordinary key. The persisted stream stores the complete certified
policy JSON plus the trust-root and policy digests. Historical audit revalidates the policy
certificate and every command signature against the externally supplied frozen trust root.

The transaction locks the Quest head, rechecks exact idempotency after any lock wait, and only
then reads PostgreSQL `clock_timestamp()`. That value is the Quest-local authorization
linearization time persisted in the v1 field named `committed_at`; it is not a claim to be the
physical PostgreSQL COMMIT instant after CAS and audit work finish. Key activity and Charter expiry
are evaluated at this serialized database time, not a command argument or injectable application
clock. Emergency stop bypasses Charter expiry only while its separately delegated emergency key
remains active; ordinary commands and amendments do not bypass expiry. Pending actions are resolved
from the replayed graph and CAS catalog and are rechecked against the current Charter version and
allowed authority classes before authorization or transition.

A fixed-policy v1 Charter must have a finite `expires_at`. At commissioning and every amendment,
each delegated emergency and amendment principal must have continuous, gap-free same-role key
coverage from the authorization linearization time through Charter expiry, and at least one
ordinary principal must have the same continuous coverage. Revocation truncates a key interval.
This prevents commissioning a Quest that is already unable to halt, amend, or perform ordinary
work inside its declared lifetime.

An emergency command is a Quest-wide halt, not an ordinary branch decision. While its separately
delegated emergency key is active, it may be authorized after Charter expiry. It selects the exact
deterministic virtual authority marker derived from the Quest and current Charter, so it requires
neither an admitted Action object nor CAS bytes and cannot smuggle action-selection alternatives.
The reducer atomically changes every `admitted`, `active`, or `paused` branch to `stopped`, makes the
graph terminal, and rejects every later event. A wrong marker, inactive emergency key, ordinary
signer, or non-emergency use of this shortcut fails closed.

The same PostgreSQL transaction writes the command receipt, append-only event, exact object
metadata admission, replay snapshot metadata, transactional outbox row, and compare-and-swap Quest
head. Deferred constraints require a complete event/object/snapshot/outbox/head bundle at commit.
Object and snapshot bytes live only in the content-addressed archive. The filesystem adapter writes
to a same-directory temporary file, fsyncs it, and atomically publishes with a no-overwrite link;
an interrupted write cannot leave a partial final digest object.

The archive is pinned when `ResearchKernelStore` is constructed; commit/audit/replay callers cannot
substitute a different per-call custodian. The current receipt binds content identity and canonical
storage key, not a remotely attested backend identity. Production service composition must keep the
pinned archive instance private. PR-4a adds a separate local qualification-only artifact store, not
a cross-node custodian identity/attestation; that remote custody contract remains deferred.

`program_id` and `campaign_id` are immutable compatibility/routing fields in the Quest scope
binding. They do not create a second Program/Campaign scientific authority. Existing
`ScientificTransitionStore` and `/programs` mutations remain explicitly legacy-scoped and cannot
write the new research-store tables.

An immutable `research_quest_authorities` namespace claim is the only shared state between the
legacy Program graph and the kernel store. Existing legacy Quest roots are backfilled as
`legacy_program_graph`; creation triggers atomically claim either that authority or
`research_kernel_v1` and reject the opposite claim for the same `qst_*` identity. A deferred
binding check rejects orphan claims. Thus route labels are not the only defense against parallel
scientific authority.

Migration `20260824_0023` owns exactly seven durable tables:
`research_quest_authorities`, `research_quest_streams`, `research_kernel_objects`,
`research_kernel_command_receipts`, `research_kernel_events`, `research_kernel_snapshots`, and
`research_kernel_outbox`. The namespace is a shared identity guard, not shared scientific state;
the other six tables belong to the new kernel authority. The migration locks legacy Quest creation
while backfilling and installing its claim trigger, closing the concurrent-insert gap. Claims are
immutable, and deferred binding requires each claim to have exactly the matching legacy Quest root
or kernel stream.

The production HTTP mutation boundary is
`POST /research-kernel/programs/{program_id}/quests/{quest_id}/commands`; it accepts only the full
`AuthorizedResearchCommand` and forwards it unchanged. The authenticated HTTP user has transport
permission but never replaces the signed scientific principal. The signed Quest and immutable
Program scope must match the path. The same scope check protects
`GET .../audit` and `GET .../replay`. The former `/research-graph` surface is removed; the retained
compatibility routes live only under the OpenAPI-deprecated `/legacy/research-graph` prefix and
remain `legacy_program_graph` authority. There is no legacy-to-kernel dual write.

HTTP composition is fail-closed and has no development authority defaults. A deployment must set
absolute, pre-existing pins for the trust-root JSON and its raw file SHA-256, the canonically
ordered Quest-to-genesis-policy registry and its raw file SHA-256, and the filesystem CAS root.
The service rejects a missing pin, digest mismatch, symbolic-link traversal, non-regular control
file, non-directory CAS root, invalid root-certified registry policy, or missing exact Quest policy
with `503`; authority is never synthesized from request data or persisted stream data.

## Trust and operational limits

PR-2 deliberately freezes one policy epoch for the lifetime of a Quest. It does not implement
post-commissioning key rotation or revocation updates. Keys must therefore be short-lived and
predeclared, and the Charter lifetime must fit inside the coverage rule above. A suspected
compromise requires an emergency stop while its emergency key is active and a newly commissioned
Quest.
A future typed policy-epoch event must authorize rotation under the previous epoch, bind the next
policy object, and preserve all historical trust roots for replay; mutating the existing stream
policy is forbidden.

The trust-root file is deployment authority. Persisting it beside a stream would make replay
self-contained but would not prove that the deployment trusted it at genesis. Operators must pin
and retain the externally approved root artifact. The CLI consequently requires `--trust-root`
for audit and replay and never invents or accepts an allow-all policy.

PostgreSQL append-only triggers protect the application role, not the database owner. Production
must separate a narrowly privileged application role from the migration/owner role; the runtime
role must not be able to disable triggers, truncate authority tables, or execute Alembic DDL. This
role separation is a deployment requirement and is not proven by the local test database.

The database clock is also an availability dependency. Stream authorization timestamps cannot move
backward: a PostgreSQL/host clock rollback is rejected until the trusted clock catches up. Operators
must monitor NTP and database clock health; code must not replace this fail-closed guard with a
caller-provided or merely monotonic application timestamp.

`ResearchCharterVersion` remains wire-compatible v1. Its `authority_receipt_sha256` is external
Charter provenance, distinct from the cryptographic per-command receipt. Ordinary command
principals live in the certified Quest policy; adding them to the Charter requires an explicit v2
schema rather than changing v1 hashes in place.

## Consequences

- A persisted event log plus CAS and the frozen external trust root is cryptographically and
  deterministically auditable across processes.
- A caller cannot select an arbitrary verifier, inject a clock, submit an unsigned model proposal,
  rebind an idempotency key, cross a Quest/scope boundary, or write ORM records through a reviewed
  production import path.
- Snapshot CAS writes may leave unreachable temporary or complete objects after rollback; they
  never create an authoritative database event without matching custody. Later garbage collection
  may remove unreachable CAS entries.
- PR-2 deliberately performs a full locked audit before and after an append. This maximizes early
  correctness but makes a Quest lifecycle quadratic in event count and repeatedly reads historical
  CAS snapshots. Before long-running autonomous campaigns, a later store revision needs verified
  incremental tail/checkpoint proofs, catalog reuse, periodic full audits, and an explicit snapshot
  retention policy without weakening the event log as source of truth.
- Legacy scientific stores are compatibility projections only for legacy scopes. No dual write to
  the new graph is permitted.
