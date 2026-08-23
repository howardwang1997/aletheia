"""Content-addressed, read-only bindings for frozen F9/F10 v1 objects.

This module is deliberately a migration leaf.  It neither imports legacy model classes nor the
new protocol package, and it is not re-exported from :mod:`aletheia.migration`.  Callers hand it the
exact serialized bytes already read from legacy custody; the adapter records their opaque identity
without interpreting, refreshing, splitting, admitting, or executing them.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_F9_V1_RUN_ID_PATTERN = r"^[0-9a-f]{32}$"
_SOURCE_TOKEN_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$"


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class _FrozenCompatibilityBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    binding_schema_version: Literal[1] = 1
    source_schema_name: str = Field(pattern=_SOURCE_TOKEN_PATTERN)
    source_schema_version: Literal[1]
    opaque_payload_sha256: str = Field(pattern=_SHA256_PATTERN)
    access_mode: Literal["read_only"] = "read_only"
    binding_sha256: str = Field(pattern=_SHA256_PATTERN)

    def _identity_material(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"binding_sha256"})

    @model_validator(mode="after")
    def _verify_binding_identity(self) -> _FrozenCompatibilityBinding:
        expected = _sha256(_canonical_json_bytes(self._identity_material()))
        if self.binding_sha256 != expected:
            raise ValueError("binding_sha256 does not match the canonical binding material")
        return self


class F9V1WholeObjectBinding(_FrozenCompatibilityBinding):
    """Opaque identity for one complete F9 v1 object under its original legacy run scope."""

    binding_schema_name: Literal["aletheia.f9_v1_whole_object_binding"] = (
        "aletheia.f9_v1_whole_object_binding"
    )
    run_id: str = Field(pattern=_F9_V1_RUN_ID_PATTERN)
    binding_granularity: Literal["whole_object"] = "whole_object"
    creates_v2_identity: Literal[False] = False
    grants_admission: Literal[False] = False
    refreshable: Literal[False] = False


class F10V1AtomicBundleBinding(_FrozenCompatibilityBinding):
    """Opaque identity for an indivisible F10 v1 capability bundle."""

    binding_schema_name: Literal["aletheia.f10_v1_atomic_bundle_binding"] = (
        "aletheia.f10_v1_atomic_bundle_binding"
    )
    binding_granularity: Literal["atomic_bundle"] = "atomic_bundle"
    splittable: Literal[False] = False
    grants_execution_authority: Literal[False] = False


def _binding_sha256(
    binding_type: type[_FrozenCompatibilityBinding], material: dict[str, object]
) -> str:
    # Materialize every fixed Literal/default through the model rather than duplicating its
    # identity schema in a hand-maintained hash function.  The final public model validation below
    # checks both the input fields and this derived identity.
    placeholder = binding_type.model_construct(**material, binding_sha256="0" * 64)
    return _sha256(_canonical_json_bytes(placeholder._identity_material()))


def bind_f9_v1_whole_object(
    *,
    run_id: str,
    source_schema_name: str,
    source_schema_version: Literal[1],
    opaque_payload: bytes,
) -> F9V1WholeObjectBinding:
    """Bind exact F9 v1 bytes without interpreting them or creating a v2 object."""

    if not isinstance(opaque_payload, bytes) or not opaque_payload:
        raise ValueError("opaque_payload must be non-empty exact bytes")
    material: dict[str, object] = {
        "run_id": run_id,
        "source_schema_name": source_schema_name,
        "source_schema_version": source_schema_version,
        "opaque_payload_sha256": _sha256(opaque_payload),
    }
    return F9V1WholeObjectBinding.model_validate(
        {**material, "binding_sha256": _binding_sha256(F9V1WholeObjectBinding, material)}
    )


def bind_f10_v1_atomic_bundle(
    *,
    source_schema_name: str,
    source_schema_version: Literal[1],
    opaque_payload: bytes,
) -> F10V1AtomicBundleBinding:
    """Bind one indivisible F10 v1 bundle without granting execution authority."""

    if not isinstance(opaque_payload, bytes) or not opaque_payload:
        raise ValueError("opaque_payload must be non-empty exact bytes")
    material: dict[str, object] = {
        "source_schema_name": source_schema_name,
        "source_schema_version": source_schema_version,
        "opaque_payload_sha256": _sha256(opaque_payload),
    }
    return F10V1AtomicBundleBinding.model_validate(
        {**material, "binding_sha256": _binding_sha256(F10V1AtomicBundleBinding, material)}
    )


__all__ = [
    "F10V1AtomicBundleBinding",
    "F9V1WholeObjectBinding",
    "bind_f10_v1_atomic_bundle",
    "bind_f9_v1_whole_object",
]
