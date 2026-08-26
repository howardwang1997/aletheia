"""Closed production worker composition for all durable controller steps.

This module assembles the local read-only recovery path and eight exact step adapters.  Every
service capable of signing, judging, compiling, proposing, or mutating an authority store remains
behind a receipt-authenticated RPC port; no private key or generic callback enters the worker.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.config import get_settings
from aletheia.db import expected_schema_revision
from aletheia.execution.terminal_runtime import (
    QualificationTerminalReaderConfig,
    compose_qualification_terminal_reader,
)
from aletheia.research_controller.action_proposals import ActionProposalStepAdapter
from aletheia.research_controller.continuation_step import ContinuationAssessmentStepAdapter
from aletheia.research_controller.contracts import (
    ControllerModel,
    ControllerStep,
    ResearchControllerManifest,
)
from aletheia.research_controller.execution_registration import (
    QualifiedExecutionRegistrationStepAdapter,
)
from aletheia.research_controller.external_rpc import (
    ControllerWorkerRPCClient,
    ControllerWorkerRPCOperation,
    ControllerWorkerRPCServicePin,
    ControllerWorkerRPCTransport,
    RPCActionProposalMaterialization,
    RPCAtomicObservationAdmission,
    RPCCommittedValidationSource,
    RPCContinuationMaterialization,
    RPCDatabaseObservationBridge,
    RPCIndependentObservationAdmission,
    RPCIndependentObservationValidator,
    RPCProtocolCompilationMaterialization,
    RPCRawRunEnvelopeSource,
    RPCScientificExecutionAuthorizationIssuer,
    RPCScientificExecutionRegistrar,
)
from aletheia.research_controller.observation_steps import (
    AtomicObservationAdmissionStepAdapter,
    IndependentObservationValidationStepAdapter,
)
from aletheia.research_controller.protocol_compilation_step import ProtocolCompilationStepAdapter
from aletheia.research_controller.recovery import PostgreSQLControllerRecoveryAdapter
from aletheia.research_controller.service import ResearchControllerService
from aletheia.research_controller.step_executor import (
    ControllerStepAdapterSetManifest,
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
    DedicatedControllerStepExecutor,
)
from aletheia.research_kernel.policy import ResearchAuthorizationTrustRootV1
from aletheia.research_kernel.schemas import canonical_sha256
from aletheia.research_store.cas import FilesystemResearchArchive
from aletheia.research_store.store import ResearchKernelStore

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$"
_MAX_SOURCE_BYTES = 4 * 1024 * 1024


class ControllerWorkerCompositionError(RuntimeError):
    """The frozen worker config or installed source closure failed closed."""


def _operations(*values: ControllerWorkerRPCOperation) -> tuple[ControllerWorkerRPCOperation, ...]:
    return tuple(sorted(values, key=lambda item: item.value))


_SERVICE_OPERATIONS = {
    "action_proposal": _operations(ControllerWorkerRPCOperation.MATERIALIZE_ACTION_PROPOSAL),
    "protocol_compilation": _operations(ControllerWorkerRPCOperation.COMPILE_PROTOCOL),
    "execution_authorization": _operations(
        ControllerWorkerRPCOperation.ISSUE_EXECUTION_AUTHORIZATION
    ),
    "execution_registration": _operations(ControllerWorkerRPCOperation.REGISTER_EXECUTION),
    "raw_run_source": _operations(ControllerWorkerRPCOperation.LOAD_RAW_RUN),
    "database_observation": _operations(
        ControllerWorkerRPCOperation.ISSUE_VALIDATION_CHALLENGE,
        ControllerWorkerRPCOperation.COMMIT_VALIDATION,
        ControllerWorkerRPCOperation.ISSUE_ADMISSION_CHALLENGE,
    ),
    "independent_validation": _operations(
        ControllerWorkerRPCOperation.PREPARE_VALIDATION_CAMPAIGN,
        ControllerWorkerRPCOperation.ISSUE_VALIDATION_RECEIPT,
    ),
    "committed_validation_source": _operations(
        ControllerWorkerRPCOperation.LOAD_COMMITTED_VALIDATION
    ),
    "independent_admission": _operations(ControllerWorkerRPCOperation.ISSUE_ADMISSION_DECISION),
    "atomic_admission": _operations(ControllerWorkerRPCOperation.COMMIT_AND_INCORPORATE),
    "continuation_assessment": _operations(ControllerWorkerRPCOperation.DERIVE_CONTINUATION),
}


class ControllerWorkerRPCServiceSet(ControllerModel):
    """Named, exhaustive service pins for the worker's eleven narrow external ports."""

    schema_name: Literal["aletheia.controller_worker_rpc_service_set"] = (
        "aletheia.controller_worker_rpc_service_set"
    )
    schema_version: Literal[1] = 1
    action_proposal: ControllerWorkerRPCServicePin
    protocol_compilation: ControllerWorkerRPCServicePin
    execution_authorization: ControllerWorkerRPCServicePin
    execution_registration: ControllerWorkerRPCServicePin
    raw_run_source: ControllerWorkerRPCServicePin
    database_observation: ControllerWorkerRPCServicePin
    independent_validation: ControllerWorkerRPCServicePin
    committed_validation_source: ControllerWorkerRPCServicePin
    independent_admission: ControllerWorkerRPCServicePin
    atomic_admission: ControllerWorkerRPCServicePin
    continuation_assessment: ControllerWorkerRPCServicePin

    @property
    def named_pins(self) -> tuple[tuple[str, ControllerWorkerRPCServicePin], ...]:
        return tuple((name, getattr(self, name)) for name in _SERVICE_OPERATIONS)

    @model_validator(mode="after")
    def _service_set_is_exact(self) -> "ControllerWorkerRPCServiceSet":
        for name, pin in self.named_pins:
            if pin.operations != _SERVICE_OPERATIONS[name]:
                raise ValueError(f"controller worker RPC service {name} has another operation set")
        pins = tuple(pin for _name, pin in self.named_pins)
        observed_operations = tuple(operation for pin in pins for operation in pin.operations)
        if len(observed_operations) != len(set(observed_operations)) or frozenset(
            observed_operations
        ) != frozenset(ControllerWorkerRPCOperation):
            raise ValueError("controller worker RPC operations are not an exhaustive partition")
        for label, values in (
            ("service ids", tuple(pin.service_id for pin in pins)),
            ("service principals", tuple(pin.service_principal_id for pin in pins)),
            ("receipt keys", tuple(pin.receipt_key_id for pin in pins)),
            ("socket paths", tuple(pin.socket_path for pin in pins)),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"controller worker RPC {label} must be pairwise distinct")
        return self

    @property
    def service_set_sha256(self) -> str:
        return canonical_sha256(self)


class ResearchKernelReadOnlyConfig(ControllerModel):
    """Public trust root and immutable filesystem custody used only for replay audit."""

    schema_name: Literal["aletheia.research_kernel_read_only_config"] = (
        "aletheia.research_kernel_read_only_config"
    )
    schema_version: Literal[1] = 1
    trust_root: ResearchAuthorizationTrustRootV1
    cas_root: str
    cas_owner_uid: int = Field(ge=0)
    cas_group_gid: int = Field(ge=0)
    cas_device_id: int = Field(ge=0)
    cas_inode: int = Field(gt=0)
    cas_directory_mode: int = Field(ge=0, le=0o777)
    max_object_bytes: int = Field(ge=1, le=1024**3)
    read_only: Literal[True] = True
    private_key_loaded_in_worker: Literal[False] = False

    @model_validator(mode="after")
    def _cas_root_is_canonical_and_read_only(self) -> "ResearchKernelReadOnlyConfig":
        candidate = Path(self.cas_root)
        if (
            not self.cas_root
            or "\x00" in self.cas_root
            or "\n" in self.cas_root
            or "\r" in self.cas_root
            or not candidate.is_absolute()
            or self.cas_root != os.path.normpath(self.cas_root)
            or self.cas_root == "/"
        ):
            raise ValueError("Research Kernel CAS root must be canonical and absolute")
        readable = any(self.cas_directory_mode & mask == mask for mask in (0o500, 0o050, 0o005))
        if self.cas_directory_mode & 0o222 or not readable:
            raise ValueError("Research Kernel CAS root must be a readable non-writable directory")
        return self

    @property
    def config_sha256(self) -> str:
        return canonical_sha256(self)


_STEP_SERVICE_NAMES = {
    ControllerStep.PROPOSE_ACTION: ("action_proposal",),
    ControllerStep.COMPILE_PROTOCOL: ("protocol_compilation",),
    ControllerStep.PROPOSE_REDESIGN: ("action_proposal",),
    ControllerStep.REGISTER_EXECUTION: (
        "execution_authorization",
        "execution_registration",
    ),
    ControllerStep.COMMIT_VALIDATION: (
        "raw_run_source",
        "database_observation",
        "independent_validation",
    ),
    ControllerStep.COMMIT_ADMISSION: (
        "committed_validation_source",
        "database_observation",
        "independent_admission",
        "atomic_admission",
    ),
    ControllerStep.DERIVE_CONTINUATION: ("continuation_assessment",),
    ControllerStep.PROPOSE_FOLLOWUP: ("action_proposal",),
}


def controller_step_rpc_configuration_sha256(
    step: ControllerStep,
    services: ControllerWorkerRPCServiceSet,
) -> str:
    """Derive the adapter config pin from only the endpoints reachable by that step."""

    names = _STEP_SERVICE_NAMES.get(step)
    if names is None:
        raise ValueError("passive controller step has no RPC adapter configuration")
    return canonical_sha256(
        {
            "schema_name": "aletheia.controller_step_rpc_configuration",
            "schema_version": 1,
            "step": step.value,
            "service_pin_sha256s": tuple(
                getattr(services, name).pin_sha256 for name in sorted(names)
            ),
        }
    )


_STEP_ADAPTER_MODULES = {
    ControllerStep.PROPOSE_ACTION: "action_proposals.py",
    ControllerStep.COMPILE_PROTOCOL: "protocol_compilation_step.py",
    ControllerStep.PROPOSE_REDESIGN: "action_proposals.py",
    ControllerStep.REGISTER_EXECUTION: "execution_registration.py",
    ControllerStep.COMMIT_VALIDATION: "observation_steps.py",
    ControllerStep.COMMIT_ADMISSION: "observation_steps.py",
    ControllerStep.DERIVE_CONTINUATION: "continuation_step.py",
    ControllerStep.PROPOSE_FOLLOWUP: "action_proposals.py",
}


def _fresh_source_sha256(path: Path, *, reviewed_code_root: Path | None = None) -> str:
    try:
        resolved = path.resolve(strict=True)
        if resolved != path:
            raise ControllerWorkerCompositionError("worker adapter source traverses a symlink")
        if reviewed_code_root is not None:
            resolved.relative_to(reviewed_code_root)
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
    except (OSError, ValueError) as exc:
        raise ControllerWorkerCompositionError(
            "worker adapter source is outside the reviewed release"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= _MAX_SOURCE_BYTES:
            raise ControllerWorkerCompositionError("worker adapter source is not a bounded file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, _MAX_SOURCE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_SOURCE_BYTES:
                raise ControllerWorkerCompositionError("worker adapter source exceeds its bound")
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        len(payload) != before.st_size
        or len(payload) > _MAX_SOURCE_BYTES
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
    ):
        raise ControllerWorkerCompositionError("worker adapter source changed while read")
    return hashlib.sha256(payload).hexdigest()


def controller_step_adapter_source_sha256(
    step: ControllerStep,
    *,
    reviewed_code_root: str | Path | None = None,
) -> str:
    """Fresh-read the checked-in source file implementing one exact active adapter."""

    filename = _STEP_ADAPTER_MODULES.get(step)
    if filename is None:
        raise ValueError("passive controller step has no adapter source")
    package_root = Path(__file__).resolve(strict=True).parent
    reviewed_root = None
    if reviewed_code_root is not None:
        reviewed_root = Path(reviewed_code_root).resolve(strict=True)
    return _fresh_source_sha256(package_root / filename, reviewed_code_root=reviewed_root)


class ResearchControllerWorkerRuntimeConfig(ControllerModel):
    """Closed worker config linking controller, adapters, RPC services, and terminal custody."""

    schema_name: Literal["aletheia.research_controller_worker_runtime_config"] = (
        "aletheia.research_controller_worker_runtime_config"
    )
    schema_version: Literal[1] = 1
    configuration_id: str | None = Field(default=None, pattern=r"^rcwc_[0-9a-f]{32}$")
    role: Literal["worker"]
    process_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    controller_id: str = Field(pattern=r"^rctl_[0-9a-f]{32}$")
    controller_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    database_url_sha256: str = Field(pattern=_SHA256_PATTERN)
    schema_revision: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    adapter_set_manifest: ControllerStepAdapterSetManifest
    rpc_services: ControllerWorkerRPCServiceSet
    kernel_reader: ResearchKernelReadOnlyConfig
    terminal_reader: QualificationTerminalReaderConfig
    prepared_at: AwareDatetime
    private_signing_key_loaded_in_worker: Literal[False] = False
    generic_step_callback_allowed: Literal[False] = False
    direct_kernel_mutation_allowed: Literal[False] = False
    direct_observation_admission_allowed: Literal[False] = False
    legacy_optimize_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _configuration_is_exact(self) -> "ResearchControllerWorkerRuntimeConfig":
        adapter_set = self.adapter_set_manifest
        if (
            adapter_set.controller_id != self.controller_id
            or adapter_set.controller_manifest_sha256 != self.controller_manifest_sha256
            or adapter_set.worker_process_principal_id != self.process_principal_id
            or adapter_set.prepared_at != self.prepared_at
            or self.terminal_reader.prepared_at != self.prepared_at
        ):
            raise ValueError("worker controller, adapter, or terminal-reader pins differ")
        adapters = {adapter.step: adapter for adapter in adapter_set.adapters}
        bindings: dict[ControllerStepAuthorityRole, ControllerStepAuthorityBinding] = {}
        for adapter in adapter_set.adapters:
            if adapter.prepared_at != self.prepared_at:
                raise ValueError("worker adapter preparation times differ")
            if adapter.adapter_config_sha256 != controller_step_rpc_configuration_sha256(
                adapter.step, self.rpc_services
            ):
                raise ValueError("worker adapter config hash differs from its RPC services")
            for binding in adapter.authorities:
                if not binding.externally_deployed:
                    raise ValueError("complete worker composition requires external step services")
                previous = bindings.get(binding.role)
                if previous is not None and previous != binding:
                    raise ValueError("worker authority role changed across adapter manifests")
                bindings[binding.role] = binding
        if frozenset(adapters) != frozenset(_STEP_SERVICE_NAMES):
            raise ValueError("worker adapter set is not exhaustive")

        role = ControllerStepAuthorityRole
        expected_binding_roles = {
            "action_proposal": (role.ACTION_PROPOSAL,),
            "protocol_compilation": (role.PROTOCOL_COMPILATION,),
            "execution_authorization": (role.EXECUTION_AUTHORIZATION,),
            "execution_registration": (role.EXECUTION_AUTHORIZATION,),
            "raw_run_source": (role.EXECUTION_AUTHORIZATION,),
            "database_observation": (role.DATABASE_ATTESTATION,),
            "independent_validation": (role.INDEPENDENT_VALIDATION,),
            "committed_validation_source": (
                role.DATABASE_ATTESTATION,
                role.INDEPENDENT_VALIDATION,
            ),
            "independent_admission": (role.INDEPENDENT_ADMISSION,),
            "atomic_admission": (
                role.DATABASE_ATTESTATION,
                role.INDEPENDENT_ADMISSION,
                role.KERNEL_COMMAND,
            ),
            "continuation_assessment": (role.CONTINUATION_ASSESSMENT,),
        }
        primary_roles = {
            "action_proposal": role.ACTION_PROPOSAL,
            "protocol_compilation": role.PROTOCOL_COMPILATION,
            "execution_authorization": role.EXECUTION_AUTHORIZATION,
            "database_observation": role.DATABASE_ATTESTATION,
            "independent_validation": role.INDEPENDENT_VALIDATION,
            "independent_admission": role.INDEPENDENT_ADMISSION,
            "continuation_assessment": role.CONTINUATION_ASSESSMENT,
        }
        for name, pin in self.rpc_services.named_pins:
            expected_hashes = tuple(
                sorted(bindings[item].binding_sha256 for item in expected_binding_roles[name])
            )
            if pin.authority_binding_sha256s != expected_hashes:
                raise ValueError(f"worker RPC service {name} changed its authority closure")
            primary_role = primary_roles.get(name)
            if primary_role is not None:
                binding = bindings[primary_role]
                if (
                    pin.service_principal_id != binding.principal_id
                    or pin.service_manifest_sha256 != binding.service_manifest_sha256
                    or pin.service_policy_sha256 != binding.policy_sha256
                ):
                    raise ValueError(
                        f"worker RPC service {name} differs from its primary authority"
                    )
            if not pin.valid_from <= self.prepared_at < pin.expires_at:
                raise ValueError("worker RPC receipt key is not valid at config preparation")

        rpc_principals = {pin.service_principal_id for _name, pin in self.rpc_services.named_pins}
        terminal_principals = set(self.terminal_reader.authority_principal_ids)
        trust_root_principals = {
            key.principal_id for key in self.kernel_reader.trust_root.commissioning_keys
        }
        if (
            self.process_principal_id in rpc_principals
            or self.process_principal_id in terminal_principals
            or self.process_principal_id in trust_root_principals
            or rpc_principals & terminal_principals
            or trust_root_principals & (rpc_principals | terminal_principals)
        ):
            raise ValueError("worker, RPC, terminal, or Kernel-root principals overlap")
        expected_id = f"rcwc_{self.configuration_sha256[:32]}"
        if self.configuration_id is not None and self.configuration_id != expected_id:
            raise ValueError("worker runtime config id differs from its payload")
        object.__setattr__(self, "configuration_id", expected_id)
        return self

    @property
    def configuration_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"configuration_id"}))


def _unique_object(pairs):
    duplicates = sorted(
        key for key, count in Counter(key for key, _value in pairs).items() if count > 1
    )
    if duplicates:
        raise ValueError(f"duplicate worker runtime config keys: {duplicates}")
    return dict(pairs)


def load_research_controller_worker_runtime_config(
    configuration_bytes: bytes,
) -> ResearchControllerWorkerRuntimeConfig:
    """Parse one canonical, duplicate-free deployment configuration."""

    try:
        raw = json.loads(configuration_bytes, object_pairs_hook=_unique_object)
        config = ResearchControllerWorkerRuntimeConfig.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise ControllerWorkerCompositionError(
            "controller worker runtime config is invalid"
        ) from exc
    return config


def _authority_bindings(
    manifest: ControllerStepAdapterSetManifest,
) -> dict[ControllerStepAuthorityRole, ControllerStepAuthorityBinding]:
    result: dict[ControllerStepAuthorityRole, ControllerStepAuthorityBinding] = {}
    for adapter in manifest.adapters:
        for binding in adapter.authorities:
            result.setdefault(binding.role, binding)
    return result


def compose_research_controller_worker_service(
    *,
    config: ResearchControllerWorkerRuntimeConfig,
    controller_manifest: ResearchControllerManifest,
    reviewed_code_root: str | Path,
    transport: ControllerWorkerRPCTransport | None = None,
    clock: Callable[[], datetime] | None = None,
    kernel_store: ResearchKernelStore | None = None,
    terminal_outbox: object | None = None,
) -> ResearchControllerService:
    """Build the exact recovery/executor service without loading any scientific private key."""

    try:
        config = ResearchControllerWorkerRuntimeConfig.model_validate(
            config.model_dump(mode="python")
        )
        controller_manifest = ResearchControllerManifest.model_validate(
            controller_manifest.model_dump(mode="python")
        )
        reviewed_root = Path(reviewed_code_root).resolve(strict=True)
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise ControllerWorkerCompositionError("worker composition inputs are invalid") from exc
    if (
        config.controller_id != controller_manifest.controller_id
        or config.controller_manifest_sha256 != controller_manifest.manifest_sha256
        or config.adapter_set_manifest.worker_manifest_sha256
        != controller_manifest.worker_manifest_sha256
        or config.database_url_sha256
        != hashlib.sha256(get_settings().database_url.encode("utf-8")).hexdigest()
        or config.schema_revision != expected_schema_revision()
    ):
        raise ControllerWorkerCompositionError(
            "worker runtime config differs from controller or database state"
        )
    external_principals = (
        {pin.service_principal_id for _name, pin in config.rpc_services.named_pins}
        | set(config.terminal_reader.authority_principal_ids)
        | {key.principal_id for key in config.kernel_reader.trust_root.commissioning_keys}
    )
    if controller_manifest.controller_key in external_principals:
        raise ControllerWorkerCompositionError(
            "controller operational principal overlaps a worker service authority"
        )
    for adapter in config.adapter_set_manifest.adapters:
        if adapter.adapter_code_sha256 != controller_step_adapter_source_sha256(
            adapter.step,
            reviewed_code_root=reviewed_root,
        ):
            raise ControllerWorkerCompositionError(
                f"worker adapter source differs for step {adapter.step.value}"
            )

    terminal_reader = (
        terminal_outbox
        if terminal_outbox is not None
        else compose_qualification_terminal_reader(config.terminal_reader)
    )
    if kernel_store is not None:
        store = kernel_store
    else:
        cas_path = Path(config.kernel_reader.cas_root)
        try:
            if cas_path.resolve(strict=True) != cas_path:
                raise ControllerWorkerCompositionError("Research Kernel CAS traverses a symlink")
            metadata = os.lstat(cas_path)
        except OSError as exc:
            raise ControllerWorkerCompositionError("Research Kernel CAS is unavailable") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != config.kernel_reader.cas_owner_uid
            or metadata.st_gid != config.kernel_reader.cas_group_gid
            or metadata.st_dev != config.kernel_reader.cas_device_id
            or metadata.st_ino != config.kernel_reader.cas_inode
            or stat.S_IMODE(metadata.st_mode) != config.kernel_reader.cas_directory_mode
        ):
            raise ControllerWorkerCompositionError(
                "Research Kernel CAS custody differs from the worker config"
            )
        archive = FilesystemResearchArchive(
            cas_path,
            max_object_bytes=config.kernel_reader.max_object_bytes,
            read_only=True,
        )
        try:
            after = os.lstat(cas_path)
        except OSError as exc:
            raise ControllerWorkerCompositionError(
                "Research Kernel CAS disappeared during composition"
            ) from exc
        if (
            metadata.st_dev != after.st_dev
            or metadata.st_ino != after.st_ino
            or metadata.st_uid != after.st_uid
            or metadata.st_gid != after.st_gid
            or stat.S_IMODE(metadata.st_mode) != stat.S_IMODE(after.st_mode)
        ):
            raise ControllerWorkerCompositionError("Research Kernel CAS changed during composition")
        store = ResearchKernelStore(
            trust_root=config.kernel_reader.trust_root,
            archive=archive,
        )
    bindings = _authority_bindings(config.adapter_set_manifest)

    def client(name: str) -> ControllerWorkerRPCClient:
        return ControllerWorkerRPCClient(
            pin=getattr(config.rpc_services, name),
            controller_id=config.controller_id,
            controller_manifest_sha256=config.controller_manifest_sha256,
            worker_process_principal_id=config.process_principal_id,
            transport=transport,
            clock=clock,
        )

    proposal_service = RPCActionProposalMaterialization(
        client("action_proposal"), bindings[ControllerStepAuthorityRole.ACTION_PROPOSAL]
    )
    compilation_service = RPCProtocolCompilationMaterialization(
        client("protocol_compilation"),
        bindings[ControllerStepAuthorityRole.PROTOCOL_COMPILATION],
    )
    execution_issuer = RPCScientificExecutionAuthorizationIssuer(
        client("execution_authorization"),
        bindings[ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION],
    )
    execution_registrar = RPCScientificExecutionRegistrar(
        client("execution_registration"),
        bindings[ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION],
    )
    raw_runs = RPCRawRunEnvelopeSource(
        client("raw_run_source"),
        bindings[ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION],
    )
    database = RPCDatabaseObservationBridge(
        client("database_observation"),
        bindings[ControllerStepAuthorityRole.DATABASE_ATTESTATION],
    )
    validator = RPCIndependentObservationValidator(
        client("independent_validation"),
        bindings[ControllerStepAuthorityRole.INDEPENDENT_VALIDATION],
    )
    validations = RPCCommittedValidationSource(
        client("committed_validation_source"),
        (
            bindings[ControllerStepAuthorityRole.DATABASE_ATTESTATION],
            bindings[ControllerStepAuthorityRole.INDEPENDENT_VALIDATION],
        ),
    )
    admission = RPCIndependentObservationAdmission(
        client("independent_admission"),
        bindings[ControllerStepAuthorityRole.INDEPENDENT_ADMISSION],
    )
    coordinator = RPCAtomicObservationAdmission(
        client("atomic_admission"),
        database_binding=bindings[ControllerStepAuthorityRole.DATABASE_ATTESTATION],
        admission_binding=bindings[ControllerStepAuthorityRole.INDEPENDENT_ADMISSION],
        kernel_binding=bindings[ControllerStepAuthorityRole.KERNEL_COMMAND],
    )
    continuation = RPCContinuationMaterialization(
        client("continuation_assessment"),
        bindings[ControllerStepAuthorityRole.CONTINUATION_ASSESSMENT],
    )

    manifests = {adapter.step: adapter for adapter in config.adapter_set_manifest.adapters}
    adapters = (
        ActionProposalStepAdapter(
            manifest=manifests[ControllerStep.PROPOSE_ACTION], proposals=proposal_service
        ),
        ProtocolCompilationStepAdapter(
            manifest=manifests[ControllerStep.COMPILE_PROTOCOL],
            compilations=compilation_service,
        ),
        ActionProposalStepAdapter(
            manifest=manifests[ControllerStep.PROPOSE_REDESIGN], proposals=proposal_service
        ),
        QualifiedExecutionRegistrationStepAdapter(
            manifest=manifests[ControllerStep.REGISTER_EXECUTION],
            issuer=execution_issuer,
            registrar=execution_registrar,
        ),
        IndependentObservationValidationStepAdapter(
            manifest=manifests[ControllerStep.COMMIT_VALIDATION],
            raw_runs=raw_runs,
            database=database,
            validator=validator,
        ),
        AtomicObservationAdmissionStepAdapter(
            manifest=manifests[ControllerStep.COMMIT_ADMISSION],
            validations=validations,
            database=database,
            admission=admission,
            coordinator=coordinator,
        ),
        ContinuationAssessmentStepAdapter(
            manifest=manifests[ControllerStep.DERIVE_CONTINUATION],
            assessments=continuation,
        ),
        ActionProposalStepAdapter(
            manifest=manifests[ControllerStep.PROPOSE_FOLLOWUP], proposals=proposal_service
        ),
    )
    executor = DedicatedControllerStepExecutor(
        controller_manifest=controller_manifest,
        worker_process_principal_id=config.process_principal_id,
        manifest=config.adapter_set_manifest,
        adapters=adapters,
    )
    recovery = PostgreSQLControllerRecoveryAdapter(
        kernel_store=store,
        terminal_outbox=terminal_reader,
        manifest=controller_manifest,
    )
    for adapter in config.adapter_set_manifest.adapters:
        if adapter.adapter_code_sha256 != controller_step_adapter_source_sha256(
            adapter.step,
            reviewed_code_root=reviewed_root,
        ):
            raise ControllerWorkerCompositionError(
                f"worker adapter source changed during composition for {adapter.step.value}"
            )
    return ResearchControllerService(recovery=recovery, executor=executor)


def validate_worker_deployment_binding(
    *,
    config: ResearchControllerWorkerRuntimeConfig,
    role: str,
    process_principal_id: str,
    controller_manifest: ResearchControllerManifest,
    prepared_at: datetime,
) -> None:
    """Bind the parsed config to the outer byte-pinned runtime deployment."""

    if (
        role != "worker"
        or config.role != role
        or config.process_principal_id != process_principal_id
        or config.controller_id != controller_manifest.controller_id
        or config.controller_manifest_sha256 != controller_manifest.manifest_sha256
        or config.prepared_at != prepared_at
    ):
        raise ControllerWorkerCompositionError(
            "controller worker config differs from the outer deployment"
        )


__all__ = [
    "ControllerWorkerCompositionError",
    "ControllerWorkerRPCServiceSet",
    "ResearchKernelReadOnlyConfig",
    "ResearchControllerWorkerRuntimeConfig",
    "compose_research_controller_worker_service",
    "controller_step_adapter_source_sha256",
    "controller_step_rpc_configuration_sha256",
    "load_research_controller_worker_runtime_config",
    "validate_worker_deployment_binding",
]
