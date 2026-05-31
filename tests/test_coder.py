"""Phase 2 increment 6: the coder — AST safety gate (allow/deny), running a
coder-authored solution through the fixed honest eval harness, and the driver
CODE stage (dry-run accepts the canned solution; bad code is gated out → fallback).
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from aletheia.coder.sandbox import check_code
from aletheia.coder.worker import CANNED_SOLUTION, extract_code
from aletheia.db import create_all
from aletheia.domains.materials.matbench_task import MaterialsBandGapPlugin


# --- AST gate -------------------------------------------------------------
def test_gate_accepts_constrained_solution():
    ok, reasons = check_code(
        "def build_pipeline():\n"
        "    from sklearn.linear_model import Ridge\n"
        "    import numpy as np\n"
        "    return Ridge(alpha=1.0)\n"
    )
    assert ok, reasons


@pytest.mark.parametrize(
    "src, needle",
    [
        ("import os\ndef build_pipeline():\n    return None\n", "forbidden import: os"),
        ("import subprocess\ndef build_pipeline():\n    return None\n", "forbidden import: subprocess"),
        ("from socket import socket\ndef build_pipeline():\n    return None\n", "forbidden import"),
        ("def build_pipeline():\n    return eval('1')\n", "forbidden name: eval"),
        ("def build_pipeline():\n    return ().__class__.__bases__\n", "forbidden attribute access"),
        ("def f():\n    return 1\n", "must define a"),
        ("def build_pipeline(:\n", "syntax error"),
    ],
)
def test_gate_rejects_dangerous_code(src, needle):
    ok, reasons = check_code(src)
    assert not ok
    assert any(needle in r for r in reasons), reasons


@pytest.mark.parametrize(
    "src",
    [
        "import xgboost\ndef build_pipeline():\n    return xgboost.XGBRegressor()\n",
        "from lightgbm import LGBMRegressor\ndef build_pipeline():\n    return LGBMRegressor()\n",
        "import torch\ndef build_pipeline():\n    from sklearn.dummy import DummyRegressor\n    return DummyRegressor()\n",
    ],
)
def test_gate_accepts_sota_frameworks(src):
    ok, reasons = check_code(src)  # B-2: SOTA frameworks are allowed
    assert ok, reasons


def test_xgboost_solution_runs_through_harness(tmp_path):
    """A coder SOTA model (xgboost) runs through the SAME fixed honest harness —
    the coder picks the model, the harness still computes the metrics."""
    plug = MaterialsBandGapPlugin()
    sol = tmp_path / "solution.py"
    sol.write_text(
        "def build_pipeline():\n"
        "    from xgboost import XGBRegressor\n"
        "    return XGBRegressor(n_estimators=20, max_depth=3, verbosity=0)\n"
    )
    design = {"solution_path": str(sol), "random_state": 0, "test_size": 0.25}

    import xgboost

    assert isinstance(plug._make_model(design), xgboost.XGBRegressor)

    rng = np.random.default_rng(0)
    X = rng.standard_normal((30, 5))
    y = X[:, 0] * 2.0 + rng.standard_normal(30) * 0.1
    groups = np.array([f"g{i % 6}" for i in range(30)], dtype=object)
    result = plug.train_evaluate(X, y, design, tmp_path, groups=groups)
    assert {"mae_lcso", "mae_cv_mean", "mae_holdout"} <= set(result.metrics)


def test_extract_code_pulls_block():
    assert extract_code("blah\n```python\ndef build_pipeline():\n    pass\n```\nend").startswith(
        "def build_pipeline"
    )
    assert extract_code("def build_pipeline(): pass") == "def build_pipeline(): pass"
    assert check_code(CANNED_SOLUTION)[0]  # the canned dry-run solution passes


# --- a coder solution runs through the fixed eval harness -----------------
def test_solution_pipeline_runs_through_harness(tmp_path):
    plug = MaterialsBandGapPlugin()
    sol = tmp_path / "solution.py"
    sol.write_text(
        "def build_pipeline():\n"
        "    from sklearn.linear_model import Ridge\n"
        "    return Ridge(alpha=0.5)\n"
    )
    design = {"solution_path": str(sol), "random_state": 0, "test_size": 0.25}

    # _make_model loads the coder's estimator instead of the fixed model
    model = plug._make_model(design)
    from sklearn.linear_model import Ridge

    assert isinstance(model, Ridge)

    # synthetic numeric data with enough rows + groups for the protocol
    rng = np.random.default_rng(0)
    X = rng.standard_normal((30, 5))
    y = X[:, 0] * 2.0 + rng.standard_normal(30) * 0.1
    groups = np.array([f"g{i % 6}" for i in range(30)])  # 6 chemical systems

    result = plug.train_evaluate(X, y, design, tmp_path, groups=groups)
    assert {"mae", "mae_lcso", "mae_cv_mean", "mae_holdout"} <= set(result.metrics)
    assert result.metrics["mae"] >= 0.0
    assert (tmp_path / "eval.json").exists()


# --- driver CODE stage ----------------------------------------------------
def test_code_stage_dry_run_accepts_canned():
    create_all()
    from aletheia.scheduler.driver import ExperimentDriver

    driver = ExperimentDriver("run-coder-dry", dry_run=True)
    code = asyncio.run(driver._code({"model": "random_forest"}, {"target_column": "y"}, None))
    assert code and "build_pipeline" in code
    assert check_code(code)[0]


def test_code_stage_rejects_bad_solution(monkeypatch):
    create_all()
    import aletheia.scheduler.driver as drv
    from aletheia.memory.service import create_run

    async def bad_worker(*a, **k):
        return "```python\nimport os\ndef build_pipeline():\n    return os.getcwd()\n```"

    monkeypatch.setattr(drv, "run_worker", bad_worker)
    run_id = create_run("coder bad", domain="materials")  # real run for the transition FK
    driver = drv.ExperimentDriver(run_id, dry_run=True)
    code = asyncio.run(driver._code({"model": "random_forest"}, {}, None))
    assert code is None  # gated out -> driver falls back to the fixed model
