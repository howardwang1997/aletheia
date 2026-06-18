"""Filesystem layout for per-run scratch + AI-generated artifacts.

Everything lives under a gitignored ``workspaces/`` at the repo root so a run's
data uploads, generated training scripts, and artifacts are isolated by run_id.
"""

from __future__ import annotations

from pathlib import Path

# aletheia/paths.py -> parents[1] == repo root
WORKSPACES_ROOT = Path(__file__).resolve().parents[1] / "workspaces"
# repo-level, gitignored home for human-inspectable run reports (e.g. e2e summaries, generated
# papers) — distinct from per-run scratch under ``workspaces/`` and from source docs under ``docs/``.
ARTIFACTS_ROOT = Path(__file__).resolve().parents[1] / "artifacts"


def artifacts_dir() -> Path:
    """Repo-level artifacts directory for human-inspectable reports (created on demand)."""
    ARTIFACTS_ROOT.mkdir(parents=True, exist_ok=True)
    return ARTIFACTS_ROOT


def run_workspace(run_id: str) -> Path:
    """Per-run scratch directory (created on demand)."""
    p = WORKSPACES_ROOT / run_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def run_data_dir(run_id: str) -> Path:
    """Where the human's uploaded datasets land for a run."""
    p = run_workspace(run_id) / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p


def run_artifacts_dir(run_id: str) -> Path:
    """Where generated artifacts (models, metrics.json, plots, reports) land."""
    p = run_workspace(run_id) / "artifacts"
    p.mkdir(parents=True, exist_ok=True)
    return p
