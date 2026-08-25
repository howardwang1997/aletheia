"""Run Manifest v1: immutable identity for every formal scientific run.

The manifest is frozen before the first scientific action. It records exact code/config/model/data
identities while deliberately excluding credentials and mutable result fields. The JSON filename,
ledger row, and payload all bind to one SHA-256 content identity.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aletheia.config import Settings, get_settings
from aletheia.config.settings import REPO_ROOT
from aletheia.paths import run_artifacts_dir

MANIFEST_SCHEMA = "aletheia.run_manifest"
MANIFEST_VERSION = 1


class ManifestCompatibilityError(RuntimeError):
    """A resume or freeze request conflicts with an already committed manifest."""


def canonical_json_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)

    def without_none(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: without_none(val) for key, val in item.items() if val is not None}
        if isinstance(item, (list, tuple)):
            return [without_none(val) for val in item]
        return item

    return json.dumps(
        without_none(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def content_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class ContentRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    uri: str | None = None
    media_type: str | None = None


class GitIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    dirty: bool
    patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RuntimeIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    python: str
    platform: str
    dependency_lock: ContentRef
    python_sbom: ContentRef
    sandbox_image: str


class ModelIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str
    vendor: str
    transport: str
    model: str


class BudgetPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    usd: float
    gpu_hours: float
    wall_clock_hours: float
    token_cap: int | None = None


class RunManifestPayload(BaseModel):
    """Hashable manifest payload; timestamps and its own hash are intentionally outside it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["aletheia.run_manifest"] = MANIFEST_SCHEMA
    schema_version: Literal[1] = MANIFEST_VERSION
    run_id: str = Field(min_length=1)
    parent_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    git: GitIdentity
    runtime: RuntimeIdentity
    models: tuple[ModelIdentity, ...]
    prompts: tuple[ContentRef, ...]
    tool_schemas: tuple[ContentRef, ...]
    domain_capabilities: tuple[ContentRef, ...]
    datasets: tuple[ContentRef, ...]
    split_ledgers: tuple[ContentRef, ...] = ()
    evaluator: ContentRef | None = None
    safety_policy: ContentRef
    budget: BudgetPolicy
    approvals: tuple[ContentRef, ...] = ()


class RunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    payload: RunManifestPayload
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frozen_at: datetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _hash_matches_payload(self) -> "RunManifest":
        actual = content_sha256(self.payload)
        if actual != self.manifest_sha256:
            raise ValueError(
                f"manifest hash mismatch: declared {self.manifest_sha256}, computed {actual}"
            )
        return self


def _run_git(*args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout


def _git_output_or_empty(*args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout if result.returncode in {0, 1} else b""


def capture_git_identity() -> GitIdentity:
    commit = _run_git("rev-parse", "HEAD").decode().strip()
    tree = _run_git("rev-parse", "HEAD^{tree}").decode().strip()
    unstaged_patch = _git_output_or_empty("diff", "--binary")
    staged_patch = _git_output_or_empty("diff", "--binary", "--cached", "HEAD")
    untracked_paths = sorted(
        p for p in _run_git("ls-files", "--others", "--exclude-standard").decode().splitlines() if p
    )
    digest = hashlib.sha256()
    digest.update(b"tracked\0")
    digest.update(unstaged_patch)
    digest.update(b"staged\0")
    digest.update(staged_patch)
    for rel in untracked_paths:
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        digest.update(b"untracked\0")
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    dirty = bool(unstaged_patch or staged_patch or untracked_paths)
    return GitIdentity(
        commit=commit,
        tree=tree,
        dirty=dirty,
        patch_sha256=digest.hexdigest(),
    )


def file_ref(name: str, path: Path, *, media_type: str | None = None) -> ContentRef:
    resolved = path.resolve()
    try:
        uri = str(resolved.relative_to(REPO_ROOT))
    except ValueError:
        uri = str(resolved)
    return ContentRef(
        name=name,
        sha256=hashlib.sha256(resolved.read_bytes()).hexdigest(),
        uri=uri,
        media_type=media_type,
    )


def json_ref(name: str, value: Any) -> ContentRef:
    return ContentRef(name=name, sha256=content_sha256(value), media_type="application/json")


def _dependency_lock_ref() -> ContentRef:
    paths = [
        REPO_ROOT / "environment.yml",
        REPO_ROOT / "pyproject.toml",
        REPO_ROOT / "frontend" / "package-lock.json",
    ]
    paths = [path for path in paths if path.is_file()]
    payload = [
        {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in paths
    ]
    return json_ref("python-and-conda-dependencies", payload)


def _python_sbom_ref(run_id: str) -> ContentRef:
    """Write an exact environment inventory without shelling out or exposing credentials."""
    distributions = sorted(
        {
            (
                (dist.metadata.get("Name") or "unknown").lower(),
                dist.version,
            )
            for dist in importlib.metadata.distributions()
        }
    )
    conda_packages: list[dict[str, Any]] = []
    conda_prefix = os.environ.get("CONDA_PREFIX")
    if conda_prefix:
        for record_path in sorted((Path(conda_prefix) / "conda-meta").glob("*.json")):
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            conda_packages.append(
                {
                    "name": record.get("name"),
                    "version": record.get("version"),
                    "build": record.get("build"),
                    "channel": record.get("channel"),
                    "subdir": record.get("subdir"),
                }
            )
    sbom = {
        "schema_name": "aletheia.python_sbom",
        "schema_version": 1,
        "python": sys.version,
        "distributions": [{"name": name, "version": version} for name, version in distributions],
        "conda_packages": conda_packages,
    }
    path = run_artifacts_dir(run_id) / "python_sbom.v1.json"
    path.write_text(json.dumps(sbom, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return file_ref("python-environment-sbom", path, media_type="application/json")


def _prompt_refs() -> tuple[ContentRef, ...]:
    files = (
        REPO_ROOT / "aletheia" / "orchestrator" / "client.py",
        REPO_ROOT / "aletheia" / "orchestrator" / "session.py",
        REPO_ROOT / "aletheia" / "orchestrator" / "worker.py",
        REPO_ROOT / "aletheia" / "coder" / "demonstration.py",
    )
    return tuple(file_ref(f"prompt-source:{p.name}", p, media_type="text/x-python") for p in files)


def _tool_schema_refs() -> tuple[ContentRef, ...]:
    files = (
        REPO_ROOT / "aletheia" / "orchestrator" / "tools.py",
        REPO_ROOT / "aletheia" / "orchestrator" / "gate.py",
        REPO_ROOT / "aletheia" / "orchestrator" / "openai_runtime.py",
        REPO_ROOT / "aletheia" / "orchestrator" / "codex_runtime.py",
    )
    return tuple(file_ref(f"tool-contract:{p.name}", p, media_type="text/x-python") for p in files)


def _domain_refs() -> tuple[ContentRef, ...]:
    domain_root = REPO_ROOT / "aletheia" / "domains"
    refs: list[ContentRef] = []
    for path in sorted(domain_root.rglob("*.py")):
        if "__pycache__" not in path.parts:
            refs.append(file_ref(f"domain:{path.relative_to(domain_root)}", path, media_type="text/x-python"))
    return tuple(refs)


def _model_identities(settings: Settings) -> tuple[ModelIdentity, ...]:
    models = [
        ModelIdentity(
            role="orchestrator",
            vendor=settings.orchestrator_vendor,
            transport=settings.orchestrator_transport,
            model=settings.orchestrator_model,
        ),
        ModelIdentity(
            role="embedding",
            vendor="local" if settings.embedding_backend == "local" else "deterministic-test",
            transport=settings.embedding_backend,
            model=settings.embedding_model,
        ),
    ]
    models.extend(
        ModelIdentity(role=f"critic:{critic.id}", vendor=critic.id, transport=critic.transport, model=critic.model)
        for critic in settings.critics.active
    )
    return tuple(models)


def _dataset_refs(run_id: str) -> tuple[ContentRef, ...]:
    from aletheia.data.registry import list_datasets

    refs: list[ContentRef] = []
    for asset in sorted(list_datasets(run_id), key=lambda row: row["id"]):
        identity = asset.get("content_sha256")
        if not identity:
            identity = content_sha256(
                {
                    key: asset.get(key)
                    for key in (
                        "id",
                        "role",
                        "source",
                        "ref",
                        "target_column",
                        "composition_column",
                        "feature_kind",
                        "status",
                    )
                }
            )
        refs.append(
            ContentRef(
                name=f"dataset:{asset['id']}",
                sha256=identity,
                uri=asset.get("uri") or asset.get("ref"),
                media_type="application/x-aletheia-data-asset",
            )
        )
    return tuple(refs)


def _split_ledger_refs(run_id: str) -> tuple[ContentRef, ...]:
    from aletheia.memory.service import (
        get_campaign_split_ledger,
        get_external_validation_ledger,
    )

    refs: list[ContentRef] = []
    campaign = get_campaign_split_ledger(run_id)
    if campaign is not None:
        immutable = {
            "dataset_fingerprint": campaign["dataset_fingerprint"],
            "row_identity_hash": campaign["row_identity_hash"],
            "plan": campaign["plan"],
        }
        refs.append(
            ContentRef(
                name="campaign-split-ledger",
                sha256=content_sha256(immutable),
                uri=f"ledger://campaign_split_ledgers/{campaign['id']}",
                media_type="application/x-aletheia-split-ledger",
            )
        )
    external = get_external_validation_ledger(run_id)
    if external is not None:
        immutable = {
            "data_asset_id": external["data_asset_id"],
            "dataset_fingerprint": external["dataset_fingerprint"],
            "row_identity_hash": external["row_identity_hash"],
            "provenance": external["provenance"],
        }
        refs.append(
            ContentRef(
                name="external-validation-ledger",
                sha256=content_sha256(immutable),
                uri=f"ledger://external_validation_ledgers/{external['id']}",
                media_type="application/x-aletheia-external-validation-ledger",
            )
        )
    return tuple(refs)


def build_run_manifest_payload(
    run_id: str,
    *,
    parent_manifest_sha256: str | None = None,
    evaluator: ContentRef | None = None,
    approvals: tuple[ContentRef, ...] = (),
    settings: Settings | None = None,
) -> RunManifestPayload:
    settings = settings or get_settings()
    from aletheia.memory.service import get_run

    run = get_run(run_id)
    if run is None:
        raise ManifestCompatibilityError(f"cannot freeze manifest: run {run_id!r} does not exist")
    safety = {
        key: getattr(settings, key)
        for key in (
            "authored_code_backend",
            "allow_unsafe_host_authored_code",
            "sandbox_allow_network",
            "sandbox_cpu_seconds",
            "sandbox_max_memory_mb",
            "sandbox_output_limit_bytes",
            "campaign_seal_v2_enabled",
            "campaign_family_alpha",
            "campaign_final_alpha",
            "campaign_external_validation_required",
            "min_review_vendors",
        )
    }
    policy_usd = run.get("budget_cap_usd")
    policy_gpu = run.get("gpu_hours_cap")
    return RunManifestPayload(
        run_id=run_id,
        parent_manifest_sha256=parent_manifest_sha256,
        git=capture_git_identity(),
        runtime=RuntimeIdentity(
            python=sys.version.split()[0],
            platform=platform.platform(),
            dependency_lock=_dependency_lock_ref(),
            python_sbom=_python_sbom_ref(run_id),
            sandbox_image=settings.sandbox_docker_image,
        ),
        models=_model_identities(settings),
        prompts=_prompt_refs(),
        tool_schemas=_tool_schema_refs(),
        domain_capabilities=_domain_refs(),
        datasets=_dataset_refs(run_id),
        split_ledgers=_split_ledger_refs(run_id),
        evaluator=evaluator,
        safety_policy=json_ref("runtime-safety-policy", safety),
        budget=BudgetPolicy(
            usd=float(policy_usd if policy_usd is not None else settings.budget_usd),
            gpu_hours=float(policy_gpu if policy_gpu is not None else settings.budget_gpu_hours),
            wall_clock_hours=settings.wall_clock_hours,
            token_cap=settings.token_cap_per_run,
        ),
        approvals=approvals,
    )


def _manifest_path(run_id: str) -> Path:
    return run_artifacts_dir(run_id) / "run_manifest.v1.json"


def load_run_manifest(run_id: str) -> RunManifest | None:
    path = _manifest_path(run_id)
    if not path.is_file():
        return None
    return RunManifest.model_validate_json(path.read_text(encoding="utf-8"))


def freeze_run_manifest(
    run_id: str,
    *,
    parent_manifest_sha256: str | None = None,
    evaluator: ContentRef | None = None,
    approvals: tuple[ContentRef, ...] = (),
    allow_dirty: bool | None = None,
    settings: Settings | None = None,
) -> RunManifest:
    """Freeze one immutable manifest; identical retries are idempotent, drift requires a fork."""
    settings = settings or get_settings()
    payload = build_run_manifest_payload(
        run_id,
        parent_manifest_sha256=parent_manifest_sha256,
        evaluator=evaluator,
        approvals=approvals,
        settings=settings,
    )
    if payload.git.dirty and not (
        settings.allow_dirty_frozen_manifest if allow_dirty is None else allow_dirty
    ):
        raise ManifestCompatibilityError(
            "refusing a release-grade frozen manifest for a dirty git tree; commit/stash changes "
            "or explicitly allow a development manifest"
        )
    digest = content_sha256(payload)
    manifest = RunManifest(
        payload=payload,
        manifest_sha256=digest,
        frozen_at=datetime.now(timezone.utc),
    )
    path = _manifest_path(run_id)
    if path.exists():
        existing = load_run_manifest(run_id)
        assert existing is not None
        if existing.manifest_sha256 != digest:
            raise ManifestCompatibilityError(
                "run environment changed after manifest freeze; fork a new run with the existing "
                f"manifest as parent (old={existing.manifest_sha256}, new={digest})"
            )
        from aletheia.memory.service import record_run_manifest

        record_run_manifest(
            run_id,
            manifest_sha256=existing.manifest_sha256,
            schema_version=MANIFEST_VERSION,
            uri=str(path),
            payload=existing.payload.model_dump(mode="json"),
            parent_manifest_sha256=existing.payload.parent_manifest_sha256,
        )
        return existing

    path.write_text(
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    from aletheia.memory.service import record_run_manifest

    record_run_manifest(
        run_id,
        manifest_sha256=manifest.manifest_sha256,
        schema_version=MANIFEST_VERSION,
        uri=str(path),
        payload=manifest.payload.model_dump(mode="json"),
        parent_manifest_sha256=parent_manifest_sha256,
    )
    return manifest
