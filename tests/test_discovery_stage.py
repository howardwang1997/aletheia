"""Driver-stage seams for the autonomous discovery loop (off by default).

Verifies the two non-trivial wirings without running a live campaign: a discovery-sourced hypothesis
SKIPS the direction gate (it already cleared the cross-vendor novelty gate inside discovery), and
when ``discovery_enabled`` the driver runs discovery IN PLACE OF single-shot ideation and adopts the
survivor (so ``reason_stage`` is never called).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import numpy as np

import aletheia.scheduler.driver as drv
from aletheia.config import get_settings
from aletheia.db import create_all
from aletheia.memory.service import create_run
from aletheia.scheduler.driver import ExperimentDriver


def test_direction_gate_skips_discovery_sourced():
    create_all()
    run_id = create_run("disc-gate", domain="materials", status="planned")
    d = ExperimentDriver(run_id, dry_run=False)
    d.hypothesis = {"statement": "x", "discovery_sourced": True}

    async def _boom(*a, **k):  # the gate must NOT call the panel for a discovery-sourced hypothesis
        raise AssertionError("gateway.review called for a discovery-sourced hypothesis")

    d.gateway.review = _boom
    assert asyncio.run(d._direction_gate({}, None)) is True


def test_ideate_adopts_discovery_when_enabled(monkeypatch):
    create_all()
    run_id = create_run("disc-ideate", domain="materials", status="planned")
    d = ExperimentDriver(run_id, dry_run=False)
    monkeypatch.setattr(get_settings(), "discovery_enabled", True)

    async def _fake_discover(plan, exp_id):
        d.hypothesis = {"statement": "discovered effect", "discovery_sourced": True}
        d._discovered_demo = {"code": "def compute_demonstration(X, y, groups, meta):\n    return {}\n",
                              "prereg": {"supported_if": {"op": ">=", "threshold": 0.1},
                                         "control_silent_if": {"op": "<=", "threshold": 0.1}}}
        return True

    d._discover = _fake_discover

    async def _boom_reason(*a, **k):  # single-shot ideation must NOT run when discovery adopts a survivor
        raise AssertionError("reason_stage called despite a discovery survivor")

    monkeypatch.setattr(drv, "reason_stage", _boom_reason)

    hyp = asyncio.run(d._ideate({"objective": "x"}, None))
    assert hyp.get("discovery_sourced") is True
    assert hyp["statement"] == "discovered effect"
    assert d._discovered_demo and d._discovered_demo["code"]  # carried for _demonstration_code to reuse


def test_driver_discovery_screens_only_sealed_explore_and_excludes_ideator(monkeypatch):
    """Candidate selection must never see CONFIRM, and the idea/code author cannot review itself."""
    import aletheia.research.discovery as discovery

    create_all()
    run_id = create_run("disc-seal", domain="materials", status="planned")
    d = ExperimentDriver(run_id, dry_run=False)
    d.plugin = SimpleNamespace(name="materials")
    d.domain = "materials"

    vendor = SimpleNamespace(id="grok", transport="api", model="grok-test", base_url="https://x.test")
    settings = SimpleNamespace(
        demonstration_explore_confirm_enabled=True,
        discovery_ideator_vendor="grok",
        discovery_coauthor=False,
        discovery_k_survivors=1,
        discovery_max_rounds=2,
        orchestrator_vendor="openai",
        critics=SimpleNamespace(active=[vendor]),
        vendor_key=lambda _vid: "test-key",
        vendor_base_url=lambda _vid: None,
    )
    monkeypatch.setattr(drv, "get_settings", lambda: settings)
    monkeypatch.setattr(drv, "resolve_data_spec", lambda _rid: {"target_column": "gap"})

    split_meta = {"seed": 42, "n_explore": 40, "n_confirm": 60, "index_hash": "sealed"}
    monkeypatch.setattr(
        d, "_stage_explore_arrays",
        lambda *_a: (np.zeros((40, 4)), np.zeros(40), np.array(["A-B"] * 40),
                     list(range(40, 100)), split_meta),
    )
    monkeypatch.setattr(discovery, "make_vendor_ideator", lambda *_a, **_k: (lambda *_: []))
    captured = {}

    def fake_discover(**kwargs):
        captured.update(n=len(kwargs["y"]), excluded=kwargs["novelty_exclude"])
        cand = {"title": "sealed", "claim": "sealed claim", "insight": "i",
                "code": "def compute_demonstration(X, y, groups, meta):\n    return {}\n",
                "prereg": {"supported_if": {"op": ">=", "threshold": 1.0},
                           "control_silent_if": {"op": "<=", "threshold": 0.1}}}
        return [{"title": "sealed", "candidate": cand}], []

    monkeypatch.setattr(discovery, "discover", fake_discover)

    async def no_index(*_a, **_k):
        return None

    monkeypatch.setattr(d, "_index", no_index)
    assert asyncio.run(d._discover({}, None)) is True
    assert captured == {"n": 40, "excluded": {"grok"}}
    assert d._discovery_split_meta == split_meta
