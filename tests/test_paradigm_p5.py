"""Paradigm-mode P5 — the AI-AUTHORED demonstration executor (the frontier path). The AI
writes the discriminating COMPUTATION itself (``compute_demonstration``); the harness owns the
verdict via a PRE-REGISTERED decision rule + a NEGATIVE CONTROL + leakage/degeneracy probes +
an independent (author-excluded) cross-vendor audit. The AI never returns ``holds``.

Tests inject fake ``compute_demonstration`` source directly (no LLM spend); the integration
tests use REAL ESOL (subsampled). Per the Codex gap analysis, several tests check the EVIDENCE
DEFINITION is strong (a sham control / artifact is refuted), not just that fields are present.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from aletheia.coder.demonstration import CANNED_DEMO, CANNED_PREREGISTRATION, extract_preregistration
from aletheia.coder.demonstration_runner import run_authored_demonstration
from aletheia.coder.sandbox import (
    DEMO_REQUIRED_FUNCTION,
    check_code,
    smoke_test_demonstration,
)
from aletheia.domains.base import DomainPlugin
from aletheia.domains.molecules.plugin import MoleculePropertyPlugin
from aletheia.scheduler.driver import ExperimentDriver

ESOL = {"source": "benchmark", "ref": "esol"}
_TOY_MOL_SPEC = {"source": "toy", "smiles_column": "smiles", "target_column": "y"}
_TOY_SMILES = [
    "C", "CC", "CCC", "CCCC", "CCCCC", "CCCCCC", "CCCCCCC", "CCCCCCCC",
    "CO", "CCO", "CCCO", "CCCCO", "CCN", "CCCN", "CCCCN", "CCCl",
    "CCBr", "CCI", "CCl", "CBr", "CI", "CC(=O)O", "CCC(=O)O", "CCOC",
    "c1ccccc1", "Cc1ccccc1", "Oc1ccccc1", "Nc1ccccc1", "Clc1ccccc1", "Brc1ccccc1",
    "c1ccncc1", "c1ccoc1", "c1ccsc1", "CCS", "CCCS", "CCF",
    "CC(C)O", "CC(C)N", "CC(C)Cl", "CC(C)Br", "CC(C)C", "CC(C)(C)O",
    "C1CCCCC1", "CC1CCCCC1", "OC1CCCCC1", "NC1CCCCC1", "ClC1CCCCC1", "BrC1CCCCC1",
]

# --- fake AI-authored demonstrations (the AI returns only test/control statistics) ---------
_HOLDS = (
    "def compute_demonstration(X, y, groups, meta):\n"
    "    n = len(y)\n"
    "    return {'test_statistic': 5.0, 'control_statistic': 0.01, 'components': {},\n"
    "            'detail': 'crafted holds', 'n_test': n // 2, 'n_control': n // 2}\n"
)
_CONTROL_FIRES = _HOLDS.replace("0.01", "5.0")  # the 'effect' is an ARTIFACT (also on control)
_DEGENERATE = (
    "def compute_demonstration(X, y, groups, meta):\n"
    "    return {'test_statistic': 5.0, 'control_statistic': 0.0, 'components': {},\n"
    "            'detail': 'sham control', 'n_test': len(y), 'n_control': 2}\n"
)
_SEEDED = (  # statistic depends on meta['random_state'] -> genuine re-computation
    "def compute_demonstration(X, y, groups, meta):\n"
    "    import numpy as np\n"
    "    rng = np.random.default_rng(int(meta['random_state']))\n"
    "    n = len(y); idx = rng.permutation(n)\n"
    "    t = idx[: n // 2]\n"
    "    return {'test_statistic': float(2.0 + np.std(y[t])), 'control_statistic': 0.01,\n"
    "            'components': {}, 'detail': 's', 'n_test': n // 2, 'n_control': n - n // 2}\n"
)
_CHEAT = (  # AI tries to assert holds=True with a non-triggering statistic
    "def compute_demonstration(X, y, groups, meta):\n"
    "    return {'test_statistic': 0.0, 'control_statistic': 0.0, 'holds': True,\n"
    "            'components': {}, 'detail': 'cheat', 'n_test': len(y) // 2, 'n_control': len(y) // 2}\n"
)
_BAD_IMPORT = "import os\ndef compute_demonstration(X, y, groups, meta):\n    return {}\n"

_PREREG = {
    "statistic_name": "gap", "computation": "c", "control_description": "d", "expected_control": "e",
    "supported_if": {"op": ">=", "threshold": 1.0},
    "control_silent_if": {"op": "<", "threshold": 0.5},
}


def _spec(code, prereg=_PREREG, **extra):
    s = {"capability": DomainPlugin.AI_AUTHORED_CAPABILITY_ID, "demonstration_code": code,
         "sample_n": 300, **extra}
    if prereg is not None:
        s["preregistration"] = prereg
    return s


@pytest.fixture
def molecules_plugin(monkeypatch):
    """Offline molecules plugin for P5 harness tests.

    These tests assert the generic AI-authored demonstration contract, not MoleculeNet download
    availability. A previous ESOL-backed version failed under transient SSL/network errors and
    returned ``holds=None`` before exercising the harness rule. Keep real ESOL for e2e scripts;
    unit tests use this local valid-SMILES frame.
    """
    plug = MoleculePropertyPlugin()
    df = pd.DataFrame({"smiles": _TOY_SMILES, "y": np.linspace(-2.0, 2.0, len(_TOY_SMILES))})
    df.attrs["data_spec"] = _TOY_MOL_SPEC
    monkeypatch.setattr(plug, "load_data", lambda data_spec: df)
    return plug


# --- the static gate + smoke test honor the demonstration contract -------------------------
def test_demo_gate_requires_compute_demonstration():
    ok, _ = check_code(CANNED_DEMO, required_function=DEMO_REQUIRED_FUNCTION)
    assert ok
    # a build_pipeline-only module does NOT satisfy the demonstration required-function
    ok2, reasons = check_code("def build_pipeline():\n    return 1\n",
                              required_function=DEMO_REQUIRED_FUNCTION)
    assert not ok2 and any("compute_demonstration" in r for r in reasons)
    # forbidden import still rejected
    ok3, _ = check_code(_BAD_IMPORT, required_function=DEMO_REQUIRED_FUNCTION)
    assert not ok3


def test_smoke_test_demonstration_runs_canned():
    ok, err = smoke_test_demonstration(CANNED_DEMO)
    assert ok, err
    # a module that returns the wrong shape fails the smoke test
    bad = "def compute_demonstration(X, y, groups, meta):\n    return 123\n"
    ok2, _ = smoke_test_demonstration(bad)
    assert not ok2


# --- the harness decision rule + probes (offline, no data) ---------------------------------
def test_apply_rule_truth_table():
    f = DomainPlugin._apply_rule
    assert f(5.0, {"op": ">=", "threshold": 1.0}) is True
    assert f(0.4, {"op": "<", "threshold": 0.5}) is True
    assert f(0.6, {"op": "<", "threshold": 0.5}) is False
    assert f(1.0, {"op": ">", "threshold": 1.0}) is False
    assert f(1.0, {"op": "<=", "threshold": 1.0}) is True
    assert f(1.0, {"op": "??", "threshold": 1.0}) is False  # malformed op -> fail closed


def test_valid_decision_rules():
    assert DomainPlugin._valid_decision_rules(_PREREG)
    assert not DomainPlugin._valid_decision_rules({"supported_if": {"op": ">=", "threshold": 1.0}})
    assert not DomainPlugin._valid_decision_rules(
        {"supported_if": {"op": "bad", "threshold": 1.0}, "control_silent_if": {"op": "<", "threshold": 0.5}})


# --- the sandbox runner stages arrays + returns a validated dict (offline) ------------------
def test_runner_returns_validated_result_on_synthetic():
    rng = np.random.default_rng(1)
    X, y = rng.random((60, 4)), rng.random(60)
    g = np.array([i % 6 for i in range(60)], dtype=object)
    res = run_authored_demonstration(_HOLDS, X, y, g, {"random_state": 7, "preregistration": {}})
    assert res is not None and set(res) >= {"test_statistic", "control_statistic", "n_test", "n_control"}
    # code that raises / returns a non-dict -> None (fail closed -> not_evaluated)
    raises = "def compute_demonstration(X, y, groups, meta):\n    raise ValueError('boom')\n"
    assert run_authored_demonstration(raises, X, y, g, {"random_state": 0, "preregistration": {}}) is None


# --- integration through the molecules plugin (offline valid-SMILES frame) -----------------
def test_ai_demo_holds_when_test_triggers_and_control_silent(molecules_plugin):
    d = molecules_plugin.run_demonstration(_spec(_HOLDS), _TOY_MOL_SPEC, "/tmp/p5_holds")
    assert d["holds"] is True and d["form"]
    assert d["capability"] == "ai_authored_demonstration" and d["reproduce_factor"] == 2.0
    assert d["statistic"] == 5.0


def test_ai_demo_refuted_when_effect_is_an_artifact_on_control(molecules_plugin):
    # EVIDENCE-STRENGTH (Codex #4): the 'effect' also appears on the control -> NOT discriminating.
    d = molecules_plugin.run_demonstration(_spec(_CONTROL_FIRES), _TOY_MOL_SPEC, "/tmp/p5_ctrl")
    assert d["holds"] is False and d["control_silent"] is False


def test_ai_demo_refuted_on_degenerate_control_probe(molecules_plugin):
    # EVIDENCE-STRENGTH: a sham control (n_control below the floor) cannot ground the claim.
    d = molecules_plugin.run_demonstration(_spec(_DEGENERATE), _TOY_MOL_SPEC, "/tmp/p5_degen")
    assert d["holds"] is False and any("control" in f for f in d["probes"]["flags"])


def test_ai_demo_fails_closed_on_code_gate_reject(molecules_plugin):
    d = molecules_plugin.run_demonstration(_spec(_BAD_IMPORT), _TOY_MOL_SPEC, "/tmp/p5_badimp")
    assert d["holds"] is False and "code gate" in d["detail"]  # no crash


def test_ai_demo_not_evaluated_without_preregistration(molecules_plugin):
    d = molecules_plugin.run_demonstration(_spec(_HOLDS, prereg=None), _TOY_MOL_SPEC, "/tmp/p5_noprereg")
    assert d is None  # no usable rule -> not_evaluated (fail closed)


def test_ai_demo_ignores_ai_asserted_holds(molecules_plugin):
    # the AI cannot grade its own homework: an AI-returned holds=True is ignored; the harness
    # derives holds from the rule (test_statistic 0 fails supported_if >= 1).
    d = molecules_plugin.run_demonstration(_spec(_CHEAT), _TOY_MOL_SPEC, "/tmp/p5_cheat")
    assert d["holds"] is False


def test_ai_demo_is_seed_perturbed_for_real_reproduction(molecules_plugin):
    a = molecules_plugin.run_demonstration(_spec(_SEEDED, random_state=1), _TOY_MOL_SPEC, "/tmp/p5_s1")
    b = molecules_plugin.run_demonstration(_spec(_SEEDED, random_state=2), _TOY_MOL_SPEC, "/tmp/p5_s2")
    assert a["statistic"] != b["statistic"]  # the random-state genuinely perturbs the compute


# --- K1: the explore->confirm seal (confirm-only compute + deterministic threshold check) ----
_LEN_STAT = (  # returns the ROW COUNT it received -> proves WHICH rows the compute saw
    "def compute_demonstration(X, y, groups, meta):\n"
    "    n = len(y)\n"
    "    return {'test_statistic': float(n), 'control_statistic': 0.01, 'components': {},\n"
    "            'detail': 'rows seen', 'n_test': n, 'n_control': n}\n"
)


def test_confirm_only_compute_runs_on_the_confirm_subset(molecules_plugin):
    # the demonstration must run ONLY on the held-out CONFIRM rows the authoring never saw:
    # _LEN_STAT returns the row count it received, so statistic == n_confirm proves the seal.
    d = molecules_plugin.run_demonstration(
        _spec(_LEN_STAT, confirm_index=list(range(30)), split_meta={"index_hash": "abc"}),
        _TOY_MOL_SPEC, "/tmp/p5_confirm")
    assert d["exploration_applied"] is True
    assert d["statistic"] == 30.0 and d["n_confirm"] == 30
    assert d["holds"] is True and d["split_meta"] == {"index_hash": "abc"}


def test_confirm_index_out_of_range_is_seal_mismatch_not_evaluated(molecules_plugin):
    d = molecules_plugin.run_demonstration(
        _spec(_LEN_STAT, confirm_index=[0, 1, 999]), _TOY_MOL_SPEC, "/tmp/p5_mismatch")
    assert d["holds"] is None and "seal mismatch" in d["detail"]
    assert d["exploration_applied"] is False


def test_starved_confirm_partition_fails_the_probe_floor(molecules_plugin):
    # a confirm subset below the probe min-sample floor cannot ground the claim (fail closed)
    d = molecules_plugin.run_demonstration(
        _spec(_LEN_STAT, confirm_index=[0, 1, 2, 3, 4], split_meta={"index_hash": "z"}),
        _TOY_MOL_SPEC, "/tmp/p5_starved")
    assert d["holds"] is False and any("too small" in f for f in d["probes"]["flags"])


def test_no_seal_marks_exploration_not_applied(molecules_plugin):
    # the blind fallback (no confirm_index) still runs full-data and marks the seal ABSENT, so
    # _finalize_claims caps the formulation below `strong`.
    d = molecules_plugin.run_demonstration(_spec(_HOLDS), _TOY_MOL_SPEC, "/tmp/p5_noseal")
    assert d["holds"] is True and d["exploration_applied"] is False and d["n_confirm"] is None


def test_prereg_consistency_doom_trivial_and_control():
    # seal #5 (deterministic): the committed threshold must be consistent with the AI's OWN
    # exploration estimates — doom-to-zero / control-not-silent / trivially-easy all fail closed.
    f = ExperimentDriver._prereg_consistent_with_exploration
    ok, _ = f(_PREREG, {"observations": {"expected_test_statistic": 5.0, "expected_control_statistic": 0.1}})
    assert ok is True
    doom, why = f(_PREREG, {"observations": {"expected_test_statistic": 0.2, "expected_control_statistic": 0.1}})
    assert doom is False and "doom-to-zero" in why  # test estimate 0.2 fails supported_if >= 1.0
    ctrl, why2 = f(_PREREG, {"observations": {"expected_test_statistic": 5.0, "expected_control_statistic": 0.9}})
    assert ctrl is False and "not silent" in why2  # control 0.9 violates control_silent_if < 0.5
    triv_prereg = {**_PREREG, "control_silent_if": {"op": "<", "threshold": 3.0}}
    triv, why3 = f(triv_prereg, {"observations": {"expected_test_statistic": 5.0, "expected_control_statistic": 2.0}})
    assert triv is False and "trivially easy" in why3  # control 2.0 ALSO meets supported_if >= 1.0
    lenient, _ = f(_PREREG, {"observations": {"y_std": 1.0}})
    assert lenient is True  # no expected statistics -> defer to the LLM auditor


def test_audit_deterministic_seal_refutes_inconsistent_threshold():
    # seal #5 runs BEFORE the LLM auditor: an inconsistent threshold (doom-to-zero vs the AI's own
    # exploration) is refuted deterministically, short-circuiting the panel entirely.
    d = ExperimentDriver("rid-seal", dry_run=False)
    d._claim_ids = {}
    called = {"llm": False}

    async def fake_review(*a, **k):
        called["llm"] = True
        return _fake_panel("approve", True, ["grok", "gemini"])

    d.gateway = SimpleNamespace(review=fake_review)
    design = {"demonstration_code": _HOLDS,
              "demonstration_exploration": {"observations": {"expected_test_statistic": 0.1,
                                                             "expected_control_statistic": 0.1}}}
    demo = {"holds": True, "preregistration": _PREREG, "capability": "ai_authored_demonstration",
            "statistic": 0.1, "test_statistic": 0.1, "control_statistic": 0.1}
    passed, err = asyncio.run(d._audit_demonstration(design, demo))
    assert passed is False and err is False
    assert demo["holds"] is False and demo["audit_refuted"] is True
    assert called["llm"] is False  # deterministic refutation never reached the LLM auditor


def _reply(threshold):
    """A worker reply: valid demo code + a prereg whose supported_if threshold we control."""
    prereg = {**_PREREG, "supported_if": {"op": ">=", "threshold": threshold}}
    return "```python\n" + _HOLDS + "```\n```json\n" + json.dumps(prereg) + "\n```"


def _authoring_driver(monkeypatch, replies):
    """Drive _demonstration_code with mocked worker replies + a fixed exploration; returns
    (design, prompts, run_id, fid) so callers can assert on the retry + the committed prereg."""
    import aletheia.scheduler.driver as drv
    from aletheia.db import create_all
    from aletheia.memory.service import create_claim, create_run, finalize_plan

    create_all()
    run_id = create_run("k1 retry test", domain="materials", status="planned")
    finalize_plan(run_id, {"objective": "x", "domain": "materials"})
    fid = create_claim(run_id, claim_text="f", claim_type="formulation", strength="speculative")
    d = drv.ExperimentDriver(run_id, dry_run=False)
    d._claim_ids = {"formulation": fid}
    # authoring="ai" -> the frontier override skips the registered-first check entirely
    d.hypothesis = {"demonstration": {"form": "discriminating_instance", "claim": "c", "authoring": "ai"}}
    d.plugin = object()  # non-None so the explore phase runs; the explore call itself is faked

    obs = {"observations": {"expected_test_statistic": 0.49, "expected_control_statistic": 0.1},
           "detail": "explore", "n": 100}

    async def fake_explore(plugin, demo, data_spec, feature_desc, rs, exp_id):
        return obs, list(range(40)), {"seed": rs, "explore_frac": 0.5, "n_explore": 100,
                                      "n_confirm": 40, "split_algo_version": 1,
                                      "group_disjoint": True, "index_hash": "deadbeef"}

    d._explore_for_demonstration = fake_explore

    prompts: list[str] = []
    seq = iter(replies)

    async def fake_worker(run_id_, role, prompt, **kw):
        prompts.append(prompt)
        return next(seq)

    monkeypatch.setattr(drv, "run_worker", fake_worker)

    async def noop_index(*a, **k):
        return None

    d._index = noop_index
    design = {"random_state": 42}
    asyncio.run(d._demonstration_code(design, {"source": "toy"}, None))
    return design, prompts, run_id, fid


def _committed_prereg(run_id, fid):
    from aletheia.memory.service import list_claims

    for c in list_claims(run_id):
        if c.get("id") != fid:
            continue
        for e in c.get("evidence", []):
            if e.get("evidence_kind") == "preregistration" and e.get("note"):
                return json.loads(e["note"])
    return None


def test_consistency_rejection_gets_one_informed_recalibration_retry(monkeypatch):
    # K1 v1.1 (seen live, run b1993c7f): the AI registered supported_if>=0.55 against its OWN
    # explore estimate 0.49 -> doomed -> rejected. The fix: ONE retry that feeds the rejection
    # reason back so the AI recalibrates PRE-COMMIT on the same explore-only observations.
    design, prompts, run_id, fid = _authoring_driver(
        monkeypatch, [_reply(0.55), _reply(0.4)])
    assert len(prompts) == 2
    assert "doom-to-zero" in prompts[1]  # the retry prompt carries the seal's specific reason
    assert design.get("demonstration_code")  # the recalibrated attempt was accepted
    assert design.get("demonstration_confirm_index") == list(range(40))
    committed = _committed_prereg(run_id, fid)
    assert committed and committed["supported_if"]["threshold"] == 0.4  # the RECALIBRATED rule


def test_still_inconsistent_after_retry_falls_back_without_commit(monkeypatch):
    # the retry is BOUNDED: a second inconsistent threshold (0.6 > estimate 0.49) is not retried
    # again — no commit, no demonstration_code, fall back to the registered-capability path.
    design, prompts, run_id, fid = _authoring_driver(
        monkeypatch, [_reply(0.55), _reply(0.6), _reply(0.4)])
    assert len(prompts) == 2  # exactly one retry, never a third attempt
    assert "demonstration_code" not in design and "demonstration_confirm_index" not in design
    assert _committed_prereg(run_id, fid) is None  # nothing was committed


def _bad_code_reply(threshold=0.4):
    # passes the static gate (defines compute_demonstration) but FAILS the smoke test (non-dict),
    # with a threshold consistent vs the fixed exploration (0.49) so ONLY the code is the problem.
    prereg = {**_PREREG, "supported_if": {"op": ">=", "threshold": threshold}}
    bad = "def compute_demonstration(X, y, groups, meta):\n    return 123\n"
    return "```python\n" + bad + "```\n```json\n" + json.dumps(prereg) + "\n```"


def test_v12_code_failure_also_gets_one_bounded_retry(monkeypatch):
    # K1 v1.2 (seen live, run 5cdb9d60): a COMPOUND first attempt — the code raised AND the
    # threshold was off — must ALSO get one informed retry (v1.1 only retried pure consistency).
    # Here the first reply's code fails the smoke test; the retry carries the runtime error.
    design, prompts, run_id, fid = _authoring_driver(
        monkeypatch, [_bad_code_reply(), _reply(0.4)])
    assert len(prompts) == 2  # the smoke-test failure triggered the informed retry
    assert "import/run failed" in prompts[1]  # the concrete runtime error is fed back
    assert design.get("demonstration_code")  # the fixed second attempt was accepted + committed
    assert _committed_prereg(run_id, fid) is not None


def test_missing_seal_caps_formulation_below_strong():
    f = ExperimentDriver._claim_strength
    sealed = f("formulation", gate_passed=True, gate_verdict="approve", reproduced=True,
               demonstration_holds=True, cross_vendor=True, audit_passed=True, exploration_missing=False)
    capped = f("formulation", gate_passed=True, gate_verdict="approve", reproduced=True,
               demonstration_holds=True, cross_vendor=True, audit_passed=True, exploration_missing=True)
    assert sealed == "strong" and capped == "moderate"


# --- pre-registration prompt/extraction --------------------------------------------------
def test_canned_preregistration_is_valid_and_extractable():
    reply = "```python\n" + CANNED_DEMO + "```\n```json\n" + json.dumps(CANNED_PREREGISTRATION) + "\n```"
    assert ExperimentDriver._valid_preregistration(CANNED_PREREGISTRATION)
    assert extract_preregistration(reply) == CANNED_PREREGISTRATION


# --- independence: the AUTHOR vendor is excluded from the audit panel ----------------------
def test_providers_exclude_author_vendor():
    from aletheia.critics.gateway import CriticGateway

    g = CriticGateway()
    ids = {p.critic_id for p in g._providers(exclude_vendors={"anthropic"})}
    assert "anthropic" not in ids  # the Opus author is never an auditor of its own code


# --- the audit forces holds=False on a refuting / non-independent verdict -------------------
def _fake_panel(verdict, gate_passed, critic_ids):
    return SimpleNamespace(
        consensus_verdict=verdict, gate_passed=gate_passed,
        critiques=[SimpleNamespace(critic_id=cid) for cid in critic_ids],
    )


def test_audit_refutation_forces_holds_false():
    d = ExperimentDriver("rid-audit", dry_run=False)
    d._claim_ids = {}  # no formulation claim -> skip the ledger attach (hermetic)

    async def fake_review(*a, **k):
        return _fake_panel("reject", False, ["gemini", "deepseek"])

    d.gateway = SimpleNamespace(review=fake_review)
    demo = {"holds": True, "preregistration": _PREREG, "test_statistic": 5.0, "control_statistic": 0.0}
    passed, err = asyncio.run(d._audit_demonstration({"demonstration_code": _HOLDS}, demo))
    assert passed is False and err is False  # audit RAN and refuted -> not an infra error
    assert demo["holds"] is False and demo["audit_refuted"] is True


def test_audit_not_independent_if_author_present_fails_closed():
    d = ExperimentDriver("rid-audit2", dry_run=False)
    d._claim_ids = {}

    async def fake_review(*a, **k):
        return _fake_panel("approve", True, ["anthropic"])  # author leaked into the panel

    d.gateway = SimpleNamespace(review=fake_review)
    demo = {"holds": True, "preregistration": _PREREG, "test_statistic": 5.0, "control_statistic": 0.0}
    passed, err = asyncio.run(d._audit_demonstration({"demonstration_code": _HOLDS}, demo))
    assert passed is False and err is False and demo["holds"] is False  # not independent -> fail closed


def test_audit_infra_error_is_not_refutation():
    # the audit INFRASTRUCTURE errors (gateway raises). This is missing verification, NOT a
    # refutation: ``holds`` is left untouched (no audit_refuted) and the call signals
    # ``audit_error=True`` so strength is capped without marking the claim refuted.
    d = ExperimentDriver("rid-audit-err", dry_run=False)
    d._claim_ids = {}

    async def fake_review(*a, **k):
        raise RuntimeError("auditor offline")

    d.gateway = SimpleNamespace(review=fake_review)
    demo = {"holds": True, "preregistration": _PREREG, "test_statistic": 5.0, "control_statistic": 0.0}
    passed, err = asyncio.run(d._audit_demonstration({"demonstration_code": _HOLDS}, demo))
    assert passed is None and err is True  # audit did NOT run
    assert demo["holds"] is True and "audit_refuted" not in demo  # untouched -> not refuted


def test_audit_skipped_for_non_ai_demonstration():
    d = ExperimentDriver("rid-audit3", dry_run=False)
    d._claim_ids = {}
    # a registered (non-AI) demonstration carries no preregistration -> no audit
    passed, err = asyncio.run(d._audit_demonstration({}, {"holds": True, "statistic": 1.6}))
    assert passed is None and err is False


def test_audit_disabled_for_ai_authored_caps_without_refuting(monkeypatch):
    from aletheia.config import get_settings

    s = get_settings()
    saved = s.demonstration_audit_enabled
    s.demonstration_audit_enabled = False
    try:
        d = ExperimentDriver("rid-audit-disabled", dry_run=False)
        d._claim_ids = {}
        demo = {"holds": True, "preregistration": _PREREG, "test_statistic": 5.0, "control_statistic": 0.0}
        passed, err = asyncio.run(d._audit_demonstration({"demonstration_code": _HOLDS}, demo))
        assert passed is None and err is True
        assert demo["holds"] is True and "audit_refuted" not in demo
    finally:
        s.demonstration_audit_enabled = saved


# --- audit vendor floor: one approval is not verification; one rejection still blocks --------
def test_audit_single_auditor_approve_is_degraded_not_verification():
    # seen live (materials e2e): every provider but grok errored out, leaving a 1-auditor
    # panel. An approval from a single surviving auditor must NOT count as full independent
    # verification — degrade (audit_error caps strength at weak) WITHOUT refuting.
    d = ExperimentDriver("rid-audit4", dry_run=False)
    d._claim_ids = {}

    async def fake_review(*a, **k):
        return _fake_panel("approve", True, ["grok"])  # one survivor approves

    d.gateway = SimpleNamespace(review=fake_review)
    demo = {"holds": True, "preregistration": _PREREG, "test_statistic": 5.0, "control_statistic": 0.0}
    passed, err = asyncio.run(d._audit_demonstration({"demonstration_code": _HOLDS}, demo))
    assert passed is None and err is True  # degraded: no usable independent verification
    assert demo["holds"] is True and "audit_refuted" not in demo  # NOT a refutation


def test_audit_single_auditor_reject_still_refutes():
    # the floor is asymmetric by design: one adversarial finding is enough to BLOCK
    # (fail-closed), even though one approval is not enough to verify.
    d = ExperimentDriver("rid-audit5", dry_run=False)
    d._claim_ids = {}

    async def fake_review(*a, **k):
        return _fake_panel("reject", False, ["grok"])

    d.gateway = SimpleNamespace(review=fake_review)
    demo = {"holds": True, "preregistration": _PREREG, "test_statistic": 5.0, "control_statistic": 0.0}
    passed, err = asyncio.run(d._audit_demonstration({"demonstration_code": _HOLDS}, demo))
    assert passed is False and err is False
    assert demo["holds"] is False and demo["audit_refuted"] is True


def test_audit_two_auditor_approve_passes():
    d = ExperimentDriver("rid-audit6", dry_run=False)
    d._claim_ids = {}

    async def fake_review(*a, **k):
        return _fake_panel("approve", True, ["grok", "gemini"])  # meets the vendor floor

    d.gateway = SimpleNamespace(review=fake_review)
    demo = {"holds": True, "preregistration": _PREREG, "test_statistic": 5.0, "control_statistic": 0.0}
    passed, err = asyncio.run(d._audit_demonstration({"demonstration_code": _HOLDS}, demo))
    assert passed is True and err is False and demo["holds"] is True


def test_refuting_audit_republishes_demonstration_event():
    # the `demonstration` event is first published at COMPUTE time; the audit's later
    # holds=False mutation must be re-published so the event stream / e2e summary / UI never
    # show a stale pre-audit verdict (seen live: summary said audit_refuted=null, audit=reject).
    from aletheia.events.bus import get_bus

    d = ExperimentDriver("rid-audit7", dry_run=False)
    d._claim_ids = {}

    async def fake_review(*a, **k):
        return _fake_panel("reject", False, ["gemini", "deepseek"])

    d.gateway = SimpleNamespace(review=fake_review)
    demo = {"holds": True, "preregistration": _PREREG, "capability": "ai_authored_demonstration",
            "statistic": 5.0, "test_statistic": 5.0, "control_statistic": 0.0}

    async def go():
        captured = []

        async def sub():
            async for evt in get_bus().subscribe():
                if evt.get("run_id") == "rid-audit7":
                    captured.append(evt)

        task = asyncio.create_task(sub())
        await asyncio.sleep(0)
        out = await d._audit_demonstration({"demonstration_code": _HOLDS}, demo)
        await asyncio.sleep(0.1)
        task.cancel()
        return out, captured

    (passed, err), events = asyncio.run(go())
    assert passed is False
    demos = [e["payload"] for e in events if e["type"] == "demonstration"]
    assert demos and demos[-1]["holds"] is False and demos[-1]["audit_refuted"] is True
    assert demos[-1]["capability"] == "ai_authored_demonstration"


# --- reproduction semantics: verdict stability vs statistic stability are DISTINCT ----------
def _repro_driver(monkeypatch, orig_demo, repro_demo):
    """Drive _reproduce with a faked re-run; returns the reproduction payload."""
    import aletheia.scheduler.driver as drv
    from aletheia.db import create_all
    from aletheia.memory.service import create_run, finalize_plan

    create_all()
    run_id = create_run("repro semantics test", domain="materials", status="planned")
    finalize_plan(run_id, {"objective": "x", "domain": "materials"})
    d = drv.ExperimentDriver(run_id, dry_run=False)

    async def fake_run_eval(design, data_spec, domain, exp_id):
        return {"metrics": {"mae": 0.5}, "info": {"demonstration": repro_demo}}

    monkeypatch.setattr(d, "_run_eval", fake_run_eval)
    result = {"metrics": {"mae": 0.5}, "info": {"demonstration": orig_demo}}
    return asyncio.run(d._reproduce({"random_state": 42}, {}, "materials", result, None))


def test_repro_decomposes_verdict_and_statistic_stability(monkeypatch):
    # seen live (materials e2e): statistic swung 20x across seeds (0.074 -> 0.0037) but the
    # payload couldn't show it. Twice-refuted -> verdict IS stable, statistic is NOT, and
    # the strict `demonstration_reproduced` gate stays False (never escalates a refuted demo).
    p = _repro_driver(
        monkeypatch,
        {"holds": False, "statistic": 0.074, "reproduce_factor": 2.0},
        {"holds": False, "statistic": 0.0037},
    )
    assert p["demonstration_reproduced"] is False
    assert p["demonstration_verdict_stable"] is True  # refuted on BOTH runs — qualitatively stable
    assert p["demonstration_statistic_stable"] is False  # 20x swing > the 2x tolerance
    assert p["demonstration_original_statistic"] == 0.074
    assert p["demonstration_repro_statistic"] == 0.0037
    assert p["demonstration_seeds"] == [42, 43]  # both seeds persisted for the auditor


def test_repro_strict_gate_requires_holds_and_stable_statistic(monkeypatch):
    held = _repro_driver(
        monkeypatch,
        {"holds": True, "statistic": 1.6, "reproduce_factor": 2.0},
        {"holds": True, "statistic": 1.5},
    )
    assert held["demonstration_reproduced"] is True
    assert held["demonstration_verdict_stable"] is True
    assert held["demonstration_statistic_stable"] is True

    swung = _repro_driver(
        monkeypatch,
        {"holds": True, "statistic": 1.6, "reproduce_factor": 2.0},
        {"holds": True, "statistic": 0.2},  # held twice, but the statistic swung 8x
    )
    assert swung["demonstration_reproduced"] is False  # statistic instability blocks `strong`
    assert swung["demonstration_verdict_stable"] is True
    assert swung["demonstration_statistic_stable"] is False


def test_repro_missing_statistic_does_not_count_as_reproduced(monkeypatch):
    # Missing comparable statistics means statistic stability was NOT evaluated. Even if the
    # qualitative verdict held twice, this must not satisfy the strict strong-claim gate.
    p = _repro_driver(
        monkeypatch,
        {"holds": True, "statistic": None, "reproduce_factor": 2.0},
        {"holds": True, "statistic": 1.5},
    )
    assert p["demonstration_verdict_stable"] is True
    assert p["demonstration_statistic_stable"] is None
    assert p["demonstration_reproduced"] is False


def test_repro_split_mismatch_does_not_count_as_reproduced(monkeypatch):
    # K1: the re-run must recompute on the SAME held-out CONFIRM partition; a different split
    # index_hash is NOT a reproduction, even if the verdict + statistic look stable.
    p = _repro_driver(
        monkeypatch,
        {"holds": True, "statistic": 1.6, "reproduce_factor": 2.0, "split_meta": {"index_hash": "aaa"}},
        {"holds": True, "statistic": 1.55, "split_meta": {"index_hash": "bbb"}},
    )
    assert p["demonstration_split_match"] is False
    assert p["demonstration_reproduced"] is False


# --- claim strength: an audit failure caps the formulation claim at weak -------------------
def test_audit_failure_caps_formulation_strength():
    f = ExperimentDriver._claim_strength
    strong = f("formulation", gate_passed=True, gate_verdict="approve", reproduced=True,
               demonstration_holds=True, cross_vendor=True, audit_passed=True)
    capped = f("formulation", gate_passed=True, gate_verdict="approve", reproduced=True,
               demonstration_holds=True, cross_vendor=True, audit_passed=False)
    assert strong == "strong" and capped == "weak"


def test_audit_error_caps_formulation_strength_without_refuting():
    # audit_error (audit could not run) caps strength at weak just like a refutation would,
    # but it is a DISTINCT state: audit_passed is None (not False), so callers must not treat
    # it as a refutation when deciding the claim's STATUS.
    f = ExperimentDriver._claim_strength
    capped = f("formulation", gate_passed=True, gate_verdict="approve", reproduced=True,
               demonstration_holds=True, cross_vendor=True, audit_passed=None, audit_error=True)
    assert capped == "weak"


# --- frontier override: prefer the AI-authored path over registered-first ------------------
def test_prefer_authored_default_is_registered_first():
    from aletheia.config import get_settings

    pref = ExperimentDriver._prefer_authored_demonstration
    s = get_settings()
    saved = s.demonstration_prefer_authored
    s.demonstration_prefer_authored = False
    try:
        # default + an untagged spec -> registered-first stands (no override)
        assert pref({"form": "impossibility", "claim": "an activity-cliff lipschitz claim"}) is False
        assert pref("a bare-string demonstration") is False
        # an explicitly tagged spec forces AI authoring even under the default setting
        assert pref({"claim": "x", "authoring": "ai"}) is True
        assert pref({"claim": "x", "authoring": "AI"}) is True
        assert pref({"claim": "x", "ai_authored": True}) is True
        # the global setting overrides regardless of (even mis-typed) spec shape
        s.demonstration_prefer_authored = True
        assert pref({"form": "impossibility", "claim": "cliff"}) is True
        assert pref("bare string") is True
    finally:
        s.demonstration_prefer_authored = saved


# ---------------------------------------------------------------------------
# K2 S3 — the campaign loop LEARNS from the last round's reason. _campaign_step's
# next-experiment prompt must carry each round's deterministic outcome reason + a
# concrete narrowing hint (not just a metric + verdict), and a hard directive built
# from the most recent reason — so round N+1 is provably shaped by round N, not blind.
# ---------------------------------------------------------------------------

def _outcome(round_idx, reason, narrowing_hint, recoverable, verdict="reject"):
    return {
        "round": round_idx, "exp_id": f"exp{round_idx}", "model": "rf",
        "metrics": {}, "headline_metric": "mae", "headline": 0.5, "units": "",
        "analysis": "", "verdict": verdict, "hypothesis": f"hypothesis {round_idx}",
        "experiment_type": "baseline" if round_idx == 1 else "ablation",
        "open_question": "q", "reason": reason, "narrowing_hint": narrowing_hint,
        "recoverable": recoverable, "outcome_detail": "d",
    }


def _campaign_prompt(monkeypatch, outcomes):
    """Run _campaign_step with a captured reason_stage; return (prompt, decision)."""
    import aletheia.scheduler.driver as drv
    from aletheia.db import create_all
    from aletheia.memory.service import create_run, finalize_plan

    create_all()
    run_id = create_run("k2 campaign test", domain="materials", status="planned")
    finalize_plan(run_id, {"objective": "x", "domain": "materials"})
    d = drv.ExperimentDriver(run_id, dry_run=False)

    prompts: list[str] = []
    cand = [{
        "experiment_type": "ablation", "open_question": "which feature drives it?",
        "expected_information_gain": 0.7, "rationale": "r",
        "hypothesis": {"statement": "s", "rationale": "r", "prediction": "p", "novelty_note": "n"},
    }]

    async def fake_reason(run_id_, role, prompt, **kw):
        prompts.append(prompt)
        return json.dumps({"candidates": cand})

    monkeypatch.setattr(drv, "reason_stage", fake_reason)

    async def noop_index(*a, **k):
        return None

    d._index = noop_index
    decision = asyncio.run(
        d._campaign_step({"objective": "x"}, outcomes, round_idx=1, max_exps=3)
    )
    return prompts[0], decision


def test_campaign_trajectory_carries_reason_and_narrowing_hint(monkeypatch):
    # the milestone case: round 1 held on confirm but the broad claim was rejected (scope_overclaim).
    out = _outcome(1, "scope_overclaim",
                   "NARROW the claim to exactly what this confirm-split demonstration shows, "
                   "or author a separate demonstration for each unshown pillar.",
                   recoverable=True)
    prompt, decision = _campaign_prompt(monkeypatch, [out])
    assert "WHY [scope_overclaim]" in prompt          # reasoned trajectory line
    assert "narrow" in prompt.lower()                  # the actionable hint reached the planner
    assert "WHAT THE LAST ROUND LEARNED" in prompt     # the hard directive header
    assert "scope_overclaim" in prompt
    assert decision["continue"] is True


def test_campaign_requires_pivot_when_reason_not_recoverable(monkeypatch):
    # did_not_generalize: the effect was not there on held-out data -> re-tuning it is forbidden.
    out = _outcome(1, "did_not_generalize",
                   "the effect calibrated on explore did NOT appear on confirm; change the effect.",
                   recoverable=False)
    prompt, _ = _campaign_prompt(monkeypatch, [out])
    assert "pivot to a DIFFERENT open question" in prompt
    assert "did_not_generalize" in prompt


def test_campaign_recoverable_reason_demands_acting_on_hint(monkeypatch):
    out = _outcome(1, "threshold_too_strong",
                   "the effect is present but the bar was too high; recalibrate conservatively.",
                   recoverable=True)
    prompt, _ = _campaign_prompt(monkeypatch, [out])
    assert "directly act on this hint" in prompt
    assert "pivot to a DIFFERENT open question" not in prompt


def test_campaign_no_demonstration_round_omits_directive(monkeypatch):
    # a non-paradigm round (no reason / no_demonstration) must not inject a learning directive.
    out = _outcome(1, "no_demonstration", "", recoverable=True, verdict="approve")
    out["reason"] = "no_demonstration"
    prompt, _ = _campaign_prompt(monkeypatch, [out])
    assert "WHAT THE LAST ROUND LEARNED" not in prompt
    assert "WHY [" not in prompt
