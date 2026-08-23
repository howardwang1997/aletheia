from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_PATH = REPOSITORY_ROOT / "architecture" / "legacy_write_owners.v1.json"
LEDGER_PATH = REPOSITORY_ROOT / "aletheia" / "memory" / "ledger.py"
DRIVER_PATH = REPOSITORY_ROOT / "aletheia" / "scheduler" / "driver.py"

LEGACY_SOURCE_AST_EXCLUDED_ROOTS = {
    "aletheia/execution": "new_authority",
    "aletheia/migration": "migration_tooling",
    "aletheia/observations": "new_authority",
    "aletheia/planning": "new_authority",
    "aletheia/protocols": "new_authority",
    "aletheia/research_kernel": "new_authority",
}
MIGRATION_SOURCE_AST_ROOTS = ("aletheia/migration", "migrations")
FRONTEND_HTTP_MUTATION_METHODS = ("DELETE", "PATCH", "POST", "PUT")

REQUIRED_WRITE_FIELDS = {
    "write_id",
    "legacy_entrypoint",
    "call_sites",
    "storage_target",
    "mutation_kind",
    "scientific_semantics",
    "authority_class",
    "current_owner",
    "current_commit_boundary",
    "target_owner",
    "target_command_or_object",
    "migration_mode",
    "allowed_legacy_callers",
    "dual_write_policy",
    "cutover_pr",
    "golden_fixture",
    "status",
    "blocker",
    "exception_expiry",
}
REFERENCE_FIELDS = (
    "legacy_entrypoint",
    "call_sites",
    "allowed_legacy_callers",
    "additional_writers",
)
AUTHORITY_CLASSES = {
    "scientific_state",
    "operational_state",
    "platform_state",
    "external_side_effect",
}


def _inventory() -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        duplicates = [key for key, count in Counter(key for key, _ in pairs).items() if count > 1]
        assert not duplicates, f"duplicate JSON keys: {duplicates}"
        return dict(pairs)

    return json.loads(
        INVENTORY_PATH.read_text(encoding="utf-8"),
        object_pairs_hook=unique_object,
    )


def _module_path(module: str) -> Path:
    relative = Path(*module.split("."))
    file_path = REPOSITORY_ROOT / relative.with_suffix(".py")
    if file_path.is_file():
        return file_path
    package_path = REPOSITORY_ROOT / relative / "__init__.py"
    assert package_path.is_file(), f"inventory references missing module {module!r}"
    return package_path


def _definition_exists(reference: dict[str, str]) -> bool:
    tree = ast.parse(_module_path(reference["module"]).read_text(encoding="utf-8"))
    scope: list[ast.stmt] = tree.body
    for part in reference["symbol"].split("."):
        match = next(
            (
                node
                for node in scope
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == part
            ),
            None,
        )
        if match is None:
            return False
        scope = match.body
    return True


def _definition_node(reference: dict[str, str]) -> ast.AST:
    tree = ast.parse(_module_path(reference["module"]).read_text(encoding="utf-8"))
    scope: list[ast.stmt] = tree.body
    match: ast.AST | None = None
    for part in reference["symbol"].split("."):
        match = next(
            (
                node
                for node in scope
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == part
            ),
            None,
        )
        assert match is not None, reference
        scope = match.body  # type: ignore[union-attr]
    return match


def _references(write: dict[str, Any]) -> list[dict[str, str]]:
    references: list[dict[str, str]] = []
    for field in REFERENCE_FIELDS:
        value = write.get(field, [])
        references.extend(value if isinstance(value, list) else [value])
    return references


def _storage_targets(write: dict[str, Any]) -> tuple[str, ...]:
    targets = write.get("storage_targets")
    if targets is None:
        return (write["storage_target"],)
    assert isinstance(targets, list) and targets, write["write_id"]
    assert len(targets) == len(set(targets)), write["write_id"]
    assert write["storage_target"] in targets, write["write_id"]
    return tuple(targets)


def _module_name(path: Path) -> str:
    relative = path.relative_to(REPOSITORY_ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _production_python_paths() -> list[Path]:
    return sorted(
        [
            *(REPOSITORY_ROOT / "aletheia").rglob("*.py"),
            *(REPOSITORY_ROOT / "docker").rglob("*.py"),
            *(REPOSITORY_ROOT / "scripts").rglob("*.py"),
        ]
    )


def _path_is_under(path: Path, relative_root: str) -> bool:
    try:
        path.relative_to(REPOSITORY_ROOT / relative_root)
    except ValueError:
        return False
    return True


def _normalized_ast_graph(paths: list[Path]) -> list[dict[str, str]]:
    graph: list[dict[str, str]] = []
    for path in sorted(set(paths)):
        tree = ast.parse(path.read_text(encoding="utf-8"), type_comments=True)
        normalized = ast.dump(tree, annotate_fields=True, include_attributes=False)
        graph.append(
            {
                "path": path.relative_to(REPOSITORY_ROOT).as_posix(),
                "ast_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            }
        )
    return graph


def _legacy_source_ast_graph() -> list[dict[str, str]]:
    return _normalized_ast_graph(
        [
            path
            for path in _production_python_paths()
            if not any(
                _path_is_under(path, relative_root)
                for relative_root in LEGACY_SOURCE_AST_EXCLUDED_ROOTS
            )
        ]
    )


def _migration_source_ast_graph() -> list[dict[str, str]]:
    return _normalized_ast_graph(
        [
            path
            for relative_root in MIGRATION_SOURCE_AST_ROOTS
            for path in (REPOSITORY_ROOT / relative_root).rglob("*.py")
        ]
    )


def _tracked_repository_entries() -> list[tuple[Path, str]]:
    result = subprocess.run(
        ["git", "ls-files", "--stage", "-z"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    entries: list[tuple[Path, str]] = []
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        metadata, relative = record.split(b"\t", 1)
        mode, _object_id, stage = metadata.split()
        assert stage == b"0", relative
        entries.append((REPOSITORY_ROOT / relative.decode("utf-8"), mode.decode("ascii")))
    return entries


def _tracked_repository_paths() -> list[Path]:
    return [path for path, _mode in _tracked_repository_entries()]


def _is_non_python_invocation_path(path: Path) -> bool:
    relative = path.relative_to(REPOSITORY_ROOT)
    parts = relative.parts
    if not parts:
        return False
    if parts[0] == "frontend":
        return len(parts) > 1 and parts[1] not in {".next", "node_modules"}
    if parts[0] == "scripts":
        return len(parts) > 1 and path.suffix == ".sh"
    if parts[0] == "docker":
        return path.suffix != ".py"
    return len(parts) == 1 and bool(re.fullmatch(r"docker-compose.*\.(?:yaml|yml)", relative.name))


def _non_python_invocation_paths() -> list[Path]:
    return sorted(
        path for path in _tracked_repository_paths() if _is_non_python_invocation_path(path)
    )


def _non_python_invocation_source_graph() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path, git_mode in sorted(_tracked_repository_entries()):
        if not _is_non_python_invocation_path(path):
            continue
        assert git_mode in {"100644", "100755"}, (path, git_mode)
        assert not path.is_symlink(), path
        assert path.is_file(), path
        rows.append(
            {
                "path": path.relative_to(REPOSITORY_ROOT).as_posix(),
                "git_mode": git_mode,
                "byte_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return rows


def _scan_frontend_http_mutations(source: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    patterns = (
        re.compile(
            r"\bmethod\s*:\s*['\"](POST|PUT|PATCH|DELETE)['\"]",
            re.IGNORECASE,
        ),
        re.compile(r"\.\s*(post|put|patch|delete)\s*\(", re.IGNORECASE),
    )
    for pattern in patterns:
        counts.update(match.group(1).upper() for match in pattern.finditer(source))
    return counts


def _frontend_http_mutation_graph() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    source_suffixes = {".js", ".jsx", ".mjs", ".ts", ".tsx"}
    for path in _non_python_invocation_paths():
        if path.parts[-2:] and path.suffix in source_suffixes and "frontend" in path.parts:
            for method, count in sorted(
                _scan_frontend_http_mutations(path.read_text(encoding="utf-8")).items()
            ):
                rows.append(
                    {
                        "path": path.relative_to(REPOSITORY_ROOT).as_posix(),
                        "method": method,
                        "count": count,
                    }
                )
    return rows


def _scan_shell_operational_sinks(source: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    logical_source = source.replace("\\\n", " ")
    curl_pattern = re.compile(
        r"\bcurl\b[^\n]*?(?:-X|--request(?:=|\s+))\s*['\"]?"
        r"(POST|PUT|PATCH|DELETE)\b",
        re.IGNORECASE,
    )
    counts.update(
        f"external.http_{match.group(1).lower()}" for match in curl_pattern.finditer(logical_source)
    )
    counts["file.tmp_write"] += len(
        re.findall(
            r"(?:-o\s+|--output(?:=|\s+)|[012]?>>?\s*)['\"]?/tmp/[A-Za-z0-9_.-]+",
            logical_source,
        )
    )
    counts["process.exec"] += len(re.findall(r"(?m)^\s*exec\b", source))
    return +counts


def _shell_operational_sink_graph() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in _non_python_invocation_paths():
        if path.parent == REPOSITORY_ROOT / "scripts" and path.suffix == ".sh":
            for sink, count in sorted(
                _scan_shell_operational_sinks(path.read_text(encoding="utf-8")).items()
            ):
                rows.append(
                    {
                        "path": path.relative_to(REPOSITORY_ROOT).as_posix(),
                        "sink": sink,
                        "count": count,
                    }
                )
    return rows


def _writer_surface_usage_graph(inventory: dict[str, Any]) -> list[dict[str, str]]:
    """Conservatively resolve repository uses of every declared writer surface.

    This is intentionally a static, in-repository audit. It resolves direct and
    re-exported imports, module/class instances, ``self`` attributes, annotated
    store parameters and callable references passed to helpers such as
    ``asyncio.to_thread``. The frozen digest makes unsupported dynamic dispatch
    an explicit review concern instead of an implied completeness claim.
    """

    surface_to_writes: defaultdict[str, set[str]] = defaultdict(set)
    root_candidates: defaultdict[str, set[str]] = defaultdict(set)
    for write in inventory["writes"]:
        for reference in [write["legacy_entrypoint"], *write.get("additional_writers", [])]:
            surface = f"{reference['module']}.{reference['symbol']}"
            surface_to_writes[surface].add(write["write_id"])
            root = reference["symbol"].split(".", 1)[0]
            root_candidates[root].add(f"{reference['module']}.{root}")
    unique_roots = {
        name: next(iter(candidates))
        for name, candidates in root_candidates.items()
        if len(candidates) == 1
    }
    target_surfaces = set(surface_to_writes)
    class_roots: set[str] = set()
    for write in inventory["writes"]:
        for reference in [write["legacy_entrypoint"], *write.get("additional_writers", [])]:
            root = reference["symbol"].split(".", 1)[0]
            tree = ast.parse(_module_path(reference["module"]).read_text(encoding="utf-8"))
            definition = next(
                (
                    node
                    for node in tree.body
                    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == root
                ),
                None,
            )
            if isinstance(definition, ast.ClassDef):
                class_roots.add(f"{reference['module']}.{root}")

    def canonical_import(full_name: str, imported_name: str) -> str:
        return unique_roots.get(imported_name, full_name)

    graph: set[tuple[str, str, str, str]] = set()
    for path in _production_python_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = _module_name(path)
        module_bindings: dict[str, str] = {
            node.name: f"{module}.{node.name}"
            for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    full_name = f"{node.module}.{alias.name}"
                    module_bindings[alias.asname or alias.name] = canonical_import(
                        full_name, alias.name
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    module_bindings[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0]
                    )

        class SurfaceVisitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.scopes: list[str] = []
                self.classes: list[str] = []
                self.environments: list[dict[str, str]] = []
                self.module_instances: dict[str, str] = {}
                self.self_attributes: defaultdict[str, dict[str, str]] = defaultdict(dict)
                self.alias_assignment_nodes: set[int] = set()

            def _lookup(self, name: str) -> str | None:
                for environment in reversed(self.environments):
                    if name in environment:
                        return environment[name]
                return self.module_instances.get(name) or module_bindings.get(name)

            def _annotation_type(self, node: ast.AST | None) -> str | None:
                if node is None:
                    return None
                candidates = {
                    module_bindings[name.id]
                    for name in ast.walk(node)
                    if isinstance(name, ast.Name)
                    and name.id in module_bindings
                    and module_bindings[name.id] in set(unique_roots.values())
                }
                return next(iter(candidates)) if len(candidates) == 1 else None

            def _resolve(self, node: ast.AST) -> str | None:
                if isinstance(node, ast.Name):
                    if node.id == "self" and self.classes:
                        return f"{module}.{self.classes[-1]}"
                    return self._lookup(node.id)
                if isinstance(node, ast.Attribute):
                    if (
                        isinstance(node.value, ast.Name)
                        and node.value.id == "self"
                        and self.classes
                        and node.attr in self.self_attributes[self.classes[-1]]
                    ):
                        return self.self_attributes[self.classes[-1]][node.attr]
                    base = self._resolve(node.value)
                    return f"{base}.{node.attr}" if base else None
                if isinstance(node, ast.Call):
                    return self._resolve(node.func)
                if isinstance(node, (ast.BoolOp, ast.IfExp)):
                    values = (
                        node.values if isinstance(node, ast.BoolOp) else [node.body, node.orelse]
                    )
                    resolved = {value for item in values if (value := self._resolve(item))}
                    return next(iter(resolved)) if len(resolved) == 1 else None
                return None

            @staticmethod
            def _assigned_names(node: ast.AST) -> list[str]:
                if isinstance(node, ast.Name):
                    return [node.id]
                if isinstance(node, (ast.Tuple, ast.List)):
                    return [
                        name for item in node.elts for name in SurfaceVisitor._assigned_names(item)
                    ]
                return []

            def _seed_assignments(
                self, statements: list[ast.stmt], environment: dict[str, str]
            ) -> None:
                assignments: list[tuple[list[ast.AST], ast.AST]] = []

                def collect(statement: ast.AST) -> None:
                    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        return
                    if isinstance(statement, ast.Assign):
                        assignments.append((list(statement.targets), statement.value))
                    elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
                        assignments.append(([statement.target], statement.value))
                    for child in ast.iter_child_nodes(statement):
                        collect(child)

                for statement in statements:
                    collect(statement)
                for _ in range(3):
                    for targets, value in assignments:
                        resolved = self._resolve(value)
                        direct_alias = isinstance(value, (ast.Name, ast.Attribute)) and (
                            resolved in target_surfaces or resolved in class_roots
                        )
                        constructed_store = isinstance(value, ast.Call) and resolved in class_roots
                        selected_store = isinstance(value, (ast.BoolOp, ast.IfExp)) and (
                            resolved in class_roots
                        )
                        if not (direct_alias or constructed_store or selected_store):
                            continue
                        if direct_alias:
                            self.alias_assignment_nodes.add(id(value))
                        for target in targets:
                            for name in self._assigned_names(target):
                                environment[name] = resolved
                            if (
                                isinstance(target, ast.Attribute)
                                and isinstance(target.value, ast.Name)
                                and target.value.id == "self"
                                and self.classes
                            ):
                                self.self_attributes[self.classes[-1]][target.attr] = resolved

            def _record(self, node: ast.AST) -> None:
                if id(node) in self.alias_assignment_nodes:
                    return
                surface = self._resolve(node)
                if surface not in target_surfaces:
                    return
                caller = ".".join(self.scopes) if self.scopes else "<module>"
                for write_id in surface_to_writes[surface]:
                    graph.add((write_id, surface, module, caller))

            def visit_Module(self, node: ast.Module) -> None:
                self._seed_assignments(node.body, self.module_instances)
                self.generic_visit(node)

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self.scopes.append(node.name)
                self.classes.append(node.name)
                self.generic_visit(node)
                self.classes.pop()
                self.scopes.pop()

            def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                environment: dict[str, str] = {}
                for argument in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                    annotation = self._annotation_type(argument.annotation)
                    if annotation:
                        environment[argument.arg] = annotation
                if node.args.vararg:
                    annotation = self._annotation_type(node.args.vararg.annotation)
                    if annotation:
                        environment[node.args.vararg.arg] = annotation
                if node.args.kwarg:
                    annotation = self._annotation_type(node.args.kwarg.annotation)
                    if annotation:
                        environment[node.args.kwarg.arg] = annotation
                self.environments.append(environment)
                self.scopes.append(node.name)
                self._seed_assignments(node.body, environment)
                self.generic_visit(node)
                self.scopes.pop()
                self.environments.pop()

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._visit_function(node)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self._visit_function(node)

            def visit_Name(self, node: ast.Name) -> None:
                if isinstance(node.ctx, ast.Load):
                    self._record(node)

            def visit_Attribute(self, node: ast.Attribute) -> None:
                if isinstance(node.ctx, ast.Load):
                    self._record(node)
                self.generic_visit(node)

        SurfaceVisitor().visit(tree)

    return [
        {"write_id": write_id, "surface": surface, "module": module, "symbol": symbol}
        for write_id, surface, module, symbol in sorted(graph)
    ]


_DIRECT_FILE_METHOD_SINKS = {
    "savefig",
    "savetxt",
    "savez",
    "savez_compressed",
    "to_csv",
    "to_excel",
    "to_feather",
    "to_json",
    "to_parquet",
    "to_pickle",
    "write_bytes",
    "write_text",
}


def _direct_file_sink_graph() -> list[dict[str, Any]]:
    """Enumerate direct file mutations in production packages and operator scripts."""

    counts: Counter[tuple[str, str, str]] = Counter()
    for path in _production_python_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = _module_name(path)
        direct_file_aliases = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module in {"json", "joblib", "pickle", "cloudpickle", "shutil"}
            for alias in node.names
            if alias.name in {"copy", "copy2", "copyfile", "dump", "move"}
        }
        calls_by_scope: defaultdict[str, list[ast.Call]] = defaultdict(list)

        class SinkVisitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.scopes: list[str] = []

            def _visit_scope(self, node: ast.AST, name: str) -> None:
                self.scopes.append(name)
                self.generic_visit(node)
                self.scopes.pop()

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self._visit_scope(node, node.name)

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._visit_scope(node, node.name)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self._visit_scope(node, node.name)

            def visit_Call(self, node: ast.Call) -> None:
                symbol = ".".join(self.scopes) if self.scopes else "<module>"
                calls_by_scope[symbol].append(node)
                self.generic_visit(node)

        SinkVisitor().visit(tree)

        def leaf(call: ast.Call) -> str | None:
            return (
                call.func.id
                if isinstance(call.func, ast.Name)
                else call.func.attr
                if isinstance(call.func, ast.Attribute)
                else None
            )

        def root_name(node: ast.AST) -> str | None:
            while isinstance(node, ast.Attribute):
                node = node.value
            return node.id if isinstance(node, ast.Name) else None

        def mode(call: ast.Call) -> str | None:
            call_leaf = leaf(call)
            call_root = root_name(call.func)
            path_method_open = (
                call_leaf == "open"
                and isinstance(call.func, ast.Attribute)
                and call_root not in {"bz2", "gzip", "io", "lzma", "tarfile"}
            )
            position = 0 if path_method_open else 1
            candidate: ast.AST | None = call.args[position] if len(call.args) > position else None
            for keyword in call.keywords:
                if keyword.arg == "mode":
                    candidate = keyword.value
            return (
                candidate.value
                if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str)
                else None
            )

        for symbol, calls in calls_by_scope.items():
            open_writes = {
                id(call): open_mode
                for call in calls
                if leaf(call) in {"open", "fdopen"}
                if (open_mode := mode(call)) and any(flag in open_mode for flag in "wax+")
            }
            has_direct_write = bool(open_writes)
            preliminary: list[str] = [
                (
                    f"{root_name(call.func)}.{leaf(call)}:{open_mode}"
                    if root_name(call.func) in {"bz2", "gzip", "io", "lzma", "tarfile"}
                    else f"{leaf(call)}:{open_mode}"
                )
                for call in calls
                if (open_mode := open_writes.get(id(call)))
            ]
            for call in calls:
                call_leaf = leaf(call)
                root = root_name(call.func)
                if call_leaf in _DIRECT_FILE_METHOD_SINKS:
                    preliminary.append(call_leaf)
                    has_direct_write = True
                elif call_leaf in {"copy", "copy2", "copyfile", "move"} and (
                    root == "shutil" or call_leaf in direct_file_aliases
                ):
                    preliminary.append(call_leaf)
                    has_direct_write = True
                elif call_leaf == "dump" and (
                    root in {"json", "joblib", "pickle", "cloudpickle"}
                    or call_leaf in direct_file_aliases
                ):
                    preliminary.append(call_leaf)
                    has_direct_write = True
                elif call_leaf in {"write", "writelines"} and (open_writes or root == "os"):
                    preliminary.append(call_leaf)
                    has_direct_write = True
                elif call_leaf == "replace" and root == "os":
                    preliminary.append(call_leaf)
                    has_direct_write = True
            if has_direct_write:
                preliminary.extend(
                    "replace"
                    for call in calls
                    if leaf(call) == "replace" and root_name(call.func) != "os"
                )
            for sink in preliminary:
                counts[(module, symbol, sink)] += 1

    return [
        {"module": module, "symbol": symbol, "sink": sink, "count": count}
        for (module, symbol, sink), count in sorted(counts.items())
    ]


def _direct_external_sink_graph() -> list[dict[str, Any]]:
    """Freeze direct event, provider, VCS and child-process side effects."""

    counts: Counter[tuple[str, str, str]] = Counter()
    github_mutators = {
        "create_for_authenticated_user",
        "create_in_org",
        "create_or_update_file_contents",
        "create_ref",
    }
    process_sinks = {"Popen", "check_call", "check_output", "run"}
    for path in _production_python_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = _module_name(path)
        decorator_calls = {
            id(decorator)
            for definition in ast.walk(tree)
            if isinstance(definition, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            for decorator in definition.decorator_list
            if isinstance(decorator, ast.Call)
        }

        def call_path(node: ast.AST) -> str | None:
            parts: list[str] = []
            while isinstance(node, ast.Attribute):
                parts.append(node.attr)
                node = node.value
            if isinstance(node, ast.Name):
                parts.append(node.id)
                return ".".join(reversed(parts))
            return None

        http_client_names: set[str] = set()
        process_aliases: dict[str, str] = {}
        for candidate in ast.walk(tree):
            if isinstance(candidate, ast.ImportFrom) and candidate.module == "subprocess":
                for alias in candidate.names:
                    if alias.name in process_sinks:
                        process_aliases[alias.asname or alias.name] = alias.name
            elif isinstance(candidate, ast.ImportFrom) and candidate.module == "asyncio":
                for alias in candidate.names:
                    if alias.name in {"create_subprocess_exec", "create_subprocess_shell"}:
                        process_aliases[alias.asname or alias.name] = alias.name
            elif isinstance(candidate, (ast.Assign, ast.AnnAssign)):
                value = candidate.value
                targets = (
                    candidate.targets if isinstance(candidate, ast.Assign) else [candidate.target]
                )
                if (
                    isinstance(value, ast.Call)
                    and (constructor := call_path(value.func))
                    and constructor.endswith(("AsyncClient", "Client", "ClientSession", "Session"))
                    and constructor.split(".", 1)[0] in {"aiohttp", "httpx", "requests"}
                ):
                    http_client_names.update(
                        target.id for target in targets if isinstance(target, ast.Name)
                    )
            elif isinstance(candidate, (ast.With, ast.AsyncWith)):
                for item in candidate.items:
                    if (
                        isinstance(item.context_expr, ast.Call)
                        and (constructor := call_path(item.context_expr.func))
                        and constructor.endswith(
                            ("AsyncClient", "Client", "ClientSession", "Session")
                        )
                        and constructor.split(".", 1)[0] in {"aiohttp", "httpx", "requests"}
                        and isinstance(item.optional_vars, ast.Name)
                    ):
                        http_client_names.add(item.optional_vars.id)

        class ExternalSinkVisitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.scopes: list[str] = []

            def _visit_scope(self, node: ast.AST, name: str) -> None:
                self.scopes.append(name)
                self.generic_visit(node)
                self.scopes.pop()

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self._visit_scope(node, node.name)

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._visit_scope(node, node.name)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self._visit_scope(node, node.name)

            def _record(self, sink: str) -> None:
                symbol = ".".join(self.scopes) if self.scopes else "<module>"
                counts[(module, symbol, sink)] += 1

            def visit_Call(self, node: ast.Call) -> None:
                path_name = call_path(node.func)
                leaf = (
                    path_name.rsplit(".", 1)[-1]
                    if path_name
                    else node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else node.func.id
                    if isinstance(node.func, ast.Name)
                    else None
                )
                if leaf == "publish":
                    self._record("event.publish")
                elif (
                    id(node) not in decorator_calls
                    and leaf in {"delete", "patch", "post", "put", "request"}
                    and path_name
                    and path_name.split(".", 1)[0]
                    in {"aiohttp", "httpx", "requests", *http_client_names}
                ):
                    self._record(f"provider.http_{leaf}")
                elif path_name and path_name.endswith(
                    (
                        "chat.completions.create",
                        "messages.create",
                        "models.generate_content",
                        "models.generate_content_async",
                        "responses.create",
                    )
                ):
                    self._record("provider.model_create")
                elif path_name and path_name.endswith("rest.pulls.create"):
                    self._record("github.create_pull_request")
                elif leaf in github_mutators:
                    self._record(f"github.{leaf}")
                elif path_name and path_name.startswith("subprocess.") and leaf in process_sinks:
                    self._record(f"process.{leaf}")
                elif leaf in process_aliases:
                    self._record(f"process.{process_aliases[leaf]}")
                elif leaf in {"create_subprocess_exec", "create_subprocess_shell"}:
                    self._record(f"process.{leaf}")
                self.generic_visit(node)

        ExternalSinkVisitor().visit(tree)

    return [
        {"module": module, "symbol": symbol, "sink": sink, "count": count}
        for (module, symbol, sink), count in sorted(counts.items())
    ]


def _orm_model_index() -> dict[str, str]:
    """Map statically defined and re-exported ORM model names to SQL tables."""

    by_fully_qualified_name: dict[str, str] = {}
    module_trees: dict[str, ast.Module] = {}
    for path in sorted((REPOSITORY_ROOT / "aletheia").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = _module_name(path)
        module_trees[module] = tree
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            table = next(
                (
                    statement.value.value
                    for statement in node.body
                    if isinstance(statement, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == "__tablename__"
                        for target in statement.targets
                    )
                    and isinstance(statement.value, ast.Constant)
                    and isinstance(statement.value.value, str)
                ),
                None,
            )
            if table:
                fully_qualified = f"{module}.{node.name}"
                by_fully_qualified_name[fully_qualified] = table

    # Resolve explicit re-exports without falling back on globally unique class
    # names.  That fallback would confuse unrelated domain/Pydantic classes such
    # as auth.providers.base.Claim and epistemics.schemas.BeliefState with the
    # identically named memory-ledger ORM entities.
    changed = True
    while changed:
        changed = False
        for module, tree in module_trees.items():
            for node in tree.body:
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                for alias in node.names:
                    table = by_fully_qualified_name.get(f"{node.module}.{alias.name}")
                    exported = f"{module}.{alias.asname or alias.name}"
                    if table and by_fully_qualified_name.get(exported) != table:
                        by_fully_qualified_name[exported] = table
                        changed = True
    return by_fully_qualified_name


def _scan_direct_orm_tree(
    tree: ast.AST,
    *,
    module: str,
    models_by_fq_name: dict[str, str],
) -> Counter[tuple[str, str, str, str]]:
    model_bindings: dict[str, str] = {}
    module_bindings: dict[str, str] = {}
    imported_mutator_bindings: dict[str, tuple[str, str | None]] = {}
    for fully_qualified, table in models_by_fq_name.items():
        model_module, _, name = fully_qualified.rpartition(".")
        if model_module == module:
            model_bindings[name] = table
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                table = models_by_fq_name.get(f"{node.module}.{alias.name}")
                if table:
                    model_bindings[alias.asname or alias.name] = table
                else:
                    module_bindings[alias.asname or alias.name] = f"{node.module}.{alias.name}"
                if (
                    alias.asname
                    and node.module
                    in {
                        "sqlalchemy",
                        "sqlalchemy.dialects.postgresql",
                        "sqlalchemy.dialects.sqlite",
                    }
                    and alias.name in {"delete", "insert", "update"}
                ):
                    imported_mutator_bindings[alias.asname] = (
                        f"sqlalchemy_{alias.name}",
                        None,
                    )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                module_bindings[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )

    def resolve_model(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return model_bindings.get(node.id)
        if isinstance(node, ast.Attribute):
            if node.attr == "__table__":
                return resolve_model(node.value)
            parts = [node.attr]
            value = node.value
            while isinstance(value, ast.Attribute):
                parts.append(value.attr)
                value = value.value
            if isinstance(value, ast.Name):
                prefix = module_bindings.get(value.id)
                if prefix:
                    fully_qualified = ".".join([prefix, *reversed(parts)])
                    return models_by_fq_name.get(fully_qualified)
        return None

    counts: Counter[tuple[str, str, str, str]] = Counter()

    class OrmVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scopes: list[str] = []
            self.scope_kinds: list[str] = []
            self.class_tables: list[str | None] = []
            self.model_values: list[dict[str, str]] = [{}]
            self.mutator_values: list[dict[str, tuple[str, str | None] | None]] = [
                dict(imported_mutator_bindings)
            ]
            self.attribute_dirty_tables: list[set[str]] = [set()]

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            qualified_name = ".".join([module, *self.scopes, node.name])
            self.scopes.append(node.name)
            self.scope_kinds.append("class")
            self.class_tables.append(models_by_fq_name.get(qualified_name))
            self.model_values.append({})
            self.mutator_values.append({})
            self.attribute_dirty_tables.append(set())
            self.generic_visit(node)
            self.attribute_dirty_tables.pop()
            self.mutator_values.pop()
            self.model_values.pop()
            self.class_tables.pop()
            self.scope_kinds.pop()
            self.scopes.pop()

        def _annotation_model(self, node: ast.AST | None) -> str | None:
            if node is None:
                return None
            tables = {
                table
                for candidate in ast.walk(node)
                if (table := resolve_model(candidate)) is not None
            }
            return next(iter(tables)) if len(tables) == 1 else None

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            self.scopes.append(node.name)
            self.scope_kinds.append("function")
            environment: dict[str, str] = {}
            positional_arguments = [*node.args.posonlyargs, *node.args.args]
            arguments = [
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            ]
            if (
                len(self.scope_kinds) >= 2
                and self.scope_kinds[-2] == "class"
                and self.class_tables[-1]
                and positional_arguments
                and positional_arguments[0].arg in {"cls", "self"}
            ):
                environment[positional_arguments[0].arg] = self.class_tables[-1]
            for argument in arguments:
                if table := self._annotation_model(argument.annotation):
                    environment[argument.arg] = table
            self.model_values.append(environment)
            self.mutator_values.append({})
            self.attribute_dirty_tables.append(set())
            self.generic_visit(node)
            self.attribute_dirty_tables.pop()
            self.mutator_values.pop()
            self.model_values.pop()
            self.scope_kinds.pop()
            self.scopes.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_function(node)

        def _record(self, operation: str, table: str) -> None:
            symbol = ".".join(self.scopes) if self.scopes else "<module>"
            counts[(module, symbol, operation, table)] += 1

        def _value_model(self, node: ast.AST) -> str | None:
            if table := resolve_model(node):
                return table
            if isinstance(node, ast.Call):
                return resolve_model(node.func) or self._query_model(node)
            if isinstance(node, ast.Name):
                for environment in reversed(self.model_values):
                    if node.id in environment:
                        return environment[node.id]
            return None

        def _lookup_mutator(self, name: str) -> tuple[str, str | None] | None:
            for environment in reversed(self.mutator_values):
                if name in environment:
                    return environment[name]
            return None

        def _query_model(self, node: ast.AST) -> str | None:
            if not isinstance(node, ast.Call):
                return None
            if isinstance(node.func, ast.Attribute):
                if node.func.attr in {"get", "query", "select"} and node.args:
                    return resolve_model(node.args[0]) or self._query_model(node.args[0])
                if node.func.attr in {"execute", "scalar", "scalars"} and node.args:
                    return self._query_model(node.args[0])
                return self._query_model(node.func.value) or self._value_model(node.func.value)
            if (
                isinstance(node.func, ast.Name)
                and node.func.id in {"get", "query", "select"}
                and node.args
            ):
                return resolve_model(node.args[0]) or self._query_model(node.args[0])
            return None

        def _attribute_target_model(self, node: ast.AST) -> str | None:
            if not isinstance(node, ast.Attribute):
                return None
            value = node.value
            while isinstance(value, ast.Attribute):
                value = value.value
            return self._value_model(value)

        def _record_attribute_target(self, node: ast.AST, operation: str) -> None:
            if table := self._attribute_target_model(node):
                self._record(operation, table)
                self.attribute_dirty_tables[-1].add(table)

        def _mutator_callable(self, node: ast.AST) -> tuple[str, str | None] | None:
            if isinstance(node, ast.Name):
                if node.id == "setattr":
                    return ("setattr", None)
                return self._lookup_mutator(node.id)
            if not isinstance(node, ast.Attribute):
                return None

            leaf = node.attr
            if leaf in {"insert", "update", "delete"}:
                if table := self._query_model(node.value):
                    return (f"query_{leaf}", table)
                if table := resolve_model(node.value):
                    return (f"table_{leaf}", table)
            relationship_mutators = {
                "add",
                "append",
                "clear",
                "discard",
                "extend",
                "pop",
                "remove",
                "setdefault",
                "update",
            }
            if leaf in relationship_mutators and (
                table := self._attribute_target_model(node.value)
            ):
                return ("instance_relationship_mutation", table)
            if leaf in {
                "add",
                "add_all",
                "bulk_insert_mappings",
                "bulk_save_objects",
                "bulk_update_mappings",
                "commit",
                "delete",
                "execute",
                "exec_driver_sql",
                "flush",
                "merge",
            }:
                return (f"session_{leaf}", None)
            return None

        def _bind_mutator(self, target: ast.AST, value: ast.AST) -> None:
            if not isinstance(target, ast.Name):
                return
            mutator = self._mutator_callable(value)
            # Keep an explicit non-mutator shadow so a same-named outer alias
            # cannot leak through a local reassignment.
            self.mutator_values[-1][target.id] = mutator

        def _record_raw_execute(self, sql_node: ast.AST) -> None:
            if isinstance(sql_node, (ast.JoinedStr, ast.BinOp)):
                self._record("unresolved_dynamic_sql", "<dynamic>")
            elif isinstance(sql_node, ast.Constant) and isinstance(sql_node.value, str):
                table_match = re.match(
                    r"^\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+[\"`]?([a-zA-Z0-9_]+)",
                    sql_node.value,
                    re.IGNORECASE,
                )
                if table_match:
                    self._record("raw_execute_mutation", table_match.group(1))

        def _record_aliased_mutator(
            self,
            node: ast.Call,
            mutator: tuple[str, str | None],
        ) -> None:
            operation, bound_table = mutator
            if operation == "setattr" and node.args:
                if table := self._value_model(node.args[0]):
                    self._record("instance_setattr_update", table)
                    self.attribute_dirty_tables[-1].add(table)
            elif operation in {"session_add", "session_add_all"}:
                values: list[ast.AST] = list(node.args)
                if (
                    operation == "session_add_all"
                    and node.args
                    and isinstance(node.args[0], (ast.List, ast.Tuple, ast.Set))
                ):
                    values = list(node.args[0].elts)
                for value in values:
                    if table := self._value_model(value):
                        self._record(operation, table)
            elif operation in {"session_delete", "session_merge"} and node.args:
                if table := self._value_model(node.args[0]):
                    self._record(operation, table)
            elif (
                operation
                in {
                    "session_bulk_insert_mappings",
                    "session_bulk_update_mappings",
                }
                and node.args
            ):
                if table := resolve_model(node.args[0]):
                    self._record(operation, table)
            elif operation == "session_bulk_save_objects" and node.args:
                values = (
                    list(node.args[0].elts)
                    if isinstance(node.args[0], (ast.List, ast.Tuple, ast.Set))
                    else [node.args[0]]
                )
                for value in values:
                    if table := self._value_model(value):
                        self._record(operation, table)
            elif operation in {"session_commit", "session_flush"}:
                leaf = operation.removeprefix("session_")
                for table in sorted(self.attribute_dirty_tables[-1]):
                    self._record(f"session_{leaf}_after_attribute_update", table)
            elif operation in {"session_execute", "session_exec_driver_sql"} and node.args:
                self._record_raw_execute(node.args[0])
            elif operation.startswith("sqlalchemy_") and node.args:
                if table := resolve_model(node.args[0]):
                    self._record(operation, table)
            elif bound_table:
                self._record(operation, bound_table)
                if operation == "instance_relationship_mutation":
                    self.attribute_dirty_tables[-1].add(bound_table)

        def visit_Assign(self, node: ast.Assign) -> None:
            for target in node.targets:
                self._record_attribute_target(target, "instance_attribute_update")
                self._bind_mutator(target, node.value)
            table = self._value_model(node.value)
            if table:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.model_values[-1][target.id] = table
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            self._record_attribute_target(node.target, "instance_attribute_update")
            table = self._value_model(node.value) if node.value else None
            if node.value:
                self._bind_mutator(node.target, node.value)
            if table and isinstance(node.target, ast.Name):
                self.model_values[-1][node.target.id] = table
            self.generic_visit(node)

        def visit_AugAssign(self, node: ast.AugAssign) -> None:
            self._record_attribute_target(node.target, "instance_attribute_update")
            self.generic_visit(node)

        def visit_Delete(self, node: ast.Delete) -> None:
            for target in node.targets:
                self._record_attribute_target(target, "instance_attribute_delete")
            self.generic_visit(node)

        def visit_For(self, node: ast.For) -> None:
            if (table := self._value_model(node.iter)) and isinstance(node.target, ast.Name):
                self.model_values[-1][node.target.id] = table
            self.generic_visit(node)

        def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
            self.visit_For(node)  # type: ignore[arg-type]

        def visit_Call(self, node: ast.Call) -> None:
            constructed_table = resolve_model(node.func)
            if constructed_table:
                self._record("model_construct", constructed_table)

            if isinstance(node.func, ast.Name) and (
                aliased_mutator := self._lookup_mutator(node.func.id)
            ):
                self._record_aliased_mutator(node, aliased_mutator)
                self.generic_visit(node)
                return

            leaf = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else None
            )
            if leaf in {"insert", "postgresql_insert", "sqlite_insert", "update", "delete"}:
                if node.args and (table := resolve_model(node.args[0])):
                    self._record(f"sqlalchemy_{leaf}", table)

            if leaf in {"add", "add_all"}:
                values: list[ast.AST] = list(node.args)
                if (
                    leaf == "add_all"
                    and node.args
                    and isinstance(node.args[0], (ast.List, ast.Tuple, ast.Set))
                ):
                    values = list(node.args[0].elts)
                for value in values:
                    if table := self._value_model(value):
                        self._record(f"session_{leaf}", table)

            if leaf in {"delete", "merge"} and node.args:
                if table := self._value_model(node.args[0]):
                    self._record(f"session_{leaf}", table)

            if (
                leaf
                in {
                    "bulk_insert_mappings",
                    "bulk_save_objects",
                    "bulk_update_mappings",
                }
                and node.args
            ):
                if leaf == "bulk_save_objects":
                    values = (
                        list(node.args[0].elts)
                        if isinstance(node.args[0], (ast.List, ast.Tuple, ast.Set))
                        else [node.args[0]]
                    )
                    for value in values:
                        if table := self._value_model(value):
                            self._record(f"session_{leaf}", table)
                elif table := resolve_model(node.args[0]):
                    self._record(f"session_{leaf}", table)

            if leaf == "setattr" and node.args:
                if table := self._value_model(node.args[0]):
                    self._record("instance_setattr_update", table)
                    self.attribute_dirty_tables[-1].add(table)

            relationship_mutators = {
                "add",
                "append",
                "clear",
                "discard",
                "extend",
                "pop",
                "remove",
                "setdefault",
                "update",
            }
            if (
                leaf in relationship_mutators
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Attribute)
                and (table := self._attribute_target_model(node.func.value))
            ):
                self._record("instance_relationship_mutation", table)
                self.attribute_dirty_tables[-1].add(table)

            if leaf in {"insert", "update", "delete"} and isinstance(node.func, ast.Attribute):
                receiver = node.func.value
                if table := self._query_model(receiver):
                    self._record(f"query_{leaf}", table)
                elif table := resolve_model(receiver):
                    self._record(f"table_{leaf}", table)

            if leaf in {"commit", "flush"}:
                for table in sorted(self.attribute_dirty_tables[-1]):
                    self._record(f"session_{leaf}_after_attribute_update", table)

            if leaf in {"execute", "exec_driver_sql"} and node.args:
                self._record_raw_execute(node.args[0])

            if leaf == "text" and node.args and isinstance(node.args[0], ast.Constant):
                sql = node.args[0].value
                if isinstance(sql, str) and re.match(
                    r"^\s*(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+", sql, re.IGNORECASE
                ):
                    table_match = re.match(
                        r"^\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+[\"`]?([a-zA-Z0-9_]+)",
                        sql,
                        re.IGNORECASE,
                    )
                    self._record(
                        "raw_sql_mutation",
                        table_match.group(1) if table_match else "<dynamic>",
                    )
            elif (
                leaf == "text"
                and node.args
                and isinstance(node.args[0], (ast.JoinedStr, ast.BinOp))
            ):
                self._record("unresolved_dynamic_sql", "<dynamic>")
            self.generic_visit(node)

    OrmVisitor().visit(tree)
    return counts


def _direct_orm_mutation_graph() -> list[dict[str, Any]]:
    models_by_fq_name = _orm_model_index()
    counts: Counter[tuple[str, str, str, str]] = Counter()
    for path in _production_python_paths():
        counts.update(
            _scan_direct_orm_tree(
                ast.parse(path.read_text(encoding="utf-8")),
                module=_module_name(path),
                models_by_fq_name=models_by_fq_name,
            )
        )
    return [
        {
            "module": module,
            "symbol": symbol,
            "operation": operation,
            "table": table,
            "count": count,
        }
        for (module, symbol, operation, table), count in sorted(counts.items())
    ]


def _protected_orm_reference_graph() -> list[dict[str, Any]]:
    """Freeze every explicit import/use of claim, evidence and credence ORM types."""

    protected_tables = {"belief_states", "claim_evidence", "claims"}
    models = {
        fully_qualified: table
        for fully_qualified, table in _orm_model_index().items()
        if table in protected_tables
    }
    counts: Counter[tuple[str, str, str, str]] = Counter()
    for path in _production_python_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = _module_name(path)
        model_bindings: dict[str, str] = {}
        module_bindings: dict[str, str] = {}
        for fully_qualified, table in models.items():
            model_module, _, name = fully_qualified.rpartition(".")
            if model_module == module:
                model_bindings[name] = table
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    table = models.get(f"{node.module}.{alias.name}")
                    if table:
                        model_bindings[alias.asname or alias.name] = table
                    else:
                        module_bindings[alias.asname or alias.name] = f"{node.module}.{alias.name}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    module_bindings[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0]
                    )

        def resolve_reference(node: ast.AST) -> str | None:
            if isinstance(node, ast.Name):
                return model_bindings.get(node.id)
            if isinstance(node, ast.Attribute):
                parts = [node.attr]
                value = node.value
                while isinstance(value, ast.Attribute):
                    parts.append(value.attr)
                    value = value.value
                if isinstance(value, ast.Name) and (prefix := module_bindings.get(value.id)):
                    return models.get(".".join([prefix, *reversed(parts)]))
            return None

        class ProtectedReferenceVisitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.scopes: list[str] = []

            def _visit_scope(self, node: ast.AST, name: str) -> None:
                self.scopes.append(name)
                self.generic_visit(node)
                self.scopes.pop()

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self._visit_scope(node, node.name)

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._visit_scope(node, node.name)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self._visit_scope(node, node.name)

            def _record(self, operation: str, table: str) -> None:
                symbol = ".".join(self.scopes) if self.scopes else "<module>"
                counts[(module, symbol, operation, table)] += 1

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                if node.module:
                    for alias in node.names:
                        if table := models.get(f"{node.module}.{alias.name}"):
                            self._record("model_import", table)

            def visit_Name(self, node: ast.Name) -> None:
                if isinstance(node.ctx, ast.Load) and (table := resolve_reference(node)):
                    self._record("model_reference", table)

            def visit_Attribute(self, node: ast.Attribute) -> None:
                if isinstance(node.ctx, ast.Load) and (table := resolve_reference(node)):
                    self._record("model_reference", table)
                self.generic_visit(node)

        ProtectedReferenceVisitor().visit(tree)

    return [
        {
            "module": module,
            "symbol": symbol,
            "operation": operation,
            "table": table,
            "count": count,
        }
        for (module, symbol, operation, table), count in sorted(counts.items())
    ]


def _postgres_inventory_by_table(
    inventory: dict[str, Any],
) -> defaultdict[str, list[dict[str, Any]]]:
    writes_by_table: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for write in inventory["writes"]:
        for target in _storage_targets(write):
            if target.startswith("postgres."):
                writes_by_table[target.rsplit(".", 1)[-1]].append(write)
    return writes_by_table


def test_inventory_schema_is_complete_and_has_no_dual_writes() -> None:
    inventory = _inventory()
    assert inventory["schema_version"] == 1
    assert inventory["inventory_id"] == "aletheia.legacy_write_owners.v1"
    assert inventory["policy"]["dual_write_policy"] == "none"
    writes = inventory["writes"]
    assert len(writes) == 68
    ids = [write["write_id"] for write in writes]
    assert len(ids) == len(set(ids)), "write_id values must be unique"

    for write in writes:
        assert REQUIRED_WRITE_FIELDS <= write.keys(), write["write_id"]
        assert write["dual_write_policy"] == "none", write["write_id"]
        assert write["authority_class"] in AUTHORITY_CLASSES, write["write_id"]
        assert write["cutover_pr"].startswith("PR-"), write["write_id"]
        assert write["scientific_semantics"].strip(), write["write_id"]
        assert write["target_owner"].startswith("aletheia."), write["write_id"]
        assert write["target_command_or_object"].strip(), write["write_id"]
        assert isinstance(write["call_sites"], list), write["write_id"]
        assert isinstance(write["allowed_legacy_callers"], list), write["write_id"]
        _storage_targets(write)
        fixture = write["golden_fixture"]
        if fixture is not None:
            assert "#" not in fixture, (
                write["write_id"],
                "golden_fixture must name a real file, not an unverified fragment",
            )
            relative_path = Path(fixture)
            assert not relative_path.is_absolute(), write["write_id"]
            assert ".." not in relative_path.parts, write["write_id"]
            assert (REPOSITORY_ROOT / relative_path).is_file(), write["write_id"]


def test_inventory_scans_application_and_operator_script_trees_recursively() -> None:
    policy = _inventory()["policy"]
    assert policy["production_python_roots"] == ["aletheia", "docker", "scripts"]
    assert policy["excluded_python_roots"] == {
        "migrations": (
            "Alembic revision history is schema/deployment authority, including "
            "historical DML and backfills. Revisions are immutable and reviewed "
            "separately through the migration chain; they are not runtime "
            "scientific-state write-owner surfaces."
        ),
        "tests": (
            "Test-only fixtures and temporary writes are outside deployed "
            "application, worker and operator-script authority."
        ),
    }
    expected = {
        *(REPOSITORY_ROOT / "aletheia").rglob("*.py"),
        *(REPOSITORY_ROOT / "docker").rglob("*.py"),
        *(REPOSITORY_ROOT / "scripts").rglob("*.py"),
    }
    assert set(_production_python_paths()) == expected


def test_legacy_and_migration_python_source_ast_graphs_are_frozen() -> None:
    policy = _inventory()["policy"]
    scheme = "python_ast_dump_no_attributes_type_comments_v1"
    assert policy["legacy_source_ast_scheme"] == scheme
    assert policy["legacy_source_ast_excluded_roots"] == LEGACY_SOURCE_AST_EXCLUDED_ROOTS

    legacy_graph = _legacy_source_ast_graph()
    assert len(legacy_graph) == policy["legacy_source_ast_file_count"]
    canonical = json.dumps(legacy_graph, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(canonical).hexdigest() == policy["legacy_source_ast_graph_sha256"]

    legacy_paths = {REPOSITORY_ROOT / row["path"] for row in legacy_graph}
    excluded_paths = {
        path
        for path in _production_python_paths()
        if any(
            _path_is_under(path, relative_root)
            for relative_root in LEGACY_SOURCE_AST_EXCLUDED_ROOTS
        )
    }
    assert legacy_paths.isdisjoint(excluded_paths)
    assert legacy_paths | excluded_paths == set(_production_python_paths())

    assert policy["migration_source_ast_scheme"] == scheme
    assert policy["migration_source_ast_roots"] == list(MIGRATION_SOURCE_AST_ROOTS)
    migration_graph = _migration_source_ast_graph()
    assert len(migration_graph) == policy["migration_source_ast_file_count"]
    canonical = json.dumps(migration_graph, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(canonical).hexdigest() == policy["migration_source_ast_graph_sha256"]
    expected_migration_paths = {
        path
        for relative_root in MIGRATION_SOURCE_AST_ROOTS
        for path in (REPOSITORY_ROOT / relative_root).rglob("*.py")
    }
    assert {REPOSITORY_ROOT / row["path"] for row in migration_graph} == (expected_migration_paths)


def test_non_python_production_invocation_sources_are_byte_frozen_and_owned() -> None:
    policy = _inventory()["policy"]
    assert policy["non_python_invocation_source_scheme"] == (
        "git_index_regular_mode_path_byte_sha256_v1"
    )
    assert policy["non_python_invocation_source_roots"] == [
        "frontend/**",
        "scripts/**/*.sh",
        "docker/** (non-Python)",
        "docker-compose*.yml|yaml",
    ]
    assert policy["non_python_invocation_source_exclusions"] == [
        "frontend/.next/**",
        "frontend/node_modules/**",
        "docker/**/*.py",
    ]

    graph = _non_python_invocation_source_graph()
    assert len(graph) == policy["non_python_invocation_source_file_count"]
    canonical = json.dumps(graph, sort_keys=True, separators=(",", ":")).encode()
    assert (
        hashlib.sha256(canonical).hexdigest() == policy["non_python_invocation_source_graph_sha256"]
    )
    paths = {row["path"] for row in graph}
    assert {
        "docker-compose.yml",
        "docker/simulation/ase-emt.Dockerfile",
        "frontend/lib/api.ts",
        "scripts/run_arc_direct.sh",
        "scripts/run_arc_proxy.sh",
        "scripts/run_e2e_direct.sh",
    } <= paths
    assert not any("/.next/" in path or "/node_modules/" in path for path in paths)
    assert not any(path.startswith("docker/") and path.endswith(".py") for path in paths)
    assert {row["git_mode"] for row in graph} <= {"100644", "100755"}
    assert all(not (REPOSITORY_ROOT / path).is_symlink() for path in paths)

    profiles = policy["non_python_invocation_authority_profiles"]
    assert profiles["frontend_http_mutation"] == {
        "authority_class": "client_proposal",
        "current_owner": "frontend",
        "server_authority_boundary": "aletheia.api",
        "admission_mode": "server_api_authority_and_admission",
        "direct_database_authority": False,
        "direct_scientific_authority": False,
    }
    assert profiles["shell_operational_launcher"] == {
        "authority_class": "operational_state",
        "current_owner": "scripts",
        "sink_kinds": ["external_http", "file", "process"],
        "target_owner": "aletheia.execution.operations",
        "scientific_evidence": False,
    }
    assert profiles["docker_launcher"] == {
        "authority_class": "operational_state",
        "current_owner": "docker",
        "target_owner": "aletheia.execution.node_agent",
        "scientific_evidence": False,
    }


def test_non_python_http_shell_and_docker_semantic_canaries_are_reviewed() -> None:
    policy = _inventory()["policy"]
    assert policy["frontend_http_mutation_methods"] == list(FRONTEND_HTTP_MUTATION_METHODS)
    assert _frontend_http_mutation_graph() == [
        {"path": "frontend/lib/api.ts", "method": "POST", "count": 13}
    ]
    assert _scan_frontend_http_mutations(
        "fetch(url, {method: 'POST'}); client.put(url); "
        'fetch(url, {method: "PATCH"}); client.delete(url);'
    ) == Counter({"POST": 1, "PUT": 1, "PATCH": 1, "DELETE": 1})

    expected_shell_sinks = {
        "scripts/run_arc_direct.sh": {
            "external.http_post": 1,
            "file.tmp_write": 2,
            "process.exec": 1,
        },
        "scripts/run_arc_proxy.sh": {
            "external.http_post": 1,
            "file.tmp_write": 2,
            "process.exec": 1,
        },
        "scripts/run_e2e_direct.sh": {
            "external.http_post": 1,
            "file.tmp_write": 2,
            "process.exec": 2,
        },
    }
    observed_shell_sinks: defaultdict[str, dict[str, int]] = defaultdict(dict)
    for row in _shell_operational_sink_graph():
        observed_shell_sinks[row["path"]][row["sink"]] = row["count"]
    assert dict(observed_shell_sinks) == expected_shell_sinks
    assert _scan_shell_operational_sinks(
        "curl -X PUT https://example.invalid -o /tmp/body 2>/tmp/error\n"
        "curl --request DELETE https://example.invalid\n"
        "exec command --arg\n"
    ) == Counter(
        {
            "external.http_put": 1,
            "external.http_delete": 1,
            "file.tmp_write": 2,
            "process.exec": 1,
        }
    )

    emt_dockerfile = (REPOSITORY_ROOT / "docker" / "simulation" / "ase-emt.Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "COPY docker/simulation/emt_worker.py" in emt_dockerfile
    assert re.search(r"(?m)^(?:CMD|ENTRYPOINT)\b", emt_dockerfile)


def test_non_python_source_freeze_recurses_and_rejects_symlinks(
    tmp_path: Path, monkeypatch: Any
) -> None:
    scripts = tmp_path / "scripts" / "nested"
    scripts.mkdir(parents=True)
    nested = scripts / "rogue.sh"
    nested.write_text("exec command\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setitem(globals(), "_tracked_repository_entries", lambda: [(nested, "100755")])
    assert _non_python_invocation_paths() == [nested]
    assert _non_python_invocation_source_graph() == [
        {
            "path": "scripts/nested/rogue.sh",
            "git_mode": "100755",
            "byte_sha256": hashlib.sha256(nested.read_bytes()).hexdigest(),
        }
    ]

    target = tmp_path / "outside.sh"
    target.write_text("exec command\n", encoding="utf-8")
    nested.unlink()
    nested.symlink_to(target)
    monkeypatch.setitem(globals(), "_tracked_repository_entries", lambda: [(nested, "120000")])
    with pytest.raises(AssertionError):
        _non_python_invocation_source_graph()


def test_all_referenced_writer_and_caller_symbols_exist() -> None:
    for write in _inventory()["writes"]:
        for reference in _references(write):
            assert set(reference) == {"module", "symbol"}, (write["write_id"], reference)
            assert _definition_exists(reference), (write["write_id"], reference)


def test_every_sqlalchemy_table_in_the_application_has_a_classified_write_surface() -> None:
    application_tables: set[str] = set()
    for path in _production_python_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        application_tables.update(
            statement.value.value
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            for statement in node.body
            if isinstance(statement, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__tablename__"
                for target in statement.targets
            )
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        )
    inventoried_tables = {
        target.rsplit(".", 1)[-1]
        for write in _inventory()["writes"]
        for target in _storage_targets(write)
        if target.startswith("postgres.")
    }
    assert inventoried_tables == application_tables


def test_each_scientific_storage_target_has_one_future_owner() -> None:
    owners: defaultdict[str, set[str]] = defaultdict(set)
    for write in _inventory()["writes"]:
        if write["authority_class"] == "scientific_state":
            for target in _storage_targets(write):
                owners[target].add(write["target_owner"])
            assert write["golden_fixture"], write["write_id"]
    conflicts = {target: values for target, values in owners.items() if len(values) != 1}
    assert not conflicts


def test_claim_evidence_and_belief_cut_over_without_mutable_mirroring() -> None:
    protected_targets = {
        "postgres.memory_ledger.claims",
        "postgres.memory_ledger.claim_evidence",
        "postgres.memory_ledger.belief_states",
    }
    writes = [
        write for write in _inventory()["writes"] if write["storage_target"] in protected_targets
    ]
    assert {write["storage_target"] for write in writes} == protected_targets
    for write in writes:
        assert write["migration_mode"] == "freeze_import_cutover", write["write_id"]
        assert write["post_cutover_legacy_mode"] == "read_only", write["write_id"]
        assert write["dual_write_policy"] == "none", write["write_id"]


def _application_service_imports() -> dict[str, list[str]]:
    imports: dict[str, list[str]] = {}
    for path in sorted((REPOSITORY_ROOT / "aletheia").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = sorted(
            {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module == "aletheia.memory.service"
                for alias in node.names
            }
        )
        if names:
            imports[_module_name(path)] = names
    return imports


def _memory_service_mutator_usage_graph(
    read_only_symbols: set[str],
) -> list[dict[str, str]]:
    """Return imported mutator name-loads bound to their lexical call sites.

    The legacy code passes some service functions through ``asyncio.to_thread``
    instead of calling them directly. Tracking imported name loads therefore
    freezes both direct calls and callable-argument uses without pretending to
    discover arbitrary SQLAlchemy mutations.
    """

    usages: set[tuple[str, str, str]] = set()
    for path in _production_python_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bindings: defaultdict[str, set[str]] = defaultdict(set)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "aletheia.memory.service":
                for alias in node.names:
                    if alias.name not in read_only_symbols:
                        bindings[alias.asname or alias.name].add(alias.name)
        if not bindings:
            continue

        module = _module_name(path)

        class UsageVisitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.scope: list[str] = []

            def _visit_scope(self, node: ast.AST, name: str) -> None:
                self.scope.append(name)
                self.generic_visit(node)
                self.scope.pop()

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self._visit_scope(node, node.name)

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._visit_scope(node, node.name)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self._visit_scope(node, node.name)

            def visit_Name(self, node: ast.Name) -> None:
                if isinstance(node.ctx, ast.Load) and node.id in bindings:
                    assert self.scope, (
                        f"module-level use of memory.service.{sorted(bindings[node.id])} in {module}"
                    )
                    for entrypoint in bindings[node.id]:
                        usages.add((entrypoint, module, ".".join(self.scope)))

        UsageVisitor().visit(tree)

    return [
        {"entrypoint": entrypoint, "module": module, "symbol": symbol}
        for entrypoint, module, symbol in sorted(usages)
    ]


def test_all_application_memory_service_imports_are_frozen_and_mutators_inventoried() -> None:
    inventory = _inventory()
    policy = inventory["policy"]
    imported_by_module = _application_service_imports()
    assert imported_by_module == policy["service_import_allowlist_by_module"]
    imported = {name for names in imported_by_module.values() for name in names}
    all_imported = {
        alias.name
        for path in _production_python_paths()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.module == "aletheia.memory.service"
        for alias in node.names
    }
    read_only = set(policy["service_read_symbols"])
    assert set(policy["driver_service_read_allowlist"]) <= read_only
    assert read_only <= all_imported
    imported_mutators = imported - read_only
    inventoried_entrypoints = {
        write["legacy_entrypoint"]["symbol"]
        for write in inventory["writes"]
        if write["legacy_entrypoint"]["module"] == "aletheia.memory.service"
    }
    assert imported_mutators <= inventoried_entrypoints


def test_memory_service_mutator_usage_graph_is_frozen() -> None:
    """Catch new uses of an already-imported mutator at a different call site."""

    policy = _inventory()["policy"]
    assert policy["service_mutator_usage_scheme"] == "ast_all_scope_imported_name_load_scope_v1"
    graph = _memory_service_mutator_usage_graph(set(policy["service_read_symbols"]))
    assert len(graph) == policy["service_mutator_usage_count"]
    canonical = json.dumps(graph, sort_keys=True, separators=(",", ":")).encode("utf-8")
    actual_sha256 = hashlib.sha256(canonical).hexdigest()
    assert actual_sha256 == policy["service_mutator_call_graph_sha256"], graph


def test_every_writer_surface_caller_graph_is_frozen_and_declared() -> None:
    inventory = _inventory()
    policy = inventory["policy"]
    assert policy["writer_surface_usage_scheme"] == "ast_all_scope_import_alias_instance_scope_v2"
    graph = _writer_surface_usage_graph(inventory)
    assert len(graph) == policy["writer_surface_usage_count"]
    canonical = json.dumps(graph, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == policy["writer_surface_call_graph_sha256"]

    usages_by_write: defaultdict[str, set[tuple[str, str]]] = defaultdict(set)
    used_surfaces = {usage["surface"] for usage in graph}
    for usage in graph:
        usages_by_write[usage["write_id"]].add((usage["module"], usage["symbol"]))

    exceptions = policy["writer_surface_static_resolution_exceptions"]
    all_surfaces = {
        (
            write["write_id"],
            f"{reference['module']}.{reference['symbol']}",
        )
        for write in inventory["writes"]
        for reference in [write["legacy_entrypoint"], *write.get("additional_writers", [])]
    }
    unresolved = {write_id for write_id, surface in all_surfaces if surface not in used_surfaces}
    assert unresolved == set(exceptions)
    assert all(isinstance(reason, str) and reason.strip() for reason in exceptions.values())

    for write in inventory["writes"]:
        declared = write["call_sites"]
        allowed = write["allowed_legacy_callers"]
        assert allowed, f"{write['write_id']} needs an explicit legacy caller allowlist"
        for candidate in allowed:
            assert any(
                caller["module"] == candidate["module"]
                and (
                    caller["symbol"] == candidate["symbol"]
                    or caller["symbol"].startswith(f"{candidate['symbol']}.")
                    or candidate["symbol"].startswith(f"{caller['symbol']}.")
                )
                for caller in declared
            ), (write["write_id"], candidate)
        actual = usages_by_write[write["write_id"]]
        if actual:
            assert declared, f"{write['write_id']} has statically resolved callers"
            uncovered = [
                {"module": module, "symbol": symbol}
                for module, symbol in sorted(actual)
                if not any(
                    candidate["module"] == module
                    and (
                        candidate["symbol"] == symbol
                        or symbol.startswith(f"{candidate['symbol']}.")
                    )
                    for candidate in declared
                )
            ]
            assert not uncovered, (write["write_id"], uncovered)
            application_uncovered = [
                {"module": module, "symbol": symbol}
                for module, symbol in sorted(actual)
                if module.startswith("aletheia.")
                if not any(
                    candidate["module"] == module
                    and (
                        candidate["symbol"] == symbol
                        or symbol.startswith(f"{candidate['symbol']}.")
                    )
                    for candidate in allowed
                )
            ]
            assert not application_uncovered, (write["write_id"], application_uncovered)
            continue

        assert write["write_id"] in exceptions
        assert declared, f"{write['write_id']} needs a verifiable declared caller"
        writer_leaves = {
            reference["symbol"].rsplit(".", 1)[-1]
            for reference in [write["legacy_entrypoint"], *write.get("additional_writers", [])]
        }
        for caller in declared:
            caller_node = _definition_node(caller)
            referenced_leaves = {
                node.id if isinstance(node, ast.Name) else node.attr
                for node in ast.walk(caller_node)
                if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load)
            }
            assert writer_leaves & referenced_leaves, (write["write_id"], caller)


def test_every_direct_file_sink_has_a_frozen_reviewed_authority_profile() -> None:
    policy = _inventory()["policy"]
    assert policy["direct_file_sink_scheme"] == "ast_reviewed_direct_file_sink_v1"
    graph = _direct_file_sink_graph()
    assert len(graph) == policy["direct_file_sink_scope_count"]
    assert sum(row["count"] for row in graph) == policy["direct_file_sink_call_count"]
    canonical = json.dumps(graph, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == policy["direct_file_sink_graph_sha256"]

    profiles = policy["direct_file_sink_profiles"]
    prefix_profiles = policy["direct_file_sink_profile_by_prefix"]
    required_profile_fields = {
        "authority_class",
        "current_owner_rule",
        "target_owner",
        "migration_mode",
        "cutover_pr",
        "dual_write_policy",
    }
    for name, profile in profiles.items():
        assert set(profile) == required_profile_fields, name
        assert profile["authority_class"] in AUTHORITY_CLASSES, name
        assert profile["current_owner_rule"] == "sink_module", name
        assert profile["target_owner"].startswith("aletheia."), name
        assert profile["cutover_pr"].startswith("PR-"), name
        assert profile["dual_write_policy"] == "none", name

    for row in graph:
        matches = [prefix for prefix in prefix_profiles if row["module"].startswith(prefix)]
        assert matches, row
        longest = max(map(len, matches))
        selected = [prefix for prefix in matches if len(prefix) == longest]
        assert len(selected) == 1, (row, selected)
        assert prefix_profiles[selected[0]] in profiles, row

    capability_profile = profiles["versioned_capability_registry"]
    assert capability_profile == {
        "authority_class": "platform_state",
        "current_owner_rule": "sink_module",
        "target_owner": "aletheia.capabilities",
        "migration_mode": "versioned_capability_registry_cutover",
        "cutover_pr": "PR-3",
        "dual_write_policy": "none",
    }
    assert prefix_profiles["scripts.capability_"] == "versioned_capability_registry"
    assert any(row["module"].startswith("scripts.capability_") for row in graph)

    required_scientific_sinks = {
        ("aletheia.api.datasets", "upload"),
        ("aletheia.data.external_supercon2", "prepare_supercon2_external"),
        ("aletheia.data.loaders", "download"),
        ("aletheia.domains.protocol", "_parity_plot"),
        ("aletheia.domains.protocol", "_persist_model"),
        ("aletheia.domains.protocol", "grouped_regression_eval"),
        ("aletheia.domains.rag.plugin", "RagEvalPlugin.evaluate"),
        ("docker.simulation.emt_worker", "_atomic_json"),
    }
    observed = {(row["module"], row["symbol"]) for row in graph}
    assert required_scientific_sinks <= observed


def test_direct_file_scanner_distinguishes_path_open_from_archive_open(
    tmp_path: Path, monkeypatch: Any
) -> None:
    package = tmp_path / "aletheia"
    scripts = tmp_path / "scripts"
    package.mkdir()
    scripts.mkdir()
    (package / "synthetic_sink.py").write_text(
        "import gzip\n"
        "def write(path):\n"
        "    with path.open('wb') as handle:\n"
        "        handle.write(b'x')\n"
        "def archive_read(path):\n"
        "    with gzip.open(path, 'rb') as handle:\n"
        "        handle.read()\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(globals(), "REPOSITORY_ROOT", tmp_path)
    graph = _direct_file_sink_graph()
    assert {(row["symbol"], row["sink"]) for row in graph} == {
        ("write", "open:wb"),
        ("write", "write"),
    }


def test_direct_orm_mutations_are_frozen_classified_and_owned_by_declared_writers() -> None:
    inventory = _inventory()
    policy = inventory["policy"]
    assert policy["direct_orm_mutation_scheme"] == "ast_import_bound_orm_mutation_v2"
    graph = _direct_orm_mutation_graph()
    assert len(graph) == policy["direct_orm_mutation_scope_count"]
    assert sum(row["count"] for row in graph) == policy["direct_orm_mutation_call_count"]
    canonical = json.dumps(graph, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == policy["direct_orm_mutation_graph_sha256"]
    assert all(row["table"] != "<dynamic>" for row in graph), graph

    writes_by_table = _postgres_inventory_by_table(inventory)
    assert writes_by_table
    for row in graph:
        classified = writes_by_table[row["table"]]
        assert classified, row
        authority_profiles = {
            (write["authority_class"], write["target_owner"]) for write in classified
        }
        assert len(authority_profiles) == 1, (row, authority_profiles)
        if row["operation"] == "model_construct":
            # Construction is frozen as a reviewed precursor, but it is not a
            # durable mutation until a recognized persistence primitive uses it.
            continue
        writer_references = [
            reference
            for write in classified
            for reference in [write["legacy_entrypoint"], *write.get("additional_writers", [])]
        ]
        assert any(
            reference["module"] == row["module"]
            and (
                reference["symbol"] == row["symbol"]
                or row["symbol"].startswith(f"{reference['symbol']}.")
            )
            for reference in writer_references
        ), row

    protected = policy["protected_scientific_orm_writers"]
    assert set(protected) == {"claims", "claim_evidence", "belief_states"}
    for table, allowed_symbols in protected.items():
        rows = [row for row in graph if row["table"] == table]
        assert {row["operation"] for row in rows} >= {"model_construct", "session_add"}
        for row in rows:
            actual = f"{row['module']}.{row['symbol']}"
            assert any(
                actual == allowed or actual.startswith(f"{allowed}.") for allowed in allowed_symbols
            ), row


def test_protected_orm_import_and_reference_graph_is_frozen() -> None:
    policy = _inventory()["policy"]
    assert policy["protected_orm_reference_scheme"] == "ast_explicit_import_and_reference_scope_v1"
    graph = _protected_orm_reference_graph()
    assert len(graph) == policy["protected_orm_reference_scope_count"]
    assert sum(row["count"] for row in graph) == policy["protected_orm_reference_call_count"]
    canonical = json.dumps(graph, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == policy["protected_orm_reference_graph_sha256"]
    assert {row["table"] for row in graph} == {
        "belief_states",
        "claim_evidence",
        "claims",
    }
    imported_by_service = {
        row["table"]
        for row in graph
        if row["module"] == "aletheia.memory.service" and row["operation"] == "model_import"
    }
    assert imported_by_service == {"belief_states", "claim_evidence", "claims"}


def test_direct_orm_scanner_catches_protected_alias_and_execute_bypasses() -> None:
    tree = ast.parse(
        """
from sqlalchemy import delete, insert, update
from aletheia.memory.ledger import BeliefState as B, Claim as C, ClaimEvidence as E

def bypass(session):
    claim = C()
    session.add(claim)
    session.add_all([E()])
    loaded_claim = session.get(C, "claim")
    loaded_belief = session.query(B).first()
    loaded_claim.strength = "strong"
    loaded_belief.credence = 0.9
    setattr(loaded_claim, "status", "accepted")
    loaded_claim.evidence.append(E())
    session.query(B).filter_by(run_id="run").update({"credence": 0.9})
    session.query(C).delete()
    session.execute(update(E).values(evidence_kind="metric"))
    session.execute(delete(B))
    session.execute(insert(C))
    session.execute(C.__table__.update())
    session.execute(C.__table__.insert().values(run_id="run"))
    session.bulk_update_mappings(C, [{"id": "claim", "strength": "weak"}])
    session.bulk_insert_mappings(B, [{"run_id": "run", "credence": 0.5}])
    session.merge(loaded_claim)
    session.flush()
    session.commit()
    session.connection().exec_driver_sql("UPDATE claims SET strength = 'weak'")
    session.execute(f"UPDATE belief_states SET credence = {0.7}")
    session.execute("DELETE FROM belief_states")
"""
    )
    counts = _scan_direct_orm_tree(
        tree,
        module="synthetic.direct_orm_bypass",
        models_by_fq_name=_orm_model_index(),
    )
    operations = {(operation, table) for _, _, operation, table in counts}
    assert {
        ("model_construct", "claims"),
        ("session_add", "claims"),
        ("session_add_all", "claim_evidence"),
        ("query_update", "belief_states"),
        ("query_delete", "claims"),
        ("sqlalchemy_update", "claim_evidence"),
        ("sqlalchemy_delete", "belief_states"),
        ("sqlalchemy_insert", "claims"),
        ("table_update", "claims"),
        ("table_insert", "claims"),
        ("instance_attribute_update", "claims"),
        ("instance_attribute_update", "belief_states"),
        ("instance_setattr_update", "claims"),
        ("instance_relationship_mutation", "claims"),
        ("session_bulk_update_mappings", "claims"),
        ("session_bulk_insert_mappings", "belief_states"),
        ("session_merge", "claims"),
        ("session_flush_after_attribute_update", "claims"),
        ("session_commit_after_attribute_update", "belief_states"),
        ("raw_execute_mutation", "claims"),
        ("raw_execute_mutation", "belief_states"),
        ("unresolved_dynamic_sql", "<dynamic>"),
    } <= operations


def test_direct_orm_scanner_catches_run_callable_alias_bypasses() -> None:
    tree = ast.parse(
        """
from aletheia.memory.ledger import Run

def rogue(session):
    run = session.get(Run, "run")
    mutate = setattr
    persist = session.merge
    bulk_write = session.bulk_update_mappings
    finish = session.commit
    mutate(run, "status", "passed")
    persist(run)
    bulk_write(Run, [{"id": "run", "status": "passed"}])
    finish()
"""
    )
    counts = _scan_direct_orm_tree(
        tree,
        module="synthetic.run_callable_alias",
        models_by_fq_name=_orm_model_index(),
    )
    operations = {(operation, table) for _, _, operation, table in counts}
    assert {
        ("instance_setattr_update", "runs"),
        ("session_merge", "runs"),
        ("session_bulk_update_mappings", "runs"),
        ("session_commit_after_attribute_update", "runs"),
    } <= operations


def test_direct_orm_scanner_binds_self_and_cls_for_orm_model_methods() -> None:
    tree = ast.parse(
        """
class LocalRun:
    __tablename__ = "runs"

    def mark(self):
        self.status = "passed"
        self.children.append(object())

    @classmethod
    def mark_class(cls):
        cls.status = "passed"
"""
    )
    counts = _scan_direct_orm_tree(
        tree,
        module="synthetic.orm_model_method",
        models_by_fq_name={"synthetic.orm_model_method.LocalRun": "runs"},
    )
    assert {
        ("LocalRun.mark", "instance_attribute_update", "runs"),
        ("LocalRun.mark", "instance_relationship_mutation", "runs"),
        ("LocalRun.mark_class", "instance_attribute_update", "runs"),
    } <= {(symbol, operation, table) for _, symbol, operation, table in counts}


def test_direct_orm_scanner_resolves_aliases_for_every_inventoried_orm_table() -> None:
    models = _orm_model_index()
    model_by_table: dict[str, str] = {}
    for fully_qualified, table in sorted(models.items()):
        model_by_table.setdefault(table, fully_qualified)

    inventoried_tables = set(_postgres_inventory_by_table(_inventory()))
    assert set(model_by_table) == inventoried_tables
    for table, fully_qualified in model_by_table.items():
        model_module, _, model_name = fully_qualified.rpartition(".")
        tree = ast.parse(
            f"from {model_module} import {model_name} as ModelAlias\n"
            "def bypass(session):\n"
            "    obj = session.get(ModelAlias, 'id')\n"
            "    mutate = setattr\n"
            "    persist = session.merge\n"
            "    bulk_write = session.bulk_update_mappings\n"
            "    mutate(obj, 'status', 'changed')\n"
            "    persist(obj)\n"
            "    bulk_write(ModelAlias, [{}])\n"
        )
        counts = _scan_direct_orm_tree(
            tree,
            module="synthetic.all_orm_models",
            models_by_fq_name=models,
        )
        assert {
            ("instance_setattr_update", table),
            ("session_merge", table),
            ("session_bulk_update_mappings", table),
        } <= {(operation, observed_table) for _, _, operation, observed_table in counts}, (
            table,
            fully_qualified,
        )


def test_direct_external_sinks_are_frozen_classified_and_event_callers_declared() -> None:
    inventory = _inventory()
    policy = inventory["policy"]
    assert policy["direct_external_sink_scheme"] == "ast_reviewed_event_provider_process_sink_v1"
    graph = _direct_external_sink_graph()
    assert len(graph) == policy["direct_external_sink_scope_count"]
    assert sum(row["count"] for row in graph) == policy["direct_external_sink_call_count"]
    canonical = json.dumps(graph, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == policy["direct_external_sink_graph_sha256"]

    profiles = policy["direct_external_sink_profiles"]
    assert set(profiles) == {"event.", "provider.", "github.", "process."}
    for prefix, profile in profiles.items():
        assert profile["authority_class"] in AUTHORITY_CLASSES, prefix
        assert profile["target_owner"].startswith("aletheia."), prefix
        assert profile["cutover_pr"].startswith("PR-"), prefix
        assert profile["dual_write_policy"] == "none", prefix
    for row in graph:
        matches = [prefix for prefix in profiles if row["sink"].startswith(prefix)]
        assert len(matches) == 1, row

    required_outbound_sinks = {
        ("aletheia.auth.providers.feishu", "provider.http_post"),
        ("aletheia.critics.providers.gemini_api", "provider.model_create"),
        ("aletheia.iam.github_app", "github.create_pull_request"),
        ("aletheia.notify.feishu", "provider.http_post"),
        ("aletheia.orchestrator.openai_runtime", "provider.model_create"),
        ("aletheia.compute.local", "process.Popen"),
    }
    observed = {(row["module"], row["sink"]) for row in graph}
    assert required_outbound_sinks <= observed

    writes_by_id = {write["write_id"]: write for write in inventory["writes"]}
    event_profile = profiles["event."]
    assert event_profile["target_owner"] == "aletheia.research_kernel.commands"
    assert event_profile["legacy_event_semantics"] == ("compatibility_telemetry_projection_only")
    assert event_profile["authoritative_research_event_admission"] == (
        "kernel_command_transaction_only"
    )
    assert event_profile["direct_authoritative_research_event"] is False
    assert {
        writes_by_id["event.persist"]["target_owner"],
        writes_by_id["event.publish"]["target_owner"],
    } == {"aletheia.research_kernel.commands"}
    assert (
        writes_by_id["event.publish"]["target_command_or_object"] == "TransactionalOutboxProjection"
    )
    assert (
        "cannot directly admit an authoritative ResearchEvent"
        in writes_by_id["event.publish"]["scientific_semantics"]
    )

    sink_write_map = policy["direct_external_sink_write_map"]
    assert set(sink_write_map) == {
        "event.publish",
        "aletheia.iam.github_app|github.create_for_authenticated_user",
        "aletheia.iam.github_app|github.create_in_org",
        "aletheia.iam.github_app|github.create_or_update_file_contents",
        "aletheia.iam.github_app|github.create_pull_request",
        "aletheia.iam.github_app|github.create_ref",
        "aletheia.notify.feishu|provider.http_post",
    }
    for key, write_ids in sink_write_map.items():
        if "|" in key:
            module, sink = key.split("|", 1)
            matching_rows = [
                row for row in graph if row["module"] == module and row["sink"] == sink
            ]
        else:
            sink = key
            matching_rows = [row for row in graph if row["sink"] == sink]
        assert matching_rows, key
        owners = {writes_by_id[write_id]["target_owner"] for write_id in write_ids}
        assert len(owners) == 1, (key, owners)
        profile_prefixes = [prefix for prefix in profiles if sink.startswith(prefix)]
        assert len(profile_prefixes) == 1, key
        assert owners == {profiles[profile_prefixes[0]]["target_owner"]}, key

    event_write = next(
        write for write in inventory["writes"] if write["write_id"] == "event.publish"
    )
    for field in ("call_sites", "allowed_legacy_callers"):
        declared = event_write[field]
        for row in graph:
            if row["sink"] != "event.publish":
                continue
            assert any(
                caller["module"] == row["module"]
                and (
                    caller["symbol"] == row["symbol"]
                    or row["symbol"].startswith(f"{caller['symbol']}.")
                )
                for caller in declared
            ), (field, row)


def test_direct_external_scanner_catches_provider_and_process_aliases(
    tmp_path: Path, monkeypatch: Any
) -> None:
    package = tmp_path / "aletheia"
    scripts = tmp_path / "scripts"
    package.mkdir()
    scripts.mkdir()
    (package / "synthetic_external.py").write_text(
        "import httpx\n"
        "from subprocess import run as launch\n"
        "def outbound(client):\n"
        "    with httpx.Client() as http:\n"
        "        http.put('https://example.invalid')\n"
        "        http.patch('https://example.invalid')\n"
        "        http.delete('https://example.invalid')\n"
        "    client.messages.create(model='model')\n"
        "    client.models.generate_content(model='model', contents='prompt')\n"
        "    launch(['true'])\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(globals(), "REPOSITORY_ROOT", tmp_path)
    observed = {row["sink"] for row in _direct_external_sink_graph()}
    assert {
        "process.run",
        "provider.http_delete",
        "provider.http_patch",
        "provider.http_put",
        "provider.model_create",
    } <= observed


def test_event_and_external_side_effect_surfaces_are_in_the_caller_audit() -> None:
    inventory = _inventory()
    graph = _writer_surface_usage_graph(inventory)
    used_surfaces = {row["surface"] for row in graph}
    exceptions = set(inventory["policy"]["writer_surface_static_resolution_exceptions"])
    required_write_ids = {
        "event.persist",
        "event.publish",
        "jobs.external_actions",
        "github.ensure_repo",
        "github.ensure_branch",
        "github.put_file",
        "github.open_pr",
    }
    writes = {write["write_id"]: write for write in inventory["writes"]}
    assert required_write_ids <= writes.keys()
    for write_id in required_write_ids:
        write = writes[write_id]
        surface = f"{write['legacy_entrypoint']['module']}.{write['legacy_entrypoint']['symbol']}"
        assert surface in used_surfaces or write_id in exceptions
        assert write["call_sites"], write_id
        assert write["dual_write_policy"] == "none", write_id


def test_all_service_mutator_entries_list_their_exact_current_callers() -> None:
    inventory = _inventory()
    graph = _memory_service_mutator_usage_graph(set(inventory["policy"]["service_read_symbols"]))
    by_entrypoint: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    for usage in graph:
        by_entrypoint[usage["entrypoint"]].append(
            {"module": usage["module"], "symbol": usage["symbol"]}
        )

    service_writes = [
        write
        for write in inventory["writes"]
        if write["legacy_entrypoint"]["module"] == "aletheia.memory.service"
    ]
    assert {write["legacy_entrypoint"]["symbol"] for write in service_writes} == set(by_entrypoint)
    for write in service_writes:
        entrypoint = write["legacy_entrypoint"]["symbol"]
        expected = {
            (reference["module"], reference["symbol"]) for reference in by_entrypoint[entrypoint]
        }
        declared = {(reference["module"], reference["symbol"]) for reference in write["call_sites"]}
        assert declared == expected, write["write_id"]

        allowed = write["allowed_legacy_callers"]
        uncovered = [
            reference
            for reference in by_entrypoint[entrypoint]
            if reference["module"].startswith("aletheia.")
            if not any(
                candidate["module"] == reference["module"]
                and (
                    candidate["symbol"] == reference["symbol"]
                    or reference["symbol"].startswith(f"{candidate['symbol']}.")
                )
                for candidate in allowed
            )
        ]
        assert not uncovered, (write["write_id"], uncovered)


def test_memory_service_cannot_bypass_symbol_level_import_inventory() -> None:
    bypasses: list[str] = []
    for path in sorted((REPOSITORY_ROOT / "aletheia").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                alias.name == "aletheia.memory.service" for alias in node.names
            ):
                bypasses.append(f"{path.relative_to(REPOSITORY_ROOT)}:{node.lineno}:module-import")
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module == "aletheia.memory"
                and any(alias.name == "service" for alias in node.names)
            ):
                bypasses.append(f"{path.relative_to(REPOSITORY_ROOT)}:{node.lineno}:from-import")
            elif (
                isinstance(node, ast.Call)
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "aletheia.memory.service"
            ):
                bypasses.append(f"{path.relative_to(REPOSITORY_ROOT)}:{node.lineno}:dynamic-import")
    assert not bypasses


def test_driver_direct_file_writers_are_explicitly_inventoried() -> None:
    tree = ast.parse(DRIVER_PATH.read_text(encoding="utf-8"))
    driver = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ExperimentDriver"
    )
    file_writers = {
        node.name
        for node in driver.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(descendant, ast.Call)
            and isinstance(descendant.func, ast.Attribute)
            and descendant.func.attr in {"write_text", "write_bytes"}
            for descendant in ast.walk(node)
        )
    }
    inventoried = {
        reference["symbol"].removeprefix("ExperimentDriver.")
        for write in _inventory()["writes"]
        for reference in [write["legacy_entrypoint"], *write.get("additional_writers", [])]
        if reference["module"] == "aletheia.scheduler.driver"
        and reference["symbol"].startswith("ExperimentDriver.")
    }
    assert file_writers <= inventoried


def test_only_durable_scheduler_imports_experiment_driver_in_production() -> None:
    actual_importers: set[str] = set()
    for path in (REPOSITORY_ROOT / "aletheia").rglob("*.py"):
        if path == DRIVER_PATH:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports_driver = any(
            (isinstance(node, ast.ImportFrom) and node.module == "aletheia.scheduler.driver")
            or (
                isinstance(node, ast.Import)
                and any(alias.name == "aletheia.scheduler.driver" for alias in node.names)
            )
            for node in ast.walk(tree)
        )
        if imports_driver:
            actual_importers.add(_module_name(path))
    assert actual_importers == set(_inventory()["policy"]["driver_import_allowlist"])


def test_inventory_has_no_expired_migration_exceptions() -> None:
    expired: list[str] = []
    for write in _inventory()["writes"]:
        expiry = write["exception_expiry"]
        if expiry is not None and date.fromisoformat(expiry) < date.today():
            expired.append(write["write_id"])
    assert not expired
