# ADR 0019: Pre-observation prediction commitment and observation seal

- Status: Accepted
- Date: 2026-08-15
- Scope: F9-S4 / Competitive Causal World Model

## Context

F9-S3 makes the causal estimand, graph, measurement process, assumptions, identification status, and
future claim ceiling explicit. It still does not prevent a system from seeing a result and then
choosing bins, changing a likelihood, sharpening a probability, or presenting an exploratory
pattern as a prediction.

Registered Reports move protocol review before outcomes are known and retain publication eligibility
independently of whether results are positive. That timing is the relevant integrity property here:
[Center for Open Science, Registered Reports](https://www.cos.io/initiatives/registered-reports).
It motivates a storage boundary, not merely an instruction in a prompt.

Probabilistic predictions also need evidence that their numbers behave as probabilities. The Brier
score compares a complete probability vector with the realized one-hot outcome:
[Brier, 1950](https://journals.ametsoc.org/view/journals/mwre/78/1/1520-0493_1950_078_0001_vofeit_2_0_co_2.xml).
Strictly proper scoring rules make truthful probability assessment optimal in expectation:
[Gneiting and Raftery, 2007](https://sites.stat.washington.edu/people/raftery/Research/PDF/Gneiting2007jasa.pdf).
Forecast quality is not concentration alone; sharpness should be considered subject to calibration:
[Gneiting, Balabdaoui, and Raftery, 2007](https://sites.stat.washington.edu/raftery/Research/PDF/Gneiting2007jrssb.pdf).

These ideas leave several implementation threats:

- outcome thresholds or analysis rules selected after the target result exists;
- a point prediction mislabeled as a probability distribution;
- a calibrated predictor manifest followed by an unrelated per-hypothesis likelihood;
- target observations reused as calibration data;
- identical or nearly identical likelihoods that cannot discriminate hypotheses;
- extreme zero/one mass that manufactures information gain;
- measurement uncertainty that makes a likelihood unstable;
- a content-addressed receipt that can be bypassed by writing observations elsewhere;
- an exact experiment namespace silently rebound to a changed commitment after its first result.

## Decision

### Bind prediction planning to one authorized causal campaign

`PredictionCommitmentRequest` exact-binds the F9-S3 campaign, F9-S2 campaign, F9-S1 snapshot,
causal contract, policy, author/evaluator manifests, outcome schema, experiment protocol, mode,
calibration report, and issue time. The source must be `ready_identified` or `ready_bounded` with
`prediction_planning_authorized=true`. Rejected assumptions, structural failures, and execution
failures cannot issue a prediction request.

The request and prediction-author manifest both declare `observation_access="none"`. Runtime
rebinding fails before the author adapter runs.

### Freeze the experiment and outcome space before prediction

`ExperimentProtocol` includes the exact causal campaign/contract/estimand, evidence kind and inherited
claim ceiling, intervention, target population, outcome measurement process, observable/protocol,
outcome schema, analysis plan, exclusion rule, stopping rule, parser, and freeze time. Its experiment
namespace is mechanically derived from the substantive protocol fields.

`OutcomeSchema` supports:

- categorical bins; or
- continuous outcomes converted to preregistered contiguous bins with both open tails, explicit
  units, and exactly one owner for every internal boundary.

The bin set must equal every F9-S2 discriminating prediction's outcome space. Measurement protocol
and error-model identities must equal the F9-S3 outcome measurement process.

### Distinguish calibrated probabilities from ordinal predictions

Every active hypothesis supplies exactly one exact-bound `HypothesisPrediction`.

- `probabilistic` mode requires a normalized probability for every bin, a unique modal bin equal to
  the F9-S2 expected outcome, a frozen likelihood identity, and measurement-error sensitivity cases;
- `ordinal` mode requires a complete ranking whose first bin equals the F9-S2 expected outcome, but
  contains no probabilities or likelihood hash.

Ordinal campaigns may be `ready` for qualitative discrimination, but are always `eig_eligible=false`.
They cannot be described as calibrated probabilities.

The prediction-author manifest freezes a likelihood-family hash. Each per-hypothesis likelihood hash
is mechanically derived from that family, the exact hypothesis version, experiment protocol, and
outcome schema. A calibrated family therefore cannot be replaced with an unrelated likelihood after
the report is issued.

### Require independent historical calibration for probability mode

An independent calibration evaluator has a separate principal and, for model runtimes, a distinct
model identity from the prediction author. It must also differ from earlier hypothesis/causal
proposal and review roles. Neither evaluator nor author receives ambient tools; the evaluator is
limited to a named historical validation split.

Every `CalibrationTrial` stores the complete probability vector, realized bin, validation namespace,
and prediction/observation times. Prediction must precede observation and cannot predate the frozen
predictor. The report must precede the target request, use a namespace different from the target
experiment, and exact-bind predictor manifest, evaluator manifest, outcome schema, and measurement
protocol.

The harness recomputes, rather than trusts:

- multiclass Brier score;
- mean logarithmic loss with a frozen numerical epsilon;
- top-label expected calibration error with a frozen bin count;
- count of realized outcomes assigned zero probability.

Policy thresholds determine admission. These metrics are bounded engineering gates over historical
fixtures; passing them is not proof of calibration in a new scientific domain.

### Derive degeneracy, discrimination, and sensitivity probes

For every probabilistic prediction, the harness derives entropy, minimum/maximum probability,
sensitivity-case count, and maximum total-variation movement. It derives pairwise total-variation
distance between every hypothesis. For ordinal mode it checks every pair has a different complete
ranking.

Calibration failure yields `blocked_calibration`. Extreme/low-entropy mass, insufficient or unstable
sensitivity cases, and weak pairwise separation yield `blocked_degeneracy`. Adapter exceptions,
malformed output, rebindings, wrong order/coverage, changed likelihood family, or future timestamps
yield hash-only `blocked_execution`. Only a probabilistic `ready` campaign is EIG-eligible.

### Make the commitment immutable and retries scientifically equivalent

The complete campaign is archived as canonical content-addressed JSON. Load rehashes bytes and
revalidates all nested contracts, metrics, probes, disposition, and EIG eligibility.

`commitment_sha256` identifies substantive content: frozen upstream campaign, policy/manifests,
experiment, schema, calibration report, predictions, derived diagnostics, disposition, and EIG
eligibility. Operational campaign/request labels and generation timestamps do not change this hash.
An exact retry can therefore reuse the sealed experiment; any scientific change cannot.

### Put observation staging behind the archived receipt

`ObservationStagingStore` accepts bytes only after it:

1. reads and rehashes the prediction campaign from its immutable archive;
2. verifies the wrapper and commit time;
3. verifies `ready` disposition;
4. proves `observed_at > committed_at`;
5. atomically creates or verifies the experiment-namespace seal;
6. writes the raw observation once under its content hash and re-reads it.

The first accepted observation seals the experiment namespace to the substantive commitment and
records the first campaign, commitment receipt, observation hash, and seal time. A later exact retry
is accepted. A different commitment for the same namespace is rejected before raw bytes are written,
and a persistent `security_and_scientific_integrity` violation is created.

Missing/corrupt archives, blocked campaigns, pre-commit observations, unsafe paths, corrupt seals,
or corrupt raw bytes fail closed. This store is the F9-S4 physical boundary; callers that write target
results outside it have not produced an admissible Aletheia observation.

## Consequences

- A claim that a hypothesis predicted a result now has a verifiable time and exact scientific
  identity.
- Continuous thresholds, stopping/exclusion rules, parser, and analysis identity cannot be tuned to
  the target result without creating a different experiment namespace or a recorded violation.
- Calibrated probabilities and qualitative orderings have distinct machine-readable semantics.
- F9-S5 can consume only `eig_eligible=true` campaigns for probability-based EIG.
- `ready_bounded` causal plans can continue, but their association ceiling is inherited unchanged.
- Calibration and sensitivity requirements increase fixture and production-validation cost.
- Filesystem integrity and use of the staging API remain part of the trusted computing boundary.
- Passing F9-S4 says nothing about whether an intervention ran correctly, an observation is valid,
  a hypothesis is true, or a causal effect exists.

## Rejected alternatives

- **Put “predict before observing” in the prompt:** prompt text cannot prove temporal order or prevent
  another code path from writing observations.
- **Store only the modal outcome:** this discards uncertainty and cannot support likelihood-based
  belief updating or EIG.
- **Treat ordinal scores as probabilities:** arbitrary scores are not normalized or calibrated and
  create fictional information gain.
- **Accept any normalized distribution:** normalization alone permits identical hypotheses,
  manufactured certainty, and unstable measurement-sensitive likelihoods.
- **Calibrate on the target namespace:** this leaks the result that the commitment is meant to
  precede.
- **Trust reported calibration metrics:** a forged scalar can hide trial omissions, zero-probability
  events, or metric substitution.
- **Bind only the predictor manifest:** an author could swap the per-hypothesis likelihood after
  calibration; the derived likelihood-family identity closes that gap.
- **Use campaign ID as scientific identity:** harmless operational retries would look like mutations.
- **Silently reject changed post-observation predictions:** rejection without a durable security and
  scientific-integrity artifact would hide attempted outcome-driven revision.
