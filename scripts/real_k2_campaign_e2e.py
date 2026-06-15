"""Real end-to-end K2 — the CAMPAIGN LEARNING LOOP (the epistemic world model).

This is the live verification owed for K2. Unlike the single-experiment paradigm e2e scripts, this
runs a MULTI-ROUND campaign (``max_experiments_per_campaign=3``) on the AI-authored demonstration
path, so each round produces a real harness-verified verdict that the campaign can LEARN from. It
then evaluates the K2 success criteria from the captured event stream + the ledger and prints an
explicit ✓/✗ checklist + a verdict.

What K2 success means (and does NOT mean):
  - SUCCESS is the loop LEARNING across rounds: round N's typed reason shapes round N+1; a calibrated
    belief credence moves ONLY on a harness-verified confirm-split verdict; the forward prediction is
    committed BEFORE each round (pre-registration); the belief trajectory + calibration are surfaced;
    and every ``holds``/``supported``/strength stays harness-owned.
  - SUCCESS is NOT "holds=True every round". An honest REFUTE that moves the credence toward beta and
    reduces entropy is a fully successful K2 round. A round that authors no demonstration and triggers
    a bounded PIVOT (S3.5) is an informative negative, not a failure.

The spine is never traded for a result: the belief is a planning aid; it never sets a verdict. The
harness applies the AI's pre-registered decision rule + negative control + leakage probes + an
independent cross-vendor audit (author excluded); the AI never returns ``holds``.

Real Opus (ideation + code authoring + go/no-go planning) + cross-vendor critic/audit gates + real
Magpie/sklearn. Spends Opus + critic credit across up to 3 rounds; CPU-only demonstration compute.

IMPORTANT — run this OUTSIDE the Claude Code session you used to build it. A live run adds its own
Claude SDK traffic on top of an already large, policy-sensitive coding context, which can trip
Anthropic's upstream AUP classifier into "unable to respond" API errors (a context-level false
positive — see docs/CLAUDE_CODE_AUP_FALSE_POSITIVE_NOTES_2026_06_04.md). Run it in a plain terminal:

    conda run -n aletheia python scripts/real_k2_campaign_e2e.py

then read the K2 CHECKLIST printed at the end + the single JSON summary (under ``artifacts/``), whose
``belief`` block holds the per-round reason, credence trajectory, predicted-vs-realized surprise, and
final durable credences.
"""

from __future__ import annotations

import asyncio
import os
import time

from _e2e_common import RunRecorder, print_usage, tee_console, write_e2e_summary

from aletheia.config import get_settings
from aletheia.data.registry import register_dataset
from aletheia.db import create_all
from aletheia.events.bus import get_bus
from aletheia.memory.service import (
    create_run,
    finalize_plan,
    get_run,
    list_claims,
    list_credences,
    list_metrics,
)
from aletheia.scheduler.driver import ExperimentDriver
from aletheia.scheduler.k2_acceptance import score_k2


def _short(p: dict, n: int = 170) -> str:
    s = ", ".join(f"{k}={v}" for k, v in (p or {}).items())
    return s if len(s) <= n else s[:n] + "…"


def evaluate_k2(events_log: list[dict], run_id: str) -> None:
    """Print an explicit ✓/✗ evaluation of the K2 success criteria from the captured stream.

    Thin printer over the pure :func:`aletheia.scheduler.k2_acceptance.score_k2` (which holds the
    real logic and is unit-tested). Reads ONLY the event stream + the ledger — the same surface a
    reviewer has — and never asserts, so a partial/honest outcome still produces a readable report.
    """
    result = score_k2(events_log, list_credences(run_id))

    print("\n=== K2 SUCCESS CRITERIA ===", flush=True)
    for c in result.checks:
        mark = "✓" if c.ok else ("—" if c.ok is None else "✗")
        print(f"  [{mark}] {c.name}\n        {c.detail}", flush=True)

    print("\n=== K2 VERDICT ===", flush=True)
    if result.verdict == "full":
        print("  ✓ FULL PASS — the campaign LEARNED across rounds with the anti-fakeability spine "
              "intact (belief moved only on harness verdicts; verdicts harness-owned).", flush=True)
    elif result.verdict == "partial":
        print("  ~ PARTIAL — the K2 machinery + spine are correct, but this run did not exercise the "
              "full thesis: it needs >=1 harness confirm-split verdict that moved a calibrated belief "
              f"(this run had {result.n_confirm_verdicts} verdict(s), {result.n_updates} belief "
              "update(s), calibration="
              f"{result.calibration}). An honest refute or a bounded pivot is NOT a failure — but a "
              "run with no verdict at all cannot be a FULL pass. Adjust the objective / authoring so "
              "a discriminating demonstration actually computes on the confirm split.", flush=True)
    else:
        print("  ✗ FAIL — a core K2 criterion or the spine did not hold (e.g. a belief moved without "
              "a harness verdict). Inspect the ✗ rows + the belief block in the JSON summary.",
              flush=True)


async def _drive_and_report(run_id: str, exp_id: str, timestamp: str) -> None:
    """Run the driver to completion (streaming events to the recorder), write the summary + usage +
    transcript, and print the K2 verdict. Shared by a fresh run and a ``--resume``."""
    recorder = RunRecorder()
    events_log: list[dict] = []  # full ordered stream, for cross-type ordering checks

    async def printer() -> None:
        async for evt in get_bus().subscribe():
            recorder.observe(evt)
            events_log.append({"type": evt.get("type", ""), "payload": evt.get("payload") or {}})
            print(f">> {evt['type']}: {_short(evt.get('payload') or {})}", flush=True)

    pt = asyncio.create_task(printer())
    try:
        await ExperimentDriver(run_id, dry_run=False).run()
    finally:
        await asyncio.sleep(0.2)  # let the final events drain into the recorder
        pt.cancel()

    run = get_run(run_id)
    print("\n--- SUMMARY ---", flush=True)
    print("run status:", run["status"])
    print("metrics:", list_metrics(exp_id))
    print("claims:", [(c["claim_type"], c["status"], c["strength"]) for c in list_claims(run_id)])
    print("final credences:", list_credences(run_id))

    summary_path = write_e2e_summary(
        run_id=run_id, exp_id=exp_id, timestamp=timestamp,
        prefer_authored=get_settings().demonstration_prefer_authored,
        recorder=recorder, domain="materials",
    )
    print(f"summary written: {summary_path}", flush=True)
    print_usage(run_id)

    evaluate_k2(events_log, run_id)


def _k2_campaign_settings() -> None:
    # MULTI-ROUND: the whole point of K2 is learning ACROSS rounds, so allow up to 3 linked
    # experiments + a bounded pivot budget for informative undemonstrated rounds (S3.5).
    get_settings().max_experiments_per_campaign = 3
    get_settings().campaign_max_pivots = 2
    # FRONTIER OVERRIDE: force the AI-authored demonstration path so each round produces a real
    # harness-verified verdict the campaign can fold into its belief (materials has no hand-built
    # fallback demonstration).
    get_settings().demonstration_prefer_authored = True
    # WEAK-NETWORK MODE (token-frugal): this box reaches the API through a flaky proxy that resets
    # long-lived streams. Every reset re-sends the whole call's context, so a retry STORM silently
    # burns the shared 5-hour window. Three layers bound that, cheapest first:
    #   1) CLAUDE_CODE_MAX_RETRIES: the SDK-driven CLI's OWN retry depth defaults to 10 (re-sending
    #      context each time). Cap it to 2 — the single biggest amplifier (last run: 61 api_retry,
    #      6 calls hit 10/10). setdefault so a shell export can still override.
    #   2) fewer concurrent long streams (less proxy pressure = fewer resets) + fewer OUTER attempts.
    #      Combined depth is now ~2x2 instead of 4x10; the resume cache makes a reset cost ≤ one call.
    #   3) a generous hard token backstop + a WINDOW-AWARE graceful stop (the precise guard): pause +
    #      checkpoint once the live 5h reading hits 0.85, then resume on a fresh window for 0 tokens.
    os.environ.setdefault("CLAUDE_CODE_MAX_RETRIES", "2")
    get_settings().max_concurrent_workers = 2
    # 3 OUTER attempts for every worker: on a stable direct link a single transient blip must not
    # degrade a short orchestrator/critic call and fail a whole gate (observed: the direction gate
    # paused on one empty-reason orchestrator degrade at attempts=2). Retries are cheap when calls
    # rarely fail; the resume cache still caps a reset at one call's work.
    get_settings().worker_max_attempts = 3
    get_settings().worker_backoff_s = 10.0
    # the discriminating-demonstration authoring is the long stream that keeps degrading on a reset;
    # give JUST that call more patient attempts to land one clean stream (everything else uses 3).
    get_settings().authoring_max_attempts = 5
    # the AI-authored demonstration is the stochastic step: give it more CONTENT rounds to fix a
    # flagged design flaw (leaky control / doomed threshold / runtime error) with feedback before the
    # round is written off as undemonstrated.
    get_settings().demonstration_authoring_rounds = 4
    get_settings().token_cap_per_run = 1_200_000
    get_settings().window_stop_utilization = 0.85


async def resume(timestamp: str, run_id: str) -> None:
    """Resume an interrupted run: replay every COMPLETED Claude call from the worker cache (0 tokens)
    and continue live from the first call that never finished. Idempotent ledger writes keep the
    summary clean. Reuses the existing run_id — does NOT re-create the run or re-finalize the plan."""
    _k2_campaign_settings()
    get_settings().resume_cache_read = True  # the switch that makes completed calls replay for free
    create_all()
    run = get_run(run_id)
    if run is None:
        raise SystemExit(f"resume: run {run_id} not found in the ledger")
    exp_id = run.get("plan_experiment_id")
    print(f"RESUMING run_id={run_id} exp_id={exp_id} — completed Claude calls replay from cache "
          "(0 tokens); the first unfinished call runs live.\n--- live events ---", flush=True)
    await _drive_and_report(run_id, exp_id, timestamp)


async def main(timestamp: str) -> None:
    _k2_campaign_settings()
    create_all()
    run_id = create_run(
        "Real e2e (K2 campaign): does a chemical element E's training data carry ELEMENT-SPECIFIC, "
        "NON-REDUNDANT information for band-gap prediction — measured by a support-AND-topology-matched "
        "counterfactual REMOVAL estimand (delta_E) on an E-FREE holdout — beyond what generic, "
        "equally-dense, equally-clustered training data supplies; and what does each round teach the next?",
        domain="materials",
        status="scoping",
        budget_cap_usd=150.0,  # up to 3 rounds of authoring + audit + gates
    )
    register_dataset(
        run_id, "benchmark", ref="matbench_expt_gap",
        target_column="gap expt", status="ready",
    )
    exp_id = finalize_plan(
        run_id,
        {
            "objective": "Quantify, per chemical element E, the ELEMENT-SPECIFIC NON-REDUNDANT "
            "information E's training compounds carry for band-gap prediction, via a "
            "SUPPORT-AND-TOPOLOGY-matched counterfactual REMOVAL estimand delta_E evaluated on an "
            "E-FREE holdout — so it is a property of the TRAINING set's information structure, NOT "
            "test-time extrapolation. This is a DIAGNOSTIC ESTIMAND (not a benchmark win, not a grand "
            "paradigm): a multi-round PROGRAM where each round authors a discriminating demonstration "
            "+ matched negative controls; the go/no-go step uses what the round learned (held / "
            "refuted / over-claimed / sample-starved) to choose the next experiment.",
            "domain": "materials",
            "direction": "a DIAGNOSTIC ESTIMAND / new measurement (methodology), not a benchmark win. "
            "The AI authors the discriminating demonstration; the harness applies its pre-registered "
            "decision rule + matched negative controls; an independent cross-vendor audit (author "
            "excluded) reviews the code; the campaign LEARNS across rounds. Positioned relative to "
            "leave-one-group-out CV, matching/stratification (causal inference), and group data "
            "valuation (Data-Shapley / influence) — the specific contribution is the "
            "support-AND-topology-matched null plus the E-free test restriction.",
            "hypothesis": "For element E, define delta_E by support-and-topology-matched counterfactual "
            "removal: fit the regressor, then measure the rise in held-out band-gap error — ON HOLDOUT "
            "COMPOUNDS THAT DO NOT CONTAIN E — when E's training compounds are removed, MINUS the LARGER "
            "of two matched-control rises that remove the SAME count of training compounds: (i) a RANDOM "
            "subset matched on feature-space support density (k-NN density in standardized Magpie "
            "space), and (ii) a spatially-COHERENT subset (one composition-space k-means cluster) "
            "matched on local neighborhood structure so the removal has the SAME topology as carving "
            "out an element. delta_E = (E-removal MAE rise) - max(control-i rise, control-ii rise). "
            "CLAIM: delta_E is significantly > 0 for SOME elements and ~ 0 for others (heterogeneous, "
            "non-redundant signal), while both matched controls' own excess (one matched removal vs "
            "another) is ~ 0. The E-free test makes this a TRAINING-set information property, not "
            "rarity, test-point distance, or generic ablation.",
            "contribution_type": "paradigm",
            "demonstration": "discriminating instance (kept tractable PER ROUND): pick ONE candidate "
            "element E with adequate training support; on an E-FREE holdout, show the MAE rise from "
            "removing E's training compounds materially EXCEEDS the rise from a TOPOLOGY-matched (one "
            "composition-space k-means cluster) removal of the SAME size, and the CONTROL (one matched "
            "removal vs another matched removal) shows ~ 0 excess. The AI authors compute_demonstration "
            "and PRE-REGISTERS the supported/refuted thresholds (delta_E with a repeated-split bootstrap "
            "CI lower bound > 0). All matching/clustering must be DETERMINISTIC (seeded).",
            "dataset": "matbench_expt_gap",
            "method": "Magpie composition features (standardized); an AI-authored compute_demonstration "
            "that (a) fits a regressor, (b) selects a target element E and an equal-size TOPOLOGY-matched "
            "control set (one seeded k-means cluster in Magpie space) — optionally also a density-matched "
            "random set (k-NN density) — (c) refits with each removal, (d) scores the MAE rise on the "
            "E-FREE holdout, (e) computes delta_E = E-removal rise - matched-control rise with a "
            "repeated-split bootstrap CI. Compute is BOUNDED: ONE element + ONE matched control per "
            "demonstration round (the campaign scales breadth across rounds; Benjamini-Hochberg is "
            "pre-specified once multiple elements are tested).",
            "metrics": "test statistic = delta_E (E-removal MAE rise minus topology-matched-removal MAE "
            "rise, on the E-free holdout); control statistic = matched-vs-matched excess (one matched "
            "removal vs another), expected ~ 0. The AI pre-registers supported_if (delta_E >= threshold "
            "with a bootstrap-CI lower bound > 0) and control_silent_if (|control| <= a small bound).",
            "success_criteria": "the program LEARNS: each round's confirm/refute reason shapes the "
            "next (narrow an over-claim, change a non-generalizing effect, scale a starved sample, or "
            "ablate a confirmed mechanism). A held + reproduced + audited demonstration is a win; an "
            "honest refute that moves the belief is ALSO a successful round.",
            "est_compute": "CPU-only; minutes per round (one element + one matched control retrain + a bootstrap)",
        },
    )
    print(f"run_id={run_id} exp_id={exp_id}\n--- live events ---", flush=True)
    await _drive_and_report(run_id, exp_id, timestamp)


if __name__ == "__main__":
    import sys

    ts = time.strftime("%Y%m%dT%H%M%S")
    resume_id: str | None = None
    if "--resume" in sys.argv:
        i = sys.argv.index("--resume")
        resume_id = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
        if not resume_id:
            raise SystemExit("usage: real_k2_campaign_e2e.py --resume <run_id>")
    with tee_console("materials", ts) as log_path:
        asyncio.run(resume(ts, resume_id) if resume_id else main(ts))
        print(f"console log: {log_path}", flush=True)
        print(f"console log: {log_path}", flush=True)
