"""Trusted, container-side ScienceAgentBench outcome evaluator.

This file is mounted into the evaluator container only.  Candidate programs never receive it,
the official evaluation programs, or gold programs.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _write_terminal_receipt(path: Path, payload: dict[str, Any]) -> None:
    """Atomically commit a trusted result before the one-shot process exits."""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(
                (
                    json.dumps(
                        payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    + "\n"
                ).encode("utf-8")
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _load_evaluator(path: Path):
    spec = importlib.util.spec_from_file_location("aletheia_scienceagentbench_eval", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load the pinned ScienceAgentBench evaluator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    evaluator = getattr(module, "eval", None)
    if not callable(evaluator):
        raise RuntimeError("ScienceAgentBench evaluator has no callable eval()")
    return evaluator


def main() -> None:
    evaluator_path = Path(os.environ["SAB_EVAL_SCRIPT"]).resolve(strict=True)
    allowed_root = Path("/testbed/benchmark/eval_programs").resolve(strict=True)
    if allowed_root not in evaluator_path.parents:
        raise RuntimeError("evaluation script escaped the evaluator-only program root")

    try:
        result = _load_evaluator(evaluator_path)()
        if not isinstance(result, (tuple, list)) or len(result) != 2:
            raise RuntimeError("ScienceAgentBench eval() must return (success, log_info)")
        success, log_info = result
        success_rate = float(success)
        if not 0.0 <= success_rate <= 1.0:
            raise RuntimeError("ScienceAgentBench success rate must be between zero and one")
    except Exception as exc:
        # The trusted entrypoint itself completed, but the submitted result could not satisfy the
        # official evaluator contract.  This is an objective scientific failure, not retryable
        # evaluator infrastructure failure.
        success_rate = 0.0
        log_info = repr(exc)
    log_bytes = repr(log_info).encode("utf-8", errors="replace")
    payload = {
        "success_rate": success_rate,
        "log_info_sha256": hashlib.sha256(log_bytes).hexdigest(),
    }
    _write_terminal_receipt(Path("/receipt/result.json"), payload)

    # The receipt is the terminal evaluator handshake; skip optional imported-module teardown.
    os._exit(0)


if __name__ == "__main__":
    main()
