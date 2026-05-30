"""The coder worker: prompt + a constrained contract for authoring model code,
and extraction of the python block from the worker's reply."""

from __future__ import annotations

import json
import re
from typing import Any

CODER_SYSTEM = (
    "You are a meticulous ML engineer. You write small, correct, leakage-free "
    "scikit-learn modeling code. You never access data, files, the network, or the "
    "process — you only construct a model. You return a single python code block."
)

_CONTRACT = """\
Write a Python module that defines EXACTLY one function:

    def build_pipeline():
        \"\"\"Return an UNFITTED scikit-learn-compatible regressor (an estimator or a
        sklearn Pipeline). A fixed, leakage-aware harness will train and evaluate it:
        leave-chemical-system-out GroupKFold is the HEADLINE metric, alongside
        RepeatedKFold 5x5 and a baseline panel. You do NOT load data, split, fit, or
        compute any metric — only construct and return the model.\"\"\"

The input X is a dense Magpie composition feature matrix (numeric, ~130 features);
y is the target property. Prefer a well-regularized model; you may compose
preprocessing (scaling, feature selection) inside a Pipeline.

Hard rules (your code is statically checked and rejected if violated):
- Import ONLY from: sklearn, numpy, scipy, pandas, math, statistics, typing,
  dataclasses, functools, itertools, collections, warnings, random.
- No file/network/process access; no eval/exec/open/__import__/pickle/joblib;
  no dunder introspection (__globals__, __subclasses__, ...).
Return ONLY the code in a single ```python ... ``` block."""

# canned, gate-passing solution for dry-run and as a safe reference
CANNED_SOLUTION = (
    "def build_pipeline():\n"
    "    from sklearn.ensemble import RandomForestRegressor\n"
    "    return RandomForestRegressor(n_estimators=200, random_state=42, n_jobs=-1)\n"
)

_CODE_BLOCK = re.compile(r"```(?:python)?\s*(.*?)```", re.DOTALL)


def coder_prompt(design: dict[str, Any], data_spec: dict[str, Any]) -> str:
    return (
        "Author the model for this experiment.\n\n"
        f"DESIGN (intended model + params, a starting point):\n{json.dumps(design, indent=2)}\n\n"
        f"DATA:\n{json.dumps(data_spec, indent=2)}\n\n" + _CONTRACT
    )


def extract_code(text: str) -> str:
    """Pull the python block from the worker reply (or fall back to the raw text)."""
    m = _CODE_BLOCK.search(text or "")
    return (m.group(1) if m else (text or "")).strip()
