#!/usr/bin/env python3
"""Run the exact qualification-only terminal-outbox service role."""

from aletheia.qualification_service_runtime import (
    QualificationServiceRole,
    run_qualification_service_cli,
)


def main() -> int:
    return run_qualification_service_cli(role=QualificationServiceRole.OUTBOX)


if __name__ == "__main__":
    raise SystemExit(main())
