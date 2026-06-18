"""Step 1c — prove the Tc UPLOAD path composes end to end with the AI-authored demonstration:

  (a) a local-file (source="upload") Tc dataset loads 'material'/'critical_temp' EXPLICITLY via the
      propagated composition_column (no first-non-numeric guessing), and Magpie featurization returns
      aligned X / y / chemical-system groups;
  (b) a canned cuprate compute_demonstration — using ONLY groups-derivable quantities (Cu/O/
      alkaline-earth membership + a model's predictions) — runs through the harness AI-authored
      demonstration capability on a CONFIRM split of that Tc data, and the harness derives `holds`
      from the pre-registration + control (never an LLM assertion).

Self-contained: a small synthetic SuperCon-shaped CSV (cuprates + non-cuprates) in tmp_path, no
network, no figshare.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from aletheia.compute.mcp_tools import resolve_data_spec
from aletheia.config import get_settings
from aletheia.data.registry import attach_local
from aletheia.db import create_all
from aletheia.domains.materials.matbench_task import MaterialsBandGapPlugin
from aletheia.memory.service import create_run


def _synthetic_tc_csv(path) -> None:
    """~90 rows shaped like UCI unique_m.csv: a 'material' formula column + 'critical_temp' (K),
    a mix of multi-alkaline-earth cuprates and non-cuprates (enough of each for a confirm split)."""
    rng = np.random.default_rng(0)
    rows = []
    cup = ["La1.85Sr0.15CuO4", "Ba2YCu3O7", "Bi2Sr2CaCu2O8", "Tl2Ba2CuO6", "HgBa2CuO4",
           "Nd1.85Ce0.15CuO4", "YBa2Cu3O6.9", "Ca2SrCu2O5", "Ba2Ca2Cu3O8", "Sr2CaCu2O6"]
    non = ["MgB2", "Nb3Sn", "FeSe", "V3Si", "NbN", "Ba0.6K0.4Fe2As2", "LaFeAsO0.9F0.1",
           "Pb", "Nb", "Sn", "Al", "Si", "ZnO", "TiO2", "Fe2O3", "Cu2O"]
    for i in range(45):  # cuprates: higher Tc
        rows.append((cup[i % len(cup)], float(max(0.0, rng.normal(70, 20)))))
    for i in range(45):  # non-cuprates: lower Tc
        rows.append((non[i % len(non)], float(max(0.0, rng.normal(15, 10)))))
    rng.shuffle(rows)  # interleave so any contiguous split (e.g. confirm half) carries BOTH classes
    pd.DataFrame(rows, columns=["material", "critical_temp"]).to_csv(path, index=False)


# canned cuprate demonstration: cuprate stratum derived ONLY from `groups` (the chemical-system /
# element set) + the model's holdout error — exactly the groups-derivable design constraint.
_CUPRATE_DEMO = '''\
def compute_demonstration(X, y, groups, meta):
    import numpy as np
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.model_selection import train_test_split
    seed = int(meta.get("random_state", 0))
    X = np.asarray(X, dtype=float); y = np.asarray(y, dtype=float)
    sysarr = np.asarray(groups, dtype=object)
    AE = {"Ba", "Sr", "Ca", "Mg", "Be"}
    def is_cuprate(s):
        els = set(str(s).split("-"))
        return ("Cu" in els) and ("O" in els) and len(els & AE) >= 1
    cup = np.array([is_cuprate(s) for s in sysarr])
    tr, te = train_test_split(np.arange(len(y)), test_size=0.45, random_state=seed)
    m = GradientBoostingRegressor(n_estimators=40, max_depth=3, random_state=0).fit(X[tr], y[tr])
    err = np.abs(m.predict(X[te]) - y[te])
    cup_te, non_te = cup[te], ~cup[te]
    test_stat = float(np.mean(err[cup_te]) - np.mean(err[non_te]))
    nidx = np.where(non_te)[0]; rng = np.random.default_rng(seed); rng.shuffle(nidx)
    a, b = nidx[: len(nidx) // 2], nidx[len(nidx) // 2:]
    ctrl = float(abs(np.mean(err[a]) - np.mean(err[b])))
    return {"test_statistic": test_stat, "control_statistic": ctrl, "components": {},
            "detail": "cuprate excess holdout MAE vs non-cuprate; control = non-cuprate split-half",
            "n_test": int(cup_te.sum()), "n_control": int(non_te.sum())}
'''

_PREREG = {
    "statistic_name": "cuprate_excess_mae",
    "computation": "holdout MAE on cuprates minus non-cuprates",
    "control_description": "non-cuprate split-half MAE difference",
    "expected_control": "two non-cuprate halves are exchangeable, so the difference ~ 0",
    "supported_if": {"op": ">=", "threshold": 0.0},
    "control_silent_if": {"op": "<=", "threshold": 1e6},
}


def test_upload_tc_path_loads_and_featurizes(tmp_path):
    """(a) explicit composition_column flows through the upload registration -> data_spec -> the
    plugin loads 'material'/'critical_temp' and featurizes to aligned X / y / chemical-system groups."""
    create_all()
    run_id = create_run("tc-upload", domain="materials", status="scoping")
    csv = tmp_path / "supercon_mini.csv"
    _synthetic_tc_csv(csv)
    attach_local(run_id, str(csv), target_column="critical_temp", composition_column="material")

    spec = resolve_data_spec(run_id)
    assert spec["source"] == "upload" and spec["composition_column"] == "material"
    assert spec["target_column"] == "critical_temp"

    plugin = MaterialsBandGapPlugin()
    df = plugin.load_data(spec)
    X, y, names, groups = plugin.featurize(df, {})
    assert X.shape[0] == len(y) == len(groups) and X.shape[1] > 100
    assert any("Cu" in str(g).split("-") for g in groups)   # cuprate chemical systems present


def test_canned_cuprate_demo_runs_through_harness_confirm_split(tmp_path, monkeypatch):
    """(b) the canned cuprate demonstration (groups-derivable) runs via _compute_ai_authored_demonstration
    on a CONFIRM split of the uploaded Tc data; the harness derives `holds` from prereg + control."""
    monkeypatch.setattr(get_settings(), "demonstration_min_samples", 5)  # small synthetic strata
    create_all()
    run_id = create_run("tc-demo", domain="materials", status="scoping")
    csv = tmp_path / "supercon_mini.csv"
    _synthetic_tc_csv(csv)
    attach_local(run_id, str(csv), target_column="critical_temp", composition_column="material")
    spec = resolve_data_spec(run_id)

    plugin = MaterialsBandGapPlugin()
    X, _y, _n, _g = plugin.featurize(plugin.load_data(spec), {})
    n = X.shape[0]
    confirm_index = list(range(n // 2, n))                  # held-out CONFIRM partition

    demo = {
        "demonstration_code": _CUPRATE_DEMO, "preregistration": _PREREG,
        "form": "discriminating_instance", "random_state": 0,
        "confirm_index": confirm_index, "split_meta": {"split_algo_version": 1},
    }
    res = plugin._compute_ai_authored_demonstration(demo, spec)
    assert isinstance(res, dict)
    assert res.get("exploration_applied") is True           # ran on the confirm split
    assert res.get("holds") in (True, False)                # harness-derived verdict (not None/error)
    assert isinstance(res.get("test_statistic"), float)
    assert "preregistration" in res
