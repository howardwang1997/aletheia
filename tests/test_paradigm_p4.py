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


def test_run_demonstration_fails_closed_on_unmatched_claim():
    # the domain cannot compute THIS demonstration -> None (don't ground with a mismatched
    # demo). Returns before any data load, so this is fast + offline.
    p = get_domain_plugin("molecules")
    demo = p.run_demonstration(
        {"claim": "a brand-new entropy theory of solubility", "form": "x"},
        {"source": "benchmark", "ref": "esol"}, "/tmp/none",
    )
    assert demo is None


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


def test_paradigm_guard_blocks_without_demonstration():
    from aletheia.db import create_all
    from aletheia.memory.service import create_run

    create_all()
    run_id = create_run("paradigm noguard", domain="molecules", status="planned")
    d = ExperimentDriver(run_id, dry_run=False)
    d.profile = get_domain_plugin("molecules").profile()
    d.hypothesis = {"contribution_type": "paradigm",
                    "demonstration": {"form": "x", "claim": "c"}}
    result = {"metrics": {}, "artifacts": [], "info": {}}  # demonstration never computed
    assert asyncio.run(d._post_execution_guards(result, None)) is False


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
