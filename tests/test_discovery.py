"""Autonomy discovery loop (aletheia/research/discovery.py) — the system self-screens bold candidates.

Offline + deterministic: a FAKE ideator yields canned candidates that each exercise one filter branch;
a FAKE gateway + FAKE literature search stand in for the cross-vendor novelty gate + grounding. The
DETERMINISTIC triage (code-gate / sandbox run / hold / magnitude) runs for real on tiny synthetic
data, so the full filter chain is exercised without network or Claude.
"""

from __future__ import annotations

import numpy as np

from aletheia.domains.materials.matbench_task import MaterialsBandGapPlugin
from aletheia.research.discovery import discover

_PREREG = {"statistic_name": "s", "supported_if": {"op": ">=", "threshold": 0.5},
           "control_silent_if": {"op": "<=", "threshold": 0.1}}


def _const_demo(test, control):
    return ("def compute_demonstration(X, y, groups, meta):\n"
            f"    return {{'test_statistic': {test}, 'control_statistic': {control}, "
            "'n_test': 50, 'n_control': 50, 'components': {}, 'detail': 'd'}\n")


# one candidate per filter branch
_CANDS = [
    {"title": "GOOD effect", "insight": "clean", "claim": "a clean discriminating effect",
     "code": _const_demo(1.0, 0.0), "prereg": _PREREG},                              # -> SURVIVES
    {"title": "buggy code", "insight": "x", "claim": "x",
     "code": "def compute_demonstration(X, y, groups, meta):\n    return 123\n", "prereg": _PREREG},  # runnability
    {"title": "trivial magnitude", "insight": "x", "claim": "x",
     "code": _const_demo(0.6, 0.55),
     "prereg": {"statistic_name": "s", "supported_if": {"op": ">=", "threshold": 0.5},
                "control_silent_if": {"op": "<=", "threshold": 0.6}}},                # scored: trivial
    {"title": "not novel", "insight": "x", "claim": "a NOTNOVEL repackaged effect",
     "code": _const_demo(1.0, 0.0), "prereg": _PREREG},                              # novelty/grnd: gate rejects
    {"title": "ungrounded", "insight": "x", "claim": "an UNGROUNDED effect",
     "code": _const_demo(1.0, 0.0), "prereg": _PREREG},                              # novelty/grnd: 0 papers
]


class _FakePanel:
    def __init__(self, gate_passed: bool):
        self.gate_passed = gate_passed
        self.consensus_verdict = "approve" if gate_passed else "reject"
        self.critiques = []  # no novelty findings -> novelty signal comes from gate_passed alone


class _FakeGateway:
    async def review(self, target, content, target_ref=None, run_id=None, dry_run=False, exclude_vendors=None):
        statement = (content.get("hypothesis") or {}).get("statement", "")
        return _FakePanel("NOTNOVEL" not in statement)  # the "not novel" candidate is rejected


def _fake_search(query, k):
    return [] if "UNGROUNDED" in query else [object()] * 6   # <3 papers => ungrounded


def test_discovery_full_filter_keeps_only_the_real_novel_grounded_candidate():
    rng = np.random.default_rng(0)
    X = rng.random((120, 8))
    y = rng.random(120)
    groups = np.array(["A-B"] * 60 + ["C-D"] * 60, dtype=object)
    plugin = MaterialsBandGapPlugin()

    survivors, rows = discover(
        ideate_fn=lambda avoid, lessons: [c for c in _CANDS if c["title"] not in set(avoid)],
        plugin=plugin, X=X, y=y, groups=groups, gateway=_FakeGateway(), run_id="t",
        k_survivors=99, max_rounds=1, search_fn=_fake_search, briefing_fn=lambda p: "", log=lambda *a: None,
        novelty_exclude={"test-author"},
    )

    assert len(rows) == 5
    by_title = {r["title"]: r for r in rows}
    # exactly the good candidate survives the FULL filter
    assert [s["title"] for s in survivors] == ["GOOD effect"]
    assert by_title["GOOD effect"]["survives"] is True
    assert by_title["GOOD effect"]["candidate"]["code"]  # survivor carries its code for the campaign
    # each other candidate died at the right gate
    assert by_title["buggy code"]["stage"] == "runnability" and not by_title["buggy code"]["survives"]
    assert by_title["trivial magnitude"]["stage"] == "scored" and not by_title["trivial magnitude"]["survives"]
    assert "too small" in by_title["trivial magnitude"]["why"]  # 0.6 vs a 0.55 control -> no separation
    assert by_title["not novel"]["stage"] == "novelty/grnd" and not by_title["not novel"]["survives"]
    assert by_title["ungrounded"]["stage"] == "novelty/grnd" and by_title["ungrounded"]["grounded"] is False


def test_discovery_promotes_real_effect_below_groks_blind_threshold():
    """The decoupling fix: a real effect with a CLEAN vanishing control survives the screen even though
    its test statistic is far BELOW grok's blind supported_if threshold (which used to mis-kill it)."""
    rng = np.random.default_rng(2)
    X, y = rng.random((120, 6)), rng.random(120)
    groups = np.array(["A-B"] * 60 + ["C-D"] * 60, dtype=object)
    cand = {"title": "real but under the blind bar", "insight": "x", "claim": "a real grounded effect",
            "code": _const_demo(0.7, 0.001),  # test 0.7 << supported_if 2.0, but control 0.001 vanishes
            "prereg": {"statistic_name": "s", "supported_if": {"op": ">=", "threshold": 2.0},
                       "control_silent_if": {"op": "<=", "threshold": 0.05}}}
    survivors, rows = discover(
        ideate_fn=lambda avoid, lessons: [cand], plugin=MaterialsBandGapPlugin(), X=X, y=y, groups=groups,
        gateway=_FakeGateway(), run_id="t", k_survivors=1, max_rounds=1,
        search_fn=_fake_search, briefing_fn=lambda p: "", log=lambda *a: None,
        novelty_exclude={"test-author"})
    assert rows[0]["survives"]  # promoted as an EXPLORATORY signal despite the blind threshold
    assert rows[0]["holds"] is False  # never misreported as satisfying its pre-registration
    assert [s["title"] for s in survivors] == ["real but under the blind bar"]


def test_discovery_fails_closed_when_grounding_search_errors():
    rng = np.random.default_rng(3)
    X, y = rng.random((120, 6)), rng.random(120)
    groups = np.array(["A-B"] * 60 + ["C-D"] * 60, dtype=object)
    cand = {"title": "search outage", "insight": "x", "claim": "a possibly novel effect",
            "code": _const_demo(1.0, 0.0), "prereg": _PREREG}

    def broken_search(_query, _k):
        raise ConnectionError("literature backend unavailable")

    survivors, rows = discover(
        ideate_fn=lambda avoid, lessons: [cand], plugin=MaterialsBandGapPlugin(), X=X, y=y,
        groups=groups, gateway=_FakeGateway(), run_id="t", k_survivors=1, max_rounds=1,
        search_fn=broken_search, briefing_fn=lambda p: "", log=lambda *a: None,
        novelty_exclude={"test-author"},
    )

    assert survivors == []
    assert rows[0]["grounded"] is None
    assert rows[0]["stage"] == "novelty/grnd"


def test_discovery_fails_closed_when_author_vendor_is_not_declared():
    rng = np.random.default_rng(4)
    X, y = rng.random((120, 6)), rng.random(120)
    groups = np.array(["A-B"] * 60 + ["C-D"] * 60, dtype=object)
    cand = {"title": "unknown author", "insight": "x", "claim": "effect",
            "code": _const_demo(1.0, 0.0), "prereg": _PREREG}
    survivors, rows = discover(
        ideate_fn=lambda avoid, lessons: [cand], plugin=MaterialsBandGapPlugin(), X=X, y=y,
        groups=groups, gateway=_FakeGateway(), run_id="t", k_survivors=1, max_rounds=1,
        search_fn=_fake_search, briefing_fn=lambda p: "", log=lambda *a: None,
    )
    assert survivors == []
    assert "author vendors" in rows[0]["why"]


def test_discovery_stops_at_k_survivors():
    rng = np.random.default_rng(1)
    X, y = rng.random((120, 6)), rng.random(120)
    groups = np.array(["A-B"] * 120, dtype=object)
    good = {"title": "g", "insight": "x", "claim": "clean", "code": _const_demo(1.0, 0.0), "prereg": _PREREG}
    survivors, _rows = discover(
        ideate_fn=lambda avoid, lessons: [dict(good, title=f"g{len(avoid)}")],  # a fresh survivor each round
        plugin=MaterialsBandGapPlugin(), X=X, y=y, groups=groups, gateway=_FakeGateway(), run_id="t",
        k_survivors=2, max_rounds=5, search_fn=_fake_search, briefing_fn=lambda p: "", log=lambda *a: None,
        novelty_exclude={"test-author"},
    )
    assert len(survivors) == 2  # stopped as soon as 2 banked, not all 5 rounds
