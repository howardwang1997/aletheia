"""Activate one reviewed site-packages directory without running ``site`` hooks.

Qualification services deliberately start Python with ``-S -s -P``.  ``-S`` prevents ambient
``.pth`` files and ``sitecustomize`` modules from executing, but it also means third-party runtime
dependencies are absent from ``sys.path``.  The systemd deployment therefore supplies one exact
site-packages directory inside the reviewed Python tree.  This module appends it *after* the
standard library has been initialized, preserving stdlib precedence while keeping ``site``
disabled.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

QUALIFICATION_SITE_PACKAGES_ENV = "ALETHEIA_QUALIFICATION_SITE_PACKAGES"


class QualificationPythonBootstrapError(RuntimeError):
    """The frozen Python-home or site-packages boundary is absent or ambiguous."""


def _canonical_absolute(value: str, *, label: str) -> Path:
    candidate = Path(value)
    if (
        not value
        or not candidate.is_absolute()
        or str(candidate) != value
        or value == "/"
        or any(character in value for character in ("\x00", "\n", "\r"))
        or any(component in {"", ".", ".."} for component in value.split("/")[1:])
    ):
        raise QualificationPythonBootstrapError(f"{label} is not one canonical absolute path")
    return candidate


def activate_reviewed_site_packages() -> str | None:
    """Append the one deployment-pinned package directory when Python runs with ``-S``.

    Normal developer/test interpreters already ran ``site`` and need no mutation.  A production
    ``-S`` interpreter must provide exact ``PYTHONHOME`` and
    ``ALETHEIA_QUALIFICATION_SITE_PACKAGES`` assignments.  The path is derived again from the
    running Python major/minor version and every component is required to be a real directory,
    never a symlink.
    """

    if not sys.flags.no_site:
        return None

    home = _canonical_absolute(os.environ.get("PYTHONHOME", ""), label="qualification PYTHONHOME")
    site_packages = _canonical_absolute(
        os.environ.get(QUALIFICATION_SITE_PACKAGES_ENV, ""),
        label="qualification site-packages",
    )
    expected = (
        home
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / ("site-packages")
    )
    if site_packages != expected or Path(sys.prefix) != home:
        raise QualificationPythonBootstrapError(
            "qualification site-packages differs from the running reviewed Python home"
        )

    current = home
    try:
        home_metadata = current.lstat()
    except OSError as exc:
        raise QualificationPythonBootstrapError(
            "qualification Python home custody is unavailable"
        ) from exc
    if current.is_symlink() or not stat.S_ISDIR(home_metadata.st_mode):
        raise QualificationPythonBootstrapError(
            "qualification Python home custody is a symlink or non-directory"
        )
    for component in site_packages.relative_to(home).parts:
        current /= component
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise QualificationPythonBootstrapError(
                "qualification site-packages custody is unavailable"
            ) from exc
        if current.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise QualificationPythonBootstrapError(
                "qualification site-packages custody contains a symlink or non-directory"
            )

    normalized_paths = {
        str(Path(value)) for value in sys.path if value and Path(value).is_absolute()
    }
    if str(site_packages) in normalized_paths:
        raise QualificationPythonBootstrapError(
            "qualification site-packages was injected before reviewed bootstrap"
        )
    sys.path.append(str(site_packages))
    return str(site_packages)


__all__ = [
    "QUALIFICATION_SITE_PACKAGES_ENV",
    "QualificationPythonBootstrapError",
    "activate_reviewed_site_packages",
]
