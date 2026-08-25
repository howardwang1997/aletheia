"""Adopt a verified pre-Alembic database without altering application tables."""

from __future__ import annotations

import json

from aletheia.schema_migrations import adopt_existing_baseline


def main() -> int:
    receipt = adopt_existing_baseline()
    print(json.dumps(receipt.__dict__, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
