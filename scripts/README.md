# scripts/

Operational entry points for Aletheia: **live e2e runners**, the **cheap probe pipeline** that
de-risks a discriminating effect before an expensive run, and **utilities**.

> **Run live (real-Claude) e2e scripts in a SEPARATE terminal**, not inside a Claude Code session —
> a large coding context + the run's own SDK traffic can trip Anthropic's AUP classifier
> (see `docs/CLAUDE_CODE_AUP_FALSE_POSITIVE_NOTES_2026_06_04.md`). The probe scripts below are
> Claude-free (or exclude the author vendor) and are safe to run anywhere.

## A. Live e2e runners (real Opus + real cross-vendor critics + real training)

| script | what it runs |
|---|---|
| `real_k2_campaign_e2e.py` | **current focus** — the K2 multi-round campaign; now the cuprate-Tc plane-doping diagnostic on UCI superconductivity. Prints a K2 ✓/✗ checklist + verdict. |
| `run_e2e_direct.sh` | launches `real_k2_campaign_e2e.py` on a **direct** connection (no FlClash/proxy) with a fail-fast api.anthropic.com pre-flight. `--resume <run_id>` supported. |
| `real_ai_demonstration_e2e.py` / `_materials.py` | single-experiment AI-authored discriminating-demonstration path (molecules / materials). |
| `real_paradigm_e2e.py`, `real_paradigm_cliff_e2e.py`, `real_leakage_law_e2e.py` | paradigm-mode contributions (new evaluation frame, judged on novelty/well-posedness, not SOTA delta). |
| `real_molecules_e2e.py`, `real_e2e.py`, `real_rag_e2e.py` | baseline full-loop runs on molecules (ESOL), materials (matbench band-gap), and RAG (eval-only). |

## B. The probe pipeline (cheap, Claude-free de-risking — run BEFORE a live FULL)

Lesson learned the hard way: a *novel* estimand that is **null** on the data is as useless as a
*real* effect that is **not novel**. So screen cheaply (minutes of compute, $0 of live-FULL tokens),
in this order, before committing a ~700k-token run:

1. **Ideate (physics-grounded).** `physics_ideation_probe.py` — non-author vendors (zhipu/grok) as
   materials-physics experts propose falsifiable, chemistry-defined (non-circular) failure-mode
   hypotheses, avoiding the exhausted categories (applicability-domain, target-tail, cliffs).
2. **Does it HOLD?** `effect_hold_probe.py` / `novel_effect_scan.py` / `chem_effect_probe.py` —
   test candidate statistics (with a permuted-strata control) on real data; keep only the holders.
   `delta_e_feasibility_probe.py` runs a candidate through the real harness demonstration path.
3. **Is it not an artifact?** `cuprate_matched_control.py` / `overconfidence_contrastive_probe.py` —
   rule out the AD/complexity confound (matched control) or a by-construction/mechanical reading.
4. **Is it NOVEL?** `direction_framing_probe.py` — drive the real `gateway.review("direction", …)`
   with the author vendor EXCLUDED, on a hand-written hypothesis dict, until it clears the novelty
   gate. Then port the framing into `real_k2_campaign_e2e.py`.
5. **Will the audit clear?** `audit_smoke_test.py` — exercise the cross-vendor demonstration-audit
   gate against a canned clean payload; confirms ≥2 distinct non-author vendors are reachable on the
   current egress BEFORE burning a full run (the gate that starved run 160232).

`demo_dense_vs_lexical.py` and `propose_gaps.py` are domain demos / a conversational gap-finder.

## C. Utilities

| script | what |
|---|---|
| `usage_report.py` | real per-run token + cost from the ledger (`--top N`, `<run_id>`). |
| `export_transcript.py` | export a run's full dialogue to `.jsonl` (lossless) + `.md` (readable). |
| `_e2e_common.py` | shared helpers for the e2e runners (not a script). |
