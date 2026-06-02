"""Real end-to-end on the RAG (eval-only, host-side) domain — the path that, before
the domain-aware artifact contract, was BLOCKED in real mode (it emits only an `eval`
artifact, not a fitted `model`). This proves the real RAG run now reaches analysis.

Real Opus reasoning + real cross-vendor critic gates + a real host-side LLM answerer.
Dense retrieval uses the local MiniLM embedder (CPU, offline once cached). Spends
Opus + Codex/critic credit. Prints events live; monitor the DB if stdout buffers.

    conda run -n aletheia python scripts/real_rag_e2e.py
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
from aletheia.scheduler.driver import ExperimentDriver


def _short(p: dict, n: int = 160) -> str:
    s = ", ".join(f"{k}={v}" for k, v in (p or {}).items())
    return s if len(s) <= n else s[:n] + "…"


async def main() -> None:
    # keep it to a single experiment round — we are verifying the real path, not breadth
    get_settings().max_experiments_per_campaign = 1
    create_all()
    run_id = create_run(
        "Real e2e (RAG): does dense retrieval raise answer F1 on a paraphrase QA set?",
        domain="rag",
        status="scoping",
        budget_cap_usd=25.0,
    )
    register_dataset(run_id, "benchmark", ref="paraphrase-qa", status="ready")
    exp_id = finalize_plan(
        run_id,
        {
            "objective": "evaluate a retrieval-augmented QA configuration",
            "domain": "rag",
            "hypothesis": "Dense (embedding) retrieval recovers paraphrased gold passages "
            "that lexical overlap misses, raising answer F1.",
            "dataset": "paraphrase-qa",
            "method": "dense (embedding) retrieval + host-side LLM extractive answer",
            "baselines": "lexical token-overlap retrieval",
            "metrics": "answer F1, recall@k, faithfulness",
            "success_criteria": "beat the lexical baseline answer F1",
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
    print("claims:", [(c["claim_type"], c["status"], c["strength"]) for c in list_claims(run_id)])


if __name__ == "__main__":
    asyncio.run(main())
