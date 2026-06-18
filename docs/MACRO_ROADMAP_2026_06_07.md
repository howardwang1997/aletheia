# Macro Roadmap: Aletheia — from honest-null machine to a frontier-science scientist

> Long-term plan, 2026-06-07. The detailed executable plan for the current keystone (K1) lives in
> this same document; K2–K5 stay at keystone granularity until we reach them.

## How to read this document

The structure mirrors how we will work:

1. **North star** — the one ultimate goal.
2. **Keystones (K1…K5)** — the ultimate goal decomposed into a few large, load-bearing
   sub-goals. Each keystone is a distinct capability the system must gain; together they span the
   gap to the north star. Listed here at keystone granularity (goal · closes which gap · success
   criterion · depends-on · status).
3. **Detailed executable plan per keystone** — we expand exactly ONE keystone at a time into a
   step-level, testable plan (files, steps, acceptance, verification). Only **K1 is detailed
   now**; K2–K5 stay at keystone granularity until we reach them, then each gets its own detailed
   plan in the same shape as K1 below.

This avoids over-planning the far future while keeping every near-term step aimed at the north
star.

---

## North star

**用 AI 做最前沿的科学研究** — have AI *conduct frontier scientific research*, end to end:
pose novel, literature-grounded questions; run real experiments (mechanisms, ablations, hypothesis
tests, not just "fit a regressor"); compare to *published* SOTA; learn across a multi-experiment
program; and emit a cited paper that never claims more than its evidence.

## Where we are, honestly (2026-06-07)

- **Proven:** the anti-fakeability spine. Deterministic FSM + hard gates + honest eval +
  adversarial cross-vendor critics + harness-owned verdict. The AI-authored demonstration path ran
  end-to-end on two domains; the harness **honestly refuted every attempt**, zero faking.
- **The current ceiling:** the system is a reliable **"honest-null machine"** — it refuses to fake,
  but it cannot yet *produce* a holding novel result, because it **pre-registers thresholds blind
  (never having seen data)** and runs **single-shot** (no learning across attempts).
- **So the gap to the north star is no longer autonomy or breadth — it is scientific reach:** the
  ability to honestly *earn* a positive result and to *learn* from negative ones.

## Invariants held across ALL keystones (never relaxed to "make it pass")

The spine is load-bearing and only ever gets *stronger* as capability grows:
- The **harness owns every verdict** — an LLM never decides `holds`, `supported`, or claim strength.
- **Fail closed** on missing evidence / degraded review / unavailable baseline / starved sample.
- **Pre-registration before results**; evidence is immutable and read back from the ledger.
- **Independent cross-vendor criticism** with a vendor floor; degraded review is marked, not hidden.
- **The paper is a view over the evidence ledger**, never the source of truth.

Every keystone below must *extend* these, never weaken them. A keystone that would let the system
believe itself more easily is wrong, no matter how much capability it adds.

## How we measure progress toward the north star (not run-completion)

- **Reachability** of an honestly-grounded positive (a demonstration that holds on held-out data).
- **Information gain per campaign round** (does round N+1 use what round N learned?).
- **Grounding quality** of novelty/SOTA claims (literature health bar cleared, published SOTA row
  compared).
- **Claim coverage** of the paper (fraction of statements backed by ledger evidence).

---

## The keystones (north star, decomposed)

| # | Keystone | Closes (north-star gap) | Success criterion | Depends on | Status |
|---|----------|--------------------------|-------------------|------------|--------|
| **K1** | **Exploratory→Confirmatory demonstration** | #5 partial (real scientific process) | A true, generalizing effect can HOLD on a disjoint confirm split; the seal is airtight (5th anti-fakeability guard) | — | **DONE ✓** (live e2e `4c97becc`: first holding + reproduced result on a disjoint confirm split) |
| **K2** | **Campaign learning loop** | #5 (research programs, not one-shot) | Round N+1's hypothesis/design is provably shaped by round N's confirm/refute *reason*, not a fresh blind guess | K1 | **BUILT (offline-green) ↓** S1–S6 all landed: reasoned trajectory + belief credences + measured EIG + weak-prior strength cap + durable persistence. Live multi-round e2e (outside the Claude process) still owed |
| **K3** | **Knowledge-grounded ideation + novelty/SOTA health** | #1, #2, #6 (partial) | Novelty/SOTA claims gated on a structured-literature quality bar + a published-SOTA row comparison; "not found" ≠ novel | — (parallel to K1/K2) | keystone only |
| **K4** | **Real-experiment repertoire beyond fit-a-regressor** | #3, #4 (mechanisms, ablations, general methods, multi-domain, scientific computing) | The system runs ablations / mechanism tests / hypothesis tests with frontier methods across ≥3 domains, not one sklearn benchmark | K1 (verdict seal), K3 (grounding) | keystone only (likely splits into sub-keystones) |
| **K5** | **Scientific output: paper as a generated evidence-bundle view** | #6 (cited, faithful paper) | The paper renders strictly from the claim→evidence ledger with citations; prose can never outrun evidence | K1–K4 feed it | keystone only |

Sequencing rationale: **K1 first** — it is the prerequisite for any honestly-earned positive, so
K2 (learning from outcomes) and K4 (richer experiments) are only worth building once a result can
legitimately hold. **K3 can proceed in parallel** (it touches the front-end: ideation + literature
+ novelty), and feeds K4. **K5 is last**, as it is a faithful view over everything K1–K4 produce.

## Organizing lens: the world model (it proposes & predicts; the spine disposes)

A unifying frame for the keystones, recorded so it isn't lost. It **changes no sequence and nothing
in K1** — it sharpens K2 and K4 when we detail them. Aletheia is **two coupled world models under
one discipline** (the world-model idea = *understand state, predict causality + state changes*):

- **Epistemic world model (≈ K2)** — a model of the *research process*. **State** = the belief
  ledger (claims as calibrated credences, e.g. `Beta(α,β)`; literature sets the prior, K3).
  **Transition** = an experiment's predicted *outcome distribution* (P(holds) / refuted /
  not_evaluated). **Planning** = choose the next experiment by **expected information gain**
  (entropy reduction of the belief state) per unit cost — naturally favors discriminating
  experiments and informative negatives. `_campaign_step`'s current EIG heuristic is the proto
  version; K2 makes it explicit forward-prediction + Bayesian update.
- **Domain world model (≈ K4)** — a model of the *phenomena*. **State** = a structured/causal
  representation (entities, mechanisms, constraints), not a flat feature matrix. It predicts
  **interventions / counterfactuals**. K1's test/control is already the atomic counterfactual
  (control = `do(remove cause)` → the effect should vanish); ablations + mechanism tests extend it
  along a causal DAG.
- **The grounding discipline (= the safety spine, non-negotiable)** — the world model only
  *proposes and predicts*; **only harness-verified, held-out (confirm-split) experiments may
  update it**. Prediction − observation = the learning/surprise signal *and* a calibration metric
  (a north-star progress measure). A learned model **never** sets a claim's verdict. This is the
  spine restated in world-model terms: don't let dreamed rollouts substitute for real transitions.

Caveats to honor when K2/K4 are detailed: calibration needs data (early credences are weak priors,
say so); don't over-claim "causal" from a single control (state the identification assumptions);
keep the one-keystone-at-a-time discipline (don't build either world model before K1 makes a
verdict honestly earnable).

---

# K1 — Exploratory→Confirmatory demonstration  (DETAILED EXECUTABLE PLAN)

## Context

Why this keystone, why first: the spine is proven, but every real run comes back null for a
structural reason the code makes explicit:

1. **Blind authoring.** `_demonstration_code` (`driver.py:1050-1126`) hands the AI only the
   hypothesis + demo spec + the data-spec *schema*. The AI authors `compute_demonstration` **and
   pre-registers a quantitative threshold** (e.g. `supported_if ≥ 0.3`) having **never seen a
   single row**. It is pre-registering a guess. Real science is the reverse: do exploratory
   analysis to confirm an effect is even there, *then* pre-register a confirmatory test.
2. **One indivisible data partition.** `_compute_ai_authored_demonstration` (`base.py:318-393`)
   loads all data, featurizes once, and passes the **entire** `(X, y, groups)` to the sandbox.
   There is no explore/confirm separation.

K1 makes a holding result *reachable* — **without weakening the spine**; it adds the spine's **5th
guard**. The AI calibrates on an **exploration** partition it may probe, pre-registers a
data-grounded threshold, and the harness confirms on a **disjoint confirmation** partition the AI's
code never touched at calibration time. `holds` stays 100% harness-owned. It does NOT force
`holds=True`; even when still refuted, the result is far more informative (we learn *which* failure:
effect didn't generalize / threshold too strong / control not silent / sample too small).

This supersedes the prior plan (Part A hardening + Part B materials e2e), complete and committed
(`646050e`, verified live in run `9f852961`).

## The two orthogonal partitions (the core idea)

A demonstration now carries **two independent splits** that must not be confused:

- **explore / confirm** (NEW) — the *anti-p-hacking* seal. The AI may look at *explore*; the
  pre-registered threshold is tested on *confirm*, which its authoring never saw. Defends against a
  threshold tuned to noise.
- **test / control** (EXISTING) — the *anti-degenerate-effect* guard. Inside the partition it runs
  on, the demonstration computes a TEST statistic and a CONTROL statistic that must vanish.
  Unchanged.

A demonstration is thus a **4-way** split (explore/confirm × test/control). `holds` is still
`test_triggers AND control_silent AND probes_clean`, computed **only on confirm**, against a prereg
**calibrated on explore**.

## The seal (the new, 5th anti-fakeability invariant)

Letting the AI see *any* data before prereg is the door p-hacking walks through, so the seal is
deterministic:
1. **Harness owns the partition.** explore/confirm is a pure function of `(groups, seed)` —
   group-level disjoint, recorded for audit. The AI never chooses it.
2. **Explore cannot decide `holds`.** The exploration step returns *descriptive* numbers only; the
   static gate **forbids verdict fields** (`holds`, `test_statistic`, `control_statistic`,
   `supported_if`, …). Its output calibrates the prereg and is **never** an input to the verdict.
3. **Prereg-before-confirm ordering.** Confirmation arrays are staged **only after** the prereg is
   committed immutably and read back — extend the `_read_committed_preregistration` gate
   (`driver.py:1020-1036`) so confirm-compute is physically gated on it.
4. **Confirm is withheld during authoring.** Exploration code is staged with the *explore* arrays
   only.
5. **Threshold consistency checked DETERMINISTICALLY first, LLM second.** A harness function fails
   closed when the prereg is inconsistent with the exploration observations: `supported_if`
   threshold outside the plausible range of the explore-observed test statistic; `control_silent_if`
   already violated by the explore control; threshold *trivially easy*; threshold *doom-to-zero*.
   The author-excluded cross-vendor auditor (`_audit_demonstration`) ALSO sees both code blocks +
   observations as a second, softer layer — but the LLM is **never the only guard**.

## Implementation

### S1. Harness-owned explore/confirm partitioner (with feasibility check)
File: `aletheia/domains/base.py` (new static helper near `_demonstration_probes`).
`_split_explore_confirm(groups, n, seed, explore_frac=0.5) -> {explore_idx, confirm_idx, meta} | None`.
Group-aware deterministic split (hash each distinct group by `(seed, group)`; no group spans both
sides; fall back to a seeded row split when `groups is None`). **Fail closed:** both partitions
clear `demonstration_min_samples`, AND confirm leaves room for a test/control split (≥ `2 ×
min_samples`); too small → return `None` (→ `not_evaluated`, never a starved split). Emit `meta` =
`{seed, explore_frac, n_explore, n_confirm, split_algo_version, group_disjoint, index_hash}`.

### S2. Exploration probe contract + sandbox call
Files: `aletheia/coder/demonstration.py`, `aletheia/coder/demonstration_runner.py`.
Add `explore_demonstration(X, y, groups, meta) -> {observations: {<name>: float}, detail, n}` with a
static gate that **rejects verdict-shaped keys**. Add `run_authored_exploration(...)` reusing the
existing `_RUNNER_SCRIPT` staging (factor shared staging into one helper). Add `exploration_prompt`
+ `CANNED_EXPLORATION` for dry-run.

### S3. Three-phase authoring in the driver
File: `aletheia/scheduler/driver.py` — `_demonstration_code` (1050-1126). Explicit phases:
1. **Split** (S1); `None` → `exploration_missing=true`, blind fallback (degradation rule below).
2. **Author the exploration probe** (worker + static gate) — *before any data is staged*.
3. **Run it on the explore arrays only**; publish a `demonstration_exploration` event.
4. **Author the confirmation demonstration + prereg**, feeding observations back via an
   `exploration:` block in `demonstration_prompt`.
5. **Deterministic threshold-consistency gate** (seal #5) — inconsistent → no commit, fall back.
6. Static gate + smoke-test, then **commit prereg immutably** (1114-1118) — the ordering barrier;
   confirm arrays are NOT staged until this returns.
7. Stash the **confirm index + split meta + seed** as evidence (see S4), not only on `design`.

**Degradation rule (closes the bypass):** {S1 infeasible, exploration worker degraded, exploration
sandbox error} → `exploration_missing=true`, run the blind path for backward compat, **but the
formulation claim is capped below `strong`** (S5). A run can never reach a strong AI-authored
formulation while bypassing the seal.

### S4. Confirm-only compute + split metadata as evidence
File: `aletheia/domains/base.py` — `_compute_ai_authored_demonstration` (318-393).
With a `confirm_index`, subset `(X, y, groups)` to **confirm** before `run_authored_demonstration`;
the AI's own test/control split happens *inside* confirm. Without it (registered caps / fallback),
behavior is **exactly as today** (backward compatible). The probe min-sample floor applies to
confirm's test/control sides. **Persist split `meta` to the formulation claim's evidence** so
audit/reproduction can confirm the *same* seal was used (the partition is evidence, like the prereg).

### S5. Claim-strength / reproduction / write-up rules (close the soft bypasses)
File: `aletheia/scheduler/driver.py` — `_claim_strength` (166-231), `_finalize_claims`, `_reproduce`,
`_audit_demonstration` (2228-2329).
- Thread `exploration_missing`/degraded-split into `_claim_strength` (like `audit_error`): no seal →
  formulation ≤ `moderate`, never `strong`.
- Audit: deterministic consistency check (seal #5) first → refutation on the existing fail-closed
  path; LLM auditor second.
- Reproduction: missing confirm statistic or mismatched split meta → **not** `reproduced` (extend
  the decomposed `verdict_stable`/`statistic_stable`).
- Write-up unchanged: rendered from the ledger, so prose can't outrun status/strength.

### S6. e2e summary surfacing
File: `scripts/_e2e_common.py` — `write_e2e_summary`. Add an `exploration` block (observations,
n_explore/n_confirm, seed, split_algo_version, group_disjoint, index_hash) + the
`exploration_missing` flag, next to `demonstration`/`reproduction`.

## Acceptance criteria (K1 "done right")
1. Exploration code returns descriptive observations only; gate rejects verdict-shaped keys.
2. Prereg committed **before** confirm arrays staged (barrier in code, not convention).
3. Confirm compute sees only the confirm subset; exploration never sees confirm rows.
4. Split metadata (seed, sizes, group-disjoint proof, index hash, algo version) recorded to claim
   evidence **and** artifact summary.
5. Missing/degraded seal → formulation cannot be `strong`.
6. Confirmation missing the confirm statistic (or mismatched split meta) is **not** `reproduced`.
7. Write-up renders strictly from the ledger.
8. Deterministic threshold-consistency check fails closed on out-of-range / trivially-easy /
   doom-to-zero / control-already-violated thresholds — independent of the LLM.

## Tests (offline / mocked — safe in-session)
- `tests/test_domain_base_split.py` (new): deterministic, group-disjoint, seed-varying, row-split
  fallback, **returns None when too small for a 4-way split**, stable split meta + index hash.
- `tests/test_demonstration_runner.py` (new/extend): `run_authored_exploration` returns descriptive
  stats; gate rejects a probe smuggling a `holds`/`test_statistic` key.
- `tests/test_paradigm_p5.py` (extend): confirm-only compute on the confirm subset; starved
  partition fails the floor; **calibrated-on-explore + holds-on-confirm → `holds True`**; overfit
  threshold (holds on explore, not confirm) → refuted; deterministic consistency rejects
  out-of-range/trivial/doom; `exploration_missing` caps strength; reproduction without confirm
  statistic is not `reproduced`.
- `tests/test_paradigm_p5_materials.py` (extend): the same two-partition path on materials.

## Verification
- Backend (env `aletheia`): `conda run -n aletheia env PYTHONPATH=. python -m pytest -q` — current
  292-pass baseline + new tests green. Targeted: `... pytest tests/test_domain_base_split.py
  tests/test_demonstration_runner.py tests/test_paradigm_p5.py tests/test_paradigm_p5_materials.py`.
- **Live e2e handed to the user, OUTSIDE this Claude Code session** (in-process trips the AUP
  classifier — `docs/CLAUDE_CODE_AUP_FALSE_POSITIVE_NOTES_2026_06_04.md`):
  `conda run -n aletheia PYTHONPATH=. python scripts/real_ai_demonstration_e2e_materials.py`.
  Success criterion is **not** `holds=True` — it is: the summary shows an `exploration` block, a
  calibrated prereg, confirm-partition compute, the split meta, and a coherent verdict. **Never
  patch the harness to force `holds=True`.**

---

# K2 — Campaign learning loop  (DETAILED EXECUTABLE PLAN)

> Detailed 2026-06-08, after K1 went live (run `4c97becc`). K1 made a holding result *reachable*;
> K2 makes the campaign *learn* across rounds. This IS the **epistemic world model** (see
> *Organizing lens*).

## Context — the loop exists, but it does not yet *learn*

The multi-round campaign loop is already in the code (`driver.py:1864-1916`: one Run → up to
`max_experiments_per_campaign=3` linked experiments; `_campaign_step:2140-2232` picks the next).
But it is not yet a *learning* loop, for three concrete reasons the code makes explicit:

1. **EIG is a vibe, not a measurement.** `_campaign_step` asks the LLM to "estimate each candidate's
   `expected_information_gain` (0..1) honestly" (`driver.py:2168`). There is no belief state, so
   "information gain" reduces nothing and is unfalsifiable — yet the deterministic selection
   (`:2190`) trusts that number against the `campaign_min_eig=0.3` floor.
2. **The next round is blind to WHY the last one held/failed.** The trajectory handed to the planner
   (`:2150-2154`) is only `'<hypothesis>' -> <headline_metric> <value> [model], verdict
   <approve/reject>`. It carries a metric + an approve/reject token but NOT the structured *reason*
   the demonstration confirmed or refuted (didn't generalize to confirm / threshold too strong /
   control not silent / sample starved / audit-refuted / scope over-claim). So round N+1 is
   "informed" only by a number + a token — still essentially a fresh guess. **This is exactly the
   milestone-run gap** (`4c97becc`): the AI demonstrated 1 of 4 claimed pillars and was rejected for
   over-claim, and nothing fed "narrow the claim, or demonstrate pillars 2-4" into a next round.
3. **No credence, no forward prediction, no Bayesian update.** Claims are discrete `status`+`strength`
   strings (`memory/ledger.py` `Claim`). There is no calibrated belief to update, so the north-star
   metric "information gain per campaign round" is unmeasurable today.

K2 closes this by making the campaign loop an **epistemic world model**: a belief-state ledger of
calibrated credences, a forward outcome predictor, a Bayesian update fired ONLY by the
harness-verified confirm-split verdict, and an EIG that is *measured* entropy reduction — plus a
deterministic **outcome-reason classifier** that feeds round N+1.

## Invariants extended (never relaxed — the world model proposes; the harness disposes)

- **The belief state is a planning aid, never a verdict.** It NEVER sets `holds`, `supported`, or
  claim strength — those stay 100% harness-owned (the seal restated). A credence is updated **only**
  by a harness-verified, held-out (confirm-split) outcome — never by a dreamed rollout, never by an
  LLM's say-so.
- **Fail closed.** No belief state / starved history / degraded round → fall back to today's
  behavior; the credence is marked low-confidence (weak prior), never silently treated as strong.
- **Calibration honesty.** Early credences ARE weak priors — surface them as such; never present an
  uncalibrated credence as a hard probability. Don't claim "causal" from a single control.
- **Pre-registration / immutability.** The forward *prediction* is committed BEFORE the experiment
  runs, so prediction−outcome surprise cannot be back-fitted. The prediction is evidence, like the
  prereg.

## Core idea — two new objects + one new signal

1. **Belief ledger** — each claim (or research question) carries a calibrated credence `Beta(α,β)`.
   The literature/scorecard sets a *weak* prior; the harness verdict on the confirm split is the
   only thing that updates it.
2. **Outcome-reason classifier** (deterministic) — from the demonstration result + audit +
   reproduction + results-gate, classify the round into a typed reason taxonomy: `generalized` /
   `did_not_generalize` / `threshold_too_strong` / `control_not_silent` / `sample_starved` /
   `audit_refuted` / `scope_overclaim` / `confound` / `infra_degraded`. A pure function over the
   existing result dicts, no LLM.
3. **Measured EIG** — the planner's candidate EIG becomes a *checkable* quantity: the expected
   entropy reduction of the belief ledger under the candidate's pre-registered predicted outcome
   distribution. The LLM may still propose, but the harness recomputes EIG from the belief state;
   the LLM's number can only *lose* to the measured one (fail-closed toward the measured value).

## Implementation

### S1. Belief-ledger primitive (storage + update math)
New `aletheia/memory/belief.py` (+ a `ClaimCredence` table in `ledger.py`, or a JSON column on
`Claim`). `Credence = Beta(alpha, beta)`; helpers: `prior_from_scorecard(scores) -> Beta` (weak:
α+β ≈ 2-4), `update(beta, harness_verdict) -> Beta` (+1 to α on a harness-confirmed hold, +1 to β
on a harness-confirmed refute; NO update on `not_evaluated`/degraded), `entropy(beta)`,
`mean(beta)`, `is_weak_prior(beta) -> bool`. `update()` accepts ONLY a harness verdict object
(`holds ∈ {True, False}` from a confirm-split demonstration); it no-ops on `None`.

### S2. Outcome-reason classifier (deterministic)  ← BUILDING NOW
New `aletheia/scheduler/outcome.py` — `classify_outcome(demo, reproduction, audit, rpanel,
split_meta) -> {reason, narrowing_hint, recoverable}`. Pure function over the existing result dicts
(no new I/O). E.g. `holds=False` + high `control_statistic` → `control_not_silent`; `holds=True` +
audit scope flag / gate reject on over-claim → `scope_overclaim`; `n_confirm < floor` →
`sample_starved`; held on explore but not confirm → `did_not_generalize`. Wire into
`_run_experiment`'s return (`:2105-2119`): add `outcome["reason"/"narrowing_hint"/"recoverable"]`.

### S3. Feed the reason into the next round (the actual learning)  ← BUILDING NOW
`driver.py` `_campaign_step` (`:2150-2173`). Replace the bare trajectory with a **reasoned
trajectory**: each line gains `reason` + `narrowing_hint` (+ the round's claim credence mean once
S1 lands). The prompt gains an explicit instruction: a `scope_overclaim` reason MUST spawn a
candidate that narrows the claim or demonstrates an as-yet-unshown pillar; a `did_not_generalize`
MUST change the effect/feature, not just re-tune the threshold; a `sample_starved` MUST scale n.
This is the seam that makes round N+1 *provably* shaped by round N's reason.

### S4. Forward prediction + measured EIG (pre-registered)
Before a round runs, commit a **predicted outcome distribution** for its hypothesis (`P(holds)` from
the candidate claim's credence) as an immutable pre-registration event. After the harness verdict,
compute the **realized surprise** = `|predicted_P(holds) − outcome|` and the **realized entropy
reduction**; emit a `belief_update` event. In `_campaign_step`, recompute each candidate's EIG as
`expected_entropy_reduction(belief, candidate.predicted_outcome_dist)`; the selection floor
(`:2186-2192`) uses the MEASURED EIG; the LLM's self-reported number is a tiebreak only and can
never raise a candidate above its measured EIG. Convergence = measured EIG below floor.

### S5. Claim-strength / calibration-honesty wiring
`_claim_strength` / `_finalize_claims`: a claim whose credence is still a weak prior (S1
`is_weak_prior`) cannot be `strong` (mirrors the K1 `exploration_missing` cap); surface the credence
+ weak-prior flag in the claim evidence. `_campaign_synthesis` (`:2234+`) reports the **belief
trajectory** (credence per round) and the **calibration** (mean `|predicted−realized|` surprise) as
the north-star progress metric, honestly labeled "weak/early."

### S6. Persistence + e2e summary surfacing
`ledger.py` (credence storage); `scripts/_e2e_common.py` `write_e2e_summary` gains a `belief` block
(per-round reason, credence trajectory, measured EIG, predicted-vs-realized surprise / calibration).
New events: `belief_prior`, `belief_update`, `campaign_reason`.

## Acceptance criteria (K2 "done right")
1. Round N+1's planner input provably contains round N's structured reason + narrowing hint (not
   just a metric + verdict token).
2. A `scope_overclaim` outcome (the milestone case) deterministically produces a next-round
   candidate that narrows the claim or demonstrates an unshown pillar.
3. A credence is updated ONLY by a harness-verified confirm-split verdict; `not_evaluated`/degraded
   never moves it.
4. EIG used for selection is *measured* entropy reduction of the belief ledger; the LLM's
   self-reported EIG can never raise a candidate above its measured value.
5. A forward outcome prediction is committed BEFORE the round runs; predicted−realized surprise is
   recorded (calibration metric).
6. A weak-prior credence cannot yield a `strong` claim; the synthesis labels early credences as weak.
7. Fail-closed: no belief state / starved history / degraded round → today's behavior, credence
   marked low-confidence.
8. The world model never sets a verdict — `holds`/`supported`/strength stay harness-owned.

## Tests (offline / mocked — safe in-session)
- new `tests/test_belief.py` (S1 math), `tests/test_outcome_classifier.py` (S2 taxonomy);
- extend `tests/test_paradigm_p5.py` (S3 reasoned trajectory + S4 measured EIG + S5 strength cap)
  and `tests/test_paradigm_p5_materials.py` (the `scope_overclaim`→narrowing path on materials).

## Verification
- Backend (env `aletheia`): full suite green + new tests.
- Live e2e OUTSIDE the Claude process (in-process trips the AUP classifier). Success = a ≥2-round
  campaign where round 2's hypothesis is visibly shaped by round 1's reason (e.g. round 1
  over-claims → round 2 narrows), the belief trajectory + calibration appear in the summary, and the
  spine's verdicts stay harness-owned. Success is NOT "holds=True every round."

## Build order
The smallest honest increment that delivers the K2 thesis is **S2 + S3** (classify the reason + feed
it forward) — that alone makes the loop *learn*. S1 / S4 / S5 then build the world-model rigor
(credences, measured EIG, calibration) on top. Order: **S2+S3 → S1 → S4 → S5 → S6.**

**Status (2026-06-09): ALL of S1–S6 built and offline-green.** S2+S3 (reasoned trajectory) +
S1 (`aletheia/memory/belief.py` credence primitive + driver seeding) + S4 (forward `belief_prediction`
+ harness-measured EIG `min(llm, measured)` + `belief_update` surprise) + S5 (weak-prior caps a
formulation claim at `moderate` + belief trajectory/calibration in the synthesis) + S6 (`belief_states`
table + `upsert/get/list_credences` + the e2e summary `belief` block). New tests: `tests/test_belief.py`
plus extensions in `tests/test_paradigm_p5.py` / `test_paradigm_p3.py` / `test_campaign.py`. The
detailed plan + open decisions live in `docs/K2_S1_S4_S6_DETAILED_PLAN_2026_06_09.md`. **Still owed:**
a live multi-round campaign e2e run OUTSIDE the Claude process (in-process trips the AUP classifier),
showing the belief trajectory + calibration with every verdict harness-owned.

---

# K3–K5 — keystone granularity (to be detailed when reached)

Each becomes its own K1-shaped detailed plan (Context · core idea · invariants extended ·
step-level Implementation · Acceptance · Tests · Verification) when we start it.

> **K2 — Campaign learning loop** is now DETAILED above (see the K2 detailed executable plan).

### K3 — Knowledge-grounded ideation + novelty/SOTA health bar
Move ideation from "plan-handed" toward literature-grounded hypothesis generation; build structured
literature memory (methods/datasets/metrics/results/limitations/gaps), and gate novelty/SOTA claims
on a quality bar + a published-SOTA row comparison ("not found" ≠ novel). Build on `sota_rows`,
`research/literature.py`. Closes north-star #1/#2/#6(partial); gap-analysis #6. **Parallel to
K1/K2; feeds K4.**

### K4 — Real-experiment repertoire beyond fit-a-regressor
Add ablations, mechanism tests, and hypothesis tests with frontier methods, across ≥3 domains and
into real scientific computing (beyond sklearn). Likely **splits into sub-keystones** (ablation
harness · mechanism/counterfactual tests · a 3rd domain · non-sklearn compute backend). Closes
north-star #3/#4. **Depends on K1 (verdict seal) and K3 (grounding).**
→ **This IS the domain world model** (see *Organizing lens*): a structured/causal state (mechanisms,
constraints) that predicts interventions/counterfactuals; K1's test/control is its atomic
counterfactual, ablations/mechanism tests extend it along a causal DAG.

### K5 — Scientific output: paper as a generated evidence-bundle view
Render the paper strictly from the claim→evidence ledger with citations and limitations; enforce
that every statement maps to evidence so prose can never outrun the bundle. Closes north-star #6;
gap-analysis #7. **Consumes K1–K4.**

## Working method
Decompose the ultimate goal into keystones (above), then expand **one keystone at a time** into a
detailed executable plan. K1 is detailed and ready to build; K2–K5 are expanded only when we reach
them, each in the K1 shape.
