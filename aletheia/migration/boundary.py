"""Static dependency boundaries for the scientific control-plane migration.

The checks in this module deliberately inspect the complete internal import graph.  A protected
package therefore cannot reach a legacy writer through a seemingly harmless helper module.  The
scanner uses only the Python standard library so it can run before application imports or database
setup in CI.
"""

from __future__ import annotations

import ast
import hashlib
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DEFAULT_TARGET_PACKAGES = (
    "research_kernel",
    "research_store",
    "protocols",
    "execution",
    "planning",
    "observations",
)

# At least the kernel sentinel must exist in PR-0.  This prevents a missing-directory typo from
# making the architecture test vacuously green.
DEFAULT_REQUIRED_TARGET_PACKAGES = ("research_kernel",)

# These packages expose mutable legacy scientific state, the fixed legacy control plane, or
# general-purpose runtime loaders.  No new authority package may reach them, even indirectly;
# otherwise a loader could reconstruct a forbidden import after this static check has completed.
DEFAULT_FORBIDDEN_MODULE_PREFIXES = (
    "builtins",
    "importlib",
    "aletheia.compute",
    "aletheia.data.registry",
    "aletheia.domains",
    "aletheia.events",
    "aletheia.memory.ledger",
    "aletheia.memory.service",
    "aletheia.orchestrator.tools",
    "aletheia.scheduler.driver",
    "aletheia.scheduler.statemachine",
    "pkgutil",
    "runpy",
    "zipimport",
)

# Production runtime loading is reserved for the content-pinned guard below.  The metadata API is
# intentionally data-only for this policy and remains available for provenance/version receipts.
DEFAULT_FORBIDDEN_RUNTIME_LOADER_PREFIXES = (
    "builtins",
    "importlib",
    "pkgutil",
    "runpy",
    "zipimport",
)
DEFAULT_ALLOWED_RUNTIME_LOADER_PREFIXES = ("importlib.metadata",)

# The kernel and protocol compiler must also remain free of operational persistence and mutable
# program projections.  Execution/planning/observation packages can later receive narrower,
# symbol-level ports; they still inherit the common legacy bans above.
DEFAULT_STRICT_TARGET_PACKAGES = ("research_kernel", "protocols")
DEFAULT_STRICT_FORBIDDEN_MODULE_PREFIXES = (
    "alembic",
    "aletheia.capabilities.observations",
    "aletheia.capabilities.promotion",
    "aletheia.capabilities.registry",
    "aletheia.db",
    "aletheia.epistemics.persistence",
    "aletheia.jobs.actions",
    "aletheia.jobs.fault_campaign",
    "aletheia.jobs.outbox",
    "aletheia.jobs.queue",
    "aletheia.knowledge.persistence",
    "aletheia.programs.endurance",
    "aletheia.programs.graph",
    "aletheia.programs.memory",
    "aletheia.programs.persistence",
    "aletheia.programs.portfolio",
    "aletheia.research_store",
    "asyncpg",
    "psycopg",
    "psycopg2",
    "sqlite3",
    "sqlalchemy",
    "sys",
)

# PR-3's protocol compiler and the small execution schema/port surface are pure contracts.  Keep
# the execution list exact: PR-4 will add operational allocators and node agents under the same
# package, and those adapters will legitimately need process, network, and persistence APIs.  The
# package initializer is listed because Python executes it before every execution submodule.
DEFAULT_PURE_CONTRACT_TARGET_PACKAGES = ("protocols",)
DEFAULT_PURE_CONTRACT_TARGET_MODULES = (
    "aletheia.execution",
    "aletheia.execution.ports",
    "aletheia.execution.schemas",
)
DEFAULT_PURE_CONTRACT_FORBIDDEN_MODULE_PREFIXES = (
    # Existing mutable/runtime scientific control planes.
    "aletheia.api",
    "aletheia.capabilities",
    "aletheia.config",
    "aletheia.db",
    "aletheia.domains",
    "aletheia.epistemics",
    "aletheia.evals",
    "aletheia.jobs",
    "aletheia.migration",
    "aletheia.research_store",
    "aletheia.scheduler",
    # Database clients and ORMs.
    "alembic",
    "asyncpg",
    "databases",
    "django.db",
    "pymongo",
    "psycopg",
    "psycopg2",
    "redis",
    "sqlite3",
    "sqlalchemy",
    # Filesystem and host-resource APIs. ``open`` itself is represented by the sentinel below.
    "aiofiles",
    "fileinput",
    "glob",
    "io.open",
    "os",
    "pathlib",
    "shutil",
    "tempfile",
    # Network clients and transports.
    "aiohttp",
    "boto3",
    "ftplib",
    "grpc",
    "http",
    "httpx",
    "paramiko",
    "requests",
    "smtplib",
    "socket",
    "socketserver",
    "urllib",
    "websockets",
    # Process, thread, and host-control APIs.
    "asyncio",
    "concurrent.futures",
    "ctypes",
    "multiprocessing",
    "pty",
    "signal",
    "subprocess",
    "threading",
)

# The temporary v1 compatibility binders are intentionally leaf modules.  They fingerprint opaque
# legacy bytes but must never import either authority graph, and their parent package must not make
# them an ambient migration API by importing/re-exporting them from ``__init__``.
DEFAULT_READ_ONLY_COMPATIBILITY_LEAF_MODULES = ("aletheia.migration.protocol_v1_compatibility",)

# The event-store adapter may use SQLAlchemy and ``aletheia.db``, but it must not recover
# authority by reaching into any mutable legacy scientific store.  This keeps the dependency
# direction one-way: research_store -> research_kernel, never research_store -> legacy authority.
DEFAULT_ADAPTER_TARGET_PACKAGES = ("research_store",)
DEFAULT_ADAPTER_FORBIDDEN_MODULE_PREFIXES = (
    "aletheia.epistemics.persistence",
    "aletheia.jobs.outbox",
    "aletheia.knowledge.persistence",
    "aletheia.programs.endurance",
    "aletheia.programs.graph",
    "aletheia.programs.memory",
    "aletheia.programs.persistence",
    "aletheia.programs.portfolio",
)

DEFAULT_LEGACY_DRIVER_IMPORTERS = ("aletheia.scheduler.durable",)
DEFAULT_RESEARCH_STORE_PERSISTENCE_IMPORTERS = (
    "aletheia.research_store.store",
    "aletheia.schema_migrations",
    "migrations.env",
)
DEFAULT_PRIVATE_RESEARCH_STORE_RECORD_SYMBOLS = (
    "ResearchKernelCommandReceiptRecord",
    "ResearchKernelEventRecord",
    "ResearchKernelObjectRecord",
    "ResearchKernelOutboxRecord",
    "ResearchKernelSnapshotRecord",
    "ResearchQuestAuthorityRecord",
    "ResearchQuestStreamRecord",
)
# Python entry points shipped outside the importable ``aletheia`` package still participate in
# the production driver boundary. Keep this finite registry explicit: CLIs, container workers, and
# Alembic migrations are all executable production inputs even though only the first two belong in
# the legacy scientific-write inventory.
DEFAULT_OPERATIONAL_PYTHON_ROOTS = ("scripts", "docker", "migrations")
_DYNAMIC_ESCAPE = "<non-literal-dynamic-import>"
_FILE_LOADER_ESCAPE = "<runtime-file-loader>"
_RUNTIME_CODE_ESCAPE = "<runtime-code-execution>"
_FILESYSTEM_API_ESCAPE = "<filesystem-api>"

# Non-literal runtime loading is centralized in one small, content-pinned guard.  Changing either
# its implementation or the identity of the permitted module therefore requires an explicit policy
# review here; repository scripts themselves receive no dynamic-escape exemption.
DEFAULT_AUDITED_DYNAMIC_LOADER_SOURCES = (
    (
        "aletheia.migration.dynamic_loader",
        "7c2227c3db42c5d9a3851bf4090865c544ce535c7c5a8b53358cea8ea0204a0e",
    ),
)
DEFAULT_AUDITED_DYNAMIC_LOADER_ESCAPE_COUNTS = (
    ("aletheia.migration.dynamic_loader", _DYNAMIC_ESCAPE, 1),
    ("aletheia.migration.dynamic_loader", _FILE_LOADER_ESCAPE, 3),
)


@dataclass(frozen=True, order=True)
class DependencyBoundaryViolation:
    path: str
    line: int
    imported_module: str
    import_kind: str
    root_module: str = ""
    dependency_chain: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ImportEdge:
    source: str
    target: str
    path: str
    line: int
    kind: str


@dataclass(frozen=True)
class _ModuleSource:
    name: str
    path: Path
    relative_path: str
    is_package: bool


def _matches_prefix(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes)


def _is_forbidden_runtime_loader_import(module: str) -> bool:
    return not _matches_prefix(module, DEFAULT_ALLOWED_RUNTIME_LOADER_PREFIXES) and _matches_prefix(
        module,
        DEFAULT_FORBIDDEN_RUNTIME_LOADER_PREFIXES,
    )


def _module_source(path: Path, repository_root: Path) -> _ModuleSource:
    relative = path.relative_to(repository_root)
    parts = relative.with_suffix("").parts
    is_package = parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    return _ModuleSource(
        name=".".join(parts),
        path=path,
        relative_path=relative.as_posix(),
        is_package=is_package,
    )


def _resolve_from_import(
    node: ast.ImportFrom,
    *,
    current_module: str,
    is_package: bool,
) -> tuple[str, ...]:
    if node.level == 0:
        base = tuple(part for part in (node.module or "").split(".") if part)
    else:
        module_parts = tuple(current_module.split("."))
        package = module_parts if is_package else module_parts[:-1]
        keep = len(package) - (node.level - 1)
        if keep < 0:
            return ()
        base = (*package[:keep], *((node.module or "").split(".")))
        base = tuple(part for part in base if part)
    candidates = {".".join(base)} if base else set()
    for alias in node.names:
        if alias.name != "*":
            candidates.add(".".join((*base, alias.name)))
    return tuple(sorted(candidates))


def _dynamic_import_call(
    node: ast.Call,
    *,
    builtin_import_names: set[str],
    builtins_modules: set[str],
    importlib_names: set[str],
    importlib_modules: set[str],
    pkgutil_names: set[str],
    pkgutil_modules: set[str],
    runpy_module_names: set[str],
    runpy_path_names: set[str],
    runpy_modules: set[str],
) -> tuple[str | None, bool]:
    function = _unwrap_conventional_callable_reference(node.func)
    is_builtin_import = (
        isinstance(function, ast.Name) and function.id in builtin_import_names
    ) or _module_attribute(
        function,
        modules=builtins_modules | importlib_modules,
        attributes={"__import__"},
    )
    is_importlib = (
        isinstance(function, ast.Name) and function.id in importlib_names
    ) or _module_attribute(
        function,
        modules=importlib_modules,
        attributes={"import_module"},
    )
    is_pkgutil = (
        isinstance(function, ast.Name) and function.id in pkgutil_names
    ) or _module_attribute(
        function,
        modules=pkgutil_modules,
        attributes={"resolve_name"},
    )
    is_runpy_module = (
        isinstance(function, ast.Name) and function.id in runpy_module_names
    ) or _module_attribute(
        function,
        modules=runpy_modules,
        attributes={"run_module"},
    )
    is_runpy_path = (
        isinstance(function, ast.Name) and function.id in runpy_path_names
    ) or _module_attribute(
        function,
        modules=runpy_modules,
        attributes={"run_path"},
    )
    loader_modules = builtins_modules | importlib_modules | pkgutil_modules | runpy_modules
    if _opaque_module_attribute(function, modules=loader_modules):
        return _DYNAMIC_ESCAPE, True
    if not (is_builtin_import or is_importlib or is_pkgutil or is_runpy_module or is_runpy_path):
        return None, False
    if is_runpy_path:
        return _DYNAMIC_ESCAPE, True
    target = _static_string(node.args[0]) if node.args else None
    if target is not None:
        if is_pkgutil:
            target = target.partition(":")[0]
        if target.startswith(".") or (
            is_builtin_import and _builtin_import_has_dynamic_context(node)
        ):
            return _DYNAMIC_ESCAPE, True
        return target, True
    return _DYNAMIC_ESCAPE, True


def _module_attribute(
    node: ast.expr,
    *,
    modules: set[str],
    attributes: set[str],
) -> bool:
    """Recognize ``module.attr`` and ``getattr(module, "attr")`` references."""

    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in modules
    ):
        return node.attr in attributes
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id in modules
    ):
        return _static_string(node.slice) in attributes
    return bool(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id in modules
        and _static_string(node.args[1]) in attributes
    )


def _static_string(node: ast.expr) -> str | None:
    """Evaluate only side-effect-free literal string concatenation."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string(node.left)
        right = _static_string(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _unwrap_conventional_callable_reference(node: ast.expr) -> ast.expr:
    """Normalize direct ``callable.__call__``/``getattr`` wrappers without evaluating code."""

    current = node
    # ASTs are acyclic, but a finite cap keeps this normalization as deliberately small as the
    # conventional source shape it audits.
    for _ in range(8):
        if isinstance(current, ast.Attribute) and current.attr == "__call__":
            current = current.value
            continue
        if (
            isinstance(current, ast.Call)
            and isinstance(current.func, ast.Name)
            and current.func.id == "getattr"
            and len(current.args) >= 2
            and _static_string(current.args[1]) == "__call__"
        ):
            current = current.args[0]
            continue
        break
    return current


def _opaque_module_attribute(node: ast.expr, *, modules: set[str]) -> bool:
    """Detect a loader attribute selected from runtime data rather than source literals."""

    opaque_getattr = bool(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id in modules
        and _static_string(node.args[1]) is None
    )
    opaque_subscript = bool(
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id in modules
        and _static_string(node.slice) is None
    )
    return opaque_getattr or opaque_subscript


def _is_module_cache_expression(
    node: ast.expr,
    *,
    sys_module_names: set[str],
    module_cache_names: set[str],
) -> bool:
    return bool(
        (isinstance(node, ast.Name) and node.id in module_cache_names)
        or (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in sys_module_names
            and node.attr == "modules"
        )
    )


def _module_cache_lookup(
    node: ast.Call | ast.Subscript,
    *,
    sys_module_names: set[str],
    module_cache_names: set[str],
) -> tuple[str | None, bool]:
    """Recognize conventional reads from ``sys.modules`` and its direct aliases."""

    target_node: ast.expr | None = None
    function = (
        _unwrap_conventional_callable_reference(node.func) if isinstance(node, ast.Call) else None
    )
    if (
        isinstance(node, ast.Call)
        and isinstance(function, ast.Attribute)
        and function.attr == "get"
        and _is_module_cache_expression(
            function.value,
            sys_module_names=sys_module_names,
            module_cache_names=module_cache_names,
        )
    ):
        target_node = node.args[0] if node.args else None
    elif isinstance(node, ast.Subscript) and _is_module_cache_expression(
        node.value,
        sys_module_names=sys_module_names,
        module_cache_names=module_cache_names,
    ):
        target_node = node.slice
    else:
        return None, False
    target = _static_string(target_node) if target_node is not None else None
    return (target if target is not None else _DYNAMIC_ESCAPE), True


def _is_importlib_util_expression(
    node: ast.expr,
    *,
    direct_modules: set[str],
    package_roots: set[str],
) -> bool:
    return bool(
        (isinstance(node, ast.Name) and node.id in direct_modules)
        or (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in package_roots
            and node.attr == "util"
        )
    )


def _file_loader_call(
    node: ast.Call,
    *,
    importlib_util_direct_modules: set[str],
    importlib_util_package_roots: set[str],
    spec_from_file_names: set[str],
    module_from_spec_names: set[str],
    exec_module_names: set[str],
) -> bool:
    function = _unwrap_conventional_callable_reference(node.func)
    loader_attributes = {"module_from_spec", "spec_from_file_location"}
    if (
        isinstance(function, ast.Call)
        and isinstance(function.func, ast.Name)
        and function.func.id == "getattr"
        and len(function.args) >= 2
        and _is_importlib_util_expression(
            function.args[0],
            direct_modules=importlib_util_direct_modules,
            package_roots=importlib_util_package_roots,
        )
    ):
        selected = _static_string(function.args[1])
        return selected is None or selected in loader_attributes
    if isinstance(function, ast.Name) and function.id in (
        spec_from_file_names | module_from_spec_names | exec_module_names
    ):
        return True
    if isinstance(function, ast.Attribute) and function.attr in loader_attributes:
        return _is_importlib_util_expression(
            function.value,
            direct_modules=importlib_util_direct_modules,
            package_roots=importlib_util_package_roots,
        )
    # Once a spec exists, the conventional execution shape is ``spec.loader.exec_module(module)``.
    # The receiver is intentionally not name-sensitive so simple spec/loader aliases cannot evade
    # the policy.
    if isinstance(function, ast.Attribute) and function.attr in {"exec_module", "load_module"}:
        return True
    if isinstance(function, ast.Name) and function.id == "getattr" and len(node.args) >= 2:
        selected = _static_string(node.args[1])
        if _is_importlib_util_expression(
            node.args[0],
            direct_modules=importlib_util_direct_modules,
            package_roots=importlib_util_package_roots,
        ):
            return selected is None or selected in loader_attributes
        return selected in {"exec_module", "load_module"} or (
            selected is None
            and isinstance(node.args[0], ast.Attribute)
            and node.args[0].attr == "loader"
        )
    return False


def _call_argument(node: ast.Call, *, position: int, keyword: str) -> ast.expr | None:
    if len(node.args) > position:
        return node.args[position]
    return next((item.value for item in node.keywords if item.arg == keyword), None)


def _is_empty_fromlist(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant) and node.value is None:
        return True
    return isinstance(node, (ast.List, ast.Set, ast.Tuple)) and not node.elts


def _builtin_import_has_dynamic_context(node: ast.Call) -> bool:
    """Fail closed when ``__import__`` can resolve a relative or implicit child module."""

    if any(item.arg is None for item in node.keywords):
        return True
    level = _call_argument(node, position=4, keyword="level")
    if level is not None and not (
        isinstance(level, ast.Constant)
        and isinstance(level.value, (bool, int))
        and level.value == 0
    ):
        return True
    fromlist = _call_argument(node, position=3, keyword="fromlist")
    return fromlist is not None and not _is_empty_fromlist(fromlist)


def _runtime_code_call(
    node: ast.Call,
    *,
    builtins_modules: set[str],
    runtime_names: set[str],
) -> bool:
    function = _unwrap_conventional_callable_reference(node.func)
    return (isinstance(function, ast.Name) and function.id in runtime_names) or _module_attribute(
        function,
        modules=builtins_modules,
        attributes={"compile", "eval", "exec"},
    )


def _filesystem_api_call(
    node: ast.Call,
    *,
    filesystem_modules: set[str],
    filesystem_names: set[str],
) -> bool:
    """Recognize direct/aliased access to the ambient built-in filesystem opener."""

    function = _unwrap_conventional_callable_reference(node.func)
    return (
        isinstance(function, ast.Name) and function.id in filesystem_names
    ) or _module_attribute(
        function,
        modules=filesystem_modules,
        attributes={"open"},
    )


def _parse_edges(source: _ModuleSource) -> tuple[_ImportEdge, ...]:
    tree = ast.parse(source.path.read_text(encoding="utf-8"), filename=str(source.path))
    builtin_import_names = {"__import__"}
    # CPython conventionally injects ``__builtins__`` as either the module or its dictionary.
    # Treat both attribute and subscript access as a builtins loader surface.
    builtins_modules: set[str] = {"__builtins__"}
    importlib_names: set[str] = set()
    importlib_modules: set[str] = set()
    importlib_util_direct_modules: set[str] = set()
    importlib_util_package_roots: set[str] = set()
    spec_from_file_names: set[str] = set()
    module_from_spec_names: set[str] = set()
    exec_module_names: set[str] = set()
    pkgutil_names: set[str] = set()
    pkgutil_modules: set[str] = set()
    runpy_module_names: set[str] = set()
    runpy_path_names: set[str] = set()
    runpy_modules: set[str] = set()
    sys_module_names: set[str] = set()
    module_cache_names: set[str] = set()
    runtime_names = {"compile", "eval", "exec"}
    filesystem_names = {"open"}
    io_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            importlib_modules.update(
                alias.asname or "importlib"
                for alias in node.names
                if alias.name == "importlib"
                or (alias.name.startswith("importlib.") and alias.asname is None)
            )
            importlib_util_package_roots.update(
                alias.asname or "importlib"
                for alias in node.names
                if alias.name == "importlib"
                or (alias.name == "importlib.util" and alias.asname is None)
            )
            importlib_util_direct_modules.update(
                alias.asname
                for alias in node.names
                if alias.name == "importlib.util" and alias.asname is not None
            )
            builtins_modules.update(
                alias.asname or alias.name for alias in node.names if alias.name == "builtins"
            )
            pkgutil_modules.update(
                alias.asname or alias.name for alias in node.names if alias.name == "pkgutil"
            )
            runpy_modules.update(
                alias.asname or alias.name for alias in node.names if alias.name == "runpy"
            )
            sys_module_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "sys"
            )
            io_modules.update(
                alias.asname or alias.name for alias in node.names if alias.name == "io"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
            importlib_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "import_module"
            )
            builtin_import_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "__import__"
            )
            importlib_util_direct_modules.update(
                alias.asname or alias.name for alias in node.names if alias.name == "util"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib.util":
            spec_from_file_names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "spec_from_file_location"
            )
            module_from_spec_names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "module_from_spec"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "builtins":
            builtin_import_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "__import__"
            )
            runtime_names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in {"compile", "eval", "exec"}
            )
            filesystem_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "open"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "io":
            filesystem_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "open"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "pkgutil":
            pkgutil_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "resolve_name"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "runpy":
            runpy_module_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "run_module"
            )
            runpy_path_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "run_path"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "sys":
            module_cache_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "modules"
            )
    # Follow simple local aliases such as ``load = importlib.import_module``.  More opaque runtime
    # construction is rejected by the non-literal/runtime-code policy rather than interpreted.
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            value = node.value
            aliases = {target.id for target in targets if isinstance(target, ast.Name)}
            if not aliases or value is None:
                continue
            value = _unwrap_conventional_callable_reference(value)
            is_builtin_loader = (
                isinstance(value, ast.Name) and value.id in builtin_import_names
            ) or _module_attribute(
                value,
                modules=builtins_modules | importlib_modules,
                attributes={"__import__"},
            )
            is_importlib_loader = (
                isinstance(value, ast.Name) and value.id in importlib_names
            ) or _module_attribute(
                value,
                modules=importlib_modules,
                attributes={"import_module"},
            )
            is_importlib_util_alias = _is_importlib_util_expression(
                value,
                direct_modules=importlib_util_direct_modules,
                package_roots=importlib_util_package_roots,
            )
            is_spec_from_file_loader = (
                isinstance(value, ast.Name) and value.id in spec_from_file_names
            ) or (
                isinstance(value, ast.Attribute)
                and value.attr == "spec_from_file_location"
                and _is_importlib_util_expression(
                    value.value,
                    direct_modules=importlib_util_direct_modules,
                    package_roots=importlib_util_package_roots,
                )
            )
            is_module_from_spec_loader = (
                isinstance(value, ast.Name) and value.id in module_from_spec_names
            ) or (
                isinstance(value, ast.Attribute)
                and value.attr == "module_from_spec"
                and _is_importlib_util_expression(
                    value.value,
                    direct_modules=importlib_util_direct_modules,
                    package_roots=importlib_util_package_roots,
                )
            )
            is_exec_module_loader = (
                isinstance(value, ast.Name) and value.id in exec_module_names
            ) or (isinstance(value, ast.Attribute) and value.attr == "exec_module")
            is_pkgutil_loader = (
                isinstance(value, ast.Name) and value.id in pkgutil_names
            ) or _module_attribute(
                value,
                modules=pkgutil_modules,
                attributes={"resolve_name"},
            )
            is_runpy_module_loader = (
                isinstance(value, ast.Name) and value.id in runpy_module_names
            ) or _module_attribute(
                value,
                modules=runpy_modules,
                attributes={"run_module"},
            )
            is_runpy_path_loader = (
                isinstance(value, ast.Name) and value.id in runpy_path_names
            ) or _module_attribute(
                value,
                modules=runpy_modules,
                attributes={"run_path"},
            )
            is_runtime_loader = (
                isinstance(value, ast.Name) and value.id in runtime_names
            ) or _module_attribute(
                value,
                modules=builtins_modules,
                attributes={"compile", "eval", "exec"},
            )
            is_filesystem_api = (
                isinstance(value, ast.Name) and value.id in filesystem_names
            ) or _module_attribute(
                value,
                modules=builtins_modules | io_modules,
                attributes={"open"},
            )
            is_importlib_alias = isinstance(value, ast.Name) and value.id in importlib_modules
            is_importlib_util_package_alias = (
                isinstance(value, ast.Name) and value.id in importlib_util_package_roots
            )
            is_builtins_alias = isinstance(value, ast.Name) and value.id in builtins_modules
            is_pkgutil_alias = isinstance(value, ast.Name) and value.id in pkgutil_modules
            is_runpy_alias = isinstance(value, ast.Name) and value.id in runpy_modules
            is_sys_alias = isinstance(value, ast.Name) and value.id in sys_module_names
            is_module_cache_alias = _is_module_cache_expression(
                value,
                sys_module_names=sys_module_names,
                module_cache_names=module_cache_names,
            )
            is_io_alias = isinstance(value, ast.Name) and value.id in io_modules
            if is_builtin_loader and not aliases <= builtin_import_names:
                builtin_import_names.update(aliases)
                changed = True
            if is_importlib_loader and not aliases <= importlib_names:
                importlib_names.update(aliases)
                changed = True
            if is_importlib_util_alias and not aliases <= importlib_util_direct_modules:
                importlib_util_direct_modules.update(aliases)
                changed = True
            if is_spec_from_file_loader and not aliases <= spec_from_file_names:
                spec_from_file_names.update(aliases)
                changed = True
            if is_module_from_spec_loader and not aliases <= module_from_spec_names:
                module_from_spec_names.update(aliases)
                changed = True
            if is_exec_module_loader and not aliases <= exec_module_names:
                exec_module_names.update(aliases)
                changed = True
            if is_pkgutil_loader and not aliases <= pkgutil_names:
                pkgutil_names.update(aliases)
                changed = True
            if is_runpy_module_loader and not aliases <= runpy_module_names:
                runpy_module_names.update(aliases)
                changed = True
            if is_runpy_path_loader and not aliases <= runpy_path_names:
                runpy_path_names.update(aliases)
                changed = True
            if is_runtime_loader and not aliases <= runtime_names:
                runtime_names.update(aliases)
                changed = True
            if is_filesystem_api and not aliases <= filesystem_names:
                filesystem_names.update(aliases)
                changed = True
            if is_importlib_alias and not aliases <= importlib_modules:
                importlib_modules.update(aliases)
                changed = True
            if is_importlib_util_package_alias and not aliases <= importlib_util_package_roots:
                importlib_util_package_roots.update(aliases)
                changed = True
            if is_builtins_alias and not aliases <= builtins_modules:
                builtins_modules.update(aliases)
                changed = True
            if is_pkgutil_alias and not aliases <= pkgutil_modules:
                pkgutil_modules.update(aliases)
                changed = True
            if is_runpy_alias and not aliases <= runpy_modules:
                runpy_modules.update(aliases)
                changed = True
            if is_sys_alias and not aliases <= sys_module_names:
                sys_module_names.update(aliases)
                changed = True
            if is_module_cache_alias and not aliases <= module_cache_names:
                module_cache_names.update(aliases)
                changed = True
            if is_io_alias and not aliases <= io_modules:
                io_modules.update(aliases)
                changed = True
    edges: list[_ImportEdge] = []
    for node in ast.walk(tree):
        targets: tuple[str, ...] = ()
        kind = ""
        if isinstance(node, ast.Import):
            targets = tuple(alias.name for alias in node.names)
            kind = "import"
        elif isinstance(node, ast.ImportFrom):
            targets = _resolve_from_import(
                node,
                current_module=source.name,
                is_package=source.is_package,
            )
            kind = "from"
        elif isinstance(node, ast.Call):
            is_dynamic_import = False
            is_file_loader = _file_loader_call(
                node,
                importlib_util_direct_modules=importlib_util_direct_modules,
                importlib_util_package_roots=importlib_util_package_roots,
                spec_from_file_names=spec_from_file_names,
                module_from_spec_names=module_from_spec_names,
                exec_module_names=exec_module_names,
            )
            target, is_module_cache_lookup = _module_cache_lookup(
                node,
                sys_module_names=sys_module_names,
                module_cache_names=module_cache_names,
            )
            if is_module_cache_lookup:
                targets = (target,) if target is not None else ()
                kind = "module-cache"
            elif is_file_loader:
                targets = (_FILE_LOADER_ESCAPE,)
                kind = "file-loader"
            else:
                target, is_dynamic_import = _dynamic_import_call(
                    node,
                    builtin_import_names=builtin_import_names,
                    builtins_modules=builtins_modules,
                    importlib_names=importlib_names,
                    importlib_modules=importlib_modules,
                    pkgutil_names=pkgutil_names,
                    pkgutil_modules=pkgutil_modules,
                    runpy_module_names=runpy_module_names,
                    runpy_path_names=runpy_path_names,
                    runpy_modules=runpy_modules,
                )
            if not (is_module_cache_lookup or is_file_loader) and is_dynamic_import:
                targets = (target,) if target is not None else ()
                kind = "dynamic"
            elif not (is_module_cache_lookup or is_file_loader) and _runtime_code_call(
                node,
                builtins_modules=builtins_modules,
                runtime_names=runtime_names,
            ):
                targets = (_RUNTIME_CODE_ESCAPE,)
                kind = "runtime-code"
            elif not (is_module_cache_lookup or is_file_loader) and _filesystem_api_call(
                node,
                filesystem_modules=builtins_modules | io_modules,
                filesystem_names=filesystem_names,
            ):
                targets = (_FILESYSTEM_API_ESCAPE,)
                kind = "filesystem-api"
            elif not (is_module_cache_lookup or is_file_loader) and _opaque_module_attribute(
                node,
                modules=builtins_modules | importlib_modules | pkgutil_modules | runpy_modules,
            ):
                targets = (_DYNAMIC_ESCAPE,)
                kind = "dynamic-loader"
        elif isinstance(node, ast.Subscript):
            target, is_module_cache_lookup = _module_cache_lookup(
                node,
                sys_module_names=sys_module_names,
                module_cache_names=module_cache_names,
            )
            if is_module_cache_lookup:
                targets = (target,) if target is not None else ()
                kind = "module-cache"
        for target in targets:
            edges.append(
                _ImportEdge(
                    source=source.name,
                    target=target,
                    path=source.relative_path,
                    line=node.lineno,
                    kind=kind,
                )
            )
    return tuple(edges)


def _load_internal_graph(
    repository_root: Path,
) -> tuple[dict[str, _ModuleSource], dict[str, tuple[_ImportEdge, ...]], tuple[_ImportEdge, ...]]:
    package_root = repository_root / "aletheia"
    sources: dict[str, _ModuleSource] = {}
    unsafe_edges: list[_ImportEdge] = []
    if package_root.is_symlink():
        unsafe_edges.append(_symlink_edge(package_root, repository_root))
        return sources, {}, tuple(unsafe_edges)
    for path in sorted(package_root.rglob("*")):
        if path.is_symlink():
            unsafe_edges.append(_symlink_edge(path, repository_root))
            continue
        if not path.is_file() or path.suffix != ".py":
            continue
        source = _module_source(path, repository_root)
        sources[source.name] = source

    graph: dict[str, tuple[_ImportEdge, ...]] = {}
    for module, source in sorted(sources.items()):
        graph[module] = _parse_edges(source)
    return sources, graph, tuple(unsafe_edges)


def _load_operational_graph(
    repository_root: Path,
) -> tuple[dict[str, _ModuleSource], dict[str, tuple[_ImportEdge, ...]], tuple[_ImportEdge, ...]]:
    """Load shipped Python entry points outside the package for the driver policy."""

    sources: dict[str, _ModuleSource] = {}
    unsafe_edges: list[_ImportEdge] = []
    for relative_root in DEFAULT_OPERATIONAL_PYTHON_ROOTS:
        operational_root = repository_root / relative_root
        if not operational_root.exists():
            continue
        if operational_root.is_symlink():
            unsafe_edges.append(_symlink_edge(operational_root, repository_root))
            continue
        for path in sorted(operational_root.rglob("*")):
            if path.is_symlink():
                unsafe_edges.append(_symlink_edge(path, repository_root))
                continue
            if not path.is_file() or path.suffix != ".py":
                continue
            source = _module_source(path, repository_root)
            sources[source.name] = source

    graph = {module: _parse_edges(source) for module, source in sorted(sources.items())}
    return sources, graph, tuple(unsafe_edges)


def _symlink_edge(path: Path, repository_root: Path) -> _ImportEdge:
    relative = path.relative_to(repository_root)
    parts = list(relative.parts)
    if path.name == "__init__.py":
        parts.pop()
    elif path.suffix:
        parts[-1] = path.stem
    return _ImportEdge(
        source=".".join(parts),
        target="<symlinked-source>",
        path=relative.as_posix(),
        line=1,
        kind="filesystem",
    )


def _internal_targets(edge: _ImportEdge, modules: set[str]) -> tuple[str, ...]:
    """Resolve an import candidate to concrete internal graph nodes.

    ``from aletheia.foo import bar`` yields both ``aletheia.foo`` and
    ``aletheia.foo.bar`` candidates.  Keeping every existing candidate is conservative and also
    follows re-exports through package ``__init__`` modules.
    """

    parts = edge.target.split(".")
    if not parts or parts[0] != "aletheia":
        return ()
    return tuple(
        candidate
        for index in range(1, len(parts) + 1)
        if (candidate := ".".join(parts[:index])) in modules
    )


def _import_initializers(module: str, modules: set[str]) -> tuple[str, ...]:
    """Return package initializers Python executes before importing ``module``."""

    parts = module.split(".")
    return tuple(
        candidate
        for index in range(1, len(parts))
        if (candidate := ".".join(parts[:index])) in modules
    )


def _violation(
    edge: _ImportEdge, *, root: str, chain: tuple[str, ...]
) -> DependencyBoundaryViolation:
    return DependencyBoundaryViolation(
        path=edge.path,
        line=edge.line,
        imported_module=edge.target,
        import_kind=edge.kind,
        root_module=root,
        dependency_chain=chain,
    )


def _find_read_only_compatibility_leaf_violations(
    *,
    sources: dict[str, _ModuleSource],
    graph: dict[str, tuple[_ImportEdge, ...]],
    leaf_modules: Iterable[str],
) -> tuple[DependencyBoundaryViolation, ...]:
    """Keep temporary legacy binders source-leaf-only and out of package initializers."""

    module_names = set(sources)
    forbidden_leaf_dependencies = tuple(
        sorted(
            {
                "aletheia",
                *DEFAULT_FORBIDDEN_RUNTIME_LOADER_PREFIXES,
                *DEFAULT_PURE_CONTRACT_FORBIDDEN_MODULE_PREFIXES,
            }
        )
    )
    escape_targets = {
        _DYNAMIC_ESCAPE,
        _FILE_LOADER_ESCAPE,
        _RUNTIME_CODE_ESCAPE,
        _FILESYSTEM_API_ESCAPE,
    }
    violations: list[DependencyBoundaryViolation] = []
    for leaf in sorted(set(leaf_modules)):
        # PR-0/PR-1 fixture repositories legitimately predate this adapter.  PR-3's separate
        # non-vacuity canary requires the real leaf; if present, its policy is mandatory here.
        if leaf not in sources:
            continue
        for edge in graph.get(leaf, ()):
            if edge.target in escape_targets or _matches_prefix(
                edge.target, forbidden_leaf_dependencies
            ):
                violations.append(_violation(edge, root=leaf, chain=(leaf, edge.target)))

        parent_package = leaf.rpartition(".")[0]
        queue: deque[tuple[str, tuple[str, ...]]] = deque([(parent_package, (parent_package,))])
        visited: set[str] = set()
        while queue:
            module, chain = queue.popleft()
            if module in visited:
                continue
            visited.add(module)
            for edge in graph.get(module, ()):
                next_chain = (*chain, edge.target)
                if edge.target in {
                    _DYNAMIC_ESCAPE,
                    _FILE_LOADER_ESCAPE,
                    _RUNTIME_CODE_ESCAPE,
                }:
                    violations.append(_violation(edge, root=parent_package, chain=next_chain))
                    continue
                internal_targets = _internal_targets(edge, module_names)
                if leaf in internal_targets:
                    violations.append(_violation(edge, root=parent_package, chain=next_chain))
                    continue
                for target in internal_targets:
                    if target not in visited:
                        queue.append((target, (*chain, target)))
    return tuple(sorted(set(violations)))


def find_dependency_boundary_violations(
    repository_root: Path,
    *,
    target_packages: Iterable[str] = DEFAULT_TARGET_PACKAGES,
    required_target_packages: Iterable[str] = DEFAULT_REQUIRED_TARGET_PACKAGES,
    forbidden_module_prefixes: Iterable[str] = DEFAULT_FORBIDDEN_MODULE_PREFIXES,
    strict_target_packages: Iterable[str] = DEFAULT_STRICT_TARGET_PACKAGES,
    strict_forbidden_module_prefixes: Iterable[str] = DEFAULT_STRICT_FORBIDDEN_MODULE_PREFIXES,
    pure_contract_target_packages: Iterable[str] = DEFAULT_PURE_CONTRACT_TARGET_PACKAGES,
    pure_contract_target_modules: Iterable[str] = DEFAULT_PURE_CONTRACT_TARGET_MODULES,
    pure_contract_forbidden_module_prefixes: Iterable[
        str
    ] = DEFAULT_PURE_CONTRACT_FORBIDDEN_MODULE_PREFIXES,
    read_only_compatibility_leaf_modules: Iterable[
        str
    ] = DEFAULT_READ_ONLY_COMPATIBILITY_LEAF_MODULES,
    adapter_target_packages: Iterable[str] = DEFAULT_ADAPTER_TARGET_PACKAGES,
    adapter_forbidden_module_prefixes: Iterable[str] = DEFAULT_ADAPTER_FORBIDDEN_MODULE_PREFIXES,
) -> tuple[DependencyBoundaryViolation, ...]:
    """Return direct and transitive protected-package violations in canonical order."""

    root_path = repository_root.resolve(strict=True)
    targets = tuple(sorted(set(target_packages)))
    required = tuple(sorted(set(required_target_packages)))
    common_forbidden = tuple(sorted(set(forbidden_module_prefixes)))
    strict_targets = set(strict_target_packages)
    strict_forbidden = tuple(sorted(set(strict_forbidden_module_prefixes)))
    pure_contract_packages = set(pure_contract_target_packages)
    pure_contract_modules = set(pure_contract_target_modules)
    pure_contract_forbidden = tuple(sorted(set(pure_contract_forbidden_module_prefixes)))
    adapter_targets = set(adapter_target_packages)
    adapter_forbidden = tuple(sorted(set(adapter_forbidden_module_prefixes)))
    sources, graph, unsafe_edges = _load_internal_graph(root_path)
    module_names = set(sources) | {edge.source for edge in unsafe_edges}
    violations = [
        _violation(edge, root=edge.source, chain=(edge.source, edge.target))
        for edge in unsafe_edges
    ]
    violations.extend(
        _find_read_only_compatibility_leaf_violations(
            sources=sources,
            graph=graph,
            leaf_modules=read_only_compatibility_leaf_modules,
        )
    )

    for package in required:
        root_module = f"aletheia.{package}"
        if root_module not in module_names:
            violations.append(
                DependencyBoundaryViolation(
                    path=f"aletheia/{package}",
                    line=1,
                    imported_module="<missing-protected-package>",
                    import_kind="filesystem",
                    root_module=root_module,
                    dependency_chain=(root_module,),
                )
            )

    unsafe_by_source: dict[str, list[_ImportEdge]] = {}
    for edge in unsafe_edges:
        unsafe_by_source.setdefault(edge.source, []).append(edge)

    for package in targets:
        package_prefix = f"aletheia.{package}"
        roots = sorted(
            module
            for module in module_names
            if module == package_prefix or module.startswith(f"{package_prefix}.")
        )
        forbidden = (
            common_forbidden
            + (strict_forbidden if package in strict_targets else ())
            + (adapter_forbidden if package in adapter_targets else ())
        )
        for root_module in roots:
            is_pure_contract = (
                package in pure_contract_packages or root_module in pure_contract_modules
            )
            root_forbidden = forbidden + (pure_contract_forbidden if is_pure_contract else ())
            escape_targets = {
                _DYNAMIC_ESCAPE,
                _FILE_LOADER_ESCAPE,
                _RUNTIME_CODE_ESCAPE,
                "<symlinked-source>",
            }
            if is_pure_contract:
                escape_targets.add(_FILESYSTEM_API_ESCAPE)
            initial_modules = (*_import_initializers(root_module, module_names), root_module)
            queue: deque[tuple[str, tuple[str, ...]]] = deque(
                (module, (root_module,) if module == root_module else (root_module, module))
                for module in initial_modules
            )
            visited: set[str] = set()
            while queue:
                module, chain = queue.popleft()
                if module in visited:
                    continue
                visited.add(module)
                for edge in (*graph.get(module, ()), *unsafe_by_source.get(module, ())):
                    next_chain = (*chain, edge.target)
                    if edge.target in escape_targets:
                        violations.append(_violation(edge, root=root_module, chain=next_chain))
                        continue
                    if _matches_prefix(edge.target, root_forbidden) and not _matches_prefix(
                        edge.target,
                        DEFAULT_ALLOWED_RUNTIME_LOADER_PREFIXES,
                    ):
                        violations.append(_violation(edge, root=root_module, chain=next_chain))
                        continue
                    for target in _internal_targets(edge, module_names):
                        if target not in visited:
                            queue.append((target, (*chain, target)))
    return tuple(sorted(set(violations)))


def find_legacy_driver_import_violations(
    repository_root: Path,
    *,
    allowed_importers: Iterable[str] = DEFAULT_LEGACY_DRIVER_IMPORTERS,
    target_packages: Iterable[str] = DEFAULT_TARGET_PACKAGES,
) -> tuple[DependencyBoundaryViolation, ...]:
    """Freeze the legacy driver to one production entry point and prevent reverse coupling."""

    root_path = repository_root.resolve(strict=True)
    sources, graph, unsafe_edges = _load_internal_graph(root_path)
    _operational_sources, operational_graph, operational_unsafe_edges = _load_operational_graph(
        root_path
    )
    allowed = set(allowed_importers)
    audited_dynamic_loaders = dict(DEFAULT_AUDITED_DYNAMIC_LOADER_SOURCES)
    audited_escape_contract = {
        (module, escape): count
        for module, escape, count in DEFAULT_AUDITED_DYNAMIC_LOADER_ESCAPE_COUNTS
    }
    protected = tuple(f"aletheia.{package}" for package in sorted(set(target_packages)))
    driver = "aletheia.scheduler.driver"
    violations = [
        _violation(edge, root=edge.source, chain=(edge.source, edge.target))
        for edge in (*unsafe_edges, *operational_unsafe_edges)
    ]
    actual_importers: set[str] = set()
    audited_escape_counts: dict[tuple[str, str], int] = {}
    for module, edges in graph.items():
        source = sources[module]
        source_sha256 = hashlib.sha256(source.path.read_bytes()).hexdigest()
        is_pinned_dynamic_loader = audited_dynamic_loaders.get(module) == source_sha256
        for edge in edges:
            if _is_forbidden_runtime_loader_import(edge.target):
                if not is_pinned_dynamic_loader:
                    violations.append(_violation(edge, root=module, chain=(module, edge.target)))
                continue
            if edge.target in {
                _DYNAMIC_ESCAPE,
                _FILE_LOADER_ESCAPE,
                _RUNTIME_CODE_ESCAPE,
            }:
                if (
                    edge.target in {_DYNAMIC_ESCAPE, _FILE_LOADER_ESCAPE}
                    and is_pinned_dynamic_loader
                ):
                    key = (module, edge.target)
                    audited_escape_counts[key] = audited_escape_counts.get(key, 0) + 1
                    continue
                violations.append(_violation(edge, root=module, chain=(module, edge.target)))
                continue
            if _matches_prefix(edge.target, (driver,)) and module != driver:
                actual_importers.add(module)
                if module not in allowed:
                    violations.append(_violation(edge, root=module, chain=(module, edge.target)))

    # Operational entry points may call the explicit guarded loading seam or the inline
    # compatibility seam in ``durable``, but every unparsed loader/runtime-code escape fails
    # closed. Literal imports, static string concatenation, runpy, and importlib getattr calls are
    # normalized by ``_parse_edges``.
    for module, edges in operational_graph.items():
        for edge in edges:
            if (
                _is_forbidden_runtime_loader_import(edge.target)
                or edge.target
                in {
                    _DYNAMIC_ESCAPE,
                    _FILE_LOADER_ESCAPE,
                    _RUNTIME_CODE_ESCAPE,
                }
                or _matches_prefix(edge.target, (driver,))
            ):
                violations.append(_violation(edge, root=module, chain=(module, edge.target)))

    # The audited loader exception is non-vacuous and admits only its pinned import/file-loader
    # call counts.  A missing file, source drift, or an added escape makes the boundary fail.
    for module, expected_sha256 in sorted(audited_dynamic_loaders.items()):
        source = sources.get(module)
        if source is None:
            violations.append(
                DependencyBoundaryViolation(
                    path=module.replace(".", "/") + ".py",
                    line=1,
                    imported_module="<missing-audited-dynamic-loader>",
                    import_kind="filesystem",
                    root_module=module,
                    dependency_chain=(module,),
                )
            )
            continue
        actual_sha256 = hashlib.sha256(source.path.read_bytes()).hexdigest()
        if actual_sha256 != expected_sha256:
            violations.append(
                DependencyBoundaryViolation(
                    path=source.relative_path,
                    line=1,
                    imported_module="<audited-dynamic-loader-source-mismatch>",
                    import_kind="policy",
                    root_module=module,
                    dependency_chain=(module,),
                )
            )
        module_contract = {
            escape: count
            for (contract_module, escape), count in audited_escape_contract.items()
            if contract_module == module
        }
        actual_contract = {
            escape: count
            for (actual_module, escape), count in audited_escape_counts.items()
            if actual_module == module
        }
        if actual_contract != module_contract:
            violations.append(
                DependencyBoundaryViolation(
                    path=source.relative_path,
                    line=1,
                    imported_module="<audited-dynamic-loader-contract-mismatch>",
                    import_kind="policy",
                    root_module=module,
                    dependency_chain=(module, "<audited-dynamic-loader-contract>"),
                )
            )

    # Follow the driver's complete internal dependency closure so a helper cannot smuggle the new
    # authority back into the legacy controller.
    module_names = set(sources) | {edge.source for edge in unsafe_edges}
    if driver in module_names:
        unsafe_by_source: dict[str, list[_ImportEdge]] = {}
        for edge in unsafe_edges:
            unsafe_by_source.setdefault(edge.source, []).append(edge)
        initial_modules = (*_import_initializers(driver, module_names), driver)
        queue: deque[tuple[str, tuple[str, ...]]] = deque(
            (module, (driver,) if module == driver else (driver, module))
            for module in initial_modules
        )
        visited: set[str] = set()
        while queue:
            module, chain = queue.popleft()
            if module in visited:
                continue
            visited.add(module)
            for edge in (*graph.get(module, ()), *unsafe_by_source.get(module, ())):
                next_chain = (*chain, edge.target)
                if _matches_prefix(edge.target, protected) or edge.target == "<symlinked-source>":
                    violations.append(_violation(edge, root=driver, chain=next_chain))
                    continue
                for target in _internal_targets(edge, module_names):
                    if target not in visited:
                        queue.append((target, (*chain, target)))
    # An allowlisted name must resolve to a real source; otherwise deleting the durable worker could
    # silently turn this into a no-importer policy.
    for module in sorted(allowed):
        if module not in sources:
            violations.append(
                DependencyBoundaryViolation(
                    path=module.replace(".", "/") + ".py",
                    line=1,
                    imported_module="<missing-legacy-driver-entrypoint>",
                    import_kind="filesystem",
                    root_module=module,
                    dependency_chain=(module,),
                )
            )
        elif module not in actual_importers:
            violations.append(
                DependencyBoundaryViolation(
                    path=sources[module].relative_path,
                    line=1,
                    imported_module="<missing-legacy-driver-import>",
                    import_kind="policy",
                    root_module=module,
                    dependency_chain=(module, driver),
                )
            )
    return tuple(sorted(set(violations)))


def find_research_store_persistence_import_violations(
    repository_root: Path,
    *,
    allowed_importers: Iterable[str] = DEFAULT_RESEARCH_STORE_PERSISTENCE_IMPORTERS,
) -> tuple[DependencyBoundaryViolation, ...]:
    """Keep authoritative ORM records private to the one command transaction adapter.

    Schema registration is intentionally read-only and explicitly enumerated.  Any other
    production import would create a second application-level route around
    ``ResearchKernelStore.commit``.
    """

    root_path = repository_root.resolve(strict=True)
    sources, graph, _unsafe_edges = _load_internal_graph(root_path)
    operational_sources, operational_graph, _operational_unsafe = _load_operational_graph(root_path)
    allowed = set(allowed_importers)
    persistence = "aletheia.research_store.persistence"
    violations: list[DependencyBoundaryViolation] = []
    actual_importers: set[str] = set()

    for module, edges in (*graph.items(), *operational_graph.items()):
        for edge in edges:
            if not _matches_prefix(edge.target, (persistence,)) or module == persistence:
                continue
            actual_importers.add(module)
            if module not in allowed:
                violations.append(_violation(edge, root=module, chain=(module, edge.target)))

    all_sources = {**sources, **operational_sources}
    public_store_module = "aletheia.research_store.store"
    private_symbols = set(DEFAULT_PRIVATE_RESEARCH_STORE_RECORD_SYMBOLS)
    private_store_bindings = private_symbols | {f"_{symbol}" for symbol in private_symbols}

    def dotted_name(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = dotted_name(node.value)
            return f"{prefix}.{node.attr}" if prefix is not None else None
        return None

    def private_symbol_violation(
        *,
        source: _ModuleSource,
        module: str,
        node: ast.AST,
        symbol: str,
        kind: str = "private-authority-symbol",
    ) -> DependencyBoundaryViolation:
        return DependencyBoundaryViolation(
            path=source.relative_path,
            line=getattr(node, "lineno", 1),
            imported_module=f"{public_store_module}:{symbol}",
            import_kind=kind,
            root_module=module,
            dependency_chain=(module, public_store_module, symbol),
        )

    # The adapter itself may import persistence records, but only under private names.  Otherwise
    # ``store.ResearchKernelEventRecord`` silently becomes a second public ORM writer surface.
    store_source = all_sources.get(public_store_module)
    if store_source is not None:
        store_tree = ast.parse(
            store_source.path.read_text(encoding="utf-8"),
            filename=str(store_source.path),
        )
        for node in ast.walk(store_tree):
            if isinstance(node, ast.ImportFrom) and node.module == persistence:
                for alias in node.names:
                    if alias.name not in private_symbols:
                        continue
                    if alias.asname is None or not alias.asname.startswith("_"):
                        violations.append(
                            private_symbol_violation(
                                source=store_source,
                                module=public_store_module,
                                node=node,
                                symbol=alias.name,
                                kind="public-authority-symbol",
                            )
                        )
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                targets: tuple[ast.AST, ...]
                if isinstance(node, ast.Assign):
                    targets = tuple(node.targets)
                else:
                    targets = (node.target,)
                for target in targets:
                    if isinstance(target, ast.Name) and target.id in private_symbols:
                        violations.append(
                            private_symbol_violation(
                                source=store_source,
                                module=public_store_module,
                                node=node,
                                symbol=target.id,
                                kind="public-authority-symbol",
                            )
                        )

    for module, source in sorted(all_sources.items()):
        if module == public_store_module:
            continue
        tree = ast.parse(source.path.read_text(encoding="utf-8"), filename=str(source.path))

        store_module_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == public_store_module:
                        store_module_names.add(alias.asname or public_store_module)
            elif isinstance(node, ast.ImportFrom) and node.module == "aletheia.research_store":
                for alias in node.names:
                    if alias.name == "store":
                        store_module_names.add(alias.asname or alias.name)

        # Resolve conventional local aliases such as ``adapter = store``.  This is deliberately
        # finite and side-effect-free; dynamic namespace tricks remain forbidden by the broader
        # runtime-loader boundary.
        changed = True
        while changed:
            changed = False
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                    continue
                value = node.value
                if dotted_name(value) not in store_module_names:
                    continue
                targets = tuple(node.targets) if isinstance(node, ast.Assign) else (node.target,)
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in store_module_names:
                        store_module_names.add(target.id)
                        changed = True

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == public_store_module:
                for alias in node.names:
                    if alias.name in private_store_bindings or alias.name == "*":
                        violations.append(
                            private_symbol_violation(
                                source=source,
                                module=module,
                                node=node,
                                symbol=alias.name,
                            )
                        )
                continue
            if (
                isinstance(node, ast.Attribute)
                and dotted_name(node.value) in store_module_names
                and node.attr in private_store_bindings
            ):
                violations.append(
                    private_symbol_violation(
                        source=source,
                        module=module,
                        node=node,
                        symbol=node.attr,
                    )
                )
                continue
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and node.args
                and dotted_name(node.args[0]) in store_module_names
            ):
                symbol = _static_string(node.args[1]) if len(node.args) >= 2 else None
                if symbol is None or symbol in private_store_bindings:
                    violations.append(
                        private_symbol_violation(
                            source=source,
                            module=module,
                            node=node,
                            symbol=symbol or "<dynamic-attribute>",
                        )
                    )
    for missing in sorted(allowed - actual_importers):
        source = all_sources.get(missing)
        violations.append(
            DependencyBoundaryViolation(
                path=(
                    source.relative_path
                    if source is not None
                    else missing.replace(".", "/") + ".py"
                ),
                line=1,
                imported_module="<missing-authoritative-store-importer>",
                import_kind="policy",
                root_module=missing,
                dependency_chain=(missing, persistence),
            )
        )
    return tuple(sorted(set(violations)))
