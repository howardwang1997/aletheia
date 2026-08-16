# F9-S4 pre-observation prediction commitment implementation report

- Date: 2026-08-15
- Scope: frozen experiment/outcome protocol, calibrated likelihood commitment, deterministic
  degeneracy and sensitivity probes, content-addressed receipts, and observation namespace sealing
- Engineering status: complete
- Scientific-exit status: not complete

## Outcome

F9-S4 now converts an authorized F9-S3 campaign into an immutable prediction artifact before target
observation access. The artifact freezes the intervention, estimand, population, measurement,
outcome bins, analysis/exclusion/stopping/parser identities, and one exact prediction for every
active hypothesis.

Probability mode requires a separate historical calibration report, complete normalized bin mass,
a likelihood mechanically derived from a calibrated frozen family, pairwise discrimination,
non-extreme entropy, and measurement-error sensitivity cases. Ordinal mode remains available when
probabilities cannot be defended, but is explicitly ineligible for EIG.

The new observation store makes temporal ordering enforceable. It first reloads the committed
campaign from its content-addressed archive, proves the campaign is ready and earlier than the
observation, seals the experiment namespace, and only then writes raw bytes. A changed substantive
commitment on that namespace is rejected before the raw write and creates a durable security and
scientific-integrity violation. Operationally exact retries remain valid.

This is an engineering result. The causal source, likelihoods, calibration trials, sensitivity
scenarios, and staged observations in tests are synthetic. No real predictor is demonstrated as
calibrated, no experiment has been validated, and no hypothesis or causal claim has gained evidence.

## Research basis

- [Center for Open Science, Registered Reports](https://www.cos.io/initiatives/registered-reports)
  motivates freezing methods before outcomes are known and separating protocol quality from result
  direction.
- [Brier 1950](https://journals.ametsoc.org/view/journals/mwre/78/1/1520-0493_1950_078_0001_vofeit_2_0_co_2.xml)
  provides the complete-distribution quadratic verification score mechanically recomputed here.
- [Gneiting and Raftery 2007](https://sites.stat.washington.edu/people/raftery/Research/PDF/Gneiting2007jasa.pdf)
  provides the proper-scoring basis for evaluating probabilistic forecasts without rewarding
  strategic probability distortion.
- [Gneiting, Balabdaoui, and Raftery 2007](https://sites.stat.washington.edu/raftery/Research/PDF/Gneiting2007jrssb.pdf)
  motivates evaluating concentration subject to calibration rather than rewarding sharp but
  unreliable forecasts.

ADR 0019 narrows those ideas to the implemented Aletheia trust boundary.

## Delivered contracts

`aletheia/epistemics/prediction.py` adds immutable extra-forbid contracts for:

- categorical and continuous-binned outcome schemas;
- complete frozen experiment protocols and deterministic namespaces;
- full bin-probability vectors and ordinal rankings;
- historical calibration trials, reports, and recomputed metrics;
- prediction-author and independent calibration-evaluator manifests;
- likelihood-family and exact per-hypothesis likelihood identities;
- sensitivity predictions and per-hypothesis diagnostics;
- complete pairwise discrimination ledgers;
- policy, request, author batch, failure, probe, disposition, campaign, and archive receipt;
- experiment namespace seals, raw observation receipts, and post-observation mutation violations.

The author and calibration output schemas have public canonical SHA-256 identities. Every model is
frozen, rejects unknown fields, and uses bounded collection sizes.

## Exact causal and measurement boundary

The request revalidates the full F9-S3/F9-S2/F9-S1 chain. It requires one exact F9-S2 source
prediction for every active hypothesis on the F9-S3 outcome indicator and protocol. The outcome
schema's measurement error model must match the causal measurement process.

The experiment protocol inherits rather than recalculates F9-S3's evidence kind and claim ceiling.
A `ready_bounded` causal source can create a prediction campaign, but remains association-only. A
rejected required assumption blocks request construction entirely.

The prediction author sees the frozen request and causal campaign only. Its manifest prohibits tools
and target observations. The calibration evaluator is independent from the author plus earlier
hypothesis/causal proposal and review roles; model-backed roles must use different model identities.

## Outcome and likelihood commitment

Categorical bins have explicit canonical IDs and order. Continuous bins additionally require units,
open outer tails, contiguous inner boundaries, and exactly one owner for each boundary. Bin IDs must
equal the F9-S2 outcome space; post-hoc threshold insertion or deletion is invalid.

Every prediction binds:

- exact hypothesis version and graph;
- exact source F9-S2 prediction;
- observable, measurement protocol, and error model;
- expected bin;
- complete probability mass or complete ordinal ranking;
- rationale;
- in probability mode, derived likelihood identity and sensitivity scenarios.

`likelihood_model_sha256` is not an opaque author choice. It is derived from the manifest's frozen
likelihood family, exact hypothesis version, protocol, and outcome schema. Substituting another hash
produces hash-only blocked execution.

## Calibration and admission derivation

Historical calibration trials retain complete mass, realized bins, validation namespace, and
prediction/observation times. They must use one separate frozen split, must predate the target
request, and each prediction must precede its historical observation. The harness rederives Brier,
log loss, top-label ECE, and observed zero-probability count. Forged metrics make the report invalid.

The policy currently gates minimum trial count, maximum Brier/log loss/ECE, and zero-probability
events. The prediction probe additionally derives:

- per-hypothesis entropy and probability extrema;
- sensitivity coverage and maximum total-variation movement;
- full pairwise total-variation distance in probability mode;
- full pairwise ranking difference in ordinal mode.

Disposition and EIG eligibility are mechanical:

- `ready`: all checks pass;
- `blocked_calibration`: historical report misses policy;
- `blocked_degeneracy`: extreme, low-entropy, weakly discriminating, or unstable likelihood;
- `blocked_execution`: adapter/output/binding/time/coverage failure;
- `eig_eligible=true`: only `ready` probabilistic campaigns.

## Immutable identity and observation isolation

Canonical campaign JSON is stored by the existing write-once archive. Reads rehash bytes and rerun
all model validators and derivations. `campaign_sha256` identifies an execution;
`commitment_sha256` identifies substantive scientific content and deliberately excludes operational
retry labels and timestamps.

`ObservationStagingStore` uses separate private write-once namespaces for:

- experiment seals;
- raw content-addressed observations;
- content-addressed mutation violations.

Staging fails before raw write if the prediction ledger is missing/corrupt, wrapper differs, campaign
is not ready, observation is not later than commitment, or the namespace is bound to another
commitment. Seal and raw reads use no-follow file opens, type/size checks, canonical typed metadata,
and SHA-256 revalidation.

The first observation seals the experiment namespace. An exact operational retry with the same
substantive commitment can stage another observation. Changed probabilities, calibration, protocol,
likelihood, diagnostics, or upstream scientific identity change the commitment and cause
`PostObservationPredictionMutation`; the violation records both identities and severity
`security_and_scientific_integrity`.

## Test evidence so far

Focused F9-S4 acceptance:

```text
30 passed in 7.62 s
changed Python Ruff and compilation: passed
```

All F9 epistemics tests through F9-S4:

```text
115 passed in 15.01 s
```

Coverage includes:

- synthetic F8-S1–S5 → F9-S2 → F9-S3 → F9-S4 exact integration;
- ready probabilistic and ready-but-non-EIG ordinal modes;
- continuous open-tail, contiguity, and boundary-ownership rules;
- normalization, unique modal outcome, complete hypothesis/bin coverage, and order;
- Brier/log-loss/ECE/zero-event recomputation and metric-forgery rejection;
- historical/target split separation and prediction-before-observation timing;
- independent principal/model roles and no-tool/no-target-observation manifests;
- likelihood-family derivation and substitution rejection;
- calibration sample/score/zero-event blockers;
- probability floor/ceiling, entropy, pairwise TV, sensitivity coverage/stability blockers;
- exact F9-S2 prediction, F9-S3 measurement/error-model, and hypothesis/graph binding;
- inherited association ceiling for ready-bounded causal campaigns;
- rejected causal-assumption source blocking;
- hash-only exception/invalid-output handling and future-output rejection;
- campaign decision-forgery rejection, archive round trip, and tamper detection;
- missing/blocked/precommit observation attempts with no raw write;
- staged observation round trip and corrupt raw/seal detection;
- exact retry acceptance;
- changed post-observation commitment rejection, no raw write, and persistent violation.

Repository-wide acceptance:

```text
non-Docker: 1032 passed, 1 skipped, 29 deselected in 316.57 s
Docker:       29 passed, 1033 deselected in 37.58 s
```

Both final runs passed on their first execution. All changed F9-S4 Python files and public exports
also pass Ruff and compilation. Repository-wide Ruff still has the same previously documented
out-of-scope exploratory/legacy failures; no new F9-S4 file is implicated.

## Files added or materially changed

- `aletheia/epistemics/prediction.py`;
- `aletheia/epistemics/__init__.py`;
- `tests/epistemics/f9s4_fixtures.py`;
- `tests/epistemics/test_prediction_commitment.py`;
- `docs/adr/0019-f9-pre-observation-prediction-commitment-and-observation-seal.md`;
- `docs/epistemics/PREOBSERVATION_PREDICTION_COMMITMENT.md`;
- this report, README, docs index, F9-S3 guide, and F7–F12 master-plan status.

## Explicit non-guarantees

- no real-domain likelihood author or independently curated calibration suite;
- no proof that finite-sample ECE or current synthetic thresholds transfer to production;
- no arbitrary continuous density, survival/censoring, hierarchical, or dependent-observation
  likelihood contract;
- no remotely witnessed transparency log, hardware attestation, signing authority, or operator
  authentication for observation seals;
- no intervention execution, randomization, parser, observation validity, or measurement validity
  result;
- no posterior update, likelihood sensitivity, revision, or contradiction handling inside F9-S4;
  those are now provided by the downstream isolated F9-S6 boundary;
- no EIG selector inside F9-S4, K3 acceptance scorer, scheduler wiring, F9 scientific exit, or
  causal discovery; the subsequent isolated F9-S5 selector does not change these F9-S4 guarantees;
- no real evidence for any hypothesis, mechanism, effect, novelty, or SOTA claim.

## Subsequent slice

F9-S5 now consumes immutable ready campaigns, uses probability-based EIG only when
`eig_eligible=true`, and performs observation-blind constrained selection across discrimination,
cost, time, risk, measurement validity, proxy risk, capability, fresh confirmation, and replication
debt. Its high-EIG invalid-proxy and complete-reason acceptance fixtures pass. See
`F9_S5_CONSTRAINED_EXPERIMENT_SELECTION_IMPLEMENTATION_REPORT_2026_08_15.md`. F9-S6 validated update
and F9-S7 independent K3 acceptance are now also implemented. See
`F9_S7_INDEPENDENT_K3_ACCEPTANCE_IMPLEMENTATION_REPORT_2026_08_15.md`; the next unfinished work is the
F9 scientific-exit/integration bridge.
