"""Paradigm-mode P4 — the demonstration EXECUTOR. A domain computes the discriminating
statistic DETERMINISTICALLY (harness-owned, no LLM 'holds' assertion); the driver runs it
on paradigm runs and feeds it to P3's grounding. Uses REAL ESOL data for the molecules
demonstration (no spend). See docs/PARADIGM_MODE_DESIGN.md."""

from __future__ import annotations

import asyncio

from aletheia.domains.registry import get_domain_plugin
from aletheia.scheduler.driver import ExperimentDriver


# --- the default: domains without a computable demonstration return None --------
def test_default_run_demonstration_is_none():
    # materials has no run_demonstration override -> base default
    assert get_domain_plugin("materials").run_demonstration({}, {}, "/tmp/none") is None


# --- molecules: a REAL, deterministic discriminating demonstration on ESOL ------
def test_molecules_demonstration_computes_real_scaffold_gap():
    """The incumbent random-split RMSE is blind to scaffold generalization: a real,
    computed (not asserted) gap on MoleculeNet ESOL. Subsampled for test speed."""
    p = get_domain_plugin("molecules")
    demo = p.run_demonstration(
        {"claim": "random-split RMSE is blind to scaffold generalization",
         "random_state": 42, "sample_n": 600},
        {"source": "benchmark", "ref": "esol"}, "/tmp/demo_test",
    )
    assert demo is not None
    assert demo["form"] == "impossibility"
    assert isinstance(demo["holds"], bool)
    # scaffold-grouped RMSE is >= random-split RMSE on ESOL — a genuine discriminating gap
    assert demo["statistic"] > 1.0
    assert "RMSE" in demo["detail"]


# --- molecules dispatches to the demonstration the hypothesis describes ----------
def test_run_demonstration_dispatches_to_activity_cliff():
    p = get_domain_plugin("molecules")
    demo = p.run_demonstration(
        {"claim": "activity cliffs force a huge local Lipschitz constant", "sample_n": 600},
        {"source": "benchmark", "ref": "esol"}, "/tmp/cliff_test",
    )
    assert demo["form"] == "impossibility"
    assert "Lipschitz" in demo["detail"]  # the cliff demo, not the scaffold one
    assert isinstance(demo["holds"], bool) and demo["statistic"] >= 0


def test_run_demonstration_dispatches_to_scaffold():
    p = get_domain_plugin("molecules")
    demo = p.run_demonstration(
        {"claim": "random-split RMSE is blind to scaffold generalization", "sample_n": 600},
        {"source": "benchmark", "ref": "esol"}, "/tmp/scaf_test",
    )
    assert "scaffold-grouped RMSE" in demo["detail"]


def test_cliff_demo_is_seed_perturbed_for_real_reproduction():
    # different seeds -> different 90% subsamples -> different statistic (a genuine
    # re-computation, so the reproduction check is real, not a bit-identical recompute).
    p = get_domain_plugin("molecules")
    a = p.run_demonstration({"claim": "cliff lipschitz", "random_state": 1, "sample_n": 700},
                            {"source": "benchmark", "ref": "esol"}, "/tmp/c1")
    b = p.run_demonstration({"claim": "cliff lipschitz", "random_state": 2, "sample_n": 700},
                            {"source": "benchmark", "ref": "esol"}, "/tmp/c2")
    assert isinstance(a["holds"], bool) and isinstance(b["holds"], bool)
    assert a["statistic"] != b["statistic"]  # seed genuinely perturbs the computation


def test_results_payload_caps_solution_code():
    big = "x" * 50000
    payload = ExperimentDriver._results_review_payload(
        {"model": "m", "solution_code": big}, {"metrics": {}, "info": {}}, analysis={}, claims=[],
    )
    assert len(payload["solution_code"]) == 20000  # capped so CLI critics never hit ARG_MAX


def test_run_demonstration_fails_closed_on_unmatched_claim():
    # the domain cannot compute THIS demonstration -> None (don't ground with a mismatched
    # demo). Returns before any data load, so this is fast + offline.
    p = get_domain_plugin("molecules")
    demo = p.run_demonstration(
        {"claim": "a brand-new entropy theory of solubility", "form": "x"},
        {"source": "benchmark", "ref": "esol"}, "/tmp/none",
    )
    assert demo is None


# --- the capability REGISTRY (Codex #2): explicit, enumerable, id-dispatched ------
def test_capability_registry_is_explicit_and_empty_by_default():
    # molecules registers exactly its two computable demonstrations; materials registers
    # none (so a paradigm claim there stays unverified — honest fail-closed).
    mol = get_domain_plugin("molecules").demonstration_capabilities()
    assert set(mol) == {
        "activity_cliff_lipschitz", "scaffold_generalization_gap", "leakage_slope_law",
        "ai_authored_demonstration",  # the frontier path: every domain gets this, merged from base
    }
    assert all(c.description and callable(c.compute) for c in mol.values())
    # materials adds no hand-built demos, but every domain still gets the AI-authored capability
    # (reachable by explicit id only — an empty/untagged spec still fails closed).
    assert set(get_domain_plugin("materials").demonstration_capabilities()) == {
        "ai_authored_demonstration"
    }


def test_run_demonstration_dispatches_by_explicit_capability_id():
    # an explicit capability id WINS over the (here, non-matching) free-text claim: the
    # harness computes EXACTLY the registered capability IDEATE chose, not a keyword guess.
    p = get_domain_plugin("molecules")
    demo = p.run_demonstration(
        {"claim": "an opaque restatement with no routing keywords", "sample_n": 600,
         "capability": "activity_cliff_lipschitz"},
        {"source": "benchmark", "ref": "esol"}, "/tmp/cap_id",
    )
    assert demo is not None and "Lipschitz" in demo["detail"]  # routed by id, not keyword


def test_demonstration_result_is_stamped_with_capability_and_factor():
    # the computed result carries the capability id + its reproduction tolerance, so the
    # artifact/critic/reproduction path all key off the registered capability (Codex #2/#5).
    p = get_domain_plugin("molecules")
    demo = p.run_demonstration(
        {"claim": "random-split RMSE is blind to scaffold generalization", "sample_n": 600},
        {"source": "benchmark", "ref": "esol"}, "/tmp/stamp",
    )
    assert demo["capability"] == "scaffold_generalization_gap"
    assert demo["reproduce_factor"] == 2.0


def test_unknown_capability_id_falls_back_then_fails_closed():
    # an unrecognized capability id is not a free pass: with no keyword match either, it
    # fails closed (None) rather than running an unrelated demonstration.
    p = get_domain_plugin("molecules")
    demo = p.run_demonstration(
        {"claim": "no routing keywords here", "capability": "made_up_capability"},
        {"source": "benchmark", "ref": "esol"}, "/tmp/badid",
    )
    assert demo is None


# --- the leakage-slope LAW: the predictive frame, executed (not just the premise) ----
def test_leakage_law_capability_registered():
    caps = get_domain_plugin("molecules").demonstration_capabilities()
    assert "leakage_slope_law" in caps
    assert caps["leakage_slope_law"].reproduce_factor == 1.5  # tighter than the ratio capabilities


def test_leakage_law_computes_real_grid_on_esol():
    """The three-part law, COMPUTED on real ESOL across a five-model grid: a slope->penalty
    rho with a bootstrap CI, a ranking-disagreement tau, and a counterfactual tau. Subsampled
    + small bootstrap for test speed."""
    p = get_domain_plugin("molecules")
    demo = p.run_demonstration(
        {"claim": "the random-split-only leakage slope forecasts each model's scaffold penalty",
         "random_state": 42, "sample_n": 500, "n_boot": 150},
        {"source": "benchmark", "ref": "esol"}, "/tmp/law_grid",
    )
    assert demo is not None and demo["form"] == "predictive_law"
    assert isinstance(demo["holds"], bool)
    assert set(demo["grid"]) == {"ridge", "knn", "rf", "gbm", "svr"}
    for row in demo["grid"].values():
        assert {"rmse_random", "rmse_scaffold", "penalty", "slope"} <= set(row)
    assert demo["slope_penalty_rho"] is None or -1.0 <= demo["slope_penalty_rho"] <= 1.0
    assert "rho=" in demo["detail"]
    assert demo["capability"] == "leakage_slope_law" and demo["reproduce_factor"] == 1.5


def test_leakage_law_dispatched_by_explicit_id():
    # an explicit capability id routes to the law even with an opaque, keyword-free claim.
    p = get_domain_plugin("molecules")
    demo = p.run_demonstration(
        {"claim": "an opaque restatement with no routing keywords", "sample_n": 200,
         "n_boot": 50, "capability": "leakage_slope_law"},
        {"source": "benchmark", "ref": "esol"}, "/tmp/law_id",
    )
    assert demo is not None and demo["form"] == "predictive_law"


def test_leakage_law_keyword_fallback_beats_scaffold_premise():
    # a law claim mentions "scaffold" too, but the law branch is checked FIRST so the
    # predictive law wins over the scaffold-gap premise (untagged spec).
    p = get_domain_plugin("molecules")
    demo = p.run_demonstration(
        {"claim": "the leakage-slope law predicts each model's scaffold penalty", "sample_n": 200,
         "n_boot": 50},
        {"source": "benchmark", "ref": "esol"}, "/tmp/law_kw",
    )
    assert demo["capability"] == "leakage_slope_law"


def test_leakage_law_is_seed_perturbed_for_real_reproduction():
    # different seeds reshuffle the random-split folds -> a different rho (the random part of
    # the law genuinely re-computes), so demonstration_reproduced is a real stability check.
    p = get_domain_plugin("molecules")
    a = p.run_demonstration({"capability": "leakage_slope_law", "random_state": 1,
                             "sample_n": 400, "n_boot": 80},
                            {"source": "benchmark", "ref": "esol"}, "/tmp/law_s1")
    b = p.run_demonstration({"capability": "leakage_slope_law", "random_state": 2,
                             "sample_n": 400, "n_boot": 80},
                            {"source": "benchmark", "ref": "esol"}, "/tmp/law_s2")
    assert a["statistic"] != b["statistic"]  # seed genuinely perturbs the random-split half


def test_leakage_law_fails_closed_on_degenerate_data():
    # too few molecules/scaffolds for a grid + slope -> holds False, statistic None, no crash.
    p = get_domain_plugin("molecules")
    demo = p.run_demonstration(
        {"capability": "leakage_slope_law", "sample_n": 40},
        {"source": "benchmark", "ref": "esol"}, "/tmp/law_small",
    )
    assert demo["holds"] is False and demo["statistic"] is None
    assert "Fail-closed" in demo["detail"]


def test_results_payload_carries_computed_demonstration_result():
    # the cross-vendor critic must see the COMPUTED result, not just the proposed spec
    result = {"metrics": {}, "info": {"demonstration":
              {"form": "impossibility", "holds": True, "statistic": 9.0, "detail": "L_cliff/L_global=9"}}}
    payload = ExperimentDriver._results_review_payload(
        {"model": "x"}, result, analysis={}, claims=[], contribution_type="paradigm",
        demonstration={"form": "impossibility", "claim": "cliffs force a huge Lipschitz constant"},
    )
    assert payload["demonstration_result"]["holds"] is True and payload["demonstration_result"]["statistic"] == 9.0
    assert payload["demonstration"]["claim"]  # the proposed spec is still carried too


# --- paradigm runs are gated on the DEMONSTRATION, not a fitted model ----------
def test_paradigm_guard_passes_on_demonstration_without_model():
    from aletheia.db import create_all
    from aletheia.memory.service import create_run

    create_all()
    run_id = create_run("paradigm guard", domain="molecules", status="planned")
    d = ExperimentDriver(run_id, dry_run=False)
    d.profile = get_domain_plugin("molecules").profile()
    d.hypothesis = {"contribution_type": "paradigm",
                    "demonstration": {"form": "impossibility", "claim": "cliff"}}
    # performance eval FAILED: no eval/model artifacts, no metrics — but demonstration present
    result = {"metrics": {}, "artifacts": [{"kind": "demonstration"}],
              "info": {"demonstration": {"holds": True}, "eval_error": "sandbox timeout"}}
    assert asyncio.run(d._post_execution_guards(result, "exp1")) is True


def test_paradigm_guard_does_not_block_without_demonstration():
    # K2 S3.5: a missing demonstration is no longer hard-paused in the guard — guards pass
    # (paradigm is verified by its demonstration, not artifacts), and the campaign loop turns
    # the missing demonstration into a bounded PIVOT / fail-closed pause instead.
    from aletheia.db import create_all
    from aletheia.memory.service import create_run

    create_all()
    run_id = create_run("paradigm noguard", domain="molecules", status="planned")
    d = ExperimentDriver(run_id, dry_run=False)
    d.profile = get_domain_plugin("molecules").profile()
    d.hypothesis = {"contribution_type": "paradigm",
                    "demonstration": {"form": "x", "claim": "c"}}
    result = {"metrics": {}, "artifacts": [], "info": {}}  # demonstration never computed
    assert asyncio.run(d._post_execution_guards(result, None)) is True


def test_capability_menu_lists_registered_capabilities():
    # IDEATE surfaces the harness's computable demonstrations so the LLM picks one by id
    # (a real run); empty when the domain has none.
    d = ExperimentDriver("rid-menu", dry_run=True)
    d.plugin = get_domain_plugin("molecules")
    menu = d._capability_menu()
    assert "activity_cliff_lipschitz" in menu and "scaffold_generalization_gap" in menu
    d.plugin = get_domain_plugin("materials")
    assert d._capability_menu() == ""


def test_run_eval_computes_demonstration_when_perf_eval_fails(monkeypatch):
    d = ExperimentDriver("rid-eval-fail", dry_run=False)
    d.hypothesis = {"contribution_type": "paradigm",
                    "demonstration": {"form": "impossibility", "claim": "cliff Lipschitz"}}

    async def boom(design, data_spec, domain, exp_id):
        raise RuntimeError("sandbox timeout")

    async def fake_demo(design, data_spec, domain):
        return {"form": "impossibility", "holds": True, "statistic": 9.0, "detail": "d"}

    monkeypatch.setattr(d, "_dispatch_eval", boom)
    monkeypatch.setattr(d, "_compute_demonstration", fake_demo)
    result = asyncio.run(d._run_eval({}, {}, "molecules", None))
    assert result["info"]["demonstration"]["holds"] is True  # demo computed despite eval failure
    assert result["info"]["eval_error"] == "sandbox timeout"
    assert any(a["kind"] == "demonstration" for a in result["artifacts"])


# --- driver wiring: demonstration runs for paradigm runs, skipped in dry-run ----
def test_compute_demonstration_skipped_in_dry_run():
    d = ExperimentDriver("rid-dry", dry_run=True)
    d.hypothesis = {"contribution_type": "paradigm",
                    "demonstration": {"form": "impossibility", "claim": "x"}}
    assert asyncio.run(d._compute_demonstration({}, {}, "molecules")) is None


def test_compute_demonstration_runs_for_paradigm(monkeypatch):
    d = ExperimentDriver("rid-real", dry_run=False)
    d.hypothesis = {"contribution_type": "paradigm",
                    "demonstration": {"form": "impossibility", "claim": "metric is blind"}}

    class _Plugin:
        def run_demonstration(self, demonstration, data_spec, workdir):
            return {"form": "impossibility", "holds": True, "statistic": 1.65, "detail": "computed"}

    d.plugin = _Plugin()
    demo = asyncio.run(d._compute_demonstration({}, {"ref": "esol"}, "molecules"))
    assert demo["holds"] is True and demo["statistic"] == 1.65


def test_run_eval_attaches_demonstration_for_paradigm(monkeypatch):
    """_run_eval feeds the demonstration into info so reproduction recomputes it and P3
    grounds the formulation claim."""
    d = ExperimentDriver("rid-eval", dry_run=False)
    d.hypothesis = {"contribution_type": "paradigm",
                    "demonstration": {"form": "impossibility", "claim": "blind"}}

    async def fake_dispatch(design, data_spec, domain, exp_id):
        return {"metrics": {"rmse": 1.0}, "info": {}}

    async def fake_compute(design, data_spec, domain):
        return {"form": "impossibility", "holds": True, "statistic": 1.5, "detail": "d"}

    monkeypatch.setattr(d, "_dispatch_eval", fake_dispatch)
    monkeypatch.setattr(d, "_compute_demonstration", fake_compute)
    result = asyncio.run(d._run_eval({}, {}, "molecules", None))
    assert result["info"]["demonstration"]["holds"] is True
