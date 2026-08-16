# F9-S8 transactional world-model continuation

## What is available

F9 now has a durable cross-round state transition:

```text
committed F9-S6 update + exact revision materializations
  -> deterministic closed transition
  -> one PostgreSQL transaction
       source snapshot
       posterior snapshot
       revised hypothesis/prediction versions
       revision-closed next snapshot
       transition record
       typed scheduler event
  -> independent committed F9-S7 verdict
  -> physically reload transition and event
  -> exact CausalWorldModelSource for the next F9-S3 round
```

The implementation is `aletheia.epistemics.continuation`. The scheduler-facing
`aletheia.scheduler.k3_continuation` functions delegate to the same persistence and authorization
paths.

## Transition dispositions

| Disposition | Next snapshot | Meaning |
|---|---:|---|
| `ready_next_round` | yes | Current set remains usable after any narrow revisions. |
| `measurement_redesign_required` | yes | Beliefs/revisions are reusable, but the next round must design a different measurement. |
| `hypothesis_set_fork_required` | no | A retirement or all-model miss requires a newly generated competing set. |

## Building and committing

```python
from aletheia.epistemics import (
    build_world_model_transition,
    persist_world_model_transition,
)

transition = build_world_model_transition(
    transition_id="transition:question-7:round-1",
    round_evidence=round_evidence,
    revision_materializations=revision_materializations,
    persistence_principal_sha256=persistence_principal_sha256,
    persisted_at=persisted_at,
)
receipt = persist_world_model_transition(transition)
```

`persisted_at` must postdate the update and all revision materializations. The source run must exist
in the `runs` table. Apply migration `20260815_0005` before using this service.

An identical retry returns `created=False` and the original event ID. Reusing either the transition
ID or update receipt for different content fails with `ImmutableEpistemicConflict`.

## Revision closure

For a `narrow` directive, F9-S7 already requires an exact-parent hypothesis and an exact set of
substantively changed prediction children. F9-S8 additionally creates:

- one child version of every assumption bound to the narrowed hypothesis;
- a belief-state child whose probabilities are unchanged but whose hypothesis-version bindings are
  current; and
- a new closed world snapshot containing the revised hypothesis, assumptions, predictions, and
  belief state.

This second belief child has `update_kind="hypothesis_revision"`; it cannot reuse the observation
or likelihood receipts from the preceding Bayesian update.

A `retire` directive is persisted, but no ordinary next snapshot is emitted. The generator must
fork/replenish the active hypothesis set before causal and prediction planning resume.

## Authorizing the next round

```python
from aletheia.scheduler.k3_continuation import authorize_k3_next_round

world_model_source = authorize_k3_next_round(
    transition_sha256=transition.transition_sha256,
    committed_acceptance=committed_k3_acceptance,
    acceptance_archive=acceptance_archive,
    authorized_at=authorized_at,
)
```

Authorization fails closed unless all of the following hold:

- the transition, every snapshot/version, exactly one typed event, and the acceptance campaign
  physically reload;
- the verdict is `accepted` or integrity-valid `partial_no_scientific_exit`;
- every mandatory F9-S7 check is non-failing and a positive validated update passed;
- the acceptance final round and terminal update exactly equal the transition;
- transition persistence predates the K3 evidence ledger and uses its persistence principal;
- source and posterior hashes appear in that ledger; and
- terminal action is `continue_research` or `seek_new_measurement`.

Pass the returned source to both `build_causal_contract_request(..., world_model_source=source)` and
`run_causal_identification_audit(..., world_model_source=source)`. The campaign records the full
source and its hash; downstream prediction, selection, update, and acceptance stages use that exact
snapshot.

## Failure and recovery

- A failure before the typed event is written rolls back snapshots, versions, transition, and
  event together.
- Immutable database triggers reject transition updates and deletes.
- `get_world_model_transition` revalidates relational index columns, full payload, materialized
  version rows, all closed snapshots, and the unique event projection.
- Missing or altered event state blocks retry/consumption instead of manufacturing a replacement.

## Current limits

- This is a typed service and scheduler entry point, not yet a policy loop inside the monolithic
  `Driver`.
- A retirement/fork requires a future F9-S2 hypothesis-set continuation protocol.
- Contradiction resolution remains append-only/open; F9-S8 only carries exact contradiction
  evidence forward.
- All current cross-round acceptance fixtures are synthetic. F9-S9 now supplies the hidden-world
  K3-versus-K2/headline comparison plus posterior-calibration and false-mechanism thresholds, but
  its live/private execution and a real materials chain remain scientific-exit gates.
