# K2 remaining steps — S1 · S4 · S5 · S6 (detailed executable plan)

> **STATUS 2026-06-09: BUILT + offline-green.** All four steps landed exactly as planned below
> (binary-entropy measured EIG, normalized to `[0,1]` so a fresh/weak belief is fail-closed to
> today's behavior; weak-prior strength cap; durable `belief_states` table). Full suite: 369 passed,
> 1 skipped (the only failure is the pre-existing `test_online_download_file_and_zip` httpx fixture
> flake, unrelated). New `tests/test_belief.py` + extensions in `test_paradigm_p5.py` /
> `test_paradigm_p3.py` / `test_campaign.py`. Decisions D1–D4 were all adopted as recommended.
> **Owed:** a live multi-round campaign e2e OUTSIDE the Claude process.

> 2026-06-09. Expands the four remaining K2 sub-steps from keystone-sketch (roadmap
> `MACRO_ROADMAP_2026_06_07.md`, lines 333–409) into a step-level, testable plan in the K1 shape
> (files · steps · acceptance · verification). **S2 + S3 are DONE** (`outcome.py` classifier +
> reasoned trajectory in `_campaign_step`); this plan covers **S1 → S4 → S5 → S6**, in that
> dependency order. Scope here is the *campaign learning loop's world-model rigor*: a calibrated
> belief state, measured (not self-reported) information gain, calibration honesty, and durable
> surfacing — without ever letting the belief state touch a verdict.

## Context — what S2+S3 already give us, and the gap S1/S4–S6 close

Today (post S2+S3) the loop **learns the reason** and **acts on it**: each round emits a typed
`reason` + `narrowing_hint` (`outcome.py`), `_run_experiment` returns them
(`driver.py:2170-2189`), and `_campaign_step` (`driver.py:2238-2363`) builds a *reasoned
trajectory* + a hard "act on the last reason / pivot if not recoverable" directive. That makes the
loop **qualitatively** learn.

What is still missing is the **quantitative** world model the roadmap calls the *epistemic world
model*:

- there is **no belief state** — nothing accumulates "how much do we now believe this line holds?"
  across rounds, so a 3-round campaign that confirms the same effect twice looks identical to one
  that guessed blind each time;
- EIG is **self-reported by the LLM** (`expected_information_gain` in the candidate JSON, floored at
  `campaign_min_eig=0.3`, `driver.py:2317-2345`) — the harness never checks it, so an inflated
  number can keep a low-value campaign running;
- claim strength is **decoupled from accumulated belief** — a single round that holds+reproduces can
  reach `strong` (`driver.py:243`) even though one confirm-split hold is, honestly, a weak prior;
- none of the belief signal is **surfaced or persisted**, so a reviewer can't see the trajectory or
  the calibration.

S1 builds the belief primitive; S4 makes EIG a measured quantity gated by it; S5 ties claim
strength + the synthesis to it honestly; S6 persists and surfaces it.

## Core idea — two objects + one signal (restated concretely)

1. **Belief state** — per *open-question lineage* within a Run, a calibrated credence `Beta(α,β)`.
   The scorecard sets a deliberately **weak** prior (α+β ≈ 2); the **only** thing that moves it is a
   harness-verified confirm-split verdict (+1 α on a confirmed hold, +1 β on an evaluated refute).
2. **Outcome-reason classifier** — DONE (S2, `outcome.py`).
3. **Measured EIG** — a candidate's information gain becomes the **expected binary-entropy reduction
   of its open-question credence under one harness update**, recomputed by the harness from the
   belief state. The LLM's self-reported EIG can only *lose* to the measured one
   (`effective = min(llm, measured)`), and selection/convergence run on the measured value.

The belief over a claim is `p = mean(Beta) = α/(α+β) ∈ (0,1)`; its uncertainty is the **binary
entropy** `H(p) = −p·log₂p − (1−p)·log₂(1−p)` (dependency-free, no digamma/scipy). EIG of running an
experiment = `H(p) − E[H(posterior)]` where the posterior is the Beta update under each of {holds,
refuted} weighted by the predicted outcome distribution (itself `{holds: p, refuted: 1−p}`).

## Invariants extended (never relaxed — the world model proposes; the harness disposes)

Carried verbatim from the roadmap (lines 302–314) and binding on every step below:

- **The belief state is a planning aid, never a verdict.** It NEVER sets `holds`/`supported`/strength
  — those stay 100% harness-owned. A credence updates ONLY on a harness-verified, held-out
  confirm-split outcome; never on `not_evaluated`/degraded/an LLM say-so/a dreamed rollout.
- **Fail closed.** No belief state / starved history / degraded round → fall back to today's exact
  behavior; mark the credence low-confidence (weak prior), never silently strong.
- **Calibration honesty.** Early credences ARE weak priors — surface them as such; never present an
  uncalibrated credence as a hard probability.
- **Pre-registration / immutability.** The forward *prediction* is committed BEFORE the round runs
  (it is evidence, like the prereg), so predicted−realized surprise can't be back-fitted.

A step that would let the system believe itself more easily is wrong no matter what capability it
adds.

---

## S1 — Belief-ledger primitive (storage math + in-memory wiring)

**Goal:** a pure, unit-testable credence math module + an in-memory belief state on the driver,
seeded as a weak prior and updated only by harness verdicts. **Durable table is deferred to S6** —
S1 persists nothing but emits a `belief_prior` event (the append-only audit trail S6 reads).

### Files
- **NEW** `aletheia/memory/belief.py` — pure math, **no I/O, no LLM** (mirrors `outcome.py`'s purity).
- `aletheia/scheduler/driver.py` — in-memory belief state + prior seeding + `belief_prior` event.
- **NEW** `tests/test_belief.py`.

### Steps
1. `belief.py`: define an immutable `Credence` (a frozen dataclass `(alpha: float, beta: float,
   n_updates: int = 0)`) and a module constant `WEAK_PRIOR_MAX_MASS = 4.0`. Helpers:
   - `prior_from_scorecard(scores: dict) -> Credence` — base `Beta(1,1)` (uniform), nudged by
     `novelty`/`feasibility` toward a slightly higher/lower mean but with **total mass ≈ 2** (e.g.
     `alpha = 1 + 0.5·novelty`, `beta = 1 + 0.5·(1−novelty)`); MUST satisfy `is_weak_prior(...) is
     True` for ANY scorecard input (assert in tests). No scorecard → `Beta(1,1)`.
   - `update(c, *, holds: bool | None, confirm_split: bool, audit_error: bool) -> Credence` — returns
     `c` UNCHANGED unless `holds in (True, False)` AND `confirm_split is True` AND `not audit_error`;
     then `+1` to α (holds True) or β (holds False), `n_updates += 1`. No-ops on `holds=None`
     (not_evaluated), non-confirm-split, degraded audit. This is the only mutator and it consumes a
     harness verdict, never an LLM output.
   - `mean(c) -> float`, `binary_entropy(p) -> float` (bits; `0·log0 ≜ 0`), `entropy(c) ->
     binary_entropy(mean(c))`.
   - `expected_entropy_reduction(c, p_holds: float) -> float` — `H(mean) − [p_holds·H(mean⁺) +
     (1−p_holds)·H(mean⁻)]` where `mean⁺/mean⁻` are the means after a hold/refute update; clamp ≥ 0.
   - `is_weak_prior(c) -> bool` — `(c.alpha + c.beta) < WEAK_PRIOR_MAX_MASS` (a credence needs ≥2
     harness updates to shed the weak-prior flag).
2. `driver.py` `__init__`: add `self._belief: dict[str, Credence] = {}` keyed by a stable
   `question_key` (slugified `hypothesis.open_question` or, round 1, the objective).
3. At ideate / scorecard time (where `self._last_scores` is set, `_scorecard_gate` ~`driver.py:919`):
   if `question_key not in self._belief`, seed `self._belief[question_key] =
   prior_from_scorecard(scores)` and emit `belief_prior` (`payload={question_key, alpha, beta, mean,
   weak_prior: True}`). Idempotent per lineage.

### Acceptance
- `prior_from_scorecard` is weak for all inputs; `update` no-ops on `holds=None`/non-confirm/degraded
  and moves exactly one of α/β by 1 otherwise; `expected_entropy_reduction` matches hand-computed
  values (e.g. `Beta(1,1)`, `p=0.5` → reduction = `1 − [0.5·H(2/3)+0.5·H(1/3)] = 1 − 0.918 = 0.082`
  bits) within 1e-6; `is_weak_prior` flips False only after ≥2 updates.
- A `belief_prior` event is emitted once per open-question lineage, marked `weak_prior: True`.
- No verdict path reads the belief state (grep: belief is read only in S4/S5 seams).

### Verification
`conda run -n aletheia python -m pytest tests/test_belief.py -q` green; full suite still green (S1
touches no existing assertion — it only adds state + one event).

---

## S4 — Forward prediction + measured EIG (pre-registered)

**Goal:** commit a forward outcome prediction before each round, and replace the LLM's self-reported
EIG with a harness-measured one for selection + convergence. **Depends on S1.**

### Files
- `aletheia/scheduler/driver.py` — `_campaign_step` (selection, `:2317-2345`), `_run_experiment`
  return (`:2156-2189`), a pre-run prediction event.
- `tests/test_paradigm_p5.py` — extend (measured-EIG selection + convergence).

### Steps
1. **Pre-register the prediction (before the round runs).** Right after a candidate is chosen in
   `_campaign_step` (`decision["next_hypothesis"]`, ~`:2342`) — i.e. before EXECUTE — emit an
   immutable `belief_prediction` event: `payload={question_key, predicted_p_holds:
   mean(self._belief[question_key]), round}`. Stash `self._pending_prediction = predicted_p_holds`
   so the post-verdict step can compute surprise. (For round 1 the prediction is committed at
   ideate/scorecard time from the freshly-seeded prior.)
2. **Measured EIG in selection.** In `_campaign_step`, for each candidate compute
   `measured = belief.expected_entropy_reduction(self._belief.get(question_key_of(c),
   prior_from_scorecard({})), p_holds=mean(that credence))`. Define `effective_eig = min(float(c.get
   ("expected_information_gain", 0) or 0), measured)` — **fail-closed toward the measured value**.
   Replace the `viable`/`best` logic (`:2321-2331`) to filter + argmax on `effective_eig`. Attach
   `c["measured_eig"] = measured` and `c["effective_eig"] = effective_eig` for the event/log.
3. **Measured backward floor + convergence.** Replace the backward floor's reliance on
   `self._last_scores["expected_information_gain"]` (`:2322`) with the **realized** entropy reduction
   of the round that just ran (computed in step 4). Convergence = `max(effective_eig) < floor` OR the
   last round's realized reduction `< floor`.
4. **Realized surprise + belief update (after the verdict).** In `_run_experiment`, alongside the
   existing `classify_outcome` block (`:2156-2169`): derive the harness verdict
   `holds = info["demonstration"].get("holds")`, `confirm_split = bool(exploration_applied)`,
   `audit_error`; call `self._belief[question_key] = belief.update(prior, holds=holds,
   confirm_split=confirm_split, audit_error=audit_error)`. Compute `realized = 1.0 if holds else 0.0`
   (skip if `holds is None`), `surprise = abs(self._pending_prediction − realized)`,
   `realized_reduction = entropy(prior) − entropy(posterior)`. Emit `belief_update`
   (`payload={question_key, round, alpha, beta, mean, n_updates, predicted_p_holds, realized,
   surprise, realized_reduction, weak_prior}`). Carry `mean`/`surprise`/`realized_reduction` on the
   round dict returned at `:2170-2189` (so the trajectory + S5 synthesis can read them).

### Acceptance (roadmap criteria 4 & 5)
- An LLM candidate with `expected_information_gain=0.99` whose credence yields `measured=0.05` is
  selected/floored on `0.05`, never `0.99` (criterion 4).
- A `belief_prediction` event is emitted BEFORE the round's EXECUTE transition; `belief_update`
  carries `predicted_p_holds` + `realized` + `surprise` after the verdict (criterion 5).
- Convergence fires when measured EIG drops below `campaign_min_eig`; a campaign of repeated
  confirmed holds converges (credence saturates → entropy reduction → 0) instead of running forever.
- Fail-closed: missing credence → `prior_from_scorecard({})` (weak) is used; `holds=None` → no
  update, no surprise recorded.

### Verification
`pytest tests/test_paradigm_p5.py -q` green incl. the new measured-EIG cases; full suite green.

---

## S5 — Claim-strength cap + calibration honesty in the synthesis

**Goal:** a weak-prior credence can't yield a `strong` claim; the campaign synthesis reports the
belief trajectory + calibration, honestly labeled. **Depends on S1 (+ S4 for calibration numbers).**

### Files
- `aletheia/scheduler/driver.py` — `_claim_strength` (`:172-255`), `_finalize_claims` (`:1615-1698`),
  `_campaign_synthesis` (`:2365-2419`).
- `tests/test_paradigm_p5.py` / `tests/test_paradigm_p5_materials.py` — extend.

### Steps
1. **Strength cap.** Add `weak_prior: bool = False` to `_claim_strength` (`:172-186`). In the
   `formulation` branch, change the `strong` condition (`:243`) to
   `if reproduced and gate_verdict == "approve" and not exploration_missing and not weak_prior:` —
   a weak-prior credence caps the formulation claim at `moderate`, exactly mirroring
   `exploration_missing`. Comment it as the S5 invariant coupling.
2. **Wire it in.** In `_finalize_claims` (`:1689-1697`), pass `weak_prior=belief.is_weak_prior(
   self._belief.get(question_key))` (default `True`/weak when no belief state exists — fail-closed).
   Attach a `ClaimEvidence` row of a new `EVIDENCE_KINDS` value `credence` recording
   `α,β,n_updates,mean,weak_prior` so the cap is auditable.
3. **Synthesis reporting.** In `_campaign_synthesis` (`:2384-2401`), add to the prompt + dry_text a
   **belief trajectory** (credence mean per round, from the round dicts S4 enriched) and a
   **calibration** line = `mean(|predicted − realized|)` across rounds with a verdict, labeled
   `"early/weak — N harness updates"`. Add `belief_trajectory` + `calibration` to the
   `campaign_finished` payload (`:2414-2418`).

### Acceptance (roadmap criterion 6)
- A formulation claim whose credence is still a weak prior is at most `moderate`, even when
  held+approved+reproduced+seal-present (this is a deliberate, invariant-extending tightening — see
  Decision D2).
- The synthesis surfaces a per-round credence mean + a calibration number explicitly labeled weak;
  it never prints a credence as a hard probability.
- No-belief-state / degraded round → `weak_prior=True` (claim can't be strong) — fail-closed.

### Verification
`pytest tests/test_paradigm_p5.py tests/test_paradigm_p5_materials.py -q` green; the existing
`scope_overclaim → narrow` materials path still passes with the belief trajectory assertion added.

---

## S6 — Persistence + e2e summary surfacing

**Goal:** make the belief state durable + auditable in one JSON file. **Depends on S1/S4/S5.**

### Files
- `aletheia/memory/ledger.py` — `BeliefState` table; `EVIDENCE_KINDS += ("credence",)`.
- `aletheia/memory/service.py` — `upsert_credence` / `get_credence` / `list_credences(run_id)`.
- `aletheia/scheduler/driver.py` — write-through: S1 seed + S4 update also call `upsert_credence`.
- `scripts/_e2e_common.py` — `write_e2e_summary` gains a `belief` block; recorder accumulates the
  belief-event *list* (current recorder is last-wins — wrong for a trajectory).
- `tests/test_campaign.py` — extend (events emitted + credence rows persisted).

### Steps
1. **Table.** `BeliefState(run_id, question_key, alpha, beta, n_updates, updated_at)` keyed by
   `(run_id, question_key)`; created via `create_all()` (the suite's pattern, no Alembic). Service
   helpers upsert by key and list per run.
2. **Write-through.** S1's seed and S4's update call `upsert_credence` after mutating
   `self._belief` (the in-memory dict stays the hot path; the table is the durable mirror that S6's
   summary + future cross-run recall read).
3. **Summary block.** In `_e2e_common.py`, add a list-accumulating recorder for `belief_prior`,
   `belief_prediction`, `belief_update`, `campaign_reason`. `write_e2e_summary` adds a `belief`
   block: `{prior, trajectory: [{round, reason, mean, predicted_p_holds, realized, surprise,
   measured_eig}], calibration, n_harness_updates, weak_prior}` — so one file shows the reasoned
   trajectory + the credence + measured EIG + calibration, alongside the existing demonstration/audit
   blocks.

### Acceptance
- After a ≥2-round campaign, `BeliefState` rows exist for each open-question lineage and match the
  final `belief_update` events.
- The e2e summary `belief` block reconstructs the full per-round trajectory (not just last-wins) with
  reason + credence + measured EIG + calibration.

### Verification
`pytest tests/test_campaign.py -q` green; a dry-run e2e (`scripts/real_paradigm_e2e.py` style)
writes a summary whose `belief` block is populated and internally consistent.

---

## Tests (offline / mocked — safe in-session)
- **NEW** `tests/test_belief.py` — S1 math (weak prior, update gating, EIG hand-checks, weak-prior
  threshold).
- extend `tests/test_paradigm_p5.py` — S4 (measured EIG beats inflated LLM EIG; convergence on
  saturated credence) + S5 (weak-prior caps strong).
- extend `tests/test_paradigm_p5_materials.py` — belief trajectory on the existing `scope_overclaim →
  narrow` path.
- extend `tests/test_campaign.py` — S6 (belief events + persisted credence rows; credence moves ONLY
  on confirm-split verdicts).

## Verification (whole keystone)
- **Backend (env `aletheia`):** `conda run -n aletheia python -m pytest -q` fully green incl. the new
  + extended tests.
- **Live e2e OUTSIDE the Claude process** (in-process trips the AUP classifier — see
  `CLAUDE_CODE_AUP_FALSE_POSITIVE_NOTES_2026_06_04.md` and the `e2e-outside-claude-process` note). A
  ≥2-round campaign where: round 2's hypothesis is visibly shaped by round 1's reason (already true
  post-S3); the belief trajectory + calibration appear in the summary; the credence moved only on a
  harness-verified confirm-split verdict; and every `holds`/`supported`/strength stayed
  harness-owned. **Success is NOT "holds=True every round"** — an honest refute that updates the
  credence toward β and reduces entropy is a successful K2 round.

## Build order
**S1 → S4 → S5 → S6.** S1 is the foundation (every later step reads its math); S4 needs S1's entropy
+ update; S5 needs S1's `is_weak_prior` + S4's surprise; S6 persists + surfaces all three. Each step
is independently green-able and leaves the spine fail-closed if the later steps never land.

## Open decisions (recommended defaults — vetoable; not blocking)
- **D1 — credence granularity.** Track per *open-question lineage* within a Run (not per Claim row),
  since the belief that learns across rounds is about the line of inquiry, not a single finalized
  claim. *Recommended: lineage-keyed, as above.*
- **D2 — does the weak-prior cap demote a single brilliant round?** Yes: under S5 a round that
  holds+reproduces+approves is capped at `moderate` until the credence has ≥2 harness updates. This
  is a deliberate tightening that *extends* the spine (one confirm-split hold honestly IS a weak
  prior) and couples "strong" to accumulated, replicated belief. *Recommended: adopt.* If undesired,
  the alternative is to gate the cap only on campaigns of length ≥2 — but that reintroduces a
  single-round path to `strong`, which is exactly what the world-model discipline argues against.
- **D3 — EIG entropy measure.** Binary entropy of the credence mean (dependency-free), not Beta
  differential entropy (needs digamma/scipy). *Recommended: binary entropy* — it is the entropy of
  the decision-relevant belief `P(holds)` and keeps `belief.py` pure-stdlib like `outcome.py`.
- **D4 — durable table timing.** Defer the `BeliefState` table to S6; S1–S5 run on in-memory state +
  append-only events (the existing campaign-state pattern). *Recommended: defer*, so S1 stays a pure
  primitive with no schema change.
