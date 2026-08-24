from __future__ import annotations

import ast
from pathlib import Path

_PACKAGE = Path(__file__).resolve().parents[2] / "aletheia" / "research_controller"
_FORBIDDEN = (
    "aletheia.scheduler.driver",
    "aletheia.scheduler.statemachine",
    "aletheia.jobs.outbox",
    "aletheia.programs",
    "aletheia.epistemics.continuation",
)


def _imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    return tuple(imported)


def test_controller_never_reaches_legacy_scientific_control_plane() -> None:
    paths = tuple(sorted(_PACKAGE.glob("*.py")))
    assert paths
    for path in paths:
        imports = _imports(path)
        assert not any(
            imported == forbidden or imported.startswith(f"{forbidden}.")
            for imported in imports
            for forbidden in _FORBIDDEN
        ), path
        source = path.read_text(encoding="utf-8")
        assert "ExperimentDriver" not in source
        assert "._optimize(" not in source
