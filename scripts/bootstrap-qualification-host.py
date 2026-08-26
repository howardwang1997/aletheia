#!/usr/bin/env python3
"""Plan or explicitly apply the disabled qualification host bootstrap."""

from aletheia.qualification_bootstrap import run_qualification_bootstrap_cli


def main() -> int:
    return run_qualification_bootstrap_cli()


if __name__ == "__main__":
    raise SystemExit(main())
