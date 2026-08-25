from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aletheia.research_kernel.schemas import ResearchCharterVersion, canonical_json_bytes
from aletheia.research_store.cli import main

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
