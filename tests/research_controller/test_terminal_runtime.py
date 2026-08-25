from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from aletheia.config import get_settings
from aletheia.db import expected_schema_revision
from aletheia.execution.authority_contracts import (
    AuthorityRegistryFilesystemPin,
    PricingAuthorityPin,
    SourceBudgetAuthorityPin,
    authority_key_id,
)
from aletheia.execution.artifact_store import ArtifactStoreError, LocalArtifactStore
from aletheia.execution.runtime_contracts import (
    QualificationAuthorityPin,
    qualification_key_id,
)
from aletheia.execution.runtime_v2_contracts import RuntimeControlAuthorityPin
from aletheia.execution.terminal_source import VerifiedQualificationTerminalOutboxReader
from aletheia.research_controller.contracts import ResearchControllerManifest
from aletheia.execution.terminal_runtime import (
    QualificationTerminalRuntimeConfig,
    TerminalNodeAuthorityConfig,
)
from aletheia.research_controller_terminal_runtime import build_terminal_runtime
from aletheia.research_controller_runtime import (
    ResearchControllerRuntimeDeployment,
    ResearchControllerRuntimeRole,
    build_research_controller_runtime,
)

_EXECUTION_FIXTURES = Path(__file__).resolve().parents[1] / "execution"
sys.path.insert(0, str(_EXECUTION_FIXTURES))
from test_allocator import _prepared  # noqa: E402


def _public_key(label: str) -> str:
    private = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(label.encode()).digest())
    return (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )


def _external_pin(model, *, label: str, principal: str, observed_at):
    public_key = _public_key(label)
    return model(
        policy_sha256=hashlib.sha256(f"{label}:policy".encode()).hexdigest(),
        principal_id=principal,
        key_id=authority_key_id(public_key),
        public_key_ed25519_hex=public_key,
        valid_from=observed_at - timedelta(days=1),
        expires_at=observed_at + timedelta(days=1),
    )


def _manifest(observed_at) -> ResearchControllerManifest:
    return ResearchControllerManifest(
        controller_key="principal.controller.production",
        controller_code_sha256="1" * 64,
        controller_policy_sha256="2" * 64,
        capability_catalog_sha256="3" * 64,
        protocol_registry_policy_sha256="4" * 64,
        scientific_bridge_policy_sha256="5" * 64,
        worker_manifest_sha256="6" * 64,
        retry_policy={
            "max_attempts": 3,
            "lease_seconds": 60,
            "heartbeat_interval_seconds": 10,
        },
        prepared_at=observed_at,
    )


def _empty_registry(root: Path) -> AuthorityRegistryFilesystemPin:
    namespaces = (
        "rate_cards",
        "execution_cost_quotes",
        "source_budgets",
        "source_budget_projections",
    )
    root.mkdir(mode=0o755)
    for namespace in namespaces:
        sha_root = root / namespace / "sha256"
        sha_root.mkdir(parents=True, mode=0o555)
        (root / namespace).chmod(0o555)
        sha_root.chmod(0o555)
    root.chmod(0o555)
    metadata = root.stat()
    return AuthorityRegistryFilesystemPin(
        registry_id="registry:terminal-runtime-test",
        owner_uid=os.getuid(),
        device_id=metadata.st_dev,
        directory_mode=stat.S_IMODE(metadata.st_mode),
        file_mode=0o444,
    )


def _config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    prepared = _prepared(monkeypatch)
    observed_at = prepared.observed_at
    node_authority = prepared.allocator._node_authorities[prepared.manifest.node_id]
    registry_root = tmp_path / "authority-registry"
    filesystem_pin = _empty_registry(registry_root)
    artifact_root = tmp_path / "artifact-store"
    LocalArtifactStore(
        artifact_root,
        verifier_principal_id="principal:terminal-artifact-verifier",
        object_store_id="store:terminal-runtime",
        max_object_bytes=1024**3,
    )
    pricing_pin = _external_pin(
        PricingAuthorityPin,
        label="terminal-pricing",
        principal="principal:terminal-pricing",
        observed_at=observed_at,
    )
    budget_pin = _external_pin(
        SourceBudgetAuthorityPin,
        label="terminal-budget",
        principal="principal:terminal-budget",
        observed_at=observed_at,
    )
    runtime_public_key = _public_key("terminal-runtime-control")
    runtime_pin = RuntimeControlAuthorityPin(
        policy_sha256=hashlib.sha256(b"terminal-runtime-control:policy").hexdigest(),
        principal_id="principal:terminal-runtime-control",
        key_id=qualification_key_id(runtime_public_key),
        public_key_ed25519_hex=runtime_public_key,
        valid_from=observed_at - timedelta(days=1),
        expires_at=observed_at + timedelta(days=1),
    )
    qualification_public_key = _public_key("terminal-qualification")
    qualification_pin = QualificationAuthorityPin(
        policy_sha256=hashlib.sha256(b"terminal-qualification:policy").hexdigest(),
        principal_id="principal:terminal-qualification",
        key_id=qualification_key_id(qualification_public_key),
        public_key_ed25519_hex=qualification_public_key,
        valid_from=observed_at - timedelta(days=1),
        expires_at=observed_at + timedelta(days=1),
    )
    controller_manifest = _manifest(observed_at)
    config = QualificationTerminalRuntimeConfig(
        role="terminal_dispatcher",
        process_principal_id="principal.controller.terminal_dispatcher",
        controller_manifest_sha256=controller_manifest.manifest_sha256,
        database_url_sha256=hashlib.sha256(get_settings().database_url.encode()).hexdigest(),
        schema_revision=expected_schema_revision(),
        artifact_store_root=str(artifact_root),
        artifact_verifier_principal_id="principal:terminal-artifact-verifier",
        artifact_object_store_id="store:terminal-runtime",
        artifact_max_object_bytes=1024**3,
        authority_registry_root=str(registry_root),
        authority_registry_filesystem_pin=filesystem_pin,
        pricing_authority_pin=pricing_pin,
        source_budget_authority_pin=budget_pin,
        qualification_authority_pin=qualification_pin,
        terminal_verification_authority_pin=prepared.terminal_pin,
        runtime_control_authority_pin=runtime_pin,
        node_authorities=(
            TerminalNodeAuthorityConfig(
                manifest=prepared.manifest,
                enrollment=node_authority.enrollment,
                enrollment_authority_pin=node_authority.enrollment_authority_pin,
                assignment_transport_pin=prepared.transport_pin,
            ),
        ),
        allowed_rate_card_sha256s=(prepared.bundle.cost_quote.rate_card_sha256,),
        allowed_currency_codes=(prepared.bundle.cost_quote.currency_code,),
        allocator_principal_id="principal:terminal-lineage-verifier",
        input_resolver_principal_id="principal:terminal-input-resolver",
        prepared_at=observed_at,
    )
    return config, controller_manifest


def _guarded_deployment(
    *,
    config: QualificationTerminalRuntimeConfig,
    controller_manifest: ResearchControllerManifest,
    tmp_path: Path,
) -> ResearchControllerRuntimeDeployment:
    repository_root = Path(__file__).resolve().parents[2]
    factory_path = (repository_root / "aletheia/research_controller_terminal_runtime.py").resolve()
    controller_path = (tmp_path / "controller.json").resolve()
    controller_path.write_text(controller_manifest.model_dump_json(), encoding="utf-8")
    config_path = (tmp_path / "terminal-runtime.json").resolve()
    config_path.write_text(
        json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return ResearchControllerRuntimeDeployment(
        role=ResearchControllerRuntimeRole.TERMINAL_DISPATCHER,
        controller_manifest_path=str(controller_path),
        controller_manifest_file_sha256=hashlib.sha256(controller_path.read_bytes()).hexdigest(),
        controller_manifest_sha256=controller_manifest.manifest_sha256,
        reviewed_code_root=str(repository_root),
        composition_factory_module="aletheia.research_controller_terminal_runtime",
        composition_factory_attribute="build_terminal_runtime",
        composition_factory_source_path=str(factory_path),
        composition_factory_source_sha256=hashlib.sha256(factory_path.read_bytes()).hexdigest(),
        composition_config_path=str(config_path),
        composition_config_file_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
        process_principal_id=config.process_principal_id,
        prepared_at=config.prepared_at,
    )


def test_terminal_runtime_config_rejects_authority_overlap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, _manifest_value = _config(monkeypatch, tmp_path)
    payload = config.model_dump(mode="python")
    payload["process_principal_id"] = config.qualification_authority_pin.principal_id
    with pytest.raises(ValidationError, match="distinct principals"):
        QualificationTerminalRuntimeConfig.model_validate(payload)


def test_terminal_factory_exposes_only_verified_reader_and_queue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, controller_manifest = _config(monkeypatch, tmp_path)
    deployment = SimpleNamespace(
        role=ResearchControllerRuntimeRole.TERMINAL_DISPATCHER,
        process_principal_id=config.process_principal_id,
        controller_manifest_sha256=controller_manifest.manifest_sha256,
        prepared_at=config.prepared_at,
    )
    dependencies = build_terminal_runtime(
        deployment=deployment,
        controller_manifest=controller_manifest,
        configuration_bytes=(
            json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode(),
    )

    assert isinstance(
        dependencies.terminal_outbox,
        VerifiedQualificationTerminalOutboxReader,
    )
    assert dependencies.queue.principal == config.process_principal_id
    assert dependencies.kernel_store is None
    assert dependencies.service is None
    assert not hasattr(dependencies.terminal_outbox, "admit_and_reserve")
    assert not hasattr(dependencies.terminal_outbox, "settle_qualification_terminal")
    allocator = dependencies.terminal_outbox._allocator
    assert allocator.runtime_control_issuance_enabled is False
    assert allocator.runtime_control_verification_enabled is True
    assert allocator._artifact_resolver._artifact_store.read_only is True


def test_terminal_factory_requires_preexisting_read_only_artifact_layout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, controller_manifest = _config(monkeypatch, tmp_path)
    artifact_root = Path(config.artifact_store_root)
    artifact_root.rename(tmp_path / "artifact-store-withheld")
    deployment = SimpleNamespace(
        role=ResearchControllerRuntimeRole.TERMINAL_DISPATCHER,
        process_principal_id=config.process_principal_id,
        controller_manifest_sha256=controller_manifest.manifest_sha256,
        prepared_at=config.prepared_at,
    )

    with pytest.raises(ArtifactStoreError, match="must already exist"):
        build_terminal_runtime(
            deployment=deployment,
            controller_manifest=controller_manifest,
            configuration_bytes=(
                json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode(),
        )

    assert not artifact_root.exists()


def test_terminal_factory_rejects_duplicate_json_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, controller_manifest = _config(monkeypatch, tmp_path)
    deployment = SimpleNamespace(
        role=ResearchControllerRuntimeRole.TERMINAL_DISPATCHER,
        process_principal_id=config.process_principal_id,
        controller_manifest_sha256=controller_manifest.manifest_sha256,
        prepared_at=config.prepared_at,
    )
    payload = json.dumps(config.model_dump(mode="json"), separators=(",", ":")).replace(
        '"schema_version":1',
        '"schema_version":1,"schema_version":1',
        1,
    )
    with pytest.raises(ValueError, match="config is invalid"):
        build_terminal_runtime(
            deployment=deployment,
            controller_manifest=controller_manifest,
            configuration_bytes=payload.encode(),
        )


def test_guarded_runtime_loader_accepts_exact_terminal_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, controller_manifest = _config(monkeypatch, tmp_path)
    deployment = _guarded_deployment(
        config=config,
        controller_manifest=controller_manifest,
        tmp_path=tmp_path,
    )

    runtime = build_research_controller_runtime(deployment)

    assert runtime.deployment == deployment
    assert runtime.deployment.role is ResearchControllerRuntimeRole.TERMINAL_DISPATCHER


@pytest.mark.asyncio
@pytest.mark.skipif(
    "ALETHEIA_DATABASE_URL" not in os.environ,
    reason="PostgreSQL terminal-runtime smoke test requires an explicit database",
)
async def test_guarded_terminal_runtime_runs_empty_postgresql_cycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, controller_manifest = _config(monkeypatch, tmp_path)
    deployment = _guarded_deployment(
        config=config,
        controller_manifest=controller_manifest,
        tmp_path=tmp_path,
    )

    receipt = await build_research_controller_runtime(deployment).run_once()

    assert receipt.result_kind == "terminal_dispatch"
    assert receipt.work_performed is False
    assert receipt.result_payload["registered_quest_count"] == 0
    assert receipt.result_payload["delivered_outbox_sha256s"] == []
