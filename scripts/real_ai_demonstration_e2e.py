"""Real end-to-end PARADIGM run (molecules) on the FRONTIER PATH: the AI AUTHORS the
discriminating demonstration itself (sandboxed ``compute_demonstration`` code), not a hand-built
registered capability. The paradigm question is phrased so it does NOT match any registered
capability keyword, so the driver's ``_demonstration_code`` stage authors the computation, the
harness applies the AI's PRE-REGISTERED decision rule + NEGATIVE CONTROL + leakage/degeneracy
probes (never an LLM 'holds'), an independent cross-vendor audit (Opus author EXCLUDED) reviews
the code, and the formulation claim is grounded honestly (supported / refuted / unverified /
not_evaluated).

Real Opus (ideation + code authoring) + cross-vendor critic/audit gates + real RDKit/sklearn.
Spends Opus + critic credit. CPU-only demonstration compute is seconds-to-minutes.

    conda run -n aletheia python scripts/real_ai_demonstration_e2e.py
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
from aletheia.scheduler.durable import run_legacy_driver_inline_compat


def _short(p: dict, n: int = 170) -> str:
    s = ", ".join(f"{k}={v}" for k, v in (p or {}).items())
    return s if len(s) <= n else s[:n] + "…"


async def main(timestamp: str) -> None:
    get_settings().max_experiments_per_campaign = 1
    # FRONTIER OVERRIDE: force the AI-authored demonstration path even if IDEATE reframes the claim
    # into something a registered capability would keyword-match. Without this, routing is
    # registered-first and the hand-built cliff/scaffold/law capabilities pre-empt AI authoring.
    get_settings().demonstration_prefer_authored = True
    create_all()
    run_id = create_run(
        "Real e2e (AI-authored demonstration): is a regressor's local sensitivity to a single "
        "halogen substitution structurally bounded?",
        domain="molecules",
        status="scoping",
        budget_cap_usd=50.0,
    )
    register_dataset(
        run_id, "benchmark", ref="esol",
        target_column="measured log solubility in mols per litre", status="ready",
    )
    exp_id = finalize_plan(
        run_id,
        {
            "objective": "Establish whether a continuous solubility regressor on Morgan "
            "fingerprints has a STRUCTURALLY BOUNDED local sensitivity to a single halogen "
            "substitution, so it cannot represent halogenation-driven solubility jumps. The AI "
            "must AUTHOR the discriminating computation and a NEGATIVE CONTROL.",
            "domain": "molecules",
            "direction": "a NEW EVALUATION FRAME (methodology), not a benchmark win — a paradigm "
            "contribution whose discriminating demonstration the AI authors itself. The harness "
            "applies the AI's pre-registered decision rule + negative control; an independent "
            "cross-vendor audit (author excluded) reviews the code for leakage/confounds.",
            "hypothesis": "Among near-identical molecule pairs that differ ONLY by a single halogen "
            "substitution, the regressor's predicted solubility change is bounded far below the "
            "observed change (a structural sensitivity ceiling); the CONTROL — pairs that differ by "
            "a single NON-halogen substitution — should show no such gap if the effect is "
            "halogen-specific.",
            "contribution_type": "paradigm",
            "demonstration": "discriminating instance: on single-halogen-substitution pairs the "
            "model's predicted Δsolubility is structurally bounded below the true Δsolubility (a "
            "ceiling the incumbent error metric cannot see); on the non-halogen control the gap "
            "vanishes. The AI authors compute_demonstration to measure both and pre-registers the "
            "decision rule.",
            "dataset": "esol",
            "method": "RDKit Morgan fingerprints; an AI-authored compute_demonstration that finds "
            "near-identical pairs differing by one substitution, splits halogen vs non-halogen "
            "(control), fits/queries a regressor, and computes the predicted-vs-true Δ gap on each",
            "metrics": "the test statistic (predicted/true Δsolubility sensitivity gap on halogen "
            "pairs) vs the control statistic (same gap on non-halogen pairs) — the AI pre-registers "
            "the supported/refuted thresholds",
            "success_criteria": "the sensitivity gap is large on halogen-substitution pairs AND "
            "vanishes on the non-halogen control (and survives the leakage probes + independent "
            "audit); otherwise the run honestly reports the formulation refuted/unverified",
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
        await run_legacy_driver_inline_compat(run_id, dry_run=False)
    finally:
        await asyncio.sleep(0.1)  # let the final events drain into the recorder
        pt.cancel()

    run = get_run(run_id)
    print("\n--- SUMMARY ---", flush=True)
    print("run status:", run["status"])
    print("metrics:", list_metrics(exp_id))
    print("claims:", [(c["claim_type"], c["status"], c["strength"]) for c in list_claims(run_id)])

    # auditable artifact: one JSON file capturing route fired + demonstration + audit + ledger.
    summary_path = write_e2e_summary(
        run_id=run_id, exp_id=exp_id, timestamp=timestamp,
        prefer_authored=get_settings().demonstration_prefer_authored,
        recorder=recorder, domain="molecules",
    )
    print(f"summary written: {summary_path}", flush=True)
    print_usage(run_id)


if __name__ == "__main__":
    ts = time.strftime("%Y%m%dT%H%M%S")
    with tee_console("molecules", ts) as log_path:
        asyncio.run(main(ts))
        print(f"console log: {log_path}", flush=True)
