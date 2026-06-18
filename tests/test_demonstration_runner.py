"""K1 S2: the AI-authored EXPLORATION probe runner.

``run_authored_exploration`` returns DESCRIPTIVE observations only and FAILS CLOSED (returns None)
when the AI smuggles a verdict field — the exploration step is never allowed to influence the
harness verdict. These run a real isolated subprocess (offline; no network)."""

from __future__ import annotations

import numpy as np

from aletheia.coder.demonstration_runner import (
    run_authored_demonstration,
    run_authored_exploration,
)

_X = np.random.default_rng(0).random((40, 4))
_Y = np.random.default_rng(1).random(40)
_G = np.array([i % 5 for i in range(40)], dtype=object)
_META = {"random_state": 0}

_GOOD_EXPLORE = (
    "def explore_demonstration(X, y, groups, meta):\n"
    "    import numpy as np\n"
    "    return {'observations': {'y_std': float(np.std(y)), 'y_mean': float(np.mean(y))},\n"
    "            'detail': 'spread', 'n': int(len(y))}\n"
)


def test_exploration_returns_descriptive_observations():
    r = run_authored_exploration(_GOOD_EXPLORE, _X, _Y, _G, _META)
    assert r is not None
    assert set(r["observations"]) == {"y_std", "y_mean"}
    assert r["n"] == 40 and isinstance(r["detail"], str)
    assert all(isinstance(v, float) for v in r["observations"].values())


def test_exploration_rejects_top_level_verdict_field():
    smuggle = (
        "def explore_demonstration(X, y, groups, meta):\n"
        "    import numpy as np\n"
        "    return {'observations': {'y_std': float(np.std(y))}, 'test_statistic': 1.0,\n"
        "            'detail': 'x', 'n': int(len(y))}\n"
    )
    assert run_authored_exploration(smuggle, _X, _Y, _G, _META) is None


def test_exploration_rejects_verdict_field_inside_observations():
    smuggle = (
        "def explore_demonstration(X, y, groups, meta):\n"
        "    return {'observations': {'holds': 1.0}, 'detail': 'x', 'n': int(len(y))}\n"
    )
    assert run_authored_exploration(smuggle, _X, _Y, _G, _META) is None


def test_exploration_rejects_empty_or_nonfinite_observations():
    empty = "def explore_demonstration(X, y, groups, meta):\n    return {'observations': {}, 'n': 1}\n"
    nonfinite = (
        "def explore_demonstration(X, y, groups, meta):\n"
        "    return {'observations': {'bad': float('inf')}, 'n': int(len(y))}\n"
    )
    assert run_authored_exploration(empty, _X, _Y, _G, _META) is None
    assert run_authored_exploration(nonfinite, _X, _Y, _G, _META) is None


def test_compute_demonstration_runner_still_works():
    # the refactor (shared staging) must not break the original demonstration runner
    code = (
        "def compute_demonstration(X, y, groups, meta):\n"
        "    import numpy as np\n"
        "    n = len(y); half = n // 2\n"
        "    return {'test_statistic': float(np.std(y[:half])),\n"
        "            'control_statistic': float(np.std(y[half:])),\n"
        "            'components': {}, 'detail': 'ok', 'n_test': half, 'n_control': n - half}\n"
    )
    r = run_authored_demonstration(code, _X, _Y, _G, _META)
    assert r is not None
    assert "test_statistic" in r and "control_statistic" in r and r["n_test"] == 20
