"""Real end-to-end on the MOLECULES domain (drug-discovery / chem-bio): predict
aqueous solubility (logS) on MoleculeNet ESOL from SMILES, under the field's
scaffold-grouped split (the leakage-aware protocol MoleculeNet reports). This is the
2nd science domain whose REAL path has never been exercised end-to-end.

Real Opus reasoning + real cross-vendor critic gates + real RDKit featurization +
real Docker-sandbox training. Spends Opus + critic credit. Monitor the DB if stdout
buffers under `conda run`.

    conda run -n aletheia python scripts/real_molecules_e2e.py
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
    list_artifacts,
    list_claims,
    list_metrics,
)
from aletheia.scheduler.durable import run_legacy_driver_inline_compat


def _short(p: dict, n: int = 160) -> str:
    s = ", ".join(f"{k}={v}" for k, v in (p or {}).items())
    return s if len(s) <= n else s[:n] + "…"


async def main() -> None:
    get_settings().max_experiments_per_campaign = 1  # verify the real path, not breadth
    create_all()
    run_id = create_run(
        "Real e2e (molecules): predict aqueous solubility (logS) from SMILES on ESOL",
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
            "objective": "Predict aqueous solubility (logS) of small molecules from structure",
            "domain": "molecules",
            "direction": "structure-based molecular property prediction",
            "hypothesis": "Morgan/ECFP fingerprints + a gradient-boosted-tree regressor "
            "predict logS, generalizing across Bemis-Murcko scaffolds.",
            "dataset": "esol",
            "method": "RDKit Morgan/ECFP fingerprints -> gradient boosting regressor",
            "baselines": "mean predictor; random forest on ECFP",
            "metrics": "scaffold-grouped RMSE, MAE, R2 on a held-out scaffold split",
            "success_criteria": "beat the mean baseline; approach the ESOL RF/ECFP SOTA",
            "risks": "scaffold leakage; fingerprint ceiling vs a learned representation",
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
    print("artifacts:", list_artifacts(exp_id))
    print("claims:", [(c["claim_type"], c["status"], c["strength"]) for c in list_claims(run_id)])


if __name__ == "__main__":
    asyncio.run(main())
