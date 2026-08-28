#!/usr/bin/env python3
"""Plan or explicitly prepare one immutable qualification Python runtime."""

from aletheia.execution.qualification_target_preparation import (
    run_qualification_python_runtime_preparation_cli,
)


def main() -> int:
    return run_qualification_python_runtime_preparation_cli()


if __name__ == "__main__":
    raise SystemExit(main())
