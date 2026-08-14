"""Paradigm-mode P5 on a SECOND domain (materials) — proving the AI-authored demonstration
path is domain-GENERAL, not molecules-only.

The generic ``_compute_ai_authored_demonstration`` (domains/base.py) stages data via the
domain's ``load_data`` + ``featurize`` into ``(X, y, groups)``, runs the AI-authored
``compute_demonstration`` in the sandbox, and derives ``holds`` from the committed
pre-registration + negative control + leakage probes. Materials already implements that
contract (Magpie features, chemical-system groups), so the SAME harness verdict machinery the
molecules tests exercise must work here on a band-gap composition frame. We monkeypatch
``load_data`` to a hand-built toy frame (no download), then run real Magpie featurization + the
real subprocess runner + the real decision rule. The AI never returns ``holds``.
"""

from __future__ import annotations

import pandas as pd
import pytest

from aletheia.domains.base import DomainPlugin
from aletheia.domains.materials.matbench_task import MaterialsBandGapPlugin

# A real composition set with plausible band gaps (eV). Sized >= 2x the min-samples probe floor
# (demonstration_min_samples=20) so a holding demonstration is not rejected for small n_test/control.
_MAT_COMPS = [
    ("Si", 1.12), ("Ge", 0.67), ("GaAs", 1.42), ("GaN", 3.4), ("ZnO", 3.37),
    ("ZnS", 3.6), ("CdTe", 1.49), ("CdS", 2.42), ("NaCl", 8.5), ("MgO", 7.8),
    ("TiO2", 3.2), ("SiC", 3.0), ("AlN", 6.2), ("InP", 1.35), ("InAs", 0.36),
    ("PbS", 0.37), ("Cu2O", 2.1), ("Fe2O3", 2.2), ("SnO2", 3.6), ("WO3", 2.7),
    ("GaP", 2.26), ("AlAs", 2.16), ("AlP", 2.45), ("InSb", 0.17), ("GaSb", 0.73),
    ("ZnSe", 2.7), ("ZnTe", 2.25), ("CdSe", 1.74), ("PbSe", 0.27), ("PbTe", 0.32),
    ("SnS", 1.3), ("SnSe", 0.9), ("Sb2Se3", 1.2), ("BN", 5.96), ("AlSb", 1.6),
    ("In2O3", 2.9), ("Ga2O3", 4.8), ("V2O5", 2.3), ("MoS2", 1.2), ("WS2", 1.3),
    ("MoSe2", 1.1), ("WSe2", 1.2), ("NiO", 3.6), ("CoO", 2.4), ("MnO", 3.6),
    ("CdO", 2.2), ("SrTiO3", 3.2), ("BaTiO3", 3.2),
]

_DATA_SPEC = {"source": "toy", "target_column": "band_gap", "composition_column": "composition"}

# --- fake AI-authored demonstrations (mirror tests/test_paradigm_p5.py; the AI returns ONLY
# test/control statistics + sample sizes — never 'holds'). Fixed n_test/n_control keep the probe
# decoupled from the toy frame size so these assert the HARNESS RULE, not data plumbing. ---------
_HOLDS = (
    "def compute_demonstration(X, y, groups, meta):\n"
    "    return {'test_statistic': 5.0, 'control_statistic': 0.01, 'components': {},\n"
    "            'detail': 'crafted holds', 'n_test': 30, 'n_control': 30}\n"
)
_CONTROL_FIRES = _HOLDS.replace("0.01", "5.0")  # the 'effect' is an ARTIFACT (also on control)
_DEGENERATE = (
    "def compute_demonstration(X, y, groups, meta):\n"
    "    return {'test_statistic': 5.0, 'control_statistic': 0.0, 'components': {},\n"
    "            'detail': 'sham control', 'n_test': 30, 'n_control': 2}\n"
)
_CHEAT = (  # AI tries to assert holds=True with a non-triggering statistic
    "def compute_demonstration(X, y, groups, meta):\n"
    "    return {'test_statistic': 0.0, 'control_statistic': 0.0, 'holds': True,\n"
    "            'components': {}, 'detail': 'cheat', 'n_test': 30, 'n_control': 30}\n"
)

_PREREG = {
    "statistic_name": "gap", "computation": "c", "control_description": "d", "expected_control": "e",
    "supported_if": {"op": ">=", "threshold": 1.0},
    "control_silent_if": {"op": "<", "threshold": 0.5},
}


def _spec(code, prereg=_PREREG, **extra):
    s = {"capability": DomainPlugin.AI_AUTHORED_CAPABILITY_ID, "demonstration_code": code, **extra}
    if prereg is not None:
        s["preregistration"] = prereg
    return s


@pytest.fixture
def materials_plugin(monkeypatch):
    """A materials plugin whose ``load_data`` returns the toy frame (no benchmark download).
    Featurization, the sandbox runner, and the decision rule all run for real."""
    plug = MaterialsBandGapPlugin()
    df = pd.DataFrame(_MAT_COMPS, columns=["composition", "band_gap"])
    df.attrs["data_spec"] = _DATA_SPEC  # featurize reads target/composition cols off attrs
    monkeypatch.setattr(plug, "load_data", lambda data_spec: df)
    return plug


def test_materials_ai_demo_holds_when_test_triggers_and_control_silent(materials_plugin, tmp_path):
    d = materials_plugin.run_demonstration(_spec(_HOLDS), _DATA_SPEC, str(tmp_path / "holds"))
    assert d is not None
    assert d["holds"] is True and d["control_silent"] is True and d["test_triggers"] is True
    # the generic AI-authored capability ran on materials (X, y, groups) — not a registered demo
    assert d["capability"] == "ai_authored_demonstration"
    assert d["statistic"] == 5.0


def test_materials_ai_demo_refuted_when_effect_is_an_artifact_on_control(materials_plugin, tmp_path):
    # the 'effect' also fires on the non-halogen/control side -> NOT discriminating -> refuted
    d = materials_plugin.run_demonstration(_spec(_CONTROL_FIRES), _DATA_SPEC, str(tmp_path / "ctrl"))
    assert d["holds"] is False and d["control_silent"] is False


def test_materials_ai_demo_not_evaluated_on_degenerate_control_probe(materials_plugin, tmp_path):
    # A sham control cannot ground a claim, but is not scientific counter-evidence.
    d = materials_plugin.run_demonstration(_spec(_DEGENERATE), _DATA_SPEC, str(tmp_path / "degen"))
    assert d["holds"] is None and any("control" in f for f in d["probes"]["flags"])


def test_materials_ai_demo_not_evaluated_without_preregistration(materials_plugin, tmp_path):
    d = materials_plugin.run_demonstration(_spec(_HOLDS, prereg=None), _DATA_SPEC, str(tmp_path / "nopr"))
    assert d is None  # no usable rule -> not_evaluated (fail closed)


def test_materials_ai_demo_ignores_ai_asserted_holds(materials_plugin, tmp_path):
    # the harness derives holds from the rule (test_statistic 0 fails supported_if >= 1), so the
    # AI-returned holds=True is ignored — the AI cannot grade its own homework on materials either.
    d = materials_plugin.run_demonstration(_spec(_CHEAT), _DATA_SPEC, str(tmp_path / "cheat"))
    assert d["holds"] is False


# --- K1 explore->confirm seal on the materials (X, y, groups) shape -------------------------
_LEN_STAT = (  # returns the row count it received -> proves the compute ran on the CONFIRM subset
    "def compute_demonstration(X, y, groups, meta):\n"
    "    n = len(y)\n"
    "    return {'test_statistic': float(n), 'control_statistic': 0.01, 'components': {},\n"
    "            'detail': 'rows seen', 'n_test': n, 'n_control': n}\n"
)


def test_materials_confirm_only_compute_runs_on_the_confirm_subset(materials_plugin, tmp_path):
    d = materials_plugin.run_demonstration(
        _spec(_LEN_STAT, confirm_index=list(range(30)), split_meta={"index_hash": "abc"}),
        _DATA_SPEC, str(tmp_path / "confirm"))
    assert d["exploration_applied"] is True
    assert d["statistic"] == 30.0 and d["n_confirm"] == 30 and d["holds"] is True


def test_materials_confirm_index_out_of_range_is_seal_mismatch(materials_plugin, tmp_path):
    d = materials_plugin.run_demonstration(
        _spec(_LEN_STAT, confirm_index=[0, 1, 999]), _DATA_SPEC, str(tmp_path / "mismatch"))
    assert d["holds"] is None and "seal mismatch" in d["detail"]
