"""Real token + cost usage report — see exactly what each run (and the project) spent.

Every Claude Agent SDK call persists a ``result`` event with the SDK's own ``total_cost_usd``
and ``usage`` token counts; :mod:`aletheia.memory.usage` sums them per run from the ledger.
This is the human-facing view over that data — no estimate, no instrumentation, just the
numbers the SDK reported.

    conda run -n aletheia python scripts/usage_report.py            # table of all runs + grand total
    conda run -n aletheia python scripts/usage_report.py <run_id>   # full breakdown for one run
    conda run -n aletheia python scripts/usage_report.py --top 10   # the 10 priciest runs by tokens

Why tokens, not dollars: under a Claude *subscription* login the SDK reports cost_usd ~0 even
though real tokens were spent, and it is TOKENS — especially cache_read — that meter the rolling
usage window (the "5-hour limit"). The cost column is still shown (it is real under API-key auth).
"""

from __future__ import annotations

import sys

from aletheia.memory.service import get_run
from aletheia.memory.usage import (
    RunUsage,
    aggregate_run_usage,
    format_rate_limit,
    list_run_ids_with_usage,
    run_rate_limit,
)


def _fmt_int(n: int) -> str:
    return f"{n:,}"


def _print_one(run_id: str) -> None:
    u = aggregate_run_usage(run_id)
    run = get_run(run_id) or {}
    goal = (run.get("goal") or "").strip().replace("\n", " ")
    print(f"\n=== usage for run {run_id} ===")
    if run:
        print(f"  domain={run.get('domain')}  status={run.get('status')}  created={run.get('created_at')}")
        print(f"  cap_usd={run.get('budget_cap_usd')}")
        if goal:
            print(f"  goal: {goal[:140]}")
    print(f"  SDK calls (with usage) : {u.n_calls}")
    print(f"  assistant turns        : {u.num_turns}")
    print(f"  cost_usd (SDK-reported): ${u.cost_usd:.4f}")
    print(f"  input_tokens           : {_fmt_int(u.input_tokens)}")
    print(f"  output_tokens          : {_fmt_int(u.output_tokens)}")
    print(f"  cache_read_input_tokens: {_fmt_int(u.cache_read_input_tokens)}  <- usually the dominant cost")
    print(f"  cache_creation_tokens  : {_fmt_int(u.cache_creation_input_tokens)}")
    print(f"  TOTAL tokens           : {_fmt_int(u.total_tokens)}")
    if u.web_search_requests or u.web_fetch_requests:
        print(f"  server tools           : {u.web_search_requests} web_search, {u.web_fetch_requests} web_fetch")
    print(f"  {format_rate_limit(run_rate_limit(run_id))}")


def _print_table(limit: int | None = None) -> None:
    run_ids = list_run_ids_with_usage()
    rows: list[tuple[str, RunUsage, dict]] = [
        (rid, aggregate_run_usage(rid), get_run(rid) or {}) for rid in run_ids
    ]
    rows.sort(key=lambda r: r[1].total_tokens, reverse=True)
    shown = rows[:limit] if limit else rows

    print(f"\n{'run_id':14} {'domain':10} {'status':16} {'calls':>5} {'cost_usd':>9} "
          f"{'out_tok':>10} {'cache_rd':>11} {'total_tok':>12} {'5h_peak':>8}")
    print("-" * 106)
    for rid, u, run in shown:
        rl = run_rate_limit(rid)
        peak = f"{rl.peak_utilization * 100:.0f}%" if rl.peak_utilization is not None else "-"
        if rl.rejections:
            peak += "!"  # the run hit the 5h wall and was throttled
        print(f"{rid[:12]:14} {str(run.get('domain') or '')[:10]:10} "
              f"{str(run.get('status') or '')[:16]:16} {u.n_calls:>5} ${u.cost_usd:>8.3f} "
              f"{u.output_tokens:>10,} {u.cache_read_input_tokens:>11,} {u.total_tokens:>12,} {peak:>8}")

    tot = RunUsage(run_id="ALL")
    for _, u, _ in rows:  # grand total over ALL runs, not just the shown slice
        tot.n_calls += u.n_calls
        tot.num_turns += u.num_turns
        tot.cost_usd += u.cost_usd
        tot.input_tokens += u.input_tokens
        tot.output_tokens += u.output_tokens
        tot.cache_read_input_tokens += u.cache_read_input_tokens
        tot.cache_creation_input_tokens += u.cache_creation_input_tokens
    print("-" * 106)
    print(f"{'TOTAL':14} {len(rows):>10} runs       {tot.n_calls:>5} ${tot.cost_usd:>8.3f} "
          f"{tot.output_tokens:>10,} {tot.cache_read_input_tokens:>11,} {tot.total_tokens:>12,}")
    if limit and len(rows) > limit:
        print(f"(showing top {limit} of {len(rows)} runs by total tokens; grand total covers all)")
    print("\nnote: cost_usd reads ~0 under subscription auth; TOTAL tokens (esp. cache_read) is the "
          "honest signal for what the rolling usage window metered.")
    print("5h_peak = highest fraction of the 5-hour rolling window the SDK reported during the run; "
          "a trailing '!' means it hit the wall (a 'rejected'/throttled report).")


def main(argv: list[str]) -> None:
    args = argv[1:]
    if args and args[0] == "--top":
        _print_table(limit=int(args[1]) if len(args) > 1 else 10)
    elif args:
        _print_one(args[0])
    else:
        _print_table()


if __name__ == "__main__":
    main(sys.argv)
