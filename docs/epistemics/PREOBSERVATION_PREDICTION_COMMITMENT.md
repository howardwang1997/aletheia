# F9-S4 pre-observation prediction and likelihood commitment

## What is available

F9-S4 transforms an authorized F9-S3 causal audit into an immutable prediction receipt and makes
that receipt a physical prerequisite for observation staging.

```text
ready_identified / ready_bounded F9-S3 campaign
  -> frozen experiment protocol + outcome schema
  -> historical calibration report (probability mode only)
  -> exact no-observation prediction request
  -> one prediction/likelihood per active hypothesis
  -> deterministic calibration + degeneracy + sensitivity probe
  -> content-addressed substantive commitment
  -> experiment namespace seal
  -> private content-addressed observation staging
```

The implementation is in `aletheia.epistemics.prediction` and is exported by
`aletheia.epistemics`. It does not parse or validate scientific observations, update beliefs,
select experiments, or upgrade claims. Those remain later gates.

## Exact upstream boundary

`PredictionCommitmentRequest` binds:

- the complete F9-S3 campaign and its F9-S2/F9-S1 source identities;
- the exact causal contract;
- prediction mode;
- complete experiment protocol and outcome schema;
- prediction-author, calibration-evaluator, and policy manifests;
- the complete historical calibration report in probability mode;
- issue time and `observation_access="none"`.

The F9-S3 source must have `prediction_planning_authorized=true`. Both `ready_identified` and
`ready_bounded` may plan predictions. A bounded source retains its association-only claim ceiling;
F9-S4 never upgrades it. `blocked_structure`, `blocked_assumptions`, and `blocked_execution` sources
cannot issue a request.

## Frozen experiment protocol

`ExperimentProtocol` contains the fields that could otherwise be selected after seeing a result:

| Boundary | Frozen identity |
|---|---|
| causal question | F9-S3 campaign, contract, estimand, evidence kind, claim ceiling |
| intervention | intervention protocol and target population |
| measurement | outcome process, observable, measurement protocol, outcome schema |
| analysis | analysis plan, exclusion rule, stopping rule, observation parser |
| namespace | deterministic hash of the substantive protocol fields |

Changing any substantive field derives another experiment namespace. Reusing the original
namespace with a changed prediction commitment after observation is a recorded violation.

## Outcome schemas

`OutcomeSchema` exact-binds the F9-S3 measurement protocol and error model. Its bin IDs must match
the F9-S2 prediction outcome space for every active hypothesis.

Categorical mode uses ordered labels without numeric boundaries. `continuous_binned` mode requires:

- explicit units;
- at least two preregistered bins;
- an open lower tail and open upper tail;
- contiguous internal numeric boundaries;
- exactly one adjacent bin owning each boundary;
- no NaN or infinite explicit bound.

The actual raw measurement remains continuous if the later parser retains it. The frozen bins define
the likelihood outcome space and cannot be chosen from the target sample.

## Prediction modes

### Probabilistic

Every hypothesis must provide:

- exact hypothesis version, causal graph, and F9-S2 prediction hashes;
- exact observable, measurement protocol, and measurement-error model;
- probability mass over every bin, in canonical order;
- a unique modal bin equal to the F9-S2 expected outcome;
- a likelihood hash derived from the calibrated likelihood family;
- policy-required measurement-error sensitivity scenarios;
- rationale hash.

For bins \(y_1,\ldots,y_K\), the harness requires nonnegative finite mass and
\(\sum_k p(y_k\mid H,e)=1\) within the frozen numerical tolerance.

### Ordinal

Every hypothesis supplies a complete bin ranking whose first bin equals its F9-S2 expected outcome.
It supplies no probability mass, likelihood hash, calibration report, or sensitivity distribution.
An ordinal campaign can be ready for qualitative experiment planning, but it is always
`eig_eligible=false`.

## Likelihood-family binding

`PredictionAuthorManifest` freezes `likelihood_family_sha256`. A probabilistic prediction's
`likelihood_model_sha256` is derived from:

```text
likelihood family
+ hypothesis ID and exact version
+ experiment protocol hash
+ outcome schema hash
```

Use `derive_hypothesis_likelihood_model_sha256`; a caller-supplied unrelated hash is rejected. The
historical calibration report binds the complete author manifest, so it transitively binds the
implementation family used to derive every hypothesis likelihood.

## Historical calibration

The calibration evaluator must use a principal distinct from the prediction author and earlier
hypothesis/causal proposal/review roles. Model-backed author/evaluator roles also need distinct model
identities. The author receives no observation; the evaluator may access only the named historical
validation split.

Each `CalibrationTrial` contains a complete probability vector, observed bin, validation namespace,
and `predicted_at < observed_at`. Trials cannot predate the frozen predictor. The report must:

- precede the target request;
- use a namespace different from the target experiment;
- bind author/evaluator manifests, outcome schema, and measurement protocol;
- cover one shared complete outcome space;
- retain every trial in canonical order.

The report's metrics are not trusted fields. Model validation recomputes:

```text
multiclass Brier = mean_i sum_k (p_ik - 1[y_i = k])^2
mean log loss    = mean_i -log(max(p_i,y_i, epsilon))
top-label ECE    = weighted absolute confidence/accuracy gap over frozen bins
zero events      = count_i 1[p_i,y_i = 0]
```

Use `derive_likelihood_calibration_metrics` to construct the expected metric object. A mismatched
metric makes the report invalid rather than merely low quality.

## Admission probe

The deterministic harness derives one `HypothesisPredictionDiagnostic` per hypothesis:

- entropy in nats;
- minimum and maximum bin probability;
- sensitivity-case count;
- maximum total-variation movement from the base prediction.

It also covers every hypothesis pair. Probability mode records total-variation distance; ordinal
mode records whether full rankings differ.

Policy failures map to:

| Disposition | Meaning |
|---|---|
| `ready` | all binding, calibration, separation, entropy, and sensitivity checks pass |
| `blocked_calibration` | historical sample/score/ECE/zero-event threshold fails |
| `blocked_degeneracy` | extreme, low-entropy, weakly separating, missing, or unstable predictions |
| `blocked_execution` | adapter error, invalid output, rebinding, incomplete coverage, changed family, or future time |

Only `ready + probabilistic` sets `eig_eligible=true`. These thresholds gate an engineering artifact;
they do not establish real-domain calibration.

## Running and committing

```python
from aletheia.epistemics import (
    build_prediction_commitment_request,
    commit_prediction_commitment_campaign,
    run_prediction_commitment,
)

request = build_prediction_commitment_request(
    request_id="prediction-001",
    source_causal_campaign=causal_campaign,
    prediction_mode=prediction_mode,
    experiment_protocol=protocol,
    outcome_schema=outcome_schema,
    policy=policy,
    author_manifest=author.manifest,
    calibration_evaluator_manifest=evaluator_manifest,
    calibration_report=calibration_report,
    issued_at=issued_at,
)

campaign = await run_prediction_commitment(
    campaign_id="prediction-001",
    source_causal_campaign=causal_campaign,
    policy=policy,
    request=request,
    author=author,
    calibration_evaluator_manifest=evaluator_manifest,
)

committed = commit_prediction_commitment_campaign(
    archive=archive,
    campaign=campaign,
    committed_at=committed_at,
)
```

`load_prediction_commitment_campaign` rehashes the archived bytes, requires canonical JSON, validates
the complete F8/F9 chain, recomputes calibration metrics and admission probes, and rejects decision
forgery.

`campaign_sha256` identifies one operational execution. `commitment_sha256` excludes operational
campaign/request labels and timestamps, but includes all substantive scientific content. Exact
retries therefore share a commitment hash.

## Observation staging boundary

Create a private observation store with the archive holding the committed campaign:

```python
from aletheia.epistemics import ObservationStagingStore

store = ObservationStagingStore(
    observation_root,
    prediction_archive=archive,
)

receipt = store.stage_observation(
    committed_campaign=committed,
    payload=raw_bytes,
    media_type="application/octet-stream",
    observed_at=observed_at,
    staged_at=staged_at,
)

raw_bytes = store.read_observation(receipt)
```

Before writing `payload`, the store loads the archived campaign through its immutable ledger and
requires:

- exact wrapper/archive equality;
- `ready` disposition;
- timezone-aware times and `observed_at > committed_at`;
- an absent namespace seal or a seal with the same substantive commitment.

The first write atomically seals the namespace and then writes the raw bytes at
`observations/<sha-prefix>/<sha>.observation`. Existing raw bytes are rehashed. Seals and raw receipts
are immutable typed objects.

If a different commitment targets a sealed namespace, staging writes no raw observation. It creates
a content-addressed `PredictionMutationViolation` with severity
`security_and_scientific_integrity` and raises `PostObservationPredictionMutation`. An exact retry
with the same substantive hash is allowed.

## What this slice guarantees

- every active hypothesis predicts the same frozen outcome space before target observation;
- probabilities are normalized, calibrated against a separate historical split under policy, and
  nondegenerate enough for later EIG;
- ordinal predictions cannot masquerade as probabilities;
- measurement-error sensitivity and pairwise discrimination are explicit;
- experiment protocol and likelihood identities are immutable and content-addressed;
- observation staging requires a readable earlier commitment;
- post-observation changes on the same namespace are rejected and durably recorded;
- corrupt/missing campaign, seal, or observation bytes fail closed.

## Current limitations

- All included likelihoods, historical trials, and observations are synthetic fixtures.
- There is no production predictor/calibration evaluator or independently curated calibration set.
- Top-label ECE is a coarse finite-sample diagnostic, not proof of calibration; thresholds need
  domain-specific preregistration and uncertainty analysis.
- This slice supports categorical likelihoods over discrete or preregistered continuous bins, not
  arbitrary density objects, censoring, survival likelihoods, or hierarchical dependence.
- Filesystem permissions and exclusive-create semantics are trusted; there is no remote transparency
  log, hardware attestation, signature authority, or authenticated operator identity yet.
- Observation staging preserves raw bytes but does not parse, validate, classify exploration versus
  confirmation, or authorize posterior updates.
- There is no EIG experiment selector, posterior update, hypothesis revision, negative-result policy,
  K3 scorer, or scheduler wiring yet.
- `ready` is not evidence that an experiment ran correctly or that any hypothesis or causal effect is
  true.

The next slice, F9-S5, may use only `eig_eligible=true` probability commitments for EIG and must
select experiments without target-observation access while accounting for cost, risk, measurement
validity, confirmation freshness, and replication debt.
