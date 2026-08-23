"""Real end-to-end PARADIGM run for gap #3 (activity cliffs as a representational
IMPOSSIBILITY). The contribution is a new framing — that near-identical molecules with
large property gaps force a local Lipschitz constant orders of magnitude larger than the
smooth-region constant, so a smooth/continuous representation cannot satisfy both the cliff
and global generalization (an impossibility, not data scarcity). The harness COMPUTES this
discriminating demonstration deterministically on ESOL Morgan fingerprints; the cross-vendor
(Claude + Grok) results gate judges it in paradigm mode (SOTA-delta irrelevant).

    conda run -n aletheia python scripts/real_paradigm_cliff_e2e.py
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
        "Real e2e (paradigm): are molecular activity cliffs a representational IMPOSSIBILITY?",
        domain="molecules", status="scoping", budget_cap_usd=50.0,
    )
    register_dataset(
        run_id, "benchmark", ref="esol",
        target_column="measured log solubility in mols per litre", status="ready",
    )
    exp_id = finalize_plan(
        run_id,
        {
            "objective": "Show whether molecular activity cliffs are a representational "
            "impossibility for smooth learned/continuous representations, not a data-scarcity problem",
            "domain": "molecules",
            "direction": "a NEW FRAMING (paradigm), not a benchmark win: reframe the activity-cliff "
            "failure as an intrinsic Lipschitz trade-off of continuous representations",
            "hypothesis": "Near-identical molecules (high Tanimoto) with large property gaps force a "
            "local Lipschitz constant orders of magnitude larger than the smooth-region constant, so a "
            "single continuous regressor cannot satisfy both the cliff and global generalization.",
            "contribution_type": "paradigm",
            "demonstration": {
                "form": "impossibility",
                "claim": "for activity-cliff matched pairs the implied local Lipschitz constant "
                "L_cliff = |Δproperty| / structural-distance is orders of magnitude larger than the "
                "smooth-region L_global; fitting the cliffs forces an L incompatible with global "
                "generalization — a representational impossibility, not data scarcity",
            },
            "dataset": "esol",
            "method": "RDKit Morgan fingerprints; pairwise Tanimoto; compare cliff-pair vs "
            "smooth-region implied local Lipschitz constants",
            "metrics": "L_cliff / L_global ratio over near-identical pairs (the impossibility frontier)",
            "success_criteria": "the new framing reveals an intrinsic Lipschitz trade-off the "
            "incumbent 'cliffs are a modeling/data problem' frame misses",
            "est_compute": "CPU-only, seconds",
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
