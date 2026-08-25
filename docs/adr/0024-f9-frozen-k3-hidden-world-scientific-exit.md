# ADR 0024: Frozen K3 hidden-world ablation and fail-closed scientific exit

- Status: Accepted
- Date: 2026-08-15

## Context

F9-S1 through S8 established a replayable engineering chain for competing hypotheses, causal
contracts, pre-observation predictions, constrained experiment selection, validated Bayesian
updates, independent evidence-chain acceptance, and transactional next-round continuation. Those
synthetic checks do not establish that K3 improves scientific behavior relative to the historical
K2 single-proposition loop.

The F9 scientific exit requires faster rejection of wrong explanations, more genuinely
discriminating experiments than a headline-metric agent, calibrated posteriors, a bounded
false-mechanism rate, and substantive hypothesis-space contraction. Final task success alone cannot
distinguish discovery from lucky action selection. Candidate self-reports cannot safely supply
truth-relative endpoints.

F7 already provides the useful infrastructure: an isolated hidden-world scorer, signed receipts,
an append-only attempt ledger, same-model paired matrices, no-best-of-N auditing, validation-before-
test threshold freezing, private-suite custody, and PASS/FAIL/BLOCKED semantics. Its four semantic
arm labels must not be repurposed because F7 `full_k2` is not F9 K3.

## Decision

1. Add a separate canonical three-arm protocol: headline metric, K2 single hypothesis, and K3
   competing hypotheses.
2. Require exact equality of base model, public task prompt, tools, budget, wall time, sampling,
   task/repeat slots, and seeds. Only the declared treatment prompt/system identity may vary.
3. Extend the trusted DiscoveryWorld scorer with a content-addressed truth-relative metrics object
   derived from the governing rule and authoritative action trace after reproducible execution.
4. Freeze two primary paired endpoints before access: K3-vs-K2 wrong-explanation elimination and
   K3-vs-headline discriminating-trial rate. Retain K3-vs-K2 scientific-success non-inferiority.
5. Report both fixed-bin top-label ECE and normalized multiclass Brier score. ECE is a diagnostic;
   the proper score prevents a passing interpretation based only on binning.
6. Measure false issued mechanism claims over all valid cells and require minimum mechanism-claim
   coverage, so abstention cannot manufacture a zero false-mechanism rate.
7. Use task/repeat hierarchical paired bootstrap intervals, exact sign tests, and Holm correction
   over the two primary comparisons. Retain every preregistered cell and authorized retry.
8. Freeze the threshold policy before validation. Validation must qualify a separate test matrix
   without treatment drift, and the resulting acceptance config must predate test execution.
9. Require a passing F7 private-custody artifact bound to the exact prospective suite for a formal
   scientific exit. Public DiscoveryWorld executions are diagnostic and return BLOCKED even if all
   measured thresholds pass.
10. Reaggregate raw ledger and signed receipts inside the final decision call. Operator-authored
    aggregate JSON is never authoritative.

## Consequences

- A successful task with an incorrect rule counts as a false mechanism, not discovery.
- Early valid exclusion is distinguishable from late or redundant experimentation.
- Missing new scorer metrics, changed scorer identity, incomplete traces, omitted attempts, forged
  signatures, or ledger drift are integrity errors rather than scientific failures.
- A complete threshold miss is FAIL; missing custody or prospective evidence is BLOCKED.
- The checked-in protocol can be frozen while honestly listing unresolved live inputs.
- The scorer implementation hash changes, so the prior public suite must be regenerated before a
  new diagnostic execution.
- Four public easy laws are insufficient to establish general frontier-scientist competence;
  private prospective tasks and the real materials chain remain required.

## Rejected alternatives

### Reuse F7 `ALETHEIA_FULL_K2` as K3

Rejected because it changes the treatment denoted by a frozen arm label and invalidates both F7 and
F9 comparisons.

### Score only official task completion

Rejected because a policy may complete the task by luck or optimize procedural progress without
maintaining, testing, or eliminating explanations.

### Trust candidate-reported EIG, posterior, or experiment labels

Rejected because all three are gameable. The scorer uses the hidden rule and evaluator-owned trace.

### Count false mechanisms only among issued claims without a coverage floor

Rejected because universal abstention would receive a perfect error rate.

### Tune endpoints or thresholds after seeing test results

Rejected. The analysis and threshold policies are content-addressed before validation; validation
qualifies but does not relabel the held-out outcome.

### Let public DiscoveryWorld satisfy the scientific exit

Rejected because its source, parametric rules, and answers are publicly inspectable. It remains a
valuable engineering and model-behavior diagnostic, not contamination-resistant final evidence.
