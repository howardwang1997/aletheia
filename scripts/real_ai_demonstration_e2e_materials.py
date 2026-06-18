"""Real end-to-end PARADIGM run on a SECOND domain (materials) via the FRONTIER PATH: the AI
AUTHORS the discriminating demonstration itself (sandboxed ``compute_demonstration``), proving
the AI-authored path is domain-GENERAL rather than molecules-only. The paradigm question is
phrased so it does NOT match any registered capability keyword, and ``demonstration_prefer_authored``
is forced, so the driver's ``_demonstration_code`` stage authors the computation; the harness
applies the AI's PRE-REGISTERED decision rule + NEGATIVE CONTROL + leakage/degeneracy probes
(never an LLM 'holds'); an independent cross-vendor audit (Opus author EXCLUDED) reviews the code;
and the formulation claim is grounded honestly (supported / refuted / unverified / not_evaluated).

Materials supplies composition→band-gap data (matbench_expt_gap) with Magpie features and
chemical-system groups, so the leakage frame here is about generalization ACROSS chemical
systems rather than across molecular scaffolds.

Real Opus (ideation + code authoring) + cross-vendor critic/audit gates + real Magpie/sklearn.
Spends Opus + critic credit. CPU-only demonstration compute is seconds-to-minutes.

IMPORTANT — run this OUTSIDE the Claude Code session you used to build it. A live run adds its
own Claude SDK traffic on top of an already large, policy-sensitive coding context, which can
trip Anthropic's upstream AUP classifier into "unable to respond" API errors (a context-level
false positive — see docs/CLAUDE_CODE_AUP_FALSE_POSITIVE_NOTES_2026_06_04.md). Run it in a plain
terminal:

    conda run -n aletheia python scripts/real_ai_demonstration_e2e_materials.py

then inspect the single JSON summary printed at the end (under ``artifacts/``).
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
    list_metrics,
)
from aletheia.scheduler.driver import ExperimentDriver


def _short(p: dict, n: int = 170) -> str:
    s = ", ".join(f"{k}={v}" for k, v in (p or {}).items())
    return s if len(s) <= n else s[:n] + "…"


async def main(timestamp: str) -> None:
    get_settings().max_experiments_per_campaign = 1
    # FRONTIER OVERRIDE: force the AI-authored demonstration path. Materials registers NO
    # hand-built demonstration capabilities, so without an authored demonstration a paradigm
    # claim here stays a proposal — this override is what makes the second-domain test meaningful.
    get_settings().demonstration_prefer_authored = True
    create_all()
    run_id = create_run(
        "Real e2e (AI-authored demonstration, materials): is a composition→band-gap regressor's "
        "predicted gap for a held-out chemical system structurally pinned to its training "
        "neighbors, so it cannot track gaps in unseen chemistries?",
        domain="materials",
        status="scoping",
        budget_cap_usd=50.0,
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
            "represent chemistry-driven gap jumps. The AI must AUTHOR the discriminating computation "
            "and a NEGATIVE CONTROL.",
            "domain": "materials",
            "direction": "a NEW EVALUATION FRAME (methodology), not a benchmark win — a paradigm "
            "contribution whose discriminating demonstration the AI authors itself. The harness "
            "applies the AI's pre-registered decision rule + negative control; an independent "
            "cross-vendor audit (author excluded) reviews the code for leakage/confounds.",
            "hypothesis": "When whole chemical systems are held out, the regressor's predicted "
            "band-gap range collapses toward its training-neighbor gaps (a structural extrapolation "
            "ceiling), so the predicted spread across held-out systems is far below the true spread; "
            "the CONTROL — holding out random rows WITHIN seen systems — should show no such collapse "
            "if the effect is genuinely about unseen chemistry rather than sample size.",
            "contribution_type": "paradigm",
            "demonstration": "discriminating instance: under leave-chemical-system-out the model's "
            "predicted band-gap spread on held-out systems is structurally compressed relative to the "
            "true spread (a ceiling the incumbent random-split error metric cannot see); on the "
            "within-system random-holdout control the compression vanishes. The AI authors "
            "compute_demonstration to measure both spreads and pre-registers the decision rule.",
            "dataset": "matbench_expt_gap",
            "method": "Magpie composition features; an AI-authored compute_demonstration that splits "
            "leave-chemical-system-out vs within-system random holdout (control), fits/queries a "
            "regressor, and computes a predicted-vs-true spread/compression statistic on each",
            "metrics": "the test statistic (predicted/true band-gap spread compression on held-out "
            "chemical systems) vs the control statistic (same compression on the within-system random "
            "holdout) — the AI pre-registers the supported/refuted thresholds",
            "success_criteria": "the compression is large under leave-chemical-system-out AND vanishes "
            "on the within-system control (and survives the leakage probes + independent audit); "
            "otherwise the run honestly reports the formulation refuted/unverified",
            "est_compute": "CPU-only, minutes",
        },
    )
    print(f"run_id={run_id} exp_id={exp_id}\n--- live events ---", flush=True)

    recorder = RunRecorder()

    async def printer() -> None:
        async for evt in get_bus().subscribe():
            recorder.observe(evt)  # accumulate for the machine-readable summary
            print(f">> {evt['type']}: {_short(evt.get('payload') or {})}", flush=True)

    pt = asyncio.create_task(printer())
    try:
        await ExperimentDriver(run_id, dry_run=False).run()
    finally:
        await asyncio.sleep(0.1)  # let the final events drain into the recorder
        pt.cancel()

    run = get_run(run_id)
    print("\n--- SUMMARY ---", flush=True)
    print("run status:", run["status"])
    print("metrics:", list_metrics(exp_id))
    print("claims:", [(c["claim_type"], c["status"], c["strength"]) for c in list_claims(run_id)])

    summary_path = write_e2e_summary(
        run_id=run_id, exp_id=exp_id, timestamp=timestamp,
        prefer_authored=get_settings().demonstration_prefer_authored,
        recorder=recorder, domain="materials",
    )
    print(f"summary written: {summary_path}", flush=True)
    print_usage(run_id)


if __name__ == "__main__":
    ts = time.strftime("%Y%m%dT%H%M%S")
    with tee_console("materials", ts) as log_path:
        asyncio.run(main(ts))
        print(f"console log: {log_path}", flush=True)
