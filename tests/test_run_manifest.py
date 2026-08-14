"""PF-2: immutable, content-addressed run manifests and resume lineage."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from aletheia.reproducibility.manifest import (
    BudgetPolicy,
    ContentRef,
    GitIdentity,
    ManifestCompatibilityError,
    ModelIdentity,
    RunManifest,
    RunManifestPayload,
    RuntimeIdentity,
    content_sha256,
)

H64 = "a" * 64
H40 = "b" * 40


def _payload(**updates):
    data = {
        "run_id": "run-1",
        "git": GitIdentity(commit=H40, tree=H40, dirty=False, patch_sha256=H64),
        "runtime": RuntimeIdentity(
            python="3.11.0",
            platform="test",
            dependency_lock=ContentRef(name="deps", sha256=H64),
            python_sbom=ContentRef(name="sbom", sha256=H64),
            sandbox_image="sha256:test",
        ),
        "models": (ModelIdentity(role="orchestrator", vendor="x", transport="api", model="m"),),
        "prompts": (ContentRef(name="prompt", sha256=H64),),
        "tool_schemas": (ContentRef(name="tools", sha256=H64),),
        "domain_capabilities": (ContentRef(name="domain", sha256=H64),),
        "datasets": (),
        "split_ledgers": (),
        "safety_policy": ContentRef(name="safety", sha256=H64),
        "budget": BudgetPolicy(usd=1, gpu_hours=2, wall_clock_hours=3),
    }
    data.update(updates)
    return RunManifestPayload(**data)


def test_manifest_hash_is_canonical_and_order_independent():
    payload = _payload()
    assert content_sha256(payload) == content_sha256(payload.model_dump(mode="json"))


def test_manifest_rejects_forged_hash():
    with pytest.raises(ValidationError, match="manifest hash mismatch"):
        RunManifest(
            payload=_payload(),
            manifest_sha256="0" * 64,
            frozen_at=datetime.now(timezone.utc),
        )


def test_freeze_is_idempotent_but_environment_drift_requires_fork(monkeypatch, tmp_path):
    import aletheia.reproducibility.manifest as module

    payload = _payload()
    manifest_path = tmp_path / "run_manifest.v1.json"
    recorded = []
    monkeypatch.setattr(module, "build_run_manifest_payload", lambda *_args, **_kwargs: payload)
    monkeypatch.setattr(module, "_manifest_path", lambda _run_id: manifest_path)
    monkeypatch.setattr(
        "aletheia.memory.service.record_run_manifest",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )

    first = module.freeze_run_manifest("run-1", allow_dirty=True)
    second = module.freeze_run_manifest("run-1", allow_dirty=True)
    assert second.manifest_sha256 == first.manifest_sha256
    assert len(recorded) == 2  # file creation and idempotent ledger reconciliation

    changed = _payload(budget=BudgetPolicy(usd=9, gpu_hours=2, wall_clock_hours=3))
    monkeypatch.setattr(module, "build_run_manifest_payload", lambda *_args, **_kwargs: changed)
    with pytest.raises(ManifestCompatibilityError, match="fork a new run"):
        module.freeze_run_manifest("run-1", allow_dirty=True)


def test_dirty_release_manifest_fails_closed(monkeypatch, tmp_path):
    import aletheia.reproducibility.manifest as module

    dirty = _payload(git=GitIdentity(commit=H40, tree=H40, dirty=True, patch_sha256=H64))
    monkeypatch.setattr(module, "build_run_manifest_payload", lambda *_args, **_kwargs: dirty)
    monkeypatch.setattr(module, "_manifest_path", lambda _run_id: tmp_path / "manifest.json")
    with pytest.raises(ManifestCompatibilityError, match="dirty git tree"):
        module.freeze_run_manifest("run-1", allow_dirty=False)


def test_build_payload_records_data_split_and_sbom_identities(monkeypatch):
    import aletheia.reproducibility.manifest as module
    from aletheia.config import Settings

    refs = {
        "data": (ContentRef(name="dataset", sha256=H64),),
        "split": (ContentRef(name="split", sha256="c" * 64),),
    }
    monkeypatch.setattr("aletheia.memory.service.get_run", lambda _run_id: {"id": "run-1"})
    monkeypatch.setattr(module, "capture_git_identity", lambda: _payload().git)
    monkeypatch.setattr(module, "_dependency_lock_ref", lambda: ContentRef(name="deps", sha256=H64))
    monkeypatch.setattr(module, "_python_sbom_ref", lambda _run_id: ContentRef(name="sbom", sha256=H64))
    monkeypatch.setattr(module, "_prompt_refs", lambda: ())
    monkeypatch.setattr(module, "_tool_schema_refs", lambda: ())
    monkeypatch.setattr(module, "_domain_refs", lambda: ())
    monkeypatch.setattr(module, "_dataset_refs", lambda _run_id: refs["data"])
    monkeypatch.setattr(module, "_split_ledger_refs", lambda _run_id: refs["split"])

    payload = module.build_run_manifest_payload(
        "run-1", settings=Settings(_env_file=None)
    )
    assert payload.datasets == refs["data"]
    assert payload.split_ledgers == refs["split"]
    assert payload.runtime.python_sbom.name == "sbom"
