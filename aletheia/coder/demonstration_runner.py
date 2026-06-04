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
    import json
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    import numpy as np

    from aletheia.coder.sandbox import resource_limits
    from aletheia.config import get_settings

    if timeout_s is None:
        timeout_s = float(getattr(get_settings(), "demonstration_timeout_s", 120.0))

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
                [sys.executable, "-c", _RUNNER_SCRIPT],
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
