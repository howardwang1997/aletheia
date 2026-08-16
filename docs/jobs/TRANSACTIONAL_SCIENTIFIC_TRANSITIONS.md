# Transactional scientific transitions

F11-S2 is the exact-commit layer beneath the F11-S1 at-least-once queue. It makes migrated
scientific state replay-safe and gives one-time holdout/external actions an explicit receipt and
reconciliation lifecycle.

## Two guarantees, kept separate

| Operation | Guarantee | Recovery after unknown outcome |
| --- | --- | --- |
| PostgreSQL scientific transition | domain rows, command result, and keyed event commit atomically; exact replay applies once | redeliver the same content-bound command |
| External/one-time action | one durable Aletheia authorization, one raw token, stable provider key, one immutable receipt | inspect/reconcile; never automatically issue another token |

The second row is deliberately not called cross-system exactly-once. A remote effect can finish
while its response is lost.

## Deploy

Apply all F11-S2 revisions before starting API or workers:

~~~bash
conda run -n aletheia alembic upgrade head
conda run -n aletheia alembic current
~~~

The expected head is `20260817_0015`; its F11-S3 revisions permit portfolio-scoped commands and add
the Quest/program graph without changing the F11-S2 receipt semantics.

## Scientific command lifecycle

A caller freezes a `ScientificCommandSpec`, then supplies a mutation callback. The callback must use
the received session and must not call `commit`:

~~~python
spec = ScientificCommandSpec(
    run_id=run_id,
    command_type="scientific.generic",
    aggregate_type="candidate_set",
    aggregate_id=candidate_set_id,
    idempotency_key=f"candidate-set:{candidate_set_id}:v1",
    source_event_key=delivery_event_key,
    input={"candidate_set_id": candidate_set_id, "items": items},
    principal="candidate-validator",
    event_type="candidate_set_committed",
)

def apply(session):
    # Add/flush domain rows with this session; do not commit it.
    ...
    return ScientificMutation(
        result={"candidate_set_id": candidate_set_id},
        event_projection={"candidate_set_id": candidate_set_id, "count": len(items)},
    )

receipt = ScientificTransitionStore().execute(spec, apply)
~~~

On exact redelivery, `receipt.created` is false and `apply` is not called. Changed input under the
same command, idempotency, or source-event identity raises `ScientificIdempotencyConflict`.

The built-in migrated boundaries are:

- `commit_prediction_transactionally`;
- `commit_observation_validation_transactionally`;
- `commit_world_belief_update_transactionally`;
- `record_transition` for stage decisions;
- `record_artifacts` for artifact batches; and
- the accepted F9 world-model continuation state/event transaction.

Prediction, validation, and belief update must remain three commands. In particular, successful
validation does not create a successor posterior until the belief-update command commits.

## One-time action lifecycle

Claim before revealing data or invoking a provider:

~~~python
claim = OneTimeExternalActionStore().claim(
    OneTimeExternalActionSpec(
        run_id=run_id,
        action_type="provider.request",
        scope_key=f"provider-request:{logical_request_id}",
        request=request,
        principal="research-worker",
        claim_ttl_seconds=3600,
    ),
    claim_owner=worker_id,
)
~~~

Only `claim.created is True` carries `claim.execution_token`. Persist neither the raw token nor a
copy in logs/artifacts. Pass `claim.action.provider_idempotency_key` to a provider that supports
idempotent calls.

Complete with the exact returned token and the provider's identifying response:

~~~python
completion = OneTimeExternalActionStore().complete(
    action_id=claim.action.action_id,
    execution_token=claim.execution_token,
    outcome=validated_outcome,
    provider_receipt={"request_id": provider_request_id},
    completed_by=worker_id,
    on_complete=commit_domain_result,
)
~~~

`on_complete` receives the same database transaction as the receipt and event. Exact callback
replay is accepted; a changed result or provider receipt fails closed.

## Final holdout and external validation

The experiment driver uses the action protocol automatically:

1. `claim_final_holdout` or `claim_external_validation` atomically moves the sealed ledger to opened
   and returns the only raw token;
2. the driver passes the stable provider key into the execution specification;
3. `record_final_holdout_result` or `record_external_validation_result` atomically stores the domain
   result and receipt; and
4. a redelivered driver returns an existing completed result, but refuses an opened action without
   a receipt.

An old ledger already opened before F11-S2 has no recoverable token. It is intentionally reported as
requiring reconciliation and is never reopened.

## Inspect and recover

Inspect a command or action:

~~~bash
conda run -n aletheia python scripts/scientific_transactions.py command <scmd-id>
conda run -n aletheia python scripts/scientific_transactions.py action <action-id>
~~~

Move expired claims into the explicit reconciliation state:

~~~bash
conda run -n aletheia python scripts/scientific_transactions.py recover-actions \
  --principal operator:external-action-recovery \
  --limit 100
~~~

This command never returns or creates execution tokens. For every recovered action:

1. inspect the action request hash and provider idempotency key;
2. query provider/lab/custodian records without repeating the effect;
3. if a verifiable response and the original in-memory token both survive, complete with that token
   and the exact provider receipt;
4. if the token was lost with the worker or the outcome is still unknown, leave the action in
   `reconciliation_required` rather than fabricating a completion;
5. create a new logical action only after a domain-specific human decision establishes that doing so
   is scientifically and operationally safe.

## Failure interpretation

| Symptom | Meaning | Safe action |
| --- | --- | --- |
| command identity conflict | the same logical identity was rebound to changed content | compare stored request/input hashes; create a new explicit identity only for genuinely new work |
| command callback/event exception | the entire local transaction rolled back | redeliver the identical command |
| action claim returns no token | the one authorization already exists | inspect its status/result; do not invoke again |
| `reconciliation_required` | local ownership expired while remote outcome or authorization continuity is unknown | reconcile using provider key and records; without the original token, keep it blocked |
| invalid completion token | caller cannot prove possession of the original authorization | reject and investigate provenance |
| changed completion receipt | a completed outcome is being rebound | retain the first immutable receipt |
| immutable-row database error | direct SQL attempted to edit a committed command/receipt or one-time action identity | do not patch history; append a new explicit correction transition |

## Acceptance suite

Run the focused contract:

~~~bash
conda run -n aletheia pytest -q \
  tests/jobs/test_outbox.py \
  tests/jobs/test_durable_queue.py \
  tests/test_campaign_split_ledger.py \
  tests/epistemics/test_transactional_commits.py \
  tests/epistemics/test_world_model_continuation.py \
  tests/test_schema_migrations.py
~~~

It covers exact and conflicting replay, duplicate source events, concurrent commands/claims, crash
points on both sides of event/receipt writes, forged tokens, immutable database receipts, stale
claim reconciliation, late verified completion, phase-separated epistemic commits, and migration
parity.

## Remaining boundary

F11-S2 closes scientific-command and one-time-action replay for the migrated paths. It does not yet
provide the Quest/Program graph (F11-S3), receipt-preserving memory compaction (F11-S4), portfolio
selection (F11-S5), broad stochastic fault injection (F11-S6), or the 72-hour endurance run
(F11-S7). Provider-specific receipt verification and production operator key custody must be
commissioned before unattended real outward actions are enabled.
