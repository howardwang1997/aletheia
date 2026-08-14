"""DataAsset registry — the human-connected data layer + launch readiness gate.

A run launches only when every declared ``DataAsset`` is ``ready`` (see
``all_ready``). Two flows converge here:

  * **pull** — Aletheia calls ``request_data`` during scoping -> a ``needed`` asset
    surfaces on the dashboard; the human satisfies it (auto-download a benchmark,
    upload a file, or drop an API key) -> ``ready``.
  * **push** — the human registers/uploads data up front -> profiled -> ``ready``;
    the profile seeds the scoping conversation.
"""

from __future__ import annotations

from typing import Any

from aletheia.db import session_scope
from aletheia.memory.ledger import DataAsset

DATA_ROLES = ("primary", "external_validation")


def content_sha256(path: str) -> str:
    """Hash a file or a directory tree deterministically for immutable data provenance."""
    import hashlib
    from pathlib import Path

    root = Path(path)
    if not root.exists():
        raise FileNotFoundError(path)
    digest = hashlib.sha256()
    files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
    for item in files:
        relative = item.name if root.is_file() else item.relative_to(root).as_posix()
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "little"))
        digest.update(encoded)
        with item.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _to_dict(a: DataAsset) -> dict[str, Any]:
    return {
        "id": a.id,
        "run_id": a.run_id,
        "role": a.role or "primary",
        "source": a.source,
        "ref": a.ref,
        "target_column": a.target_column,
        "composition_column": a.composition_column,
        "feature_kind": a.feature_kind,
        "description": a.description,
        "status": a.status,
        "uri": a.uri,
        "content_sha256": a.content_sha256,
        "profile": a.profile_json,
        "requested_by": a.requested_by,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def register_dataset(
    run_id: str,
    source: str,
    *,
    role: str = "primary",
    ref: str | None = None,
    target_column: str | None = None,
    composition_column: str | None = None,
    feature_kind: str | None = None,
    description: str | None = None,
    uri: str | None = None,
    status: str = "needed",
    requested_by: str = "human",
    profile_json: dict[str, Any] | None = None,
    content_sha256: str | None = None,
) -> str:
    """Create a DataAsset row. Returns its id."""
    if role not in DATA_ROLES:
        raise ValueError(f"role must be one of {DATA_ROLES}, got {role!r}")
    with session_scope() as s:
        a = DataAsset(
            run_id=run_id,
            role=role,
            source=source,
            ref=ref,
            target_column=target_column,
            composition_column=composition_column,
            feature_kind=feature_kind,
            description=description,
            uri=uri,
            status=status,
            requested_by=requested_by,
            profile_json=profile_json,
            content_sha256=content_sha256,
        )
        s.add(a)
        s.flush()
        return a.id


def get_dataset(asset_id: str) -> dict[str, Any] | None:
    with session_scope() as s:
        a = s.get(DataAsset, asset_id)
        return _to_dict(a) if a else None


def list_datasets(run_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = (
            s.query(DataAsset)
            .filter(DataAsset.run_id == run_id)
            .order_by(DataAsset.created_at)
            .all()
        )
        return [_to_dict(a) for a in rows]


def update_dataset(asset_id: str, **fields: Any) -> dict[str, Any] | None:
    """Patch a DataAsset. Only non-None fields that are real columns are applied."""
    with session_scope() as s:
        a = s.get(DataAsset, asset_id)
        if a is None:
            return None
        for k, v in fields.items():
            if v is not None and hasattr(a, k):
                setattr(a, k, v)
        s.flush()
        return _to_dict(a)


def mark_ready(
    asset_id: str,
    *,
    uri: str | None = None,
    profile_json: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    return update_dataset(asset_id, status="ready", uri=uri, profile_json=profile_json)


def pending_datasets(run_id: str) -> list[dict[str, Any]]:
    """Datasets still blocking launch (anything not ``ready``)."""
    return [d for d in list_datasets(run_id) if d["status"] != "ready"]


def all_ready(run_id: str) -> bool:
    """Launch gate: True when no declared dataset is outstanding.

    Vacuously True when nothing is declared (a run may need no external data);
    the domain plugin's ``load_data`` is the backstop that fails loudly if data is
    actually required but missing.
    """
    return not pending_datasets(run_id)


def attach_local(
    run_id: str,
    path: str,
    *,
    asset_id: str | None = None,
    role: str = "primary",
    target_column: str | None = None,
    composition_column: str | None = None,
    feature_kind: str | None = "composition",
    description: str | None = None,
) -> dict[str, Any] | None:
    """Profile a local file OR directory and mark it ready.

    Source is inferred: a directory -> ``directory``, a file -> ``upload``. If
    ``asset_id`` is given it satisfies an existing (``needed``) request (pull);
    otherwise it creates a fresh ``ready`` asset (push).
    """
    import os

    if role not in DATA_ROLES:
        raise ValueError(f"role must be one of {DATA_ROLES}, got {role!r}")

    from aletheia.data.profiler import profile_path

    profile = profile_path(path, target_hint=target_column)
    content_hash = content_sha256(path)
    source = "directory" if os.path.isdir(path) else "upload"
    if asset_id:
        return update_dataset(
            asset_id,
            status="ready",
            source=source,
            uri=path,
            ref=path,
            profile_json=profile,
            target_column=target_column,
            composition_column=composition_column,
            feature_kind=feature_kind,
            description=description,
            role=role,
            content_sha256=content_hash,
        )
    new_id = register_dataset(
        run_id,
        source=source,
        role=role,
        ref=path,
        uri=path,
        target_column=target_column,
        composition_column=composition_column,
        feature_kind=feature_kind,
        description=description,
        status="ready",
        requested_by="human",
        profile_json=profile,
        content_sha256=content_hash,
    )
    return get_dataset(new_id)


def attach_url(
    run_id: str,
    url: str,
    *,
    asset_id: str | None = None,
    role: str = "primary",
    target_column: str | None = None,
    composition_column: str | None = None,
    feature_kind: str | None = "composition",
    description: str | None = None,
) -> dict[str, Any] | None:
    """Download an online dataset (extracting archives), profile it, mark it ready.
    The original URL is kept as ``ref``; the local materialized path is ``uri``."""
    from aletheia.data.loaders import download
    from aletheia.data.profiler import profile_path
    from aletheia.paths import run_data_dir

    local = download(url, run_data_dir(run_id))
    profile = profile_path(str(local), target_hint=target_column)
    content_hash = content_sha256(str(local))
    if asset_id:
        return update_dataset(
            asset_id,
            status="ready",
            source="url",
            ref=url,
            uri=str(local),
            profile_json=profile,
            target_column=target_column,
            composition_column=composition_column,
            feature_kind=feature_kind,
            description=description,
            role=role,
            content_sha256=content_hash,
        )
    new_id = register_dataset(
        run_id,
        source="url",
        role=role,
        ref=url,
        uri=str(local),
        target_column=target_column,
        composition_column=composition_column,
        feature_kind=feature_kind,
        description=description,
        status="ready",
        requested_by="human",
        profile_json=profile,
        content_sha256=content_hash,
    )
    return get_dataset(new_id)


def attach_upload(
    run_id: str,
    path: str,
    *,
    asset_id: str | None = None,
    role: str = "primary",
    target_column: str | None = None,
    composition_column: str | None = None,
    feature_kind: str | None = "composition",
    description: str | None = None,
) -> dict[str, Any] | None:
    """Back-compat single-file entry point (delegates to ``attach_local``)."""
    return attach_local(
        run_id,
        path,
        asset_id=asset_id,
        role=role,
        target_column=target_column,
        composition_column=composition_column,
        feature_kind=feature_kind,
        description=description,
    )
