"""real_discovery_campaign_e2e.py — the AUTONOMOUS ARC: discover -> demonstrate -> audit -> believe.

With ``discovery_enabled``, the driver's IDEATE stage runs the autonomous discovery loop
(aletheia.research.discovery): generate bold candidates WITH code -> self-screen each on real data
(code-gate -> sandbox run -> hold -> non-trivial -> grounded -> cross-vendor novelty gate) ->
adopt a survivor's hypothesis + its already-verified demonstration. The direction gate is then skipped
(the loop already cleared the novelty gate) and the campaign runs the discovered demonstration through
the harness confirm/compute -> cross-vendor audit -> belief update -> cross-round learning. So the
whole arc is autonomous: the system both PROPOSES and VETS the science, harness-owned throughout.

IMPORTANT — run OUTSIDE the Claude Code session (the campaign adds live Claude SDK traffic that can
trip the AUP classifier in a large coding context). The discovery loop itself is Claude-free (the
ideator + novelty audit exclude the author), but the downstream demonstration authoring/critique is
live Claude.

    conda run -n aletheia python scripts/real_discovery_campaign_e2e.py
"""

from __future__ import annotations

import asyncio
import time

from _e2e_common import tee_console

from aletheia.config import get_settings
from aletheia.data.registry import register_dataset
from aletheia.db import create_all
from aletheia.memory.service import create_run, finalize_plan
from real_k2_campaign_e2e import _drive_and_report, _k2_campaign_settings


async def main(timestamp: str) -> None:
    _k2_campaign_settings()
    get_settings().discovery_enabled = True   # the IDEATE stage runs the discovery loop
    create_all()
    run_id = create_run(
        "Real e2e (AUTONOMOUS DISCOVERY): the system generates bold candidates, self-screens them "
        "(feasible + non-trivial + grounded + novel) on real band-gap data, and runs a survivor "
        "through demonstrate -> audit -> belief — proposing AND vetting the science itself.",
        domain="materials", status="scoping", budget_cap_usd=150.0,
    )
    register_dataset(run_id, "benchmark", ref="matbench_expt_gap",
                     target_column="gap expt", status="ready")
    exp_id = finalize_plan(run_id, {
        "objective": "Autonomously DISCOVER + verify a chemistry-specific failure mode of a Magpie "
        "band-gap model. The discovery stage proposes bold candidates with code, self-screens them "
        "(code-gate -> runs+holds -> non-trivial -> grounded -> cross-vendor novelty gate), and the "
        "campaign runs a survivor through demonstrate -> audit -> belief. The hypothesis + "
        "demonstration come from discovery, NOT a hand-written plan.",
        "domain": "materials",
        "direction": "autonomous discovery -> guarded campaign. The discovery loop clears the novelty "
        "gate (author excluded) and verifies the demonstration HOLDS before adoption; the harness then "
        "applies the pre-registered rule + control + cross-vendor audit; the campaign LEARNS across rounds.",
        "dataset": "matbench_expt_gap",
        "success_criteria": "the discovery stage banks >=1 novel-AND-feasible-AND-grounded survivor; the "
        "campaign demonstrates + audits it and moves a calibrated belief. An honest refute that moves "
        "belief is ALSO a successful round. (A single round is a strong PARTIAL, not FULL.)",
        "est_compute": "CPU-only; minutes per round (discovery loop + RF demonstrations + audit)",
    })
    print(f"run_id={run_id} exp_id={exp_id} discovery_enabled=True\n--- live events ---", flush=True)
    await _drive_and_report(run_id, exp_id, timestamp)


if __name__ == "__main__":
    ts = time.strftime("%Y%m%dT%H%M%S")
    with tee_console("materials", ts) as log_path:
        asyncio.run(main(ts))
        print(f"console log: {log_path}", flush=True)
