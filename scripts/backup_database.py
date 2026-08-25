"""Create a PostgreSQL custom-format backup and a machine-readable SHA-256 receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import make_url

from aletheia.config import get_settings


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_database(output: Path) -> dict[str, object]:
    pg_dump = shutil.which("pg_dump")
    if pg_dump is None:
        raise RuntimeError("pg_dump is not installed; install the PostgreSQL client before backup")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    url = make_url(get_settings().database_url)
    command = [pg_dump, "--format=custom", "--no-owner", "--file", str(output)]
    if url.host:
        command.extend(["--host", url.host])
    if url.port:
        command.extend(["--port", str(url.port)])
    if url.username:
        command.extend(["--username", url.username])
    command.append(url.database or "")
    env = None
    if url.password:
        import os

        env = dict(os.environ)
        env["PGPASSWORD"] = url.password
    subprocess.run(command, check=True, env=env)
    receipt = {
        "schema_version": 1,
        "database": url.database,
        "host": url.host,
        "backup_path": str(output),
        "bytes": output.stat().st_size,
        "sha256": _sha256(output),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "format": "postgresql-custom",
    }
    receipt_path = output.with_suffix(output.suffix + ".receipt.json")
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(backup_database(args.output), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
