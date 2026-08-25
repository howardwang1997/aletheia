# ADR 0022: Independent K3 evidence-chain acceptance

- Status: Accepted
- Date: 2026-08-15
- Scope: F9-S7 / Competitive Causal World Model

## Context

F9-S1 through F9-S6 can produce typed competing hypotheses, causal contracts, pre-observation
likelihoods, constrained selections, validated observations, posteriors, revisions, and
contradictions. A collection of valid individual artifacts is still not proof that the complete
campaign respected temporal order, updated every and only valid observation, persisted negative
evidence, or withheld an unsupported mechanism claim.

The acceptance decision must therefore be independently reconstructable from committed artifacts,
not from a model-authored narrative or a scheduler's self-reported score.

This follows several useful precedents:

- Strong inference emphasizes multiple alternatives and experiments that can distinguish or exclude
  them rather than accumulating support for one favored explanation:
  [Platt, 1964](https://pubmed.ncbi.nlm.nih.gov/17739513/).
- The prequential view evaluates sequential probability forecasts against later observations, which
  makes prediction-before-observation a first-class boundary:
  [Dawid, 1984](https://rss.onlinelibrary.wiley.com/doi/10.2307/2981683).
- W3C PROV represents entities, activities, agents, derivations, and revisions as explicit
  provenance chains:
  [W3C PROV-O](https://www.w3.org/TR/prov-o/).
- The National Academies distinguishes computational reproducibility on the same data/code from
  replication with new data, and stresses complete methods and data-product reporting:
  [NASEM, 2019](https://nap.nationalacademies.org/catalog/25303/reproducibility-and-replicability-in-science).
- The TOP Guidelines organize preregistration, reporting, sharing, and verification practices across
  the research lifecycle:
  [Center for Open Science, TOP Guidelines](https://www.cos.io/initiatives/top-guidelines).

The concrete threats are:

- scoring embedded artifacts without reopening their physical archives;
- allowing the hypothesis/validation/update author to grade the chain;
- giving the scorer raw observations, ambient tools, or a second scheduler-specific scoring path;
- accepting zero valid observations because `0 updates == 0 valid observations` is vacuously true;
- updating one valid observation zero or multiple times, or updating rejected evidence;
- choosing an experiment that separates only low-belief or irrelevant hypotheses;
- treating a fragile posterior or causal-audit ceiling as permission for a mechanism claim;
- claiming alternative exclusion from only the nominal posterior while sensitivity cases retain an
  alternative;
- calling a negative result a revision when only prose changes and predictions stay identical;
- overwriting a hypothesis instead of materializing linked hypothesis and prediction children;
- losing failed attempts, belief versions, directives, contradictions, or stop reasons;
- continuing against the F9-S6 world revision; or
- trusting caller-supplied checks, verdicts, or damaged archive bytes.

## Decision

### Score committed artifacts, not summaries

`K3AcceptanceRequest` contains one or more ordered `K3RoundEvidence` objects. Each round exact-binds
one committed F9-S5 selection, one committed F9-S6 validation attempt, and zero or one committed
F9-S6 update attempt. It also binds a committed `K3EvidenceLedger` that records versions,
materialized revisions, mechanism-claim attempts, contradictions, and a terminal decision.

Before scoring, `run_k3_acceptance` physically reloads every selection, validation, update, and
evidence ledger from its declared content-addressed archive. Embedded and reloaded objects must be
equal. One missing, corrupt, noncanonical, or rebound artifact blocks the campaign, discards all
partial verifications/checks, and retains only a sanitized failure.

### Freeze an independent deterministic scorer before the first selection

`K3AcceptanceScorerManifest` freezes code, output schema, and principal; declares deterministic
runtime, no tools, and `observation_access="committed_artifacts_only"`; and must predate the first
selection. Its principal must differ from persistence/terminal roles and all F9-S2 through F9-S6
generator, reviewer, author, evaluator, assessor, validator, selector-harness, validation-harness,
and update-harness principals.

The scheduler-facing `score_k3` function delegates to the same epistemics scorer. There is no
alternate event-summary formula.

### Re-derive eleven complete checks

The scorer emits one canonical check for each boundary:

1. a nonduplicate active set containing null, primary, and alternative hypotheses;
2. prediction and selection commitments before observation, followed by validation and update;
3. exactly one update attempt for every validated confirmation and none for rejected evidence;
4. the selected likelihood separates at least two hypotheses above the frozen belief floor;
5. one exact question/belief lineage with append-only child snapshots;
6. mechanism claims respect causal and alternative-exclusion gates;
7. primary-negative results materialize real append-only hypothesis/prediction change;
8. every derived contradiction is persisted exactly;
9. every attempt, snapshot, belief, hypothesis, prediction, directive, and contradiction is in the
   evidence ledger;
10. one persisted terminal action follows the final F9-S6 world revision; and
11. at least one validated observation produced a successful belief update.

Checks contain canonical reason codes, evidence hashes, and relevant counts/thresholds. Campaign
model validation recomputes the complete check tuple and disposition, so a forged pass or changed
metric is invalid.

### Separate integrity rejection from an honest incomplete campaign

False chronology, update cardinality, lineage, claim, negative-result, contradiction, persistence,
or terminal checks produce `rejected_integrity`. Missing/corrupt archives produce
`blocked_execution`.

An intact chain without a successful update, or whose selected experiment misses the frozen
high-belief discrimination floor, is `partial_no_scientific_exit`. This preserves the valid spine
without letting an empty run pass. `accepted` requires an intact spine, a discriminating selected
experiment, and at least one successful validated update.

`accepted` is an engineering verdict about this evidence chain. It is not the F9 scientific exit,
which additionally requires frozen hidden-world comparison against K2, calibration/false-mechanism
thresholds, and a real materials campaign.

### Gate mechanism claims under every frozen sensitivity case

A claim record may be `withheld` or `issued`. Every record must bind an existing update, round,
evidence, and post-update decision time. An issued descriptive/association claim cannot exceed the
F9-S3 causal ceiling.

An issued within-model/causal mechanism claim additionally requires:

- a robust F9-S6 update and stable current hypothesis set;
- a non-null mechanistic target retained by the revision policy;
- target posterior at or above the claim floor in nominal and every sensitivity posterior;
- every competing explanation at or below the exclusion ceiling in nominal and every sensitivity
  posterior; and
- the requested ceiling no higher than the F9-S3 causal-audit ceiling.

Withholding an unauthorized claim passes; issuing it rejects the evidence chain.

### Require prediction-changing negative-result materialization

Every F9-S6 `narrow` or `retire` directive must have exactly one persisted
`K3RevisionMaterialization`. It creates a child hypothesis version with exact parent, identity,
role, chronology, and lifecycle.

`narrow` must also create child prediction versions for the exact source prediction set. Every child
prediction binds the revised hypothesis, exact parent, and `version + 1`, and changes at least one
testable field: observable, outcome space, expected outcome, direction, discrimination targets, or
measurement protocol. Merely changing hypothesis prose/rationale while retaining the same
prediction is rejected. `retire` cannot add predictions.

### Persist the terminal scientific response

The final decision binds the final round, evidence, principal, reason codes, and decision time. A
successful final update also binds its receipt and world-revision directive. Allowed responses are:

- `continue_research` or `stop_and_archive` after `continue_current_set`;
- `fork_hypothesis_set` or `stop_and_archive` after `fork_hypothesis_set`; and
- `seek_new_measurement` or `stop_and_archive` after `seek_new_measurement_or_stop`.

Without a successful final update, only `stop_and_archive` is accepted. The scorer does not execute
the terminal action.

## Consequences

- F9 now has a deterministic, observation-free, archive-replayed acceptance boundary.
- Empty campaigns cannot exploit a vacuously true one-to-one invariant.
- Validated observations and update attempts have an exact bijection.
- Mechanism claims are bounded by causal identification and robust alternative exclusion.
- Negative results must change future testable predictions, not only language.
- Missing attempts, versions, contradictions, or stop reasons are visible integrity failures.
- The evidence ledger is itself a physically committed artifact, but remains an isolated
  content-addressed ledger rather than proof of transactional PostgreSQL/scheduler persistence.
- A single round is supported; multi-round requests require the next selection to consume the exact
  prior round child snapshot. The current scheduler does not yet materialize that loop.
- This scorer does not measure hidden-world accuracy, calibration, false-mechanism rate, K2-relative
  efficiency, real measurement validity, or real-domain scientific success.

## Rejected alternatives

- **Ask an LLM for a pass/fail summary:** it cannot establish archive integrity or exact arithmetic.
- **Score scheduler prose/events only:** current F9 artifacts already provide stronger typed,
  content-addressed evidence; a future event adapter must resolve to these receipts.
- **Reuse a prior scientific role as scorer:** self-grading defeats the independent boundary.
- **Give the scorer raw observations:** F9-S6 validation is the only raw-data entry point.
- **Treat zero updates as full because counts match:** spine correctness at zero is not positive
  scientific evidence.
- **Count updates without identity mapping:** equal counts can hide a duplicate and an omission.
- **Use F9-S5's all-hypothesis minimum TV as the only exit check:** the accepted experiment must
  discriminate hypotheses that currently carry meaningful belief mass.
- **Authorize a mechanism claim from the nominal posterior only:** sensitivity can preserve or make
  dominant an alternative explanation.
- **Accept a negative-result wording change:** it creates no new falsifiable prediction.
- **Require every campaign to issue a mechanism claim:** safe withholding is valid and often the
  scientifically correct outcome.
- **Convert missing persistence into partial:** loss of attempts or versions is an integrity failure,
  not merely incomplete scientific evidence.
- **Auto-execute a fork/stop from the scorer:** outward scheduling remains a separately authorized
  consumer action.
