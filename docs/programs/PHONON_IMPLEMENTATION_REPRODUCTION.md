# Phonon implementation-diverse reproduction

This is the precommitted scientific evidence producer for the first production F11 endurance Quest.
It tests whether the retained F10 aligned-structure signal survives a separately implemented
same-source path. It does not claim independent external replication, mechanism, or causality.

## Separation from the F10 implementation

The frozen F10 path uses repository feature helpers plus
`sklearn.random_forest_regressor`. The reproduction path in
`aletheia/domains/materials/phonon_reproduction.py` instead:

1. reloads the exact licensed `matbench_phonons` bytes;
2. uses public Matminer/Pymatgen APIs directly rather than the F10 feature helper functions;
3. independently reconstructs composition and species-blind geometry matrices;
4. requires both matrix hashes and the target/split hashes to equal the zero-fit F10 plan;
5. creates a new within-role permutation from a separately frozen seed;
6. fits capacity-matched `ExtraTreesRegressor` arms exactly once; and
7. independently cluster-bootstraps both internal-validation and locked-holdout roles.

This gives implementation and estimator diversity while holding the source, target, split, model
capacity, and acceptance threshold fixed. Because the dataset is still the original public computed
corpus, the maximum conclusion is same-source implementation reproduction.

## Zero-fit preparation

The production protocol can be prepared only from tracked, committed code. Preparation binds the
production commissioning, real endurance gate/controller, original and reproduction Campaigns,
exact source files and their internal model hashes, package versions, estimator/permutation/
bootstrap policies, and the same-source claim ceiling.

In the commands below, replace `vN` with the matching immutable controller/protocol versions from
the final pre-start commit; older files remain historical and are never overwritten.

~~~bash
conda run -n aletheia python scripts/run_phonon_reproduction.py prepare \
  --controller artifacts/phonon-quest/endurance/controller-manifest-vN.json \
  --commissioning artifacts/phonon-quest/commissioning-manifest.json \
  --dataset workspaces/evaluator/materials-structure-phonons-v1/source/matbench_phonons.json.gz \
  --source-plan workspaces/evaluator/materials-structure-phonons-v1/plan.json \
  --source-result workspaces/evaluator/materials-structure-phonons-v1/result.json \
  --reproduction-campaign-id cmp_3343df54838b7b5a4742f46ab160f1b5 \
  --output artifacts/phonon-quest/endurance/reproduction/protocol-vN.json

conda run -n aletheia python scripts/run_phonon_reproduction.py preflight \
  artifacts/phonon-quest/endurance/reproduction/protocol-vN.json \
  --controller artifacts/phonon-quest/endurance/controller-manifest-vN.json
~~~

Preflight rehashes committed code and source files, verifies the frozen Git commit contains those
exact component bytes, checks the active original-evidence Campaign and planned reproduction
Campaign, confirms that the endurance gate has not started, and reports `model_fit_count=0`. It does
not inspect a new model outcome.

## In-window workflow

Only after the external supervisor and all other gate work orders are deployed may an operator
explicitly start the endurance controller. Then activate the already-precommitted reproduction
branch:

~~~bash
conda run -n aletheia python scripts/run_phonon_reproduction.py activate \
  artifacts/phonon-quest/endurance/reproduction/protocol-vN.json \
  --principal controller:phonon-science
~~~

`run` fails unless the exact gate is live and both original/reproduction Campaigns are active. It
fits the three arms and takes `completed_at` from PostgreSQL only after computation finishes:

~~~bash
conda run -n aletheia python scripts/run_phonon_reproduction.py run \
  artifacts/phonon-quest/endurance/reproduction/protocol-vN.json \
  --output artifacts/phonon-quest/endurance/reproduction/result.json

conda run -n aletheia python scripts/run_phonon_reproduction.py verify \
  artifacts/phonon-quest/endurance/reproduction/protocol-vN.json \
  artifacts/phonon-quest/endurance/reproduction/result.json
~~~

The retained result is mechanically classified as:

- `confirmed` only when both roles meet the frozen positive-CI and relative-MAE floor;
- `contradicted` when a stable aligned advantage does not survive; or
- `inconclusive` for positive but non-robust evidence.

Commit physically replays the result before any mutation. It registers an exact result,
negative-result, or limitation memory fact according to that classification, then writes one typed
reproduction envelope to the endurance controller spool:

~~~bash
conda run -n aletheia python scripts/run_phonon_reproduction.py commit \
  artifacts/phonon-quest/endurance/reproduction/protocol-vN.json \
  artifacts/phonon-quest/endurance/reproduction/result.json \
  --controller artifacts/phonon-quest/endurance/controller-manifest-vN.json \
  --result-uri artifacts/phonon-quest/endurance/reproduction/result.json \
  --principal controller:phonon-science \
  --producer worker:phonon-independent-replay
~~~

Exact retry reuses the memory command and evidence envelope. A contradicted result becomes a
non-droppable negative fact for `pivot-analysis`; it does not automatically stop a Campaign,
activate a successor, or claim that a structural pivot occurred. Such a pivot needs a separately
assessed changed prediction/strategy and its actual graph transitions.

The production contradiction-only implementation is documented in
`PHONON_NEGATIVE_RESULT_PIVOT.md`. It accepts this commit receipt only when the exact conclusion is
`contradicted`; confirmed and inconclusive outcomes remain non-pivots.

## Tests and honest boundary

~~~bash
conda run -n aletheia pytest -q tests/domains/materials/test_phonon_reproduction.py
~~~

The tests prove matrix parity from the independent path, estimator-family separation, deterministic
physical replay, target-drift rejection, disposition anti-relabeling, safe paths, distinct Campaign
identity, and the committed-code requirement. They use synthetic data. No production reproduction
model has been fit yet, so there is no outcome to report and no real endurance evidence has been
created by this module.
