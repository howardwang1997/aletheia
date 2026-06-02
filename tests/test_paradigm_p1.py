"""Paradigm-mode P1 — vocabulary + honesty (no gate change). A `formulation` claim type
exists; IDEATE captures a `contribution_type`; a PARADIGM hypothesis gets a first-class
`formulation` claim (proposed/speculative, since no demonstration grounds it yet) instead of
being flattened into a metric. Offline, no spend. See docs/PARADIGM_MODE_DESIGN.md."""

from __future__ import annotations

import asyncio

import aletheia.scheduler.driver as drv
from aletheia.db import create_all
from aletheia.memory.ledger import CLAIM_TYPES
from aletheia.memory.service import create_run, finalize_plan, list_claims
from aletheia.scheduler.driver import ExperimentDriver


def test_formulation_is_a_claim_type():
    assert "formulation" in CLAIM_TYPES


def _run_ideate(monkeypatch, hypo_json: str):
    create_all()
    run_id = create_run("paradigm p1", domain="materials", status="planned")
    exp_id = finalize_plan(run_id, {"objective": "x", "domain": "materials"})

    async def fake_reason(run_id, stage, prompt, **kw):
        return hypo_json

    monkeypatch.setattr(drv, "reason_stage", fake_reason)
    d = ExperimentDriver(run_id, dry_run=True)
    asyncio.run(d._ideate({"objective": "x"}, exp_id))
    return run_id, d


def test_paradigm_hypothesis_creates_formulation_claim(monkeypatch):
    run_id, d = _run_ideate(
        monkeypatch,
        '{"statement": "reframe solubility prediction as a ranking problem", "rationale": "r", '
        '"prediction": "p", "novelty_note": "n", "contribution_type": "paradigm", '
        '"demonstration": {"form": "discriminating_instance", "claim": "two molecules RMSE rates '
        'equal but a chemist ranks differently"}}',
    )
    assert d._contribution_type() == "paradigm"
    claims = list_claims(run_id)
    form = [c for c in claims if c["claim_type"] == "formulation"]
    assert form, "a paradigm hypothesis must record a formulation claim"
    # ungrounded until a discriminating demonstration exists — honest, not a finding
    assert form[0]["status"] == "proposed" and form[0]["strength"] == "speculative"


def test_performance_hypothesis_creates_no_formulation_claim(monkeypatch):
    run_id, d = _run_ideate(
        monkeypatch,
        '{"statement": "GBT beats RF on ESOL RMSE", "rationale": "r", "prediction": "p", '
        '"novelty_note": "n"}',  # no contribution_type -> defaults to performance
    )
    assert d._contribution_type() == "performance"
    claims = list_claims(run_id)
    assert not any(c["claim_type"] == "formulation" for c in claims)
    assert any(c["claim_type"] == "novelty" for c in claims)  # novelty still recorded
