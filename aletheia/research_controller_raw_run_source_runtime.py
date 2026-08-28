"""Guarded-loader factory for the read-only scientific raw-run source RPC service."""

from __future__ import annotations


def build_raw_run_source_rpc_service(*, deployment, configuration_bytes):
    """Compose exactly ``LOAD_RAW_RUN`` with public verification material only."""

    import hashlib
    import json
    import os
    import stat
    from collections import Counter
    from pathlib import Path
    from typing import Literal

    from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

    from aletheia.config import get_settings
    from aletheia.db import expected_schema_revision, session_factory
    from aletheia.execution.runtime_contracts import QualificationAuthorityVerifier
    from aletheia.execution.terminal_runtime import (
        QualificationTerminalReaderConfig,
        compose_qualification_raw_run_material_reader,
    )
    from aletheia.observations import adapters as source_module
    from aletheia.observations.adapters import (
        PostgreSQLRawRunEnvelopeSourceAdapter,
        RawRunEnvelopeSourceVerificationContext,
        RawRunTerminalMaterialPending,
    )
    from aletheia.observations.scientific_bridge import (
        ScientificBridgeAuthorityPin,
        ScientificBridgeRole,
    )
    from aletheia.research_controller.external_rpc import (
        ControllerWorkerRPCOperation,
        RawRunLoadResult,
    )
    from aletheia.research_controller.external_rpc_server import (
        ControllerWorkerRPCHandlerBinding,
        ControllerWorkerRPCHandlerSet,
        ScientificSlotLookupRPCPayload,
    )
    from aletheia.research_controller.step_executor import (
        ControllerStepAuthorityBinding,
        ControllerStepAuthorityRole,
    )
    from aletheia.research_kernel.schemas import canonical_json_bytes

    class RawRunSourceRPCConfig(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        schema_name: Literal["aletheia.raw_run_source_rpc_service_config"] = (
            "aletheia.raw_run_source_rpc_service_config"
        )
        schema_version: Literal[1] = 1
        controller_id: str = Field(pattern=r"^rctl_[0-9a-f]{32}$")
        controller_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        worker_process_principal_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$")
        service_id: str = Field(pattern=r"^rpcs_[0-9a-f]{32}$")
        service_pin_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        database_url_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        schema_revision: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
        authority_binding: ControllerStepAuthorityBinding
        execution_authority_pin: ScientificBridgeAuthorityPin
        validator_authority_pin: ScientificBridgeAuthorityPin
        admission_authority_pin: ScientificBridgeAuthorityPin
        qualification_reader: QualificationTerminalReaderConfig
        source_implementation_source_path: str
        source_implementation_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        prepared_at: AwareDatetime
        private_domain_signing_key_loaded: Literal[False] = False
        execution_mutation_allowed: Literal[False] = False
        database_mutation_allowed: Literal[False] = False
        validation_allowed: Literal[False] = False
        direct_observation_admission_allowed: Literal[False] = False
        direct_kernel_mutation_allowed: Literal[False] = False

        @model_validator(mode="after")
        def _authority_is_read_only_and_closed(self):
            binding = self.authority_binding
            execution = self.execution_authority_pin
            validator = self.validator_authority_pin
            admission = self.admission_authority_pin
            reader = self.qualification_reader
            source = Path(self.source_implementation_source_path)
            bridge_principals = (
                execution.principal_id,
                validator.principal_id,
                admission.principal_id,
            )
            bridge_keys = (execution.key_id, validator.key_id, admission.key_id)
            bridge_policies = (
                execution.policy_sha256,
                validator.policy_sha256,
                admission.policy_sha256,
            )
            reader_keys = (
                reader.pricing_authority_pin.key_id,
                reader.source_budget_authority_pin.key_id,
                reader.qualification_authority_pin.key_id,
                reader.terminal_verification_authority_pin.key_id,
                reader.runtime_control_authority_pin.key_id,
                *(item.manifest.node_signing_key_id for item in reader.node_authorities),
                *(item.enrollment_authority_pin.key_id for item in reader.node_authorities),
                *(
                    item.assignment_transport_pin.transport_key_id
                    for item in reader.node_authorities
                ),
            )
            reader_policies = (
                reader.pricing_authority_pin.policy_sha256,
                reader.source_budget_authority_pin.policy_sha256,
                reader.qualification_authority_pin.policy_sha256,
                reader.terminal_verification_authority_pin.policy_sha256,
                reader.runtime_control_authority_pin.policy_sha256,
                *(item.enrollment_authority_pin.policy_sha256 for item in reader.node_authorities),
                *(
                    item.assignment_transport_pin.transport_policy_sha256
                    for item in reader.node_authorities
                ),
            )
            node_execution_policies = {
                item.manifest.sandbox_policy_sha256 for item in reader.node_authorities
            }
            active_reader_pins = (
                reader.pricing_authority_pin,
                reader.source_budget_authority_pin,
                reader.qualification_authority_pin,
                reader.terminal_verification_authority_pin,
                reader.runtime_control_authority_pin,
                *(item.enrollment_authority_pin for item in reader.node_authorities),
                *(item.assignment_transport_pin for item in reader.node_authorities),
            )
            nodes_active = all(
                item.manifest.key_valid_from
                <= self.prepared_at
                < min(
                    item.manifest.key_expires_at,
                    item.manifest.key_revoked_at or item.manifest.key_expires_at,
                )
                for item in reader.node_authorities
            )
            if (
                binding.role is not ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION
                or not binding.externally_deployed
                or execution.role is not ScientificBridgeRole.EXECUTION_AUTHORIZER
                or validator.role is not ScientificBridgeRole.OBSERVATION_VALIDATOR
                or admission.role is not ScientificBridgeRole.OBSERVATION_ADMITTER
                or binding.principal_id != execution.principal_id
                or binding.key_id != execution.key_id
                or binding.policy_sha256 != execution.policy_sha256
                or reader.prepared_at != self.prepared_at
                or not all(
                    pin.active_at(self.prepared_at) for pin in (execution, validator, admission)
                )
                or not all(pin.active_at(self.prepared_at) for pin in active_reader_pins)
                or not nodes_active
                or len(set(bridge_principals)) != len(bridge_principals)
                or len(set(bridge_keys)) != len(bridge_keys)
                or len(set(bridge_policies)) != len(bridge_policies)
                or set(bridge_principals) & set(reader.authority_principal_ids)
                or set(bridge_keys) & set(reader_keys)
                or set(bridge_policies) & (set(reader_policies) | node_execution_policies)
                or len(set(reader_keys)) != len(reader_keys)
                or len(set(reader_policies)) != len(reader_policies)
                or self.worker_process_principal_id
                in set(bridge_principals) | set(reader.authority_principal_ids)
                or not source.is_absolute()
                or self.source_implementation_source_path != os.path.normpath(source)
            ):
                raise ValueError("raw-run source RPC authority is not closed")
            return self

    def unique_object(pairs):
        duplicates = sorted(
            key for key, count in Counter(key for key, _value in pairs).items() if count > 1
        )
        if duplicates:
            raise ValueError(f"duplicate raw-run source RPC config keys: {duplicates}")
        return dict(pairs)

    def fresh_regular_bytes(path: Path, *, expected_sha256: str, label: str) -> bytes:
        try:
            if path.resolve(strict=True) != path or path.is_symlink():
                raise ValueError(f"{label} traverses a symlink")
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                before = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1
                    or not 0 < before.st_size <= 4 * 1024 * 1024
                ):
                    raise ValueError(f"{label} has unsafe file custody")
                chunks = []
                remaining = before.st_size
                while remaining:
                    chunk = os.read(descriptor, min(65_536, remaining))
                    if not chunk:
                        raise ValueError(f"{label} ended unexpectedly")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                after = os.fstat(descriptor)
                if os.read(descriptor, 1) or (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise ValueError(f"{label} changed while read")
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise ValueError(f"{label} is unavailable") from exc
        payload = b"".join(chunks)
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ValueError(f"{label} differs from its byte pin")
        return payload

    try:
        raw = json.loads(configuration_bytes, object_pairs_hook=unique_object)
        config = RawRunSourceRPCConfig.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("raw-run source RPC config is invalid") from exc
    if canonical_json_bytes(config) != configuration_bytes:
        raise ValueError("raw-run source RPC config is not canonical JSON")

    pin = deployment.service_pin
    binding = config.authority_binding
    reader = config.qualification_reader
    signed_public_keys = {
        config.execution_authority_pin.public_key_ed25519_hex,
        config.validator_authority_pin.public_key_ed25519_hex,
        config.admission_authority_pin.public_key_ed25519_hex,
        reader.pricing_authority_pin.public_key_ed25519_hex,
        reader.source_budget_authority_pin.public_key_ed25519_hex,
        reader.qualification_authority_pin.public_key_ed25519_hex,
        reader.terminal_verification_authority_pin.public_key_ed25519_hex,
        reader.runtime_control_authority_pin.public_key_ed25519_hex,
        *(item.manifest.node_signing_public_key_ed25519_hex for item in reader.node_authorities),
        *(item.enrollment_authority_pin.public_key_ed25519_hex for item in reader.node_authorities),
    }
    transport_public_keys = {
        item.assignment_transport_pin.public_key_x25519_hex for item in reader.node_authorities
    }
    all_authority_principals = set(reader.authority_principal_ids) | {
        config.execution_authority_pin.principal_id,
        config.validator_authority_pin.principal_id,
        config.admission_authority_pin.principal_id,
    }
    all_authority_policies = {
        config.execution_authority_pin.policy_sha256,
        config.validator_authority_pin.policy_sha256,
        config.admission_authority_pin.policy_sha256,
        reader.pricing_authority_pin.policy_sha256,
        reader.source_budget_authority_pin.policy_sha256,
        reader.qualification_authority_pin.policy_sha256,
        reader.terminal_verification_authority_pin.policy_sha256,
        reader.runtime_control_authority_pin.policy_sha256,
        *(item.enrollment_authority_pin.policy_sha256 for item in reader.node_authorities),
        *(
            item.assignment_transport_pin.transport_policy_sha256
            for item in reader.node_authorities
        ),
        *(item.manifest.sandbox_policy_sha256 for item in reader.node_authorities),
    }
    if (
        pin.operations != (ControllerWorkerRPCOperation.LOAD_RAW_RUN,)
        or pin.authority_binding_sha256s != (binding.binding_sha256,)
        or config.controller_id != deployment.controller_id
        or config.controller_manifest_sha256 != deployment.controller_manifest_sha256
        or config.worker_process_principal_id != deployment.worker_process_principal_id
        or config.service_id != pin.service_id
        or config.service_pin_sha256 != pin.pin_sha256
        or config.prepared_at != deployment.prepared_at
        or config.database_url_sha256
        != hashlib.sha256(get_settings().database_url.encode("utf-8")).hexdigest()
        or config.schema_revision != expected_schema_revision()
        or pin.service_principal_id in all_authority_principals
        or pin.service_principal_id == config.worker_process_principal_id
        or pin.service_policy_sha256 in all_authority_policies
        or pin.receipt_public_key_ed25519_hex in signed_public_keys | transport_public_keys
    ):
        raise ValueError("raw-run source RPC config differs from deployment or authority")

    reviewed_root = Path(deployment.reviewed_code_root)
    implementation_path = Path(config.source_implementation_source_path)
    expected_module_path = Path(source_module.__file__).resolve(strict=True)
    try:
        implementation_path.relative_to(reviewed_root)
    except ValueError as exc:
        raise ValueError("raw-run source implementation escaped reviewed source") from exc
    if implementation_path != expected_module_path:
        raise ValueError("raw-run source implementation resolved another module")
    custody_roots = (
        Path(reader.artifact_store_root),
        Path(reader.authority_registry_root),
        reviewed_root,
        Path(deployment.socket_parent_path),
        Path(deployment.composition_config_path).parent,
        Path(deployment.receipt_private_key_path).parent,
    )
    for index, first in enumerate(custody_roots):
        for second in custody_roots[index + 1 :]:
            if first == second or first in second.parents or second in first.parents:
                raise ValueError("raw-run source custody roots overlap")

    before = fresh_regular_bytes(
        implementation_path,
        expected_sha256=config.source_implementation_source_sha256,
        label="raw-run source implementation",
    )
    execution_material = compose_qualification_raw_run_material_reader(reader)
    source = PostgreSQLRawRunEnvelopeSourceAdapter(
        execution_material=execution_material,
        sea_sessions=session_factory(),
        verification=RawRunEnvelopeSourceVerificationContext(
            qualification_authority=QualificationAuthorityVerifier(
                reader.qualification_authority_pin
            ),
            execution_authority_pin=config.execution_authority_pin,
            validator_authority_pin=config.validator_authority_pin,
            admission_authority_pin=config.admission_authority_pin,
        ),
    )

    def load_raw_run(payload):
        if type(payload) is not ScientificSlotLookupRPCPayload:
            raise TypeError("raw-run source RPC handler received another payload type")
        try:
            raw_run = source.load_raw_run(
                quest_id=payload.quest_id,
                action_sha256=payload.action_sha256,
                scientific_slot_id=payload.scientific_slot_id,
            )
        except RawRunTerminalMaterialPending:
            return RawRunLoadResult(
                disposition="pending",
                pending_code="raw_run:terminal_material_pending",
                retry_after_milliseconds=250,
            )
        return RawRunLoadResult(disposition="ready", raw_run=raw_run)

    after = fresh_regular_bytes(
        implementation_path,
        expected_sha256=config.source_implementation_source_sha256,
        label="raw-run source implementation",
    )
    if before != after:
        raise ValueError("raw-run source implementation changed during composition")
    return ControllerWorkerRPCHandlerSet(
        operations=pin.operations,
        bindings=(
            ControllerWorkerRPCHandlerBinding(
                operation=ControllerWorkerRPCOperation.LOAD_RAW_RUN,
                handler=load_raw_run,
            ),
        ),
    )


__all__ = ["build_raw_run_source_rpc_service"]
