"""Real end-to-end PARADIGM run (molecules): execute the THREE-PART similarity-leakage LAW
that the prior paradigm run only set up the *premise* for (paper.md was rejected because the
law itself was never tested — only a single-model scaffold/random gap). The contribution is a
predictive FRAME, not a benchmark win: across a model grid scored under matched random vs
scaffold splits, (1) do the rankings disagree, (2) does a random-split-ONLY leakage-sensitivity
slope FORECAST each model's scaffold penalty, and (3) does similarity-matching reconcile the
rankings? IDEATE should pick contribution_type=paradigm and the ``leakage_slope_law`` capability
from the menu; the harness then COMPUTES it deterministically on real ESOL, the results gate
judges it in PARADIGM MODE (SOTA-delta irrelevant), and the formulation claim is grounded by the
reproducible demonstration — ending honestly as supported / refuted / unverified.

Real Opus + cross-vendor critic gates + real RDKit/sklearn. Spends Opus + critic credit.

    conda run -n aletheia python scripts/real_leakage_law_e2e.py
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
from aletheia.scheduler.driver import ExperimentDriver


def _short(p: dict, n: int = 170) -> str:
    s = ", ".join(f"{k}={v}" for k, v in (p or {}).items())
    return s if len(s) <= n else s[:n] + "…"


async def main() -> None:
    get_settings().max_experiments_per_campaign = 1
    create_all()
    run_id = create_run(
        "Real e2e (paradigm): does a random-split-only leakage slope forecast the scaffold penalty?",
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
            "objective": "Test the THREE-PART similarity-leakage LAW for aqueous-solubility "
            "regressors: across a MODEL GRID under matched random vs scaffold splits, (1) do the "
            "two protocols' model rankings DISAGREE, (2) does a random-split-ONLY leakage-"
            "sensitivity slope FORECAST each model's scaffold penalty, and (3) does restricting "
            "the random split to its most scaffold-like molecules RECONCILE the rankings?",
            "domain": "molecules",
            "direction": "a NEW PREDICTIVE EVALUATION FRAME (methodology), not a benchmark win — a "
            "paradigm contribution: a law that forecasts a model's scaffold-generalization penalty "
            "from random-split-only behaviour. This goes BEYOND the prior premise (that a gap "
            "exists) to a falsifiable predictive claim.",
            "hypothesis": "A random-split-only leakage-sensitivity slope (per-molecule error vs "
            "distance to the nearest training neighbour) forecasts each model's scaffold penalty "
            "(scaffold-minus-random RMSE) across a model grid; the two protocols' rankings disagree, "
            "and similarity-matching the random split reconciles them.",
            "contribution_type": "paradigm",
            "demonstration": "discriminating instance: across a five-model grid the random-split-only "
            "leakage slope predicts the scaffold penalty (positive rho with a bootstrap CI excluding "
            "zero), the random vs scaffold rankings disagree, and similarity-matching reconciles them "
            "— a predictive law the incumbent random-split metric cannot express",
            "dataset": "esol",
            "method": "RDKit Morgan fingerprints; a 5-model grid (ridge, knn, rf, gbm, svr) scored "
            "under matched random KFold vs scaffold GroupKFold; per-molecule leakage distance to the "
            "nearest training neighbour; OLS leakage slope; paired bootstrap CI on slope->penalty rho",
            "metrics": "slope->penalty correlation rho (95% bootstrap CI), ranking-disagreement "
            "Kendall tau, similarity-matched counterfactual tau",
            "success_criteria": "the random-split-only slope predicts the scaffold penalty (rho CI > 0), "
            "the rankings disagree, and the counterfactual reconciles them — the law holds; otherwise "
            "the run honestly reports the law REFUTED",
            "est_compute": "CPU-only, minutes",
        },
    )
    print(f"run_id={run_id} exp_id={exp_id}\n--- live events ---", flush=True)

    async def printer() -> None:
        async for evt in get_bus().subscribe():
            print(f">> {evt['type']}: {_short(evt.get('payload') or {})}", flush=True)

    pt = asyncio.create_task(printer())
    try:
        await ExperimentDriver(run_id, dry_run=False).run()
    finally:
        pt.cancel()

    run = get_run(run_id)
    print("\n--- SUMMARY ---", flush=True)
    print("run status:", run["status"])
    print("metrics:", list_metrics(exp_id))
    print("claims:", [(c["claim_type"], c["status"], c["strength"]) for c in list_claims(run_id)])


if __name__ == "__main__":
    asyncio.run(main())
