# F9-S8 transactional world-model continuation implementation report

Date: 2026-08-15
Status: Engineering slice complete; F9 scientific exit not complete

## Outcome

F9 can now persist a validated posterior and its negative-result revisions as one replayable state
transition, then consume the exact physical next snapshot in a second causal-planning round after
independent K3 authorization. The old F9-S2 campaign remains immutable.

## Implemented surface

- `WorldModelTransition`, three explicit dispositions, deterministic revision closure, and typed
  event projection in `aletheia/epistemics/continuation.py`.
- `epistemic_world_model_transitions` immutable PostgreSQL table and Alembic revision
  `20260815_0005`.
- Session-scoped snapshot storage so source, posterior, materializations, next snapshot, transition,
  and event share one commit/rollback boundary.
- Physical reload of snapshots, standalone revised versions, relational columns, transition payload,
  and unique event.
- K3-gated next-round authorization with exact round/update/persistence/terminal bindings.
- `CausalWorldModelSource` in F9-S3 and effective-snapshot propagation through F9-S4–S7.
- Thin scheduler entry points with no second derivation path.
- Fail-closed fork behavior for retirement and all-model hypothesis-set failure.

## Acceptance evidence

Focused F9-S8 tests cover:

- closed assumption/prediction/belief rebinding after narrowing;
- rejection of a detached or hand-edited next snapshot;
- database migration/ORM equality and immutable trigger;
- transaction rollback when typed-event insertion fails;
- exact round trip, idempotent retry, and unique event projection;
- stable update-to-transition identity conflict;
- independent K3 authorization and a real second F9-S3 causal campaign using the child state;
- missing physical acceptance archive rejection;
- stop-action rejection; and
- retirement-to-fork behavior.

The focused suite currently passes 12 tests. Full F9 and repository regression results are recorded
after final verification below.

## Verification

- Focused F9-S8: `12 passed`.
- F9-S3 regression before S8 tests: `38 passed`.
- F9-S4–S7 regression after effective-source refactor: `117 passed`.
- Complete `tests/epistemics`: `214 passed in 446.18s`.
- Full non-Docker repository: `1131 passed, 1 skipped, 29 deselected in 803.65s`.
- Real Docker suite: `29 passed, 1132 deselected in 28.28s` on the final full run. The first Docker
  attempt hit one 30-second CORE-Bench candidate-container infrastructure timeout; the exact failed
  test immediately passed (`1 passed in 1.99s`) and the subsequent complete Docker rerun was green.

## Explicit non-claims

This slice proves an engineering state hand-off, not a scientific result. It does not yet provide a
frozen hidden-world K3-versus-K2 benchmark, posterior calibration, false-mechanism measurement,
authenticated laboratory evidence, or a real materials alternatives-to-update campaign.

**Subsequent status (2026-08-15):** F9-S9 implemented and froze the hidden-world comparison,
truth-relative calibration/false-mechanism endpoints, and decision machinery. No live/private
matrix has passed it, so the scientific non-claim above remains unchanged in evidentiary terms.
