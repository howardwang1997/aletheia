"""F7-S1 adversarial checks for the evaluator/research path and capability boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from aletheia.evals.boundary import (
    EvaluationBoundary,
    EvaluationBoundaryError,
    validate_capabilities,
)


def _boundary(tmp_path: Path) -> EvaluationBoundary:
    return EvaluationBoundary(
        research_workspace=tmp_path / "research",
        submission_inbox=tmp_path / "inbox",
        evaluator_workspace=tmp_path / "evaluator",
        hidden_assets_root=tmp_path / "hidden",
    )


def test_research_can_write_only_workspace_or_submission_inbox(tmp_path):
    boundary = _boundary(tmp_path)
    assert boundary.assert_research_write(tmp_path / "research" / "result.json")
    assert boundary.assert_research_write(tmp_path / "inbox" / "submission.json")
    with pytest.raises(EvaluationBoundaryError):
        boundary.assert_research_write(tmp_path / "hidden" / "gold.json")
    with pytest.raises(EvaluationBoundaryError):
        boundary.assert_research_write(tmp_path / "outside.json")


def test_path_traversal_is_rejected_after_canonicalization(tmp_path):
    boundary = _boundary(tmp_path)
    attack = tmp_path / "research" / ".." / "hidden" / "gold.json"
    with pytest.raises(EvaluationBoundaryError, match="evaluator assets"):
        boundary.assert_research_read(attack)


def test_overlapping_security_roots_are_rejected(tmp_path):
    with pytest.raises(EvaluationBoundaryError, match="must be disjoint"):
        EvaluationBoundary(
            research_workspace=tmp_path / "shared",
            submission_inbox=tmp_path / "shared" / "inbox",
            evaluator_workspace=tmp_path / "eval",
            hidden_assets_root=tmp_path / "hidden",
        )


def test_research_plane_cannot_request_scorer_or_hidden_asset_capabilities():
    validate_capabilities(plane="research", requested={"read_public_task", "write_submission"})
    with pytest.raises(EvaluationBoundaryError, match="forbidden capabilities"):
        validate_capabilities(plane="research", requested={"read_hidden_asset"})
