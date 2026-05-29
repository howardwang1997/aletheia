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


def _to_dict(a: DataAsset) -> dict[str, Any]:
    return {
        "id": a.id,
        "run_id": a.run_id,
        "source": a.source,
        "ref": a.ref,
        "target_column": a.target_column,
        "feature_kind": a.feature_kind,
        "description": a.description,
        "status": a.status,
        "uri": a.uri,
        "profile": a.profile_json,
        "requested_by": a.requested_by,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def register_dataset(
    run_id: str,
    source: str,
    *,
    ref: str | None = None,
    target_column: str | None = None,
    feature_kind: str | None = None,
    description: str | None = None,
    uri: str | None = None,
    status: str = "needed",
    requested_by: str = "human",
    profile_json: dict[str, Any] | None = None,
) -> str:
    """Create a DataAsset row. Returns its id."""
    with session_scope() as s:
        a = DataAsset(
            run_id=run_id,
            source=source,
            ref=ref,
            target_column=target_column,
            feature_kind=feature_kind,
            description=description,
            uri=uri,
            status=status,
            requested_by=requested_by,
            profile_json=profile_json,
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


def attach_upload(
    run_id: str,
    path: str,
    *,
    asset_id: str | None = None,
    target_column: str | None = None,
    feature_kind: str | None = "composition",
    description: str | None = None,
) -> dict[str, Any] | None:
    """Profile an uploaded file and mark it ready.

    If ``asset_id`` is given, it satisfies an existing (``needed``) request (pull);
    otherwise it creates a fresh ``ready`` upload asset (push).
    """
    from aletheia.data.profiler import profile_file

    profile = profile_file(path, target_hint=target_column)
    if asset_id:
        return update_dataset(
            asset_id,
            status="ready",
            uri=path,
            ref=path,
            profile_json=profile,
            target_column=target_column,
            feature_kind=feature_kind,
            description=description,
        )
    new_id = register_dataset(
        run_id,
        source="upload",
        ref=path,
        uri=path,
        target_column=target_column,
        feature_kind=feature_kind,
        description=description,
        status="ready",
        requested_by="human",
        profile_json=profile,
    )
    return get_dataset(new_id)
