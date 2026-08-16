# F11-S2 Transactional Scientific Transitions Implementation Report

Date: 2026-08-17

## Outcome

F11-S2 is engineering-complete. At-least-once worker delivery can now terminate at an exact
PostgreSQL scientific commit: migrated domain state, canonical command result, immutable receipt,
and one keyed durable event either all commit or all roll back. Exact redelivery returns the first
receipt without invoking the scientific mutation again, while changed input under a reused command,
idempotency, or source-event identity fails closed.

Prediction commitment, observation validation, and belief update are three distinct commands.
Validation cannot silently create a successor posterior. Stage decisions, artifact batches, and the
accepted F9 world-model continuation path also use the transactional event boundary.

One-time final-holdout and external-validation openings now have a durable action intent. Only the
first claimant receives the raw authorization token; the database stores its SHA-256. Completion
atomically binds the domain result to a canonical provider receipt and immutable keyed event. If a
claim becomes stale, it enters `reconciliation_required` and no replacement token is issued.

This closes F11-S2, not F11. It guarantees exact local commits and at-most-one authorization from
Aletheia. It does not claim globally atomic or exactly-once execution across an arbitrary provider,
laboratory, or data custodian. F11-S3 through F11-S7 remain.

## Related-work basis

The implementation uses three narrow distributed-systems contracts:

- [AWS transactional outbox guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)
  identifies database/event dual writes as the failure boundary and recommends persisting both in
  one transaction, with idempotent duplicate handling downstream.
- [PostgreSQL transaction semantics](https://www.postgresql.org/docs/current/glossary.html) provide
  the atomic all-or-none boundary used for domain rows, receipts, and events.
- [Stripe's idempotent request contract](https://docs.stripe.com/api/idempotent_requests) is a
  concrete primary-source example of binding one remote key to one parameter set and replaying the
  first result. Aletheia derives and passes a stable provider key, but only relies on provider replay
  when that provider explicitly supports it.

The separate F11-S1 queue remains at-least-once. A queue acknowledgement is not a scientific
receipt, and an expired execution lease does not prove that a remote effect never happened.

## Durable schema

Three forward-only Alembic revisions extend the single migration head:

1. `20260817_0010` adds `scientific_commands`, `one_time_external_actions`, and
   `external_action_receipts`; binds artifact batches and stage decisions to scientific command
   IDs; binds final-holdout/external-validation ledgers to action and receipt identities; and installs
   immutable-receipt triggers.
2. `20260817_0011` makes the shared PostgreSQL immutability trigger safe for both committed command
   rows and receipt rows.
3. `20260817_0012` makes action intent identity/token/request fields immutable, forbids deletion,
   enforces monotonic state/event versions, and adds composite foreign keys proving that a domain
   ledger's receipt belongs to its exact action.

`scientific_commands` uniquely binds command ID, idempotency key, optional source-event key, output
event key, and output event ID. It persists canonical input/result hashes and the full event
projection. A command is visible after commit only in the complete `committed` state; PostgreSQL
rejects later update/delete.

`one_time_external_actions` uniquely binds action scope and provider idempotency key. It records the
canonical request, claim owner, token hash, reconciliation deadline, current state version, and last
keyed event. Its database trigger permits only the initial same-transaction event binding followed
by `claimed -> reconciliation_required -> completed` or direct `claimed -> completed`; identity
mutation and deletion fail. `external_action_receipts` admits exactly one canonical completion per
action and is database-immutable. Composite foreign keys prevent a holdout/external-validation row
from binding another action's receipt.

Legacy-baseline schema adoption excludes the new post-baseline objects while exact migration parity
continues to validate a fresh/current database.

## Scientific command boundary

`ScientificCommandSpec` canonicalizes immutable input and derives `scmd_<digest>` from command type
and idempotency identity. `ScientificTransitionStore.execute` then:

1. inserts an `applying` command under its unique identities;
2. invokes the mutation callback with the same SQLAlchemy session;
3. freezes and hashes the callback result and event projection;
4. writes one immutable keyed event to the existing `events` table;
5. stores the exact event/result receipt and changes the command to `committed`; and
6. commits the entire unit once.

The existing event table is the outbox because SSE and durable consumers already resume from its
database ID. A second relay-only table would create another truth surface without improving the
local atomicity guarantee.

Exact replay reconstructs and hashes the stored request/result/event before returning it with
`created=false`. Concurrent insertion races have the same outcome: one callback and one receipt.
Injected failures after domain state but before event, and after event but before receipt, prove that
all local writes roll back.

## Epistemic phase separation

`aletheia.epistemics.transactional` adds three typed wrappers:

- `commit_prediction_transactionally` persists the source world snapshot and immutable
  pre-observation prediction;
- `commit_observation_validation_transactionally` persists validated observation state without a
  posterior successor; and
- `commit_world_belief_update_transactionally` persists source/successor snapshots and the belief
  update only after the separate validation fact exists.

The content-addressed archive is written first. If the database transaction later fails, an
unreferenced immutable file can remain, but no authoritative database transition exists without its
command/event receipt. This is preferable to a database claim pointing at missing archive bytes.

The existing F9 continuation transition now uses the keyed event store through its existing shared
transaction. Stage decisions carry their scientific command ID. Artifact rows carry command ID and
ordinal, making an exact batch replay incapable of inserting a second batch.

## One-time external-action boundary

`OneTimeExternalActionSpec` derives `act_<digest>` from action type and scope and derives a stable
provider idempotency key from action ID plus canonical request. `claim` atomically creates the action,
applies the domain opening callback, and writes state event version 1.

The creator receives a random raw execution token once. Only SHA-256(token) is stored. Every
concurrent or later claim receives the existing snapshot with no token. A forged token cannot
complete the action.

`complete` row-locks the intent, verifies the token, freezes outcome and provider receipt, advances
the action, writes the completion event, inserts the immutable `ExternalActionReceipt`, and applies
the domain result callback in one transaction. The receipt is flushed before a ledger stores its
immediate composite foreign key, but neither becomes visible until commit. Exact receipt replay is
accepted; changed outcome or provider content is rejected. A fault after either local write rolls
the action, event, receipt, and domain result back together.

`recover_stale` transitions an expired claim to `reconciliation_required`, writes a new state event,
and never returns a token. A still-running original execution can attach a late, independently
verified receipt with its original token. If process loss destroyed that token, the action remains
blocked; there is deliberately no automatic path back to `claimed` and no replacement authorization.

## Driver and ledger integration

Final holdout and pre-sealed external validation now follow the same sequence:

1. atomically claim the one-time action and move the sealed domain ledger to opened;
2. pass action ID and stable provider key into the execution specification;
3. execute the frozen protocol once;
4. atomically store result, provider receipt, receipt hash, completion event, and completed ledger
   state; and
5. on driver redelivery, return an existing completed result or stop for reconciliation if the first
   outcome is unknown.

Pre-F11 ledgers already opened without an action token remain fail-closed. Reconstructing or issuing
a new token would create a scientifically invalid second look.

The provider receipt currently records the local executor status and available sandbox/provider
identifier. Production adapters must replace or extend this with their authenticated provider/lab
receipt contract before real unattended outward execution is commissioned.

## Operator surface

`scripts/scientific_transactions.py` requires the exact current schema and supports:

- inspecting a committed scientific command;
- inspecting an external action and its receipt/status; and
- moving expired claims to reconciliation without issuing another token.

The detailed deploy, inspect, failure, and reconciliation procedure is in
`docs/jobs/TRANSACTIONAL_SCIENTIFIC_TRANSITIONS.md`.

## Fault and adversarial coverage

The F11-S2 tests cover:

1. exact scientific command replay without callback reinvocation;
2. changed command/source-event binding rejection;
3. rollback after domain state but before event;
4. rollback after event but before command receipt;
5. concurrent command delivery producing one mutation and receipt;
6. artifact-batch and stage-decision replay safety;
7. three separate prediction/validation/belief boundaries and exact replay;
8. one raw outward token under sequential and concurrent claims;
9. token hash-only persistence and forged-token rejection;
10. exact external receipt replay and changed-receipt rejection;
11. claim callback/event rollback;
12. stale-claim reconciliation without replacement authorization;
13. late verified completion from reconciliation state;
14. receipt and domain-result rollback before transaction commit;
15. final-holdout/external-validation ledger integration; and
16. PostgreSQL rejection of committed command/receipt mutation and action deletion/identity
    rebinding.

## Validation

Closeout evidence recorded while implementing this report:

- expanded F11-S2/F11-S1/migration suite: 53 passed in 114.91 s;
- scoped Ruff over every changed Python file: passed;
- Alembic head: `20260817_0012`;
- ORM/Alembic schema differences: 0;
- full non-Docker regression: 1258 passed, 1 skipped, 29 deselected in 761.13 s;
- warnings: 2611 existing spglib deprecation warnings only; and
- `git diff --check`: passed.

## Files

- `aletheia/jobs/{outbox,actions,persistence}.py`
- `aletheia/events/store.py`
- `aletheia/epistemics/transactional.py`
- `aletheia/epistemics/continuation.py`
- `aletheia/memory/{ledger,service}.py`
- `aletheia/scheduler/{statemachine,driver}.py`
- `migrations/versions/20260817_0010_f11_scientific_transactions.py`
- `migrations/versions/20260817_0011_f11_receipt_trigger_fix.py`
- `migrations/versions/20260817_0012_f11_immutable_action_intents.py`
- `scripts/scientific_transactions.py`
- `tests/jobs/test_outbox.py`
- `tests/epistemics/test_transactional_commits.py`
- `tests/test_campaign_split_ledger.py`
- `tests/epistemics/test_world_model_continuation.py`
- `tests/test_schema_migrations.py`
- `docs/jobs/TRANSACTIONAL_SCIENTIFIC_TRANSITIONS.md`
- `docs/adr/0034-f11-transactional-scientific-commands-and-external-action-receipts.md`

## Honest boundary and next work

F11-S2 means durable redelivery no longer duplicates migrated scientific state or reauthorizes a
one-time holdout/external action. It does not prove every future plugin has an authenticated receipt,
make external systems transactionally atomic with PostgreSQL, or automatically resolve unknown
remote outcomes.

The next implementation slice is F11-S3: define immutable Quest and Program identities, make the
Quest/Program/Campaign/Experiment hierarchy and dependency graph reconstructible from the ledger,
bind budgets and data roles to that graph, enforce acyclicity and lifecycle transitions, and expose
the UI strictly as a view/controller over durable state.
