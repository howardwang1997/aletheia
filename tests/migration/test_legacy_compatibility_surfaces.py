from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SURFACES = {
    "aletheia/compute/base.py": ("ComputeBackend", "legacy_protocol_executor_port"),
    "aletheia/domains/base.py": ("DomainPlugin", "legacy_protocol_executor_port"),
    "aletheia/scheduler/driver.py": ("ExperimentDriver", "legacy_protocol_executor"),
}
COMPATIBILITY_PORT_CLASS_AST_SHA256 = {
    "ComputeBackend": "c58211bc46b202c76be4521f7ee4086fd66fed72f0e18e223d56fcd4f334355a",
    "DomainPlugin": "47197235bdabda091df7ae52076c0bb4bc89e932edf8cd7a86a8ae524b274f4b",
}
DRIVER_CONTROL_FLOW_SHA256 = {
    "run": "42cbc0da655b01dfbbcaf6f671f2bd909b4151ebed2f8d1ff9b09b4e2aa1994e",
    "_run": "bfa42f27953a70350945f8fcfd12873a18fa821a1c596a8bcd658c54d3ba8b8a",
    "_run_experiment": "f218b581f42d37ec83f50510a9ff752408bbedfbb8b7b291d2f7b6294d135951",
}
DRIVER_METHOD_SURFACE_COUNT = 90
DRIVER_METHOD_SURFACE_SHA256 = "2fe2d3d8b8de8ac8379414ce5cd2bca90384d2bae3343c3a9e810b3fcdca4d48"
DRIVER_CLASS_AST_SHA256 = "f1807c807fd01dcebed9b06934968a07d624d6cc36c0c22a3b80f0e8b9477fde"
DRIVER_MODULE_AST_SHA256 = "1b0439c7e33415309a9f359794ac2cdad24d97c664eb11ef0dc7f719d120055b"
DURABLE_DRIVER_GATEWAY_AST_SHA256 = (
    "efecf93d102269285cf4976c02452d02a7fe867d3c4ec438eea7b3d5594455ed"
)


def _class(path: Path, name: str) -> ast.ClassDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name)


def _literal_class_assignments(node: ast.ClassDef) -> dict[str, object]:
    assignments: dict[str, object] = {}
    for statement in node.body:
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and isinstance(statement.value, ast.Constant)
        ):
            assignments[statement.targets[0].id] = statement.value.value
    return assignments


def test_legacy_driver_plugin_and_compute_ports_are_code_marked_compatibility_only() -> None:
    for relative_path, (class_name, migration_status) in SURFACES.items():
        assignments = _literal_class_assignments(_class(ROOT / relative_path, class_name))
        assert assignments["COMPATIBILITY_API"] is True
        assert assignments["MIGRATION_STATUS"] == migration_status
        assert assignments["NEW_SCIENTIFIC_EXTENSIONS_ALLOWED"] is False


def test_legacy_plugin_and_compute_port_classes_require_explicit_migration_review() -> None:
    """A marker alone must not permit new scientific methods on a frozen legacy port."""

    for relative_path, (class_name, _) in SURFACES.items():
        if class_name == "ExperimentDriver":
            continue
        node = _class(ROOT / relative_path, class_name)
        canonical = ast.dump(node, annotate_fields=True, include_attributes=False).encode("utf-8")
        assert (
            hashlib.sha256(canonical).hexdigest() == COMPATIBILITY_PORT_CLASS_AST_SHA256[class_name]
        ), class_name


def test_legacy_driver_control_flow_requires_explicit_migration_review() -> None:
    """Freeze complete orchestration ASTs without hashing unrelated helpers."""

    driver = _class(ROOT / "aletheia/scheduler/driver.py", "ExperimentDriver")
    functions = {
        node.name: node
        for node in driver.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for function_name, expected_sha256 in DRIVER_CONTROL_FLOW_SHA256.items():
        canonical = ast.dump(
            functions[function_name],
            annotate_fields=True,
            include_attributes=False,
        ).encode("utf-8")
        assert hashlib.sha256(canonical).hexdigest() == expected_sha256, (
            function_name,
            ast.dump(functions[function_name], include_attributes=False),
        )


def test_legacy_driver_method_surface_requires_explicit_migration_review() -> None:
    """Catch a new stage method even before it is wired into an orchestration entry point."""

    driver = _class(ROOT / "aletheia/scheduler/driver.py", "ExperimentDriver")
    surface = [
        {
            "kind": type(node).__name__,
            "name": node.name,
            "args": ast.dump(node.args, annotate_fields=True, include_attributes=False),
            "returns": (
                ast.dump(node.returns, annotate_fields=True, include_attributes=False)
                if node.returns is not None
                else None
            ),
            "decorators": [
                ast.dump(decorator, annotate_fields=True, include_attributes=False)
                for decorator in node.decorator_list
            ],
        }
        for node in driver.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    canonical = json.dumps(surface, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert len(surface) == DRIVER_METHOD_SURFACE_COUNT
    assert hashlib.sha256(canonical).hexdigest() == DRIVER_METHOD_SURFACE_SHA256, surface


def test_complete_legacy_driver_class_requires_explicit_migration_review() -> None:
    """Freeze every helper body so existing methods cannot hide a new scientific stage."""

    driver = _class(ROOT / "aletheia/scheduler/driver.py", "ExperimentDriver")
    canonical = ast.dump(driver, annotate_fields=True, include_attributes=False).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == DRIVER_CLASS_AST_SHA256


def test_complete_legacy_driver_module_requires_explicit_migration_review() -> None:
    """Freeze module helpers and launch code that influence the driver outside its class body."""

    path = ROOT / "aletheia/scheduler/driver.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    canonical = ast.dump(tree, annotate_fields=True, include_attributes=False).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == DRIVER_MODULE_AST_SHA256


def test_durable_gateway_cannot_select_an_unreviewed_driver_implementation() -> None:
    """Freeze the sole production importer's complete normalized module AST."""

    path = ROOT / "aletheia/scheduler/durable.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    canonical = ast.dump(tree, annotate_fields=True, include_attributes=False).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == DURABLE_DRIVER_GATEWAY_AST_SHA256
