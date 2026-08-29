"""Static regressions for the qualification allocator's least-privilege SQL."""

from __future__ import annotations

import ast
from pathlib import Path

import aletheia.execution.allocator as allocator
from aletheia.execution.qualification_deployment import ALLOCATOR_UPDATE_TABLES


def _selected_record_name(call: ast.Call) -> str:
    for candidate in ast.walk(call.func.value):
        if (
            isinstance(candidate, ast.Call)
            and isinstance(candidate.func, ast.Name)
            and candidate.func.id == "select"
            and len(candidate.args) == 1
            and isinstance(candidate.args[0], ast.Name)
        ):
            return candidate.args[0].id
    raise AssertionError(f"FOR UPDATE at line {call.lineno} has no single-record select")


def test_allocator_only_row_locks_tables_in_its_update_acl() -> None:
    """PostgreSQL requires UPDATE privilege on every table named by FOR UPDATE."""

    source_path = Path(allocator.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    locked_tables: set[str] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "with_for_update"
        ):
            continue
        record_name = _selected_record_name(node)
        record = getattr(allocator, record_name)
        locked_tables.add(record.__tablename__)

    assert locked_tables
    assert locked_tables <= set(ALLOCATOR_UPDATE_TABLES)
