# ADR 0037: Observation-blind, shadow-only research portfolio planning

Date: 2026-08-18

## Status

Accepted for the F11-S5 engineering boundary. Production autonomous allocation, signed/IAM-backed
approval commissioning, broad fault injection, the 72-hour endurance gate, and scientific exit
remain open.

## Context

F11-S3 made Quest, Program, Campaign, scientific-family, dependency, data-role, and budget state
reconstructible. F11-S4 made task-scoped scientific memory reconstructible without deleting
negative results. Neither layer could decide what a multi-Program research system should do next.
A model could propose an attractive action, but letting that same output supply cost, expected
information gain (EIG), feasibility, and a final score would recreate self-grading at the point
where resource authority matters most.

Related primary work demonstrates useful scaling patterns without supplying this trust boundary:

- the [AI co-scientist](https://arxiv.org/abs/2502.18864) uses asynchronous generation, debate,
  evolution, and ranking to allocate inference effort across hypotheses;
- [The AI Scientist-v2](https://arxiv.org/abs/2504.08066) uses agentic tree search to pursue an
  end-to-end research direction;
- [Kosmos](https://arxiv.org/abs/2511.02824) combines long-running agents with a structured world
  model, while an [independent Kosmos evaluation](https://arxiv.org/abs/2511.13825) explicitly
  tests false-hypothesis and null settings; and
- robust submodular observation selection formalizes useful diminishing-return and diversity
  structure under uncertainty ([Krause et al., JMLR](https://jmlr.csail.mit.edu/papers/v9/krause08b.html)).

These references motivate asynchronous search, structured state, null testing, and diversity. They
do not make an auto-evaluation receipt independent truth, prove a proposed action is safe, or grant
Aletheia budget authority. F11-S5 therefore separates proposal, independent inputs, deterministic
calculation, human comparison, and any future activation decision.

## Decision

### 1. The proposer names actions and rationales, never scores

`PortfolioProposal` may contain 2–64 typed actions:

- advance a Campaign;
- run a discriminating experiment, replication, or mechanism test;
- acquire data or repair a capability;
- start a Campaign; or
- propose pausing/stopping a Program.

The schema is `extra="forbid"`. It contains no cost, probability, EIG, replication-debt, utility,
or total-score field. Stable candidate identity covers action type, target, family, task, and title;
the full action hash also covers rationale. The proposal binds the exact Quest graph hash and a
fresh Quest-scoped `portfolio-plan` memory-context receipt for its provider/model.

### 2. Independent assessment supplies frozen inputs, not a verdict

Every candidate receives exactly one independently manifested assessment. Its principal must
differ from the proposer; a model assessor must also use a different model identity. Assessments
freeze:

- per-kind costs and duration;
- risk, measurement evidence, capabilities, and data roles;
- integer-ppm priors and hypothesis/outcome likelihoods;
- importance, novelty, and success inputs with evidence hashes;
- replication-debt ledger, reduction, and independent protocol;
- correlation/diversity tags; and
- optional content-bound approval evidence.

The assessment can still be scientifically wrong. Its role separation and hashes prove which
inputs were used, not that a human, model, or evidence source was calibrated. A production approval
will require a signed/IAM-backed contract outside this slice; an approval evidence hash is not a
signature.

### 3. The harness derives all gates and scores

The frozen selector recomputes discrete EIG from ppm priors and likelihoods with `Decimal`, 60-digit
precision, and half-even conversion to integer micronats. It stores prior entropy, expected
posterior entropy, EIG, and ratio with exact integer reconciliation.

Hard blockers are derived before utility:

1. Quest/Program/Campaign state and dependency completion;
2. Program budget allocation and currently available microunits;
3. required data-role allocation;
4. missing capability identities;
5. validated measurement for experiment-like actions;
6. duration, risk, and required non-expired approval;
7. minimum EIG for information actions; and
8. replication action/debt/protocol coherence.

The base microscore combines harness EIG, independently assessed importance/novelty/success,
replication-debt reduction, and penalties for cost, duration, and risk under integer-ppm weights.
Infeasible candidates retain a score for audit, but can never enter a batch.

### 4. Batch selection is constrained and deterministic

The selector uses deterministic greedy marginal utility with a diversity bonus and stable tie
breaks. It enforces:

- total, per-Program, and per-family action caps;
- one action per target;
- per-correlation-tag caps;
- cumulative cost against each frozen Program allocation; and
- a minimum replication quota.

If a required replication quota cannot be satisfied by positive-utility feasible candidates, the
whole epoch becomes `policy_blocked` with no selected actions. Budget output is a projection only;
no `BudgetEvent`, reservation, task, graph transition, or external action is written.

This is a transparent heuristic, not a claim of global combinatorial optimality or the robust
submodular guarantees of the cited work.

### 5. Human comparison is committed before planner revelation

Each slate permits one `HumanPortfolioPlanSpec`. Its `planner_output_access` is literally `none`,
its principal differs from proposer and assessor, and PostgreSQL refuses another plan. Only after
that commit may the harness materialize an epoch. The comparison records:

- set overlap, exact match, and Jaccard ppm;
- human selections that violated hard filters;
- human batch-constraint violations; and
- planner versus human feasible base-utility sums.

This controls hindsight contamination in the software workflow. It does not prove a person avoided
all out-of-band knowledge of the algorithm.

### 6. Every epoch is shadow-only

Five append-only tables store the workflow:

| Table | Authority |
| --- | --- |
| `research_portfolio_slates` | frozen policy/proposal/assessment, graph, budget, memory receipt |
| `research_portfolio_candidates` | exact action/assessment rows within the slate |
| `research_portfolio_human_plans` | one pre-result human plan |
| `research_portfolio_epochs` | one shadow decision/comparison per slate |
| `research_portfolio_scores` | every harness-derived candidate score and rank |

Each state change is one `research_portfolio.mutation` scientific command plus result receipt and
keyed event. PostgreSQL insert guards require the parent command to be `applying`; deferred triggers
verify candidate/score completeness; all five tables reject update/delete. Epoch constraints require
`shadow_only = true` and `actions_enqueued = false`.

The audit can conclude only `eligible_for_human_activation_review`. Its
`autonomous_allocation_enabled` field is literally `false`, and no activation method exists.

### 7. Current state gates writes; frozen state preserves audit

Slate registration locks the Quest and freezes exact graph, Program budget availability, and a
fresh latest-leaf memory context. Evaluation locks again and refuses to run if any of those changed.
This prevents a score calculated on stale capacity or forgotten evidence from becoming current.

After an epoch commits, reads re-derive it solely from its frozen graph/budget/spec/human plan. A
later graph transition or memory compaction therefore cannot erase old audit evidence. Old memory
delivery receipts are audit-readable but cannot be reused as current planning context.

## Consequences

- A proposal cannot smuggle its own total score through the typed action schema.
- Cost, EIG, risk, replication debt, and diversity are visible, replayable components rather than
  opaque model preferences.
- Human/planner comparison is interpretable because the human set predates planner materialization.
- Quest locking and allocation-row locking serialize graph/budget races at a low-volume strategic
  boundary.
- Freezing complete graph JSON and candidate inputs increases storage, but allows old epochs to be
  reconstructed after current state changes.
- Independent assessment quality, likelihood calibration, and policy-weight calibration remain
  scientific/operational obligations.
- No result of this slice authorizes production allocation. Activation requires a later reviewed
  contract and evidence from shadow epochs, F11-S6 fault injection, and F11-S7 endurance.

## Rejected alternatives

### Ask one model for actions and final scores

Rejected because the proposer can optimize the explanation and score together, making a receipt a
record of self-preference rather than an independent calculation.

### Reveal the planner output before asking for a human plan

Rejected because agreement would be contaminated by anchoring and could not serve as shadow-mode
evidence.

### Reserve budget or enqueue selected work immediately

Rejected because F11-S5 has not earned action authority. It projects a batch and records receipts;
it does not mutate execution state.

### Re-score old slates against the latest graph

Rejected because historical comparisons would drift and become unreproducible. Current state gates
new evaluation; frozen state reconstructs committed history.

### Treat approval evidence hashes as production authorization

Rejected because a content hash has no signer, key custody, IAM policy, revocation proof, or
external reconciliation semantics by itself.
