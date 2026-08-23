#!/usr/bin/env python3
"""Read-only exporter for sanitized legacy run projection sources.

The exporter deliberately selects no event payload columns and emits no artifact
bytes.  Artifact files are streamed only to calculate opaque SHA-256 identities.
Its JSON output is intended for explicit, reviewed fixture versioning; the script
never writes to the repository or source workspaces.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import select

from aletheia.db import REPO_ROOT, engine, schema_status
from aletheia.memory.ledger import Event, Run


EXCLUDED_PATTERNS = (
    "**/__pycache__/**",
    "**/*.py",
    "**/*.pyc",
    "**/job.log",
    "**/payload.json",
    "**/transcript*",
)
_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_PRIVATE_NAME_MARKERS = (
    "credential",
    "database_url",
    "password",
    "private_key",
    "secret",
    "token",
)


def _file_identity(path: Path) -> tuple[str, int]:
    before = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"source object is not a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    after = path.lstat()
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise RuntimeError(f"source object changed while it was hashed: {path}")
    return digest.hexdigest(), before.st_size


def _is_excluded(relative_path: Path) -> bool:
    return (
        "__pycache__" in relative_path.parts
        or relative_path.suffix in {".py", ".pyc"}
        or relative_path.name in {"job.log", "payload.json"}
        or relative_path.name.startswith("transcript")
    )


def _artifact_role(path: Path) -> str:
    if path.suffix == ".joblib":
        return "opaque_model_blob"
    if path.suffix == ".png":
        return "plot"
    if path.suffix in {".bib", ".md"}:
        return "rendered_report"
    if path.suffix == ".json":
        return "structured_result"
    raise ValueError(f"unsupported artifact type requires a new export policy: {path}")


def _artifact_objects(workspace: Path) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for path in sorted(workspace.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"legacy workspace contains a symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(workspace)
        if _is_excluded(relative):
            continue
        lowered_parts = tuple(part.lower() for part in relative.parts)
        if any(marker in part for marker in _PRIVATE_NAME_MARKERS for part in lowered_parts):
            raise ValueError(f"private-looking source path is not exportable: {relative}")
        sha256, size_bytes = _file_identity(path)
        objects.append(
            {
                "relative_path": relative.as_posix(),
                "role": _artifact_role(path),
                "sha256": sha256,
                "size_bytes": size_bytes,
            }
        )
    if not objects:
        raise ValueError(f"legacy workspace has no exportable artifacts: {workspace.name}")
    return objects


def _resolve_workspace(workspace_root: Path, run_id: str) -> Path:
    """Bind artifacts to the requested run directory without following a run-level alias."""

    candidate = workspace_root / run_id
    if candidate.is_symlink():
        raise ValueError(f"legacy run workspace cannot be a symlink: {run_id}")
    workspace = candidate.resolve(strict=True)
    if not workspace.is_relative_to(workspace_root) or not workspace.is_dir():
        raise ValueError(f"invalid legacy workspace: {run_id}")
    return workspace


def export_sources(
    run_ids: Iterable[str],
    *,
    workspace_root: Path,
    exported_on: date,
) -> dict[str, Any]:
    """Build a sanitized source bundle without mutating the DB or workspaces."""

    ordered_run_ids = tuple(run_ids)
    if not ordered_run_ids or len(set(ordered_run_ids)) != len(ordered_run_ids):
        raise ValueError("run IDs must be a non-empty unique sequence")
    workspace_root = workspace_root.resolve(strict=True)
    cases: list[dict[str, Any]] = []
    with engine().connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        status = schema_status(connection)
        if status.current_revision is None:
            raise RuntimeError("source database has no Alembic revision")
        for run_id in ordered_run_ids:
            if _RUN_ID.fullmatch(run_id) is None:
                raise ValueError(f"invalid legacy run ID: {run_id!r}")
            run = connection.execute(
                select(Run.id, Run.domain, Run.status).where(Run.id == run_id)
            ).one_or_none()
            if run is None:
                raise ValueError(f"unknown legacy run: {run_id}")
            event_rows = connection.execute(
                select(Event.type, Event.event_key, Event.event_sha256)
                .where(Event.run_id == run_id)
                .order_by(Event.id.asc())
            ).all()
            if not event_rows:
                raise ValueError(f"legacy run has no events: {run_id}")
            if _SAFE_IDENTIFIER.fullmatch(run.domain) is None:
                raise ValueError(f"legacy run has an unsafe domain value: {run_id}")
            if _SAFE_IDENTIFIER.fullmatch(run.status) is None:
                raise ValueError(f"legacy run has an unsafe terminal status: {run_id}")
            event_types = [row.type for row in event_rows]
            if any(_SAFE_IDENTIFIER.fullmatch(event_type) is None for event_type in event_types):
                raise ValueError(f"legacy run has an unsafe event type: {run_id}")
            workspace = _resolve_workspace(workspace_root, run_id)
            cases.append(
                {
                    "artifact_source": {
                        "excluded_patterns": list(EXCLUDED_PATTERNS),
                        "objects": _artifact_objects(workspace),
                    },
                    "domain": run.domain,
                    "event_source": {
                        "event_key_present_count": sum(
                            row.event_key is not None for row in event_rows
                        ),
                        "event_sha256_present_count": sum(
                            row.event_sha256 is not None for row in event_rows
                        ),
                        "event_types": event_types,
                        "ordering": "events.id_ascending",
                    },
                    "run_id": run.id,
                    "terminal_status": run.status,
                }
            )

    script_path = Path(__file__).resolve(strict=True)
    script_sha256, _ = _file_identity(script_path)
    return {
        "capture": {
            "artifact_blob_content_included": False,
            "artifact_hashing": "streamed_opaque_bytes_sha256_without_deserialization",
            "event_fields_included": [
                "type",
                "event_key_presence",
                "event_sha256_presence",
            ],
            "event_payloads_included": False,
            "exported_on": exported_on.isoformat(),
            "read_only": True,
        },
        "cases": cases,
        "database_schema_revision": status.current_revision,
        "exporter": {
            "path": script_path.relative_to(REPO_ROOT).as_posix(),
            "sha256": script_sha256,
        },
        "schema_name": "aletheia.legacy_run_projection_sources",
        "schema_version": 1,
        "source_authority": "validating_postgresql_store_and_local_workspace",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_ids", nargs="+")
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=REPO_ROOT / "workspaces",
    )
    parser.add_argument(
        "--exported-on",
        type=date.fromisoformat,
        default=date.today(),
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    payload = export_sources(
        args.run_ids,
        workspace_root=args.workspace_root,
        exported_on=args.exported_on,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
