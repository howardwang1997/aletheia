#!/usr/bin/env python3
"""Run an independent F11 durable worker with explicitly registered Python handlers."""

from __future__ import annotations

import argparse
import asyncio
import signal
from collections.abc import Callable

from aletheia.db import require_schema_current
from aletheia.jobs.worker import DurableWorker, TaskHandler
from aletheia.migration.dynamic_loader import resolve_guarded_dynamic_attribute


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--worker-manifest-sha256", required=True)
    parser.add_argument(
        "--handler",
        action="append",
        required=True,
        metavar="TASK_TYPE=MODULE:CALLABLE",
        help="trusted handler registration; may be repeated",
    )
    parser.add_argument("--idle-seconds", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    return parser


def _resolve_handler(reference: str) -> tuple[str, TaskHandler]:
    try:
        task_type, target = reference.split("=", 1)
        module_name, attribute = target.split(":", 1)
    except ValueError as exc:
        raise ValueError("handler must use TASK_TYPE=MODULE:CALLABLE") from exc
    if not task_type or not module_name or not attribute:
        raise ValueError("handler task type, module, and callable cannot be empty")
    handler = resolve_guarded_dynamic_attribute(module_name, attribute)
    if not isinstance(handler, Callable):
        raise TypeError(f"handler is not callable: {target}")
    return task_type, handler


async def _run(args) -> None:
    handlers = dict(_resolve_handler(reference) for reference in args.handler)
    if len(handlers) != len(args.handler):
        raise ValueError("each task type may have only one handler")
    worker = DurableWorker(
        worker_id=args.worker_id,
        worker_manifest_sha256=args.worker_manifest_sha256,
        handlers=handlers,
    )
    if args.once:
        await worker.run_once()
        return

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stop.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX compatibility
            pass
    await worker.run_forever(stop=stop, idle_seconds=args.idle_seconds)


def main() -> int:
    args = _parser().parse_args()
    require_schema_current()
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
