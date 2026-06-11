"""Real token/cost accounting (:mod:`aletheia.memory.usage`) + the budget guardrail binding
on the SDK's real reported cost and an optional token cap.

The accounting reads the ground truth already in the ledger: every Claude SDK call persists a
``result`` event with ``total_cost_usd`` + ``usage``. These tests persist synthetic result
events and assert the aggregation, the no-double-count invariant, tolerance of dry-run/odd
shapes, and that ``BudgetTracker.breaches()`` fires on real cost / tokens.
"""

from __future__ import annotations

from aletheia.config import get_settings
from aletheia.db import create_all
from aletheia.events.bus import make_event
from aletheia.events.store import persist_event
from aletheia.memory.service import create_run
from aletheia.memory.usage import (
    aggregate_run_usage,
    list_run_ids_with_usage,
    recent_window_status,
    run_rate_limit,
)
from aletheia.scheduler.budget import BudgetTracker


def _result_evt(
    run_id: str,
    cost: float,
    *,
    in_tok: int = 0,
    out_tok: int = 0,
    cache_read: int = 0,
    cache_create: int = 0,
    turns: int = 1,
    usage: bool = True,
) -> None:
    payload = {"result": None, "cost_usd": cost, "is_error": False, "num_turns": turns}
    if usage:
        payload["usage"] = {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_create,
            # nested breakdown of cache_creation_input_tokens — must NOT be double-counted
            "cache_creation": {"ephemeral_1h_input_tokens": cache_create, "ephemeral_5m_input_tokens": 0},
            "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
        }
    persist_event(make_event("result", run_id=run_id, agent="worker", payload=payload))


def test_aggregate_sums_cost_and_tokens_across_calls():
    create_all()
    run_id = create_run("usage agg", domain="materials", status="scoping")
    _result_evt(run_id, 0.04, in_tok=2, out_tok=1000, cache_read=12000, cache_create=4000, turns=3)
    _result_evt(run_id, 0.06, in_tok=3, out_tok=500, cache_read=8000, cache_create=1000, turns=2)

    u = aggregate_run_usage(run_id)
    assert u.n_calls == 2
    assert u.num_turns == 5
    assert abs(u.cost_usd - 0.10) < 1e-9
    assert u.input_tokens == 5
    assert u.output_tokens == 1500
    assert u.cache_read_input_tokens == 20000
    assert u.cache_creation_input_tokens == 5000
    # total = in + out + cache_read + cache_create; the nested ephemeral_* breakdown is NOT added
    assert u.total_tokens == 5 + 1500 + 20000 + 5000


def test_tolerates_dry_run_and_odd_usage_shapes():
    create_all()
    run_id = create_run("usage odd", domain="materials", status="scoping")
    # a dry-run result event: cost 0.0, NO usage key (run_dryrun shape)
    persist_event(make_event("result", run_id=run_id, payload={"result": "dry", "cost_usd": 0.0}))
    # a stringified usage (SDK shape drift): cost still counts, tokens skipped
    persist_event(make_event(
        "result", run_id=run_id, payload={"cost_usd": 0.02, "usage": "Usage(input_tokens=1)"}))
    _result_evt(run_id, 0.03, out_tok=10)

    u = aggregate_run_usage(run_id)
    assert abs(u.cost_usd - 0.05) < 1e-9  # 0.0 + 0.02 + 0.03
    assert u.n_calls == 1  # only the one with a dict usage
    assert u.output_tokens == 10
    assert u.total_tokens == 10


def test_empty_run_is_all_zeros():
    create_all()
    run_id = create_run("usage empty", domain="materials", status="scoping")
    u = aggregate_run_usage(run_id)
    assert u.n_calls == 0 and u.cost_usd == 0.0 and u.total_tokens == 0


def test_list_run_ids_with_usage_only_includes_result_runs():
    create_all()
    with_usage = create_run("has usage", domain="materials", status="scoping")
    without = create_run("no usage", domain="materials", status="scoping")
    _result_evt(with_usage, 0.01, out_tok=5)
    persist_event(make_event("assistant_text", run_id=without, payload={"text": "hi"}))

    ids = list_run_ids_with_usage()
    assert with_usage in ids
    assert without not in ids


def test_budget_breaches_on_real_cost_over_cap():
    create_all()
    run_id = create_run("budget real", domain="materials", status="scoping", budget_cap_usd=0.05)
    # no estimate charged (so _cum_usd == 0), but the SDK reported real cost above the cap
    _result_evt(run_id, 0.04, out_tok=100)
    _result_evt(run_id, 0.05, out_tok=100)

    tracker = BudgetTracker(run_id)
    breaches = {b["kind"]: b for b in tracker.breaches()}
    assert "usd" in breaches
    assert abs(breaches["usd"]["real_usd"] - 0.09) < 1e-9
    assert breaches["usd"]["spent"] >= 0.09  # max(estimate=0, real=0.09)


def test_budget_token_cap(monkeypatch):
    create_all()
    monkeypatch.setattr(get_settings(), "token_cap_per_run", 10_000)
    run_id = create_run("budget tokens", domain="materials", status="scoping", budget_cap_usd=1000.0)
    _result_evt(run_id, 0.0, out_tok=6000, cache_read=6000)  # 12k > 10k cap, cost 0 (subscription)

    tracker = BudgetTracker(run_id)
    breaches = {b["kind"]: b for b in tracker.breaches()}
    assert "tokens" in breaches
    assert breaches["tokens"]["spent"] == 12000
    assert "usd" not in breaches  # cost 0, generous usd cap


def _system_evt(run_id: str, repr_str: str) -> None:
    persist_event(make_event("system", run_id=run_id, payload={"repr": repr_str}))


def test_rate_limit_parses_five_hour_window_and_ignores_other_windows():
    create_all()
    run_id = create_run("rate limit", domain="materials", status="completed")
    # two five_hour reports (rising), one of them throttled; plus a seven_day window that must be ignored
    _system_evt(run_id, "RateLimitEvent(rate_limit_info=RateLimitInfo(status='allowed_warning', "
                "resets_at=1780989600, rate_limit_type='five_hour', utilization=0.95, raw={}))")
    _system_evt(run_id, "RateLimitEvent(rate_limit_info=RateLimitInfo(status='rejected', "
                "resets_at=1780989600, rate_limit_type='five_hour', utilization=0.99, raw={}))")
    _system_evt(run_id, "RateLimitEvent(rate_limit_info=RateLimitInfo(status='allowed', "
                "resets_at=1780000000, rate_limit_type='seven_day', utilization=0.10, raw={}))")
    # a system event with no rate-limit info at all
    _system_evt(run_id, "SystemMessage(subtype='init', model='claude-opus-4-8')")

    rl = run_rate_limit(run_id)
    assert rl.samples == 2  # only the two five_hour reports
    assert rl.peak_utilization == 0.99  # max, NOT the seven_day 0.10
    assert rl.worst_status == "rejected"
    assert rl.rejections == 1


def test_rate_limit_empty_when_no_reports():
    create_all()
    run_id = create_run("rate limit empty", domain="materials", status="scoping")
    rl = run_rate_limit(run_id)
    assert rl.samples == 0 and rl.peak_utilization is None and rl.rejections == 0


def test_token_cap_off_by_default():
    create_all()
    run_id = create_run("budget no tokcap", domain="materials", status="scoping", budget_cap_usd=1000.0)
    _result_evt(run_id, 0.0, out_tok=50_000)
    tracker = BudgetTracker(run_id)
    assert tracker.token_cap is None
    assert tracker.breaches() == []  # token cap off, cost 0, generous usd cap, fresh wall clock


def _five_hour_repr(status: str, util: str) -> str:
    return (
        f"RateLimitEvent(rate_limit_info=RateLimitInfo(status='{status}', resets_at=1780989600, "
        f"rate_limit_type='five_hour', utilization={util}, raw={{}}))"
    )


def _system_evt_at(run_id: str, repr_str: str, hours_ago: float) -> None:
    """Insert a system event with an explicit ``ts`` so the time-filter (resume safety) is testable."""
    from datetime import datetime, timedelta, timezone

    from aletheia.db import session_scope
    from aletheia.memory.ledger import Event

    with session_scope() as s:
        s.add(Event(
            run_id=run_id, type="system", payload={"repr": repr_str},
            ts=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
        ))


def test_recent_window_status_takes_latest_recent_reading():
    create_all()
    run_id = create_run("recent latest", domain="materials", status="scoping")
    _system_evt_at(run_id, _five_hour_repr("allowed_warning", "0.80"), hours_ago=0.3)  # ~18 min
    _system_evt_at(run_id, _five_hour_repr("allowed_warning", "0.92"), hours_ago=0.05)  # ~3 min
    status, util = recent_window_status(run_id)
    assert status == "allowed_warning"
    assert util == 0.92  # the later (more recent) reading wins, both within the recency window


def test_recent_window_status_ignores_stale_prior_window():
    # THE resume-safety invariant: a 'rejected' from a prior, already-reset window (>5h ago) must
    # NOT be the live reading — only the fresh-window 'allowed' inside the rolling window counts.
    create_all()
    run_id = create_run("recent stale", domain="materials", status="scoping")
    _system_evt_at(run_id, _five_hour_repr("rejected", "None"), hours_ago=8.0)
    _system_evt_at(run_id, _five_hour_repr("allowed", "0.10"), hours_ago=0.02)
    status, util = recent_window_status(run_id)
    assert status == "allowed"
    assert util == 0.10


def test_recent_window_status_none_when_no_recent_sample():
    create_all()
    run_id = create_run("recent none", domain="materials", status="scoping")
    _system_evt_at(run_id, _five_hour_repr("rejected", "None"), hours_ago=9.0)  # all stale
    assert recent_window_status(run_id) == (None, None)


def test_budget_window_stop_fires_on_high_util(monkeypatch):
    create_all()
    monkeypatch.setattr(get_settings(), "window_stop_utilization", 0.85)
    run_id = create_run("win stop", domain="materials", status="scoping", budget_cap_usd=1000.0)
    _system_evt_at(run_id, _five_hour_repr("allowed_warning", "0.90"), hours_ago=0.05)
    tracker = BudgetTracker(run_id)
    breaches = {b["kind"]: b for b in tracker.breaches()}
    assert "five_hour_window" in breaches
    assert breaches["five_hour_window"]["utilization"] == 0.90


def test_budget_window_stop_fires_on_rejected_even_without_util(monkeypatch):
    create_all()
    monkeypatch.setattr(get_settings(), "window_stop_utilization", 0.85)
    run_id = create_run("win rej", domain="materials", status="scoping", budget_cap_usd=1000.0)
    _system_evt_at(run_id, _five_hour_repr("rejected", "None"), hours_ago=0.05)
    assert "five_hour_window" in {b["kind"] for b in BudgetTracker(run_id).breaches()}


def test_budget_window_stop_ignores_stale_rejection(monkeypatch):
    # resume safety at the budget layer: a stale rejection (prior window) does not pause a run that
    # has resumed into a FRESH, healthy window.
    create_all()
    monkeypatch.setattr(get_settings(), "window_stop_utilization", 0.85)
    run_id = create_run("win resume", domain="materials", status="scoping", budget_cap_usd=1000.0)
    _system_evt_at(run_id, _five_hour_repr("rejected", "None"), hours_ago=8.0)
    _system_evt_at(run_id, _five_hour_repr("allowed", "0.05"), hours_ago=0.02)
    assert "five_hour_window" not in {b["kind"] for b in BudgetTracker(run_id).breaches()}


def test_budget_window_stop_off_by_default():
    create_all()
    run_id = create_run("win off", domain="materials", status="scoping", budget_cap_usd=1000.0)
    _system_evt_at(run_id, _five_hour_repr("rejected", "None"), hours_ago=0.05)
    tracker = BudgetTracker(run_id)
    assert tracker.window_stop_util is None
    assert tracker.breaches() == []  # window stop off by default -> a rejection does not pause
