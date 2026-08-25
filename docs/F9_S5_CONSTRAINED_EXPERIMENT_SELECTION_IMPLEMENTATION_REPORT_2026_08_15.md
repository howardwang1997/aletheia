# F9-S5 constrained experiment selection implementation report

- Date: 2026-08-15
- Scope: archived prediction-candidate custody, exact discrete EIG, hard scientific/operational
  gates, fixed multi-attribute utility, complete reasoned ranking, and selection archive
- Engineering status: complete
- Scientific-exit status: not complete

## Outcome

F9-S5 now compares at least two immutable F9-S4 commitments under one exact F9-S1 belief state and
F9-S2 competing-hypothesis campaign. Before ranking, it physically reloads and rehashes every
prediction campaign from its content-addressed archive. It then derives every outcome marginal and
hypothetical posterior from the frozen prior and likelihoods, computes expected entropy reduction,
and retains the full audit.

Information does not override validity. Cost, time, risk, measurement validity, proxy risk,
capability, confirmation freshness, EIG, and pairwise-discrimination floors are hard constraints.
Only candidates without blockers receive a frozen multi-attribute utility. The decision retains every
candidate, score, blocker, rank, and reason; if no candidate is feasible, it selects nothing.

The assessment and request both declare no target-observation access. The assessor must be
independent from prior proposal, causal review, prediction, and calibration roles. Campaign replay
rederives information, constraints, utility, ranking, and disposition, so changing a score or winner
without changing its inputs is invalid.

This is an engineering result. All candidate protocols, calibrated likelihoods, validity evidence,
costs, capabilities, safety assessments, data partitions, and replication ledgers in acceptance tests
are synthetic. No real experiment is shown to be safe, feasible, valid, worth its cost, or capable of
distinguishing a true mechanism.

## Research basis

- [Lindley 1956](https://projecteuclid.org/journals/annals-of-mathematical-statistics/volume-27/issue-4/On-a-Measure-of-the-Information-Provided-by-an-Experiment/10.1214/aoms/1177728069.full)
  supplies the expected-reduction-in-uncertainty basis for information from an experiment.
- [Chaloner and Verdinelli 1995](https://projecteuclid.org/journals/statistical-science/volume-10/issue-3/Bayesian-Experimental-Design-A-Review/10.1214/ss/1177009939.full)
  frames Bayesian experiment design as expected utility under explicit design assumptions.
- [Box and Hill 1967](https://www.stat.cmu.edu/technometrics/59-69/VOL-09-01/v0901057.pdf)
  connects sequential experimental choice, expected entropy change, and discrimination among rival
  mechanistic models.
- [Farrow and Goldstein 2006](https://doi.org/10.1016/j.jspi.2004.07.008) motivates making
  information/cost trade-offs explicit in multicriterion experiment design.
- [Cronbach and Meehl 1955](https://pubmed.ncbi.nlm.nih.gov/13245896/) motivates treating construct
  validity as an evidence problem rather than inferring validity from a useful-looking score.
- [Prentice 1989](https://onlinelibrary.wiley.com/doi/pdf/10.1002/sim.4780080407) motivates a separate,
  demanding validity boundary for proxy/surrogate outcomes.

ADR 0020 narrows these foundations to the exact implemented trust boundary. The current weighted
utility is an engineering policy, not a claim that these references prescribe its particular
weights.

## Delivered contracts

`aletheia/epistemics/selector.py` adds frozen, extra-forbid contracts for:

- risk, measurement-validity, proxy-risk, campaign, and candidate dispositions;
- unused fresh-confirmation partition custody and expiry;
- exact-bound per-candidate scientific/operational assessments;
- complete assessment batches and an independent observation-blind assessor manifest;
- normalized utility weights and a frozen selection policy;
- committed prediction candidates and an exact shared-world selection request;
- per-outcome marginals, complete hypothetical posteriors, and EIG audits;
- physical prediction-archive verification receipts;
- candidate score components, blockers, ranks, reasons, and decision;
- hash-only archive failure;
- mechanically rederived selection campaign; and
- canonical content-addressed campaign commitment time, receipt, and replay.

The assessment output schema has a public canonical SHA-256 identity. Every collection has bounded
size and canonical ordering/uniqueness rules.

## Exact prior and candidate boundary

Every candidate embeds a `CommittedPredictionCommitmentCampaign`. The request rejects fewer than two
candidates, duplicate IDs, noncanonical ordering, duplicate substantive commitments, and duplicate
experiment namespaces.

All candidates must share the exact:

- research-question hash;
- F9-S1 snapshot and normalized belief-state hash; and
- F9-S2 source hypothesis-campaign hash.

The candidate commitments and all policy/assessor/assessment dependencies must predate the request.
Each assessment exact-binds the F9-S4 campaign and commitment, experiment protocol, F9-S3 outcome
measurement process/error model, and assessor manifest. Incomplete assessment coverage or any
rebinding makes request construction fail.

## Physical archive admission

The selector does not rank embedded payloads on trust. It calls the F9-S4 archive loader for every
candidate, which verifies canonical bytes, content hashes, nested causal/hypothesis/world-model
contracts, and F9-S4 calibration/diagnostic/decision derivations. Loaded and embedded campaign
objects must be identical.

Successful verification retains campaign and commitment hashes, F9-S4 commitment receipt, archive
ledger receipt, custody identity, and verification time. A missing/corrupt/rebound candidate returns
`blocked_execution` with:

- the failed candidate ID;
- stable failure kind `prediction_archive_invalid`;
- error class; and
- SHA-256 of detail text.

No prior partial verification, score, decision, or raw exception detail is presented as a trusted
result.

## EIG and discrimination derivation

Only an F9-S4 `ready` probabilistic campaign with `eig_eligible=true` is eligible. The harness reads
the exact F9-S1 belief vector and complete F9-S4 bin likelihoods and derives:

```text
p(y | e)     = sum_h p(h) p(y | h,e)
p(h | y,e)   = p(h) p(y | h,e) / p(y | e)
EIG(e)       = H[p(h)] - sum_y p(y | e) H[p(h | y,e)]
EIG ratio    = EIG(e) / H[p(h)]
pairwise TV  = 0.5 * sum_y |p(y | h_i,e) - p(y | h_j,e)|
```

The audit keeps every preregistered outcome even when marginal probability is zero. For every
positive-marginal outcome, posterior probabilities cover the exact active hypothesis versions and
sum to one. EIG is bounded against negative floating-point residue, the normalized ratio is clamped
to `[0,1]`, and output metrics use a frozen 12-digit representation.

The full ledger is important: the selected scalar can be checked against every marginal and
hypothetical posterior. These posterior objects are planning calculations only and never mutate the
world-model belief state.

## Hard gates before utility

Candidate blockers are mechanically derived for:

- non-ready, ordinal, or otherwise non-EIG-eligible prediction campaigns;
- EIG ratio or minimum pairwise total variation below policy;
- cost above the fixed budget or currency mismatch;
- duration above the fixed limit;
- risk above the maximum and any prohibited risk;
- non-validated measurement or validity confidence below policy;
- bounded or invalid surrogate/proxy risk;
- missing required capability identities;
- missing, expired, post-assessment, calibration-reused, or target-reused confirmation partitions.

Blockers are canonical, unique, and retained. Feasibility is exactly `not blockers`; it is not an
adapter-authored boolean. A high-EIG invalid proxy is therefore infeasible and cannot win.

## Fixed multi-attribute utility

Feasible candidates combine four benefits and three penalties:

- normalized EIG;
- minimum pairwise total variation;
- fresh-confirmation availability up to a frozen saturation count;
- proportional repayment of frozen replication debt;
- cost divided by the policy budget;
- duration divided by the policy limit; and
- a fixed burden for the categorical risk level.

Weights are nonnegative, finite, frozen, and sum to one. Default weights are 0.45 EIG, 0.20 minimum
TV, 0.10 freshness, 0.10 replication, and 0.05 each for cost, duration, and risk penalties.

No component uses candidate-relative min/max scaling. A decoy cannot alter another candidate's score
by changing the comparison set. Replication benefit requires an independent frozen protocol and
cannot reduce debt below zero.

## Complete deterministic decision

Ordering is:

1. feasible before infeasible;
2. larger constrained utility;
3. larger absolute EIG;
4. lower cost ratio;
5. lower duration ratio; and
6. lexical candidate ID.

Exactly one feasible candidate receives `selected/highest_constrained_utility`. Other feasible
candidates receive `feasible_not_selected/lower_constrained_utility`. Infeasible candidates retain
all blockers as reasons. If no candidate is feasible, disposition is `no_feasible_experiment`, all
candidates remain in the ranking, and the selected ID is absent.

Campaign model validation reruns the complete derivation. Forged utility, score, order, reason,
winner, or disposition is rejected.

## Independence and observation isolation

The assessor manifest requires `tool_policy="none"`, no named tools, and
`observation_access="none"`. Deterministic adapters cannot declare transport; model adapters may
declare only frozen model transport and require instruction/model identities.

The assessor principal must differ from hypothesis generator/reviewer, causal author/reviewer,
prediction author, and calibration evaluator across every candidate. A model-backed assessor must
also use a distinct model identity. The request repeats the no-observation declaration and contains
no observation payload or observation-store handle.

The implementation trusts the typed assessment and its evidence hashes after structural and exact
binding checks. It does not yet authenticate evidence issuers, open evidence objects, or execute an
assessment adapter. This limitation is explicit and remains a production release gate.

## Selection archive

The complete campaign is stored as canonical JSON through the existing write-once archive. The
committed wrapper binds campaign, ledger, and an explicit timezone-aware commit time no earlier than
campaign generation, and exposes a content-hash receipt. The ledger binds campaign object hash,
canonical byte hash, byte length, and that same archive time. Loading rehashes bytes, requires
canonical JSON, validates the campaign identity, and reruns nested derivations.

Selection replay proves what inputs and policy produced the stored decision. It does not keep the
upstream prediction archive alive or reauthenticate external assessment evidence; archive custody
and long-term evidence retention remain operational responsibilities.

## Acceptance evidence

Focused F9-S5 acceptance:

```text
30 passed in 42.69 s
changed Python Ruff and compilation: passed
```

All epistemics tests through F9-S5:

```text
145 passed in 49.87 s
```

Coverage includes:

- synthetic F8-S1–S5 → F9-S2 → F9-S3 → three F9-S4 candidates → F9-S5 integration;
- an exact closed-form symmetric three-hypothesis EIG case;
- complete outcome-marginal and hypothetical-posterior retention/normalization;
- observation-blind request/assessor and frozen output-schema identity;
- archive verification of every candidate before ranking;
- high-EIG invalid-proxy rejection;
- cost, currency, duration, high/prohibited risk, measurement confidence/status, and bounded-proxy
  hard gates;
- missing capability and missing/expired/calibration-reused confirmation blockers;
- duplicate confirmation-partition rejection and explicit selection commit chronology/receipt;
- replication-debt-prioritized and cost-prioritized frozen policies;
- exact score invariance when a candidate is removed, proving no candidate-relative rescaling;
- deterministic tie resolution;
- ordinal and F9-S4-blocked candidates retained as infeasible;
- duplicate commitment, candidate ordering, incomplete assessment, and measurement rebinding
  rejection;
- assessor principal independence and request/assessment/commitment chronology;
- explicit no-feasible decision without fallback;
- missing upstream archive producing a hash-only whole-campaign failure;
- score/decision forgery rejection; and
- selection archive round trip and byte-tamper detection.

Repository-wide acceptance:

```text
non-Docker: 1062 passed, 1 skipped, 29 deselected in 380.76 s
Docker:       29 passed, 1063 deselected in 31.31 s
```

Both final repository-wide runs passed. After mechanical Ruff formatting, the complete focused
F9-S5 suite passed again, and all changed Python files passed Ruff lint, Ruff format check, and
compilation.

## Files added or materially changed

- `aletheia/epistemics/selector.py`;
- `aletheia/epistemics/__init__.py`;
- `tests/epistemics/f9s5_fixtures.py`;
- `tests/epistemics/test_eig_selector.py`;
- `docs/adr/0020-f9-observation-blind-constrained-experiment-selection.md`;
- `docs/epistemics/CONSTRAINED_EXPERIMENT_SELECTION.md`;
- this report, README, docs index, F9-S4 guide, and F7–F12 master-plan status.

## Explicit non-guarantees

- no real-domain candidate, likelihood, measurement-validation suite, cost estimate, risk approval,
  capability evidence, data-custody service, or replication ledger;
- no proof that EIG or pairwise TV is robust to plausible priors, likelihood families, binning, or
  measurement error outside the already frozen F9-S4 scenarios;
- no continuous-density integration, sequential/adaptive horizon, experiment portfolio, Pareto
  frontier, imprecise utility, or value-of-sample-information analysis;
- no empirically calibrated utility weights or universal commensurability of money, time, risk,
  replication, and information;
- no authenticated assessment evidence or executed independent assessment adapter;
- no atomic confirmation reservation, debt consumption, budget debit, capability lease, or safety
  authority;
- no scheduler wiring, intervention execution, or result parsing inside F9-S5; the downstream F9-S6
  slice now provides an isolated observation-validation, posterior-update, negative-result revision,
  and contradiction boundary without changing this selector's guarantees;
- no K3 acceptance score or F9 scientific exit; and
- no real evidence for any hypothesis, mechanism, effect, construct validity, causal claim, novelty,
  replication, or SOTA claim.

## Downstream slice

F9-S6 now consumes only an independently validated observation exact-bound to the selected F9-S4
commitment and F9-S5 decision. It derives posterior, surprise, entropy change, and likelihood
sensitivity; refuses updates on material/unknown protocol deviation or exploration/confirmation
leakage; preserves negative evidence; and represents retirement, narrowing, and forks as append-only
directives rather than history mutation. See
`F9_S6_VALIDATED_OBSERVATION_BELIEF_UPDATE_IMPLEMENTATION_REPORT_2026_08_15.md`. The next unfinished
work after the completed F9-S7 independent scorer is the F9 scientific-exit/integration bridge. See
`F9_S7_INDEPENDENT_K3_ACCEPTANCE_IMPLEMENTATION_REPORT_2026_08_15.md`.
