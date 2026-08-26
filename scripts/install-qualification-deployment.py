#!/usr/bin/env python3
"""Plan or explicitly apply one disabled qualification file installation."""

from aletheia.qualification_installer import run_qualification_installer_cli


def main() -> int:
    return run_qualification_installer_cli()


if __name__ == "__main__":
    raise SystemExit(main())
