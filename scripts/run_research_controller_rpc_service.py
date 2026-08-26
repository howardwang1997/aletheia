#!/usr/bin/env python3
"""Run one byte-pinned external controller-worker RPC service process."""

from __future__ import annotations

import argparse
import json
import signal
from threading import Event

from aletheia.research_controller_rpc_runtime import (
    build_controller_worker_rpc_server_runtime,
    load_controller_worker_rpc_server_deployment,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--deployment-manifest",
        required=True,
        help="absolute path to the closed RPC service deployment manifest",
    )
    parser.add_argument(
        "--deployment-manifest-sha256",
        required=True,
        help="external SHA-256 pin for the exact deployment-manifest bytes",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="open the socket for one bounded accept window, then exit",
    )
    return parser


def _emit(value: object) -> None:
    if not hasattr(value, "model_dump"):
        raise TypeError("RPC service runtime emitted an unsupported value")
    print(
        json.dumps(
            value.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ),
        flush=True,
    )


def main() -> int:
    args = _parser().parse_args()
    deployment = load_controller_worker_rpc_server_deployment(
        args.deployment_manifest,
        expected_file_sha256=args.deployment_manifest_sha256,
    )
    runtime = build_controller_worker_rpc_server_runtime(deployment)
    stop = Event()

    def request_stop(_signum, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        _emit(runtime.start())
        while not stop.is_set():
            receipt = runtime.serve_once()
            if receipt is not None:
                _emit(receipt)
            if args.once:
                break
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
