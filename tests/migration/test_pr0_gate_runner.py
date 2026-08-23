from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts.run_pr0_gate import gate_pytest_args, golden_test_nodeids


ROOT = Path(__file__).resolve().parents[2]
EXPECTED_GOLDEN_NODEIDS = {
    "tests/test_domain_materials.py::test_materials_plugin_trains_and_scores",
    "tests/test_domain_molecules.py::test_molecule_plugin_trains_and_scores",
    "tests/test_domain_rag.py::test_run_experiment_returns_quality_metrics",
    "tests/test_driver_e2e.py::test_full_dry_run_loop",
    "tests/test_phase0_skeleton.py::test_dryrun_persists_events_and_worklog",
    "tests/test_rag_e2e.py::test_rag_dry_run_reaches_archive",
}


def test_pr0_gate_executes_every_frozen_golden_node() -> None:
    nodeids = golden_test_nodeids()
    assert set(nodeids) == EXPECTED_GOLDEN_NODEIDS
    assert gate_pytest_args() == ("-q", "tests/migration", *nodeids)


def test_memory_service_import_is_not_masked_by_test_collection_order() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from aletheia.memory.service import create_run; assert callable(create_run)",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_documented_pr0_gate_cli_collects_from_repo_root() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/run_pr0_gate.py"), "--collect-only"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "tests/migration/test_pr0_gate_runner.py" in result.stdout
    for nodeid in EXPECTED_GOLDEN_NODEIDS:
        assert nodeid in result.stdout
