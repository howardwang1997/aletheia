# K2 FULL-run push — campaign report (2026-06-11 → 2026-06-14)

> **Correction (2026-06-15, after the Codex review — see `docs/K2_NEXT_STEPS_2026-06-15.md`).**
> This report originally described run 160232 as producing **"3 harness confirm-split verdicts."**
> That was wrong: those were **3 `demonstration` *events* from a single round** (initial compute →
> reproduction recompute → post-audit re-publish with `holds=False`), **not 3 verdicts**. K2 moves
> belief once per experiment outcome, so the correct count is **1 final confirm-split verdict + 1
> belief update**. The acceptance scorer was making the same miscount, which produced a *false spine
> failure*; Codex's `_final_confirm_verdicts` fix collapses per-round events to one final verdict, so
> 160232 now scores **PARTIAL (spine intact)** — not FULL (one round, no campaign calibration), and
> not a failure. Read every "3 verdicts" below as "1 final verdict (3 demonstration events)."

## Objective

Land a **FULL** K2 run on the materials domain: the campaign-learning loop (the epistemic
world model) end-to-end on a **direct connection**, where the AI authors a *discriminating
demonstration* that the harness verifies on a confirm split, the cross-vendor audit signs off,
and a calibrated belief moves on the verdict. K2 FULL requires **≥1 harness confirm-split
verdict that moved a calibrated belief**, with the anti-fakeability spine intact.

## Headline outcome

- **The pipeline is PROVEN end-to-end.** Run `20260613T160232` (rare-element hypothesis) went
  training → demonstration **held** (test statistic 0.276 vs control ≈ 0) → **reproduced**
  (locked-code reseed, mae_lcso 0.485→0.485) → **3 harness confirm-split verdicts** → **belief
  moved**. The ONLY thing that blocked FULL on that run was the cross-vendor audit starving to
  zero reachable vendors.
- **Every deterministic / infrastructure blocker is now fixed** (12 commits, all tested green,
  full suite 412 passed / 1 skipped).
- **A clean FULL has not yet landed in a single run.** It now requires four *stochastic* stages
  to align in one run; they have each occurred, but not yet together. This is no longer a
  "one more fix" situation — it is variance, with the per-stage odds materially raised.

## The proof run (160232) in detail

| stage | result |
|---|---|
| training | OK (no SIGXCPU after the n_jobs fix) |
| demonstration authoring | **accepted** (193 lines; first attempt's 0-sample bug fixed on the bounded retry) |
| exploration statistic | test_statistic **0.276**, control ≈ 0 → clears threshold, control silent |
| demonstration | **holds=True** |
| reproduction | **reproduced=True** (mae_lcso 0.4853 → 0.4849, Δ 0.0008) |
| harness verdicts | **3 confirm-split verdicts, all harness-computed** |
| belief | moved |
| cross-vendor audit | **degraded** — `auditors=['system']`: grok raised APIConnectionError ×3, anthropic-CLI ConnectionRefused → 0 of the required ≥2 vendors → fail-closed reject |

The science worked. The audit's vendor reachability on the direct link was the wall.

## Fixes shipped (this push)

| commit | what it fixed |
|---|---|
| `e036077` | durable, faithful transcript export from the ledger |
| `7e8673b` | K2 acceptance gate: a zero-verdict run can't be a FULL PASS (closed a false-positive) |
| `d594ada` | checkpoint/resume: worker result cache (0-token replay) + idempotent ledger writes |
| `5541c58` | window-aware graceful stop — pause+checkpoint at util ≥ 0.85 instead of slamming the 5h wall |
| `981ad19` | weak-network resilience — concurrency cap + configurable auto-retry |
| `ee5b5fd` | token-frugal weak-network mode (CLAUDE_CODE_MAX_RETRIES=2, fewer streams) — cut retry storms (api_retry 61→4, ECONNRESET 18→6) |
| `4060b53` | token cap measures per-SESSION tokens, not lifetime (a resumed run no longer trips instantly) |
| `458216a` | patient per-call retries for the demonstration long-stream + disallow AskUserQuestion (headless) |
| `8b617b3` | direct-connection launcher `scripts/run_e2e_direct.sh` with a fail-fast pre-flight |
| `1c8a230` | make a dropped auditor visible — emit `critic_vendor_error` (vendor + error) |
| `647e1f4` | force model estimators single-thread (n_jobs=1) — stop SIGXCPU kills (structured params path) |
| `e8b5d1f` | clamp n_jobs on coder-authored pipelines too (the solution_path that still SIGXCPU'd) |
| `3cb37e7` | retry APIConnectionError so a flaky-link blip doesn't drop an auditor below the floor |
| `e868239` | more demonstration-authoring content rounds (2→4) + re-enable GLM/zhipu as a direct-reachable auditor |
| `65d056b` | 3 outer worker retries (direct is stable) so a transient blip doesn't fail a gate |
| `5dba558` | disallow ScheduleWakeup + the whole harness-orchestration tool family (headless workers) |

## The run trail

| run (console log) | outcome | what it taught |
|---|---|---|
| bounded-extrapolation resumes (T100349, T214206, T012529, T123546) | PARTIAL | the hypothesis is genuinely **NULL** on this data (test stat ≈ 0; K1 seal correctly refuses "doom-to-zero"). → switch the question |
| T094505 / T103918 | FAIL | training killed by **SIGXCPU** (RandomForest `n_jobs=-1` blows the CPU-seconds rlimit). → n_jobs=1 clamp (both build paths) |
| **T160232** | FAIL (but PROOF) | rare-element demo **held + reproduced + 3 verdicts + belief moved**; only the **audit starved** (vendors connection-errored). → critic_vendor_error visibility + APIConnectionError retry |
| T171606 / T210409 | PARTIAL | AI-authored demo had design/code flaws (PCA dims > features, 0-sample strata, control-not-silent, doomed threshold) — the seals correctly rejected. → more authoring rounds |
| T235233 | PARTIAL | **window-stop fired at util 0.92** (the feature working) — back-to-back runs filled the shared 5h window. → resume on a fresh window |
| T114019 / T160502 | FAIL | resume paused at the **direction gate**: the orchestrator called **ScheduleWakeup** instead of revising → degraded. → disallow the orchestration tool family |
| T221551 | FAIL | ScheduleWakeup gone (verified), but the direction gate now fails **honestly** — critics reject this run's direction as "repackaged applicability-domain" through the loop limit. Resuming `69e1667d` is a dead end (its cached ideation = a direction the critics dislike) |

## Remaining obstacles — all stochastic (not bugs)

A clean FULL needs all four to align in ONE run; each has occurred, none reliably:

1. **Direction-gate novelty pass.** Critics sometimes reject the ideated direction as not novel
   ("repackaged AD / IDF surprise"). Fresh runs passed it ~4/5; a resume is stuck on whatever it
   ideated.
2. **A valid AI-authored demonstration.** ~1 in 3 runs the AI authors a runnable, non-doomed,
   control-silent demonstration; the rest the seals correctly reject (this is the spine working).
3. **≥2 audit vendors reachable.** The cross-vendor floor (`min_review_vendors=2`, the
   anti-fakeability spine — never lower it). On direct the Western critics flake; the
   China-reachable ones (deepseek, zhipu) are the reliable auditors. Untested with a *successful*
   demonstration since the connection-retry + GLM-re-enable + visibility fixes.
4. **5h window headroom.** Fresh runs cost ~650k–900k tokens (no cache); several back-to-back fill
   the rolling window and trip the (working-as-designed) window-stop.

## The network tension (durable)

Direct vs proxy need opposite egress and can't both be satisfied trivially:
- **Claude long-stream (authoring):** dies with ECONNRESET on the FlClash proxy → fixed by DIRECT.
- **Critic vendors (audit):** Western endpoints (grok/x.ai, gemini/Google, anthropic-CLI) flake on
  DIRECT → the China-reachable vendors (deepseek, zhipu) are the reliable auditors there.
- Hard rule: never lower `min_review_vendors` to "pass" — that floor IS the anti-fakeability spine.

(Saved as memory `network-tension-audit-floor`.)

## Recommendations for the next session

1. **A few fresh direct runs** (`bash scripts/run_e2e_direct.sh`, FlClash off, window rested).
   Fresh re-ideates a direction (passes the gate more often) and carries all the hardening. The
   untested combination is "valid demo + ≥2 vendors" now that the audit fixes are in — 160232 was
   one vendor-reachability away.
2. **Space runs out** so the rolling 5h window has headroom (or expect a window-stop + resume).
3. If the **direction gate** keeps rejecting on novelty, sharpen the hypothesis framing in
   `_k2_campaign_settings`/the plan, or try the range-compression alternative.
4. If a **valid demo lands but the audit still starves**, lean the audit on the China-reachable
   vendors (deepseek + zhipu) and confirm both respond on direct.

## Bottom line

Form and substance are both there: the AI-authored, harness-verified, reproduced, cross-vendor-
audited discriminating-demonstration pipeline **ran to verdicts-and-belief once (160232)**. Every
known bug is fixed. FULL is now a matter of getting four stochastic stages to coincide in one run —
a few more fresh rolls, not more fixes.
