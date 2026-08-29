from __future__ import annotations

import ast
from functools import cached_property, partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from aletheia.migration.boundary import (
    DEFAULT_AUDITED_DYNAMIC_LOADER_ESCAPE_COUNTS,
    DEFAULT_AUDITED_DYNAMIC_LOADER_SOURCES,
    DEFAULT_OPERATIONAL_PYTHON_ROOTS,
    DEFAULT_PRIVATE_EXECUTION_RECORD_SYMBOLS,
    find_dependency_boundary_violations,
    find_execution_persistence_import_violations,
    find_legacy_driver_import_violations,
    find_research_store_persistence_import_violations,
)
from aletheia.migration.dynamic_loader import (
    load_guarded_source_module,
    load_guarded_source_bytes,
    resolve_guarded_dynamic_attribute,
)
from scripts.durable_worker import _resolve_handler

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_GUARD_GLOBAL_REFERENCE: object | None = None


def _guard_global_wrapper() -> object | None:
    return _GUARD_GLOBAL_REFERENCE


def _identity(value: object) -> object:
    return value


def _source(root: Path, relative_path: str, source: str = "") -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _minimal_repository(tmp_path: Path) -> Path:
    _source(tmp_path, "aletheia/__init__.py")
    _source(tmp_path, "aletheia/research_kernel/__init__.py")
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


def test_repository_dependency_boundary_is_non_vacuous_and_clean() -> None:
    assert (REPOSITORY_ROOT / "aletheia/research_kernel/__init__.py").is_file()
    assert (REPOSITORY_ROOT / "aletheia/research_controller/__init__.py").is_file()
    assert find_dependency_boundary_violations(REPOSITORY_ROOT) == ()


def test_pure_kernel_cannot_import_operational_research_store(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(root, "aletheia/research_store/__init__.py")
    _source(
        root,
        "aletheia/research_kernel/escape.py",
        "from aletheia.research_store import store\n",
    )

    violations = find_dependency_boundary_violations(root)

    assert any(
        violation.root_module == "aletheia.research_kernel.escape"
        and violation.imported_module == "aletheia.research_store"
        for violation in violations
    )


def test_research_store_adapter_cannot_import_legacy_authority(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "aletheia/research_store/__init__.py",
        "from aletheia.jobs import outbox\n",
    )
    _source(root, "aletheia/jobs/__init__.py")
    _source(root, "aletheia/jobs/outbox.py")

    violations = find_dependency_boundary_violations(root)

    assert any(
        violation.root_module == "aletheia.research_store"
        and violation.imported_module == "aletheia.jobs.outbox"
        for violation in violations
    )


def test_repository_freezes_legacy_driver_to_durable_worker() -> None:
    assert find_legacy_driver_import_violations(REPOSITORY_ROOT) == ()


def test_repository_has_one_authoritative_research_store_writer() -> None:
    assert find_research_store_persistence_import_violations(REPOSITORY_ROOT) == ()


def test_repository_has_one_authoritative_execution_writer() -> None:
    assert find_execution_persistence_import_violations(REPOSITORY_ROOT) == ()


def test_execution_private_record_allowlist_matches_the_registered_schema() -> None:
    persistence_path = REPOSITORY_ROOT / "aletheia" / "execution" / "persistence.py"
    tree = ast.parse(persistence_path.read_text(encoding="utf-8"))
    record_symbols = {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name.startswith("_Execution")
        and node.name.endswith("Record")
    }

    assert record_symbols
    assert set(DEFAULT_PRIVATE_EXECUTION_RECORD_SYMBOLS) == record_symbols
    assert len(DEFAULT_PRIVATE_EXECUTION_RECORD_SYMBOLS) == len(record_symbols)


def test_new_module_cannot_import_execution_authority_records(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(root, "aletheia/execution/__init__.py")
    _source(root, "aletheia/execution/persistence.py")
    _source(
        root,
        "aletheia/rogue_execution_writer.py",
        "from aletheia.execution.persistence import _ExecutionAttemptRecord\n",
    )

    violations = find_execution_persistence_import_violations(root, allowed_importers=())

    assert any(
        violation.root_module == "aletheia.rogue_execution_writer"
        and violation.imported_module == "aletheia.execution.persistence"
        for violation in violations
    )


def test_execution_allocator_cannot_publicly_alias_private_record(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(root, "aletheia/execution/__init__.py")
    _source(root, "aletheia/execution/persistence.py")
    _source(
        root,
        "aletheia/execution/allocator.py",
        "from aletheia.execution.persistence import "
        "_ExecutionAttemptRecord as ExecutionAttemptRecord\n",
    )

    violations = find_execution_persistence_import_violations(
        root,
        allowed_importers=("aletheia.execution.allocator",),
    )

    assert any(
        violation.root_module == "aletheia.execution.allocator"
        and violation.import_kind == "public-execution-authority-symbol"
        for violation in violations
    )


@pytest.mark.parametrize(
    "rogue_source",
    [
        (
            "import aletheia.execution.allocator as adapter\n"
            "record = adapter._ExecutionAttemptRecord\n"
        ),
        (
            "from aletheia.execution import allocator as imported_allocator\n"
            "adapter = imported_allocator\n"
            "record = getattr(adapter, '_ExecutionAttemptRecord')\n"
        ),
        "from aletheia.execution.allocator import _ExecutionAttemptRecord\n",
        "from aletheia.execution.allocator import *\n",
    ],
)
def test_execution_allocator_private_record_cannot_be_recovered(
    tmp_path: Path,
    rogue_source: str,
) -> None:
    root = _minimal_repository(tmp_path)
    _source(root, "aletheia/execution/__init__.py")
    _source(root, "aletheia/execution/persistence.py")
    _source(
        root,
        "aletheia/execution/allocator.py",
        "from aletheia.execution.persistence import _ExecutionAttemptRecord\n",
    )
    _source(root, "aletheia/rogue_execution_writer.py", rogue_source)

    violations = find_execution_persistence_import_violations(
        root,
        allowed_importers=("aletheia.execution.allocator",),
    )

    assert any(
        violation.root_module == "aletheia.rogue_execution_writer"
        and violation.import_kind == "private-execution-authority-symbol"
        for violation in violations
    )


def test_execution_schema_registration_import_is_side_effect_only(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(root, "aletheia/execution/__init__.py")
    _source(root, "aletheia/execution/persistence.py")
    _source(
        root,
        "aletheia/schema_migrations.py",
        "import aletheia.execution.persistence  # register metadata only\n",
    )

    assert (
        find_execution_persistence_import_violations(
            root,
            allowed_importers=("aletheia.schema_migrations",),
        )
        == ()
    )


@pytest.mark.parametrize(
    "schema_source",
    [
        "from aletheia.execution.persistence import _ExecutionAttemptRecord\n",
        (
            "import aletheia.execution.persistence as records\n"
            "record = records._ExecutionAttemptRecord\n"
        ),
        (
            "import aletheia.execution.persistence\n"
            "record = aletheia.execution.persistence._ExecutionAttemptRecord\n"
        ),
        (
            "from aletheia.execution import persistence as records\n"
            "record = getattr(records, '_ExecutionAttemptRecord')\n"
        ),
        (
            "import aletheia.execution.persistence as records\n"
            "alias = records\n"
            "record = getattr(alias, dynamic_name)\n"
        ),
    ],
)
def test_execution_schema_registration_cannot_recover_private_records(
    tmp_path: Path,
    schema_source: str,
) -> None:
    root = _minimal_repository(tmp_path)
    _source(root, "aletheia/execution/__init__.py")
    _source(root, "aletheia/execution/persistence.py")
    _source(root, "aletheia/schema_migrations.py", schema_source)

    violations = find_execution_persistence_import_violations(
        root,
        allowed_importers=("aletheia.schema_migrations",),
    )

    assert any(
        violation.root_module == "aletheia.schema_migrations"
        and violation.import_kind == "schema-registration-execution-authority-access"
        and violation.imported_module.startswith("aletheia.execution.persistence:")
        for violation in violations
    )


def test_new_module_cannot_import_authoritative_store_records(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(root, "aletheia/research_store/__init__.py")
    _source(root, "aletheia/research_store/persistence.py")
    _source(
        root,
        "aletheia/rogue_writer.py",
        "from aletheia.research_store.persistence import ResearchKernelEventRecord\n",
    )

    violations = find_research_store_persistence_import_violations(
        root,
        allowed_importers=(),
    )

    assert any(
        violation.root_module == "aletheia.rogue_writer"
        and violation.imported_module == "aletheia.research_store.persistence"
        for violation in violations
    )


def test_public_store_module_cannot_reexport_private_orm_records(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(root, "aletheia/research_store/__init__.py")
    _source(root, "aletheia/research_store/persistence.py")
    _source(
        root,
        "aletheia/research_store/store.py",
        "from aletheia.research_store.persistence import ResearchKernelEventRecord\n",
    )
    _source(
        root,
        "aletheia/rogue_writer.py",
        "from aletheia.research_store.store import ResearchKernelEventRecord\n",
    )

    violations = find_research_store_persistence_import_violations(
        root,
        allowed_importers=("aletheia.research_store.store",),
    )

    assert any(
        violation.root_module == "aletheia.rogue_writer"
        and violation.import_kind == "private-authority-symbol"
        for violation in violations
    )
    assert any(
        violation.root_module == "aletheia.research_store.store"
        and violation.import_kind == "public-authority-symbol"
        for violation in violations
    )


@pytest.mark.parametrize(
    ("rogue_source", "symbol"),
    [
        (
            "import aletheia.research_store.store as adapter\n"
            "record = adapter.ResearchKernelEventRecord\n",
            "ResearchKernelEventRecord",
        ),
        (
            "import aletheia.research_store.store as adapter\n"
            "record = adapter.ResearchQuestAuthorityRecord\n",
            "ResearchQuestAuthorityRecord",
        ),
        (
            "from aletheia.research_store import store as imported_store\n"
            "adapter = imported_store\n"
            "record = adapter._ResearchKernelEventRecord\n",
            "_ResearchKernelEventRecord",
        ),
        (
            "import aletheia.research_store.store as adapter\n"
            "record = getattr(adapter, 'ResearchKernelEventRecord')\n",
            "ResearchKernelEventRecord",
        ),
        (
            "from aletheia.research_store.store import _ResearchKernelEventRecord\n",
            "_ResearchKernelEventRecord",
        ),
        (
            "import aletheia.research_store.store as adapter\n"
            "name = input()\n"
            "record = getattr(adapter, name)\n",
            "<dynamic-attribute>",
        ),
    ],
)
def test_private_orm_records_cannot_escape_through_store_module_attributes(
    tmp_path: Path,
    rogue_source: str,
    symbol: str,
) -> None:
    root = _minimal_repository(tmp_path)
    _source(root, "aletheia/research_store/__init__.py")
    _source(root, "aletheia/research_store/persistence.py")
    _source(
        root,
        "aletheia/research_store/store.py",
        "from aletheia.research_store.persistence import (\n"
        "    ResearchKernelEventRecord as _ResearchKernelEventRecord,\n"
        ")\n",
    )
    _source(root, "aletheia/rogue_writer.py", rogue_source)

    violations = find_research_store_persistence_import_violations(
        root,
        allowed_importers=("aletheia.research_store.store",),
    )

    assert any(
        violation.root_module == "aletheia.rogue_writer"
        and violation.import_kind == "private-authority-symbol"
        and violation.imported_module == f"aletheia.research_store.store:{symbol}"
        for violation in violations
    )


def test_dynamic_worker_registration_cannot_load_raw_legacy_driver() -> None:
    with pytest.raises(ValueError, match="raw legacy driver handlers are forbidden"):
        _resolve_handler("research.experiment_driver.v1=aletheia.scheduler.driver:ExperimentDriver")


def test_guarded_loader_rejects_raw_driver_before_import(monkeypatch: pytest.MonkeyPatch) -> None:
    imported = False

    def unexpected_import(_module_name: str) -> object:
        nonlocal imported
        imported = True
        raise AssertionError("raw driver request reached importlib")

    monkeypatch.setattr(
        "aletheia.migration.dynamic_loader._import_module",
        unexpected_import,
    )

    with pytest.raises(ValueError, match="raw legacy driver handlers are forbidden"):
        resolve_guarded_dynamic_attribute(
            "aletheia.scheduler.driver",
            "ExperimentDriver",
        )

    assert imported is False


def test_guarded_loader_rejects_indirect_driver_object(monkeypatch: pytest.MonkeyPatch) -> None:
    class ReexportedDriver:
        pass

    ReexportedDriver.__module__ = "aletheia.scheduler.driver"
    alias_module = SimpleNamespace(factory=partial(ReexportedDriver))
    monkeypatch.setattr(
        "aletheia.migration.dynamic_loader._import_module",
        lambda _module_name: alias_module,
    )

    with pytest.raises(ValueError, match="resolved dynamic object belongs"):
        resolve_guarded_dynamic_attribute("trusted.plugin", "factory")


def test_guarded_loader_rejects_raw_driver_subclass(monkeypatch: pytest.MonkeyPatch) -> None:
    class RawDriver:
        pass

    RawDriver.__module__ = "aletheia.scheduler.driver"

    class DriverSubclass(RawDriver):
        pass

    monkeypatch.setattr(
        "aletheia.migration.dynamic_loader._import_module",
        lambda _module_name: SimpleNamespace(factory=DriverSubclass),
    )

    with pytest.raises(ValueError, match="resolved dynamic object belongs"):
        resolve_guarded_dynamic_attribute("trusted.plugin", "factory")


def test_guarded_loader_rejects_function_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    class RawDriver:
        pass

    RawDriver.__module__ = "aletheia.scheduler.driver"

    def closure_wrapper() -> object:
        return RawDriver

    def default_wrapper(
        positional: object = RawDriver,
        *,
        keyword: object = RawDriver,
    ) -> tuple[object, object]:
        return positional, keyword

    monkeypatch.setitem(
        _guard_global_wrapper.__globals__,
        "_GUARD_GLOBAL_REFERENCE",
        RawDriver,
    )
    wrapped_values = (closure_wrapper, default_wrapper, _guard_global_wrapper)

    for wrapped_value in wrapped_values:
        monkeypatch.setattr(
            "aletheia.migration.dynamic_loader._import_module",
            lambda _module_name, value=wrapped_value: SimpleNamespace(factory=value),
        )
        with pytest.raises(ValueError, match="resolved dynamic object belongs"):
            resolve_guarded_dynamic_attribute("trusted.plugin", "factory")


def test_guarded_loader_rejects_callable_instance_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RawDriver:
        pass

    RawDriver.__module__ = "aletheia.scheduler.driver"

    class CallableWrapper:
        def __init__(self, value: object) -> None:
            self.value = value

        def __call__(self) -> object:
            return self.value

    class SlottedCallableWrapper:
        __slots__ = ("value",)

        def __init__(self, value: object) -> None:
            self.value = value

        def __call__(self) -> object:
            return self.value

    class ClassStateCallableWrapper:
        value = RawDriver

        def __call__(self) -> object:
            return self.value

    wrapped_values = (
        CallableWrapper(RawDriver),
        SlottedCallableWrapper(RawDriver),
        ClassStateCallableWrapper(),
    )

    for wrapped_value in wrapped_values:
        monkeypatch.setattr(
            "aletheia.migration.dynamic_loader._import_module",
            lambda _module_name, value=wrapped_value: SimpleNamespace(factory=value),
        )
        with pytest.raises(ValueError, match="resolved dynamic object belongs"):
            resolve_guarded_dynamic_attribute("trusted.plugin", "factory")


def test_guarded_loader_rejects_wrapped_callable_class_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RawDriver:
        pass

    RawDriver.__module__ = "aletheia.scheduler.driver"

    class CallableBase:
        def __call__(self) -> None:
            return None

    class ContainerWrapper(CallableBase):
        exports = {"driver": RawDriver}

    class StaticMethodWrapper(CallableBase):
        @staticmethod
        def factory(value: object = RawDriver) -> object:
            return value

    class ClassMethodWrapper(CallableBase):
        @classmethod
        def factory(cls, value: object = RawDriver) -> tuple[type, object]:
            return cls, value

    wrapped_values = (ContainerWrapper(), StaticMethodWrapper(), ClassMethodWrapper())

    for wrapped_value in wrapped_values:
        monkeypatch.setattr(
            "aletheia.migration.dynamic_loader._import_module",
            lambda _module_name, value=wrapped_value: SimpleNamespace(factory=value),
        )
        with pytest.raises(ValueError, match="resolved dynamic object belongs"):
            resolve_guarded_dynamic_attribute("trusted.plugin", "factory")


def test_guarded_loader_rejects_direct_class_state(monkeypatch: pytest.MonkeyPatch) -> None:
    class RawDriver:
        pass

    RawDriver.__module__ = "aletheia.scheduler.driver"

    class Wrapper:
        driver = RawDriver

    monkeypatch.setattr(
        "aletheia.migration.dynamic_loader._import_module",
        lambda _module_name: SimpleNamespace(factory=Wrapper),
    )

    with pytest.raises(ValueError, match="resolved dynamic object belongs"):
        resolve_guarded_dynamic_attribute("trusted.plugin", "factory")


def test_guarded_loader_rejects_safe_descriptor_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    class RawDriver:
        pass

    RawDriver.__module__ = "aletheia.scheduler.driver"

    class PropertyWrapper:
        @property
        def driver(self, hidden: object = RawDriver) -> object:
            return hidden

    class CachedPropertyWrapper:
        @cached_property
        def driver(self, hidden: object = RawDriver) -> object:
            return hidden

    for wrapped_value in (PropertyWrapper, CachedPropertyWrapper):
        monkeypatch.setattr(
            "aletheia.migration.dynamic_loader._import_module",
            lambda _module_name, value=wrapped_value: SimpleNamespace(factory=value),
        )
        with pytest.raises(ValueError, match="resolved dynamic object belongs"):
            resolve_guarded_dynamic_attribute("trusted.plugin", "factory")


def test_guarded_loader_rejects_function_custom_state(monkeypatch: pytest.MonkeyPatch) -> None:
    class RawDriver:
        pass

    RawDriver.__module__ = "aletheia.scheduler.driver"

    def attribute_wrapper() -> None:
        return None

    def annotation_wrapper() -> None:
        return None

    setattr(attribute_wrapper, "driver", RawDriver)
    annotation_wrapper.__annotations__["driver"] = RawDriver

    for wrapped_value in (attribute_wrapper, annotation_wrapper):
        monkeypatch.setattr(
            "aletheia.migration.dynamic_loader._import_module",
            lambda _module_name, value=wrapped_value: SimpleNamespace(factory=value),
        )
        with pytest.raises(ValueError, match="resolved dynamic object belongs"):
            resolve_guarded_dynamic_attribute("trusted.plugin", "factory")


def test_guarded_loader_rejects_partial_argument_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    class RawDriver:
        pass

    RawDriver.__module__ = "aletheia.scheduler.driver"
    wrapped_values = (partial(_identity, RawDriver), partial(_identity, value=RawDriver))

    for wrapped_value in wrapped_values:
        monkeypatch.setattr(
            "aletheia.migration.dynamic_loader._import_module",
            lambda _module_name, value=wrapped_value: SimpleNamespace(factory=value),
        )
        with pytest.raises(ValueError, match="resolved dynamic object belongs"):
            resolve_guarded_dynamic_attribute("trusted.plugin", "factory")


def test_guarded_loader_rejects_container_reexports(monkeypatch: pytest.MonkeyPatch) -> None:
    class RawDriver:
        pass

    RawDriver.__module__ = "aletheia.scheduler.driver"
    wrapped_values = (
        {"driver": RawDriver},
        {RawDriver: "driver"},
        [RawDriver],
        (RawDriver,),
        {RawDriver},
        frozenset({RawDriver}),
    )

    for wrapped_value in wrapped_values:
        monkeypatch.setattr(
            "aletheia.migration.dynamic_loader._import_module",
            lambda _module_name, value=wrapped_value: SimpleNamespace(factory=value),
        )
        with pytest.raises(ValueError, match="resolved dynamic object belongs"):
            resolve_guarded_dynamic_attribute("trusted.plugin", "factory")


def test_guarded_loader_fails_closed_when_function_origins_cannot_be_inspected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def safe_factory() -> None:
        return None

    def unavailable_closure_inspection(_function: object) -> object:
        raise RuntimeError("inspection unavailable")

    monkeypatch.setattr(
        "aletheia.migration.dynamic_loader._import_module",
        lambda _module_name: SimpleNamespace(factory=safe_factory),
    )
    monkeypatch.setattr(
        "aletheia.migration.dynamic_loader.inspect.getclosurevars",
        unavailable_closure_inspection,
    )

    with pytest.raises(ValueError, match="could not safely inspect dynamic function origins"):
        resolve_guarded_dynamic_attribute("trusted.plugin", "factory")


def test_guarded_loader_returns_non_driver_object(monkeypatch: pytest.MonkeyPatch) -> None:
    def safe_factory() -> None:
        return None

    alias_module = SimpleNamespace(factory=safe_factory)
    monkeypatch.setattr(
        "aletheia.migration.dynamic_loader._import_module",
        lambda _module_name: alias_module,
    )

    assert resolve_guarded_dynamic_attribute("trusted.plugin", "factory") is safe_factory


def test_guarded_file_loader_rejects_raw_driver_path() -> None:
    driver_path = REPOSITORY_ROOT / "aletheia/scheduler/driver.py"

    with pytest.raises(ValueError, match="raw legacy driver source paths are forbidden"):
        load_guarded_source_module("reviewed_alias", driver_path)


def test_guarded_file_loader_rejects_resolved_driver_export(tmp_path: Path) -> None:
    source = tmp_path / "reexport.py"
    source.write_text(
        "class DriverAlias:\n    pass\nDriverAlias.__module__ = 'aletheia.scheduler.driver'\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="resolved dynamic object belongs"):
        load_guarded_source_module("reviewed_alias", source)


def test_guarded_byte_loader_executes_exact_supplied_bytes(tmp_path: Path) -> None:
    source = tmp_path / "factory.py"
    source.write_text("VALUE = 'disk'\n", encoding="utf-8")

    module = load_guarded_source_bytes(
        "reviewed_exact_bytes",
        source,
        b"VALUE = 'pinned'\n",
    )

    assert module.VALUE == "pinned"


def test_guarded_byte_loader_accepts_production_node_composition() -> None:
    source = REPOSITORY_ROOT / "aletheia/execution/qualification_node_composition.py"
    module_name = "aletheia.execution.qualification_node_composition"

    module = load_guarded_source_bytes(module_name, source, source.read_bytes())

    assert module.compose_node_service.__module__ == module_name


def test_guarded_file_loader_preserves_three_reviewed_call_sites(tmp_path: Path) -> None:
    from aletheia.domains.materials.matbench_task import MaterialsBandGapPlugin
    from aletheia.domains.molecules.plugin import MoleculePropertyPlugin
    from aletheia.evals.adapters.scienceagentbench_scorer_entrypoint import _load_evaluator

    solution = tmp_path / "solution.py"
    solution.write_text(
        "from sklearn.dummy import DummyRegressor\n"
        "def build_pipeline():\n"
        "    return DummyRegressor()\n",
        encoding="utf-8",
    )
    evaluator = tmp_path / "evaluator.py"
    evaluator.write_text("def eval():\n    return 1.0, 'ok'\n", encoding="utf-8")

    assert MoleculePropertyPlugin()._load_solution_pipeline(solution).fit is not None
    assert MaterialsBandGapPlugin()._load_solution_pipeline(solution).fit is not None
    assert _load_evaluator(evaluator)() == (1.0, "ok")


def test_direct_legacy_import_is_rejected(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(root, "aletheia/memory/__init__.py")
    _source(root, "aletheia/memory/service.py")
    _source(
        root,
        "aletheia/research_kernel/reducer.py",
        "from aletheia.memory.service import set_run_status\n",
    )

    violations = find_dependency_boundary_violations(root)

    assert any(item.imported_module == "aletheia.memory.service" for item in violations)


def test_controller_cannot_import_f9_v1_migration_compatibility(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(root, "aletheia/research_controller/__init__.py")
    _source(
        root,
        "aletheia/research_controller/adapter.py",
        "import aletheia.migration.f9_v1_observation_compatibility\n",
    )
    _source(root, "aletheia/migration/f9_v1_observation_compatibility.py")

    violations = find_dependency_boundary_violations(root)

    assert any(
        item.root_module == "aletheia.research_controller.adapter"
        and item.imported_module == "aletheia.migration.f9_v1_observation_compatibility"
        for item in violations
    )


def test_new_authority_cannot_import_legacy_evaluation_capability(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(root, "aletheia/legacy_evaluation/__init__.py")
    _source(root, "aletheia/legacy_evaluation/capability.py")
    _source(
        root,
        "aletheia/research_controller/compatibility.py",
        "from aletheia.legacy_evaluation.capability import LegacyEvaluationCapability\n",
    )

    violations = find_dependency_boundary_violations(root)

    assert any(
        item.root_module == "aletheia.research_controller.compatibility"
        and item.imported_module == "aletheia.legacy_evaluation.capability"
        for item in violations
    )


def test_transitive_legacy_import_reports_dependency_chain(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(root, "aletheia/memory/__init__.py")
    _source(root, "aletheia/memory/service.py")
    _source(root, "aletheia/helper.py", "import aletheia.memory.service\n")
    _source(root, "aletheia/research_kernel/reducer.py", "import aletheia.helper\n")

    violations = find_dependency_boundary_violations(root)

    violation = next(
        item for item in violations if item.imported_module == "aletheia.memory.service"
    )
    assert violation.root_module == "aletheia.research_kernel.reducer"
    assert violation.dependency_chain == (
        "aletheia.research_kernel.reducer",
        "aletheia.helper",
        "aletheia.memory.service",
    )


def test_importing_submodule_also_traverses_parent_package(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(root, "aletheia/memory/__init__.py")
    _source(root, "aletheia/memory/service.py")
    _source(root, "aletheia/helper/__init__.py", "import aletheia.memory.service\n")
    _source(root, "aletheia/helper/pure.py", "VALUE = 1\n")
    _source(root, "aletheia/research_kernel/reducer.py", "import aletheia.helper.pure\n")

    violations = find_dependency_boundary_violations(root)

    assert any(item.imported_module == "aletheia.memory.service" for item in violations)


def test_protected_import_traverses_top_level_package_initializer(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(root, "aletheia/memory/__init__.py")
    _source(root, "aletheia/memory/service.py")
    _source(root, "aletheia/__init__.py", "import aletheia.memory.service\n")

    violations = find_dependency_boundary_violations(root)

    assert any(item.imported_module == "aletheia.memory.service" for item in violations)


def test_literal_and_non_literal_dynamic_imports_are_rejected(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(root, "aletheia/memory/__init__.py")
    _source(root, "aletheia/memory/service.py")
    _source(
        root,
        "aletheia/research_kernel/dynamic.py",
        "import importlib\n"
        "literal = importlib.import_module('aletheia.memory.service')\n"
        "def load(name):\n"
        "    return importlib.import_module(name)\n",
    )

    imported_modules = {item.imported_module for item in find_dependency_boundary_violations(root)}

    assert "aletheia.memory.service" in imported_modules
    assert "<non-literal-dynamic-import>" in imported_modules


def test_aliased_and_relative_dynamic_imports_fail_closed(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "aletheia/research_kernel/dynamic.py",
        "import importlib as loader\n"
        "from importlib import import_module\n"
        "one = loader.import_module('.hidden', package=__package__)\n"
        "two = import_module('aletheia.scheduler.driver')\n",
    )

    imported_modules = {item.imported_module for item in find_dependency_boundary_violations(root)}

    assert "<non-literal-dynamic-import>" in imported_modules
    assert "aletheia.scheduler.driver" in imported_modules


def test_renamed_and_assigned_import_loader_aliases_fail_closed(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "aletheia/research_kernel/dynamic.py",
        "from importlib import import_module as load\n"
        "again = load\n"
        "one = load('aletheia.memory.service')\n"
        "two = again('aletheia.scheduler.driver')\n",
    )

    imported_modules = {item.imported_module for item in find_dependency_boundary_violations(root)}

    assert "aletheia.memory.service" in imported_modules
    assert "aletheia.scheduler.driver" in imported_modules


def test_runpy_cannot_escape_protected_dependency_boundary(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "aletheia/research_kernel/dynamic.py",
        "import runpy\nrunpy.run_module('aletheia.scheduler.' + 'driver')\n",
    )

    imported_modules = {item.imported_module for item in find_dependency_boundary_violations(root)}

    assert "runpy" in imported_modules
    assert "aletheia.scheduler.driver" in imported_modules


def test_computed_importlib_getattr_cannot_escape_boundary(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "aletheia/research_kernel/dynamic.py",
        "import importlib\ngetattr(importlib, 'import_' + 'module')('aletheia.scheduler.driver')\n",
    )

    violations = find_dependency_boundary_violations(root)

    assert any(
        item.imported_module == "aletheia.scheduler.driver" and item.import_kind == "dynamic"
        for item in violations
    )


def test_importlib_module_object_alias_fails_closed(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "aletheia/research_kernel/dynamic.py",
        "import importlib\n"
        "loader = importlib\n"
        "value = loader.import_module('aletheia.memory.service')\n",
    )

    imported_modules = {item.imported_module for item in find_dependency_boundary_violations(root)}

    assert "aletheia.memory.service" in imported_modules


def test_builtin_import_level_aliases_and_runtime_attrs_fail_closed(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(root, "aletheia/memory/__init__.py")
    _source(root, "aletheia/memory/service.py")
    _source(
        root,
        "aletheia/research_kernel/dynamic.py",
        "import builtins as runtime\n"
        "from builtins import __import__ as load\n"
        "one = __import__('memory.service', globals(), locals(), (), 2)\n"
        "two = load('aletheia.memory.service')\n"
        "three = runtime.__import__('aletheia.scheduler.driver')\n"
        "four = runtime.eval('1 + 1')\n",
    )

    imported_modules = {item.imported_module for item in find_dependency_boundary_violations(root)}

    assert "<non-literal-dynamic-import>" in imported_modules
    assert "aletheia.memory.service" in imported_modules
    assert "aletheia.scheduler.driver" in imported_modules
    assert "<runtime-code-execution>" in imported_modules


def test_implicit_builtins_mapping_and_attribute_imports_fail_both_policies(
    tmp_path: Path,
) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "aletheia/research_kernel/dynamic.py",
        "runtime = __builtins__\n"
        "one = runtime['__im' + 'port__']('aletheia.scheduler.driver')\n"
        "two = __builtins__.__import__('aletheia.scheduler.driver')\n",
    )

    dependency_violations = find_dependency_boundary_violations(root)
    driver_violations = find_legacy_driver_import_violations(root)

    assert any(
        item.imported_module == "aletheia.scheduler.driver" and item.import_kind == "dynamic"
        for item in dependency_violations
    )
    assert any(
        item.imported_module == "aletheia.scheduler.driver" and item.import_kind == "dynamic"
        for item in driver_violations
    )


def test_implicit_builtins_runtime_functions_fail_both_policies(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "aletheia/research_kernel/dynamic.py",
        "one = __builtins__['e' + 'val']('1 + 1')\n"
        "two = __builtins__.exec('value = 1')\n"
        "three = __builtins__['com' + 'pile']('1 + 1', '<test>', 'eval')\n",
    )

    dependency_violations = find_dependency_boundary_violations(root)
    driver_violations = find_legacy_driver_import_violations(root)

    assert (
        sum(item.imported_module == "<runtime-code-execution>" for item in dependency_violations)
        >= 3
    )
    assert (
        sum(item.imported_module == "<runtime-code-execution>" for item in driver_violations) >= 3
    )


def test_implicit_builtins_opaque_subscript_fails_closed(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "aletheia/research_kernel/dynamic.py",
        "loader_name = '__import__'\n__builtins__[loader_name]('aletheia.scheduler.driver')\n",
    )

    dependency_violations = find_dependency_boundary_violations(root)
    driver_violations = find_legacy_driver_import_violations(root)

    assert any(
        item.imported_module == "<non-literal-dynamic-import>" for item in dependency_violations
    )
    assert any(item.imported_module == "<non-literal-dynamic-import>" for item in driver_violations)


def test_sys_module_cache_driver_lookup_fails_both_policies(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "aletheia/research_kernel/dynamic.py",
        "import sys\n"
        "cache = sys.modules\n"
        "one = cache.get('aletheia.scheduler.' + 'driver')\n"
        "two = sys.modules['aletheia.scheduler.driver']\n",
    )

    dependency_violations = find_dependency_boundary_violations(root)
    driver_violations = find_legacy_driver_import_violations(root)

    assert any(item.imported_module == "sys" for item in dependency_violations)
    assert any(
        item.imported_module == "aletheia.scheduler.driver" and item.import_kind == "module-cache"
        for item in dependency_violations
    )
    assert any(
        item.imported_module == "aletheia.scheduler.driver" and item.import_kind == "module-cache"
        for item in driver_violations
    )


def test_nonliteral_sys_module_cache_lookup_fails_driver_policy(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "aletheia/api.py",
        "from sys import modules as cache\n"
        "def lookup(module_name):\n"
        "    return cache.get(module_name)\n",
    )

    violations = find_legacy_driver_import_violations(root)

    assert any(
        item.imported_module == "<non-literal-dynamic-import>"
        and item.import_kind == "module-cache"
        for item in violations
    )


def test_builtin_import_nonempty_fromlist_fails_closed(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "aletheia/research_kernel/dynamic.py",
        "__import__('aletheia.memory', fromlist=('service',))\n",
    )

    imported_modules = {item.imported_module for item in find_dependency_boundary_violations(root)}

    assert "<non-literal-dynamic-import>" in imported_modules


def test_pure_internal_helper_is_allowed(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(root, "aletheia/pure_helper.py", "VALUE = 1\n")
    _source(root, "aletheia/research_kernel/reducer.py", "import aletheia.pure_helper\n")

    assert find_dependency_boundary_violations(root) == ()


def test_missing_required_kernel_fails_closed(tmp_path: Path) -> None:
    _source(tmp_path, "aletheia/__init__.py")

    violations = find_dependency_boundary_violations(tmp_path)

    assert any(item.imported_module == "<missing-protected-package>" for item in violations)


def test_symlinked_protected_source_fails_closed(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    (root / "aletheia/research_kernel/linked.py").symlink_to(outside)

    violations = find_dependency_boundary_violations(root)

    assert any(item.imported_module == "<symlinked-source>" for item in violations)


def test_symlinked_package_directory_fails_closed(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    outside = tmp_path / "outside_package"
    _source(tmp_path, "outside_package/__init__.py")
    _source(tmp_path, "outside_package/pure.py", "VALUE = 1\n")
    (root / "aletheia/helper").symlink_to(outside, target_is_directory=True)
    _source(root, "aletheia/research_kernel/reducer.py", "import aletheia.helper.pure\n")

    violations = find_dependency_boundary_violations(root)

    assert any(item.path == "aletheia/helper" for item in violations)


def test_driver_policy_rejects_opaque_symlinked_importer(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    outside = tmp_path / "outside_importer.py"
    outside.write_text(
        "from aletheia.scheduler.driver import ExperimentDriver\n",
        encoding="utf-8",
    )
    (root / "aletheia/api.py").symlink_to(outside)

    violations = find_legacy_driver_import_violations(root)

    assert any(item.path == "aletheia/api.py" for item in violations)


def test_second_production_driver_importer_is_rejected(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "aletheia/api.py",
        "from aletheia.scheduler.driver import ExperimentDriver\n",
    )

    violations = find_legacy_driver_import_violations(root)

    assert any(item.root_module == "aletheia.api" for item in violations)


def test_script_direct_legacy_driver_import_is_rejected(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "scripts/rogue.py",
        "from aletheia.scheduler.driver import ExperimentDriver\n",
    )

    violations = find_legacy_driver_import_violations(root)

    assert any(
        item.root_module == "scripts.rogue" and item.imported_module == "aletheia.scheduler.driver"
        for item in violations
    )


def test_script_obvious_dynamic_driver_import_is_rejected(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "scripts/rogue.py",
        "import importlib\n"
        "getattr(importlib, 'import_' + 'module')"
        "('aletheia.scheduler.' + 'driver')\n",
    )

    violations = find_legacy_driver_import_violations(root)

    assert any(
        item.root_module == "scripts.rogue" and item.imported_module == "aletheia.scheduler.driver"
        for item in violations
    )


def test_script_callable_dunder_loader_escapes_are_rejected(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "scripts/rogue.py",
        "import importlib\n"
        "name = chr(97) + 'letheia.scheduler.driver'\n"
        "importlib.import_module.__call__(name)\n"
        "load = importlib.import_module\n"
        "getattr(load, '__call__')(name)\n"
        "spec.loader.exec_module.__call__(module)\n"
        "execute = spec.loader.exec_module\n"
        "getattr(execute, '__call__')(module)\n",
    )

    violations = find_legacy_driver_import_violations(root)

    assert (
        sum(
            item.root_module == "scripts.rogue"
            and item.imported_module == "<non-literal-dynamic-import>"
            for item in violations
        )
        >= 2
    )
    assert (
        sum(
            item.root_module == "scripts.rogue" and item.imported_module == "<runtime-file-loader>"
            for item in violations
        )
        >= 2
    )


def test_new_script_nonliteral_importlib_escape_is_rejected(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "scripts/rogue.py",
        "import importlib\n"
        "def load(module_name):\n"
        "    return importlib.import_module(module_name)\n",
    )

    violations = find_legacy_driver_import_violations(root)

    assert any(
        item.root_module == "scripts.rogue"
        and item.imported_module == "<non-literal-dynamic-import>"
        for item in violations
    )


def test_new_script_nonliteral_runpy_escape_is_rejected(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "scripts/rogue.py",
        "import runpy\n"
        "runner = runpy\n"
        "def load(module_name):\n"
        "    return runner.run_module(module_name)\n",
    )

    violations = find_legacy_driver_import_violations(root)

    assert any(
        item.root_module == "scripts.rogue"
        and item.imported_module == "<non-literal-dynamic-import>"
        for item in violations
    )


def test_new_script_runtime_code_escape_is_rejected(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(root, "scripts/rogue.py", "def load(source):\n    return exec(source)\n")

    violations = find_legacy_driver_import_violations(root)

    assert any(
        item.root_module == "scripts.rogue" and item.imported_module == "<runtime-code-execution>"
        for item in violations
    )


def test_docker_python_worker_is_inside_the_production_driver_boundary(tmp_path: Path) -> None:
    assert DEFAULT_OPERATIONAL_PYTHON_ROOTS == ("scripts", "docker", "migrations")
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "docker/simulation/rogue_worker.py",
        "from aletheia.scheduler.driver import ExperimentDriver\n",
    )

    violations = find_legacy_driver_import_violations(root)

    assert any(
        item.root_module == "docker.simulation.rogue_worker"
        and item.imported_module == "aletheia.scheduler.driver"
        for item in violations
    )


def test_alembic_python_is_inside_the_production_driver_boundary(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "migrations/versions/rogue_revision.py",
        "import aletheia.scheduler.driver\n",
    )

    violations = find_legacy_driver_import_violations(root)

    assert any(
        item.root_module == "migrations.versions.rogue_revision"
        and item.imported_module == "aletheia.scheduler.driver"
        for item in violations
    )


def test_new_script_importlib_util_file_loader_is_rejected(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "scripts/rogue.py",
        "import importlib.util\n"
        "spec = importlib.util.spec_from_file_location"
        "('x', 'aletheia/scheduler/driver.py')\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n",
    )

    violations = find_legacy_driver_import_violations(root)

    assert (
        sum(
            item.root_module == "scripts.rogue" and item.imported_module == "<runtime-file-loader>"
            for item in violations
        )
        >= 3
    )


def test_file_loader_function_and_receiver_aliases_are_rejected(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "aletheia/api.py",
        "import importlib.util as loader_util\n"
        "make_spec = getattr(loader_util, 'spec_from_' + 'file_location')\n"
        "make_module = loader_util.module_from_spec\n"
        "spec = make_spec('x', 'plugin.py')\n"
        "module = make_module(spec)\n"
        "execute = spec.loader.exec_module\n"
        "execute(module)\n",
    )

    violations = find_legacy_driver_import_violations(root)

    assert (
        sum(
            item.root_module == "aletheia.api" and item.imported_module == "<runtime-file-loader>"
            for item in violations
        )
        >= 3
    )


def test_protected_package_file_loader_is_rejected(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "aletheia/research_kernel/file_loader.py",
        "from importlib.util import module_from_spec, spec_from_file_location\n"
        "spec = spec_from_file_location('x', 'plugin.py')\n"
        "module = module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n",
    )

    violations = find_dependency_boundary_violations(root)

    assert sum(item.imported_module == "<runtime-file-loader>" for item in violations) >= 3


def test_protected_callable_dunder_loader_and_runtime_escapes_are_rejected(
    tmp_path: Path,
) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "aletheia/research_kernel/callable_escape.py",
        "import importlib\n"
        "name = chr(97) + 'letheia.scheduler.driver'\n"
        "getattr(importlib.import_module, '__call__')(name)\n"
        "execute = spec.loader.exec_module\n"
        "getattr(execute, '__call__')(module)\n"
        "__builtins__['eval'].__call__('1 + 1')\n",
    )

    imported_modules = {item.imported_module for item in find_dependency_boundary_violations(root)}

    assert {
        "<non-literal-dynamic-import>",
        "<runtime-code-execution>",
        "<runtime-file-loader>",
    } <= imported_modules


def test_runtime_loader_imports_are_globally_reserved_for_pinned_guard(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    loader_imports = (
        "import builtins\n"
        "import importlib\n"
        "import importlib.machinery\n"
        "import importlib.util\n"
        "import pkgutil\n"
        "import runpy\n"
        "import zipimport\n"
    )
    _source(root, "aletheia/unreviewed_loader.py", loader_imports)
    _source(root, "scripts/unreviewed_loader.py", loader_imports)

    violations = find_legacy_driver_import_violations(root)
    expected = {
        "builtins",
        "importlib",
        "importlib.machinery",
        "importlib.util",
        "pkgutil",
        "runpy",
        "zipimport",
    }

    for root_module in ("aletheia.unreviewed_loader", "scripts.unreviewed_loader"):
        assert expected <= {
            item.imported_module for item in violations if item.root_module == root_module
        }
    assert not any(
        item.root_module == "aletheia.migration.dynamic_loader" and item.imported_module in expected
        for item in violations
    )


def test_importlib_metadata_remains_allowed_in_production_and_protected_code(
    tmp_path: Path,
) -> None:
    root = _minimal_repository(tmp_path)
    _source(root, "aletheia/research_kernel/versions.py", "import importlib.metadata\n")
    _source(root, "scripts/versions.py", "import importlib.metadata as metadata\n")

    assert find_dependency_boundary_violations(root) == ()
    assert find_legacy_driver_import_violations(root) == ()


def test_audited_dynamic_loader_allowlist_is_exact_and_does_not_spread(tmp_path: Path) -> None:
    assert DEFAULT_AUDITED_DYNAMIC_LOADER_SOURCES == (
        (
            "aletheia.migration.dynamic_loader",
            "522280ff0844c54eb1b7f73df82547850bba519afe5aee692e7d1fe8921ec0a6",
        ),
    )
    assert DEFAULT_AUDITED_DYNAMIC_LOADER_ESCAPE_COUNTS == (
        ("aletheia.migration.dynamic_loader", "<non-literal-dynamic-import>", 1),
        ("aletheia.migration.dynamic_loader", "<runtime-file-loader>", 4),
        ("aletheia.migration.dynamic_loader", "<runtime-code-execution>", 4),
    )
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "aletheia/migration/unreviewed_loader.py",
        (REPOSITORY_ROOT / "aletheia/migration/dynamic_loader.py").read_text(encoding="utf-8"),
    )

    violations = find_legacy_driver_import_violations(root)

    assert any(
        item.root_module == "aletheia.migration.unreviewed_loader"
        and item.imported_module == "<non-literal-dynamic-import>"
        for item in violations
    )


def test_audited_dynamic_loader_source_drift_fails_closed(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    guard_path = root / "aletheia/migration/dynamic_loader.py"
    guard_path.write_text(
        guard_path.read_text(encoding="utf-8") + "\n# unreviewed source drift\n",
        encoding="utf-8",
    )

    violations = find_legacy_driver_import_violations(root)

    assert any(
        item.root_module == "aletheia.migration.dynamic_loader"
        and item.imported_module == "<audited-dynamic-loader-source-mismatch>"
        for item in violations
    )


def test_legacy_driver_cannot_reach_kernel_through_helper(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(root, "aletheia/helper.py", "import aletheia.research_kernel\n")
    _source(root, "aletheia/scheduler/driver.py", "import aletheia.helper\n")

    violations = find_legacy_driver_import_violations(root)

    violation = next(
        item for item in violations if item.imported_module == "aletheia.research_kernel"
    )
    assert violation.dependency_chain == (
        "aletheia.scheduler.driver",
        "aletheia.helper",
        "aletheia.research_kernel",
    )


def test_allowlisted_legacy_worker_must_keep_driver_import(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(root, "aletheia/scheduler/durable.py")

    violations = find_legacy_driver_import_violations(root)

    assert any(item.imported_module == "<missing-legacy-driver-import>" for item in violations)


def test_nonliteral_dynamic_import_cannot_evade_driver_policy(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "aletheia/api.py",
        "import importlib\n"
        "def load(name):\n"
        "    return importlib.import_module('aletheia.scheduler.' + name)\n",
    )

    violations = find_legacy_driver_import_violations(root)

    assert any(item.imported_module == "<non-literal-dynamic-import>" for item in violations)


def test_opaque_loader_attribute_cannot_evade_driver_policy(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "aletheia/api.py",
        "import importlib\n"
        "attribute = 'import_module'\n"
        "getattr(importlib, attribute)('aletheia.scheduler.driver')\n",
    )

    violations = find_legacy_driver_import_violations(root)

    assert any(item.imported_module == "<non-literal-dynamic-import>" for item in violations)


def test_builtins_import_alias_cannot_evade_driver_policy(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "aletheia/api.py",
        "from builtins import __import__ as load\nload('aletheia.scheduler.driver')\n",
    )

    violations = find_legacy_driver_import_violations(root)

    assert any(item.imported_module == "aletheia.scheduler.driver" for item in violations)


def test_strict_kernel_rejects_direct_persistence_drivers(tmp_path: Path) -> None:
    root = _minimal_repository(tmp_path)
    _source(
        root,
        "aletheia/research_kernel/persistence.py",
        "import alembic\nimport asyncpg\nimport psycopg\nimport psycopg2\nimport sqlite3\n",
    )

    imported_modules = {item.imported_module for item in find_dependency_boundary_violations(root)}

    assert {"alembic", "asyncpg", "psycopg", "psycopg2", "sqlite3"} <= imported_modules
