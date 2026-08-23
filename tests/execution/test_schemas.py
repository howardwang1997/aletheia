from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from aletheia.execution.schemas import (
    ArtifactCustodyMode,
    ArtifactManifest,
    ArtifactManifestEntry,
    ArtifactRole,
    ArtifactVerifiedReceipt,
    CapabilityFailureCategory,
    DataLocality,
    ExecutionEffectClass,
    ExecutionFailure,
    ExecutionFailureCategory,
    ExecutionIntent,
    ExecutionReceipt,
    ExecutionResourceRequest,
    ExecutionRetryBindingError,
    ExecutionRetryDisposition,
    ExecutionRetryMode,
    ExecutionRetryPolicy,
    ExecutionRetryRule,
    ExecutionTerminalState,
    ExpectedArtifact,
    ExternalRequestIdentity,
    InfrastructureAttempt,
    InputArtifactBinding,
    NetworkPolicy,
    RawScientificOutcome,
    ResourceKind,
    ScientificReplicateKind,
    ScientificReplicateSlot,
    StaticResourceCatalog,
    StaticResourceClass,
    canonical_json_bytes,
    verify_execution_retry_binding,
)

H0 = "0" * 64
H1 = "1" * 64
H2 = "2" * 64
H3 = "3" * 64
H4 = "4" * 64
H5 = "5" * 64
H6 = "6" * 64
H7 = "7" * 64
QUEST_ID = "qst_" + "a" * 32
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def _resource_class() -> StaticResourceClass:
    return StaticResourceClass(
        class_key="gpu-v100-32gb",
        kind=ResourceKind.ACCELERATOR,
        cpu_architecture="x86_64",
        oci_platform="linux/amd64",
        container_runtime="oci-v1",
        cpu_cores=8,
        memory_bytes=32 * 1024**3,
        scratch_bytes=100 * 1024**3,
        accelerator_model="Tesla V100-SXM2",
        accelerator_count=1,
        accelerator_memory_bytes=32 * 1024**3,
        accelerator_compute_capability="7.0",
        features=("fp64",),
        network_policies=(NetworkPolicy.NONE,),
        supports_checkpointing=True,
    )


def _resource_request(resource_class_id: str, *, attempts: int = 2) -> ExecutionResourceRequest:
    return ExecutionResourceRequest(
        accepted_resource_class_ids=(resource_class_id,),
        cpu_cores=4,
        memory_bytes=8 * 1024**3,
        scratch_bytes=10 * 1024**3,
        wall_time_seconds=3600,
        accelerator_count=1,
        allowed_accelerator_models=("Tesla V100-SXM2",),
        minimum_accelerator_memory_bytes=16 * 1024**3,
        minimum_compute_capability="7.0",
        required_features=("fp64",),
        artifact_quota_bytes=1024**3,
        max_infrastructure_attempts=attempts,
    )


def _retry_policy(*, attempts: int) -> ExecutionRetryPolicy:
    if attempts == 1:
        return ExecutionRetryPolicy(
            mode=ExecutionRetryMode.NEVER,
            maximum_attempts_per_scientific_slot=1,
        )
    return ExecutionRetryPolicy(
        mode=ExecutionRetryMode.IDEMPOTENT_NEW_ATTEMPT,
        maximum_attempts_per_scientific_slot=attempts,
        retry_rules=(
            ExecutionRetryRule(
                capability_failure_id="failure.infrastructure",
                capability_failure_category=CapabilityFailureCategory.INFRASTRUCTURE,
                detection_rule_sha256=H6,
                disposition=ExecutionRetryDisposition.RETRYABLE,
            ),
        ),
        idempotency_rule_sha256=H5,
    )


def _expected_artifact(
    *,
    key: str = "raw-result",
    role: ArtifactRole = ArtifactRole.RAW_OUTPUT,
) -> ExpectedArtifact:
    return ExpectedArtifact(
        artifact_key=key,
        role=role,
        media_type="application/json",
        schema_sha256=H1,
        max_bytes=1_000_000,
        data_classification="research-internal",
        retention_policy_sha256=H2,
    )


def _slot(*, slot_index: int = 1, slot_count: int = 1) -> ScientificReplicateSlot:
    return ScientificReplicateSlot(
        quest_id=QUEST_ID,
        protocol_sha256=H0,
        work_order_id="work-order.simulation",
        work_order_node_id="node.simulation",
        work_order_node_sha256=H7,
        slot_count=slot_count,
        slot_index=slot_index,
        replicate_kind=ScientificReplicateKind.CONFIRMATION,
        preregistration_sha256=H3,
        randomization_seed_sha256=H4,
    )


def _attempt(
    slot: ScientificReplicateSlot,
    *,
    number: int = 1,
    previous_attempt_id: str | None = None,
    prior_failure: str | None = None,
    prior_failure_category: ExecutionFailureCategory | None = None,
) -> InfrastructureAttempt:
    return InfrastructureAttempt(
        replicate_slot_id=slot.replicate_slot_id,
        attempt_number=number,
        previous_attempt_id=previous_attempt_id,
        prior_confirmed_failure_receipt_sha256=prior_failure,
        prior_failure_category=prior_failure_category,
    )


def _intent(
    *,
    slot: ScientificReplicateSlot | None = None,
    attempt: InfrastructureAttempt | None = None,
    effect_class: ExecutionEffectClass = ExecutionEffectClass.REPLAY_SAFE,
    external_request: ExternalRequestIdentity | None = None,
    expected_artifacts: tuple[ExpectedArtifact, ...] | None = None,
    max_attempts: int = 2,
) -> ExecutionIntent:
    slot = slot or _slot()
    attempt = attempt or _attempt(slot)
    resource_class = _resource_class()
    return ExecutionIntent(
        quest_id=QUEST_ID,
        protocol_sha256=H0,
        work_order_id="work-order.simulation",
        work_order_sha256=H5,
        work_order_node_id="node.simulation",
        work_order_node_sha256=H7,
        capability_id="capability.simulation.v2",
        capability_manifest_sha256=H6,
        external_action_kind=(external_request.action_kind if external_request else None),
        resource_catalog_sha256=StaticResourceCatalog(
            catalog_key="test-catalog",
            resource_classes=(resource_class,),
        ).catalog_sha256,
        resource_request=_resource_request(
            resource_class.resource_class_id,
            attempts=max_attempts,
        ),
        retry_policy=_retry_policy(attempts=max_attempts),
        replicate_slot=slot,
        infrastructure_attempt=attempt,
        input_artifact_bindings=(
            InputArtifactBinding(
                input_port_id="input.raw",
                source_kind="protocol_input",
                artifact_verified_receipt_sha256=H7,
            ),
        ),
        expected_artifacts=expected_artifacts or (_expected_artifact(),),
        environment_sha256=H1,
        command_sha256=H2,
        execution_parameters_sha256=H3,
        effect_class=effect_class,
        external_request=external_request,
        authorized_at=NOW,
        deadline=NOW + timedelta(hours=2),
    )


def _manifest_and_verification(
    intent: ExecutionIntent,
) -> tuple[ArtifactManifest, ArtifactVerifiedReceipt]:
    requirement = intent.expected_artifacts[0]
    entry = ArtifactManifestEntry(
        expected_artifact_id=requirement.expected_artifact_id,
        artifact_key=requirement.artifact_key,
        role=requirement.role,
        content_sha256=H4,
        bytes=128,
        media_type=requirement.media_type,
        schema_sha256=requirement.schema_sha256,
        quarantine_ref="quarantine://attempt/raw-result",
    )
    manifest = ArtifactManifest(
        intent_sha256=intent.intent_sha256,
        execution_id=intent.execution_id,
        replicate_slot_id=intent.replicate_slot.replicate_slot_id,
        infrastructure_attempt_id=intent.infrastructure_attempt.infrastructure_attempt_id,
        entries=(entry,),
        produced_at=NOW + timedelta(minutes=10),
    )
    verified = ArtifactVerifiedReceipt(
        artifact_manifest_sha256=manifest.manifest_sha256,
        producer_attempt_id=intent.infrastructure_attempt.infrastructure_attempt_id,
        artifact=entry,
        custody_mode=ArtifactCustodyMode.CENTRAL_REHASH,
        verifier_principal_id="artifact-verifier",
        object_store_id="research-cas",
        final_object_ref=f"cas://sha256/{entry.content_sha256}",
        final_object_version="generation-1",
        verified_at=NOW + timedelta(minutes=12),
    )
    return manifest, verified


def test_static_catalog_is_content_addressed_and_rejects_live_inventory() -> None:
    resource = _resource_class()
    same = StaticResourceClass.model_validate(resource.model_dump())
    catalog = StaticResourceCatalog(
        catalog_key="test-catalog",
        resource_classes=(resource,),
    )

    assert resource.resource_class_id == same.resource_class_id
    assert resource.resource_class_sha256 == same.resource_class_sha256
    assert catalog.resource_class_ids == (resource.resource_class_id,)
    assert (
        catalog.catalog_sha256
        == StaticResourceCatalog.model_validate(catalog.model_dump()).catalog_sha256
    )
    assert b"current_free_memory" not in canonical_json_bytes(catalog)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        StaticResourceClass.model_validate({**resource.model_dump(), "current_free_memory": 1234})
    with pytest.raises(ValidationError, match="frozen"):
        resource.cpu_cores = 99  # type: ignore[misc]


def test_execution_identity_hashes_have_a_frozen_v1_golden() -> None:
    resource = _resource_class()
    slot = _slot()
    intent = _intent(slot=slot)

    assert resource.resource_class_id == "rsc_994ce73b3b4a73194772739ea7a43140"
    assert resource.resource_class_sha256 == (
        "487fa71c3aaa550b7fe0c600a916e6eb27d170c4755ed59b989300d5c6dbc4e8"
    )
    assert slot.replicate_slot_id == "rps_3fdf1c977d2f60e59f156d916fd69c02"
    assert intent.infrastructure_attempt.infrastructure_attempt_id == (
        "iat_b7e2a869f4954383e9a5e5be53eee019"
    )
    assert intent.execution_id == "exe_c1c313ab026ecda444f560f596fd0cca"
    assert intent.intent_sha256 == (
        "5746d0435250428db2471c1e84a2d49efc2236f2d83a5600ee29f80d818ab55f"
    )


def test_resource_request_is_structural_and_fail_closed() -> None:
    resource = _resource_class()
    request = _resource_request(resource.resource_class_id)

    assert request.accepted_resource_class_ids == (resource.resource_class_id,)
    assert len(request.request_sha256) == 64
    with pytest.raises(ValidationError, match="accelerator requests require"):
        request.model_copy(
            update={"allowed_accelerator_models": ()},
        ).model_validate(request.model_copy(update={"allowed_accelerator_models": ()}))
    with pytest.raises(ValidationError, match="pinned locality"):
        ExecutionResourceRequest.model_validate(
            {
                **request.model_dump(),
                "data_locality": DataLocality.SITE_PINNED,
                "locality_labels": (),
            }
        )
    with pytest.raises(ValidationError, match="egress allowlist"):
        ExecutionResourceRequest.model_validate(
            {**request.model_dump(), "egress_allowlist_sha256": H0}
        )


def test_scientific_slot_is_not_an_infrastructure_attempt() -> None:
    slot = _slot()
    first = _attempt(slot)
    first_intent = _intent(slot=slot, attempt=first)
    second = _attempt(
        slot,
        number=2,
        previous_attempt_id=first.infrastructure_attempt_id,
        prior_failure=H7,
        prior_failure_category=ExecutionFailureCategory.INFRASTRUCTURE,
    )
    retry_intent = _intent(slot=slot, attempt=second)

    assert retry_intent.execution_id == first_intent.execution_id
    assert (
        retry_intent.replicate_slot.replicate_slot_id
        == first_intent.replicate_slot.replicate_slot_id
    )
    assert retry_intent.infrastructure_attempt.infrastructure_attempt_id != (
        first_intent.infrastructure_attempt.infrastructure_attempt_id
    )
    assert retry_intent.intent_sha256 != first_intent.intent_sha256

    another_replicate = _intent(slot=_slot(slot_index=2, slot_count=2))
    assert another_replicate.execution_id != first_intent.execution_id
    with pytest.raises(ValidationError, match="complete confirmed failure lineage"):
        _attempt(
            slot,
            number=2,
            previous_attempt_id=first.infrastructure_attempt_id,
        )


def test_execution_identity_rejects_cross_slot_attempts() -> None:
    first_slot = _slot(slot_index=1, slot_count=2)
    second_slot = _slot(slot_index=2, slot_count=2)
    with pytest.raises(ValidationError, match="another scientific replicate slot"):
        _intent(slot=first_slot, attempt=_attempt(second_slot))


def test_reliable_negative_is_engineering_success_not_execution_failure() -> None:
    intent = _intent()
    manifest, verified = _manifest_and_verification(intent)
    receipt = ExecutionReceipt(
        intent=intent,
        worker_node_manifest_sha256=H0,
        node_inventory_sha256=H1,
        resource_lease_sha256=H2,
        node_execution_receipt_sha256=H3,
        started_at=NOW,
        ended_at=NOW + timedelta(minutes=10),
        observed_at=NOW + timedelta(minutes=11),
        terminal_state=ExecutionTerminalState.ENGINEERING_SUCCEEDED,
        raw_scientific_outcome=RawScientificOutcome.NEGATIVE,
        artifact_manifest=manifest,
        artifact_verified_receipts=(verified,),
        telemetry_sha256=H5,
        verified_by_principal_id="execution-verifier",
        verified_at=NOW + timedelta(minutes=13),
    )

    assert receipt.terminal_state is ExecutionTerminalState.ENGINEERING_SUCCEEDED
    assert receipt.failure is None
    assert receipt.raw_scientific_outcome is RawScientificOutcome.NEGATIVE
    assert len(receipt.execution_receipt_sha256) == 64

    with pytest.raises(ValidationError, match="failed execution cannot manufacture"):
        ExecutionReceipt.model_validate(
            {
                **receipt.model_dump(),
                "terminal_state": ExecutionTerminalState.EXECUTION_FAILED,
                "failure": ExecutionFailure(
                    category=ExecutionFailureCategory.INFRASTRUCTURE,
                    detail_sha256=H6,
                    capability_failure_id="failure.infrastructure",
                    capability_failure_detection_rule_sha256=H6,
                    retryable_after_confirmed_termination=True,
                ),
                "artifact_manifest": None,
                "artifact_verified_receipts": (),
            }
        )


def test_engineering_success_requires_all_declared_artifacts_to_be_verified() -> None:
    intent = _intent()
    manifest, _ = _manifest_and_verification(intent)
    with pytest.raises(ValidationError, match="verification of every artifact"):
        ExecutionReceipt(
            intent=intent,
            worker_node_manifest_sha256=H0,
            node_inventory_sha256=H1,
            resource_lease_sha256=H2,
            node_execution_receipt_sha256=H3,
            started_at=NOW,
            ended_at=NOW + timedelta(minutes=10),
            observed_at=NOW + timedelta(minutes=11),
            terminal_state=ExecutionTerminalState.ENGINEERING_SUCCEEDED,
            artifact_manifest=manifest,
            verified_by_principal_id="execution-verifier",
            verified_at=NOW + timedelta(minutes=13),
        )


def test_external_ambiguity_reconciles_same_attempt_and_one_time_never_retries() -> None:
    slot = _slot()
    external_request = ExternalRequestIdentity(
        provider_id="measurement-provider",
        action_kind="physical.measurement",
        scope_key="specimen-42",
        replicate_slot_id=slot.replicate_slot_id,
        request_sha256=H0,
    )
    provider_artifact = _expected_artifact(
        key="provider-receipt",
        role=ArtifactRole.PROVIDER_RECEIPT,
    )
    intent = _intent(
        slot=slot,
        effect_class=ExecutionEffectClass.ONE_TIME_EXTERNAL,
        external_request=external_request,
        expected_artifacts=(provider_artifact,),
        max_attempts=1,
    )
    reconciliation = ExecutionReceipt(
        intent=intent,
        worker_node_manifest_sha256=H0,
        node_inventory_sha256=H1,
        resource_lease_sha256=H2,
        node_execution_receipt_sha256=H3,
        started_at=NOW,
        observed_at=NOW + timedelta(minutes=5),
        terminal_state=ExecutionTerminalState.RECONCILIATION_REQUIRED,
        failure=ExecutionFailure(
            category=ExecutionFailureCategory.AMBIGUOUS_EXTERNAL_OUTCOME,
            detail_sha256=H4,
        ),
        verified_by_principal_id="execution-verifier",
        verified_at=NOW + timedelta(minutes=6),
    )

    assert reconciliation.intent.infrastructure_attempt.infrastructure_attempt_id == (
        intent.infrastructure_attempt.infrastructure_attempt_id
    )
    assert reconciliation.external_provider_receipt_sha256 is None

    manifest, verified = _manifest_and_verification(intent)
    reconciled_success = ExecutionReceipt(
        intent=intent,
        worker_node_manifest_sha256=H0,
        node_inventory_sha256=H1,
        resource_lease_sha256=H2,
        node_execution_receipt_sha256=H5,
        started_at=NOW,
        ended_at=NOW + timedelta(minutes=4),
        observed_at=NOW + timedelta(minutes=11),
        terminal_state=ExecutionTerminalState.ENGINEERING_SUCCEEDED,
        artifact_manifest=manifest,
        artifact_verified_receipts=(verified,),
        external_provider_receipt_sha256=H4,
        reconciles_receipt_sha256=reconciliation.execution_receipt_sha256,
        verified_by_principal_id="execution-verifier",
        verified_at=NOW + timedelta(minutes=13),
    )
    assert reconciled_success.intent.infrastructure_attempt.infrastructure_attempt_id == (
        reconciliation.intent.infrastructure_attempt.infrastructure_attempt_id
    )
    assert reconciled_success.reconciles_receipt_sha256 == (reconciliation.execution_receipt_sha256)
    assert external_request.provider_idempotency_key is not None
    assert external_request.provider_idempotency_key.startswith("aletheia:")
    with pytest.raises(ValidationError, match="one-time external effects permit exactly one"):
        _intent(
            slot=slot,
            attempt=_attempt(
                slot,
                number=2,
                previous_attempt_id=intent.infrastructure_attempt.infrastructure_attempt_id,
                prior_failure=reconciliation.execution_receipt_sha256,
                prior_failure_category=ExecutionFailureCategory.INFRASTRUCTURE,
            ),
            effect_class=ExecutionEffectClass.ONE_TIME_EXTERNAL,
            external_request=external_request,
            expected_artifacts=(provider_artifact,),
            max_attempts=2,
        )


def test_external_effect_requires_stable_request_and_provider_receipt_expectation() -> None:
    with pytest.raises(ValidationError, match="stable external request identity"):
        _intent(effect_class=ExecutionEffectClass.IDEMPOTENT_EXTERNAL)
    slot = _slot()
    external_request = ExternalRequestIdentity(
        provider_id="provider",
        action_kind="measurement",
        scope_key="sample",
        replicate_slot_id=slot.replicate_slot_id,
        request_sha256=H0,
    )
    with pytest.raises(ValidationError, match="idempotency key does not match"):
        ExternalRequestIdentity(
            provider_id="provider",
            action_kind="measurement",
            scope_key="sample",
            replicate_slot_id=slot.replicate_slot_id,
            request_sha256=H0,
            provider_idempotency_key="caller-chosen-key",
        )
    with pytest.raises(ValidationError, match="provider receipt artifact"):
        _intent(
            slot=slot,
            effect_class=ExecutionEffectClass.IDEMPOTENT_EXTERNAL,
            external_request=external_request,
        )


def _failed_execution_receipt(
    intent: ExecutionIntent,
    *,
    category: ExecutionFailureCategory = ExecutionFailureCategory.INFRASTRUCTURE,
    retryable: bool = True,
    node_receipt_sha256: str = H3,
) -> ExecutionReceipt:
    return ExecutionReceipt(
        intent=intent,
        worker_node_manifest_sha256=H0,
        node_inventory_sha256=H1,
        resource_lease_sha256=H2,
        node_execution_receipt_sha256=node_receipt_sha256,
        started_at=NOW,
        ended_at=NOW + timedelta(minutes=1),
        observed_at=NOW + timedelta(minutes=2),
        terminal_state=ExecutionTerminalState.EXECUTION_FAILED,
        failure=ExecutionFailure(
            category=category,
            detail_sha256=H6,
            capability_failure_id="failure.infrastructure" if retryable else None,
            capability_failure_detection_rule_sha256=H6 if retryable else None,
            retryable_after_confirmed_termination=retryable,
        ),
        verified_by_principal_id="execution-verifier",
        verified_at=NOW + timedelta(minutes=3),
    )


def _retry_contracts() -> tuple[ExecutionIntent, ExecutionReceipt, ExecutionIntent]:
    slot = _slot()
    previous = _intent(slot=slot)
    receipt = _failed_execution_receipt(previous)
    current = _intent(
        slot=slot,
        attempt=_attempt(
            slot,
            number=2,
            previous_attempt_id=previous.infrastructure_attempt.infrastructure_attempt_id,
            prior_failure=receipt.execution_receipt_sha256,
            prior_failure_category=ExecutionFailureCategory.INFRASTRUCTURE,
        ),
    )
    return previous, receipt, current


def test_retry_binding_accepts_only_the_exact_next_attempt_and_failure_receipt() -> None:
    previous, receipt, current = _retry_contracts()

    verify_execution_retry_binding(previous, current, receipt)

    assert current.execution_id == previous.execution_id
    assert current.replicate_slot == previous.replicate_slot
    assert current.infrastructure_attempt.attempt_number == 2
    assert current.infrastructure_attempt.previous_attempt_id == (
        previous.infrastructure_attempt.infrastructure_attempt_id
    )
    assert current.infrastructure_attempt.prior_confirmed_failure_receipt_sha256 == (
        receipt.execution_receipt_sha256
    )
    assert current.infrastructure_attempt.prior_failure_category is (
        receipt.failure.category if receipt.failure is not None else None
    )


def test_retry_binding_rejects_frozen_receipt_category_and_lineage_mutations() -> None:
    previous, receipt, current = _retry_contracts()
    attempt = current.infrastructure_attempt

    changed_frozen_field = current.model_copy(update={"command_sha256": H0})
    changed_receipt_hash = current.model_copy(
        update={
            "infrastructure_attempt": attempt.model_copy(
                update={"prior_confirmed_failure_receipt_sha256": H0}
            )
        }
    )
    changed_category = current.model_copy(
        update={
            "infrastructure_attempt": attempt.model_copy(
                update={"prior_failure_category": ExecutionFailureCategory.TIMEOUT}
            )
        }
    )
    changed_attempt_lineage = current.model_copy(
        update={
            "infrastructure_attempt": attempt.model_copy(
                update={"previous_attempt_id": "iat_" + "f" * 32}
            )
        }
    )
    changed_prior_receipt = receipt.model_copy(update={"node_execution_receipt_sha256": H7})
    assert receipt.failure is not None
    undeclared_capability_failure = receipt.model_copy(
        update={
            "failure": receipt.failure.model_copy(
                update={"capability_failure_id": "failure.undeclared"}
            )
        }
    )
    wrong_detection_rule = receipt.model_copy(
        update={
            "failure": receipt.failure.model_copy(
                update={"capability_failure_detection_rule_sha256": H0}
            )
        }
    )

    cases = (
        (changed_frozen_field, receipt, "changed a frozen scientific"),
        (changed_receipt_hash, receipt, "lineage does not bind"),
        (changed_category, receipt, "lineage does not bind"),
        (changed_attempt_lineage, receipt, "lineage does not bind"),
        (current, changed_prior_receipt, "lineage does not bind"),
        (current, undeclared_capability_failure, "not retryable under"),
        (current, wrong_detection_rule, "not retryable under"),
    )
    for candidate, prior_receipt, message in cases:
        with pytest.raises(ExecutionRetryBindingError, match=message):
            verify_execution_retry_binding(previous, candidate, prior_receipt)


def test_retry_binding_rejects_a_nonretryable_prior_failure() -> None:
    slot = _slot()
    previous = _intent(slot=slot)
    receipt = _failed_execution_receipt(previous, retryable=False)
    current = _intent(
        slot=slot,
        attempt=_attempt(
            slot,
            number=2,
            previous_attempt_id=previous.infrastructure_attempt.infrastructure_attempt_id,
            prior_failure=receipt.execution_receipt_sha256,
            prior_failure_category=ExecutionFailureCategory.INFRASTRUCTURE,
        ),
    )

    with pytest.raises(ExecutionRetryBindingError, match="confirmed retryable failure"):
        verify_execution_retry_binding(previous, current, receipt)


def test_generic_retry_binding_rejects_checkpoint_resume_without_checkpoint_custody() -> None:
    previous, receipt, current = _retry_contracts()
    checkpoint_policy = ExecutionRetryPolicy(
        mode=ExecutionRetryMode.CHECKPOINT_RESUME,
        maximum_attempts_per_scientific_slot=2,
        retry_rules=previous.retry_policy.retry_rules,
        idempotency_rule_sha256=H5,
        checkpoint_schema_sha256=H4,
    )
    previous = previous.model_copy(update={"retry_policy": checkpoint_policy})
    receipt = _failed_execution_receipt(previous)
    current = current.model_copy(
        update={
            "retry_policy": checkpoint_policy,
            "infrastructure_attempt": current.infrastructure_attempt.model_copy(
                update={"prior_confirmed_failure_receipt_sha256": receipt.execution_receipt_sha256}
            ),
        }
    )

    with pytest.raises(ExecutionRetryBindingError, match="direct-idempotent"):
        verify_execution_retry_binding(previous, current, receipt)


def test_retry_binding_rejects_failure_category_relabeling() -> None:
    slot = _slot()
    previous = _intent(slot=slot)
    receipt = _failed_execution_receipt(
        previous,
        category=ExecutionFailureCategory.PROCESS_ERROR,
    )
    current = _intent(
        slot=slot,
        attempt=_attempt(
            slot,
            number=2,
            previous_attempt_id=previous.infrastructure_attempt.infrastructure_attempt_id,
            prior_failure=receipt.execution_receipt_sha256,
            prior_failure_category=ExecutionFailureCategory.PROCESS_ERROR,
        ),
    )

    with pytest.raises(ExecutionRetryBindingError, match="not retryable under"):
        verify_execution_retry_binding(previous, current, receipt)
