# F9-S10 real-materials evidence-chain implementation report

Date: 2026-08-15
Status: Engineering and real-data chain complete; F9 scientific exit remains blocked

## Outcome

F9 now has an executable real-materials path with three versioned competing explanations,
observation-blind EIG selection, pre-observation likelihood commitment, real Matbench measurement,
separately keyed physical validation, and validated-outcome-only Bayesian update.

The authoritative v2 run completed end to end and returned
`valid_update_without_robust_contraction`. This is a scientifically informative non-exit: the
controlled experiment favored generic model shrinkage, but the conclusion was not strong enough
under the skeptical likelihood to satisfy the frozen contraction threshold.

## Implemented boundary

- `aletheia/domains/materials/k3_evidence.py`
  - frozen protocol and hypotheses;
  - complete likelihood scenarios and EIG derivation;
  - content-addressed dataset, feature, target, group, partition, prediction, and model identities;
  - deterministic chemical-system partition and cluster bootstrap;
  - HMAC measurement and validation envelopes;
  - exact physical validation by full recomputation;
  - nominal plus conservative/skeptical posterior updates;
  - conservative revision and explicit mechanism-claim withholding; and
  - bundle replay and signature/derivation audit.
- `scripts/real_k3_materials_e2e.py`
  - `inspect`, `preregister`, `measure`, `validate`, `update`, and `verify` commands;
  - create-only atomic JSON artifacts; and
  - no benchmark loading during inspection or preregistration.
- `configs/materials/k3_band_gap_range_compression_v1.yaml`
  - retained first frozen protocol.
- `configs/materials/k3_band_gap_range_compression_v2.yaml`
  - current protocol with a new split and all-scenarios retirement semantics.
- `tests/domains/materials/test_k3_evidence.py`
  - selection, observation blindness, signed update, sensitivity contraction, conservative revision,
    forgery rejection, physical recomputation, and CLI tests.

## Frozen experiment

Question: does a composition-only random forest compress prediction spread more for unseen chemical
systems than for held-out rows from chemical systems represented in training?

The two feasible candidates had:

| Candidate | Expected information gain |
|---|---:|
| unseen systems versus represented-system control | 0.380368 nats |
| random holdout only | 0.003148 nats |

The selector chose the controlled candidate without observation access. The v2 protocol fixed 300
trees, minimum leaf size 2, feature fraction 0.70, model/split seed 20260817, 1000
chemical-system-cluster bootstrap resamples, outcome rules, priors, and three likelihood scenarios.

## Evidence chronology and identities

| Boundary | Identity/time |
|---|---|
| protocol | `4aaa54b387f35cc841af8cdcc928ce053391dc90e4e1e20c60bb62209b57e949` |
| implementation | `0bd322d28a989a79457802bfc0e0be89758e71b370c391729fdf0a79f9ed136c` |
| preregistration | `62b26527d43b0a088dfd286863538d89626188ee8f9223b822ea84fb353fd327` |
| dataset logical rows | `83e824a05131f1c16131783c8ffdaa9bb2dcb7c82f651621fe3c5b1403148ebf` |
| signed observation | `4ad60bd6308dea6c6e049a2ecfd99d9ac18815732cc56af93306e3ad39369713` |
| recomputed result | `571e0ae1c6c8f45f8ba5d5c958241b6b350c663fc64341a2ae5450474c682cf6` |
| signed validation | `98b9e1a2074830a5a1c6b7de9ee53fe1b2daf3e59e9883cf659dbd4fd25f468d` |
| final bundle | `7163113d8d93058156fde1762271dceea0d1872f2e0a0a5f22d9629e8a41b270` |

The final `verify --recompute` pass reloaded the public benchmark, rebuilt all Magpie features,
reconstructed all partitions, retrained the model, reran 1000 bootstraps, verified both signatures,
and rederived the update and decision exactly.

## Measured result

The v2 split contained 3273 training rows, 921 rows from 767 unseen chemical systems, and 410 control
rows from 385 systems retained in training.

| Metric | Value |
|---|---:|
| unseen true/predicted SD | 1.4481 / 1.0993 eV |
| unseen compression | 0.2409 |
| control true/predicted SD | 1.3591 / 1.0944 eV |
| control compression | 0.1948 |
| unseen minus control | 0.0461 |
| cluster-bootstrap 95% interval | [-0.0140, 0.1145] |
| bootstrap probability delta > 0 | 0.930 |
| unseen/control MAE | 0.4337 / 0.3642 eV |

The frozen classifier therefore emitted `generic_model_shrinkage`, not
`unseen_system_specific_compression`.

## Belief update

The prior was H0/H1/H2 = 0.30/0.40/0.30. Posterior families were:

| Likelihood scenario | H0 | H1 unseen-specific | H2 generic | Effective-count contraction |
|---|---:|---:|---:|---:|
| nominal | 0.1607 | 0.1429 | 0.6964 | 0.2329 |
| conservative | 0.2105 | 0.2105 | 0.5789 | 0.1099 |
| skeptical | 0.2727 | 0.2909 | 0.4364 | 0.01339 minimum |

H2 is the winner in every scenario, but the minimum contraction is only 1.34%, below the frozen 10%
threshold. H0 and H1 are narrowed rather than retired. No mechanism claim is issued.

## Superseded v1 attempt

The v1 20260816 result was fully signed and reproduced: unseen/control compression 0.2288/0.1607,
delta 0.0681, interval [0.00535, 0.1309], and nominal H1 posterior 0.8235. A post-run code audit found
that the revision layer used nominal posterior alone to retire H0 even though the skeptical H0
posterior was 0.1343. The implementation and all v1 artifacts were retained under
`workspaces/evaluator/materials-k3-band-gap-v1`; nothing was overwritten. The v2 implementation
requires a hypothesis to fall below the retirement floor in every likelihood scenario.

## Verification

- Focused materials K3 tests: `5 passed`.
- Combined materials K3 plus hidden-world K3 focused tests: `11 passed` before the live v2 run.
- Broader evaluator, epistemics, non-Docker, and Docker regressions: pending final verification.
- Targeted Ruff, formatting, import, protocol validation, and diff checks: pass.
- Real v2 measurement, independent recomputation, update, and third physical audit: pass.

## Scientific conclusion and next action

The project can now execute the required real alternatives → discriminating experiment → validated
update chain and preserve an unfavorable result. It cannot yet claim the F9 scientific exit: the
current v2 result lacks robust substantial contraction, is retrospective on a public benchmark, and
has local rather than external custody.

The next experiment must be registered as a complete multi-partition replication matrix before any
additional seeds are opened. That requirement moves directly into F10's registered experiment
capability and replication layer.
