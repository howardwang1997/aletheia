from __future__ import annotations

import argparse
import json
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aletheia.research_kernel.schemas import ResearchCharterVersion, canonical_json_bytes
from aletheia.research_store.cli import main
from aletheia.research_store.cli import _existing_root_archive

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "research_kernel_store.py"
_HASH = "a" * 64


def _charter() -> ResearchCharterVersion:
    return ResearchCharterVersion(
        quest_id="qst_" + "1" * 32,
        charter_id="charter:cli-test",
        version=1,
        mission="Test operator CAS ingestion",
        value_boundaries=("honesty",),
        included_scopes=("fixture",),
        allowed_action_classes=("characterize",),
        safety_policy_sha256=_HASH,
        ethics_policy_sha256=_HASH,
        license_policy_sha256=_HASH,
        privacy_policy_sha256=_HASH,
        egress_policy_sha256=_HASH,
        budget_policy_sha256=_HASH,
        approval_policy_sha256=_HASH,
        publication_policy_sha256=_HASH,
        amendment_principal_ids=("human:owner",),
        emergency_stop_principal_ids=("human:owner",),
        authorized_by_principal_id="human:owner",
        authority_receipt_sha256=_HASH,
        authorized_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )


def test_direct_script_help_has_a_working_repository_import_path() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "archive-object" in result.stdout
    assert "audit" in result.stdout


def test_archive_object_cli_stages_and_reports_exact_metadata(tmp_path: Path, capsysbinary) -> None:
    charter = _charter()
    source = tmp_path / "charter.json"
    source.write_bytes(canonical_json_bytes(charter))

    assert (
        main(
            [
                "archive-object",
                "--input",
                str(source),
                "--cas-root",
                str(tmp_path / "cas"),
            ]
        )
        == 0
    )

    output = json.loads(capsysbinary.readouterr().out)
    assert output["object_ref"]["object_sha256"] == charter.object_sha256
    target = tmp_path / "cas" / output["storage_key"]
    assert target.read_bytes() == canonical_json_bytes(charter)


@pytest.mark.parametrize("command", ["audit", "replay"])
def test_audit_commands_require_an_external_trust_root(
    tmp_path: Path,
    command: str,
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                command,
                "--quest-id",
                "qst_" + "1" * 32,
                "--cas-root",
                str(tmp_path / "cas"),
            ]
        )

    assert exc_info.value.code == 2


def _audit_namespace(cas_root: Path) -> argparse.Namespace:
    return argparse.Namespace(cas_root=cas_root, max_object_bytes=64 * 1024 * 1024)


def test_audit_archive_composes_from_the_live_root_custody_shape(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private-cas"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    shared = tmp_path / "shared-cas"
    shared.mkdir(mode=0o750)
    shared.chmod(0o750)

    for root, directory_mode, object_mode in (
        (private, 0o700, 0o400),
        (shared, 0o750, 0o440),
    ):
        archive = _existing_root_archive(_audit_namespace(root))
        assert archive.directory_mode == directory_mode
        assert archive.object_mode == object_mode
        assert archive.read_only is False


@pytest.mark.parametrize("command", ["audit", "replay"])
def test_audit_commands_refuse_roots_outside_the_pinned_custody_shapes(
    tmp_path: Path,
    command: str,
) -> None:
    loose = tmp_path / "cas"
    loose.mkdir(mode=0o755)
    loose.chmod(0o755)

    with pytest.raises(SystemExit, match="outside the pinned custody shapes"):
        main(
            [
                command,
                "--quest-id",
                "qst_" + "1" * 32,
                "--trust-root",
                str(tmp_path / "trust.json"),
                "--cas-root",
                str(loose),
            ]
        )


@pytest.mark.parametrize("command", ["audit", "replay"])
def test_audit_commands_refuse_an_absent_cas_root(
    tmp_path: Path,
    command: str,
) -> None:
    with pytest.raises(SystemExit, match="is not a directory"):
        main(
            [
                command,
                "--quest-id",
                "qst_" + "1" * 32,
                "--trust-root",
                str(tmp_path / "trust.json"),
                "--cas-root",
                str(tmp_path / "cas"),
            ]
        )


@pytest.mark.parametrize("command", ["audit", "replay"])
def test_audit_commands_refuse_a_symlinked_cas_root(
    tmp_path: Path,
    command: str,
) -> None:
    real = tmp_path / "real-cas"
    real.mkdir(mode=0o750)
    real.chmod(0o750)
    link = tmp_path / "cas"
    link.symlink_to(real)

    with pytest.raises(SystemExit, match="cannot be a symlink"):
        main(
            [
                command,
                "--quest-id",
                "qst_" + "1" * 32,
                "--trust-root",
                str(tmp_path / "trust.json"),
                "--cas-root",
                str(link),
            ]
        )


def test_archive_object_cli_stages_onto_an_existing_group_read_root(
    tmp_path: Path,
    capsysbinary,
) -> None:
    cas = tmp_path / "cas"
    cas.mkdir(mode=0o750)
    cas.chmod(0o750)
    charter = _charter()
    source = tmp_path / "charter.json"
    source.write_bytes(canonical_json_bytes(charter))

    assert main(["archive-object", "--input", str(source), "--cas-root", str(cas)]) == 0

    output = json.loads(capsysbinary.readouterr().out)
    target = cas / output["storage_key"]
    assert target.read_bytes() == canonical_json_bytes(charter)
    assert stat.S_IMODE(target.stat().st_mode) == 0o440


def test_archive_object_cli_refuses_a_symlinked_cas_root(tmp_path: Path) -> None:
    real = tmp_path / "real-cas"
    real.mkdir(mode=0o750)
    real.chmod(0o750)
    link = tmp_path / "cas"
    link.symlink_to(real)
    charter = _charter()
    source = tmp_path / "charter.json"
    source.write_bytes(canonical_json_bytes(charter))

    with pytest.raises(SystemExit, match="cannot be a symlink"):
        main(["archive-object", "--input", str(source), "--cas-root", str(link)])
