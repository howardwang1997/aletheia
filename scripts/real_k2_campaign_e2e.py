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


async def main(timestamp: str) -> None:
    # MULTI-ROUND: the whole point of K2 is learning ACROSS rounds, so allow up to 3 linked
    # experiments + a bounded pivot budget for informative undemonstrated rounds (S3.5).
    get_settings().max_experiments_per_campaign = 3
    get_settings().campaign_max_pivots = 2
    # FRONTIER OVERRIDE: force the AI-authored demonstration path so each round produces a real
    # harness-verified verdict the campaign can fold into its belief (materials has no hand-built
    # fallback demonstration).
    get_settings().demonstration_prefer_authored = True
    create_all()
    run_id = create_run(
        "Real e2e (K2 campaign): does a Magpie band-gap regressor have a structurally bounded "
        "ability to extrapolate to UNSEEN chemical systems — and what does each round's outcome "
        "teach the next?",
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
            "objective": "Establish whether a Magpie-feature band-gap regressor has a STRUCTURALLY "
            "BOUNDED ability to extrapolate to UNSEEN chemical systems: its predicted gap for a "
            "held-out system is pinned near the gaps of its nearest training systems, so it cannot "
            "represent chemistry-driven gap jumps. This is a multi-round PROGRAM: each round authors a "
            "discriminating demonstration + a NEGATIVE CONTROL; the go/no-go step uses what the round "
            "learned (held / refuted / over-claimed / sample-starved) to choose the next experiment.",
            "domain": "materials",
            "direction": "a NEW EVALUATION FRAME (methodology), not a benchmark win — a paradigm "
            "program whose discriminating demonstrations the AI authors itself. The harness applies "
            "the AI's pre-registered decision rule + negative control; an independent cross-vendor "
            "audit (author excluded) reviews the code; the campaign LEARNS across rounds.",
            "hypothesis": "When whole chemical systems are held out, the regressor's predicted "
            "band-gap range collapses toward its training-neighbor gaps (a structural extrapolation "
            "ceiling), so the predicted spread across held-out systems is far below the true spread; "
            "the CONTROL — holding out random rows WITHIN seen systems — should show no such collapse "
            "if the effect is genuinely about unseen chemistry rather than sample size.",
            "contribution_type": "paradigm",
            "demonstration": "discriminating instance: under leave-chemical-system-out the model's "
            "predicted band-gap spread on held-out systems is structurally compressed relative to the "
            "true spread; on the within-system random-holdout control the compression vanishes. The AI "
            "authors compute_demonstration to measure both spreads and pre-registers the decision rule.",
            "dataset": "matbench_expt_gap",
            "method": "Magpie composition features; an AI-authored compute_demonstration that splits "
            "leave-chemical-system-out vs within-system random holdout (control), fits/queries a "
            "regressor, and computes a predicted-vs-true spread/compression statistic on each",
            "metrics": "the test statistic (predicted/true band-gap spread compression on held-out "
            "chemical systems) vs the control statistic (same compression on the within-system random "
            "holdout) — the AI pre-registers the supported/refuted thresholds",
            "success_criteria": "the program LEARNS: each round's confirm/refute reason shapes the "
            "next (narrow an over-claim, change a non-generalizing effect, scale a starved sample, or "
            "ablate a confirmed mechanism). A held + reproduced + audited demonstration is a win; an "
            "honest refute that moves the belief is ALSO a successful round.",
            "est_compute": "CPU-only, minutes per round",
        },
    )
    print(f"run_id={run_id} exp_id={exp_id}\n--- live events ---", flush=True)

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


if __name__ == "__main__":
    ts = time.strftime("%Y%m%dT%H%M%S")
    with tee_console("materials", ts) as log_path:
        asyncio.run(main(ts))
        print(f"console log: {log_path}", flush=True)
