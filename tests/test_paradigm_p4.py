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
        {"random_state": 42, "sample_n": 600}, {"source": "benchmark", "ref": "esol"}, "/tmp/demo_test"
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
