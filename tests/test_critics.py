"""Phase 1 step 5: critic gateway — dry-run panel, persistence, consensus gate,
and (opt-in) a real GPT-5.5 call via the Codex Coding-Plan transport."""

from __future__ import annotations

import os

import pytest

from aletheia.critics.gateway import CriticGateway
from aletheia.critics.schemas import CriticFinding, Critique
from aletheia.db import create_all, session_scope
from aletheia.memory.ledger import CritiquePanel as CritiquePanelRow


def test_dry_run_panel_persists_and_passes_gate():
    create_all()
    gw = CriticGateway()
    panel = gw.review_sync("design", {"model": "random_forest"}, "exp-test-1", dry_run=True)
    assert panel.target == "design"
    assert len(panel.critiques) == 2  # supportive + adversarial
    assert panel.gate_passed is True
    assert panel.consensus_verdict in ("approve", "approve_with_changes")
    # persisted to the ledger
    with session_scope() as s:
        rows = s.query(CritiquePanelRow).filter(CritiquePanelRow.target_ref == "exp-test-1").all()
        assert len(rows) >= 1
        assert rows[-1].gate_passed is True


def test_real_gate_without_providers_fails_closed(monkeypatch):
    create_all()
    gw = CriticGateway()
    monkeypatch.setattr(gw, "_providers", lambda: [])

    panel = gw.review_sync("design", {"model": "random_forest"}, "exp-no-critics", dry_run=False)

    assert panel.consensus_verdict == "reject"
    assert panel.gate_passed is False
    assert panel.critiques[0].critic_id == "system"
    assert panel.critiques[0].findings[0].severity == "blocker"
    with session_scope() as s:
        rows = (
            s.query(CritiquePanelRow)
            .filter(CritiquePanelRow.target_ref == "exp-no-critics")
            .all()
        )
        assert len(rows) >= 1
        assert rows[-1].gate_passed is False


def test_consensus_blocker_fails_gate():
    gw = CriticGateway()
    critiques = [
        Critique(
            critic_id="openai",
            stance="adversarial",
            verdict="reject",
            confidence=0.9,
            summary="leakage",
            findings=[
                CriticFinding(
                    severity="blocker",
                    category="leakage",
                    claim="test set overlaps train",
                    evidence="shared formulas",
                    suggestion="dedupe across split",
                )
            ],
        )
    ]
    consensus, gate = gw._consensus(critiques)
    assert consensus == "reject"
    assert gate is False


@pytest.mark.skipif(
    os.environ.get("ALETHEIA_RUN_REAL_CRITIC") != "1",
    reason="set ALETHEIA_RUN_REAL_CRITIC=1 to spend a real GPT-5.5 critic call",
)
def test_real_critic_call():
    create_all()
    gw = CriticGateway()
    design = {
        "objective": "predict experimental band gap from composition",
        "dataset": "matbench_expt_gap (official folds)",
        "method": "matminer Magpie features -> RandomForest",
        "baselines": "mean predictor",
        "metrics": "MAE, R2 on held-out fold",
        "split": "official train/test folds",
    }
    panel = gw.review_sync("design", design, "exp-real-1", dry_run=False)
    assert len(panel.critiques) >= 2
    assert panel.consensus_verdict in ("approve", "approve_with_changes", "reject")
    for c in panel.critiques:
        assert c.summary  # non-empty real review
