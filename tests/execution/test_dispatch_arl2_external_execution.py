"""Unit coverage for the bridge-dispatch driver (scripts/dispatch_arl2_external_execution.py).

The v2 allocator regressions prove the eight lifecycle methods; this file
proves the operator surface that drives them: the full dispatch verb against
the external test ledger (admit → authorize → workload under bridge custody
→ launch accept → termination → terminal artifacts → settle → reader tail),
crash-resume replay from the persisted ladder records (the workload must NOT
run twice, and every stage contract must reach the allocator byte-identically),
and the custody fail-closed paths (state pin, database sha, key pin, lease
token).

The driver's deployment-state composition (registry tree, artifact store,
reader config) is Linux deployment material authored by author-arl2-deployments;
the test patches the _compose_allocator seam and runs everything else for
real, including the workload subprocess.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

import aletheia.execution.allocator as allocator_module
from aletheia.db import session_factory
from aletheia.execution.runtime_contracts import ExternalBridgeAuthority
from aletheia.execution.runtime_v2_contracts import MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from postgres_test_safety import require_isolated_pr4_postgres  # noqa: E402
from test_allocator import (  # noqa: E402
    BRIDGE_PRIVATE_KEY,
    _EXECUTION_TABLES,
    _prepared,
)
from test_allocator_external_v2 import _issuer  # noqa: E402
import dispatch_arl2_external_execution as driver  # noqa: E402

# The workload appends one line per invocation: after a resumed dispatch the
# file must still hold exactly one line, proving the workload ran once.
WORKLOAD_SCRIPT = """
import json, os
out = os.environ["ALETHEIA_WORKLOAD_OUTPUT"]
with open(os.path.join(out, "invocations.txt"), "a") as handle:
    handle.write(os.environ["ALETHEIA_ATTEMPT_ID"] + "\\n")
with open(os.path.join(out, "diagnostic_report.json"), "w") as handle:
    json.dump({"attempt": os.environ["ALETHEIA_ATTEMPT_ID"], "rows": 4}, handle, sort_keys=True)
"""


@pytest.fixture(autouse=True)
def _clean_execution_tables() -> Iterator[None]:
    require_isolated_pr4_postgres()
    sessions = session_factory()
    with sessions() as session, session.begin():
        session.execute(text(f"TRUNCATE {', '.join(_EXECUTION_TABLES)} RESTART IDENTITY CASCADE"))
    yield
    require_isolated_pr4_postgres()
    with sessions() as session, session.begin():
        session.execute(text(f"TRUNCATE {', '.join(_EXECUTION_TABLES)} RESTART IDENTITY CASCADE"))


class _WallClock:
    """Driver-side virtual wall clock; the ledger follows it.

    Every signed contract must order after the request that precedes it and
    land inside its freshness window against database time, so the test lets
    database time read the driver's current virtual moment: each contract
    timestamp is fresh (age >= 0) and each stage sees a clock at or past the
    contracts it verifies.
    """

    def __init__(self, base) -> None:
        self._moment = base
        self._step = timedelta(seconds=1)

    def __call__(self):
        self._moment += self._step
        return self._moment

    def peek(self):
        return self._moment


class _Harness:
    """One commissioned external window: fixture allocator behind the seam."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        prepared = _prepared(
            monkeypatch,
            external=True,
            runtime_control_issuer=_issuer(),
            artifact_quota_bytes=MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
        )
        self.prepared = prepared
        self.working_root = tmp_path / "working"
        self.working_root.mkdir()
        workload_output = tmp_path / "workload-output"
        workload_output.mkdir()
        (tmp_path / "invocations.txt").write_text("")
        self.workload_output = workload_output

        configs = self.working_root / "configs"
        configs.mkdir()
        self.state_path = configs / "arl2-deployment-state.json"
        self.state_path.write_text(
            json.dumps(
                {
                    "schema_name": "aletheia.arl2_deployment_state",
                    "database_url_sha256": driver._sha256_bytes(
                        os.environ["ALETHEIA_DATABASE_URL"].encode("utf-8")
                    ),
                    "qualification": {
                        "external_bridge_pin": prepared.bridge_pin.model_dump(mode="json"),
                    },
                }
            )
        )
        (self.working_root / "bundle.json").write_text(prepared.bundle.model_dump_json())
        (self.working_root / "grant.json").write_text(prepared.grant.model_dump_json())

        bridge_authority = ExternalBridgeAuthority(
            manifest=prepared.manifest, bridge_authority_pin=prepared.bridge_pin
        )
        reader = SimpleNamespace(
            artifact_verifier_principal_id="principal:bridge-verifier",
            artifact_object_store_id="bridge-store-0",
        )
        monkeypatch.setattr(
            driver,
            "_compose_allocator",
            lambda _state, _state_path, _args: (
                prepared.allocator,
                reader,
                bridge_authority,
                BRIDGE_PRIVATE_KEY,
            ),
        )
        self._clock = _WallClock(prepared.observed_at)
        monkeypatch.setattr(driver, "_utc_now", self._clock)
        monkeypatch.setattr(
            allocator_module, "_database_time", lambda _session: self._clock.peek()
        )

    def argv(self, **overrides) -> list[str]:
        values = {
            "deployment_state": str(self.state_path),
            "database_url": os.environ["ALETHEIA_DATABASE_URL"],
            "bundle": str(self.working_root / "bundle.json"),
            "grant": str(self.working_root / "grant.json"),
            "workload_command": [sys.executable, "-c", WORKLOAD_SCRIPT],
            "workload_output": str(self.workload_output),
            "evidence": None,
            **overrides,
        }
        argv = ["dispatch-arl2-external-execution"]
        argv += ["--deployment-state", values["deployment_state"]]
        argv += ["--database-url", values["database_url"]]
        argv += ["--bundle", values["bundle"], "--grant", values["grant"]]
        argv += ["--workload-output", values["workload_output"]]
        if values["evidence"] is not None:
            argv += ["--evidence", values["evidence"]]
        argv += ["--acknowledge", "DISPATCH_ARL2_EXTERNAL_EXECUTION"]
        argv += ["--workload-command", *values["workload_command"]]
        return argv


def _run(monkeypatch: pytest.MonkeyPatch, harness: _Harness, capsys, **overrides) -> dict:
    monkeypatch.setattr(sys, "argv", harness.argv(**overrides))
    assert driver.main() == 0
    evidence = json.loads(
        capsys.readouterr().out.rsplit("DISPATCH_EVIDENCE ", 1)[1].strip()
    )
    capsys.readouterr()
    return evidence


def test_dispatch_drives_the_ladder_and_prints_reader_evidence(
    monkeypatch, tmp_path, capsys
) -> None:
    harness = _Harness(monkeypatch, tmp_path)
    evidence_path = tmp_path / "evidence.json"
    first = _run(monkeypatch, harness, capsys, evidence=str(evidence_path))

    assert first["status"] == "succeeded"
    assert first["disposition"] == "process_succeeded"
    assert first["outbox_authority_kind"] == "accepted_terminal_submission"
    assert first["artifact_count"] == 2  # diagnostic_report.json + invocations.txt
    assert first["workload"]["exit_code"] == 0
    assert json.loads(evidence_path.read_text()) == first

    records = Path(first["dispatch_records"])
    assert records.parent == harness.working_root / "dispatch"
    assert (records / "lease-token").exists()
    assert stat.S_IMODE((records / "lease-token").stat().st_mode) == 0o400
    for name in (
        "runtime-preparation",
        "launch-authorization-request",
        "executor-identity",
        "launch-evidence",
        "launch-receipt",
        "workload-outcome",
        "termination-evidence",
        "termination-receipt",
        "artifact-manifest",
        "terminal-submission",
    ):
        assert (records / f"{name}.json").exists(), name
    assert (harness.workload_output / "invocations.txt").read_text().count("\n") == 1

    with session_factory()() as session:
        row = session.execute(
            text("SELECT status FROM execution_attempts WHERE attempt_id = :id"),
            {"id": first["attempt_id"]},
        ).scalar_one()
        assert row == "succeeded"


def test_re_run_replays_the_persisted_ladder_without_rerunning_the_workload(
    monkeypatch, tmp_path, capsys
) -> None:
    harness = _Harness(monkeypatch, tmp_path)
    first = _run(monkeypatch, harness, capsys)

    # replay-admit returns lease_token=None: the driver must re-attach through
    # its own 0400 custody file and replay every stage byte-identically
    second = _run(monkeypatch, harness, capsys)
    for field in (
        "attempt_id",
        "terminal_authority_sha256",
        "lineage_sha256",
        "material_sha256",
        "artifact_manifest_sha256",
        "charged_microunits",
        "outbox_id",
    ):
        assert second[field] == first[field], field
    assert (harness.workload_output / "invocations.txt").read_text().count("\n") == 1


def test_custody_failures_close_exactly(monkeypatch, tmp_path) -> None:
    harness = _Harness(monkeypatch, tmp_path)

    # a foreign database URL is refused before any session opens
    bad_url = list(harness.argv())
    bad_url[bad_url.index("--database-url") + 1] = "postgresql://other/database"
    monkeypatch.setattr(sys, "argv", bad_url)
    with pytest.raises(SystemExit, match="database URL sha differs"):
        driver.main()

    # a state file without the deployment schema is refused
    harness.state_path.write_text("{}")
    argv = harness.argv()
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit, match="not an ARL-2 deployment state"):
        driver.main()

    # a key file that does not match its pin is refused
    key_file = tmp_path / "bridge.key"
    key_file.write_bytes(b"\x01" * 32)
    with pytest.raises(SystemExit, match="differs from its deployment pin"):
        driver._load_key(
            key_file,
            pin_public_hex=driver._public_key_hex(BRIDGE_PRIVATE_KEY),
            label="external-bridge",
        )

    # a token custody file that does not hash to the pinned token is refused
    ladder = driver._Ladder(tmp_path / "dispatch" / "attempt-x")
    custody = ladder.root / "lease-token"
    custody.write_text("not-the-token\n")
    os.chmod(custody, 0o400)
    with pytest.raises(SystemExit, match="does not hash to the attempt's pinned token"):
        driver._recover_lease_token(
            ladder,
            SimpleNamespace(attempt_id="attempt-x", lease_token_sha256="0" * 64),
            SimpleNamespace(lease_token_file=None),
        )
