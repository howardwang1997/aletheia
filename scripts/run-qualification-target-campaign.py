#!/usr/bin/env python3
"""Plan or explicitly run one destructive qualification-only target-host campaign."""

from aletheia.qualification_campaign import run_qualification_target_campaign_cli


def main() -> int:
    return run_qualification_target_campaign_cli()


if __name__ == "__main__":
    raise SystemExit(main())
