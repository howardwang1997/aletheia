"""Export the full conversation record (transcript) for a run from the event ledger.

Every model turn, tool call, tool result, and per-call token usage during a run is already
persisted to the ``events`` table — this exports it to durable, readable files so the precious
data survives a DB reset and can be read/re-ingested offline.

    conda run -n aletheia python scripts/export_transcript.py <run_id>    # one run
    conda run -n aletheia python scripts/export_transcript.py --last 5    # 5 most recent runs
    conda run -n aletheia python scripts/export_transcript.py --all       # every run with events

Writes ``artifacts/transcript_<run_id>.jsonl`` (lossless, full payloads) and
``artifacts/transcript_<run_id>.md`` (readable, lane-tagged, with a token/cost header).
"""

from __future__ import annotations

import sys

from aletheia.events.store import list_run_ids_with_events
from aletheia.memory.transcript import export_transcript


def _export(run_id: str) -> None:
    paths = export_transcript(run_id)
    print(f"  {run_id[:12]}  {paths['events']:>4} events  ->  {paths['md'].name}  +  {paths['jsonl'].name}")


def main(argv: list[str]) -> None:
    args = argv[1:]
    if not args:
        print("usage: export_transcript.py <run_id> | --last N | --all", file=sys.stderr)
        raise SystemExit(2)

    if args[0] == "--all":
        run_ids = list_run_ids_with_events()
    elif args[0] == "--last":
        run_ids = list_run_ids_with_events(limit=int(args[1]) if len(args) > 1 else 5)
    else:
        run_ids = [args[0]]

    print(f"exporting {len(run_ids)} run(s) to artifacts/ …")
    for rid in run_ids:
        try:
            _export(rid)
        except Exception as exc:  # noqa: BLE001 — keep going across a batch
            print(f"  {rid[:12]}  FAILED: {exc}", file=sys.stderr)
    print("done.")


if __name__ == "__main__":
    main(sys.argv)
