# K2 next steps for Claude development — 2026-06-15

## Purpose

This note records the 2026-06-14 Codex review and follow-up work so Claude can continue K2
development without re-discovering the same state.

Audience: Claude / future development sessions.

Scope: K2 live acceptance, recent e2e hardening, and the current blocker for landing a clean FULL
K2 run.

## Current state

K2 is still best described as:

> built and offline-green; live FULL validation still pending.

The recent Claude report `docs/K2_FULL_RUN_REPORT_2026-06-14.md` is directionally right: recent
development has been focused on the real K2 live-run blockers, not on unrelated feature expansion.
The work since 2026-06-10 hardened:

- zero-verdict acceptance scoring;
- checkpoint/resume and worker result cache;
- window-aware graceful stop before the 5h limit;
- weak-network worker retries and concurrency caps;
- direct-connection e2e launcher;
- visible `critic_vendor_error` events;
- single-threaded model execution to avoid SIGXCPU;
- APIConnectionError retry for flaky critic vendors;
- more demonstration-authoring rounds;
- disabling headless orchestration tools such as `ScheduleWakeup`.

The core remaining requirement has not changed: a clean K2 FULL run needs a multi-round campaign
where at least one final harness confirm-split verdict moves a calibrated belief, calibration is
surfaced, and verdicts remain harness-owned with the cross-vendor floor intact.

## What Codex found

The report underplayed one acceptance-scoring bug in the 20260613T160232 "proof run".

That run did show the important partial proof:

- AI-authored demonstration computed on the confirm split;
- demonstration held initially;
- reproduction recomputed it;
- audit/refutation logic produced the final outcome;
- belief moved once from the final round outcome.

But the acceptance scorer was counting every `demonstration` event as an independent K2 verdict.
A single experiment round can emit multiple demonstration events:

1. initial compute result;
2. reproduction recompute;
3. post-audit re-publication, possibly with `holds=False` if the audit refutes it.

K2 belief updates once per experiment outcome. Therefore acceptance must compare belief updates
against the final verdict per experiment round, not against every intermediate demonstration event.

Before the fix, 160232 was mis-scored as:

```text
1 belief_update vs 3 harness confirm-split verdicts
```

That produced a false spine failure. The correct interpretation is:

```text
1 belief_update vs 1 final harness confirm-split verdict
```

The run is still not FULL, because it was only one round and had no campaign-level calibration, but
it should be PARTIAL rather than a spine failure.

## Changes made by Codex

Files changed:

- `aletheia/scheduler/k2_acceptance.py`
- `tests/test_k2_acceptance.py`
- `aletheia/config/critics.yaml`

### Acceptance scorer

`score_k2` now uses a helper that collapses demonstration events to one final confirm-split verdict
per experiment round.

Implementation detail:

- It tracks the active round from the latest preceding `experiment` event.
- For each round, the latest `demonstration` payload wins.
- It then counts only final payloads with:
  - `computed=True`
  - `exploration_applied=True`
  - boolean `holds`

This preserves the anti-fakeability invariant:

```text
belief_update count must match final harness verdict count
```

but avoids false failures from intermediate snapshots.

### Regression test

Added a test for the 160232-shaped stream:

```text
experiment
demonstration holds=True
demonstration holds=True
demonstration holds=False audit_refuted=True
belief_update realized=0.0
```

Expected result:

- `n_confirm_verdicts == 1`
- `n_updates == 1`
- spine check passes
- verdict remains `partial`, not `full`, because the run does not exercise multi-round K2

The previous zero-verdict regression remains intact: a multi-round run with no verdicts and no
belief updates cannot be scored FULL.

### Critic config comment

`aletheia/config/critics.yaml` had stale comments claiming a temporary Claude-only reviewer roster,
while the actual config enables Anthropic, Gemini, DeepSeek, Zhipu, and Grok with OpenAI disabled.
The comment was updated so future developers do not misread the current direct-run strategy.

## Verification run by Codex

Commands run:

```bash
conda run -n aletheia python -m pytest tests/test_k2_acceptance.py -q
conda run -n aletheia python -m pytest tests/test_belief.py tests/test_paradigm_p5.py tests/test_campaign.py -q
conda run -n aletheia python -m pytest tests/test_worker_resilience.py tests/test_critic_providers.py tests/test_peer_review.py -q
conda run -n aletheia python -m pytest tests/test_k2_acceptance.py tests/test_worker_resilience.py tests/test_critic_providers.py tests/test_peer_review.py -q
git diff --check
```

Observed results:

- `tests/test_k2_acceptance.py`: 6 passed
- K2 key tests: 74 passed
- worker/critic tests: 20 passed, then 26 passed for the final focused set
- `git diff --check`: clean

The real 160232 transcript was also manually scored after the fix. It now scores:

```text
partial
n_confirm_verdicts = 1
n_updates = 1
calibration = None
```

That is the intended result.

## Current blocker

Do not treat this as "one known code bug remains". The known scoring bug above has been fixed.

The live FULL blocker is now operational and stochastic:

1. A fresh direction must pass the direction gate.
2. The AI must author a runnable, non-doomed, control-silent demonstration.
3. At least two independent audit vendors must be reachable.
4. The run needs enough 5h-window headroom to reach campaign synthesis.
5. The campaign must exercise K2 across rounds and surface calibration.

Resuming run `69e1667d9c2841c795cd714036bcc31c` is likely a dead end because its cached ideation is
a direction the critics reject as repackaged applicability-domain / rarity framing.

## Recommended next move

Use fresh direct runs, not resume of the rejected direction:

```bash
bash scripts/run_e2e_direct.sh
```

Before running:

- Turn FlClash off, not merely proxy env vars.
- Let the 5h window rest.
- Keep `min_review_vendors >= 2`; do not lower it to force a pass.

If direction rejection keeps happening, sharpen the initial K2 framing instead of adding more
network retries. The stronger path is likely to frame the contribution as a method/diagnostic:

```text
matched-removal counterfactual / delta_E
```

rather than a broad paradigm claim about element rarity or applicability domain. The critics are
right that naive rarity/error framing often sounds like repackaged AD. The more defensible object is
an explicit matched-removal estimand that separates generic support-density loss from
element-specific non-redundant information.

## FULL criteria to preserve

A run should only be accepted as K2 FULL if all of these are true:

- at least two campaign rounds, or otherwise an explicit multi-round K2 learning trace;
- at least one final harness confirm-split verdict;
- at least one matching `belief_update`;
- calibration surfaced in the campaign synthesis;
- final verdict count matches belief update count;
- verdicts are harness-owned, not LLM-owned;
- credences are persisted;
- weak-prior honesty remains intact;
- cross-vendor review floor is met.

Partial outcomes are useful and should be preserved, but they should not be re-labeled as FULL.

