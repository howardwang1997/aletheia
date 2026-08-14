from __future__ import annotations

import asyncio

import numpy as np
import pytest

from aletheia.domains.materials.matbench_task import MaterialsBandGapPlugin
from aletheia.research.discovery import discover
from .f8s3_fixtures import sha
from .f8s5_fixtures import (
    build_f8s5_direction_fixture,
    build_f8s5_live_fixture,
)


_PREREG = {
    "statistic_name": "s",
    "supported_if": {"op": ">=", "threshold": 0.5},
    "control_silent_if": {"op": "<=", "threshold": 0.1},
}


def _demo() -> str:
    return (
        "def compute_demonstration(X, y, groups, meta):\n"
        "    return {'test_statistic': 1.0, 'control_statistic': 0.0, "
        "'n_test': 50, 'n_control': 50, 'components': {}, 'detail': 'd'}\n"
    )


@pytest.fixture(scope="module")
def gates(tmp_path_factory):
    live = {
        kind: asyncio.run(
            build_f8s5_live_fixture(
                tmp_path_factory.mktemp(f"discovery-{kind}"),
                novelty_kind=kind,
            )
        )
        for kind in ("strong", "known")
    }
    return {kind: build_f8s5_direction_fixture(fixture)["gate"] for kind, fixture in live.items()}


def _data():
    rng = np.random.default_rng(51)
    return (
        rng.random((120, 8)),
        rng.random(120),
        np.array(["A-B"] * 60 + ["C-D"] * 60, dtype=object),
    )


def test_discovery_uses_exact_f8s5_gate_instead_of_count_critic_shortcut(gates) -> None:
    candidate_sha256 = gates["strong"].novelty_decision.assessment.candidate_claim_sha256s[0]
    candidates = [
        {
            "title": title,
            "insight": "audited",
            "claim": title,
            "candidate_claim_sha256": candidate_sha256,
            "code": _demo(),
            "prereg": _PREREG,
        }
        for title in ("strong", "known")
    ]
    X, y, groups = _data()

    survivors, rows = discover(
        ideate_fn=lambda _avoid, _lessons: candidates,
        plugin=MaterialsBandGapPlugin(),
        X=X,
        y=y,
        groups=groups,
        gateway=object(),
        run_id="auditable-discovery",
        k_survivors=9,
        max_rounds=1,
        auditable_novelty_gate_fn=lambda candidate: gates[candidate["title"]],
        log=lambda *_args: None,
    )

    by_title = {row["title"]: row for row in rows}
    assert [row["title"] for row in survivors] == ["strong"]
    assert by_title["strong"]["auditable_direction_gate_sha256"] == gates["strong"].gate_sha256
    assert by_title["strong"]["claim_strength_ceiling"] == "moderate"
    assert by_title["known"]["consensus"] == "reject_known_direction"
    assert by_title["known"]["survives"] is False


def test_discovery_fails_closed_when_gate_is_for_another_atomic_claim(gates) -> None:
    X, y, groups = _data()
    candidate = {
        "title": "mismatched",
        "insight": "audited",
        "claim": "mismatched",
        "candidate_claim_sha256": sha("another-candidate-claim"),
        "code": _demo(),
        "prereg": _PREREG,
    }

    survivors, rows = discover(
        ideate_fn=lambda _avoid, _lessons: [candidate],
        plugin=MaterialsBandGapPlugin(),
        X=X,
        y=y,
        groups=groups,
        gateway=object(),
        run_id="mismatched-auditable-discovery",
        k_survivors=1,
        max_rounds=1,
        auditable_novelty_gate_fn=lambda _candidate: gates["strong"],
        log=lambda *_args: None,
    )

    assert survivors == []
    assert "identity mismatch" in rows[0]["why"]
