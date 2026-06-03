"""Conversational pre-research: survey a direction and propose RANKED genuine open gaps
for a HUMAN to review + pick before an autonomous run is launched from the chosen gap.

    conda run -n aletheia python scripts/propose_gaps.py "<domain>" "<direction>"

Real Opus + real literature (arXiv/OpenAlex). Prints structured ranked gaps; the human
then picks one, which is seeded into a run's plan (contribution_type + demonstration).
"""

from __future__ import annotations

import asyncio
import sys

from aletheia.db import create_all
from aletheia.memory.service import create_run
from aletheia.research.gaps import discover_gaps


async def main() -> None:
    domain = sys.argv[1] if len(sys.argv) > 1 else "molecules"
    direction = sys.argv[2] if len(sys.argv) > 2 else "open problems in molecular property prediction"
    create_all()
    run_id = create_run(f"gap discovery: {direction}", domain=domain, status="scoping")
    print(f"run_id={run_id}\nsurveying + ranking genuine open gaps for: {domain} / {direction}\n", flush=True)
    gaps = await discover_gaps(run_id, domain, direction, k_gaps=5)
    if not gaps:
        print("(no gaps produced — survey/LLM degraded or returned nothing)", flush=True)
        return
    print(f"=== {len(gaps)} candidate gaps (ranked, most-open first) ===\n", flush=True)
    for g in gaps:
        cf = g.get("candidate_framing") or {}
        print(f"[{g.get('rank')}] {g.get('statement')}", flush=True)
        print(f"    why open : {g.get('why_open')}", flush=True)
        print(f"    framing  : {cf.get('contribution_type')} — {cf.get('demonstration')}", flush=True)
        print(f"    scores   : openness={g.get('openness')} feasibility={g.get('feasibility')}", flush=True)
        print(f"    evidence : {g.get('evidence')}\n", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
