"""Static safety gate + resource limits for AI-authored model code.

``check_code`` walks the AST and rejects anything outside a tight allowlist
(imports, dangerous builtins, dunder-escape patterns). ``resource_limits``
returns a ``preexec_fn`` that caps CPU + address space for the training
subprocess. These are guardrails against runaway/accidental code on a personal
machine — NOT a hard sandbox against a determined adversary (that is Docker).
The code is authored by the configured orchestrator; this catches mistakes
and obvious foot-guns and keeps the surface honest.
"""

from __future__ import annotations

import ast

# Module roots the solution may import. Enough to build sklearn pipelines /
# feature transforms; deliberately excludes io / net / process / serialization.
ALLOWED_IMPORT_ROOTS = {
    "__future__",  # compile-time directive (``from __future__ import annotations``), not io
    "sklearn",
    "numpy",
    "np",
    "scipy",
    "pandas",
    "math",
    "statistics",
    "typing",
    "dataclasses",
    "functools",
    "itertools",
    "collections",
    "warnings",
    "random",
    # SOTA model frameworks (B-2) — fit/predict estimators only; the eval harness
    # still computes the metrics (the coder never grades its own homework).
    "xgboost",
    "lightgbm",
    "torch",
    "skorch",
}

# Names that must never be called/used in solution code.
FORBIDDEN_NAMES = {
    "eval", "exec", "compile", "__import__", "open", "input",
    "exit", "quit", "breakpoint", "globals", "locals", "vars",
    "memoryview", "help",
}

# Attribute names that enable sandbox escapes via introspection.
FORBIDDEN_ATTRS = {
    "__globals__", "__builtins__", "__subclasses__", "__bases__", "__mro__",
    "__class__", "__dict__", "__code__", "__closure__", "__import__",
    "__getattribute__", "__reduce__", "__reduce_ex__",
}

REQUIRED_FUNCTION = "build_pipeline"
# The AI-authored DEMONSTRATION contract (the frontier path): the AI writes the
# discriminating computation, not a model. Same allowlist/forbidden sets — it needs only
# numpy/scipy/sklearn/math/statistics, and is denied io/net/process exactly as solutions are.
DEMO_REQUIRED_FUNCTION = "compute_demonstration"
# The AI-authored EXPLORATION probe (K1, the explore->confirm seal): the AI looks at a DISJOINT
# exploration subset and returns DESCRIPTIVE observations only (never a verdict) so it can calibrate
# the pre-registration before the harness confirms on held-out data. Same allowlist/forbidden sets.
EXPLORE_REQUIRED_FUNCTION = "explore_demonstration"


def _import_root(name: str) -> str:
    return (name or "").split(".")[0]


def check_code(source: str, *, required_function: str = REQUIRED_FUNCTION) -> tuple[bool, list[str]]:
    """Return (ok, reasons). ok=True means the code passes the static gate and defines
    ``required_function`` (``build_pipeline`` for solutions, ``compute_demonstration`` for
    AI-authored demonstrations). The allowlist/forbidden sets are identical for both."""
    reasons: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return False, [f"syntax error: {exc}"]

    defines_required = False

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == required_function:
                defines_required = True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if _import_root(alias.name) not in ALLOWED_IMPORT_ROOTS:
                    reasons.append(f"forbidden import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if _import_root(node.module or "") not in ALLOWED_IMPORT_ROOTS:
                reasons.append(f"forbidden import: from {node.module}")
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_ATTRS:
                reasons.append(f"forbidden attribute access: {node.attr}")
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES:
                reasons.append(f"forbidden name: {node.id}")

    if not defines_required:
        reasons.append(f"must define a `{required_function}()` function")
    return (len(reasons) == 0), reasons


def resource_limits():
    """A ``preexec_fn`` for subprocess.Popen that caps CPU time and address space.
    Returns None on platforms without ``resource`` (caller skips it)."""
    try:
        import resource
    except ImportError:  # pragma: no cover - non-POSIX
        return None

    from aletheia.config import get_settings

    s = get_settings()
    cpu = int(s.sandbox_cpu_seconds)
    mem_bytes = int(s.sandbox_max_memory_mb) * 1024 * 1024

    def _apply():  # runs in the child, after fork, before exec
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        try:
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        except (ValueError, OSError):
            pass  # RLIMIT_AS is unreliable on macOS; CPU + wall-clock still apply

    return _apply


def smoke_test_solution(
    source: str, timeout_s: float = 30.0, *, backend: str | None = None
) -> tuple[bool, str]:
    """Beyond the static AST gate: actually IMPORT the solution + build the pipeline in
    an isolated subprocess. Catches a runtime import error (e.g. a real symbol imported
    from the WRONG module — ``VarianceThreshold`` from ``sklearn.preprocessing``) or a
    build error BEFORE a full training run, so a coder slip degrades to the fixed model
    instead of crashing the whole experiment. Returns ``(ok, error)``.

    Safe enough: ``source`` already passed ``check_code`` (allowlisted imports, no
    os/subprocess/open/exec), and ``build_pipeline`` only CONSTRUCTS estimators here."""
    from aletheia.coder.executor import execute_python_files

    probe = (
        "import importlib.util as u\n"
        "s=u.spec_from_file_location('sol','solution.py')\n"
        "m=u.module_from_spec(s);s.loader.exec_module(m)\n"
        "p=m.build_pipeline()\n"
        "assert hasattr(p,'fit') and hasattr(p,'predict'),"
        "'build_pipeline must return an estimator'\n"
        "print('ALETHEIA_SMOKE_OK')\n"
    )
    result = execute_python_files(
        {"solution.py": source, "runner.py": probe}, timeout_s=timeout_s, backend=backend
    )
    if not result.ok or "ALETHEIA_SMOKE_OK" not in result.output:
        err = (result.error or result.output or "non-zero exit").strip()
        return False, err.splitlines()[-1][:200] if err else "non-zero exit"
    return True, ""


def smoke_test_demonstration(
    source: str, timeout_s: float = 30.0, sample=None, *, backend: str | None = None
) -> tuple[bool, str]:
    """Beyond the static gate: IMPORT the AI-authored demonstration and CALL
    ``compute_demonstration`` on a small data frame in an isolated subprocess, asserting it
    returns a dict with FINITE ``test_statistic``/``control_statistic`` AND a NON-DEGENERATE
    shape (``n_test``/``n_control`` present, whole, > 0, and <= the probe row count). Catches a
    runtime import/shape error — and the recurring *0-sample / broken-selection* design bug —
    BEFORE the real run, so the authoring loop gets an informed retry instead of burning the
    whole round at confirm time (where the degeneracy is only caught by ``_demonstration_probes``
    after the loop is over). Returns ``(ok, error)``.

    ``sample`` = an optional ``(X, y, groups)`` slice of the REAL data. PASS IT whenever you have
    the data: the synthetic fallback (200x8 random floats, INTEGER groups) contradicts a domain's
    actual contract — e.g. materials candidates are told ``groups`` are chemical-system STRINGS
    ('As-Ga') and parse them (``group.split('-')``), so on integer groups they raise AttributeError
    or select nothing (degenerate) and get FALSE-KILLED here before ever reaching real data. A real
    slice gives the true feature dims + group dtype, so only genuinely-broken code is rejected.

    Scope note (anti-fakeability): this is a RUNNABILITY + shape check only. It NEVER judges
    whether the statistic is large enough, whether the control is silent, or whether the effect
    is real — a genuine NULL (adequate n, small statistic) passes here and is then honestly
    refuted by the seals/probes on the held-out confirm split. We only reject what is
    unambiguously BROKEN (empty/degenerate sample, impossible count, non-finite output).

    Safe: ``source`` already passed ``check_code`` (allowlisted imports, no os/open/exec); here
    it only runs on a small matrix in a subprocess. Same CPU/memory rlimits as training."""
    import io

    import numpy as _np

    from aletheia.coder.executor import execute_python_files

    payload: dict[str, str | bytes] = {"demo.py": source}
    if sample is not None:
        Xs, ys, gs = sample
        buf = io.BytesIO()
        _np.savez(
            buf,
            X=_np.asarray(Xs, dtype=float),
            y=_np.asarray(ys, dtype=float),
            g=_np.asarray(gs, dtype=str),
        )
        payload["probe.npz"] = buf.getvalue()
        data_setup = (
            "d=np.load('probe.npz',allow_pickle=True)\n"
            "X=d['X'];y=d['y'];g=d['g'];N=len(y)\n"
        )
    else:
        data_setup = (
            "rng=np.random.default_rng(0);N=200\n"
            "X=rng.random((N,8));y=rng.random(N)\n"
            "g=np.array([i%10 for i in range(N)],dtype=object)\n"
        )
    probe = (
        "import importlib.util as u, numpy as np, math\n"
        "s=u.spec_from_file_location('demo','demo.py')\n"
        "m=u.module_from_spec(s);s.loader.exec_module(m)\n"
        + data_setup
        + "r=m.compute_demonstration(X,y,g,{'random_state':0,'preregistration':{},"
          "'family_alpha':0.05})\n"
        "assert isinstance(r,dict),'compute_demonstration must return a dict'\n"
        "ts=float(r['test_statistic']);cs=float(r['control_statistic'])\n"
        "assert math.isfinite(ts) and math.isfinite(cs),'statistics must be finite'\n"
        "nt=r.get('n_test');nc=r.get('n_control')\n"
        "assert nt is not None and nc is not None,'must return n_test and n_control sample sizes'\n"
        "nt=float(nt);nc=float(nc)\n"
        "assert nt.is_integer() and nc.is_integer(),'n_test/n_control must be whole sample counts'\n"
        "assert nt>0 and nc>0,('degenerate sample (n_test=%d, n_control=%d): a 0-sample test or "
        "control means the selection is broken -- ensure BOTH conditions retain samples'%(nt,nc))\n"
        "assert nt<=N and nc<=N,('n_test=%d/n_control=%d exceed the %d available rows -- the "
        "selection double-counts or leaks samples'%(nt,nc,N))\n"
        "print('ALETHEIA_SMOKE_OK')\n"
    )
    payload["runner.py"] = probe
    result = execute_python_files(payload, timeout_s=timeout_s, backend=backend)
    if not result.ok or "ALETHEIA_SMOKE_OK" not in result.output:
        err = (result.error or result.output or "non-zero exit").strip()
        return False, err.splitlines()[-1][:200] if err else "non-zero exit"
    return True, ""
