"""auto_discovery.py — CLI over aletheia.research.discovery.

Runs the autonomy discovery loop on matbench_expt_gap (grok ideator + cross-vendor novelty gate,
Claude-free): generate bold candidates with code -> self-screen each on real data (code-gate ->
sandbox run -> hold -> non-trivial -> grounded -> novel) -> surface survivors, learning across rounds.
The logic lives in the package module (driver-importable); this is just the wiring + print.

    conda run -n aletheia python scripts/auto_discovery.py            # pure grok (Claude-free, in-session OK)
    conda run -n aletheia python scripts/auto_discovery.py --coauthor # grok ANGLE + Claude CODE (live-Claude!
                                                                      # run OUTSIDE the Claude Code session)
"""

from __future__ import annotations

import sys
import time

import numpy as np

from aletheia.config import get_settings
from aletheia.critics.gateway import CriticGateway
from aletheia.db import create_all
from aletheia.domains.materials.datasets import load_benchmark
from aletheia.domains.materials.featurizers import magpie_features
from aletheia.domains.materials.matbench_task import MaterialsBandGapPlugin
from aletheia.memory.service import create_run
from aletheia.research.discovery import (
    discover, make_claude_code_author, make_coauthor_ideator, make_vendor_ideator,
    materials_angle_context, materials_ideate_context,
)

K_SURVIVORS, MAX_ROUNDS = 2, 3


def main() -> int:
    print("=" * 92)
    print("AUTO-DISCOVERY — generate bold candidates + self-screen on real data (Claude-free)")
    print("=" * 92)
    t0 = time.time()
    plugin = MaterialsBandGapPlugin()
    df = load_benchmark("matbench_expt_gap")
    Xdf, _names, work = magpie_features(df, composition_col="composition")
    X = np.asarray(Xdf, dtype=float)
    y = work["gap expt"].to_numpy(dtype=float)
    groups = work["_comp_obj"].map(lambda c: c.chemical_system).to_numpy()
    print(f"staged matbench_expt_gap: n={len(y)}, d={X.shape[1]}  ({time.time()-t0:.1f}s)")
    create_all()
    run_id = create_run("auto-discovery", domain="materials", status="scoping")

    s = get_settings()
    vend = next((c for c in s.critics.active if c.id == "grok" and c.transport == "api"), None)
    if vend is None or not s.vendor_key("grok"):
        print("no grok credentials; cannot ideate")
        return 1
    coauthor = "--coauthor" in sys.argv
    base = (vend.model, s.vendor_base_url("grok") or vend.base_url, s.vendor_key("grok"))
    if coauthor:
        # grok proposes the ANGLE, Claude writes the code; novelty panel excludes BOTH authors.
        angle_ideate = make_vendor_ideator(*base, context=materials_angle_context(6))
        author = make_claude_code_author(run_id, dry_run=False)
        ideate = make_coauthor_ideator(angle_ideate, author, log=print)
        novelty_exclude = {"anthropic", "grok"}
        print("MODE: CO-AUTHOR (grok ANGLE + Claude CODE) — live-Claude in the loop.")
    else:
        ideate = make_vendor_ideator(*base, context=materials_ideate_context(6))
        novelty_exclude = {"anthropic"}
    print(f"\nLOOP: ideate -> auto-triage, up to {MAX_ROUNDS} rounds or {K_SURVIVORS} survivors "
          "(each round LEARNS from prior rejections).")
    survivors, rows = discover(ideate_fn=ideate, plugin=plugin, X=X, y=y, groups=groups,
                               gateway=CriticGateway(), run_id=run_id,
                               k_survivors=K_SURVIVORS, max_rounds=MAX_ROUNDS,
                               novelty_exclude=novelty_exclude)

    print("-" * 92)
    print(f"auto-triage: {len(survivors)}/{len(rows)} survived the FULL filter "
          "(ran clean + held + non-trivial + cleared the cross-vendor novelty gate + grounded).")
    for r in survivors:
        print(f"  ✅ {r['title']}\n     held test={r['test']} vs control={r['control']} "
              f"(n={r['n_test']}/{r['n_control']}); novelty={r.get('consensus','?')}; "
              f"grounding n_papers={r.get('n_papers','?')}")
    print("=" * 92)
    if survivors:
        print("SELF-DISCOVERED + SELF-VETTED (no human in the loop): novel (cross-vendor gate, author "
              "excluded) + feasible + non-trivial + grounded. Ready to hand to a live K2 campaign.")
    else:
        print("0 survivors — every bold candidate auto-killed (bad code / didn't run / didn't hold / "
              "trivial / not novel / ungrounded). The FULL rigor filter working: creativity is cheap; "
              "novel-AND-feasible-AND-grounded is the scarce part, now filtered automatically.")
    print(f"elapsed {time.time()-t0:.0f}s")
    return 0 if survivors else 2


if __name__ == "__main__":
    raise SystemExit(main())
