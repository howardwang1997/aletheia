# ADR 0023: Transactional F9 world-model continuation

- Status: Accepted
- Date: 2026-08-15
- Scope: F9-S8 child-snapshot persistence and next-round consumption

## Context

F9-S6 could derive a child posterior snapshot and F9-S7 could independently verify its archived
evidence chain, but neither fact made that state the input to another experiment. The posterior was
not written with the revision materializations and scheduler event in one PostgreSQL transaction.
Moreover, replacing a hypothesis version alone creates an invalid world model: assumptions,
predictions, and belief members still point at the parent version.

This gap allowed two unsafe outcomes: a resumed scheduler could silently reuse the original F9-S2
prior, or it could pass a partially rebound object into prediction planning.

## Decision

Introduce a content-addressed `WorldModelTransition` with these rules:

1. It binds exactly one successful committed F9-S6 update and the exact F9-S7 revision
   materialization set.
2. Narrowing creates an exact-parent hypothesis child, uses the materialized prediction children,
   creates exact-parent assumption children, and creates a probability-preserving
   `hypothesis_revision` belief child. The result must validate as one closed
   `WorldModelSnapshot`.
3. Retirement or an F9-S6 hypothesis-set fork never flows directly into another round. It produces
   `hypothesis_set_fork_required` and no next-round snapshot.
4. Source, posterior, materialized versions, next snapshot, transition record, and one typed
   `f9_world_model_transition_committed` event are committed in one database transaction. Any
   failure rolls all of them back; identical retry is idempotent.
5. The next snapshot is exposed as a `CausalWorldModelSource` only after physically reloading the
   transition and checking an independent committed F9-S7 verdict. `continue_research` and
   `seek_new_measurement` may authorize continuation; `stop_and_archive` and
   `fork_hypothesis_set` may not.
6. F9-S3 embeds the exact world-model source hash and snapshot. F9-S4 through F9-S7 read the
   effective snapshot from the causal campaign, so the posterior/revision state—not the original
   F9-S2 prior—propagates through the next evidence chain.

The scheduler module delegates to the epistemics implementation and has no alternative mutation or
authorization formula.

## Consequences

- A database event and the scientific state it announces cannot diverge through a partial commit.
- Historical snapshots and all parent versions remain immutable and replayable.
- Narrowing is operational rather than documentary: a later prediction author sees the revised
  version and changed predictions.
- Retirement deliberately pauses autonomous continuation until a new competing set is generated.
- The transition payload is large because it contains the committed round evidence required for
  deterministic validation. This is accepted in favor of an unverifiable hash-only hand-off.

## Rejected alternatives

- **Mutate the F9-S2 campaign snapshot.** This destroys the content identity of an archived
  generation campaign.
- **Persist only the F9-S6 posterior.** This loses negative-result revisions and can replay stale
  predictions.
- **Let the scheduler copy hashes into an event.** An event without the same database transaction
  does not prove the referenced snapshot was committed.
- **Carry retired hypotheses as ordinary next-round candidates.** Current likelihood and causal
  contracts require an exact active set; silently treating retirement as active would negate the
  revision.
- **Authorize on acceptance disposition alone.** Continuation also requires exact round/update,
  persistence principal, timestamp, terminal action, and physical snapshot/event bindings.

## Scientific boundary

This decision completes an engineering state-transition invariant. It does not demonstrate that F9
outperforms K2, calibrate posteriors, authenticate a laboratory observation, or satisfy the F9
scientific exit.
