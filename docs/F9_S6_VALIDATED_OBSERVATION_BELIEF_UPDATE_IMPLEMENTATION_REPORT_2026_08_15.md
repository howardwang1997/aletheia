# F9-S6 validated-observation belief update implementation report

- Date: 2026-08-15
- Status: Engineering slice complete; scientific exit not claimed
- Scope: Independent observation validation, exact Bayesian update, likelihood sensitivity,
  append-only revision directives, and contradiction queue

## Outcome

F9-S6 now provides the first isolated path from one committed F9-S5 experiment selection to a child
F9-S1 belief state. The path is intentionally split: an independent validator is the only component
that receives exact staged raw bytes; the updater accepts only a physically reloaded, committed
`validated_confirmation` artifact.

For an admitted observation, the harness reuses the exact F9-S1 prior and F9-S4 pre-observation
likelihood, computes the complete nominal and sensitivity posteriors, creates a new belief version,
and emits immutable hypothesis/world revision directives plus open contradictions. Invalid or
incomplete evidence produces no partial posterior.

This proves deterministic machinery over synthetic fixtures. It does not prove the scientific
truth of any hypothesis, the correctness of a real likelihood, the validity of a real measurement,
or the independence/authenticity of a production validator.

## Related-work decisions

The design was informed by:

- Bayesian workflow and its separation of inference, checking, comparison, and sensitivity:
  [Gelman et al., 2020](https://arxiv.org/abs/2011.01808);
- explicit prior-data conflict/surprise diagnostics:
  [Evans and Moshonov, 2006](https://doi.org/10.1214/06-BA129);
- robust Bayesian sensitivity rather than reporting one posterior as assumption-free:
  [Berger and Mortera, 1994](https://www.sciencedirect.com/science/article/pii/0378375894901317) and
  [Lopes and Tobias, 2011](https://doi.org/10.1146/annurev-economics-111809-125134);
- before-outcome protocol review as represented by Registered Reports:
  [Nature](https://www.nature.com/nature-portfolio/editorial-policies/registered-reports);
- explicit deviation classification, with the cited FDA page treated as draft/nonbinding process
  guidance rather than a regulatory guarantee:
  [FDA](https://www.fda.gov/regulatory-information/search-fda-guidance-documents/protocol-deviations-clinical-investigations-drugs-biological-products-and-devices); and
- contradiction retention and assumption-aware revision from truth-maintenance work:
  [de Kleer, 1986](https://www.dekleer.org/Publications/An%20Assumption-Based%20TMS.pdf).

The resulting contract is narrower than a general Bayesian workflow: it supports exact discrete
updates against finite pre-frozen likelihood scenarios and emits revision work. It does not perform
posterior predictive simulation, continuous robust Bayes, causal rediscovery, or contradiction
resolution.

## Implemented boundary

```text
committed selection + exact staged payload
  -> selection archive verification
  -> selected prediction archive verification
  -> raw-observation store verification
  -> independent validator with no tools and exact-payload-only access
  -> hard scientific admission probe
  -> committed validation receipt
  -> validation archive verification
  -> updater with validated-artifact-only access
  -> posterior/surprise/entropy/likelihood-sensitivity audit
  -> child belief state + new snapshot
  -> hypothesis/world revision directives + open contradictions
  -> committed update receipt
```

## Observation-validation transaction

### Frozen independence and chronology

`ObservationValidatorManifest` freezes runtime, adapter, parser, output schema, principal, optional
model/instruction, transport, and zero-tool authority. Its principal and model identity are checked
against the F9-S5 assessor and every F9-S2/F9-S3/F9-S4 scientific role. Both validator and validation
policy must be frozen before selection commitment.

The request requires the F9-S5 selection to be committed before observation, the raw payload to be
staged before validation, and the F9-S4 receipt to match experiment namespace, protocol, prediction
campaign, substantive commitment, and prediction receipt exactly.

### Physical reloading before validation

`run_observation_validation` reads the selection campaign, selected prediction campaign, and raw
observation through their real content-addressed stores. Embedded objects must equal the reloaded
objects, and the raw content digest must match the staging receipt. Any archive/store failure yields
one of:

- `selection_archive_invalid`;
- `prediction_archive_invalid`; or
- `observation_store_invalid`.

Failures retain class/detail hashes and contain no partial physical-verification or validation
artifacts.

### Exact output binding and hard gates

The validator receives only `request` and `raw_observation`. Its output exact-binds all selection,
prediction, observation, reservation, parser, analysis, measurement/error-model, validator, and
execution identities. The observed outcome must belong to the frozen schema, and the batch/partition
must be the selected fresh-confirmation reservation available at observation time.

Belief-update admission requires confirmation role, verified experiment identity and custody, valid
measurement, intact blinding, exact or frozen-tolerance protocol adherence, resolved-accept audit,
and the minimum confirmatory sample. A sub-minimum sample is admitted only through the exact small-
sample rule frozen before selection. Exploration, calibration, material/unknown deviations, identity
or custody uncertainty, invalid measurement, blinding compromise, audit rejection/unresolved state,
reservation rebinding, and unregistered small-sample exceptions never receive update authority.

Malformed/future validator output and adapter exceptions are sanitized. The campaign stores hashes,
not raw untrusted payload or exception text.

## Belief-update transaction

### Validated-artifact-only input

`build_world_belief_update_request` refuses anything other than a committed
`validated_confirmation`. It exact-binds question, source snapshot and belief state, selection,
prediction commitment, validated outcome, policy, and validation archive custody. The updater has no
raw payload or observation-store argument.

`run_world_belief_update` reloads the validation campaign before deriving anything. Missing,
corrupt, noncanonical, or rebound validation bytes yield `blocked_execution` and no partial audit,
snapshot, revision, or contradiction queue.

### Exact nominal posterior

For every active hypothesis, the harness records prior, realized outcome likelihood, unnormalized
mass, posterior, and modal-prediction match. It also derives prior/posterior entropy, realized entropy
change, prior-predictive probability, surprisal, winner set, primary-negative status,
all-model-low-likelihood status, realized-outcome informativeness, and an exact likelihood-bundle
identity.

The implementation uses exact discrete Bayes normalization. Zero prior-predictive mass blocks the
update instead of inserting an implicit epsilon.

### Complete likelihood sensitivity

Each active hypothesis must have the same policy-minimum set of precommitted F9-S4 sensitivity
scenarios. The harness retains the complete posterior and prior-predictive probability for every
scenario, total variation from the nominal posterior, and whether its winner changes.

Misaligned/incomplete scenarios or zero scenario predictive mass yield `blocked_likelihood`. A
winner change or total variation above the frozen ceiling yields `updated_fragile` and an open
likelihood-sensitivity contradiction; otherwise the disposition is `updated_robust`.

### Append-only belief and revision artifacts

Every successful update creates a `BeliefState` child with:

- exact parent belief hash and `version + 1`;
- unchanged question and hypothesis versions;
- `validated_observation` update kind;
- validation commitment receipt and realized-likelihood bundle; and
- frozen harness author/time.

The child is embedded in a new `WorldModelSnapshot`; source objects remain unchanged.

Per-hypothesis directives are `retain`, `retire`, or `narrow`. A modal miss retires only if its
posterior stays at/below policy in both nominal and every sensitivity case. Other misses narrow. Both
retirement and narrowing require a new version and mark mutation forbidden.

The world directive forks the hypothesis set when every model assigns low likelihood, seeks a new
measurement or stops when realized likelihoods do not discriminate, and otherwise continues the
current set. A fork explicitly requires a new hypothesis lineage.

### Contradiction preservation

Canonical open records are emitted for hypothesis prediction misses, prior-predictive surprise,
likelihood sensitivity, all-model low likelihood, and uninformative realized outcomes. Each binds the
validation receipt, prediction commitment, affected hypotheses, evidence, reason, severity, and time.
F9-S6 does not auto-resolve them.

## Mechanical replay and storage

Validation campaign model checks recompute request bindings, physical receipts, scientific probe,
blockers, and disposition. Update campaign model checks recompute posterior math, sensitivity,
fragility, child snapshot, all directives, contradiction queue, blockers, and disposition.

Both campaign types support canonical content-addressed commit/load wrappers. Commit time is explicit
and cannot predate generation. Replay rehashes bytes, rejects noncanonical JSON, revalidates nested
lineage, and detects forged derived fields or tampered archive bytes.

## Acceptance evidence

Focused F9-S6 acceptance:

```text
31 passed in 76.56 s
```

The focused suite covers:

- exact source/child lineage, posterior normalization, entropy, and surprise;
- complete sensitivity posteriors and robust/fragile disposition;
- physical selection, prediction, raw-observation, and validation archive verification;
- updater raw-data isolation and independent validator role boundaries;
- confirmation role, identity, custody, measurement, blinding, protocol, audit, and sample gates;
- bounded pre-registered deviations and exact small-sample exceptions;
- reservation and outcome-schema rebinding rejection;
- positive and primary-negative results without history mutation;
- robust retirement versus conservative narrowing;
- uninformative observations forcing new measurement/stop;
- all-model miss forcing a new hypothesis lineage;
- incomplete sensitivity matrix blocking update;
- missing physical inputs without partial evidence;
- validator exception, malformed output, future output, and secret sanitization;
- validation/policy freeze and selection-before-observation chronology;
- append-only revision and open contradiction artifacts;
- derived-field forgery rejection; and
- canonical validation/update archive round trip and byte-tamper detection.

All F9 epistemics tests through F9-S6:

```text
176 passed in 149.44 s
```

Repository-wide acceptance:

```text
non-Docker: 1093 passed, 1 skipped, 29 deselected in 528.15 s
Docker:       29 passed, 1094 deselected in 26.10 s
```

The first Docker run had one environment-probe timeout after 28 tests passed. The exact failed test
then passed alone in 0.31 s, and the complete 29-test Docker matrix passed on immediate clean rerun.
No application-code change was made for the transient Docker client/runtime event. Ruff lint, Ruff
format check, compilation, focused F9-S5/F9-S6 regression, all epistemics tests, non-Docker full
suite, and the final real-Docker suite passed.

## Files added or materially changed

- `aletheia/epistemics/belief_update.py`;
- `aletheia/epistemics/__init__.py`;
- `tests/epistemics/f9s6_fixtures.py`;
- `tests/epistemics/test_belief_update.py`;
- `tests/epistemics/f9s5_fixtures.py` for additional controlled likelihood variants;
- `docs/adr/0021-f9-validated-observation-bayesian-revision.md`;
- `docs/epistemics/VALIDATED_OBSERVATION_BELIEF_UPDATE.md`; and
- this report, README, docs index, F9-S5 guide/report, and F7–F12 master-plan status.

## Explicit non-guarantees

- no real-domain hypothesis, intervention, observation, likelihood calibration, posterior, or
  scientific conclusion;
- no authenticated instrument/personnel identity, chain of custody, measurement validity,
  blinding, protocol audit, or electronic signature service;
- no proof that the validation adapter's evidence claims are true outside the typed boundary;
- no continuous-outcome density integration, prior robustness, nonparametric likelihood
  uncertainty, posterior predictive checking, unknown-model component, or model averaging;
- no empirically domain-calibrated fragility, surprise, all-model miss, or retirement thresholds;
- no transactionally persisted F9 child snapshot, materialized retire/narrow hypothesis version,
  contradiction resolution, or migration change;
- no atomic confirmation reservation consumption, budget debit, capability lease, risk authority,
  scheduler integration, experiment execution, or replication-debt transition;
- no adaptive multi-step design, F9 scientific exit, or autonomous real-world campaign; the
  downstream isolated F9-S7 K3 acceptance score is now implemented; and
- no evidence for mechanism truth, causal effect, construct/surrogate validity, novelty,
  replication, SOTA, or publication readiness.

## Downstream slice

F9-S7 now independently scores the committed K3 evidence chain. It verifies substantive competing
hypotheses, prediction/selection chronology, one-to-one validated updates, high-belief discrimination,
prediction-changing negative revision, robust mechanism-claim exclusion, exact persistence,
contradictions, and stopping decisions, with no archive fallback. See
`F9_S7_INDEPENDENT_K3_ACCEPTANCE_IMPLEMENTATION_REPORT_2026_08_15.md`. The next unfinished work is the
F9 scientific-exit/integration bridge rather than another isolated schema.
