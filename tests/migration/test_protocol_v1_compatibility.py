from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from aletheia.migration.protocol_v1_compatibility import (
    F10V1AtomicBundleBinding,
    F9V1WholeObjectBinding,
    bind_f10_v1_atomic_bundle,
    bind_f9_v1_whole_object,
)

RUN_ID = "a" * 32


def test_f9_binding_preserves_legacy_scope_and_only_retains_opaque_identity() -> None:
    source = ('{"run_id":"' + RUN_ID + '","hypothesis":{"opaque":true}}').encode()

    binding = bind_f9_v1_whole_object(
        run_id=RUN_ID,
        source_schema_name="aletheia.f9.hypothesis",
        source_schema_version=1,
        opaque_payload=source,
    )

    assert binding.run_id == RUN_ID
    assert binding.source_schema_name == "aletheia.f9.hypothesis"
    assert binding.source_schema_version == 1
    assert binding.opaque_payload_sha256 == hashlib.sha256(source).hexdigest()
    assert binding.binding_granularity == "whole_object"
    assert binding.access_mode == "read_only"
    assert binding.creates_v2_identity is False
    assert binding.grants_admission is False
    assert binding.refreshable is False
    assert source.decode() not in binding.model_dump_json()


def test_f9_binding_is_deterministic_and_byte_sensitive() -> None:
    arguments = {
        "run_id": RUN_ID,
        "source_schema_name": "aletheia.f9.hypothesis",
        "source_schema_version": 1,
    }

    first = bind_f9_v1_whole_object(**arguments, opaque_payload=b'{"value":1}')
    replay = bind_f9_v1_whole_object(**arguments, opaque_payload=b'{"value":1}')
    changed = bind_f9_v1_whole_object(**arguments, opaque_payload=b'{"value":2}')

    assert replay == first
    assert replay.binding_sha256 == first.binding_sha256
    assert changed.opaque_payload_sha256 != first.opaque_payload_sha256
    assert changed.binding_sha256 != first.binding_sha256


def test_f10_binding_keeps_v1_bundle_indivisible_and_non_authoritative() -> None:
    source = b'{"manifest":{},"implementation":{},"qualification":{}}'

    binding = bind_f10_v1_atomic_bundle(
        source_schema_name="aletheia.f10.capability_bundle",
        source_schema_version=1,
        opaque_payload=source,
    )

    assert binding.binding_granularity == "atomic_bundle"
    assert binding.splittable is False
    assert binding.grants_execution_authority is False
    assert binding.access_mode == "read_only"
    assert binding.opaque_payload_sha256 == hashlib.sha256(source).hexdigest()
    assert not any("component" in field for field in type(binding).model_fields)
    assert source.decode() not in binding.model_dump_json()


@pytest.mark.parametrize(
    "factory",
    [bind_f9_v1_whole_object, bind_f10_v1_atomic_bundle],
)
@pytest.mark.parametrize("payload", [b"", bytearray(b"legacy")])
def test_binding_requires_nonempty_exact_custody_bytes(factory: object, payload: object) -> None:
    arguments = {
        "source_schema_name": "aletheia.legacy.object",
        "source_schema_version": 1,
        "opaque_payload": payload,
    }
    if factory is bind_f9_v1_whole_object:
        arguments["run_id"] = RUN_ID

    with pytest.raises(ValueError, match="non-empty exact bytes"):
        factory(**arguments)  # type: ignore[operator]


@pytest.mark.parametrize("run_id", [" " + "a" * 31, "a" * 31 + " ", "g" * 32])
def test_f9_binding_does_not_normalize_or_accept_ambiguous_legacy_run_id(
    run_id: str,
) -> None:
    with pytest.raises(ValidationError, match="run_id"):
        bind_f9_v1_whole_object(
            run_id=run_id,
            source_schema_name="aletheia.f9.hypothesis",
            source_schema_version=1,
            opaque_payload=b"legacy",
        )


def test_compatibility_binding_rejects_non_v1_source_version() -> None:
    with pytest.raises(ValidationError, match="source_schema_version"):
        bind_f10_v1_atomic_bundle(
            source_schema_name="aletheia.f10.capability_bundle",
            source_schema_version=2,  # type: ignore[arg-type]
            opaque_payload=b"legacy-f10",
        )


@pytest.mark.parametrize(
    ("model", "factory", "arguments"),
    [
        (
            F9V1WholeObjectBinding,
            bind_f9_v1_whole_object,
            {
                "run_id": RUN_ID,
                "source_schema_name": "aletheia.f9.hypothesis",
                "source_schema_version": 1,
                "opaque_payload": b"legacy-f9",
            },
        ),
        (
            F10V1AtomicBundleBinding,
            bind_f10_v1_atomic_bundle,
            {
                "source_schema_name": "aletheia.f10.capability_bundle",
                "source_schema_version": 1,
                "opaque_payload": b"legacy-f10",
            },
        ),
    ],
)
def test_serialized_binding_rejects_identity_tampering(
    model: type[F9V1WholeObjectBinding] | type[F10V1AtomicBundleBinding],
    factory: object,
    arguments: dict[str, object],
) -> None:
    binding = factory(**arguments)  # type: ignore[operator]
    serialized = binding.model_dump(mode="json")
    serialized["opaque_payload_sha256"] = "f" * 64

    with pytest.raises(ValidationError, match="binding_sha256"):
        model.model_validate(serialized)


def test_compatibility_leaf_is_not_reexported_from_migration_package() -> None:
    import aletheia.migration as migration

    assert "F9V1WholeObjectBinding" not in migration.__all__
    assert "F10V1AtomicBundleBinding" not in migration.__all__
    assert "bind_f9_v1_whole_object" not in migration.__all__
    assert "bind_f10_v1_atomic_bundle" not in migration.__all__
