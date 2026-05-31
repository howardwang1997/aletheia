"""Phase A-2: research front-end — deep-research SURVEY (briefing + gaps + ingested
literature), IDEATE (a chosen hypothesis persisted to the experiment), and the
novelty/feasibility direction gate. All dry-run (no network, no spend)."""

from __future__ import annotations

import asyncio

from aletheia.db import create_all, session_scope
from aletheia.memory.ledger import CritiquePanel, Experiment, MemoryChunk
from aletheia.memory.service import create_run, finalize_plan
from aletheia.scheduler.driver import ExperimentDriver


def _setup():
    create_all()
    run_id = create_run("frontend test", domain="materials", status="scoping")
    exp_id = finalize_plan(
        run_id,
        {"objective": "predict band gap", "domain": "materials", "direction": "composition ML"},
    )
    return run_id, exp_id


def test_survey_dry_returns_briefing_and_gaps_and_ingests():
    run_id, exp_id = _setup()
    d = ExperimentDriver(run_id, dry_run=True)
    brief, gaps = asyncio.run(d._survey({"objective": "predict band gap", "direction": "composition ML"}, exp_id))
    assert brief and len(gaps) >= 1
    with session_scope() as s:
        n = s.query(MemoryChunk).filter(
            MemoryChunk.run_id == run_id, MemoryChunk.kind == "literature"
        ).count()
        assert n >= 1


def test_ideate_dry_persists_hypothesis():
    run_id, exp_id = _setup()
    d = ExperimentDriver(run_id, dry_run=True)
    d.survey_brief, d.survey_gaps = "prior work...", ["a concrete gap"]
    hypo = asyncio.run(d._ideate({"objective": "predict band gap"}, exp_id))
    assert hypo.get("statement")
    with session_scope() as s:
        assert s.get(Experiment, exp_id).hypothesis  # persisted to the experiment


def test_direction_gate_dry_passes_and_records_panel():
    run_id, exp_id = _setup()
    d = ExperimentDriver(run_id, dry_run=True)
    d.hypothesis = {"statement": "a novel, feasible hypothesis"}
    d.survey_brief, d.survey_gaps = "prior work", ["gap"]
    ok = asyncio.run(d._direction_gate({"objective": "predict band gap"}, exp_id))
    assert ok is True
    with session_scope() as s:
        panel = s.query(CritiquePanel).filter(
            CritiquePanel.target == "direction", CritiquePanel.target_ref == exp_id
        ).first()
        assert panel is not None and panel.gate_passed is True
