#!/usr/bin/env python3
"""Freeze and verify immutable legacy snapshots during the PR-0 migration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import aletheia.migration.legacy as legacy_freezer_module
from aletheia.migration.legacy import (
    LegacyDataClass,
    LegacyDataRole,
    LegacyFreezeRequest,
    LegacyFreezerIdentity,
    LegacySnapshotManifest,
    build_legacy_freezer_identity,
    build_legacy_import_receipt,
    freeze_legacy_snapshot,
    legacy_exporter_code_sha256,
    verify_legacy_snapshot,
)

_GIT_SHA1 = re.compile(r"^[0-9a-f]{40}$")


def _read_json(path: Path) -> Any:
    resolved = path.expanduser().resolve(strict=True)
    return json.loads(resolved.read_text(encoding="utf-8"))


def _print_model(value: Any) -> None:
    payload = value.model_dump(mode="json", exclude_none=True)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _atomic_new_json(path: Path, value: Any) -> None:
    destination = path.expanduser().resolve(strict=False)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"refusing to replace frozen output: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        payload = value.model_dump(mode="json", exclude_none=True)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fchmod(handle.fileno(), 0o440)
            os.fsync(handle.fileno())
        os.link(temporary, destination)
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _git(exporter_root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(exporter_root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _normalized_entrypoint(value: str | Path) -> str:
    raw = value.as_posix() if isinstance(value, Path) else value
    if "\\" in raw:
        raise ValueError("exporter entrypoint must use POSIX separators")
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("exporter entrypoint must be a normalized repository-relative path")
    return path.as_posix()


def _tracked_entrypoint_identity(root: Path, entrypoint: str | Path) -> dict[str, Any]:
    relative_path = _normalized_entrypoint(entrypoint)
    worktree_path = root.joinpath(*PurePosixPath(relative_path).parts)
    cursor = root
    for part in PurePosixPath(relative_path).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("exporter entrypoint cannot traverse a symlink")
    if not worktree_path.exists():
        raise FileNotFoundError(f"exporter entrypoint is missing: {relative_path}")
    if not worktree_path.is_file():
        raise ValueError("exporter entrypoint must be a regular file")

    listing = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "-z", "--full-tree", "HEAD", "--", relative_path],
        check=True,
        capture_output=True,
    ).stdout
    records = [record for record in listing.split(b"\0") if record]
    if len(records) != 1:
        raise ValueError("exporter entrypoint must be exactly one file tracked at HEAD")
    header, separator, listed_path = records[0].partition(b"\t")
    if not separator or listed_path.decode("utf-8") != relative_path:
        raise ValueError("exporter entrypoint Git record does not match its canonical path")
    mode, object_type, object_id = header.decode("ascii").split()
    if mode == "120000":
        raise ValueError("exporter entrypoint cannot be a tracked symlink")
    if object_type != "blob" or not mode.startswith("100"):
        raise ValueError("exporter entrypoint must be a regular file tracked at HEAD")
    head_payload = subprocess.run(
        ["git", "-C", str(root), "cat-file", "blob", object_id],
        check=True,
        capture_output=True,
    ).stdout
    worktree_payload = worktree_path.read_bytes()
    return {
        "entrypoint": relative_path,
        "entrypoint_git_blob": object_id,
        "entrypoint_sha256": hashlib.sha256(head_payload).hexdigest(),
        "entrypoint_matches_head": worktree_payload == head_payload,
    }


def _git_exporter_identity(
    exporter_root: Path,
    exporter_entrypoint: str | Path,
) -> dict[str, Any]:
    root = exporter_root.expanduser().resolve(strict=True)
    top_level = Path(_git(root, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if top_level != root:
        raise ValueError("--exporter-root must be the Git repository top level")
    commit = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    if not _GIT_SHA1.fullmatch(commit) or not _GIT_SHA1.fullmatch(tree):
        raise RuntimeError("exporter Git identity is not a full SHA-1 commit/tree pair")
    entrypoint_identity = _tracked_entrypoint_identity(root, exporter_entrypoint)
    dirty = bool(_git(root, "status", "--porcelain", "--untracked-files=normal"))
    material = {
        "schema_name": "aletheia.legacy_exporter_git_entrypoint_identity",
        "schema_version": 1,
        "commit": commit,
        "tree": tree,
        **entrypoint_identity,
    }
    return {
        **material,
        "dirty": dirty,
        "exporter_execution_assurance": "operator_attested",
        "exporter_code_sha256": legacy_exporter_code_sha256(
            commit=commit,
            tree=tree,
            entrypoint=entrypoint_identity["entrypoint"],
            entrypoint_sha256=entrypoint_identity["entrypoint_sha256"],
        ),
    }


def _runtime_freezer_identity() -> LegacyFreezerIdentity:
    return build_legacy_freezer_identity(
        entrypoint="scripts/freeze_legacy_snapshot.py",
        source_files=(
            ("aletheia/migration/legacy.py", Path(legacy_freezer_module.__file__)),
            ("scripts/freeze_legacy_snapshot.py", Path(__file__)),
        ),
    )


def _require_committed_exporter(
    request: LegacyFreezeRequest,
    *,
    exporter_root: Path,
    exporter_entrypoint: str | Path,
) -> dict[str, Any]:
    identity = _git_exporter_identity(exporter_root, exporter_entrypoint)
    if identity["dirty"]:
        raise RuntimeError("refusing a snapshot from a dirty exporter repository")
    if not identity["entrypoint_matches_head"]:  # pragma: no cover - dirty also catches this
        raise RuntimeError("refusing an exporter entrypoint whose bytes differ from HEAD")
    if (
        request.exporter_identity_scheme != "git_tracked_entrypoint_v1"
    ):  # pragma: no cover - Literal
        raise ValueError("unsupported exporter identity scheme")
    if request.exporter_entrypoint != identity["entrypoint"]:
        raise ValueError("request exporter_entrypoint does not match --exporter-entrypoint")
    if request.exporter_entrypoint_sha256 != identity["entrypoint_sha256"]:
        raise ValueError("request exporter_entrypoint_sha256 does not match its HEAD blob")
    if request.exporter_code_sha256 != identity["exporter_code_sha256"]:
        raise ValueError(
            "request exporter_code_sha256 does not match the declared Git entrypoint binding"
        )
    if request.exporter_git_commit != identity["commit"]:
        raise ValueError("request exporter_git_commit does not match --exporter-root HEAD")
    if request.exporter_git_tree != identity["tree"]:
        raise ValueError("request exporter_git_tree does not match --exporter-root HEAD tree")
    return identity


def _exporter_identity(args: argparse.Namespace) -> None:
    identity = _git_exporter_identity(args.exporter_root, args.exporter_entrypoint)
    identity["acceptable_for_release_freeze"] = (
        not identity["dirty"] and identity["entrypoint_matches_head"]
    )
    print(json.dumps(identity, ensure_ascii=False, indent=2, sort_keys=True))


def _require_release_data_classes(request: LegacyFreezeRequest) -> None:
    if all(item.data_class is LegacyDataClass.DEV_FIXTURE for item in request.objects):
        return
    # This function exists to make the release/dev distinction explicit at the CLI boundary.  Both
    # paths still require a clean, identity-matched exporter repository.
    if not all(
        item.data_class in {LegacyDataClass.PUBLIC, LegacyDataClass.INTERNAL_SANITIZED}
        for item in request.objects
    ):
        raise ValueError("release snapshots cannot mix dev fixtures with release data classes")


def _freeze(args: argparse.Namespace) -> None:
    request = LegacyFreezeRequest.model_validate(_read_json(args.request))
    _require_release_data_classes(request)
    _require_committed_exporter(
        request,
        exporter_root=args.exporter_root,
        exporter_entrypoint=args.exporter_entrypoint,
    )
    manifest = freeze_legacy_snapshot(
        request,
        source_root=args.source_root,
        snapshot_store=args.snapshot_store,
        freezer_identity=_runtime_freezer_identity(),
        freezer_source_root=Path(__file__).resolve().parents[1],
    )
    if args.output_manifest is not None:
        _atomic_new_json(args.output_manifest, manifest)
    _print_model(manifest)


def _manifest(path: Path) -> LegacySnapshotManifest:
    return LegacySnapshotManifest.model_validate(_read_json(path))


def _verify(args: argparse.Namespace) -> None:
    manifest = _manifest(args.manifest)
    verify_legacy_snapshot(manifest, snapshot_store=args.snapshot_store)
    _print_model(manifest)


def _receipt(args: argparse.Namespace) -> None:
    manifest = _manifest(args.manifest)
    receipt = build_legacy_import_receipt(
        manifest,
        snapshot_store=args.snapshot_store,
        target_scope_id=args.target_scope,
        imported_by=args.imported_by,
        importer_code_sha256=args.importer_code_sha256,
        imported_at=datetime.now(timezone.utc),
        data_role=LegacyDataRole(args.data_role),
    )
    _print_model(receipt)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    freeze = subcommands.add_parser("freeze", help="create a new-or-identical CAS snapshot")
    freeze.add_argument("request", type=Path)
    freeze.add_argument("--exporter-root", type=Path, required=True)
    freeze.add_argument("--exporter-entrypoint", required=True)
    freeze.add_argument("--source-root", type=Path, required=True)
    freeze.add_argument("--snapshot-store", type=Path, required=True)
    freeze.add_argument(
        "--output-manifest",
        type=Path,
        help="atomically create an immutable pretty-JSON manifest after the clean-tree check",
    )
    freeze.set_defaults(handler=_freeze)

    identity = subcommands.add_parser(
        "exporter-identity",
        help="derive the request exporter hash from a Git commit/tree",
    )
    identity.add_argument("--exporter-root", type=Path, required=True)
    identity.add_argument("--exporter-entrypoint", required=True)
    identity.set_defaults(handler=_exporter_identity)

    verify = subcommands.add_parser("verify", help="rehash a frozen manifest and every CAS object")
    verify.add_argument("manifest", type=Path)
    verify.add_argument("--snapshot-store", type=Path, required=True)
    verify.set_defaults(handler=_verify)

    receipt = subcommands.add_parser(
        "receipt", help="issue a non-live engineering-only import receipt"
    )
    receipt.add_argument("manifest", type=Path)
    receipt.add_argument("--snapshot-store", type=Path, required=True)
    receipt.add_argument("--target-scope", required=True)
    receipt.add_argument("--imported-by", required=True)
    receipt.add_argument("--importer-code-sha256", required=True)
    receipt.add_argument(
        "--data-role",
        choices=[item.value for item in LegacyDataRole],
        default=LegacyDataRole.COMPATIBILITY_ONLY.value,
    )
    receipt.set_defaults(handler=_receipt)
    return parser


def main() -> int:
    args = _parser().parse_args()
    args.handler(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
