"""Real end-to-end: the full autonomous loop on matbench_expt_gap with real Opus
reasoning + real GPT-5.5 critic gates + real training. Prints events live and a
final summary. Spends Opus + Coding-Plan credit.

    conda run -n aletheia python scripts/real_e2e.py
"""

from __future__ import annotations

import asyncio

from aletheia.data.registry import register_dataset
from aletheia.db import create_all
from aletheia.events.bus import get_bus
from aletheia.memory.service import (
    create_run,
    finalize_plan,
    get_run,
    list_artifacts,
    list_metrics,
)
from aletheia.scheduler.driver import ExperimentDriver


def _short(p: dict, n: int = 160) -> str:
    s = ", ".join(f"{k}={v}" for k, v in (p or {}).items())
    return s if len(s) <= n else s[:n] + "…"


async def main() -> None:
    create_all()
    run_id = create_run(
        "Real e2e: predict experimental band gap from composition",
        domain="materials",
        status="scoping",
        budget_cap_usd=50.0,
    )
    register_dataset(
        run_id, "benchmark", ref="matbench_expt_gap", target_column="gap expt", status="ready"
    )
    exp_id = finalize_plan(
        run_id,
        {
            "objective": "Predict experimental band gap from chemical composition",
            "domain": "materials",
            "direction": "composition-based property prediction",
            "hypothesis": "Magpie composition features + a tree regressor predict band gap.",
            "dataset": "matbench_expt_gap",
            "method": "matminer Magpie features -> RandomForest/GBM",
            "baselines": "mean predictor; RandomForest",
            "metrics": "MAE, R2, RMSE on a held-out split",
            "success_criteria": "beat the mean baseline",
            "risks": "composition-only ceiling; leakage across split",
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
    print("artifacts:", list_artifacts(exp_id))


if __name__ == "__main__":
    asyncio.run(main())
