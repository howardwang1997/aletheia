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
from pathlib import Path

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
    # UCI superconductivity Tc (figshare is blocked on this box; the file is fetched from
    # archive.ics.uci.edu and cached locally). Step 1 wired the upload path + composition_column.
    csv = Path(__file__).resolve().parents[1] / "artifacts" / "datasets" / "superconduct_unique_m.csv"
    if not csv.exists():
        raise SystemExit(
            f"missing dataset: {csv}\n"
            "Fetch it from UCI (figshare is blocked here):\n"
            "  curl -sSL -o /tmp/sc.zip "
            "'https://archive.ics.uci.edu/static/public/464/superconductivty+data.zip'\n"
            f"  unzip -o /tmp/sc.zip unique_m.csv && mkdir -p {csv.parent} && cp unique_m.csv {csv}"
        )
    run_id = create_run(
        "Real e2e (K2 campaign): does a composition-only Magpie/random-forest superconducting-Tc model "
        "have a CHEMISTRY-SPECIFIC blind spot on multi-alkaline-earth CUPRATES — larger error there "
        "than on non-cuprates matched for generic composition complexity — and what does each round "
        "teach the next?",
        domain="materials",
        status="scoping",
        budget_cap_usd=150.0,  # up to 3 rounds of authoring + audit + gates
    )
    # explicit, auditable upload spec (Step 1a): the formula column 'material' propagates through
    # resolve_data_spec -> data_spec -> featurizer + authoring prompt; target 'critical_temp' (K).
    register_dataset(
        run_id, "upload", ref=str(csv), uri=str(csv),
        target_column="critical_temp", composition_column="material",
        feature_kind="composition", status="ready",
    )
    exp_id = finalize_plan(
        run_id,
        {
            "objective": "Establish whether a composition-only Magpie / random-forest superconducting-Tc "
            "model has a CHEMISTRY-SPECIFIC blind spot on multi-alkaline-earth CUPRATES — the family "
            "where Tc is governed by plane-specific hole doping that composition-AVERAGED features "
            "cannot resolve. This is a DIAGNOSTIC characterization of a model failure mode (NOT a new "
            "material, NOT a SOTA win): a multi-round PROGRAM where each round authors a discriminating "
            "demonstration + a matched negative control; the go/no-go step uses what the round learned "
            "(held / refuted / over-claimed / sample-starved) to choose the next experiment.",
            "domain": "materials",
            "direction": "a DIAGNOSTIC / model failure-mode characterization (methodology), not a "
            "benchmark win and not a grand paradigm. The AI authors the discriminating demonstration; "
            "the harness applies its pre-registered decision rule + matched negative control on a "
            "held-out confirm split; an independent cross-vendor audit (author excluded) reviews the "
            "code; the campaign LEARNS across rounds. Positioned relative to applicability-domain, "
            "leave-one-group-out CV, and group data valuation — the specific contribution is the "
            "CHEMISTRY-defined cuprate stratum plus a complexity-matched control that rules out generic "
            "composition complexity.",
            "hypothesis": "On the UCI superconductivity dataset (Magpie composition features), a "
            "random-forest Tc model makes systematically LARGER errors on MULTI-ALKALINE-EARTH CUPRATES "
            "(the composition contains Cu AND O AND >= 2 of {Ba,Sr,Ca,Mg}) than on non-cuprates MATCHED "
            "on composition complexity (element count) and feature-space density. The stratum is defined "
            "PURELY from elemental co-occurrence (NOT from Tc), so the test is non-circular and not "
            "applicability-domain. MECHANISM: Tc in cuprates is set by plane-specific hole doping "
            "(~0.16 holes per Cu) that composition averages cannot represent, so the model defaults to a "
            "family mean and misses the doping-driven Tc variation. Reference probes (for calibration, "
            "not asserted by the run): raw cuprate MAE ~11 K vs ~5 K on the rest; +5.6 K median excess "
            "vs a permuted-strata null (p95 ~0.5 K); +3.58 K vs a complexity-matched control "
            "(bootstrap 95% CI [+2.46, +4.72]).",
            "contribution_type": "paradigm",
            "demonstration": "discriminating instance using ONLY quantities derivable from the harness "
            "inputs X (Magpie features), y (Tc), and groups (the chemical-system / element SET) — NOT "
            "raw stoichiometry, which is not passed to the demonstration: (1) identify the "
            "multi-alkaline-earth cuprate stratum from `groups` (Cu, O, and >= 2 alkaline earths); "
            "(2) on a held-out confirm split, fit the Tc model and show its MAE on that stratum EXCEEDS "
            "its MAE on an equal-size control matched on element count (from `groups`) + feature-space "
            "density (from `X`), beyond a permuted-strata null. The AI authors compute_demonstration and "
            "PRE-REGISTERS the supported/refuted thresholds (a positive matched excess with a "
            "repeated-split bootstrap-CI lower bound > 0). Do NOT compute stoichiometric hole-count or "
            "plane-doping proxies — formula fractions are unavailable; the mechanism only MOTIVATES the "
            "claim. All matching must be DETERMINISTIC (seeded).",
            "dataset": "UCI superconductivity (unique_m.csv, ~21k formulas + critical_temp; composition "
            "column 'material')",
            "method": "Magpie composition features; an AI-authored compute_demonstration that fits an RF "
            "Tc regressor, derives the cuprate stratum + element count from the chemical-system "
            "`groups`, builds an equal-size complexity-matched (element-count + feature-density) "
            "non-cuprate control, and computes the cuprate-minus-matched-control MAE excess on the "
            "confirm split with a repeated-split bootstrap CI; a permuted-strata reshuffle is the "
            "negative control (expected ~ 0).",
            "metrics": "test statistic = cuprate-minus-complexity-matched-control MAE excess (K) on the "
            "confirm split; control statistic = permuted-strata excess (random equal-size subset), "
            "expected ~ 0. The AI pre-registers supported_if (excess >= threshold with a bootstrap-CI "
            "lower bound > 0) and control_silent_if (|control| <= a small bound).",
            "success_criteria": "the program LEARNS: each round's confirm/refute reason shapes the next "
            "(narrow an over-claim, change a non-generalizing effect, scale a starved sample, or ablate "
            "a confirmed mechanism). A held + reproduced + audited demonstration is a win; an honest "
            "refute that moves the belief is ALSO a successful round. NOTE: a single held cuprate "
            "demonstration is NOT by itself K2 FULL — FULL also requires multi-round learning, >=1 "
            "harness confirm-split verdict, a matching belief update, calibration surfaced in the "
            "campaign synthesis, and the cross-vendor audit floor met.",
            "est_compute": "CPU-only; minutes per round (RF fit + cuprate/matched-control MAE + a bootstrap)",
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
