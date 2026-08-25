"""Real end-to-end PARADIGM run (molecules): the contribution is a new EVALUATION FRAME,
not a benchmark win. The research question is methodological — "is random-split RMSE blind
to scaffold generalization for molecular solubility?" — so ideation should choose
contribution_type=paradigm and name a discriminating demonstration. The harness then COMPUTES
that demonstration deterministically on real ESOL (random-split vs scaffold-grouped RMSE),
the results gate judges it in PARADIGM MODE (SOTA-delta irrelevant), and the formulation claim
is grounded by the reproducible demonstration.

Real Opus + cross-vendor critic gates + real RDKit/sklearn. Spends Opus + critic credit.

    conda run -n aletheia python scripts/real_paradigm_e2e.py
"""

from __future__ import annotations

import asyncio

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


async def main() -> None:
    get_settings().max_experiments_per_campaign = 1
    create_all()
    run_id = create_run(
        "Real e2e (paradigm): is random-split RMSE blind to scaffold generalization?",
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
            "objective": "Show whether the standard random-split RMSE used for molecular "
            "solubility is BLIND to generalization across chemical scaffolds",
            "domain": "molecules",
            "direction": "a NEW EVALUATION FRAME (methodology), not a benchmark win — this is a "
            "paradigm contribution: change how solubility models are judged",
            "hypothesis": "Random-split RMSE cannot certify generalization: a model with strong "
            "random-split RMSE is materially worse under a scaffold-grouped split, so the "
            "incumbent metric is blind to the scaffold-generalization gap.",
            "contribution_type": "paradigm",
            "demonstration": "discriminating instance: a model the random-split metric rates good "
            "fails on leave-whole-scaffold-out evaluation (a gap the incumbent metric cannot see)",
            "dataset": "esol",
            "method": "RDKit Morgan fingerprints; compare random-holdout RMSE vs scaffold-grouped RMSE",
            "metrics": "RMSE under random split vs scaffold-grouped split (the gap is the evidence)",
            "success_criteria": "the new frame reveals a generalization gap the incumbent metric hides",
            "est_compute": "CPU-only, minutes",
        },
    )
    print(f"run_id={run_id} exp_id={exp_id}\n--- live events ---", flush=True)

    async def printer() -> None:
        async for evt in get_bus().subscribe():
            print(f">> {evt['type']}: {_short(evt.get('payload') or {})}", flush=True)

    pt = asyncio.create_task(printer())
    try:
        await run_legacy_driver_inline_compat(run_id, dry_run=False)
    finally:
        pt.cancel()

    run = get_run(run_id)
    print("\n--- SUMMARY ---", flush=True)
    print("run status:", run["status"])
    print("metrics:", list_metrics(exp_id))
    print("claims:", [(c["claim_type"], c["status"], c["strength"]) for c in list_claims(run_id)])


if __name__ == "__main__":
    asyncio.run(main())
