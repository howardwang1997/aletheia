# ADR 0021: Validated-observation Bayesian update and append-only revision

- Status: Accepted
- Date: 2026-08-15
- Scope: F9-S6 / Competitive Causal World Model

## Context

F9-S5 can select and commit one observation-blind experiment, but selection is not permission to
turn arbitrary bytes into scientific belief. A posterior update needs a one-way boundary between
raw observations and the world model, exact reuse of the pre-observation likelihood, sensitivity to
likelihood assumptions, and an immutable account of negative or contradictory evidence.

Bayesian workflow treats model building, inference, checking, comparison, and sensitivity as an
iterative process rather than equating one posterior with validation:
[Gelman et al., 2020](https://arxiv.org/abs/2011.01808).
Prior-data conflict diagnostics make surprising observations visible rather than silently absorbing
them into a posterior:
[Evans and Moshonov, 2006](https://doi.org/10.1214/06-BA129).
Robust Bayesian analysis asks how conclusions vary under plausible prior or likelihood
perturbations:
[Berger and Mortera, 1994](https://www.sciencedirect.com/science/article/pii/0378375894901317) and
[Lopes and Tobias, 2011](https://doi.org/10.1146/annurev-economics-111809-125134).

Registered Reports provide a useful process precedent: methods and analysis plans are reviewed
before outcomes are known, reducing result-contingent changes:
[Nature, Registered Reports](https://www.nature.com/nature-portfolio/editorial-policies/registered-reports).
Protocol deviations still need explicit classification and handling; the FDA's current page is
draft, nonbinding guidance and is used here only as a process reference, not as a regulatory claim:
[FDA, Protocol Deviations](https://www.fda.gov/regulatory-information/search-fda-guidance-documents/protocol-deviations-clinical-investigations-drugs-biological-products-and-devices).
Truth-maintenance systems also motivate retaining contradictions and their assumptions rather than
destructively rewriting conclusions:
[de Kleer, 1986](https://www.dekleer.org/Publications/An%20Assumption-Based%20TMS.pdf).

The implementation must address these threats:

- updating from exploration, calibration, unblinded, identity-uncertain, or custody-uncertain data;
- accepting an outcome outside the frozen F9-S4 schema or a partition not reserved by F9-S5;
- validating against a changed parser, analysis plan, measurement protocol, or error model;
- freezing validation rules only after selection or observation;
- letting a prior scientific role validate its own result or giving the validator ambient tools;
- exposing raw observation bytes to the posterior updater;
- accepting a material or unknown protocol deviation as confirmation;
- using a small sample without a pre-registered update rule;
- loading an embedded receipt without physically verifying its archive/store object;
- computing only the nominal posterior and hiding likelihood sensitivity or model miss;
- treating a negative result as automatic deletion, or silently changing hypothesis history;
- continuing when all hypotheses make the same realized prediction;
- overwriting F9-S1 belief state or hypothesis versions; and
- trusting caller-supplied posterior, revision, contradiction, or archive fields.

## Decision

### Split validation and belief update into two committed transactions

The raw-observation side accepts exactly one staged F9-S4 payload and a committed F9-S5 selection.
`run_observation_validation` physically reloads and rehashes the selection campaign, selected
prediction campaign, and staged raw bytes before invoking the validator. It archives a canonical
validation campaign and commitment receipt.

The belief-update side accepts only a committed validation campaign with disposition
`validated_confirmation`. It physically reloads that campaign and declares
`observation_access="validated_artifact_only"`; it receives no raw bytes or observation-store
handle. Missing, corrupt, noncanonical, or rebound objects fail closed with hash-only errors and no
partial posterior.

### Freeze an independent, bounded validation role before selection

`ObservationValidatorManifest` freezes adapter, parser, output schema, principal, and optional model
identity. It has no ambient tools and can see only the exact staged payload. Its principal and model
identity must differ from the selector assessor and every F9-S2/F9-S3/F9-S4 generator, reviewer,
author, and calibration role. Both validator and validation policy must be frozen no later than the
F9-S5 selection commitment.

Validation output exact-binds the request, selection and prediction commitments, observation
receipt and bytes hash, fresh-confirmation batch and partition, outcome bin, parser, analysis plan,
measurement protocol, error model, validator, execution evidence, and completion time. The selected
experiment must have been committed before observation, and the reservation must be valid at the
recorded observation time.

### Treat scientific validity as non-compensable

Only confirmation data with verified experiment identity and custody, valid measurement, intact
blinding, exact or pre-registered-tolerance protocol adherence, resolved-accept audit, and sufficient
sample size may update belief. A smaller sample is allowed only when the exact rule hash was frozen
in policy and returned by validation. Exploration/calibration data, material or unknown deviations,
invalid measurement, compromised blinding, unresolved audit, reservation rebinding, or an outcome
outside the schema yield `rejected_scientific` or invalid-output failure, never a posterior.

Validator exceptions and malformed output retain error class and opaque hashes, not raw error text or
untrusted payloads. Future-dated validator output is invalid.

### Reuse the exact frozen likelihood and retain the complete update audit

For validated outcome \(y\), exact source prior \(p(h)\), and F9-S4 likelihood \(p(y\mid h)\), the
harness computes

\[
p(y)=\sum_h p(h)p(y\mid h), \qquad
p(h\mid y)=\frac{p(h)p(y\mid h)}{p(y)}.
\]

The audit retains every prior, realized likelihood, unnormalized mass, posterior, modal-prediction
match, prior/posterior entropy, realized entropy change, prior-predictive probability, surprisal,
winner set, primary-negative flag, and an exact realized-likelihood bundle hash. Zero predictive
mass blocks the update.

Every hypothesis must also contain the same policy-minimum set of F9-S4 sensitivity scenarios. The
harness computes a complete posterior for every scenario, its total-variation distance from the
nominal posterior, and whether the winner changes. An incomplete or zero-mass matrix blocks the
update. A winner change or total variation above policy yields `updated_fragile`; it does not erase
the posterior, and it opens a likelihood-sensitivity contradiction.

### Create child belief state and append-only revision directives

A successful update creates a child `BeliefState` with `version + 1`, an exact parent hash,
`validated_observation` update kind, the validation receipt, and the realized-likelihood bundle. It
creates a new `WorldModelSnapshot` while preserving the exact question, hypothesis, assumption, and
prediction versions. Existing objects are never mutated.

Per-hypothesis directives are:

- `retain` when the realized outcome matches the modal prediction;
- `retire` only when a miss leaves posterior probability at or below the retirement ceiling in the
  nominal update and every sensitivity scenario; or
- `narrow` when the modal prediction misses but retirement is not robust.

`retire` and `narrow` require a future hypothesis version and explicitly forbid mutation. The
campaign emits directives; it does not materialize rewritten scientific content.

### Keep contradictions open and force world-level responses

The campaign creates immutable, open contradiction records for modal prediction misses,
prior-predictive surprise, likelihood sensitivity, all-model low likelihood, and an uninformative
realized outcome. Records bind the validation receipt, prediction commitment, hypotheses, evidence,
severity, reason code, and detection time.

If every model assigns low likelihood, the world directive is `fork_hypothesis_set` and requires a
new hypothesis lineage. If realized likelihoods are equal within frozen tolerance, the directive is
`seek_new_measurement_or_stop`. Otherwise it is `continue_current_set`. Negative evidence is thus
preserved without making a single miss equivalent to proof of falsity.

### Re-derive and archive all outcomes

Pydantic campaign validation recomputes the validation probe, posterior, sensitivity matrix,
fragility, revision directives, world directive, contradiction queue, blockers, and disposition.
Both validation and update campaigns use canonical content-addressed archives with explicit commit
times and receipts. Replay rehashes bytes and reruns nested validation, so forged decisions or
archive tampering fail closed.

## Consequences

- Raw scientific data has one bounded entry point; the posterior updater consumes only a validated
  artifact.
- A committed selection alone is insufficient to update belief.
- Protocol, identity, custody, measurement, blinding, and confirmation-role failures cannot be
  offset by a persuasive result.
- Nominal posterior calculation is accompanied by complete likelihood-sensitivity posteriors.
- Negative results create inspectable revision work instead of silent deletion or history mutation.
- All-model miss and uninformative outcomes force explicit hypothesis-set or measurement decisions.
- Archive verification and mechanical re-derivation improve replayability and tamper detection.
- The validator remains a trusted evidence-producing boundary; typed output does not prove that its
  real-world identity, measurement, custody, or audit assertions are true.
- This slice does not execute experiments, authenticate laboratory systems, persist directives into
  the F9 PostgreSQL repository, schedule follow-up work, or establish real scientific validity.

## Rejected alternatives

- **Update directly from staged bytes:** it collapses measurement/audit policy into posterior code
  and expands raw-data authority.
- **Accept any well-typed outcome:** schema validity is not scientific validity.
- **Freeze the validator after seeing the result:** it permits outcome-dependent validation rules.
- **Use the same model or principal for proposal and validation:** it removes the intended
  independence boundary.
- **Treat deviations as a numeric penalty:** material or unknown deviations must block confirmation.
- **Ignore small samples but widen uncertainty later:** eligibility for an exceptional update must be
  pre-registered, not invented after observation.
- **Report only the nominal posterior:** apparently decisive results may reverse under plausible
  likelihood perturbations.
- **Delete a hypothesis after one negative result:** likelihood models and observations can be wrong;
  retirement requires robustness and append-only versioning.
- **Keep going when every hypothesis predicts the same realized outcome:** such evidence cannot
  discriminate the current set.
- **Normalize away all-model miss:** a low likelihood under every model is a signal to expand the
  hypothesis space, not merely redistribute mass.
- **Mutate the existing belief row or hypothesis:** it destroys provenance and makes replay
  ambiguous.
- **Auto-resolve contradictions inside the update:** resolution needs new evidence or scientific
  judgment and belongs to a later workflow.
