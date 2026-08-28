#!/usr/bin/env python3
"""Run the exact qualification-only shared-workspace service role."""

from aletheia.qualification_python_bootstrap import activate_reviewed_site_packages

activate_reviewed_site_packages()

from aletheia.qualification_service_runtime import (  # noqa: E402
    QualificationServiceRole,
    run_qualification_service_cli,
)


def main() -> int:
    return run_qualification_service_cli(role=QualificationServiceRole.WORKSPACE)


if __name__ == "__main__":
    raise SystemExit(main())
