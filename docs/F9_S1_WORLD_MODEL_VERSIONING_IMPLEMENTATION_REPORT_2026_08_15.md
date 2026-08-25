# F9-S1 world-model schema and versioning implementation report

- Date: 2026-08-15
- Scope: competing-hypothesis schemas, immutable lineage/version persistence, and K2 compatibility
- Engineering status: complete
- Scientific-exit status: not complete

## Outcome

F9-S1 adds the first durable competitive-world-model layer without changing the existing K2
campaign loop. A complete F9 snapshot now requires a null hypothesis, primary explanation, and
credible alternative, with exact assumptions, discriminating predictions, and a normalized belief
vector over their immutable versions.

The implementation separates stable scientific lineage from content identity. Every revision must
name its immediate parent; old content remains queryable and database triggers prohibit update or
delete. The legacy K2 Beta row and event stream remain untouched and readable through both their
existing service and an explicitly labelled, non-writing compatibility view.

This is an engineering result. Fixtures contain synthetic mechanisms, assumptions, predictions,
and probabilities. It does not show that Aletheia can generate a credible alternative, identify a
causal effect, calibrate a likelihood, or choose an informative real experiment.

## Research basis

- [W3C PROV-DM](https://www.w3.org/TR/prov-dm/) and
  [PROV Constraints](https://www.w3.org/TR/prov-constraints/) support the stable-entity versus
  fixed-version/revision distinction;
- [Bayesian Workflow](https://arxiv.org/abs/2011.01808) motivates retaining iterative construction,
  checking, expansion, and comparison of multiple models;
- [Pearl 1995](https://escholarship.org/uc/item/6gv9n38c) motivates keeping causal assumptions as
  explicit queryable objects rather than prose hidden inside a selected hypothesis;
- [Tsilifis et al. 2017](https://epubs.siam.org/doi/10.1137/15M1043303) motivates the later EIG
  selector while also making clear why an explicit probabilistic model is needed first.

ADR 0016 translates those ideas into repository invariants and records rejected alternatives.

## Delivered contracts

`aletheia/epistemics/schemas.py` adds frozen, extra-forbid contracts for:

- `ResearchQuestion`;
- `HypothesisVersion`;
- `Assumption`;
- `Prediction`;
- multi-hypothesis `BeliefState` and `HypothesisBelief`;
- closed `WorldModelSnapshot`;
- explicitly legacy `LegacyK2BeliefView`.

Stable IDs use typed prefixes and 32 hexadecimal identity bytes. Content SHA-256 values cover the
entire canonical payload. Initial versions prohibit a parent; all later versions require one.

The closed snapshot enforces three-way competition, exact child bindings, at least one assumption
and prediction per hypothesis, canonical ordering, complete normalized beliefs, and time closure.
A belief attributed to a validated observation requires both observation and likelihood receipts;
other state types cannot carry them.

## Durable persistence and migration

`aletheia/epistemics/persistence.py` and Alembic revision `20260815_0004` add seven normalized tables:

```text
epistemic_research_questions
epistemic_hypothesis_versions
epistemic_assumptions
epistemic_predictions
epistemic_belief_states
epistemic_belief_state_members
epistemic_world_model_snapshots
```

Primary keys are content hashes. Stable `(lineage, version)` identities are unique. Parent,
question, hypothesis, belief-member, and snapshot bindings use foreign keys. A shared trigger
rejects every update/delete. Storage is atomic, exact-load validating, conflict detecting, and
idempotent; creation detection uses `INSERT ... ON CONFLICT DO NOTHING RETURNING`, avoiding
driver-dependent row-count semantics.

Revision `0004` creates no K2 rows and executes no update against `belief_states` or the event
ledger. `k2_belief_state_compat` projects the existing alpha, beta, update count, and derived mean,
labels the representation `legacy_k2_beta_bernoulli`, and rejects view writes through an
`INSTEAD OF` trigger.

## Test evidence so far

Focused schema, persistence, migration, mutation, history, and compatibility checks:

```text
31 passed
changed Python Ruff + compilation: passed
```

Coverage includes:

- complete H0/primary/alternative closure;
- frozen Pydantic mutation rejection;
- stable IDs, content-hash changes, and exact immediate parents;
- missing, spurious, invented, and skipped parents;
- wrong hypothesis/question binding;
- non-normalized and reordered belief vectors;
- observation/likelihood receipt pairing;
- canonical assumption and prediction ordering;
- exact, idempotent PostgreSQL round trip;
- same-lineage/version content conflicts;
- simultaneous readability of old and revised snapshots;
- database update/delete rejection;
- ORM/migration parity and compatibility-view presence;
- unchanged K2 service reads/writes, exact Beta mean projection, no F9 backfill, and rejected view
  writes.

K2/F9 integration and final repository acceptance:

```text
K2/F9 integration: 136 passed in 15.63 s
non-Docker:         938 passed, 1 skipped, 29 deselected in 314.65 s
Docker:              29 passed, 939 deselected in 38.37 s
```

The first Docker matrix attempt had one evaluator-owned ScienceAgentBench image-environment probe
time out at 30 seconds before the candidate test began; the other 28 tests passed. The exact failed
case then passed alone in 0.34 seconds, and the complete clean rerun produced the 29/29 result above.
No timeout policy or benchmark code was weakened.

## Files added or materially changed

- `aletheia/epistemics/__init__.py`;
- `aletheia/epistemics/schemas.py`;
- `aletheia/epistemics/persistence.py`;
- `migrations/versions/20260815_0004_f9_world_model.py`;
- `aletheia/schema_migrations.py` and `migrations/env.py`;
- `tests/epistemics/` and `tests/test_schema_migrations.py`;
- `docs/adr/0016-f9-immutable-competing-world-model-versioning.md`;
- `docs/epistemics/WORLD_MODEL_VERSIONING.md`;
- this report, migration guide, README, docs index, and master-plan status.

## Explicit non-guarantees

- no F8-grounded competing-hypothesis generator or semantic duplicate detector;
- no causal variable/edge/latent-confound schema or identification audit;
- no independently reviewed assumption disposition;
- no pre-observation signed prediction receipt;
- no observation validator, likelihood implementation, posterior update, sensitivity analysis, or
  negative-result policy;
- no EIG/cost/risk experiment selector;
- no K3 acceptance scorer or scheduler integration;
- no evidence that the synthetic belief vector is calibrated;
- no completion of F9 engineering or scientific exit.

## Next slice

F9-S2 should generate the active hypothesis set from an exact F8-grounded research question. For a
mechanism question it must produce H0, one primary explanation, and at least one credible
alternative, remove semantic duplicates without deleting provenance, and block or downgrade the
question when no genuinely discriminating prediction can be formed.
