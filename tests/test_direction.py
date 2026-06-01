"""Phase L: the headline-metric DIRECTION (min for error metrics, max for F1/recall).
A max-goal domain must compare/optimize/beat-SOTA in the higher-is-better direction."""

from __future__ import annotations

from aletheia.domains.registry import get_domain_plugin
from aletheia.scheduler.driver import ExperimentDriver


def _driver_with(domain: str) -> ExperimentDriver:
    d = ExperimentDriver(f"dir-{domain}", dry_run=True)
    d.profile = get_domain_plugin(domain).profile()
    return d


def test_is_better_honors_goal():
    rag = _driver_with("rag")  # goal = max
    assert rag._maximize() is True
    assert rag._is_better(0.6, 0.5) is True   # higher F1 wins
    assert rag._is_better(0.4, 0.5) is False
    assert rag._worst() == float("-inf")

    mat = _driver_with("materials")  # goal = min
    assert mat._maximize() is False
    assert mat._is_better(0.3, 0.4) is True   # lower MAE wins
    assert mat._is_better(0.5, 0.4) is False
    assert mat._worst() == float("inf")


def test_compare_to_sota_max_goal():
    rag = _driver_with("rag")
    rag.survey_sota = [{"method": "BM25", "dataset": "mini-QA", "metric": "answer_f1", "score": 0.25}]
    best, comparable, beat = rag._compare_to_sota("answer_f1", 0.30)
    assert comparable and best["score"] == 0.25
    assert beat is True  # 0.30 > 0.25 under max goal

    best, comparable, beat = rag._compare_to_sota("answer_f1", 0.20)
    assert beat is False  # 0.20 < 0.25


def test_compare_to_sota_min_goal_unchanged():
    mat = _driver_with("materials")
    mat.survey_sota = [{"method": "MODNet", "dataset": "expt_gap", "metric": "mae", "score": 0.33}]
    _best, _comparable, beat = mat._compare_to_sota("mae_lcso", 0.30)
    assert beat is True  # 0.30 < 0.33 under min goal
