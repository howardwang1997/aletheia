from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from aletheia.migration.legacy import (
    LegacyDataClass,
    LegacyFreezeRequest,
    LegacyObjectRole,
    LegacySnapshotManifest,
    LegacySnapshotInput,
)
from scripts.freeze_legacy_snapshot import (
    _freeze,
    _git_exporter_identity,
    _parser,
    _require_committed_exporter,
)


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _exporter_repository(tmp_path: Path) -> Path:
    root = tmp_path / "exporter"
    root.mkdir()
    _git(root, "init", "--quiet")
    _git(root, "config", "user.email", "migration-test@example.invalid")
    _git(root, "config", "user.name", "Migration Test")
    (root / "exporter.py").write_text("VERSION = 1\n", encoding="utf-8")
    (root / "other.py").write_text("UNRELATED = True\n", encoding="utf-8")
    _git(root, "add", "exporter.py", "other.py")
    _git(root, "commit", "--quiet", "-m", "freeze exporter")
    return root


def _request(
    identity: dict[str, object], *, exporter_sha: str | None = None
) -> LegacyFreezeRequest:
    return LegacyFreezeRequest(
        source_system="legacy-test",
        source_scope="run/example",
        source_version="export/v1",
        redaction_manifest_sha256="0" * 64,
        exporter_git_commit=identity["commit"],
        exporter_git_tree=identity["tree"],
        exporter_entrypoint=identity["entrypoint"],
        exporter_entrypoint_sha256=identity["entrypoint_sha256"],
        exporter_code_sha256=exporter_sha or identity["exporter_code_sha256"],
        objects=(
            LegacySnapshotInput(
                logical_name="contract",
                source_relative_path="contract.json",
                role=LegacyObjectRole.GOLDEN_CONTRACT,
                media_type="application/json",
                data_class=LegacyDataClass.DEV_FIXTURE,
            ),
        ),
    )


def test_clean_exporter_identity_is_derived_and_required(tmp_path: Path) -> None:
    root = _exporter_repository(tmp_path)
    identity = _git_exporter_identity(root, "exporter.py")

    assert identity["dirty"] is False
    assert identity["commit"] == _git(root, "rev-parse", "HEAD")
    assert identity["tree"] == _git(root, "rev-parse", "HEAD^{tree}")
    assert identity["entrypoint"] == "exporter.py"
    assert len(identity["entrypoint_sha256"]) == 64
    assert len(identity["exporter_code_sha256"]) == 64
    assert (
        _require_committed_exporter(
            _request(identity),
            exporter_root=root,
            exporter_entrypoint="exporter.py",
        )
        == identity
    )


def test_dirty_exporter_is_rejected_even_for_dev_fixture(tmp_path: Path) -> None:
    root = _exporter_repository(tmp_path)
    identity = _git_exporter_identity(root, "exporter.py")
    (root / "untracked.py").write_text("DIRTY = True\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="dirty exporter"):
        _require_committed_exporter(
            _request(identity),
            exporter_root=root,
            exporter_entrypoint="exporter.py",
        )


def test_request_cannot_claim_an_invented_commit_tree_or_code_hash(tmp_path: Path) -> None:
    root = _exporter_repository(tmp_path)
    identity = _git_exporter_identity(root, "exporter.py")

    with pytest.raises(ValueError, match="exporter code hash"):
        _request(identity, exporter_sha="f" * 64)

    wrong_commit = _request(identity).model_copy(update={"exporter_git_commit": "f" * 40})
    with pytest.raises(ValueError, match="exporter_git_commit"):
        _require_committed_exporter(
            wrong_commit,
            exporter_root=root,
            exporter_entrypoint="exporter.py",
        )


def test_exporter_root_must_be_repository_top_level(tmp_path: Path) -> None:
    root = _exporter_repository(tmp_path)
    nested = root / "nested"
    nested.mkdir()

    with pytest.raises(ValueError, match="top level"):
        _git_exporter_identity(nested, "exporter.py")


def test_exporter_entrypoint_must_exist_be_tracked_and_not_be_a_symlink(
    tmp_path: Path,
) -> None:
    root = _exporter_repository(tmp_path)

    with pytest.raises(ValueError, match="normalized repository-relative"):
        _git_exporter_identity(root, "../exporter.py")

    with pytest.raises(FileNotFoundError, match="missing"):
        _git_exporter_identity(root, "missing.py")

    (root / "untracked.py").write_text("UNTRACKED = True\n", encoding="utf-8")
    with pytest.raises(ValueError, match="tracked at HEAD"):
        _git_exporter_identity(root, "untracked.py")

    (root / "linked.py").symlink_to("exporter.py")
    _git(root, "add", "linked.py")
    _git(root, "commit", "--quiet", "-m", "tracked exporter symlink")
    with pytest.raises(ValueError, match="symlink"):
        _git_exporter_identity(root, "linked.py")


def test_unrelated_cli_entrypoint_cannot_satisfy_the_request(tmp_path: Path) -> None:
    root = _exporter_repository(tmp_path)
    identity = _git_exporter_identity(root, "exporter.py")

    with pytest.raises(ValueError, match="exporter_entrypoint"):
        _require_committed_exporter(
            _request(identity),
            exporter_root=root,
            exporter_entrypoint="other.py",
        )


def test_freeze_cli_binds_full_exporter_identity_and_creates_manifest_once(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exporter = _exporter_repository(tmp_path)
    identity = _git_exporter_identity(exporter, "exporter.py")
    request = _request(identity)
    source = tmp_path / "source"
    source.mkdir()
    (source / "contract.json").write_text("{}\n", encoding="utf-8")
    request_path = tmp_path / "request.json"
    request_path.write_text(request.model_dump_json(), encoding="utf-8")
    output_manifest = tmp_path / "exports" / "manifest.json"
    arguments = argparse.Namespace(
        request=request_path,
        exporter_root=exporter,
        exporter_entrypoint="exporter.py",
        source_root=source,
        snapshot_store=tmp_path / "store",
        output_manifest=output_manifest,
    )

    _freeze(arguments)

    stdout_manifest = json.loads(capsys.readouterr().out)
    stored_manifest = json.loads(output_manifest.read_text(encoding="utf-8"))
    assert stored_manifest == stdout_manifest
    assert stored_manifest["exporter_identity_scheme"] == "git_tracked_entrypoint_v1"
    assert stored_manifest["exporter_git_commit"] == identity["commit"]
    assert stored_manifest["exporter_git_tree"] == identity["tree"]
    assert stored_manifest["exporter_entrypoint"] == "exporter.py"
    assert stored_manifest["exporter_entrypoint_sha256"] == identity["entrypoint_sha256"]
    assert stored_manifest["exporter_code_sha256"] == identity["exporter_code_sha256"]
    assert stored_manifest["exporter_execution_assurance"] == "operator_attested"
    assert stored_manifest["freezer_identity"]["entrypoint"] == (
        "scripts/freeze_legacy_snapshot.py"
    )
    LegacySnapshotManifest.model_validate(stored_manifest)

    tampered = json.loads(json.dumps(stored_manifest))
    tampered["freezer_identity"]["code_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="freezer code hash"):
        LegacySnapshotManifest.model_validate(tampered)

    with pytest.raises(FileExistsError, match="refusing to replace frozen output"):
        _freeze(arguments)


def test_freeze_parser_requires_exporter_source_and_store_roots() -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(["freeze", "request.json"])
