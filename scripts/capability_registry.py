"""Validate, freeze, inspect, and query F10 experiment-capability registries."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from aletheia.capabilities import (
    CapabilityPlanningQuery,
    CapabilityRegistrySnapshot,
    ExperimentCapabilityManifest,
    build_capability_registry_snapshot,
    plan_capability,
)


def _read(path: Path) -> Any:
    resolved = path.expanduser().resolve(strict=True)
    text = resolved.read_text(encoding="utf-8")
    if resolved.suffix.lower() in {".yaml", ".yml"}:
        return yaml.safe_load(text)
    return json.loads(text)


def _atomic_new_json(path: Path, value: object) -> Path:
    destination = path.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(f"refusing to replace frozen capability artifact: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            if hasattr(value, "model_dump"):
                value = value.model_dump(mode="json", exclude_none=True)  # type: ignore[union-attr]
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return destination


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _validate(args: argparse.Namespace) -> None:
    manifest = ExperimentCapabilityManifest.model_validate(_read(args.manifest))
    _print(
        {
            "capability_id": manifest.capability_id,
            "version": manifest.version,
            "lifecycle": manifest.lifecycle.value,
            "maximum_evidence_level": manifest.maximum_evidence_level.value,
            "manifest_sha256": manifest.manifest_sha256,
        }
    )


def _freeze(args: argparse.Namespace) -> None:
    manifests = tuple(
        ExperimentCapabilityManifest.model_validate(_read(path)) for path in args.manifest
    )
    snapshot = build_capability_registry_snapshot(
        registry_id=args.registry_id,
        manifests=manifests,
        created_at=datetime.now(timezone.utc),
    )
    destination = _atomic_new_json(args.output, snapshot)
    _print(
        {
            "output": str(destination),
            "registry_id": snapshot.registry_id,
            "snapshot_sha256": snapshot.snapshot_sha256,
            "manifest_count": len(snapshot.manifests),
        }
    )


def _inspect(args: argparse.Namespace) -> None:
    snapshot = CapabilityRegistrySnapshot.model_validate(_read(args.registry))
    _print(
        {
            "registry_id": snapshot.registry_id,
            "snapshot_sha256": snapshot.snapshot_sha256,
            "created_at": snapshot.created_at.isoformat(),
            "manifests": [
                {
                    "capability_id": item.capability_id,
                    "version": item.version,
                    "lifecycle": item.lifecycle.value,
                    "maximum_evidence_level": item.maximum_evidence_level.value,
                    "manifest_sha256": item.manifest_sha256,
                }
                for item in snapshot.manifests
            ],
        }
    )


def _query(args: argparse.Namespace) -> None:
    snapshot = CapabilityRegistrySnapshot.model_validate(_read(args.registry))
    query = CapabilityPlanningQuery.model_validate(_read(args.query))
    plan = plan_capability(snapshot=snapshot, query=query)
    _print(plan.model_dump(mode="json", exclude_none=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="F10 experiment capability registry")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-manifest")
    validate.add_argument("--manifest", type=Path, required=True)
    validate.set_defaults(handler=_validate)

    freeze = subparsers.add_parser("freeze")
    freeze.add_argument("--registry-id", required=True)
    freeze.add_argument("--manifest", type=Path, action="append", required=True)
    freeze.add_argument("--output", type=Path, required=True)
    freeze.set_defaults(handler=_freeze)

    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--registry", type=Path, required=True)
    inspect.set_defaults(handler=_inspect)

    query = subparsers.add_parser("query")
    query.add_argument("--registry", type=Path, required=True)
    query.add_argument("--query", type=Path, required=True)
    query.set_defaults(handler=_query)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
