# K2 cuprate Tc campaign plan — 2026-06-16

## Purpose

This note records the revised plan for turning the current K2 materials campaign from the
`matbench_expt_gap` / band-gap / `delta_E` framing into a runnable K2 FULL candidate on the UCI
superconductivity Tc dataset.

Audience: future Claude / Codex development sessions and reviewers.

Scope: what must be built, what the resulting campaign can demonstrate, and how far that is from a
general AI scientist.

## One-line plan

Wire the cached UCI superconductivity dataset into the materials harness explicitly, then rewrite
the K2 live campaign around a chemistry-defined cuprate Tc blind-spot diagnostic:

> composition-only Magpie / random-forest Tc models make specifically larger errors on
> multi-alkaline-earth cuprates than on non-cuprates matched for generic composition complexity.

The result is a runnable K2 FULL candidate, not a guaranteed FULL result.

## Current code state

The repository already has most of the generic K2 spine:

- AI-authored `compute_demonstration(X, y, groups, meta)` support;
- explore / confirm split sealing;
- harness-owned `holds` derivation from pre-registration and negative control;
- reproduction;
- cross-vendor demonstration audit;
- belief priors, predictions, updates, persistence, and K2 acceptance scoring.

The current live K2 script is still the old band-gap campaign:

- `scripts/real_k2_campaign_e2e.py` creates a `matbench_expt_gap` run;
- it registers `target_column="gap expt"`;
- the objective, hypothesis, demonstration, metrics, and method all describe `delta_E` on band-gap
  prediction;
- the materials plugin profile and training report still contain band-gap semantics.

The cached UCI superconductivity data exists locally:

```text
artifacts/datasets/superconduct_unique_m.csv
rows: about 21,263
formula column: material
target column: critical_temp
target units: K
```

## Verified effect to target

The intended scientific object is diagnostic, not a new material claim.

Claim:

> Magpie composition features have a chemistry-specific blind spot on multi-alkaline-earth cuprates:
> the model error is larger on that family even after matching non-cuprate controls on element count
> and feature-space density.

The effect numbers must be stated with their controls separated:

- Raw error contrast: cuprate MAE about `11.1 K` vs rest about `5.1 K`.
- Permuted-strata null: median cuprate excess about `+5.6 K`, with null p95 about `0.5 K`.
- Complexity-matched control: cuprate excess about `+3.58 K`, bootstrap 95% CI about
  `[+2.46, +4.72]`.

These numbers should not be collapsed into one claim. The matched-control number is the cleanest
answer to the critic concern that "complex or rare formulas are just harder."

## Build plan

### Step 1 — Tc data wiring

This is more than swapping a dataset name.

Acceptance items:

1. Explicit data spec
   - Register the UCI file with:
     - `source="upload"` or equivalent local-file source;
     - `uri="artifacts/datasets/superconduct_unique_m.csv"`;
     - `target_column="critical_temp"`;
     - `composition_column="material"`.
   - Propagate `composition_column` through the data registry / `resolve_data_spec` path so it is
     visible in the effective `data_spec` and in AI authoring prompts.
   - Do not rely on the current first-non-numeric-column heuristic.

2. Tc-aware materials semantics
   - Prevent hardcoded band-gap semantics from leaking into Tc runs.
   - At minimum, report `units="K"` and avoid `gap` labels for Tc.
   - Prefer a small target-semantics layer selected by `target_column` or dataset reference:
     - band-gap: eV, gap-range stratifier, Matbench profile;
     - superconductivity Tc: K, Tc-range stratifier, UCI superconductivity profile.

3. Focused tests
   - Prove the upload/local-file Tc path loads `material` and `critical_temp` explicitly.
   - Prove Magpie featurization returns aligned `X`, `y`, and chemical-system `groups`.
   - Prove a canned cuprate `compute_demonstration` can run through the AI-authored demonstration
     capability on a confirm split of the Tc data.
   - Prove the harness derives `holds` from the pre-registration and control statistic.

### Step 2 — Port the K2 campaign framing

Rewrite the live campaign plan, not only the title.

Replace all delta_E / band-gap content in `scripts/real_k2_campaign_e2e.py`:

- run objective;
- dataset registration;
- plan objective;
- direction;
- hypothesis;
- demonstration claim;
- method;
- metrics;
- success criteria;
- estimated compute;
- any profile wording surfaced to ideation, authoring, or review.

Important design constraint:

The AI-authored demonstration currently receives only:

```python
compute_demonstration(X, y, groups, meta)
```

where `groups` is the chemical-system string, i.e. the sorted element set. Therefore the authored
demonstration must be constrained to quantities derivable from `X`, `y`, and `groups`:

- Cu/O/multi-alkaline-earth membership from `groups`;
- element count from `groups`;
- feature-space density from `X`;
- target error from model predictions against `y`.

Do not require the AI-authored demonstration to compute stoichiometric hole-count proxies or
plane-doping fractions, because raw formula stoichiometry is not currently passed into `meta`.
The mechanism can motivate the claim, but the runnable demonstration should test the
groups-derivable matched-control diagnostic.

## What the runnable candidate can do

If Step 1 and Step 2 are implemented, the resulting K2 campaign can:

- run on a real scientific dataset rather than a toy table;
- ask whether a composition-only materials model has a chemistry-specific Tc failure mode;
- have the AI author the discriminating demonstration code;
- execute that code on a held-out confirm split;
- apply a pre-registered decision rule and negative control;
- reproduce the computation;
- submit the authored demonstration to independent cross-vendor audit;
- update a calibrated belief only from the final harness-owned verdict;
- use the typed outcome reason to shape the next campaign round.

This is a meaningful end-to-end computational research loop. The important property is not that the
effect is guaranteed to hold in every live run, but that the system's verdict is not owned by the
LLM that proposed or coded the test.

## What result would count as K2 FULL

A live run should only be called K2 FULL if the acceptance criteria remain intact:

- at least two campaign rounds, or an explicit multi-round learning trace;
- at least one final harness confirm-split verdict;
- at least one matching `belief_update`;
- calibration surfaced in campaign synthesis;
- final verdict count matches belief update count;
- verdicts are harness-owned, not LLM-owned;
- credences are persisted;
- weak-prior honesty remains intact;
- cross-vendor review floor is met.

A single held cuprate demonstration is valuable but is not by itself K2 FULL. It is a strong partial
unless the campaign also shows the learning and belief-calibration spine above.

## Honest caveats

The planned campaign is de-risked but not deterministic.

- Direction novelty is likely improved, not guaranteed. The real direction gate may still reject
  the framing if it sees the claim as repackaged applicability-domain analysis.
- The reference probes show the effect exists, but the live system must author its own runnable,
  leakage-free, control-silent demonstration.
- The authored demonstration should stay within the current information surface: `X`, `y`,
  `groups`, and `meta`.
- Audit availability remains operationally important. A passing audit with fewer than the vendor
  floor should not be treated as independent verification.
- The 5h live window still matters. The run needs enough headroom for ideation, authoring,
  confirm compute, reproduction, audit, belief update, and campaign synthesis.

## Distance from a true AI scientist

If the campaign lands, it is a substantive milestone:

> Aletheia can complete an audited computational science loop on a real dataset: direction review,
> AI-authored demonstration, confirm-split verification, reproduction, audit, belief update, and
> cross-round learning.

It still should not be described as a complete AI scientist.

What remains missing:

- Autonomous discovery across a broad search space. This cuprate effect was pre-screened before
  being handed to the K2 live campaign.
- Deep domain world-modeling. The system is testing a composition-feature diagnostic, not modeling
  crystal structure, oxygen content, valence, or CuO2-plane carrier concentration.
- Strong literature mastery. Critic/RAG gates reduce novelty mistakes but are not equivalent to a
  full expert literature review.
- Rich experimental design. The current loop is computational and tabular; it does not plan
  synthesis, measurement, DFT, structure refinement, or new data acquisition.
- Robust autonomous coding. AI-authored demonstrations are still stochastic and need harness gates,
  retries, and independent audit.

Best description after a successful FULL run:

> an early guarded AI research agent that can execute and audit a real computational research
> program, update beliefs from verified outcomes, and adapt across rounds.

Not yet:

> a general autonomous scientist.

The next maturity gap is to move from "run a pre-screened verified candidate through a guarded
campaign" to "autonomously discover, justify, test, and refine candidates across multiple evidence
sources and experimental modalities."
