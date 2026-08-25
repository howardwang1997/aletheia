# F10 materials capability replication

This runbook covers the provisional band-gap range-compression capability and its frozen five-slot
replication matrix. It produces exploratory evidence only.

## Frozen identities

| Object | SHA-256 |
|---|---|
| provisional manifest v1 (retained) | `3f67775a74a78eba77d8c009ea413997b2e033fafeab188ade397bf47f89d972` |
| provisional manifest v2.0.0 | `ff07124b21e53391158eb2a91b6c6af23dfc8195aafa97f4df4c371fc525dd24` |
| registry v2 snapshot | `56c167d9c2571c79f0bfd78bd2abb3bf5dc85ce9896da24b85df781bb58a1329` |
| replication implementation | `30024692489e2538ead1018e29c689a1056cfddee7b288e343fe447f636ca880` |
| replication plan | `554492251d7e0fe26e2d1f173b834f1a59a3b7411449573a29ee98ba42ad2939` |
| final aggregation | `1ba278061a230960b5ee150db1c757ff39b4c5af0decdbde1603477d34a59222` |
| final bundle | `56f183f0e23d72e42f86c4485f3dbb58a2e8a80bf6d456a2f7f24fff75a5f80e` |

Local immutable artifacts are under
`workspaces/evaluator/materials-capability-replication-v1`. Signing keys are separate mode-0600
files under `.keys/` and must never be committed.

## Commands

Use the project Conda environment:

```bash
conda run -n aletheia python scripts/capability_registry.py query \
  --registry workspaces/evaluator/capabilities/materials_registry_v2.json \
  --query configs/capabilities/queries/materials_band_gap_range_compression_exploratory_v2.yaml

conda run -n aletheia python scripts/real_materials_replication_e2e.py inspect \
  --plan workspaces/evaluator/materials-capability-replication-v1/plan.json

conda run -n aletheia python scripts/real_materials_replication_e2e.py verify \
  --bundle workspaces/evaluator/materials-capability-replication-v1/bundle.json \
  --measurement-key workspaces/evaluator/materials-capability-replication-v1/.keys/measurement.key \
  --validation-key workspaces/evaluator/materials-capability-replication-v1/.keys/validation.key \
  --recompute
```

`run-all` is resumable only from valid success checkpoints. A retained `failure.json` forbids a
retry under the same plan. Existing JSON is never replaced.

## Frozen rule and observed matrix

The plan freezes seeds 20260818–20260822, one measurement attempt per seed, two exact
recomputations per slot, all-slot retention, and a 4/5 consensus rule.

| Seed | Outcome | Unseen compression | Control compression | Delta | 95% cluster-bootstrap interval |
|---:|---|---:|---:|---:|---:|
| 20260818 | unseen-specific | 0.2947 | 0.2068 | 0.0879 | [0.0210, 0.1547] |
| 20260819 | unseen-specific | 0.2388 | 0.1506 | 0.0882 | [0.0161, 0.1657] |
| 20260820 | generic shrinkage | 0.2220 | 0.1868 | 0.0352 | [-0.0334, 0.1104] |
| 20260821 | generic shrinkage | 0.2162 | 0.1720 | 0.0442 | [-0.0215, 0.1124] |
| 20260822 | ambiguous | 0.2662 | 0.1821 | 0.0841 | [-0.0015, 0.1676] |

Mean/median delta are 0.06793/0.08411 and the between-partition sample SD is 0.02603. Every point
delta is positive, but only two intervals lie strictly above zero. The frozen aggregation is
`partition_sensitive`; there is no consensus outcome and no joint posterior.

## Interpretation boundary

This is a real, reproducible capability demonstration on one public retrospective benchmark. It is
not a prospective dataset, independent implementation, external site replication, registered
capability, physical material measurement, or mechanism result. The correct next action is to
improve typed observation/measurement identity and obtain independent review or fresh evidence—not
to open more unregistered seeds.
