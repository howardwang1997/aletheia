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

_CONTRACT_TEMPLATE = """\
Write a Python module that defines EXACTLY one function:

    def build_pipeline():
        \"\"\"Return an UNFITTED scikit-learn-compatible regressor (an estimator or a
        sklearn Pipeline). A fixed, leakage-aware harness will train and evaluate it
        ({protocol}). You do NOT load data, split, fit, or compute any metric — only
        construct and return the model.\"\"\"

The input X is {feature_desc} (numeric); y is the target property. You may use a
STATE-OF-THE-ART model — gradient boosting (xgboost / lightgbm) or a small neural net
(torch, wrapped as a fit/predict estimator e.g. via skorch or a tiny custom class) —
and compose preprocessing (scaling, feature selection) inside a Pipeline. Prefer
well-regularized models; default to a strong gradient-boosting baseline unless you
have reason to go deeper.

The evaluation is NOT yours: a fixed, leakage-aware harness trains + scores the model
you return ({protocol}). So you only construct the model — you cannot influence the
metric. Make it honestly strong.

Hard rules (your code is statically checked and rejected if violated):
- Import ONLY from: sklearn, numpy, scipy, pandas, math, statistics, xgboost,
  lightgbm, torch, skorch (+ typing, dataclasses, functools, itertools, collections,
  warnings, random).
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


def coder_prompt(
    design: dict[str, Any],
    data_spec: dict[str, Any],
    *,
    feature_desc: str = "a dense numeric feature matrix",
    protocol: str = "grouped cross-validation (headline) + RepeatedKFold + a baseline panel",
) -> str:
    contract = _CONTRACT_TEMPLATE.format(feature_desc=feature_desc, protocol=protocol)
    return (
        "Author the model for this experiment.\n\n"
        f"DESIGN (intended model + params, a starting point):\n{json.dumps(design, indent=2)}\n\n"
        f"DATA:\n{json.dumps(data_spec, indent=2)}\n\n" + contract
    )


def extract_code(text: str) -> str:
    """Pull the python block from the worker reply (or fall back to the raw text)."""
    m = _CODE_BLOCK.search(text or "")
    return (m.group(1) if m else (text or "")).strip()
