# ADR 0014: Evaluator-owned novelty calibration, artifact-derived coverage, and direction gating

- Status: Accepted
- Date: 2026-08-15
- Scope: F8-S5 / Knowledge Boundary Engine

## Context

F8-S1 through F8-S4 can preserve a licensed temporal corpus, replay search and citation traversal,
extract source-bound atomic claims, and produce reviewed nearest-prior relations. None of those
artifacts alone establishes novelty. A system can still manufacture a false novelty conclusion by:

- evaluating on examples chosen after seeing its output;
- exposing temporal-holdout labels to the candidate system;
- reporting a point estimate while ignoring small-sample uncertainty;
- measuring recall but not false strong-novelty errors;
- changing a claim's wording or query vocabulary until the answer changes;
- letting the live caller type six favorable coverage numbers;
- treating a failed calibration as irrelevant to an otherwise successful search;
- using fewer nearby prior works than the frozen policy requires;
- allowing candidate authors to review their own evidence package;
- upgrading an incremental result to a strong novelty label in the driver;
- treating a model critic's confidence or a missing search result as proof of novelty.

Recent benchmarks reinforce the need for a measured, literature-grounded decision rather than a
single judge score. RINoBench separates idea resemblance from reliable novelty scoring and releases
expert-labelled research ideas: [RINoBench](https://arxiv.org/abs/2603.10303). An axiomatic study
finds that no examined novelty metric satisfies every desired property:
[Axiomatic Benchmark](https://arxiv.org/abs/2604.15145). RQ-Bench reports disagreement between LLM
research-quality judgments and experts: [RQ-Bench](https://arxiv.org/abs/2606.12071). ScholarEval
and NovBench emphasize literature-grounded, multi-dimensional assessment rather than one scalar:
[ScholarEval](https://arxiv.org/abs/2510.16234),
[NovBench](https://arxiv.org/abs/2604.11543). The ACL Idea Novelty Checker evaluates retrieval-based
idea comparison: [Idea Novelty Checker](https://aclanthology.org/2025.sdp-1.9/). TREC Total Recall
provides the precedent for high-recall, review-oriented retrieval evaluation:
[TREC Total Recall guidelines](https://trec.nist.gov/data/total-recall/2016/Total%20Recall%20Guidelines%202016.html).
LitSearch supplies an ad-hoc scientific retrieval benchmark:
[LitSearch](https://aclanthology.org/2024.emnlp-main.840/).

These works motivate the protocol. They do not validate Aletheia's synthetic fixtures or establish
domain-general thresholds.

## Decision

### Separate global calibration from a live novelty decision

F8-S5 has three explicit layers:

1. evaluator-owned known-answer and temporal-holdout calibration;
2. live coverage derived from committed F8-S1 through F8-S4 artifacts;
3. author-excluded review, claim-strength capping, and a mechanical direction gate.

No layer may infer that a previous layer passed. Each object embeds or hashes the exact evidence it
consumes and is revalidated during construction and archive load.

### Freeze a validation split and a later temporal holdout

`NoveltyCalibrationSuite` contains canonical validation cases followed by strictly later temporal
holdout cases. The default floor is 40 cases in each split, with at least 30 known non-strong cases
and 10 strong-novel cases per split. Every case has a base formulation and at least two
semantics-preserving perturbations. Candidate authors cannot adjudicate labels.

The suite stores a commitment to evaluator-owned labels, not the labels themselves. Labels must be
frozen before suite sealing. The current custody boundary is contractual and content-addressed; it
does not claim hardware-enforced secrecy. Production evaluation still needs an independently
operated private suite and access controls.

Validation cutoffs must all precede every temporal-holdout cutoff. System, evaluator, relation-view
parser, classification policy, case inputs, corpus snapshots, graph bundles, search protocols, and
perturbation evidence are frozen identities.

### Sign every case/variant trial and retain errors

The evaluator emits an HMAC-SHA-256 receipt for every exact case/variant pair. A receipt binds the
system and evaluator manifests, candidate claim, prior-art resolution, search session, reviewed
relations, derived class, recovered search papers, outcome, and completion time. Keys must contain
at least 32 bytes and are not persisted in the report.

Every sealed variant must appear exactly once in canonical order. Trial IDs, receipt hashes,
resolution hashes, and search hashes cannot be reused. A failed trial retains its exception class
and a hash of its message, not the raw message, and makes the split fail.

### Use confidence bounds, not only point estimates

The report derives for both splits:

- known-answer claim recall;
- seed-reference recovery;
- novelty-class accuracy;
- false strong-novelty rate on known non-strong cases;
- missed strong-novelty rate;
- semantics-preserving perturbation stability;
- nearest-prior mean reciprocal rank;
- explicit failed-trial count.

Binomial signals use one-sided 95% Wilson bounds. Minimum signals compare their lower bound with the
threshold; error-rate signals compare their upper bound. Both validation and temporal holdout must
pass. The initial frozen limits include known-answer/seed lower bounds of 0.80, classification lower
bound of 0.75, false-strong upper bound of 0.10, missed-strong upper bound of 0.25, stability lower
bound of 0.90, and MRR of 0.80.

These are conservative engineering defaults and minimum sample floors, not universal scientific
constants. A production domain suite must preregister power, prevalence, label protocol, and any
stricter thresholds before evaluation.

### Freeze one relation-to-classification rule

The classifier consumes reviewed F8-S4 relations, never model prose. Any `equivalent` relation
yields `known_equivalent`; any `subsumes` or `special_case` relation yields `known_special_case`.
Otherwise the top ranked relation determines contradiction or combination, then exact difference
components distinguish novel method, novel phenomenon, and incremental extension. The classifier's
content hash is part of the evaluator manifest and is reused for live decisions.

### Derive all live coverage values from artifacts

`CalibratedNoveltyCoverageAssessment` accepts no caller-supplied observation tuple. It derives:

- known-answer recall and perturbation stability from the temporal calibration lower bounds;
- live seed recovery from the replayed campaign's exact paper hits;
- full-text availability from access grants for resolved prior papers;
- source-span verification from relation spans present in the immutable corpus;
- correction/retraction completion from the bound correction report.

The original F8-S2 harness still derives query-family coverage, source diversity, citation
saturation, and uncovered-source fraction. The live policy is rebuilt from the calibration policy,
must freeze before search starts, and cannot be weakened by the caller.

A failed global calibration blocks the combined verdict even if every live search signal passes.
The gate also enforces at least three resolved nearest-prior relations for every candidate claim;
this closes the previously unused `minimum_nearest_prior_art` policy field.

### Review the exact package with authors excluded

An authorship manifest binds candidate claims to one exact candidate artifact and a frozen sorted
author set. A novelty evidence package then binds calibrated coverage, policy, authorship, candidate
claim, ranked prior relations, mechanically derived classification and exact differences, temporal
cutoff, disclosures, and blockers.

Reviews must preserve package identity and canonical order, carry reviewer credentials and an
attestation receipt, occur after package assembly, and exclude every candidate author. Direction
authorization requires confirmed reviews from at least a domain expert and a research librarian.
A request for more search, rejected classification, missing role, or insufficient review count is
an unresolved blocker.

### Cap claims and authorize experiments mechanically

Insufficient coverage always yields `indeterminate_due_to_coverage`, a speculative ceiling, and no
experiment authorization. Equivalent or special-case prior art yields a `none` ceiling and rejects
the direction. Incremental or contradictory work may advance only as a bounded direction with a
`weak` novelty ceiling. A strong class reaches a `moderate` ceiling only when coverage, three-prior
floor, exact differences, author-excluded reviews, required roles, and all confirmations pass.

The direction gate cannot be edited independently of the reviewed decision. Its disposition,
authorization bit, rationale codes, and maximum claim strength are rederived on construction and
load.

### Add an auditable path to discovery without silently changing legacy runs

`discover()` now accepts an optional `auditable_novelty_gate_fn`. When present, the callback must
return a valid `ResearchDirectionGate` whose single candidate claim SHA-256 exactly equals the
candidate's `candidate_claim_sha256`. Callback errors, wrong types, coverage failures, known prior
art, and identity mismatches fail closed. The legacy count-plus-critic path remains for existing
runs but does not constitute F8-S5 acceptance.

## Consequences

- Calibration failures can no longer be hidden behind a successful live search.
- A perfect point estimate from a small sample cannot bypass one-sided confidence bounds.
- Temporal false-strong novelty, missed novelty, retrieval quality, ranking, and perturbation
  stability remain separately visible.
- Live callers cannot type or replace the six external coverage observations in the F8-S5 object.
- Reviewed prior-art relations, not model confidence, determine the novelty class.
- Candidate self-review and same-role review pairs cannot authorize a direction.
- Bounded incremental work may proceed without being advertised as strong novelty.
- Reports, combined coverage, and direction gates are content-addressed and reverify calibration
  signatures during load.
- The synthetic 80-case/240-trial fixture proves derivation and attack resistance only. It is not a
  real known-answer corpus, real temporal false-novelty result, or scientific novelty claim.
- Production adapters, expert labels, private custody, calibrated domain thresholds, and real
  prospective results remain scientific release gates.

## Rejected alternatives

- **Use an LLM novelty score:** it is not a recall measurement, has no temporal counterfactual, and
  cannot expose missed prior art.
- **Treat no retrieved match as novel:** retrieval outage and incomplete coverage would become
  positive novelty evidence.
- **Report only accuracy:** it hides false strong-novelty asymmetry and class prevalence.
- **Use point thresholds:** small perfect samples would appear more certain than the evidence
  warrants.
- **Let validation pass compensate for temporal failure:** this defeats the future-held-out test.
- **Let callers submit coverage observations:** it lets downstream code manufacture eligibility.
- **Count reviewer confirmations without roles:** two similar reviewers do not replace retrieval
  expertise plus domain expertise.
- **Reject all non-strong ideas:** useful incremental and contradiction-focused work can proceed if
  its novelty language remains bounded.
- **Replace the existing discovery gate unconditionally:** current live runs lack the complete F8
  artifacts, so an unconditional switch would either break them or invite fabricated placeholders.
