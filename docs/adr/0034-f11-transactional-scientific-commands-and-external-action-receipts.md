# ADR 0034: Transactional scientific commands and external-action receipts

Date: 2026-08-17

## Status

Accepted for the F11-S2 engineering boundary. F11-S3 through F11-S7, production provider
commissioning, and the final reality-linked replication gate remain open.

## Context

F11-S1 made task delivery durable and explicitly at-least-once. That exposed two different replay
problems which a queue cannot solve by itself:

1. a worker can write a scientific row and die before publishing its event or recording that the
   command completed; and
2. a worker can reveal a holdout or invoke a remote system and die before recording the response.

The first problem is local and can be solved with one PostgreSQL transaction. The second crosses a
system boundary: no local database transaction can prove whether an arbitrary remote side effect
happened after a connection was lost.

The previous implementation also blurred three epistemically different facts. A prediction being
committed, an observation being validated, and a posterior being updated must be separate durable
transitions. Validation alone must not silently advance belief.

The design follows narrowly scoped, primary guidance:

- [AWS transactional outbox guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)
  describes the database/event dual-write failure and recommends writing state and an outbox row in
  one transaction; it also warns that consumers must tolerate duplicate delivery.
- [PostgreSQL transaction semantics](https://www.postgresql.org/docs/current/glossary.html)
  define a transaction as an atomic unit whose commands all succeed or all fail.
- [Stripe idempotent-request semantics](https://docs.stripe.com/api/idempotent_requests)
  demonstrate a useful remote boundary: bind one key to one parameter set and replay the first
  result. Aletheia passes such a stable key when the provider supports it, but does not assume every
  provider implements this contract.

## Decision

### 1. A scientific command is the exact local commit boundary

Every migrated scientific mutation is described by a frozen `ScientificCommandSpec`. Its
deterministic command ID binds command type and idempotency key; its request hash binds run,
aggregate, canonical input, principal, source-event identity, and output event type.

`ScientificTransitionStore.execute` inserts the command, invokes a callback with the same
SQLAlchemy session, persists the callback result, writes one keyed event, and marks the command
committed inside one PostgreSQL transaction. The callback may flush but may not commit. A failure at
any point rolls back the domain rows, command, result, and event together.

The existing keyed `events` table is the transactional outbox. It is already the durable source for
SSE and later consumers, so a second relay-only outbox table would duplicate truth without adding a
delivery guarantee.

### 2. Replay is content-bound, not best-effort deduplication

`scientific_commands` has unique command, idempotency, optional source-event, output-event key, and
output-event ID identities. Exact redelivery returns the persisted receipt without invoking the
mutation callback. Reusing any identity with changed content fails closed.

The receipt binds the canonical request and input hashes, canonical result and hash, committed
timestamp, and exact keyed event ID. PostgreSQL rejects update or deletion of a committed command;
reads reconstruct and re-hash the persisted contract.

### 3. Prediction, validation, and belief update stay separate

Three wrappers establish distinct command types and events:

| Boundary | Persists | Must not imply |
| --- | --- | --- |
| `prediction.commit` | source world snapshot and pre-observation prediction | validated observation or posterior change |
| `observation_validation.commit` | validated observation campaign | posterior change |
| `belief_update.commit` | source and successor world snapshots plus update | a prediction or validation that is absent from its own receipt |

Content-addressed archive files are written before the database command. A rolled-back database
transaction may therefore leave an unreferenced immutable file, which is safe to garbage-collect;
it cannot leave an authoritative database transition without its command/event receipt.

### 4. Stage decisions and artifact batches use the same command contract

Each stage decision and its `stage` event now commit together. Each artifact batch is normalized,
content-hashed, and bound to one command; `(scientific_command_id, commit_ordinal)` prevents a replay
from inserting another batch. Existing F9 world-model continuation writes its keyed event in the
same transaction as the accepted transition.

### 5. An outward action has one durable authorization and one raw token

`OneTimeExternalActionStore.claim` first persists an intent, domain opening state, and keyed claim
event in one transaction. The action ID binds type and scope; the request hash binds its immutable
content. A deterministic provider idempotency key is derived from the action and request.

Only the transaction that creates the intent receives the raw execution token. PostgreSQL stores
only SHA-256(token). Concurrent or later claims receive the existing snapshot and no token, so a
queue redelivery cannot authorize a second holdout look or external invocation.

A database trigger rejects deletion, identity/token/request mutation, non-monotonic state versions,
or any transition outside `claimed -> reconciliation_required -> completed` (with direct
`claimed -> completed` also allowed). Composite foreign keys require each holdout/external ledger's
receipt hash to belong to that same action ID.

Completion requires the original token. Domain result state, canonical outcome, provider receipt,
immutable `ExternalActionReceipt`, and keyed completion event commit in one transaction. Exact
completion replay returns the first receipt; changed outcome or provider receipt is rejected.

### 6. Unknown remote outcome requires reconciliation, never automatic reissue

The action states are:

~~~text
claimed --valid token + receipt------------------------------> completed
   |
   | reconciliation deadline passes; no new token is issued
   v
reconciliation_required --late original token + receipt-----> completed
~~~

Recovery changes stale `claimed` actions to `reconciliation_required` and emits a durable event. It
does not issue a replacement token. An operator may inspect the provider using the stable provider
key; if the original token still exists, it can attach the verified late receipt. If the worker and
token were lost, or the outcome cannot be established, the action remains blocked rather than
fabricating authorization or execution history.

This is an at-most-one Aletheia authorization protocol, not a claim of globally atomic or magical
exactly-once remote execution. A provider which ignores the idempotency key can still perform an
effect and lose its response; the safe response is reconciliation, not retry.

### 7. Holdout and external-validation ledgers bind their action receipts

Final-holdout and external-validation opening now occurs in the same transaction as the action
claim. Their result rows bind the action and immutable receipt hash. Pre-F11 rows already marked
opened without an action receipt remain fail-closed because their raw authorization cannot be
reconstructed safely.

## Consequences

- Worker redelivery can neither duplicate a migrated scientific update nor create a second event.
- A process crash at any local commit point leaves either the whole transition or none of it.
- Final holdout and external validation cannot be reopened automatically after an unknown result.
- Providers that support idempotency receive a stable, content-derived key suitable for safe lookup
  or replay under that provider's documented retention contract.
- Operators now have an explicit `reconciliation_required` queue instead of an ambiguous
  “opened but missing result” state.
- Immutable archive files may be orphaned by rollback and require later content-addressed garbage
  collection.
- This slice does not build the Quest/Program graph, compact memory, plan a portfolio, run the broad
  fault-injection matrix, or pass the 72-hour endurance gate.

## Rejected alternatives

### Publish the event after committing scientific state

Rejected because a crash between the two writes loses the observable transition, while retry can
duplicate the scientific mutation.

### Mark a task successful and infer that the scientific state committed

Rejected because delivery state is not scientific evidence and task acknowledgement can fail
independently of the scientific transaction.

### Reissue an external action after its claim lease expires

Rejected because the first invocation may have succeeded before its response was lost. Lease expiry
proves loss of local ownership, not absence of the remote effect.

### Persist the raw execution token for operator convenience

Rejected because any database reader or replaying worker could then recover authorization for a
second one-time action. Only its SHA-256 is retained.

### Claim cross-system exactly-once execution

Rejected because PostgreSQL cannot atomically commit with an arbitrary provider, laboratory, or
private holdout custodian. Stable provider keys and receipts improve reconciliation; they do not
erase this distributed-systems boundary.
