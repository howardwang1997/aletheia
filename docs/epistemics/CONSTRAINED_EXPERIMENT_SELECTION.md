# F9-S5 observation-blind constrained experiment selection

## What is available

F9-S5 compares multiple immutable F9-S4 prediction commitments under one exact world model, derives
their expected information gain, applies non-compensable feasibility gates, and emits a complete,
deterministic selection ledger.

```text
same F9-S1 belief state + F9-S2 hypothesis campaign
  -> at least two archived F9-S4 commitments
  -> independent observation-blind candidate assessments
  -> physical archive verification for every commitment
  -> full outcome-marginal / hypothetical-posterior EIG audits
  -> cost, time, risk, validity, proxy, capability, freshness hard gates
  -> fixed multi-attribute utility for feasible candidates only
  -> selected / feasible-not-selected / infeasible ranking with reasons
  -> canonical content-addressed selection campaign
```

The implementation is in `aletheia.epistemics.selector` and is exported by
`aletheia.epistemics`. It selects a frozen experiment commitment, not an executable job. It does not
read observations, reserve or consume data itself, start equipment, spend money, validate results,
or update beliefs.

## Exact candidate boundary

An `ExperimentSelectionRequest` requires 2–128 canonical candidates. Every candidate must:

- contain a `CommittedPredictionCommitmentCampaign`;
- share the exact research question, world-model snapshot, F9-S1 belief state, and F9-S2 source
  campaign;
- use a unique substantive prediction commitment and unique experiment namespace;
- have been committed no later than the selection request; and
- have exactly one complete, exact-bound assessment.

Candidates may use different F9-S3 causal campaigns and F9-S4 experiment protocols. That difference
is the point of comparison, but all candidates must address the same active hypotheses and prior.

The request carries `observation_access="none"`. It does not accept target bytes, parsed outcomes, or
an observation-store handle.

## Independent candidate assessment

`ExperimentAssessmentManifest` freezes:

- assessor principal and, for a model runtime, model identity;
- adapter, parser, instruction, and output-schema hashes;
- no ambient tools;
- no observation access; and
- either no transport or model-only transport.

The assessor must be independent from every candidate's hypothesis generator/reviewer, causal
author/reviewer, prediction author, and calibration evaluator. A model identity also cannot be
reused across those roles.

Each `CandidateExperimentAssessment` exact-binds the prediction campaign/commitment, protocol,
measurement process, measurement-error model, and assessor. It records evidence hashes for:

- measurement validity status and confidence;
- proxy/surrogate risk;
- estimated cost, currency, duration, and risk;
- required and available capabilities;
- unused confirmation-partition custody;
- current replication debt and expected reduction; and
- the complete assessment.

The current implementation validates identities, chronology, completeness, and coherence. It does
not independently open each evidence hash or prove that a human/model assessment is scientifically
correct. Production admission therefore needs an authenticated evidence registry and domain policy.

## Archive verification before ranking

`run_experiment_selection` reloads every F9-S4 campaign through
`load_prediction_commitment_campaign`. This rehashes the content-addressed bytes and revalidates the
complete embedded F8/F9 chain and F9-S4 derivations. The loaded campaign must equal the wrapper in the
selection request.

Each successful read produces `PredictionArchiveVerification` binding candidate, execution hash,
substantive commitment hash, F9-S4 receipt, archive ledger receipt, custody identity, and verification
time. A missing, corrupt, noncanonical, or rebound candidate blocks the entire campaign as
`blocked_execution`. The failure stores error class and detail hash, but no untrusted error text or
partial scores.

## Expected information gain

Only `ready`, probabilistic, `eig_eligible=true` F9-S4 campaigns receive an EIG audit. Ordinal and
blocked campaigns remain in the candidate ledger but are infeasible.

For prior \(p(h)\), candidate likelihood \(p(y\mid h,e)\), and every frozen outcome bin:

\[
p(y\mid e)=\sum_h p(h)p(y\mid h,e), \qquad
p(h\mid y,e)=\frac{p(h)p(y\mid h,e)}{p(y\mid e)}
\]

\[
\operatorname{EIG}(e)=H[p(h)]-\sum_y p(y\mid e)H[p(h\mid y,e)].
\]

`ExpectedInformationGainAudit` retains:

- exact commitment and belief-state identities;
- prior and expected posterior entropy in nats;
- absolute EIG and EIG divided by prior entropy;
- every outcome marginal;
- every outcome's complete hypothetical posterior and entropy; and
- minimum and maximum pairwise total-variation distance between hypothesis likelihoods.

The outcome posteriors are counterfactual planning quantities. They are never written to F9-S1 as a
belief update.

## Hard feasibility gates

Hard gates run before utility and cannot be offset by higher EIG.

| Boundary | Required condition |
|---|---|
| prediction | ready, probabilistic, EIG-eligible archived F9-S4 commitment |
| information | EIG ratio and minimum pairwise TV meet frozen floors |
| money | exact currency and cost no greater than budget |
| time | duration no greater than policy limit |
| safety | risk no greater than policy maximum and never prohibited |
| measurement | validated status plus minimum confidence |
| proxy | `none`; bounded and invalid surrogate risk both block |
| capability | every required capability has an available identity |
| confirmation | minimum count of unused, unexpired reserved partitions |
| leakage | confirmation partition is neither calibration split nor target namespace |

Blocker strings are canonical and retained on `ExperimentCandidateScore`, for example
`cost:budget_exceeded`, `measurement:proxy_risk:invalid_surrogate`, or
`fresh_confirmation:reuses_calibration:<batch-sha256>`.

## Fresh confirmation and replication debt

A `FreshConfirmationBatch` records content, partition, custody, seal time, availability deadline, and
the only accepted state: `reserved_unused`. A valid batch must still be available when selection is
issued and must predate its assessment. Batch identities must be canonical and both batch and
partition identities must be unique within a candidate assessment, so duplicate receipts cannot
inflate availability. Calibration-split and target-namespace reuse are explicit blockers.
Availability contributes to utility only up to the policy's fixed saturation count.

Replication debt is a bounded count with an immutable ledger hash. Claimed reduction cannot exceed
existing debt and is credited only when `independent_replication=true` and a frozen replication
protocol hash exists. The score records debt before, proportional reduction, and debt after.

Neither mechanism consumes a reservation or mutates debt. Those atomic state transitions belong to
later scheduler/persistence integration.

## Fixed constrained utility and ordering

After all blockers are derived, feasible candidates receive:

```text
utility =
    w_eig         * EIG / prior entropy
  + w_tv          * minimum pairwise TV
  + w_fresh       * min(valid batches / saturation batches, 1)
  + w_replication * debt reduction / debt before
  - w_cost        * estimated cost / policy budget
  - w_duration    * estimated duration / policy limit
  - w_risk        * fixed risk burden
```

`SelectionUtilityWeights` must be finite, nonnegative, frozen, and sum to one. Default weights are:

| Component | Weight |
|---|---:|
| normalized EIG | 0.45 |
| minimum pairwise TV | 0.20 |
| fresh confirmation | 0.10 |
| replication-debt reduction | 0.10 |
| cost penalty | 0.05 |
| duration penalty | 0.05 |
| risk penalty | 0.05 |

Cost, duration, and risk use policy-fixed denominators/burdens. No score is normalized against the
best or worst current candidate.

Ranking uses, in order: feasible first, larger constrained utility, larger absolute EIG, lower cost
ratio, lower duration ratio, and lexical candidate ID. The winner receives
`highest_constrained_utility`; other feasible candidates receive `lower_constrained_utility`;
infeasible candidates retain every blocker. If none is feasible, selection returns
`no_feasible_experiment` and `selected_candidate_id=None`.

## Building and running a selection

```python
from aletheia.epistemics import (
    ExperimentCandidate,
    build_experiment_selection_request,
    commit_experiment_selection_campaign,
    run_experiment_selection,
)

candidates = tuple(
    ExperimentCandidate(
        candidate_id=candidate_id,
        committed_prediction=committed_prediction,
    )
    for candidate_id, committed_prediction in committed_predictions
)

request = build_experiment_selection_request(
    selection_id="selection-001",
    candidates=candidates,
    assessment_batch=assessment_batch,
    assessor_manifest=assessor_manifest,
    policy=policy,
    prediction_archive_custody_sha256=archive_custody_sha256,
    issued_at=issued_at,
)

campaign = run_experiment_selection(
    campaign_id="selection-001",
    policy=policy,
    assessor_manifest=assessor_manifest,
    request=request,
    prediction_archive=prediction_archive,
)

committed = commit_experiment_selection_campaign(
    archive=selection_archive,
    campaign=campaign,
    committed_at=committed_at,
)
```

`load_experiment_selection_campaign` rehashes canonical campaign bytes, revalidates all nested
contracts, and mechanically rederives information audits, blockers, utilities, ranks, reasons, and
the decision. It detects archive-byte and decision/score tampering. The committed wrapper also binds
the archive receipt to a timezone-aware `committed_at >= campaign.generated_at` and exposes its own
receipt hash for the later observation/update boundary.

## Dispositions

| Campaign disposition | Meaning |
|---|---|
| `ready_selected` | at least one feasible candidate; exactly one is selected |
| `no_feasible_experiment` | every candidate is retained and infeasible; no fallback |
| `blocked_execution` | at least one required prediction archive could not be trusted; no ranking |

Candidate dispositions are `selected`, `feasible_not_selected`, and `infeasible`.

## What this slice guarantees

- every ranked candidate is tied to one exact prior and physically verified F9-S4 receipt;
- EIG is derived from complete likelihoods and every hypothetical outcome posterior is retained;
- invalid measurement, proxy risk, safety, resource, capability, and freshness failures are hard
  blockers;
- utility semantics do not change when a decoy candidate is added;
- independent assessment and selection declare no observation access or ambient tools;
- ranking, reasons, winner, and no-feasible outcome are deterministic and replayable;
- forged derived fields and corrupt archives fail closed.

## Current limitations

- All candidate likelihoods, assessments, evidence identities, budgets, capabilities, partitions,
  and replication debt in tests are synthetic.
- The assessor output is accepted as a typed, exact-bound evidence claim; underlying measurement,
  safety, cost, and custody evidence is not independently authenticated or opened here.
- Only discrete/categorical or preregistered binned likelihoods are supported; there is no numerical
  integration over continuous densities or adaptive multi-step design.
- Utility weights and hard floors are engineering policy, not empirically calibrated cross-domain
  preferences. There is no Pareto-front, imprecise-utility, value-of-sample-information, or robust
  prior/likelihood sensitivity selector yet.
- Fresh batches and replication debt are checked but not atomically reserved/consumed.
- The selector chooses one commitment only. It does not allocate portfolios, schedule jobs, execute
  interventions, validate observations, spend budget, or enforce operator authorization.
- Selection does not prove construct validity, surrogate validity, likelihood calibration, safety,
  feasibility, mechanism truth, causal effect, novelty, or SOTA.
- F9-S6 now provides the downstream validated-observation posterior, negative-result revision, and
  contradiction queue, but F9-S5 itself still does not validate or consume observations.
- F9-S7 now provides the isolated K3 scorer; atomic scheduler integration and real-domain scientific
  exit remain absent.

F9-S6 now consumes the committed selection through an independently frozen validator, keeps raw
bytes outside the updater, derives nominal and likelihood-sensitivity posteriors, and emits
append-only revisions and contradictions. See
[`VALIDATED_OBSERVATION_BELIEF_UPDATE.md`](VALIDATED_OBSERVATION_BELIEF_UPDATE.md). F9-S7 now provides
the independent full-chain scorer; see
[`INDEPENDENT_K3_ACCEPTANCE.md`](INDEPENDENT_K3_ACCEPTANCE.md). The next work is the F9
scientific-exit/integration bridge.
