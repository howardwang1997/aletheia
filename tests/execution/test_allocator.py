"""PostgreSQL integration tests for the PR-4a fenced allocation authority.

The resolver doubles below are deliberately test-only.  This repository does not yet contain a
deployable quote/source-budget registry adapter, and the allocator has no permissive default.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
import hashlib
from pathlib import Path
import secrets
import sys
from threading import Barrier

import pytest
from sqlalchemy import null, select, text
from sqlalchemy.exc import DBAPIError

import aletheia.execution.allocator as allocator_module
from aletheia.db import session_factory
from aletheia.execution.assignment_contracts import (
    NodeAssignmentTransportPin,
    QualificationAssignmentSecret,
    node_transport_key_id,
    open_qualification_assignment,
    seal_qualification_assignment,
    x25519_public_key_hex,
)
from aletheia.protocols.capabilities import DataClassification
from aletheia.execution.allocator import (
    AdmissionConflict,
    CapacityUnavailable,
    InventoryRejected,
    LeaseAuthorityError,
    LocalPricingAuthorityPin,
    PostgreSQLExecutionAllocator,
    PostgreSQLExecutionReceiptArchive,
)
from aletheia.execution.persistence import (
    _ExecutionAssignmentEnvelopeRecord,
    _ExecutionAttemptRecord,
    _ExecutionBudgetAuthorizationRecord,
    _ExecutionBudgetEventRecord,
    _ExecutionBudgetHeadRecord,
    _ExecutionBudgetReservationRecord,
    _ExecutionHeadRecord,
    _ExecutionNodeRecord,
    _ExecutionQualificationAdmissionRecord,
    _ExecutionResourceLeaseRecord,
)
from aletheia.execution.runtime_contracts import (
    AttemptAdoptionReason,
    EngineeringQualificationBundle,
    ExecutionCostQuote,
    NodeHealth,
    NodeInventoryResource,
    RuntimeInspectionState,
    TerminalVerificationAuthorityPin,
    TerminalVerificationAuthorityVerifier,
    VerifiedInputArtifactResolution,
    QualificationAuthorityVerifier,
    issue_attempt_adoption_receipt,
    issue_engineering_qualification_grant,
    issue_node_inventory_attestation,
    issue_node_execution_receipt,
    issue_runtime_inspection_receipt,
    issue_terminal_verification_attestation,
    qualification_key_id,
)
from aletheia.execution.schemas import (
    ArtifactCustodyMode,
    ArtifactManifest,
    ArtifactVerifiedReceipt,
    DataLocality,
    ExecutionFailure,
    ExecutionFailureCategory,
    ExecutionReceipt,
    ExecutionTerminalState,
    InputArtifactBinding,
    ResourceKind,
    StaticResourceClass,
    canonical_sha256,
)
from aletheia.protocols.compiler import compile_protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))
from postgres_test_safety import require_isolated_pr4_postgres  # noqa: E402
from test_runtime_contracts import (  # noqa: E402
    PRIVATE_KEY,
    TERMINAL_PRIVATE_KEY,
    _AuthorityResolver,
    _digest,
    _intent,
    _output_manifest,
    _protocol_input_resolution,
    _public_key_hex,
    _qualification_case,
    _retryable_compilation,
    _runtime_identity,
    _signed_case,
    _worker_authority,
    _worker_manifest,
)

_EXECUTION_TABLES = (
    "execution_outbox",
    "execution_terminal_receipts",
    "execution_budget_events",
    "execution_budget_reservations",
    "execution_device_leases",
    "execution_resource_leases",
    "execution_attempt_adoptions",
    "execution_assignment_envelopes",
    "execution_attempts",
    "execution_heads",
    "execution_budget_heads",
    "execution_budget_authorizations",
    "execution_qualification_admissions",
    "execution_device_heads",
    "execution_inventory_devices",
    "execution_inventory_attestations",
    "execution_nodes",
)

TRANSPORT_PRIVATE_KEY = bytes.fromhex("71" * 32)


@pytest.fixture(autouse=True)
def _clean_execution_authority_tables():
    require_isolated_pr4_postgres()
    sessions = session_factory()
    with sessions() as session, session.begin():
        session.execute(text(f"TRUNCATE {', '.join(_EXECUTION_TABLES)} RESTART IDENTITY CASCADE"))
    yield
    require_isolated_pr4_postgres()
    with sessions() as session, session.begin():
        session.execute(text(f"TRUNCATE {', '.join(_EXECUTION_TABLES)} RESTART IDENTITY CASCADE"))


class _TestArtifactResolver:
    def __init__(self, resolution) -> None:
        self._resolutions = {resolution.verified_receipt_sha256: resolution}
        self._manifests: dict[str, object] = {}

    def add_manifest(self, manifest) -> None:
        self._manifests[manifest.manifest_sha256] = manifest

    def add_resolution(self, resolution) -> None:
        self._resolutions[resolution.verified_receipt_sha256] = resolution

    def resolve_artifact_manifest(self, *, manifest_sha256: str, observed_at):
        del observed_at
        return self._manifests.get(manifest_sha256)

    def resolve_verified_input_artifact(self, *, verified_receipt_sha256: str, observed_at):
        resolution = self._resolutions.get(verified_receipt_sha256)
        if resolution is None:
            return None
        return resolution.model_copy(update={"resolved_at": observed_at})


@dataclass(frozen=True)
class _Prepared:
    allocator: PostgreSQLExecutionAllocator
    bundle: EngineeringQualificationBundle
    grant: object
    manifest: object
    inventory: object
    observed_at: object
    artifacts: _TestArtifactResolver
    terminal_pin: TerminalVerificationAuthorityPin
    transport_pin: NodeAssignmentTransportPin
    case: object


def _transport_pin(manifest, *, observed_at) -> NodeAssignmentTransportPin:
    public_key = x25519_public_key_hex(TRANSPORT_PRIVATE_KEY)
    return NodeAssignmentTransportPin(
        node_id=manifest.node_id,
        node_manifest_sha256=manifest.manifest_sha256,
        transport_policy_sha256=_digest("allocator-assignment-transport-policy:v1"),
        transport_principal_id="principal:node_assignment_transport",
        transport_key_id=node_transport_key_id(public_key),
        public_key_x25519_hex=public_key,
        valid_from=observed_at - timedelta(days=1),
        expires_at=observed_at + timedelta(days=1),
    )


def _accelerator_qualification_case(*, accelerator_count: int, retryable: bool):
    base = _qualification_case()
    base_request = _retryable_compilation()[0] if retryable else base.request
    target_step = next(
        item for item in base_request.protocol.steps if item.step_id == "step.01_group"
    )
    cpu_class = next(
        item
        for item in base_request.resource_catalog.resource_classes
        if item.resource_class_id in target_step.resource_request.accepted_resource_class_ids
    )
    accelerator_model = "Tesla V100-SXM2"
    accelerator_memory_bytes = 16 * 1024**3
    accelerator_class = StaticResourceClass.model_validate(
        {
            **cpu_class.model_dump(mode="python"),
            "class_key": "allocator-v100-16gb",
            "kind": ResourceKind.ACCELERATOR,
            "accelerator_model": accelerator_model,
            "accelerator_count": accelerator_count,
            "accelerator_memory_bytes": accelerator_memory_bytes,
            "accelerator_compute_capability": "7.0",
        }
    )
    resource_request = type(target_step.resource_request).model_validate(
        {
            **target_step.resource_request.model_dump(mode="python"),
            "accepted_resource_class_ids": tuple(
                sorted(
                    (
                        *target_step.resource_request.accepted_resource_class_ids,
                        accelerator_class.resource_class_id,
                    )
                )
            ),
            "accelerator_count": accelerator_count,
            "allowed_accelerator_models": (accelerator_model,),
            "minimum_accelerator_memory_bytes": 8 * 1024**3,
            "minimum_compute_capability": "7.0",
        }
    )
    updated_step = type(target_step).model_validate(
        {
            **target_step.model_dump(mode="python"),
            "resource_request": resource_request,
        }
    )
    protocol = type(base_request.protocol).model_validate(
        {
            **base_request.protocol.model_dump(mode="python"),
            "steps": tuple(
                updated_step if item.step_id == updated_step.step_id else item
                for item in base_request.protocol.steps
            ),
        }
    )
    resource_catalog = type(base_request.resource_catalog).model_validate(
        {
            **base_request.resource_catalog.model_dump(mode="python"),
            "resource_classes": tuple(
                sorted(
                    (*base_request.resource_catalog.resource_classes, accelerator_class),
                    key=lambda item: item.resource_class_id,
                )
            ),
        }
    )
    request = type(base_request).model_validate(
        {
            **base_request.model_dump(mode="python"),
            "protocol": protocol,
            "resource_catalog": resource_catalog,
        }
    )
    result = compile_protocol(request)
    assert result.work_order is not None, result.report.blockers
    node = next(
        item for item in result.work_order.nodes if item.protocol_step_id == updated_step.step_id
    )
    resolution = _protocol_input_resolution(
        request=request,
        input_port_id=node.input_port_ids[0],
        resolved_at=base.observed_at,
    )
    intent = _intent(
        work_order=result.work_order,
        node=node,
        input_bindings=(
            InputArtifactBinding(
                input_port_id=node.input_port_ids[0],
                source_kind="protocol_input",
                artifact_verified_receipt_sha256=resolution.verified_receipt_sha256,
            ),
        ),
    )
    return _signed_case(
        request=request,
        result=result,
        intent=intent,
        resolution=resolution,
        observed_at=base.observed_at,
    )


def _prepared(
    monkeypatch: pytest.MonkeyPatch,
    *,
    accelerator_count: int = 0,
    inventory_accelerator_count: int | None = None,
    retryable: bool = False,
    artifact_quota_bytes: int | None = None,
    initial_assignment_lease_seconds: int | None = None,
) -> _Prepared:
    case = (
        _accelerator_qualification_case(
            accelerator_count=accelerator_count,
            retryable=retryable,
        )
        if accelerator_count
        else _qualification_case()
    )
    if artifact_quota_bytes is not None:
        original_node = next(
            item
            for item in case.bundle.work_order.nodes
            if item.node_id == case.bundle.intent.work_order_node_id
        )
        protocol = case.request.protocol
        steps = tuple(
            item.model_copy(
                update={
                    "resource_request": item.resource_request.model_copy(
                        update={"artifact_quota_bytes": artifact_quota_bytes}
                    )
                }
            )
            if item.step_id == original_node.protocol_step_id
            else item
            for item in protocol.steps
        )
        maximum_total_artifact_bytes = sum(
            item.resource_request.artifact_quota_bytes * item.scientific_replicate_count
            for item in steps
        )
        request = case.request.model_copy(
            update={
                "protocol": protocol.model_copy(
                    update={
                        "steps": steps,
                        "resource_budget": protocol.resource_budget.model_copy(
                            update={"maximum_total_artifact_bytes": (maximum_total_artifact_bytes)}
                        ),
                    }
                )
            }
        )
        result = compile_protocol(request)
        assert result.work_order is not None
        node = next(
            item
            for item in result.work_order.nodes
            if item.protocol_step_id == original_node.protocol_step_id
        )
        resolution = _protocol_input_resolution(
            request=request,
            input_port_id=node.input_port_ids[0],
            resolved_at=case.observed_at,
        )
        intent = _intent(
            work_order=result.work_order,
            node=node,
            input_bindings=(
                InputArtifactBinding(
                    input_port_id=node.input_port_ids[0],
                    source_kind="protocol_input",
                    artifact_verified_receipt_sha256=resolution.verified_receipt_sha256,
                ),
            ),
        )
        case = _signed_case(
            request=request,
            result=result,
            intent=intent,
            resolution=resolution,
            observed_at=case.observed_at,
        )
    request = case.bundle.intent.resource_request
    static_class = next(
        item
        for item in case.bundle.compilation_request.resource_catalog.resource_classes
        if item.resource_class_id in request.accepted_resource_class_ids
    )
    base_manifest = _worker_manifest()
    manifest = type(base_manifest).model_validate(
        {
            **base_manifest.model_dump(mode="python"),
            "resource_class_ids": request.accepted_resource_class_ids,
            "container_runtime": static_class.container_runtime,
            "allowed_data_classifications": tuple(
                sorted({item.data_classification for item in case.bundle.intent.expected_artifacts})
            ),
        }
    )
    authority = _worker_authority(manifest, observed_at=case.observed_at)
    quote = ExecutionCostQuote.model_validate(
        {
            **case.bundle.cost_quote.model_dump(mode="python"),
            "permitted_node_manifest_sha256s": (manifest.manifest_sha256,),
            "selected_node_manifest_sha256": manifest.manifest_sha256,
            "selected_resource_ids": (
                (
                    "cpu.socket-0",
                    *(f"gpu.{index}" for index in range(accelerator_count)),
                )
                if accelerator_count
                else ("cpu.socket-0",)
            ),
        }
    )
    bundle = EngineeringQualificationBundle.model_validate(
        {**case.bundle.model_dump(mode="python"), "cost_quote": quote}
    )
    authority_resolver = _AuthorityResolver(bundle)
    artifacts = _TestArtifactResolver(case.resolution)
    grant = issue_engineering_qualification_grant(
        bundle,
        pin=case.pin,
        artifact_resolver=artifacts,
        authority_resolver=authority_resolver,
        private_key=PRIVATE_KEY,
        authorized_at=case.grant.message.authorized_at,
        expires_at=case.grant.message.expires_at,
    )
    cpu = NodeInventoryResource(
        resource_id="cpu.socket-0",
        kind=ResourceKind.CPU,
        resource_class_ids=request.accepted_resource_class_ids,
        health=NodeHealth.HEALTHY,
        cpu_cores_total=3,
        cpu_cores_safety_reserve=0,
        cpu_cores_managed_occupied=0,
        cpu_cores_external_occupied=0,
        cpu_cores_allocatable=3,
        memory_bytes_total=3_221_225_472,
        memory_bytes_safety_reserve=0,
        memory_bytes_managed_occupied=0,
        memory_bytes_external_occupied=0,
        memory_bytes_allocatable=3_221_225_472,
        scratch_bytes_total=6_442_450_944,
        scratch_bytes_safety_reserve=0,
        scratch_bytes_managed_occupied=0,
        scratch_bytes_external_occupied=0,
        scratch_bytes_allocatable=6_442_450_944,
        features=request.required_features,
        external_process_count=0,
    )
    inventory_resources = [cpu]
    if accelerator_count:
        inventory_accelerator_count = inventory_accelerator_count or accelerator_count
        accelerator_class_id = next(
            item
            for item in request.accepted_resource_class_ids
            if next(
                candidate
                for candidate in case.bundle.compilation_request.resource_catalog.resource_classes
                if candidate.resource_class_id == item
            ).kind
            is ResourceKind.ACCELERATOR
        )
        inventory_resources.extend(
            NodeInventoryResource(
                resource_id=f"gpu.{index}",
                kind=ResourceKind.ACCELERATOR,
                resource_class_ids=(accelerator_class_id,),
                health=NodeHealth.HEALTHY,
                cpu_cores_total=0,
                cpu_cores_safety_reserve=0,
                cpu_cores_managed_occupied=0,
                cpu_cores_external_occupied=0,
                cpu_cores_allocatable=0,
                memory_bytes_total=0,
                memory_bytes_safety_reserve=0,
                memory_bytes_managed_occupied=0,
                memory_bytes_external_occupied=0,
                memory_bytes_allocatable=0,
                scratch_bytes_total=0,
                scratch_bytes_safety_reserve=0,
                scratch_bytes_managed_occupied=0,
                scratch_bytes_external_occupied=0,
                scratch_bytes_allocatable=0,
                accelerator_uuid=f"gpu.uuid-{index}",
                accelerator_model="Tesla V100-SXM2",
                accelerator_memory_bytes_total=16 * 1024**3,
                accelerator_memory_bytes_safety_reserve=0,
                accelerator_memory_bytes_managed_occupied=0,
                accelerator_memory_bytes_external_occupied=0,
                accelerator_memory_bytes_allocatable=16 * 1024**3,
                accelerator_compute_capability="7.0",
                features=request.required_features,
                external_process_count=0,
            )
            for index in range(inventory_accelerator_count)
        )
    inventory = issue_node_inventory_attestation(
        manifest=manifest,
        boot_id="boot.001",
        sequence=1,
        observed_monotonic_ns=1_000,
        resources=tuple(inventory_resources),
        collector_implementation_sha256=_digest("allocator-test-collector"),
        collector_output_sha256=_digest("allocator-test-inventory"),
        observed_at=case.observed_at,
        expires_at=case.observed_at + timedelta(seconds=30),
        private_key=PRIVATE_KEY,
    )
    terminal_public_key = _public_key_hex(TERMINAL_PRIVATE_KEY)
    terminal_pin = TerminalVerificationAuthorityPin(
        policy_sha256=_digest("allocator-terminal-verification-policy:v1"),
        principal_id="principal:execution-verifier",
        key_id=qualification_key_id(terminal_public_key),
        public_key_ed25519_hex=terminal_public_key,
        valid_from=case.observed_at - timedelta(days=1),
        expires_at=case.observed_at + timedelta(days=1),
    )
    transport_pin = _transport_pin(manifest, observed_at=case.observed_at)
    allocator = PostgreSQLExecutionAllocator(
        authority=QualificationAuthorityVerifier(case.pin),
        artifact_resolver=artifacts,
        execution_authority_resolver=authority_resolver,
        pricing_authority=LocalPricingAuthorityPin(
            quote_principal_ids=frozenset({quote.quoted_by_principal_id}),
            rate_card_sha256s=frozenset({quote.rate_card_sha256}),
            pricing_policy_sha256s=frozenset({quote.pricing_policy_sha256}),
            currency_codes=frozenset({quote.currency_code}),
        ),
        node_authorities=(authority,),
        node_assignment_transport_pins=(transport_pin,),
        terminal_verification_authority=TerminalVerificationAuthorityVerifier(terminal_pin),
        allocator_principal_id="principal:allocator",
        max_inventory_ttl_seconds=30,
        heartbeat_extension_seconds=15,
        initial_assignment_lease_seconds=initial_assignment_lease_seconds,
    )
    monkeypatch.setattr(allocator_module, "_database_time", lambda _session: case.observed_at)
    return _Prepared(
        allocator=allocator,
        bundle=bundle,
        grant=grant,
        manifest=manifest,
        inventory=inventory,
        observed_at=case.observed_at,
        artifacts=artifacts,
        terminal_pin=terminal_pin,
        transport_pin=transport_pin,
        case=case,
    )


def _register_and_inventory(prepared: _Prepared) -> None:
    registration = prepared.allocator.register_node(prepared.manifest.node_id)
    assert registration.created is True
    append = prepared.allocator.append_inventory(prepared.inventory)
    assert append.created is True


def test_atomic_admission_is_exactly_idempotent_and_token_is_one_time(monkeypatch) -> None:
    prepared = _prepared(monkeypatch)
    _register_and_inventory(prepared)
    sessions = session_factory()
    with sessions() as session, session.begin():
        assert (
            prepared.allocator.load_exact_qualification_reservation_in_session(
                session,
                bundle=prepared.bundle,
                grant=prepared.grant,
            )
            is None
        )

    first = prepared.allocator.admit_and_reserve(bundle=prepared.bundle, grant=prepared.grant)
    with sessions() as session, session.begin():
        assert (
            prepared.allocator.load_exact_qualification_reservation_in_session(
                session,
                bundle=prepared.bundle,
                grant=prepared.grant,
            )
            == first.snapshot
        )
    monkeypatch.setattr(
        allocator_module,
        "_database_time",
        lambda _session: prepared.observed_at + timedelta(seconds=1),
    )
    replay = prepared.allocator.admit_and_reserve(bundle=prepared.bundle, grant=prepared.grant)

    assert first.created is True and first.lease_token is not None
    assert replay.created is False and replay.lease_token is None
    assert replay.snapshot == first.snapshot
    delivery = prepared.allocator.pull_sealed_assignment(
        node_id=prepared.manifest.node_id,
        node_manifest_sha256=prepared.manifest.manifest_sha256,
    )
    assert delivery is not None
    opened = open_qualification_assignment(
        envelope=delivery.envelope,
        transport_pin=prepared.transport_pin,
        node_transport_private_key=TRANSPORT_PRIVATE_KEY,
        observed_at=prepared.observed_at + timedelta(seconds=1),
    )
    assert opened.lease_token == first.lease_token
    assert opened.lease_token_sha256 == first.snapshot.lease_token_sha256
    monkeypatch.setattr(
        allocator_module,
        "_database_time",
        lambda _session: first.snapshot.lease_expires_at,
    )
    assert (
        prepared.allocator.pull_sealed_assignment(
            node_id=prepared.manifest.node_id,
            node_manifest_sha256=prepared.manifest.manifest_sha256,
        )
        is None
    )
    with session_factory()() as session:
        attempt = session.get(_ExecutionAttemptRecord, first.snapshot.attempt_id)
        envelope_record = session.execute(
            select(_ExecutionAssignmentEnvelopeRecord).where(
                _ExecutionAssignmentEnvelopeRecord.attempt_id == first.snapshot.attempt_id
            )
        ).scalar_one()
        budget = session.get(_ExecutionBudgetHeadRecord, first.snapshot.budget_authorization_sha256)
        assert attempt is not None
        assert first.lease_token not in str(attempt.__dict__)
        assert first.lease_token not in str(envelope_record.__dict__)
        assert budget is not None
        assert budget.reserved_microunits == first.snapshot.held_microunits


def test_caller_owned_admission_transaction_rolls_back_without_an_orphan_attempt(
    monkeypatch,
) -> None:
    prepared = _prepared(monkeypatch)
    _register_and_inventory(prepared)
    attempt_id = prepared.bundle.intent.infrastructure_attempt.infrastructure_attempt_id
    sessions = session_factory()

    with pytest.raises(RuntimeError, match="injected caller crash"):
        with sessions() as session, session.begin():
            claim = prepared.allocator.admit_and_reserve_in_session(
                session,
                bundle=prepared.bundle,
                grant=prepared.grant,
            )
            assert claim.created is True
            assert session.get(_ExecutionAttemptRecord, attempt_id) is not None
            raise RuntimeError("injected caller crash")

    with sessions() as session:
        assert session.get(_ExecutionAttemptRecord, attempt_id) is None

    committed = prepared.allocator.admit_and_reserve(
        bundle=prepared.bundle,
        grant=prepared.grant,
    )
    assert committed.created is True
    with sessions() as session:
        assert session.get(_ExecutionAttemptRecord, attempt_id) is not None


def test_caller_owned_admission_requires_an_active_transaction(monkeypatch) -> None:
    prepared = _prepared(monkeypatch)
    with session_factory()() as session:
        with pytest.raises(ValueError, match="active Session transaction"):
            prepared.allocator.admit_and_reserve_in_session(
                session,
                bundle=prepared.bundle,
                grant=prepared.grant,
            )
        with pytest.raises(ValueError, match="active Session transaction"):
            prepared.allocator.load_exact_qualification_reservation_in_session(
                session,
                bundle=prepared.bundle,
                grant=prepared.grant,
            )


@pytest.mark.parametrize("_iteration", range(8))
def test_concurrent_exact_admission_mints_only_one_raw_token(
    monkeypatch,
    _iteration: int,
) -> None:
    prepared = _prepared(monkeypatch)
    _register_and_inventory(prepared)
    start = Barrier(2)

    def admit():
        start.wait(timeout=10)
        return prepared.allocator.admit_and_reserve(bundle=prepared.bundle, grant=prepared.grant)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = tuple(pool.map(lambda _item: admit(), range(2)))

    assert sorted(item.created for item in claims) == [False, True]
    assert sum(item.lease_token is not None for item in claims) == 1
    with session_factory()() as session:
        reservations = tuple(session.execute(select(_ExecutionBudgetReservationRecord)).scalars())
        assert len(reservations) == 1


def test_final_locked_clock_rejects_inventory_expiring_during_admission(
    monkeypatch,
) -> None:
    prepared = _prepared(monkeypatch)
    _register_and_inventory(prepared)
    clock_samples = iter(
        (
            prepared.inventory.expires_at - timedelta(seconds=1),
            prepared.inventory.expires_at + timedelta(seconds=1),
        )
    )
    monkeypatch.setattr(allocator_module, "_database_time", lambda _session: next(clock_samples))

    with pytest.raises(InventoryRejected, match="expired while waiting"):
        prepared.allocator.admit_and_reserve(bundle=prepared.bundle, grant=prepared.grant)

    with session_factory()() as session:
        assert (
            session.get(
                _ExecutionAttemptRecord,
                prepared.bundle.intent.infrastructure_attempt.infrastructure_attempt_id,
            )
            is None
        )


def test_terminal_verification_pin_must_cover_the_full_lease(monkeypatch) -> None:
    prepared = _prepared(monkeypatch)
    _register_and_inventory(prepared)

    def allocator_with_pin(
        pin: TerminalVerificationAuthorityPin,
    ) -> PostgreSQLExecutionAllocator:
        return PostgreSQLExecutionAllocator(
            authority=prepared.allocator._authority,
            artifact_resolver=prepared.artifacts,
            execution_authority_resolver=prepared.allocator._execution_authority_resolver,
            pricing_authority=prepared.allocator._pricing_authority,
            node_authorities=tuple(prepared.allocator._node_authorities.values()),
            node_assignment_transport_pins=tuple(
                prepared.allocator._node_assignment_transport_pins.values()
            ),
            terminal_verification_authority=TerminalVerificationAuthorityVerifier(pin),
            allocator_principal_id="principal:allocator",
        )

    expired_pin = TerminalVerificationAuthorityPin.model_validate(
        {
            **prepared.terminal_pin.model_dump(mode="python"),
            "expires_at": prepared.observed_at,
        }
    )
    with pytest.raises(AdmissionConflict, match="not active at locked DB time"):
        allocator_with_pin(expired_pin).admit_and_reserve(
            bundle=prepared.bundle,
            grant=prepared.grant,
        )

    short_pin = TerminalVerificationAuthorityPin.model_validate(
        {
            **prepared.terminal_pin.model_dump(mode="python"),
            "expires_at": prepared.observed_at + timedelta(seconds=30),
        }
    )
    with pytest.raises(AdmissionConflict, match="full quoted lease"):
        allocator_with_pin(short_pin).admit_and_reserve(
            bundle=prepared.bundle,
            grant=prepared.grant,
        )

    with session_factory()() as session:
        assert (
            session.get(
                _ExecutionAttemptRecord,
                prepared.bundle.intent.infrastructure_attempt.infrastructure_attempt_id,
            )
            is None
        )


def test_inventory_refresh_reconciles_majority_managed_occupancy(monkeypatch) -> None:
    prepared = _prepared(monkeypatch)
    _register_and_inventory(prepared)
    claim = prepared.allocator.admit_and_reserve(bundle=prepared.bundle, grant=prepared.grant)
    request = prepared.bundle.intent.resource_request
    original = prepared.inventory.resources[0]
    refreshed_cpu = NodeInventoryResource.model_validate(
        {
            **original.model_dump(mode="python"),
            "cpu_cores_managed_occupied": request.cpu_cores,
            "cpu_cores_allocatable": original.cpu_cores_allocatable - request.cpu_cores,
            "memory_bytes_managed_occupied": request.memory_bytes,
            "memory_bytes_allocatable": original.memory_bytes_allocatable - request.memory_bytes,
            "scratch_bytes_managed_occupied": request.scratch_bytes,
            "scratch_bytes_allocatable": original.scratch_bytes_allocatable - request.scratch_bytes,
        }
    )
    refreshed_at = prepared.observed_at + timedelta(seconds=5)
    refreshed = issue_node_inventory_attestation(
        manifest=prepared.manifest,
        boot_id=prepared.inventory.boot_id,
        sequence=2,
        observed_monotonic_ns=2_000,
        resources=(refreshed_cpu,),
        collector_implementation_sha256=_digest("allocator-test-collector"),
        collector_output_sha256=_digest("allocator-test-inventory-refresh"),
        observed_at=refreshed_at,
        expires_at=refreshed_at + timedelta(seconds=30),
        private_key=PRIVATE_KEY,
    )
    monkeypatch.setattr(allocator_module, "_database_time", lambda _session: refreshed_at)

    appended = prepared.allocator.append_inventory(refreshed)

    assert appended.created is True
    assert claim.snapshot.cpu_cores * 2 > original.cpu_cores_total
    with session_factory()() as session:
        node = session.get(_ExecutionNodeRecord, claim.snapshot.node_id)
        assert node is not None
        assert node.current_inventory_sha256 == refreshed.inventory_sha256
        assert node.reserved_cpu_cores == request.cpu_cores


def test_stale_first_inventory_and_failed_placement_rollback(monkeypatch) -> None:
    prepared = _prepared(monkeypatch)
    prepared.allocator.register_node(prepared.manifest.node_id)
    stale = issue_node_inventory_attestation(
        manifest=prepared.manifest,
        boot_id="boot.stale",
        sequence=1,
        observed_monotonic_ns=500,
        resources=prepared.inventory.resources,
        collector_implementation_sha256=_digest("allocator-test-stale-collector"),
        collector_output_sha256=_digest("allocator-test-stale-inventory"),
        observed_at=prepared.observed_at - timedelta(minutes=10),
        expires_at=prepared.observed_at + timedelta(seconds=10),
        private_key=PRIVATE_KEY,
    )
    with pytest.raises(InventoryRejected, match="age/window"):
        prepared.allocator.append_inventory(stale)

    _register = prepared.allocator.append_inventory(prepared.inventory)
    assert _register.created is True
    bad_quote = ExecutionCostQuote.model_validate(
        {
            **prepared.bundle.cost_quote.model_dump(mode="python"),
            "selected_resource_ids": ("cpu.missing",),
        }
    )
    bad_bundle = EngineeringQualificationBundle.model_validate(
        {**prepared.bundle.model_dump(mode="python"), "cost_quote": bad_quote}
    )
    bad_resolver = _AuthorityResolver(bad_bundle)
    bad_grant = issue_engineering_qualification_grant(
        bad_bundle,
        pin=prepared.allocator._authority.pin,
        artifact_resolver=prepared.artifacts,
        authority_resolver=bad_resolver,
        private_key=PRIVATE_KEY,
        authorized_at=prepared.grant.message.authorized_at,
        expires_at=prepared.grant.message.expires_at,
    )
    failing = PostgreSQLExecutionAllocator(
        authority=prepared.allocator._authority,
        artifact_resolver=prepared.artifacts,
        execution_authority_resolver=bad_resolver,
        pricing_authority=prepared.allocator._pricing_authority,
        node_authorities=tuple(prepared.allocator._node_authorities.values()),
        node_assignment_transport_pins=tuple(
            prepared.allocator._node_assignment_transport_pins.values()
        ),
        terminal_verification_authority=(prepared.allocator._terminal_verification_authority),
        allocator_principal_id="principal:allocator",
    )
    with pytest.raises(CapacityUnavailable, match="resource id|placement|absent"):
        failing.admit_and_reserve(bundle=bad_bundle, grant=bad_grant)
    with session_factory()() as session:
        assert (
            session.get(
                _ExecutionAttemptRecord,
                bad_bundle.intent.infrastructure_attempt.infrastructure_attempt_id,
            )
            is None
        )


def test_multi_accelerator_and_terminal_key_reuse_fail_closed(monkeypatch) -> None:
    prepared = _prepared(monkeypatch, accelerator_count=2)
    _register_and_inventory(prepared)

    with pytest.raises(CapacityUnavailable, match="at most one local accelerator"):
        prepared.allocator.admit_and_reserve(bundle=prepared.bundle, grant=prepared.grant)

    qualification_pin = prepared.allocator._authority.pin
    colliding_terminal_pin = TerminalVerificationAuthorityPin(
        policy_sha256=_digest("allocator-colliding-terminal-policy"),
        principal_id="principal:execution-verifier",
        key_id=qualification_pin.key_id,
        public_key_ed25519_hex=qualification_pin.public_key_ed25519_hex,
        valid_from=qualification_pin.valid_from,
        expires_at=qualification_pin.expires_at,
    )
    enrollment_pin = next(
        iter(prepared.allocator._node_authorities.values())
    ).enrollment_authority_pin
    enrollment_key_collision = TerminalVerificationAuthorityPin(
        policy_sha256=_digest("allocator-enrollment-key-collision"),
        principal_id="principal:execution-verifier.other",
        key_id=enrollment_pin.key_id,
        public_key_ed25519_hex=enrollment_pin.public_key_ed25519_hex,
        valid_from=enrollment_pin.valid_from,
        expires_at=enrollment_pin.expires_at,
    )
    principal_collision = prepared.terminal_pin.model_copy(
        update={"principal_id": prepared.manifest.principal_id}
    )

    def build_with_terminal_pin(pin: TerminalVerificationAuthorityPin) -> None:
        PostgreSQLExecutionAllocator(
            authority=prepared.allocator._authority,
            artifact_resolver=prepared.artifacts,
            execution_authority_resolver=prepared.allocator._execution_authority_resolver,
            pricing_authority=prepared.allocator._pricing_authority,
            node_authorities=tuple(prepared.allocator._node_authorities.values()),
            node_assignment_transport_pins=tuple(
                prepared.allocator._node_assignment_transport_pins.values()
            ),
            terminal_verification_authority=TerminalVerificationAuthorityVerifier(pin),
            allocator_principal_id="principal:allocator",
        )

    for colliding_pin in (
        colliding_terminal_pin,
        enrollment_key_collision,
        principal_collision,
    ):
        with pytest.raises(ValueError, match="must be distinct"):
            build_with_terminal_pin(colliding_pin)

    with pytest.raises(ValueError, match="allocator role must be distinct"):
        PostgreSQLExecutionAllocator(
            authority=prepared.allocator._authority,
            artifact_resolver=prepared.artifacts,
            execution_authority_resolver=prepared.allocator._execution_authority_resolver,
            pricing_authority=prepared.allocator._pricing_authority,
            node_authorities=tuple(prepared.allocator._node_authorities.values()),
            node_assignment_transport_pins=tuple(
                prepared.allocator._node_assignment_transport_pins.values()
            ),
            terminal_verification_authority=(prepared.allocator._terminal_verification_authority),
            allocator_principal_id=qualification_pin.principal_id,
        )


def test_region_and_wrong_site_locality_fail_closed(monkeypatch) -> None:
    prepared = _prepared(monkeypatch)
    _register_and_inventory(prepared)
    with session_factory()() as session:
        node = session.get(_ExecutionNodeRecord, prepared.manifest.node_id)
        assert node is not None

        for locality, labels, message in (
            (DataLocality.REGION_PINNED, ("region:local",), "region identity"),
            (DataLocality.SITE_PINNED, ("site:elsewhere",), "site locality"),
        ):
            request = prepared.bundle.intent.resource_request.model_copy(
                update={"data_locality": locality, "locality_labels": labels}
            )
            intent = prepared.bundle.intent.model_copy(update={"resource_request": request})
            bundle = prepared.bundle.model_copy(update={"intent": intent})
            with pytest.raises(CapacityUnavailable, match=message):
                prepared.allocator._validate_exact_placement(
                    bundle=bundle,
                    manifest=prepared.manifest,
                    inventory=prepared.inventory,
                    node=node,
                )

        frozen_protocol = prepared.bundle.compilation_request.protocol
        restricted_ports = tuple(
            item.model_copy(update={"data_classification": DataClassification.RESTRICTED})
            if item.port_id == "input.records"
            else item
            for item in frozen_protocol.data_ports
        )
        restricted_protocol = frozen_protocol.model_copy(update={"data_ports": restricted_ports})
        restricted_compilation = prepared.bundle.compilation_request.model_copy(
            update={"protocol": restricted_protocol}
        )
        restricted_bundle = prepared.bundle.model_copy(
            update={"compilation_request": restricted_compilation}
        )
        with pytest.raises(CapacityUnavailable, match="input/output classification"):
            prepared.allocator._validate_exact_placement(
                bundle=restricted_bundle,
                manifest=prepared.manifest,
                inventory=prepared.inventory,
                node=node,
            )


def test_dependent_raw_release_cannot_free_active_attempt_holds(monkeypatch) -> None:
    prepared = _prepared(monkeypatch)
    _register_and_inventory(prepared)
    claim = prepared.allocator.admit_and_reserve(bundle=prepared.bundle, grant=prepared.grant)
    released_at = claim.snapshot.reserved_at + timedelta(seconds=1)

    with pytest.raises(DBAPIError):
        with session_factory()() as session, session.begin():
            session.execute(
                text(
                    "UPDATE execution_resource_leases SET state = 'released', "
                    "released_at = :released_at WHERE attempt_id = :attempt_id"
                ),
                {"attempt_id": claim.snapshot.attempt_id, "released_at": released_at},
            )
            session.execute(
                text(
                    "UPDATE execution_nodes SET reserved_cpu_cores = 0, "
                    "reserved_memory_bytes = 0, reserved_scratch_bytes = 0, "
                    "exclusive_lease_id = NULL, state_version = state_version + 1, "
                    "updated_at = :released_at WHERE node_id = :node_id"
                ),
                {"node_id": claim.snapshot.node_id, "released_at": released_at},
            )
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    with pytest.raises(DBAPIError):
        with session_factory()() as session, session.begin():
            session.execute(
                text(
                    "UPDATE execution_budget_reservations SET state = 'released', "
                    "settled_at = :released_at WHERE attempt_id = :attempt_id"
                ),
                {"attempt_id": claim.snapshot.attempt_id, "released_at": released_at},
            )
            session.execute(
                text(
                    "UPDATE execution_budget_heads SET reserved_microunits = "
                    "reserved_microunits - :held, state_version = state_version + 1, "
                    "updated_at = :released_at WHERE authorization_sha256 = :authorization"
                ),
                {
                    "held": claim.snapshot.held_microunits,
                    "released_at": released_at,
                    "authorization": claim.snapshot.budget_authorization_sha256,
                },
            )
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    with pytest.raises(DBAPIError):
        with session_factory()() as session, session.begin():
            session.execute(
                text("DELETE FROM execution_attempts WHERE attempt_id = :attempt_id"),
                {"attempt_id": claim.snapshot.attempt_id},
            )

    with pytest.raises(DBAPIError, match="budget event ledger"):
        with session_factory()() as session, session.begin():
            reservation = session.execute(
                select(_ExecutionBudgetReservationRecord).where(
                    _ExecutionBudgetReservationRecord.attempt_id == claim.snapshot.attempt_id
                )
            ).scalar_one()
            previous = session.execute(
                select(_ExecutionBudgetEventRecord).where(
                    _ExecutionBudgetEventRecord.reservation_id == reservation.reservation_id
                )
            ).scalar_one()
            event_sha256 = _digest("raw-unbacked-budget-adoption-event")
            recorded_at = claim.snapshot.reserved_at + timedelta(seconds=1)
            payload = {
                "schema_name": "aletheia.execution_budget_event",
                "schema_version": 1,
                "reservation_id": reservation.reservation_id,
                "authorization_sha256": reservation.authorization_sha256,
                "sequence": 2,
                "previous_event_sha256": previous.event_sha256,
                "event_type": "adopted",
                "reserved_delta_microunits": 0,
                "spent_delta_microunits": 0,
                "details": {},
                "recorded_at": recorded_at.isoformat(),
            }
            session.add(
                _ExecutionBudgetEventRecord(
                    event_sha256=event_sha256,
                    reservation_id=reservation.reservation_id,
                    authorization_sha256=reservation.authorization_sha256,
                    sequence=2,
                    previous_event_sha256=previous.event_sha256,
                    event_type="adopted",
                    reserved_delta_microunits=0,
                    spent_delta_microunits=0,
                    payload_sha256=_digest("raw-unbacked-budget-adoption-payload"),
                    payload_json=payload,
                    recorded_at=recorded_at,
                )
            )
            session.flush()
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    with session_factory()() as session:
        resource = session.execute(
            select(_ExecutionResourceLeaseRecord).where(
                _ExecutionResourceLeaseRecord.attempt_id == claim.snapshot.attempt_id
            )
        ).scalar_one()
        reservation = session.execute(
            select(_ExecutionBudgetReservationRecord).where(
                _ExecutionBudgetReservationRecord.attempt_id == claim.snapshot.attempt_id
            )
        ).scalar_one()
        device_trigger_count = session.execute(
            text(
                "SELECT count(*) FROM pg_trigger "
                "WHERE tgrelid = 'execution_device_leases'::regclass "
                "AND tgname = 'trg_execution_device_attempt_bundle_complete'"
            )
        ).scalar_one()
        assert resource.state == reservation.state == "held"
        assert device_trigger_count == 1
        events = session.execute(select(_ExecutionBudgetEventRecord)).scalars().all()
        assert len(events) == 1 and events[0].event_type == "reserved"


def test_raw_genesis_with_missing_selected_cpu_is_rejected(monkeypatch) -> None:
    prepared = _prepared(monkeypatch)
    _register_and_inventory(prepared)
    intent = prepared.bundle.intent
    quote = prepared.bundle.cost_quote
    authorization = prepared.bundle.budget_authorization
    request = intent.resource_request
    attempt_id = intent.infrastructure_attempt.infrastructure_attempt_id
    execution_id = intent.execution_id
    now = prepared.observed_at
    hard_deadline = now + timedelta(seconds=quote.maximum_lease_seconds)
    lease_expires_at = now + timedelta(seconds=15)
    admission_sha256 = _digest("raw-missing-cpu-admission")
    lease_id = f"rle_{_digest('raw-missing-cpu-lease-id')}"
    lease_sha256 = _digest("raw-missing-cpu-lease")
    reservation_id = f"brv_{_digest('raw-missing-cpu-budget-reservation')}"
    bundle_json = prepared.bundle.model_dump(mode="json")
    bundle_json["cost_quote"]["selected_resource_ids"] = ["cpu.missing"]
    lease_json = {
        "schema_name": "aletheia.local_resource_lease",
        "schema_version": 1,
        "execution_id": execution_id,
        "attempt_id": attempt_id,
        "intent_sha256": intent.intent_sha256,
        "node_id": prepared.manifest.node_id,
        "node_manifest_sha256": prepared.manifest.manifest_sha256,
        "inventory_sha256": prepared.inventory.inventory_sha256,
        "selected_resource_ids": ["cpu.missing"],
        "fencing_epoch_at_acquisition": 1,
        "cpu_cores": request.cpu_cores,
        "memory_bytes": request.memory_bytes,
        "scratch_bytes": request.scratch_bytes,
        "accelerator_count": request.accelerator_count,
        "exclusive": request.exclusive,
        "acquired_at": now.isoformat(),
        "hard_deadline": hard_deadline.isoformat(),
    }
    budget_event_payload = {
        "schema_name": "aletheia.execution_budget_event",
        "schema_version": 1,
        "reservation_id": reservation_id,
        "authorization_sha256": authorization.authorization_sha256,
        "sequence": 1,
        "previous_event_sha256": None,
        "event_type": "reserved",
        "reserved_delta_microunits": quote.maximum_charge_microunits,
        "spent_delta_microunits": 0,
        "details": {},
        "recorded_at": now.isoformat(),
    }
    raw_token = "raw-missing-cpu-token-" + "x" * 48
    raw_token_sha256 = hashlib.sha256(raw_token.encode()).hexdigest()
    assignment_secret = QualificationAssignmentSecret(
        infrastructure_attempt_id=attempt_id,
        admission_sha256=admission_sha256,
        grant_sha256=prepared.grant.grant_sha256,
        bundle_sha256=prepared.bundle.bundle_sha256,
        node_id=prepared.manifest.node_id,
        node_manifest_sha256=prepared.manifest.manifest_sha256,
        resource_lease_sha256=lease_sha256,
        fencing_epoch=1,
        lease_token=raw_token,
        lease_token_sha256=raw_token_sha256,
        issued_at=now,
        expires_at=hard_deadline,
    )
    sealed_assignment = seal_qualification_assignment(
        secret=assignment_secret,
        transport_pin=prepared.transport_pin,
    )

    with pytest.raises(DBAPIError, match="admitted placement"):
        with session_factory()() as session, session.begin():
            node = session.execute(
                select(_ExecutionNodeRecord)
                .where(_ExecutionNodeRecord.node_id == prepared.manifest.node_id)
                .with_for_update()
            ).scalar_one()
            session.add(
                _ExecutionQualificationAdmissionRecord(
                    admission_sha256=admission_sha256,
                    grant_sha256=prepared.grant.grant_sha256,
                    bundle_sha256=prepared.bundle.bundle_sha256,
                    intent_sha256=intent.intent_sha256,
                    execution_id=execution_id,
                    infrastructure_attempt_id=attempt_id,
                    budget_authorization_sha256=authorization.authorization_sha256,
                    cost_quote_sha256=quote.quote_sha256,
                    authority_policy_sha256=(
                        prepared.grant.message.qualification_authority_policy_sha256
                    ),
                    authority_key_id=prepared.grant.message.authorization_key_id,
                    bundle_json=bundle_json,
                    grant_json=prepared.grant.model_dump(mode="json"),
                    verified_receipt_json={},
                    verified_at=now,
                    admitted_at=now,
                )
            )
            session.add(
                _ExecutionBudgetAuthorizationRecord(
                    authorization_sha256=authorization.authorization_sha256,
                    quest_id=authorization.quest_id,
                    protocol_sha256=authorization.protocol_sha256,
                    work_order_sha256=authorization.work_order_sha256,
                    resource_budget_sha256=authorization.resource_budget_sha256,
                    source_budget_authorization_sha256=(
                        authorization.source_budget_authorization_sha256
                    ),
                    currency_code=authorization.currency_code,
                    cap_microunits=authorization.maximum_cost_microunits,
                    authorized_at=authorization.authorized_at,
                    expires_at=authorization.expires_at,
                    authorized_by_principal_id=(authorization.authorized_by_principal_id),
                    payload_sha256=authorization.authorization_sha256,
                    payload_json=authorization.model_dump(mode="json"),
                    registered_at=now,
                )
            )
            session.add(
                _ExecutionBudgetHeadRecord(
                    authorization_sha256=authorization.authorization_sha256,
                    currency_code=authorization.currency_code,
                    cap_microunits=authorization.maximum_cost_microunits,
                    reserved_microunits=quote.maximum_charge_microunits,
                    spent_microunits=0,
                    state_version=1,
                    updated_at=now,
                )
            )
            session.add(
                _ExecutionHeadRecord(
                    execution_id=execution_id,
                    quest_id=intent.quest_id,
                    protocol_sha256=intent.protocol_sha256,
                    work_order_id=intent.work_order_id,
                    work_order_sha256=intent.work_order_sha256,
                    replicate_slot_id=intent.replicate_slot.replicate_slot_id,
                    replicate_slot_sha256=intent.replicate_slot.replicate_slot_sha256,
                    last_attempt_number=1,
                    active_attempt_id=attempt_id,
                    state_version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.flush()
            session.add(
                _ExecutionAttemptRecord(
                    attempt_id=attempt_id,
                    execution_id=execution_id,
                    attempt_number=1,
                    intent_sha256=intent.intent_sha256,
                    intent_json=intent.model_dump(mode="json"),
                    admission_sha256=admission_sha256,
                    grant_sha256=prepared.grant.grant_sha256,
                    bundle_sha256=prepared.bundle.bundle_sha256,
                    cost_quote_sha256=quote.quote_sha256,
                    node_id=node.node_id,
                    node_inventory_sha256=prepared.inventory.inventory_sha256,
                    status="reserved",
                    state_version=1,
                    fencing_epoch=1,
                    lease_token_sha256=raw_token_sha256,
                    adoption_count=0,
                    latest_adoption_sha256=None,
                    last_runtime_inspection_sequence=0,
                    last_runtime_inspection_sha256=None,
                    last_runtime_inspected_at=None,
                    last_runtime_inspected_monotonic_ns=None,
                    authorized_at=prepared.grant.message.authorized_at,
                    reserved_at=now,
                    heartbeat_at=now,
                    lease_expires_at=lease_expires_at,
                    hard_deadline=hard_deadline,
                    reconciliation_reason=None,
                    runtime_identity_sha256=None,
                    runtime_identity_json=null(),
                    terminal_receipt_sha256=None,
                    updated_at=now,
                )
            )
            session.flush()
            session.add(
                _ExecutionResourceLeaseRecord(
                    lease_id=lease_id,
                    attempt_id=attempt_id,
                    node_id=node.node_id,
                    inventory_sha256=prepared.inventory.inventory_sha256,
                    lease_sha256=lease_sha256,
                    lease_json=lease_json,
                    state="held",
                    fencing_epoch=1,
                    cpu_cores=request.cpu_cores,
                    memory_bytes=request.memory_bytes,
                    scratch_bytes=request.scratch_bytes,
                    exclusive=request.exclusive,
                    accelerator_count=0,
                    acquired_at=now,
                    heartbeat_at=now,
                    lease_expires_at=lease_expires_at,
                    released_at=None,
                )
            )
            session.flush()
            session.add(
                _ExecutionAssignmentEnvelopeRecord(
                    assignment_envelope_sha256=sealed_assignment.envelope_sha256,
                    assignment_secret_sha256=(sealed_assignment.assignment_secret_sha256),
                    attempt_id=attempt_id,
                    admission_sha256=admission_sha256,
                    grant_sha256=prepared.grant.grant_sha256,
                    bundle_sha256=prepared.bundle.bundle_sha256,
                    node_id=prepared.manifest.node_id,
                    node_manifest_sha256=prepared.manifest.manifest_sha256,
                    resource_lease_sha256=lease_sha256,
                    initial_fencing_epoch=1,
                    lease_token_sha256=raw_token_sha256,
                    transport_pin_sha256=prepared.transport_pin.pin_sha256,
                    transport_key_id=prepared.transport_pin.transport_key_id,
                    transport_pin_json=prepared.transport_pin.model_dump(mode="json"),
                    payload_sha256=sealed_assignment.envelope_sha256,
                    payload_json=sealed_assignment.model_dump(mode="json"),
                    issued_at=now,
                    expires_at=hard_deadline,
                    created_at=now,
                )
            )
            session.add(
                _ExecutionBudgetReservationRecord(
                    reservation_id=reservation_id,
                    authorization_sha256=authorization.authorization_sha256,
                    attempt_id=attempt_id,
                    execution_id=execution_id,
                    cost_quote_sha256=quote.quote_sha256,
                    currency_code=quote.currency_code,
                    fixed_charge_microunits=quote.fixed_charge_microunits,
                    charge_per_second_microunits=(quote.charge_per_second_microunits),
                    maximum_lease_seconds=quote.maximum_lease_seconds,
                    actual_lease_seconds=None,
                    held_microunits=quote.maximum_charge_microunits,
                    settled_microunits=0,
                    state="held",
                    reserved_at=now,
                    settled_at=None,
                )
            )
            session.flush()
            session.add(
                _ExecutionBudgetEventRecord(
                    event_sha256=_digest("raw-missing-cpu-budget-event"),
                    reservation_id=reservation_id,
                    authorization_sha256=authorization.authorization_sha256,
                    sequence=1,
                    previous_event_sha256=None,
                    event_type="reserved",
                    reserved_delta_microunits=quote.maximum_charge_microunits,
                    spent_delta_microunits=0,
                    payload_sha256=_digest("raw-missing-cpu-budget-event-payload"),
                    payload_json=budget_event_payload,
                    recorded_at=now,
                )
            )
            node.reserved_cpu_cores = request.cpu_cores
            node.reserved_memory_bytes = request.memory_bytes
            node.reserved_scratch_bytes = request.scratch_bytes
            node.exclusive_lease_id = lease_id if request.exclusive else None
            node.state_version += 1
            node.updated_at = now
            session.flush()
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    with session_factory()() as session:
        assert session.get(_ExecutionAttemptRecord, attempt_id) is None


def test_expiry_is_sticky_and_raw_head_tampering_rolls_back(monkeypatch) -> None:
    prepared = _prepared(monkeypatch)
    _register_and_inventory(prepared)
    claim = prepared.allocator.admit_and_reserve(bundle=prepared.bundle, grant=prepared.grant)
    monkeypatch.setattr(
        allocator_module,
        "_database_time",
        lambda _session: claim.snapshot.lease_expires_at + timedelta(seconds=1),
    )
    reconciled = prepared.allocator.reconcile_expired()
    assert reconciled and reconciled[0].status == "reconciliation_required"
    with session_factory()() as session:
        node = session.get(_ExecutionNodeRecord, claim.snapshot.node_id)
        reservation = session.execute(
            select(_ExecutionBudgetReservationRecord).where(
                _ExecutionBudgetReservationRecord.attempt_id == claim.snapshot.attempt_id
            )
        ).scalar_one()
        resource = session.execute(
            select(_ExecutionResourceLeaseRecord).where(
                _ExecutionResourceLeaseRecord.attempt_id == claim.snapshot.attempt_id
            )
        ).scalar_one()
        assert node is not None and node.reserved_cpu_cores == claim.snapshot.cpu_cores
        assert reservation.state == resource.state == "reconciliation_required"

    with pytest.raises(DBAPIError):
        with session_factory()() as session, session.begin():
            session.execute(
                text(
                    "UPDATE execution_nodes SET reserved_cpu_cores = reserved_cpu_cores + 999, "
                    "state_version = state_version + 1, updated_at = updated_at "
                    "WHERE node_id = :node_id"
                ),
                {"node_id": claim.snapshot.node_id},
            )
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    with session_factory()() as session:
        node = session.get(_ExecutionNodeRecord, claim.snapshot.node_id)
        assert node is not None and node.reserved_cpu_cores == claim.snapshot.cpu_cores


@pytest.mark.parametrize("retry_device_id", ("gpu.0", "gpu.1"))
def test_typed_adoption_terminal_custody_and_actual_budget_settlement(
    monkeypatch, retry_device_id: str
) -> None:
    prepared = _prepared(
        monkeypatch,
        accelerator_count=1,
        inventory_accelerator_count=2,
        retryable=True,
    )
    _register_and_inventory(prepared)
    claim = prepared.allocator.admit_and_reserve(bundle=prepared.bundle, grant=prepared.grant)
    assert claim.lease_token is not None
    intent = prepared.bundle.intent
    request = intent.resource_request
    acquired_cpu = next(
        item for item in prepared.inventory.resources if item.kind is ResourceKind.CPU
    )
    acquired_gpus = tuple(
        item for item in prepared.inventory.resources if item.kind is ResourceKind.ACCELERATOR
    )
    refreshed_cpu = NodeInventoryResource.model_validate(
        {
            **acquired_cpu.model_dump(mode="python"),
            "cpu_cores_managed_occupied": request.cpu_cores,
            "cpu_cores_allocatable": acquired_cpu.cpu_cores_allocatable - request.cpu_cores,
            "memory_bytes_managed_occupied": request.memory_bytes,
            "memory_bytes_allocatable": (
                acquired_cpu.memory_bytes_allocatable - request.memory_bytes
            ),
            "scratch_bytes_managed_occupied": request.scratch_bytes,
            "scratch_bytes_allocatable": (
                acquired_cpu.scratch_bytes_allocatable - request.scratch_bytes
            ),
        }
    )
    managed_accelerator_bytes = request.minimum_accelerator_memory_bytes or 0
    refreshed_gpus = tuple(
        NodeInventoryResource.model_validate(
            {
                **item.model_dump(mode="python"),
                "accelerator_memory_bytes_managed_occupied": (
                    managed_accelerator_bytes if item.resource_id == "gpu.0" else 0
                ),
                "accelerator_memory_bytes_allocatable": (
                    (item.accelerator_memory_bytes_allocatable or 0)
                    - (managed_accelerator_bytes if item.resource_id == "gpu.0" else 0)
                ),
            }
        )
        for item in acquired_gpus
    )
    refreshed_at = claim.snapshot.reserved_at + timedelta(milliseconds=500)
    refreshed_inventory = issue_node_inventory_attestation(
        manifest=prepared.manifest,
        boot_id=prepared.inventory.boot_id,
        sequence=2,
        observed_monotonic_ns=1_050,
        resources=(refreshed_cpu, *refreshed_gpus),
        collector_implementation_sha256=_digest("allocator-test-collector"),
        collector_output_sha256=_digest("allocator-test-gpu-refresh"),
        observed_at=refreshed_at,
        expires_at=refreshed_at + timedelta(seconds=30),
        private_key=PRIVATE_KEY,
    )
    monkeypatch.setattr(allocator_module, "_database_time", lambda _session: refreshed_at)
    refreshed = prepared.allocator.append_inventory(refreshed_inventory)
    assert refreshed.created is True
    assert claim.snapshot.node_inventory_sha256 == prepared.inventory.inventory_sha256

    started_at = claim.snapshot.reserved_at + timedelta(seconds=1)
    runtime = _runtime_identity(
        manifest=prepared.manifest,
        intent=intent,
    ).model_copy(update={"started_at": started_at, "started_monotonic_ns": 1_100})

    too_early_runtime = runtime.model_copy(
        update={"started_at": claim.snapshot.reserved_at - timedelta(seconds=1)}
    )
    monkeypatch.setattr(allocator_module, "_database_time", lambda _session: started_at)
    with pytest.raises(LeaseAuthorityError, match="runtime start"):
        prepared.allocator.start_attempt(
            attempt_id=claim.snapshot.attempt_id,
            lease_token=claim.lease_token,
            fencing_epoch=claim.snapshot.fencing_epoch,
            runtime_identity=too_early_runtime,
        )
    prepared.allocator.start_attempt(
        attempt_id=claim.snapshot.attempt_id,
        lease_token=claim.lease_token,
        fencing_epoch=claim.snapshot.fencing_epoch,
        runtime_identity=runtime,
    )
    monkeypatch.setattr(
        allocator_module,
        "_database_time",
        lambda _session: started_at + timedelta(seconds=1),
    )
    prepared.allocator.mark_running(
        attempt_id=claim.snapshot.attempt_id,
        lease_token=claim.lease_token,
        fencing_epoch=claim.snapshot.fencing_epoch,
    )
    heartbeat_at = started_at + timedelta(milliseconds=1500)
    monkeypatch.setattr(allocator_module, "_database_time", lambda _session: heartbeat_at)
    heartbeat = prepared.allocator.heartbeat(
        attempt_id=claim.snapshot.attempt_id,
        lease_token=claim.lease_token,
        fencing_epoch=claim.snapshot.fencing_epoch,
    )
    assert heartbeat.snapshot.node_inventory_sha256 == prepared.inventory.inventory_sha256

    inspection_at = started_at + timedelta(seconds=2)
    running_inspection = issue_runtime_inspection_receipt(
        manifest=prepared.manifest,
        runtime_identity=runtime,
        fencing_epoch=claim.snapshot.fencing_epoch,
        lease_token_sha256=hashlib.sha256(claim.lease_token.encode()).hexdigest(),
        inspection_sequence=1,
        state=RuntimeInspectionState.RUNNING,
        inspection_evidence_sha256=_digest("allocator-running-inspection"),
        inspected_at=inspection_at,
        inspected_monotonic_ns=1_500,
        expires_at=inspection_at + timedelta(seconds=20),
        private_key=PRIVATE_KEY,
    )
    new_token = secrets.token_urlsafe(32)
    adoption = issue_attempt_adoption_receipt(
        manifest=prepared.manifest,
        runtime_inspection_receipt=running_inspection,
        adoption_sequence=1,
        new_fencing_epoch=claim.snapshot.fencing_epoch + 1,
        new_lease_token_sha256=hashlib.sha256(new_token.encode()).hexdigest(),
        reason=AttemptAdoptionReason.CONTROL_PLANE_FAILOVER,
        singleton_lock_evidence_sha256=_digest("allocator-singleton-lock"),
        singleton_lock_acquired_monotonic_ns=1_600,
        allocator_principal_id="principal:allocator",
        adopted_at=inspection_at + timedelta(seconds=1),
        private_key=PRIVATE_KEY,
    )
    adoption_now = inspection_at + timedelta(seconds=2)
    monkeypatch.setattr(allocator_module, "_database_time", lambda _session: adoption_now)
    adopted = prepared.allocator.adopt_attempt(
        receipt=adoption,
        new_lease_token=new_token,
    )
    assert adopted.snapshot.fencing_epoch == claim.snapshot.fencing_epoch + 1

    regressive_inspection = issue_runtime_inspection_receipt(
        manifest=prepared.manifest,
        runtime_identity=runtime,
        fencing_epoch=adopted.snapshot.fencing_epoch,
        lease_token_sha256=hashlib.sha256(new_token.encode()).hexdigest(),
        inspection_sequence=2,
        state=RuntimeInspectionState.RUNNING,
        inspection_evidence_sha256=_digest("allocator-regressive-inspection"),
        inspected_at=inspection_at - timedelta(seconds=1),
        inspected_monotonic_ns=1_400,
        expires_at=inspection_at + timedelta(seconds=19),
        private_key=PRIVATE_KEY,
    )
    rejected_token = secrets.token_urlsafe(32)
    regressive_adoption = issue_attempt_adoption_receipt(
        manifest=prepared.manifest,
        runtime_inspection_receipt=regressive_inspection,
        adoption_sequence=2,
        new_fencing_epoch=adopted.snapshot.fencing_epoch + 1,
        new_lease_token_sha256=hashlib.sha256(rejected_token.encode()).hexdigest(),
        reason=AttemptAdoptionReason.ALLOCATOR_PROCESS_RECOVERY,
        singleton_lock_evidence_sha256=_digest("allocator-regressive-singleton"),
        singleton_lock_acquired_monotonic_ns=1_700,
        allocator_principal_id="principal:allocator",
        adopted_at=adoption_now,
        private_key=PRIVATE_KEY,
    )
    with pytest.raises(LeaseAuthorityError, match="runtime inspection"):
        prepared.allocator.adopt_attempt(
            receipt=regressive_adoption,
            new_lease_token=rejected_token,
        )

    ended_at = started_at + timedelta(seconds=8)
    termination_at = ended_at + timedelta(seconds=1)
    termination = issue_runtime_inspection_receipt(
        manifest=prepared.manifest,
        runtime_identity=runtime,
        fencing_epoch=adopted.snapshot.fencing_epoch,
        lease_token_sha256=hashlib.sha256(new_token.encode()).hexdigest(),
        inspection_sequence=2,
        state=RuntimeInspectionState.TERMINATED,
        inspection_evidence_sha256=_digest("allocator-terminated-inspection"),
        inspected_at=termination_at,
        inspected_monotonic_ns=2_100,
        expires_at=termination_at + timedelta(seconds=20),
        private_key=PRIVATE_KEY,
    )
    output_manifest = _output_manifest(intent=intent, produced_at=ended_at)
    artifact_receipts = tuple(
        ArtifactVerifiedReceipt(
            artifact_manifest_sha256=output_manifest.manifest_sha256,
            producer_attempt_id=claim.snapshot.attempt_id,
            artifact=entry,
            custody_mode=ArtifactCustodyMode.CENTRAL_REHASH,
            verifier_principal_id="principal:artifact-verifier",
            object_store_id="research-cas",
            final_object_ref=f"cas://sha256/{entry.content_sha256}",
            final_object_version="generation-1",
            verified_at=ended_at,
        )
        for entry in output_manifest.entries
    )
    prepared.artifacts.add_manifest(output_manifest)
    for artifact_receipt in artifact_receipts:
        prepared.artifacts.add_resolution(
            VerifiedInputArtifactResolution(
                verified_receipt_sha256=(artifact_receipt.verified_receipt_sha256),
                verified_receipt=artifact_receipt,
                artifact_manifest=output_manifest,
                content_rehash_sha256=artifact_receipt.artifact.content_sha256,
                content_bytes=artifact_receipt.artifact.bytes,
                resolved_by_principal_id="principal:archive-resolver",
                resolved_at=termination_at,
            )
        )
    signed_at = termination_at + timedelta(seconds=1)
    node_receipt = issue_node_execution_receipt(
        manifest=prepared.manifest,
        intent=intent,
        node_inventory_sha256=claim.snapshot.node_inventory_sha256,
        resource_lease_sha256=claim.snapshot.resource_lease_sha256,
        runtime_identity=runtime,
        fencing_epoch=adopted.snapshot.fencing_epoch,
        lease_token_sha256=hashlib.sha256(new_token.encode()).hexdigest(),
        ended_at=ended_at,
        ended_monotonic_ns=2_000,
        exit_code=1,
        artifact_manifest=output_manifest,
        termination_inspection_receipt=termination,
        signed_at=signed_at,
        private_key=PRIVATE_KEY,
    )
    receipt_fields = dict(
        intent=intent,
        worker_node_manifest_sha256=prepared.manifest.manifest_sha256,
        node_inventory_sha256=claim.snapshot.node_inventory_sha256,
        resource_lease_sha256=claim.snapshot.resource_lease_sha256,
        node_execution_receipt_sha256=node_receipt.node_execution_receipt_sha256,
        started_at=started_at,
        ended_at=ended_at,
        observed_at=signed_at,
        artifact_manifest=output_manifest,
        artifact_verified_receipts=artifact_receipts,
        verified_by_principal_id="principal:execution-verifier",
        verified_at=signed_at,
    )
    forged_success = ExecutionReceipt(
        **receipt_fields,
        terminal_state=ExecutionTerminalState.ENGINEERING_SUCCEEDED,
    )
    terminal_now = signed_at + timedelta(seconds=1)
    monkeypatch.setattr(allocator_module, "_database_time", lambda _session: terminal_now)
    forged_success_attestation = issue_terminal_verification_attestation(
        execution_receipt=forged_success,
        node_execution_receipt=node_receipt,
        pin=prepared.terminal_pin,
        private_key=TERMINAL_PRIVATE_KEY,
        expires_at=terminal_now + timedelta(seconds=20),
    )
    with pytest.raises(LeaseAuthorityError, match="signed exit"):
        prepared.allocator.commit_terminal_receipt(
            receipt=forged_success,
            node_execution_receipt=node_receipt,
            terminal_verification_attestation=forged_success_attestation,
            lease_token=new_token,
            fencing_epoch=adopted.snapshot.fencing_epoch,
        )

    clean_exit_receipt = issue_node_execution_receipt(
        manifest=prepared.manifest,
        intent=intent,
        node_inventory_sha256=claim.snapshot.node_inventory_sha256,
        resource_lease_sha256=claim.snapshot.resource_lease_sha256,
        runtime_identity=runtime,
        fencing_epoch=adopted.snapshot.fencing_epoch,
        lease_token_sha256=hashlib.sha256(new_token.encode()).hexdigest(),
        ended_at=ended_at,
        ended_monotonic_ns=2_000,
        exit_code=0,
        artifact_manifest=output_manifest,
        termination_inspection_receipt=termination,
        signed_at=signed_at,
        private_key=PRIVATE_KEY,
    )
    arbitrary_failure = ExecutionFailure(
        category=ExecutionFailureCategory.INFRASTRUCTURE,
        detail_sha256=_digest("allocator-forged-infrastructure-failure"),
    )
    forged_failure = ExecutionReceipt(
        **{
            **receipt_fields,
            "node_execution_receipt_sha256": (clean_exit_receipt.node_execution_receipt_sha256),
        },
        terminal_state=ExecutionTerminalState.EXECUTION_FAILED,
        failure=arbitrary_failure,
    )
    forged_failure_attestation = issue_terminal_verification_attestation(
        execution_receipt=forged_failure,
        node_execution_receipt=clean_exit_receipt,
        pin=prepared.terminal_pin,
        private_key=TERMINAL_PRIVATE_KEY,
        expires_at=terminal_now + timedelta(seconds=20),
    )
    with pytest.raises(LeaseAuthorityError, match="output closure"):
        prepared.allocator.commit_terminal_receipt(
            receipt=forged_failure,
            node_execution_receipt=clean_exit_receipt,
            terminal_verification_attestation=forged_failure_attestation,
            lease_token=new_token,
            fencing_epoch=adopted.snapshot.fencing_epoch,
        )

    empty_manifest = ArtifactManifest(
        intent_sha256=intent.intent_sha256,
        execution_id=intent.execution_id,
        replicate_slot_id=intent.replicate_slot.replicate_slot_id,
        infrastructure_attempt_id=claim.snapshot.attempt_id,
        entries=(),
        produced_at=ended_at,
    )
    prepared.artifacts.add_manifest(empty_manifest)
    invalid_output_node_receipt = issue_node_execution_receipt(
        manifest=prepared.manifest,
        intent=intent,
        node_inventory_sha256=claim.snapshot.node_inventory_sha256,
        resource_lease_sha256=claim.snapshot.resource_lease_sha256,
        runtime_identity=runtime,
        fencing_epoch=adopted.snapshot.fencing_epoch,
        lease_token_sha256=hashlib.sha256(new_token.encode()).hexdigest(),
        ended_at=ended_at,
        ended_monotonic_ns=2_000,
        exit_code=1,
        artifact_manifest=empty_manifest,
        termination_inspection_receipt=termination,
        signed_at=signed_at,
        private_key=PRIVATE_KEY,
    )
    retry_rule = intent.retry_policy.retry_rules[0]
    failure = ExecutionFailure(
        category=ExecutionFailureCategory.INFRASTRUCTURE,
        detail_sha256=_digest("allocator-confirmed-runtime-failure"),
        capability_failure_id=retry_rule.capability_failure_id,
        capability_failure_detection_rule_sha256=retry_rule.detection_rule_sha256,
        retryable_after_confirmed_termination=True,
    )
    failed_receipt = ExecutionReceipt(
        **{
            **receipt_fields,
            "node_execution_receipt_sha256": (
                invalid_output_node_receipt.node_execution_receipt_sha256
            ),
            "artifact_manifest": empty_manifest,
            "artifact_verified_receipts": (),
        },
        terminal_state=ExecutionTerminalState.EXECUTION_FAILED,
        failure=failure,
    )
    terminal_attestation = issue_terminal_verification_attestation(
        execution_receipt=failed_receipt,
        node_execution_receipt=invalid_output_node_receipt,
        pin=prepared.terminal_pin,
        private_key=TERMINAL_PRIVATE_KEY,
        expires_at=terminal_now + timedelta(seconds=20),
    )
    with pytest.raises(LeaseAuthorityError, match="signed verification authority"):
        prepared.allocator.commit_terminal_receipt(
            receipt=failed_receipt,
            node_execution_receipt=invalid_output_node_receipt,
            terminal_verification_attestation=forged_success_attestation,
            lease_token=new_token,
            fencing_epoch=adopted.snapshot.fencing_epoch,
        )
    committed = prepared.allocator.commit_terminal_receipt(
        receipt=failed_receipt,
        node_execution_receipt=invalid_output_node_receipt,
        terminal_verification_attestation=terminal_attestation,
        lease_token=new_token,
        fencing_epoch=adopted.snapshot.fencing_epoch,
    )
    expected_seconds = int((terminal_now - claim.snapshot.reserved_at).total_seconds())
    quote = prepared.bundle.cost_quote
    assert committed.snapshot.status == "failed"
    assert committed.charged_microunits == (
        quote.fixed_charge_microunits + quote.charge_per_second_microunits * expected_seconds
    )
    archive = PostgreSQLExecutionReceiptArchive(
        terminal_verification_authority=(prepared.allocator._terminal_verification_authority)
    )
    archived = archive.list_terminal_receipts_for_attempt(
        infrastructure_attempt_id=claim.snapshot.attempt_id
    )
    assert len(archived) == 1
    assert archived[0].receipt == failed_receipt
    assert (
        archived[0].node_execution_receipt_sha256
        == invalid_output_node_receipt.node_execution_receipt_sha256
    )
    assert (
        archived[0].terminal_verification_attestation_sha256
        == terminal_attestation.attestation_sha256
    )
    assert archived[0].terminal_verification_authority_pin_sha256 == canonical_sha256(
        prepared.terminal_pin
    )
    assert archived[0].terminal_verification_policy_sha256 == prepared.terminal_pin.policy_sha256
    assert archived[0].terminal_verification_key_id == prepared.terminal_pin.key_id
    assert archived[0].committed_by_principal_id == prepared.terminal_pin.principal_id
    assert archived[0].artifact_manifest_sha256 == empty_manifest.manifest_sha256
    resolved = archive.resolve_execution_receipt(
        execution_receipt_sha256=failed_receipt.execution_receipt_sha256,
        observed_at=terminal_now,
    )
    assert resolved is not None
    assert resolved.execution_receipt == failed_receipt
    assert resolved.committed_at == terminal_now

    foreign_public_key = _public_key_hex(PRIVATE_KEY)
    foreign_pin = TerminalVerificationAuthorityPin(
        policy_sha256=_digest("foreign-terminal-policy"),
        principal_id="principal:foreign-terminal-verifier",
        key_id=qualification_key_id(foreign_public_key),
        public_key_ed25519_hex=foreign_public_key,
        valid_from=prepared.observed_at - timedelta(days=1),
        expires_at=prepared.observed_at + timedelta(days=1),
    )
    foreign_archive = PostgreSQLExecutionReceiptArchive(
        terminal_verification_authority=TerminalVerificationAuthorityVerifier(foreign_pin)
    )
    with pytest.raises(AdmissionConflict, match="archived execution receipt bytes"):
        foreign_archive.list_terminal_receipts_for_attempt(
            infrastructure_attempt_id=claim.snapshot.attempt_id
        )

    with pytest.raises(DBAPIError, match="budget event ledger"):
        with session_factory()() as session, session.begin():
            reservation = session.execute(
                select(_ExecutionBudgetReservationRecord).where(
                    _ExecutionBudgetReservationRecord.attempt_id == claim.snapshot.attempt_id
                )
            ).scalar_one()
            previous = session.execute(
                select(_ExecutionBudgetEventRecord)
                .where(_ExecutionBudgetEventRecord.reservation_id == reservation.reservation_id)
                .order_by(_ExecutionBudgetEventRecord.sequence.desc())
                .limit(1)
            ).scalar_one()
            sequence = previous.sequence + 1
            recorded_at = terminal_now + timedelta(seconds=1)
            payload = {
                "schema_name": "aletheia.execution_budget_event",
                "schema_version": 1,
                "reservation_id": reservation.reservation_id,
                "authorization_sha256": reservation.authorization_sha256,
                "sequence": sequence,
                "previous_event_sha256": previous.event_sha256,
                "event_type": "adopted",
                "reserved_delta_microunits": 0,
                "spent_delta_microunits": 0,
                "details": {},
                "recorded_at": recorded_at.isoformat(),
            }
            session.add(
                _ExecutionBudgetEventRecord(
                    event_sha256=_digest("raw-post-terminal-budget-event"),
                    reservation_id=reservation.reservation_id,
                    authorization_sha256=reservation.authorization_sha256,
                    sequence=sequence,
                    previous_event_sha256=previous.event_sha256,
                    event_type="adopted",
                    reserved_delta_microunits=0,
                    spent_delta_microunits=0,
                    payload_sha256=_digest("raw-post-terminal-budget-payload"),
                    payload_json=payload,
                    recorded_at=recorded_at,
                )
            )
            session.flush()
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    retry_observed_at = terminal_now + timedelta(seconds=5)
    retry_inventory = issue_node_inventory_attestation(
        manifest=prepared.manifest,
        boot_id=prepared.inventory.boot_id,
        sequence=3,
        observed_monotonic_ns=3_000,
        resources=prepared.inventory.resources,
        collector_implementation_sha256=_digest("allocator-test-collector"),
        collector_output_sha256=_digest(f"allocator-retry-inventory:{retry_device_id}"),
        observed_at=retry_observed_at,
        expires_at=retry_observed_at + timedelta(seconds=30),
        private_key=PRIVATE_KEY,
    )
    monkeypatch.setattr(
        allocator_module,
        "_database_time",
        lambda _session: retry_observed_at,
    )
    prepared.allocator.append_inventory(retry_inventory)

    next_attempt = type(intent.infrastructure_attempt)(
        replicate_slot_id=intent.replicate_slot.replicate_slot_id,
        attempt_number=2,
        previous_attempt_id=intent.infrastructure_attempt.infrastructure_attempt_id,
        prior_confirmed_failure_receipt_sha256=(failed_receipt.execution_receipt_sha256),
        prior_failure_category=failure.category,
    )
    retry_intent = type(intent).model_validate(
        {
            **intent.model_dump(mode="python"),
            "infrastructure_attempt": next_attempt,
        }
    )
    retry_case = _signed_case(
        request=prepared.case.request,
        result=prepared.case.result,
        intent=retry_intent,
        resolution=prepared.case.resolution,
        observed_at=retry_observed_at,
        prior_execution_receipt=failed_receipt,
        quote_at=terminal_now + timedelta(seconds=1),
        grant_at=terminal_now + timedelta(seconds=2),
        grant_expires_at=terminal_now + timedelta(minutes=10),
    )
    retry_quote = ExecutionCostQuote.model_validate(
        {
            **retry_case.bundle.cost_quote.model_dump(mode="python"),
            "permitted_node_manifest_sha256s": (prepared.manifest.manifest_sha256,),
            "selected_node_manifest_sha256": prepared.manifest.manifest_sha256,
            "selected_resource_ids": ("cpu.socket-0", retry_device_id),
        }
    )
    retry_bundle = EngineeringQualificationBundle.model_validate(
        {
            **retry_case.bundle.model_dump(mode="python"),
            "cost_quote": retry_quote,
        }
    )
    retry_resolver = _AuthorityResolver(
        retry_bundle,
        prior_committed_at=terminal_now,
    )
    retry_grant = issue_engineering_qualification_grant(
        retry_bundle,
        pin=prepared.allocator._authority.pin,
        artifact_resolver=prepared.artifacts,
        authority_resolver=retry_resolver,
        private_key=PRIVATE_KEY,
        authorized_at=terminal_now + timedelta(seconds=2),
        expires_at=terminal_now + timedelta(minutes=10),
    )
    retry_allocator = PostgreSQLExecutionAllocator(
        authority=prepared.allocator._authority,
        artifact_resolver=prepared.artifacts,
        execution_authority_resolver=retry_resolver,
        pricing_authority=LocalPricingAuthorityPin(
            quote_principal_ids=frozenset({retry_quote.quoted_by_principal_id}),
            rate_card_sha256s=frozenset({retry_quote.rate_card_sha256}),
            pricing_policy_sha256s=frozenset({retry_quote.pricing_policy_sha256}),
            currency_codes=frozenset({retry_quote.currency_code}),
        ),
        node_authorities=tuple(prepared.allocator._node_authorities.values()),
        node_assignment_transport_pins=tuple(
            prepared.allocator._node_assignment_transport_pins.values()
        ),
        terminal_verification_authority=(prepared.allocator._terminal_verification_authority),
        allocator_principal_id="principal:allocator",
        max_inventory_ttl_seconds=30,
        heartbeat_extension_seconds=15,
    )
    retry_claim = retry_allocator.admit_and_reserve(
        bundle=retry_bundle,
        grant=retry_grant,
    )
    assert retry_claim.snapshot.attempt_number == 2
    assert retry_claim.snapshot.fencing_epoch == adopted.snapshot.fencing_epoch + 1
    assert tuple(item.device_id for item in retry_claim.snapshot.device_leases) == (
        retry_device_id,
    )


def test_clock_rollback_and_budget_cap_mutation_fail_closed(monkeypatch) -> None:
    prepared = _prepared(monkeypatch)
    _register_and_inventory(prepared)
    claim = prepared.allocator.admit_and_reserve(bundle=prepared.bundle, grant=prepared.grant)
    with pytest.raises(DBAPIError):
        with session_factory()() as session, session.begin():
            session.execute(
                text(
                    "UPDATE execution_nodes SET state_version = state_version + 1, "
                    "updated_at = updated_at - interval '1 second' WHERE node_id = :node_id"
                ),
                {"node_id": claim.snapshot.node_id},
            )
    with pytest.raises(DBAPIError):
        with session_factory()() as session, session.begin():
            session.execute(
                text(
                    "UPDATE execution_budget_heads SET cap_microunits = cap_microunits + 1, "
                    "state_version = state_version + 1, updated_at = updated_at "
                    "WHERE authorization_sha256 = :authorization_sha256"
                ),
                {"authorization_sha256": claim.snapshot.budget_authorization_sha256},
            )
