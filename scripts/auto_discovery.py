"""auto_discovery.py — CLI over aletheia.research.discovery.

Runs the autonomy discovery loop on the sealed EXPLORE partition of matbench_expt_gap (vendor
ideator + cross-vendor novelty gate): generate bold candidates with code -> self-screen each on
exploration data (code-gate -> sandbox run -> signal -> grounded -> novel) -> surface candidates for
later confirmation, learning across rounds.
The logic lives in the package module (driver-importable); this is just the wiring + print.

    conda run -n aletheia python scripts/auto_discovery.py
    conda run -n aletheia python scripts/auto_discovery.py --coauthor
        # grok proposes angles; the configured Claude/GPT orchestrator writes code
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
    discover, make_coauthor_ideator, make_vendor_ideator, make_worker_code_author,
    materials_angle_context, materials_ideate_context,
)

K_SURVIVORS, MAX_ROUNDS = 2, 3


def main() -> int:
    print("=" * 92)
    print("AUTO-DISCOVERY — generate bold candidates + screen on sealed exploration data")
    print("=" * 92)
    t0 = time.time()
    plugin = MaterialsBandGapPlugin()
    df = load_benchmark("matbench_expt_gap")
    Xdf, _names, work = magpie_features(df, composition_col="composition")
    X = np.asarray(Xdf, dtype=float)
    y = work["gap expt"].to_numpy(dtype=float)
    groups = work["_comp_obj"].map(lambda c: c.chemical_system).to_numpy()
    split = plugin._split_explore_confirm(groups, len(y), 42)
    if split is None:
        print("dataset cannot support an honest explore/confirm split")
        return 1
    ex = split["explore_idx"]
    X, y, groups = X[ex], y[ex], groups[ex]
    print(f"staged SEALED explore partition: n={len(y)}, d={X.shape[1]}, "
          f"confirm_n={split['meta']['n_confirm']} ({time.time()-t0:.1f}s)")
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
        # grok proposes the angle; the configured orchestrator writes code; exclude both authors.
        angle_ideate = make_vendor_ideator(*base, context=materials_angle_context(6))
        author = make_worker_code_author(run_id, dry_run=False)
        ideate = make_coauthor_ideator(angle_ideate, author, log=print)
        novelty_exclude = {"grok", s.orchestrator_vendor}
        print(f"MODE: CO-AUTHOR (grok ANGLE + {s.orchestrator_provider} CODE).")
    else:
        ideate = make_vendor_ideator(*base, context=materials_ideate_context(6))
        novelty_exclude = {"grok"}
    print(f"\nLOOP: ideate -> auto-triage, up to {MAX_ROUNDS} rounds or {K_SURVIVORS} survivors "
          "(each round LEARNS from prior rejections).")
    survivors, rows = discover(ideate_fn=ideate, plugin=plugin, X=X, y=y, groups=groups,
                               gateway=CriticGateway(), run_id=run_id,
                               k_survivors=K_SURVIVORS, max_rounds=MAX_ROUNDS,
                               novelty_exclude=novelty_exclude)

    print("-" * 92)
    print(f"auto-triage: {len(survivors)}/{len(rows)} survived the EXPLORATORY filter "
          "(ran clean + signal + cross-vendor novelty gate + grounded).")
    for r in survivors:
        print(f"  ✅ {r['title']}\n     exploratory test={r['test']} vs control={r['control']} "
              f"(n={r['n_test']}/{r['n_control']}); novelty={r.get('consensus','?')}; "
              f"grounding n_papers={r.get('n_papers','?')}")
    print("=" * 92)
    if survivors:
        print("SELF-DISCOVERED exploratory candidates: novel (author excluded), feasible and grounded. "
              "They are NOT confirmed until an untouched CONFIRM partition independently holds.")
    else:
        print("0 survivors — every bold candidate auto-killed (bad code / no exploratory signal / "
              "trivial / not novel / ungrounded). The FULL rigor filter working: creativity is cheap; "
              "novel-AND-feasible-AND-grounded is the scarce part, now filtered automatically.")
    print(f"elapsed {time.time()-t0:.0f}s")
    return 0 if survivors else 2


if __name__ == "__main__":
    raise SystemExit(main())
