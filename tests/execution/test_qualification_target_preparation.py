from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

import aletheia.execution.qualification_target_preparation as preparation
from aletheia.execution.qualification_deployment import QualificationExpectedRootExecutable
from aletheia.execution.schemas import canonical_json_bytes


NOW = datetime(2026, 8, 29, 2, 0, 0, tzinfo=timezone.utc)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _native_sources() -> tuple[preparation.QualificationNativeRuntimeSourceV1, ...]:
    values = (
        ("ld-linux-x86-64.so.2", "elf_interpreter", 0o555, 0o555),
        ("libc.so.6", "shared_library", 0o644, 0o444),
        ("libdl.so.2", "shared_library", 0o644, 0o444),
        ("libm.so.6", "shared_library", 0o644, 0o444),
        ("libpthread.so.0", "shared_library", 0o644, 0o444),
        ("libresolv.so.2", "shared_library", 0o644, 0o444),
        ("libutil.so.1", "shared_library", 0o644, 0o444),
    )
    return tuple(
        preparation.QualificationNativeRuntimeSourceV1(
            source_path=f"/usr/lib/x86_64-linux-gnu/{name}",
            source_sha256=_sha(name),
            source_mode=source_mode,
            target_relative_path=f"lib/{name}",
            target_mode=target_mode,
            role=role,
        )
        for name, role, source_mode, target_mode in sorted(values)
    )


def _request(**updates: object) -> preparation.QualificationPythonRuntimePreparationRequestV1:
    values: dict[str, object] = {
        "source_environment_root": "/opt/aletheia-build/qualification-env",
        "target_environment_root": "/opt/aletheia/python",
        "source_patchelf": QualificationExpectedRootExecutable(
            path="/opt/aletheia-build/qualification-env/bin/patchelf",
            reviewed_sha256=_sha("patchelf"),
            expected_mode=0o755,
        ),
        "site_packages_relative_path": "lib/python3.11/site-packages",
        "native_sources": _native_sources(),
        "probe_modules": (
            "cryptography.hazmat.bindings._rust",
            "pgvector",
            "psycopg",
            "pydantic",
            "pydantic_settings",
            "sqlalchemy",
            "yaml",
        ),
        "requested_at": NOW,
    }
    values.update(updates)
    return preparation.QualificationPythonRuntimePreparationRequestV1(**values)


def test_request_and_plan_are_canonical_and_non_authoritative() -> None:
    request = _request()
    plan = preparation.build_qualification_python_runtime_preparation_plan(request)

    assert request.request_id == f"qpr_{request.identity_sha256[:32]}"
    assert request.file_sha256 == hashlib.sha256(canonical_json_bytes(request)).hexdigest()
    assert plan.plan_id == f"qpp_{plan.identity_sha256[:32]}"
    assert plan.request_sha256 == request.file_sha256
    assert plan.native_target_relative_paths == tuple(
        source.target_relative_path for source in request.native_sources
    )
    assert plan.mutation_enabled is False
    assert plan.scientific_admission_allowed is False


@pytest.mark.parametrize(
    ("update", "message"),
    (
        (
            {"target_environment_root": "/opt/aletheia-build/qualification-env/child"},
            "source, target",
        ),
        ({"python_relative_path": "bin/python3"}, "Python"),
        ({"site_packages_relative_path": "lib/python3.11"}, "site-packages"),
        (
            {
                "native_sources": tuple(
                    source.model_copy(update={"role": "shared_library", "target_mode": 0o444})
                    for source in _native_sources()
                )
            },
            "exactly one ELF",
        ),
        ({"probe_modules": ("sqlalchemy", "pydantic")}, "canonical"),
    ),
)
def test_request_rejects_ambiguous_runtime_boundary(
    update: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        _request(**update)


def test_source_inventory_is_stable_and_rejects_an_external_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    library = source / "lib"
    library.mkdir()
    payload = library / "payload.so"
    payload.write_bytes(b"one")
    (source / "alias.so").symlink_to("lib/payload.so")

    first = preparation._source_inventory_sha256(  # noqa: SLF001
        source,
        expected_owner_uid=os.getuid(),
    )
    second = preparation._source_inventory_sha256(  # noqa: SLF001
        source,
        expected_owner_uid=os.getuid(),
    )
    assert first == second

    payload.write_bytes(b"two")
    assert (
        preparation._source_inventory_sha256(  # noqa: SLF001
            source,
            expected_owner_uid=os.getuid(),
        )
        != first
    )

    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    (source / "external.so").symlink_to(outside)
    with pytest.raises(preparation.QualificationTargetPreparationError, match="escaped"):
        preparation._source_inventory_sha256(  # noqa: SLF001
            source,
            expected_owner_uid=os.getuid(),
        )


def test_source_inventory_rejects_recursive_directory_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    child = source / "child"
    child.mkdir()
    (child / "cycle").symlink_to("..", target_is_directory=True)

    with pytest.raises(preparation.QualificationTargetPreparationError, match="recursive"):
        preparation._source_inventory_sha256(  # noqa: SLF001
            source,
            expected_owner_uid=os.getuid(),
        )


def test_copy_and_normalize_materializes_aliases_and_breaks_hardlinks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    package = source / "package"
    package.mkdir()
    first = package / "module.py"
    first.write_bytes(b"value = 1\n")
    first.chmod(0o644)
    (package / "hardlink.py").hardlink_to(first)
    (source / "module-alias.py").symlink_to("package/module.py")
    (source / "package-alias").symlink_to("package", target_is_directory=True)

    target = tmp_path / "target"
    shutil.copytree(source, target, symlinks=False, copy_function=shutil.copy2)
    preparation._normalize_materialized_tree(  # noqa: SLF001
        target,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )

    files = tuple(target.rglob("*.py"))
    assert len(files) == 5
    assert all(not path.is_symlink() for path in target.rglob("*"))
    assert all(path.stat().st_nlink == 1 for path in files)
    assert all(path.stat().st_mode & 0o777 == 0o444 for path in files)
    assert all(
        path.stat().st_mode & 0o777 == 0o555
        for path in (target, target / "package", target / "package-alias")
    )


def test_failed_tree_cleanup_requires_the_exact_created_inode(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    created = target.lstat()
    preparation._remove_exact_preparation_tree(  # noqa: SLF001
        target,
        expected_device=created.st_dev,
        expected_inode=created.st_ino,
    )
    assert not target.exists()

    original = tmp_path / "original"
    target.mkdir()
    original_metadata = target.lstat()
    target.rename(original)
    target.mkdir()
    with pytest.raises(preparation.QualificationTargetPreparationError, match="rebound"):
        preparation._remove_exact_preparation_tree(  # noqa: SLF001
            target,
            expected_device=original_metadata.st_dev,
            expected_inode=original_metadata.st_ino,
        )
    assert target.is_dir()


def test_loader_requires_exact_canonical_bytes_and_rejects_duplicate_keys(
    tmp_path: Path,
) -> None:
    request = _request()
    path = tmp_path / "request.json"
    payload = canonical_json_bytes(request)
    path.write_bytes(payload)

    assert (
        preparation.load_qualification_python_runtime_preparation_request(
            path,
            expected_file_sha256=hashlib.sha256(payload).hexdigest(),
            require_root_custody=False,
        )
        == request
    )

    duplicate = payload.replace(b'{"', b'{"schema_version":1,"', 1)
    path.write_bytes(duplicate)
    with pytest.raises(preparation.QualificationTargetPreparationError, match="invalid"):
        preparation.load_qualification_python_runtime_preparation_request(
            path,
            expected_file_sha256=hashlib.sha256(duplicate).hexdigest(),
            require_root_custody=False,
        )


def test_emit_writes_exactly_one_canonical_json_line(
    capfdbinary: pytest.CaptureFixture[bytes],
) -> None:
    plan = preparation.build_qualification_python_runtime_preparation_plan(_request())

    preparation._emit(plan)  # noqa: SLF001

    captured = capfdbinary.readouterr()
    assert captured.out == canonical_json_bytes(plan) + b"\n"
    assert captured.err == b""


def test_runtime_probe_binds_and_loads_relocated_timezone_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "python").write_bytes(b"python")
    site_packages = root / "lib" / "python3.11" / "site-packages"
    site_packages.mkdir(parents=True)
    timezone_data = root / "share" / "zoneinfo"
    timezone_data.mkdir(parents=True)
    invocation: dict[str, object] = {}

    def run(argv, **kwargs):
        invocation.update({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, 0, stdout="[]\n", stderr="")

    monkeypatch.setattr(preparation.subprocess, "run", run)

    assert (
        preparation._probe_runtime(  # noqa: SLF001
            root=root,
            python_relative_path="bin/python",
            site_packages_relative_path="lib/python3.11/site-packages",
            modules=("psycopg",),
        )
        == ()
    )
    assert invocation["env"]["PYTHONTZPATH"] == str(timezone_data)
    assert "zoneinfo.ZoneInfo('UTC')" in invocation["argv"][-1]
    assert "zoneinfo.ZoneInfo('Etc/UTC')" in invocation["argv"][-1]


def test_runtime_probe_rejects_missing_timezone_data(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "python").write_bytes(b"python")

    with pytest.raises(preparation.QualificationTargetPreparationError, match="timezone data"):
        preparation._probe_runtime(  # noqa: SLF001
            root=root,
            python_relative_path="bin/python",
            site_packages_relative_path="lib/python3.11/site-packages",
            modules=("psycopg",),
        )


def test_apply_fails_before_mutation_outside_linux_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preparation.sys, "platform", "darwin")
    with pytest.raises(preparation.QualificationTargetPreparationError, match="Linux root"):
        preparation.prepare_qualification_python_runtime(_request())
