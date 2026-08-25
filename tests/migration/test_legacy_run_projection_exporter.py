from __future__ import annotations

from pathlib import Path

import pytest

from scripts.export_legacy_run_projections import _artifact_objects, _resolve_workspace


def test_run_workspace_cannot_alias_another_runs_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    actual = root / ("b" * 32)
    actual.mkdir()
    alias = root / ("a" * 32)
    alias.symlink_to(actual, target_is_directory=True)

    with pytest.raises(ValueError, match="workspace cannot be a symlink"):
        _resolve_workspace(root.resolve(strict=True), "a" * 32)


def test_artifact_export_rejects_nested_symlinks_and_unknown_file_types(tmp_path: Path) -> None:
    workspace = tmp_path / ("a" * 32)
    workspace.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    (workspace / "linked.json").symlink_to(outside)

    with pytest.raises(ValueError, match="contains a symlink"):
        _artifact_objects(workspace)

    (workspace / "linked.json").unlink()
    (workspace / "unexpected.csv").write_text("private,unreviewed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported artifact type"):
        _artifact_objects(workspace)
