"""Run an AI-authored ``compute_demonstration`` on host-staged arrays in an isolated,
resource-limited subprocess — the same trust split the Docker backend uses (host featurizes;
sandbox computes on staged numpy arrays). The AI code never touches data/files/network: it
receives ``X``/``y``/``groups`` via a temp ``.npz`` and returns a JSON result on stdout.

This is best-effort and FAIL-CLOSED: any failure (timeout, non-zero exit, unparseable output,
missing keys, non-finite statistics) returns ``None``, which the caller maps to
``holds=None`` (not_evaluated) — never a crash, never a trusted-but-wrong result.
"""

from __future__ import annotations

from typing import Any

# The in-subprocess probe: load staged arrays + meta, import the AI module, call
# compute_demonstration, validate the shape, print the result as JSON on a sentinel line.
_RUNNER_SCRIPT = """\
import json, math, sys
import numpy as np
import importlib.util as u

_d = np.load("staged.npz", allow_pickle=True)
X = _d["X"]; y = _d["y"]
groups = _d["groups"] if "groups" in _d and _d["groups"].ndim and _d["groups"].size else None
with open("meta.json") as _f:
    meta = json.load(_f)

s = u.spec_from_file_location("demo", "demo.py")
m = u.module_from_spec(s); s.loader.exec_module(m)
r = m.compute_demonstration(X, y, groups, meta)
if not isinstance(r, dict):
    print("ALETHEIA_DEMO_ERR not-a-dict", file=sys.stderr); sys.exit(2)
ts = float(r["test_statistic"]); cs = float(r["control_statistic"])
if not (math.isfinite(ts) and math.isfinite(cs)):
    print("ALETHEIA_DEMO_ERR non-finite", file=sys.stderr); sys.exit(3)
out = {
    "test_statistic": ts,
    "control_statistic": cs,
    "components": r.get("components") if isinstance(r.get("components"), dict) else {},
    "detail": str(r.get("detail", "")),
    "n_test": int(r.get("n_test", 0)),
    "n_control": int(r.get("n_control", 0)),
}
print("ALETHEIA_DEMO_OK " + json.dumps(out))
"""

# The EXPLORATION probe (K1 explore->confirm seal): call ``explore_demonstration`` and return
# DESCRIPTIVE observations only. FAIL CLOSED if the AI smuggles a verdict field (a `holds` /
# `test_statistic` / decision key, at top level OR inside observations) — the exploration step is
# never allowed to influence the verdict.
_EXPLORE_RUNNER_SCRIPT = """\
import json, math, sys
import numpy as np
import importlib.util as u

_d = np.load("staged.npz", allow_pickle=True)
X = _d["X"]; y = _d["y"]
groups = _d["groups"] if "groups" in _d and _d["groups"].ndim and _d["groups"].size else None
with open("meta.json") as _f:
    meta = json.load(_f)

s = u.spec_from_file_location("demo", "demo.py")
m = u.module_from_spec(s); s.loader.exec_module(m)
r = m.explore_demonstration(X, y, groups, meta)
if not isinstance(r, dict):
    print("ALETHEIA_DEMO_ERR not-a-dict", file=sys.stderr); sys.exit(2)
obs = r.get("observations")
if not isinstance(obs, dict) or not obs:
    print("ALETHEIA_DEMO_ERR no-observations", file=sys.stderr); sys.exit(3)
_FORBIDDEN = {"holds", "test_statistic", "control_statistic", "supported_if",
              "control_silent_if", "verdict"}
bad = _FORBIDDEN & (set(map(str, r.keys())) | set(map(str, obs.keys())))
if bad:
    print("ALETHEIA_DEMO_ERR verdict-fields:" + ",".join(sorted(bad)), file=sys.stderr); sys.exit(4)
clean = {}
for k, v in obs.items():
    fv = float(v)
    if not math.isfinite(fv):
        print("ALETHEIA_DEMO_ERR non-finite-observation", file=sys.stderr); sys.exit(5)
    clean[str(k)] = fv
out = {"observations": clean, "detail": str(r.get("detail", "")), "n": int(r.get("n", len(y)))}
print("ALETHEIA_DEMO_OK " + json.dumps(out))
"""


def _stage_and_run(
    code: str, X: Any, y: Any, groups: Any, meta: dict[str, Any],
    runner_script: str, timeout_s: float,
) -> dict[str, Any] | None:
    """Stage ``X``/``y``/``groups`` + ``meta`` into a temp dir and run ``runner_script`` (which
    imports ``demo.py`` and prints an ``ALETHEIA_DEMO_OK <json>`` line) in an isolated rlimit
    subprocess. Returns the parsed result dict, or ``None`` on any failure (FAIL CLOSED)."""
    import json
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    import numpy as np

    from aletheia.coder.sandbox import resource_limits

    try:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "demo.py").write_text(code)
            (tdp / "meta.json").write_text(json.dumps(meta))
            np.savez(
                tdp / "staged.npz",
                X=np.asarray(X, dtype=float),
                y=np.asarray(y, dtype=float),
                groups=(np.asarray(groups, dtype=object) if groups is not None
                        else np.asarray([], dtype=object)),
            )
            proc = subprocess.run(
                [sys.executable, "-c", runner_script],
                cwd=str(tdp), capture_output=True, text=True, timeout=timeout_s,
                stdin=subprocess.DEVNULL, preexec_fn=resource_limits(),
            )
    except Exception:  # noqa: BLE001 - any staging/timeout failure -> fail closed
        return None

    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("ALETHEIA_DEMO_OK "):
            try:
                return json.loads(line[len("ALETHEIA_DEMO_OK "):])
            except (ValueError, TypeError):
                return None
    return None


def _resolve_timeout(timeout_s: float | None) -> float:
    if timeout_s is not None:
        return float(timeout_s)
    from aletheia.config import get_settings
    return float(getattr(get_settings(), "demonstration_timeout_s", 120.0))


def run_authored_demonstration(
    code: str,
    X: Any,
    y: Any,
    groups: Any,
    meta: dict[str, Any],
    timeout_s: float | None = None,
) -> dict[str, Any] | None:
    """Execute ``code``'s ``compute_demonstration`` on staged ``X``/``y``/``groups`` in an
    isolated rlimit subprocess. Returns the validated result dict, or ``None`` on any failure."""
    return _stage_and_run(code, X, y, groups, meta, _RUNNER_SCRIPT, _resolve_timeout(timeout_s))


def run_authored_exploration(
    code: str,
    X: Any,
    y: Any,
    groups: Any,
    meta: dict[str, Any],
    timeout_s: float | None = None,
) -> dict[str, Any] | None:
    """Execute ``code``'s ``explore_demonstration`` on the staged EXPLORATION arrays in an isolated
    rlimit subprocess. Returns ``{"observations": {..}, "detail": str, "n": int}`` with descriptive
    numbers only, or ``None`` on any failure — INCLUDING when the AI smuggles a verdict field, which
    the runner rejects (the exploration step must never touch the verdict)."""
    return _stage_and_run(code, X, y, groups, meta, _EXPLORE_RUNNER_SCRIPT, _resolve_timeout(timeout_s))
