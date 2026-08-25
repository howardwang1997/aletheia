# ADR 0020: Observation-blind constrained experiment selection

- Status: Accepted
- Date: 2026-08-15
- Scope: F9-S5 / Competitive Causal World Model

## Context

F9-S4 can freeze several experiment protocols and calibrated likelihoods before any target result is
available. It does not decide which experiment should consume scarce data, time, equipment, money,
or risk allowance. Selecting the largest predicted effect is not enough: rival mechanisms can make
similar predictions, and a highly separated proxy can be cheap to game while failing to measure the
construct named by the causal question.

Expected entropy reduction is a classical Bayesian experiment-design objective. Lindley defines
information supplied by an experiment through the expected change in uncertainty:
[Lindley, 1956](https://projecteuclid.org/journals/annals-of-mathematical-statistics/volume-27/issue-4/On-a-Measure-of-the-Information-Provided-by-an-Experiment/10.1214/aoms/1177728069.full).
Chaloner and Verdinelli review Bayesian design as expected-utility optimization rather than a single
universal score:
[Chaloner and Verdinelli, 1995](https://projecteuclid.org/journals/statistical-science/volume-10/issue-3/Bayesian-Experimental-Design-A-Review/10.1214/ss/1177009939.full).
Box and Hill apply expected entropy change to sequential discrimination among rival mechanistic
models and emphasize prediction separation relative to error:
[Box and Hill, 1967](https://www.stat.cmu.edu/technometrics/59-69/VOL-09-01/v0901057.pdf).

Experiment choice is also multi-objective. Cost, time, and knowledge gain have explicit trade-offs in
decision-theoretic design:
[Farrow and Goldstein, 2006](https://doi.org/10.1016/j.jspi.2004.07.008).
Measurement quality cannot be inferred from information gain. Construct validity requires evidence
connecting an operational measurement to its intended interpretation:
[Cronbach and Meehl, 1955](https://pubmed.ncbi.nlm.nih.gov/13245896/).
A surrogate endpoint needs its own validation rather than merely correlation or convenience:
[Prentice, 1989](https://onlinelibrary.wiley.com/doi/pdf/10.1002/sim.4780080407).

These ideas leave concrete implementation threats:

- ranking an unarchived, changed, ordinal, or otherwise non-EIG-eligible prediction;
- computing EIG from a caller-supplied prior rather than the exact F9-S1 belief state;
- omitting an outcome with low probability or retaining only the winning posterior;
- allowing high EIG to compensate for invalid measurement, prohibited risk, or missing capability;
- normalizing cost or utility against the current candidate set so adding a decoy changes the winner;
- calling a calibration split or already targeted partition “fresh confirmation”;
- treating replication as an aspiration without a frozen independent protocol;
- letting the prediction author self-assess feasibility and measurement validity;
- giving the assessor ambient tools or target-observation access;
- silently falling back to an infeasible experiment;
- discarding why non-selected candidates lost; or
- accepting a forged score, rank, decision, or damaged archive.

## Decision

### Compare exact archived commitments under one world state

`ExperimentSelectionRequest` contains at least two canonical `ExperimentCandidate` objects. Every
candidate embeds a committed F9-S4 campaign and must share the exact F9-S2 source campaign, F9-S1
world-model snapshot, belief state, and research question. Duplicate substantive commitments and
duplicate experiment namespaces are invalid.

Before any scoring, `run_experiment_selection` physically reads and rehashes every F9-S4 campaign
from the declared content-addressed archive. The embedded wrapper must equal the loaded campaign.
One missing, corrupt, noncanonical, or rebound archive object blocks the whole selection and emits a
hash-only failure; no partial ranking is trusted.

### Derive discrete EIG and retain the complete posterior ledger

Only an F9-S4 campaign with `ready + probabilistic + eig_eligible=true` receives an information
audit. For candidate experiment \(e\), exact F9-S1 prior \(p(h)\), frozen F9-S4 likelihood
\(p(y\mid h,e)\), and every preregistered outcome bin \(y\), the harness computes:

\[
p(y\mid e)=\sum_h p(h)p(y\mid h,e)
\]

\[
p(h\mid y,e)=\frac{p(h)p(y\mid h,e)}{p(y\mid e)}
\]

\[
\operatorname{EIG}(e)=H[p(h)]-\sum_y p(y\mid e)H[p(h\mid y,e)].
\]

The audit retains prior entropy, every outcome marginal, every complete hypothetical posterior,
every posterior entropy, expected posterior entropy, absolute EIG, prior-entropy-normalized EIG, and
minimum/maximum pairwise total-variation distance. The selector does not receive a realized outcome
and these hypothetical posteriors are not belief updates.

### Apply hard gates before utility

The following cannot be compensated by information or by any weighted benefit:

- F9-S4 campaign is not ready, probabilistic, and EIG-eligible;
- EIG ratio or minimum pairwise total variation is below policy;
- cost exceeds budget or uses another currency;
- duration exceeds the fixed limit;
- risk exceeds policy or is prohibited;
- measurement is not independently assessed as validated or misses its confidence floor;
- any bounded or invalid proxy/surrogate risk remains;
- a required capability lacks evidence of availability;
- too few distinct, unused, unexpired, pre-assessed confirmation partitions are reserved; or
- a claimed fresh partition reuses calibration or target-experiment identity.

Every blocker is retained in canonical machine-readable form. An ordinal prediction can remain a
useful F9-S4 qualitative artifact, but it is infeasible for this probability-based selector.

### Rank feasible candidates with a fixed multi-attribute utility

`ExperimentSelectionPolicy` freezes a weight vector summing to one. Feasible candidates receive:

```text
+ weight_eig         * normalized EIG
+ weight_tv          * minimum pairwise total variation
+ weight_fresh       * confirmation availability up to a fixed saturation count
+ weight_replication * proportional replication-debt reduction
- weight_cost        * cost / fixed budget
- weight_duration    * duration / fixed limit
- weight_risk        * fixed risk burden
```

All normalizers and risk burdens are policy-fixed rather than inferred from the candidate set. A
new decoy therefore cannot rescale another candidate's utility. Replication-debt reduction is
credited only when an independent frozen replication protocol exists and cannot exceed recorded
debt.

Ordering is deterministic: feasibility, constrained utility, absolute EIG, cost ratio, duration
ratio, then canonical candidate ID. Exactly one feasible candidate is selected. Other feasible
candidates retain `lower_constrained_utility`; infeasible candidates retain all blockers. If none is
feasible, disposition is `no_feasible_experiment` with no selected candidate and no fallback.

### Separate assessment from proposal and observation

`ExperimentAssessmentManifest` freezes adapter/parser/schema/principal identities, prohibits ambient
tools, and declares `observation_access="none"`. Its principal must differ from prediction,
calibration, causal-author/reviewer, and hypothesis-generator/reviewer principals across every
candidate; model-backed roles also require a distinct model identity.

The assessment exact-binds prediction campaign and commitment, experiment protocol, causal
measurement process and error model, assessor, evidence hashes, budget/time/risk, capabilities,
fresh partitions, and replication debt. It must be complete before the selection request.

### Re-derive and archive the decision

`ExperimentSelectionCampaign` model validation recomputes every information audit, hard blocker,
utility component, rank, reason, winner, and disposition. A changed score or decision is invalid.
The full campaign is committed as canonical content-addressed JSON. Replay rehashes archive bytes and
reruns nested validation and decision derivation. The commitment wrapper binds a timezone-aware
commit time no earlier than campaign generation to the archive receipt, so a later observation gate
can require selection commitment before execution/observation.

## Consequences

- Experiment selection now has an exact, inspectable connection to one prior and a set of immutable
  pre-observation likelihoods.
- A high-information invalid proxy, unsafe experiment, or unavailable action cannot win by score.
- Candidate addition cannot change existing utilities through relative min/max normalization.
- Operators can distinguish “lower utility” from scientific, safety, resource, and custody blockers.
- No feasible candidate is an allowed and explicit scientific outcome.
- Fresh confirmation and replication debt influence selection without being confused with EIG.
- Assessment evidence collection is more expensive and remains a trusted input boundary.
- This slice selects a protocol artifact; it does not schedule, execute, validate, or interpret an
  experiment.
- Passing selection provides no evidence that the likelihoods, measurements, mechanisms, or causal
  claims are true in a real domain.

## Rejected alternatives

- **Choose maximum EIG:** invalid measurement, prohibited risk, or impossible execution must not be
  purchasable with information.
- **Use predicted effect size:** large effects can be predicted by all hypotheses and therefore have
  little model-discrimination value.
- **Use pairwise separation alone:** it ignores current belief mass and the full outcome posterior.
- **Score ordinal predictions as probabilities:** arbitrary ranks do not define outcome marginals or
  Bayesian posteriors.
- **Normalize features over current candidates:** decoys and candidate removal would change the
  meaning of every score.
- **Convert hard gates to large penalties:** sufficiently large EIG could still select an invalid or
  unsafe experiment.
- **Trust “fresh=true”:** exact partition identities and time windows are needed to detect calibration
  reuse, target reuse, and expiration.
- **Let the proposal author assess feasibility:** this collapses independent review and creates an
  incentive to understate cost, proxy, and execution risk.
- **Return only the winner:** missing alternatives and reasons prevent audit, revision, and later
  negative-result analysis.
- **Pick the least-bad infeasible candidate:** absence of a valid experiment is information and must
  stop execution rather than trigger an implicit fallback.
