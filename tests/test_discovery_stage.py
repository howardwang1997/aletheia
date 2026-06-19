"""Driver-stage seams for the autonomous discovery loop (off by default).

Verifies the two non-trivial wirings without running a live campaign: a discovery-sourced hypothesis
SKIPS the direction gate (it already cleared the cross-vendor novelty gate inside discovery), and
when ``discovery_enabled`` the driver runs discovery IN PLACE OF single-shot ideation and adopts the
survivor (so ``reason_stage`` is never called).
"""

from __future__ import annotations

import asyncio

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
