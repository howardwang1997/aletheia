from __future__ import annotations

from pathlib import Path

import pytest

from aletheia.migration.boundary import (
    DEFAULT_PURE_CONTRACT_TARGET_MODULES,
    find_dependency_boundary_violations,
    find_legacy_driver_import_violations,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _source(root: Path, relative_path: str, source: str = "") -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _minimal_pr3_repository(tmp_path: Path, *, legacy_driver: bool = False) -> Path:
    _source(tmp_path, "aletheia/__init__.py")
    _source(tmp_path, "aletheia/research_kernel/__init__.py")
    _source(tmp_path, "aletheia/protocols/__init__.py")
    _source(tmp_path, "aletheia/protocols/schemas.py")
    _source(tmp_path, "aletheia/execution/__init__.py")
    _source(tmp_path, "aletheia/execution/schemas.py")
    _source(tmp_path, "aletheia/execution/ports.py")
    if legacy_driver:
        _source(tmp_path, "aletheia/migration/__init__.py")
        _source(
            tmp_path,
            "aletheia/migration/dynamic_loader.py",
            (REPOSITORY_ROOT / "aletheia/migration/dynamic_loader.py").read_text(encoding="utf-8"),
        )
        _source(tmp_path, "aletheia/scheduler/__init__.py")
        _source(tmp_path, "aletheia/scheduler/driver.py", "class ExperimentDriver:\n    pass\n")
        _source(
            tmp_path,
            "aletheia/scheduler/durable.py",
            "from aletheia.scheduler.driver import ExperimentDriver\n",
        )
    return tmp_path


def test_repository_pr3_boundaries_are_non_vacuous_and_clean() -> None:
    required_sources = (
        "aletheia/protocols/__init__.py",
        "aletheia/protocols/base.py",
        "aletheia/execution/__init__.py",
        "aletheia/execution/schemas.py",
        "aletheia/execution/ports.py",
        "aletheia/migration/protocol_v1_compatibility.py",
    )
    assert all((REPOSITORY_ROOT / relative).is_file() for relative in required_sources)
    assert find_dependency_boundary_violations(REPOSITORY_ROOT) == ()
    assert find_legacy_driver_import_violations(REPOSITORY_ROOT) == ()


def test_execution_pure_contract_allowlist_is_exact() -> None:
    assert DEFAULT_PURE_CONTRACT_TARGET_MODULES == (
        "aletheia.execution",
        "aletheia.execution.ports",
        "aletheia.execution.schemas",
    )


@pytest.mark.parametrize(
    "forbidden_import",
    [
        "aletheia.capabilities",
        "aletheia.config",
        "aletheia.db",
        "aletheia.domains",
        "aletheia.epistemics",
        "aletheia.evals",
        "aletheia.jobs",
        "aletheia.research_store",
        "aletheia.scheduler",
        "sqlalchemy",
        "pathlib",
        "requests",
        "subprocess",
        "importlib",
    ],
)
@pytest.mark.parametrize(
    "protected_source",
    ["aletheia/protocols/schemas.py", "aletheia/execution/schemas.py"],
)
def test_pr3_pure_contracts_reject_forbidden_dependencies(
    tmp_path: Path,
    forbidden_import: str,
    protected_source: str,
) -> None:
    root = _minimal_pr3_repository(tmp_path)
    _source(root, protected_source, f"import {forbidden_import}\n")

    violations = find_dependency_boundary_violations(root)

    assert any(item.imported_module == forbidden_import for item in violations)


@pytest.mark.parametrize(
    "source",
    [
        "open('ambient-input.json', 'rb')\n",
        "reader = open\nreader('ambient-input.json', 'rb')\n",
        "reader = open.__call__\nreader('ambient-input.json', 'rb')\n",
        "import io as streams\nstreams.open('ambient-input.json', 'rb')\n",
    ],
)
def test_protocols_reject_ambient_builtin_filesystem_access(
    tmp_path: Path,
    source: str,
) -> None:
    root = _minimal_pr3_repository(tmp_path)
    _source(root, "aletheia/protocols/compiler.py", source)

    violations = find_dependency_boundary_violations(root)

    assert any(
        item.root_module == "aletheia.protocols.compiler"
        and item.imported_module == "<filesystem-api>"
        for item in violations
    )


def test_protocol_reexport_cannot_hide_transitive_legacy_dependency(tmp_path: Path) -> None:
    root = _minimal_pr3_repository(tmp_path)
    _source(root, "aletheia/protocols/__init__.py", "from aletheia.facade import catalog\n")
    _source(root, "aletheia/facade/__init__.py", "from .catalog import CATALOG\n")
    _source(root, "aletheia/facade/catalog.py", "from aletheia.jobs import queue\nCATALOG = {}\n")
    _source(root, "aletheia/jobs/__init__.py")
    _source(root, "aletheia/jobs/queue.py")

    violations = find_dependency_boundary_violations(root)

    assert any(
        item.root_module == "aletheia.protocols.schemas"
        and item.imported_module == "aletheia.jobs"
        and "aletheia.facade.catalog" in item.dependency_chain
        for item in violations
    )


def test_execution_initializer_cannot_hide_network_dependency(tmp_path: Path) -> None:
    root = _minimal_pr3_repository(tmp_path)
    _source(root, "aletheia/execution/__init__.py", "from aletheia.transport import CLIENT\n")
    _source(root, "aletheia/transport.py", "import socket\nCLIENT = object()\n")

    violations = find_dependency_boundary_violations(root)

    assert any(
        item.root_module == "aletheia.execution.ports"
        and item.imported_module == "socket"
        and "aletheia.transport" in item.dependency_chain
        for item in violations
    )


def test_future_operational_execution_module_is_not_misclassified_as_pure(
    tmp_path: Path,
) -> None:
    root = _minimal_pr3_repository(tmp_path)
    _source(root, "aletheia/execution/node_agent.py", "import os\n")

    assert find_dependency_boundary_violations(root) == ()


def test_pure_protocol_can_depend_on_pure_kernel_contract(tmp_path: Path) -> None:
    root = _minimal_pr3_repository(tmp_path)
    _source(root, "aletheia/research_kernel/schemas.py", "KERNEL_VERSION = 'v1'\n")
    _source(
        root,
        "aletheia/protocols/schemas.py",
        "from aletheia.research_kernel.schemas import KERNEL_VERSION\n",
    )

    assert find_dependency_boundary_violations(root) == ()


def test_read_only_v1_compatibility_leaf_cannot_import_either_authority_graph(
    tmp_path: Path,
) -> None:
    root = _minimal_pr3_repository(tmp_path)
    _source(root, "aletheia/migration/__init__.py")
    _source(
        root,
        "aletheia/migration/protocol_v1_compatibility.py",
        "import aletheia.protocols.schemas\n",
    )

    violations = find_dependency_boundary_violations(root)

    assert any(
        item.root_module == "aletheia.migration.protocol_v1_compatibility"
        and item.imported_module == "aletheia.protocols.schemas"
        for item in violations
    )


@pytest.mark.parametrize(
    "package_initializer",
    [
        "from .protocol_v1_compatibility import F9V1WholeObjectBinding\n",
        "from .facade import F9V1WholeObjectBinding\n",
    ],
)
def test_migration_package_cannot_reexport_compatibility_leaf_directly_or_indirectly(
    tmp_path: Path,
    package_initializer: str,
) -> None:
    root = _minimal_pr3_repository(tmp_path)
    _source(root, "aletheia/migration/__init__.py", package_initializer)
    _source(
        root,
        "aletheia/migration/protocol_v1_compatibility.py",
        "class F9V1WholeObjectBinding:\n    pass\n",
    )
    if ".facade" in package_initializer:
        _source(
            root,
            "aletheia/migration/facade.py",
            "from .protocol_v1_compatibility import F9V1WholeObjectBinding\n",
        )

    violations = find_dependency_boundary_violations(root)

    assert any(
        item.root_module == "aletheia.migration"
        and item.imported_module == "aletheia.migration.protocol_v1_compatibility"
        for item in violations
    )


@pytest.mark.parametrize(
    ("driver_source", "protected_module"),
    [
        ("import aletheia.protocols.schemas\n", "aletheia.protocols.schemas"),
        (
            "import aletheia.legacy_bridge\n",
            "aletheia.execution.ports",
        ),
    ],
)
def test_legacy_driver_cannot_reach_pr3_authority_directly_or_transitively(
    tmp_path: Path,
    driver_source: str,
    protected_module: str,
) -> None:
    root = _minimal_pr3_repository(tmp_path, legacy_driver=True)
    _source(root, "aletheia/scheduler/driver.py", driver_source)
    if "legacy_bridge" in driver_source:
        _source(root, "aletheia/legacy_bridge.py", "import aletheia.execution.ports\n")

    violations = find_legacy_driver_import_violations(root)

    assert any(
        item.root_module == "aletheia.scheduler.driver" and item.imported_module == protected_module
        for item in violations
    )
