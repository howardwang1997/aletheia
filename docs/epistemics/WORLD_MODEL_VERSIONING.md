# F9-S1 world-model versioning guide

## What is available

F9-S1 provides an append-only representation for one closed set of competing explanations. It is
the storage and identity layer beneath later hypothesis generation, causal audit, prediction
commitment, experiment selection, and belief update.

The public package is `aletheia.epistemics`. Its core objects are:

| Object | Stable lineage | Immutable version identity | Required exact binding |
|---|---|---|---|
| `ResearchQuestion` | `question_id` | `question_sha256` | run and scope |
| `HypothesisVersion` | `hypothesis_id` | `hypothesis_sha256` | question version |
| `Assumption` | `assumption_id` | `assumption_sha256` | hypothesis version |
| `Prediction` | `prediction_id` | `prediction_sha256` | hypothesis and measurement protocol |
| `BeliefState` | `belief_lineage_id` | `belief_state_sha256` | question plus every hypothesis version |
| `WorldModelSnapshot` | content root | `snapshot_sha256` | all objects above |

All models are frozen Pydantic objects with `extra="forbid"`. Canonical hashes use the repository's
JSON canonicalization. A revision keeps its stable ID, increments `version` by exactly one, and
sets the matching `parent_*_sha256`. Do not use `model_copy(update=...)` to bypass validation;
revalidate a complete payload and persist it as a child.

## Complete snapshot rules

`WorldModelSnapshot` is intentionally stricter than an arbitrary draft graph:

1. hypotheses are canonically sorted and contain exactly one H0, one primary explanation, and at
   least one credible alternative;
2. every hypothesis has an explicit assumption and discriminating prediction;
3. every child names the exact current question/hypothesis hash, not only a human-readable ID;
4. a belief vector covers exactly the current hypothesis set, uses canonical order, and sums to one;
5. assumptions and predictions are also canonical and cannot repeat a lineage/version;
6. no member may have a freeze time after the snapshot.

These are integrity rules, not a quality score. F9-S2 now provides an exact F8-grounded admission
path that retains raw drafts, independently resolves semantic duplicates, and requires complete
pairwise observable discrimination before creating this snapshot. F9-S3 must still assess causal
identification. F9-S4 must commit predictions before observation. See
[`F8_GROUNDED_HYPOTHESIS_GENERATION.md`](F8_GROUNDED_HYPOTHESIS_GENERATION.md).

## Persistence

Upgrade a reviewed database with:

```bash
conda run -n aletheia alembic upgrade head
```

World-model persistence was introduced at `20260815_0004`; deploy the current repository head,
`20260825_0024`. Application startup fails closed when the deployed schema differs from the current
head.

Store and reload only a fully validated snapshot:

```python
from aletheia.epistemics import get_world_model_snapshot, store_world_model_snapshot

receipt = store_world_model_snapshot(snapshot)
loaded = get_world_model_snapshot(receipt.snapshot_sha256)
assert loaded == snapshot
```

An identical retry returns `created=False`. Reusing the same stable lineage/version with different
content raises `ImmutableEpistemicConflict`. A missing, skipped, wrong-lineage, or time-reversed
parent raises `EpistemicLineageError`. Read validates both JSON payloads and normalized rows.

Direct SQL `UPDATE` and `DELETE` on all F9 tables are rejected. A correction must be a child version
and a new snapshot. Back up the database before migration; do not disable the triggers to repair an
object in place.

## K2 compatibility

Legacy K2 calls remain unchanged:

```python
from aletheia.memory.service import get_credence, list_credences, upsert_credence
```

Read their explicitly labelled projection with:

```python
from aletheia.epistemics import list_legacy_k2_belief_compat

rows = list_legacy_k2_belief_compat(run_id)
```

The projection derives only `P(holds)` from the existing Beta parameters. It is not inserted into
the F9 tables and cannot authorize a multi-hypothesis or causal claim. Writes through
`k2_belief_state_compat` are rejected. Continue using the existing K2 service for a legacy campaign;
new F9 consumers should require an exact `WorldModelSnapshot` hash.

## Failure and recovery

- If migration `0004` fails, the transaction rolls back; inspect the database and rerun only after
  addressing the reported schema conflict.
- If an insert conflicts, load the already-bound stable version. If its content is correct, reuse
  it; otherwise allocate a child version—never delete it.
- If a load detects missing/tampered membership, treat the snapshot as unusable evidence and restore
  from a verified backup.
- If the compatibility view rejects a legacy row because alpha/beta are invalid, repair the legacy
  campaign through a reviewed migration; do not coerce it into an F9 posterior.

## Current boundary

F9-S1 itself does not call an LLM, search literature, create a causal DAG, read an observation,
calculate a likelihood, select an experiment, or update probabilities. F9-S2 can now consume a
separately implemented deterministic or model-backed proposal adapter, but that adapter has no tools
or observation access and cannot bypass F8 or snapshot admission. The repository has only synthetic
F9-S2 adapters today. F9-S3 now adds an explicit typed causal contract and conservative back-door
audit above the snapshot; it too has only synthetic adapters and reviewed assumptions. Prediction
commitment, likelihoods, selection, and updates remain later, separately testable gates.
