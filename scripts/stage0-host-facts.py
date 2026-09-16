#!/usr/bin/env python3
"""Capture the v100ts host facts that later ARL-2 commissioning JSON pins.

One-shot, read-only: prints one canonical-JSON facts sheet to stdout and
exits 0.  Every field is sampled from the live host (never hand-typed):
uname, free uid/gid slots below the ARL-1 range, /run layout, conda env
identity for arl2-cuprate, the PostgreSQL cluster identity behind
/run/postgresql, and disk headroom at /opt/aletheia.

Run on the box as root:

    PYTHONDONTWRITEBYTECODE=1 /opt/aletheia/python/bin/python \
        scripts/stage0-host-facts.py > host_facts.json
"""

from __future__ import annotations

import json
import os
import platform
import pwd
import stat
import subprocess
import sys
import time


def _safe_stat(path: str) -> dict | None:
    try:
        info = os.stat(path)
        return {
            "path": path,
            "st_dev": info.st_dev,
            "st_ino": info.st_ino,
            "st_uid": info.st_uid,
            "st_gid": info.st_gid,
            "st_mode": oct(stat.S_IMODE(info.st_mode)),
        }
    except OSError:
        return None


def _free_id_slots(used: set[int], floor: int, ceiling: int, count: int) -> list[int]:
    free = [i for i in range(floor, ceiling) if i not in used]
    if len(free) < count:
        raise SystemExit(f"host has fewer than {count} free ids in [{floor}, {ceiling})")
    return free[:count]


def main() -> int:
    used_uids = {entry.pw_uid for entry in pwd.getpwall()}
    try:
        import grp

        used_gids = {entry.gr_gid for entry in grp.getgrall()}
    except ImportError:  # pragma: no cover - Linux always has grp
        used_gids = set()

    facts = {
        "schema_name": "aletheia.arl2_stage0_host_facts",
        "schema_version": 1,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "uname": " ".join(platform.uname()),
        "python": sys.version.split()[0],
        "free_uid_slots": _free_id_slots(used_uids, 2300, 2400, 6),
        "free_gid_slots": _free_id_slots(used_gids, 2300, 2400, 2),
        "arl1_uid_range_observed": sorted(
            uid for uid in used_uids if 2200 <= uid <= 2299
        ),
        "run_postgresql": _safe_stat("/run/postgresql/.s.PGSQL.5432"),
        "aletheia_root": _safe_stat("/opt/aletheia"),
        "release_8cf0036_a1": _safe_stat("/opt/aletheia/release-8cf0036-a1"),
        "arl2_dryrun_root": _safe_stat("/opt/aletheia/arl2-dryrun"),
    }

    env_python = "/root/miniconda3/envs/arl2-cuprate/bin/python"
    probe = (
        "import json\n"
        "mods = {}\n"
        "for name in ('numpy','pandas','sklearn','scipy','pydantic',"
        "'cryptography','matminer','pymatgen.core'):\n"
        "    try:\n"
        "        module = __import__(name)\n"
        "        mods[name] = getattr(module, '__version__', '?')\n"
        "    except ImportError:\n"
        "        mods[name] = None\n"
        "print(json.dumps(mods))\n"
    )
    env_check = subprocess.run(
        [env_python, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )
    facts["arl2_cuprate_env_versions"] = json.loads(env_check.stdout)

    pg_probe = (
        "import psycopg\n"
        "conn = psycopg.connect('host=/run/postgresql user=root "
        "dbname=postgres')\n"
        "row = conn.execute('select version()').fetchone()\n"
        "print(row[0].split()[1])\n"
        "conn.close()\n"
    )
    control_plane = "/opt/aletheia/python/bin/python"
    pg_check = subprocess.run(
        [control_plane, "-c", pg_probe],
        capture_output=True,
        text=True,
    )
    facts["postgresql_version"] = pg_check.stdout.strip() if pg_check.returncode == 0 else None

    statvfs = os.statvfs("/opt/aletheia")
    facts["opt_aletheia_free_bytes"] = statvfs.f_bavail * statvfs.f_frsize
    facts["opt_aletheia_total_bytes"] = statvfs.f_blocks * statvfs.f_frsize

    sys.stdout.write(json.dumps(facts, indent=1, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
