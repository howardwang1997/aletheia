#!/usr/bin/env python3
"""Run one deployment-pinned Research Kernel controller process role."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal

from aletheia.schema_migrations import require_schema_exact
from aletheia.research_controller_runtime import (
    ResearchControllerRuntimeCycleReceipt,
    build_research_controller_runtime,
    load_research_controller_runtime_deployment,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--deployment-manifest",
        required=True,
        help="absolute path to the closed runtime deployment manifest",
    )
    parser.add_argument(
        "--deployment-manifest-sha256",
        required=True,
        help="external SHA-256 pin for the exact deployment-manifest bytes",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="perform startup recovery and one role cycle, then exit",
    )
    return parser


def _emit(value: object) -> None:
    if hasattr(value, "model_dump"):
        payload = value.model_dump(mode="json")
    else:  # pragma: no cover - all production receipts are closed Pydantic models
        raise TypeError("research-controller runtime emitted an unsupported value")
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")), flush=True)


async def _run(args: argparse.Namespace) -> None:
    deployment = load_research_controller_runtime_deployment(
        args.deployment_manifest,
        expected_file_sha256=args.deployment_manifest_sha256,
    )
    runtime = build_research_controller_runtime(deployment)
    _emit(await runtime.start())
    if args.once:
        _emit(await runtime.run_once())
        return

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stop.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX compatibility
            pass

    def emit_cycle(receipt: ResearchControllerRuntimeCycleReceipt) -> None:
        _emit(receipt)

    await runtime.run_forever(stop=stop, emit=emit_cycle)


def main() -> int:
    args = _parser().parse_args()
    require_schema_exact()
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
