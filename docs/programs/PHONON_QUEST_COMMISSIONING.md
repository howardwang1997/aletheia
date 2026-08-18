# Structure/phonon Quest commissioning

This runbook converts the real F10 `matbench_phonons` structure-signal evidence into the first
production-shaped F11 Quest. It commissions a scientific program; it does not claim an independent
replication, a mechanism, a completed endurance run, or authority for autonomous outward actions.

## What is frozen

Preparation validates and content-binds the exact local dataset, pre-fit plan, and retained result.
The current evidence boundary is:

- 1,265 public retrospective computed structures;
- one chemical-system-disjoint three-arm plan;
- aligned structure versus composition and a capacity-matched within-role permutation;
- result SHA-256 `f1384600dfbc8289e6643aae13e6dbb16b0b429c89e7325bd83c88cd8522bb29`;
- robust predictive information on the same source corpus; and
- explicit prohibition on causal/mechanism and independent-external-replication claims.

The manifest also freezes the live commissioning code matrix, one Quest, one Program, one shared
scientific family, three Campaigns, three stable legacy Runs, two complete F9 world models, one
exploration-only DataAsset, five Quest/Program budget pairs, and all initial lifecycle transitions.

## Scientific branches

| Campaign | Initial state | Question or purpose | Precommitted boundary |
|---|---|---|---|
| independent implementation replay | active | Does the source result survive an independently reviewed implementation path? | one formal attempt; confirmed, contradicted, or inconclusive retained |
| local versus global ablation | planned | Is gain carried mainly by local packing, global lattice/symmetry, or neither? | matched capacity; group-disjoint; locked role cannot select the analysis |
| independent calculation corpus | planned | Does directional gain transfer after source/target qualification? | no registration before lineage audit; target-blind matching; one-time external opening |

Each research question contains exactly one null, one primary, and one credible alternative
hypothesis. Every hypothesis has an explicit assumption, violation risk, discriminating prediction,
measurement-protocol hash, and normalized prior. These are priors, not observations.

## External-corpus research boundary

The commissioning records candidates, not eligible data allocations:

- [Phonondb's official migration index](https://github.com/atztogo/phonondb) points to NIMS MDR
  PBEsol calculations and documents finite-displacement inputs. Its target extraction, calculation
  independence, and overlap with the source still require an audit.
- [Alexandria's official datasets page](https://alexandria.icams.rub.de/datasets.html) lists PBE
  phonon recalculations of MDR materials and original PBEsol runs. This is promising for a
  separately calculated target, but workflow lineage and target harmonisation are not yet frozen.
- [Materials Project's official phonon methodology](https://docs.materialsproject.org/methodology/materials-methodology/phonon-dispersion)
  traces the legacy DFPT corpus to the same Materials Project/Petretto family as the current
  Matbench task. A fresh API extraction is therefore excluded as independent data.
- [Phonix](https://phonix-db.org/) is a 2026 anharmonic/thermal-conductivity resource built from
  Materials Project and Phonondb structures. It is useful for a distinct-property program, not a
  drop-in external last-harmonic-DOS-peak validation.

This classification is deliberately conservative. Shared initial structures do not necessarily
mean shared target calculations, but neither a different download URL nor a different file format
proves independence.

## Two-phase operator flow

Use the project Conda environment. Store manifests under an access-controlled artifact directory;
the CLI creates output write-once and refuses to replace an existing file.

~~~bash
conda run -n aletheia python scripts/commission_phonon_quest.py prepare \
  --workspace workspaces/evaluator/materials-structure-phonons-v1 \
  --principal aletheia:autonomous-scientist \
  --output artifacts/phonon-quest/commissioning-manifest.json
~~~

Preparation performs no database mutation. Rehash it before applying:

~~~bash
conda run -n aletheia python scripts/commission_phonon_quest.py verify \
  artifacts/phonon-quest/commissioning-manifest.json
~~~

Apply the exact manifest:

~~~bash
conda run -n aletheia python scripts/commission_phonon_quest.py apply \
  artifacts/phonon-quest/commissioning-manifest.json \
  --output artifacts/phonon-quest/commissioning-replay-receipt.json
~~~

Runs and the source DataAsset use stable primary keys with insert-or-verify semantics. F9 snapshots
are content addressed. Every graph operation uses a commissioning-derived scientific-command
idempotency key. A crash can therefore be resumed with the same manifest; identical work returns
existing receipts, while changed content under a stable identity fails closed.

Before any research transition beyond the commissioned state, retain the read-only initial audit:

~~~bash
conda run -n aletheia python scripts/commission_phonon_quest.py audit \
  artifacts/phonon-quest/commissioning-manifest.json \
  --output artifacts/phonon-quest/commissioning-initial-audit.json
~~~

The initial audit is intentionally exact. It rejects extra nodes, dependencies, bindings, family
rows, allocations, state transitions, changed Runs/DataAssets, changed world models, or local
artifact/code drift. Once real Campaign progress begins, the general graph/endurance ledgers—not
this initial-state audit—become the authoritative evolving view.

## Resource and authority boundary

Quest/Program caps are frozen for USD, GPU hours, tokens, wall-clock hours, and experiment count.
The three legacy Run caps fit the Program's USD/GPU caps. Only the F10 source is allocated, and only
as exclusive `exploration` data. Its policy permits replay, independent implementation, and
ablation design, while forbidding external-replication, causal-mechanism, and experimental claims.

The manifest sets outward actions and autonomous allocation to false. It does not download a
candidate corpus, spend money, open an external target, start a fault campaign, or start the
72-hour clock.

## Remaining commissioning blockers

The immutable initial apply receipt continues to report the four blockers present when it was
created. Subsequent receipt-backed work has now passed the Quest-scoped ten-boundary fault campaign
and completed the restart-safe controller engineering/accelerated acceptance. The live blockers
are therefore:

1. qualify and register a genuinely independent calculation corpus;
2. freeze the zero-fit implementation-diverse protocol and commit its still-unknown in-window result;
3. freeze and preflight the committed production gate/controller manifests; and
4. explicitly start and complete the real 72-hour gate after workers/supervisor are deployed.

F11 scientific exit additionally needs real database-clock elapsed time, on-cadence checkpoints, a
negative result, a genuine hypothesis/prediction pivot, in-window process/provider interruptions,
a replay-verified portfolio epoch, and material efficiency improvement. F12 requires stronger
reality-linked independence beyond this commissioning.

The gate-bound same-source producer is documented in
`programs/PHONON_IMPLEMENTATION_REPRODUCTION.md`. Its claim ceiling remains below independent
external replication even if the implementation-diverse result confirms the source signal.

## Tests

~~~bash
conda run -n aletheia pytest -q tests/domains/materials/test_phonon_commissioning.py
~~~

The suite covers closed competing world models and claim ceilings, pre-mutation artifact-drift
rejection, first application, exact retry, stable graph reconstruction, and read-only audit.

## Current commissioned instance (2026-08-18)

The production-shaped local instance has been applied and immediately replayed/audited:

| Object | Identity |
|---|---|
| commissioning | `pcm_2bd8b42a47aab1afadb8781b0eec170d` |
| manifest content | `2bd8b42a47aab1afadb8781b0eec170d8388dcd37b389e18e47e9865f9f73f14` |
| Quest | `qst_cd143727c9e8c48fcff45ab6087db3d2` |
| Program | `prg_85ceadbe36c9135b9e44af05799a7e41` |
| initial active Campaign | `cmp_a269864873184e324da89875e1a52c9e` |
| exact initial graph | `41a47946b28c9685468b5946e6b782c7f9979a8c2e9fada6d201a4b2c34286b8` |
| source DataAsset | `f9ca6cfab83c973b6d3f8ceb0118f66d` |

The first application created 31 durable objects. An immediate second application created zero and
replayed all 31. The commissioning audit and the independent general research-graph CLI rebuilt the
same graph hash. Local write-once evidence is retained under `artifacts/phonon-quest/` (gitignored):

| File | File SHA-256 |
|---|---|
| `commissioning-manifest.json` | `7dd478efc9375d32bf97dc12b466ccd118aaf87c974c2a048a7704f157f3fbc5` |
| `commissioning-replay-receipt.json` | `1f86faa0ec6d60e0e34e5f1a3d3e8dc2d765208eb1e1071907d34caf84ae933d` |
| `commissioning-initial-audit.json` | `2bc419ba9996a8b291a0fdabc33bba53dce003bd409902245c13e9eefc97850d` |

This means the bounded Quest now exists and its independent-implementation Campaign is active. It
does not mean that Campaign has produced a reproduction result or that any of the four blockers is
closed.
