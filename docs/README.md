# Aletheia Docs

Index of the architecture, plans, reviews, and operational notes. Start with **Current**, then
**Architecture & reference**; the **Historical context** notes are a dated audit trail (kept for
provenance, not the live plan).

## Current (the live plan + state)

- `K2_CUPRATE_CAMPAIGN_PLAN_2026_06_16.md` — **the live north star.** The cuprate-Tc plane-doping
  diagnostic on UCI superconductivity: what's built, what a runnable FULL can demonstrate, and the
  honest distance from a general AI scientist.
- `K2_NEXT_STEPS_2026-06-15.md` — K2 acceptance state + the acceptance-scorer fix (one final
  confirm-split verdict per round) and the remaining live-FULL blockers.

## Architecture & reference (foundational, current in intent)

1. `ARCHITECTURE.md` — system architecture + invariants (deterministic FSM, honest evaluation,
   cross-vendor review, full provenance, sandboxed code, budget guardrails).
2. `PROJECT_REVIEW.md` — neutral assessment: strengths, risks, and the highest-risk failure modes.
3. `AUTONOMOUS_RESEARCH_ROADMAP.md` — 3-month / 6-month / long-term improvement plan.
4. `RFC_GUARDRAILS_AND_EVIDENCE.md` — engineering RFC for the guardrails + evidence layer.
5. `PARADIGM_MODE_DESIGN.md` — judging a paradigm contribution (new question/formulation/metric) on
   novelty/well-posedness, not SOTA delta. (Designed; paradigm e2e scripts exist + pass tests.)
6. `MACRO_ROADMAP_2026_06_07.md` — long-arc plan from "honest-null machine" to frontier scientist.

## Operational notes

- `CLAUDE_CODE_AUP_FALSE_POSITIVE_NOTES_2026_06_04.md` — **read before running a live e2e.** A large
  coding context + a run's own SDK traffic can trip the AUP classifier → run live e2e in a separate
  terminal.

## Historical context (dated audit trail — superseded by the Current docs above)

- `K2_S1_S4_S6_DETAILED_PLAN_2026_06_09.md` — pre-Codex K2 S1–S6 plan (scope superseded by the
  cuprate campaign plan; still documents the S1–S6 structure).
- `CLAUDE_CODE_NEXT_STEPS_2026_06_05.md`, `CLAUDE_CODE_DEVELOPMENT_REVIEW_2026_06_04.md`,
  `SELF_REVIEW_2026_06_04.md`, `AUTONOMOUS_SCIENTIST_GAP_ANALYSIS_2026_06_04.md`,
  `CLAUDE_HARDENING_REVIEW_2026_06_02.md` — review/self-review/gap-analysis notes; most of their
  recommendations are now folded into the Current plan + the built K1/K2 spine.
