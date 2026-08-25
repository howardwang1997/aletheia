"""Pure primitives shared by the domain-independent scientific protocol contracts.

PR-3 contracts are values, not services.  This module deliberately has no persistence,
configuration, filesystem, process, or network dependency.  The one authority dependency is the
PR-2 research kernel: protocol scope must reuse its immutable routing binding and exact question
reference instead of inventing a second question or Quest namespace.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aletheia.research_kernel.commands import ResearchScopeBinding
from aletheia.research_kernel.schemas import (
    KernelObjectKind,
    KernelObjectRef,
    canonical_json_bytes,
    canonical_sha256,
)

PROTOCOL_SCHEMA_VERSION = 1

SHA256_PATTERN = r"^[0-9a-f]{64}$"
BRANCH_ID_PATTERN = r"^rbr_[0-9a-f]{32}$"
SCOPE_NODE_ID_PATTERN = r"^(?:qst|prg|cmp)_[0-9a-f]{32}$"
LOCAL_ID_PATTERN = r"^[a-z][a-z0-9_.:/-]{1,127}$"
SEMVER_PATTERN = r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
PRINCIPAL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_:/.-]{0,127}$"


class ProtocolModel(BaseModel):
    """Immutable, closed-world base for every PR-3 pure contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def _datetimes_are_aware_utc(self) -> "ProtocolModel":
        for field_name in type(self).model_fields:
            value = getattr(self, field_name)
            if isinstance(value, datetime) and (
                value.tzinfo is None or value.utcoffset() != timedelta(0)
            ):
                raise ValueError(f"{field_name} must be timezone-aware UTC")
        return self


def canonical_strings(
    values: tuple[str, ...], label: str, *, required: bool = False
) -> tuple[str, ...]:
    """Require a deterministic set encoded as a sorted tuple."""

    if required and not values:
        raise ValueError(f"{label} must not be empty")
    if any(not value or value != value.strip() for value in values):
        raise ValueError(f"{label} must contain nonempty canonical strings")
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{label} must be unique and canonically ordered")
    return values


def canonical_sha256s(
    values: tuple[str, ...], label: str, *, required: bool = False
) -> tuple[str, ...]:
    """Require a canonical tuple containing only lowercase SHA-256 identities."""

    canonical_strings(values, label, required=required)
    if any(re.fullmatch(SHA256_PATTERN, value) is None for value in values):
        raise ValueError(f"{label} must contain lowercase SHA-256 identities")
    return values


_ProtocolModelT = TypeVar("_ProtocolModelT", bound=ProtocolModel)


def canonical_models(
    values: tuple[_ProtocolModelT, ...],
    *,
    key: Callable[[_ProtocolModelT], str],
    label: str,
) -> tuple[_ProtocolModelT, ...]:
    """Require immutable model collections to be unique and in their declared key order."""

    keys = tuple(key(item) for item in values)
    if len(keys) != len(set(keys)) or keys != tuple(sorted(keys)):
        raise ValueError(f"{label} must be unique and canonically ordered")
    return values


class JsonSchemaRef(ProtocolModel):
    """Exact identity of a typed JSON port schema; the schema bytes live in CAS."""

    schema_name: Literal["aletheia.json_schema_ref"] = "aletheia.json_schema_ref"
    schema_version: Literal[1] = PROTOCOL_SCHEMA_VERSION
    schema_id: str = Field(pattern=LOCAL_ID_PATTERN)
    semantic_version: str = Field(pattern=SEMVER_PATTERN)
    schema_sha256: str = Field(pattern=SHA256_PATTERN)
    media_type: str = Field(default="application/schema+json", min_length=1, max_length=255)


class ProtocolScope(ProtocolModel):
    """Exact protocol binding to one committed PR-2 research-graph view.

    ``scope_node_id`` is redundant on purpose: it records the most-specific address selected from
    the immutable PR-2 scope and is mechanically checked.  ``question_ref`` is the authoritative
    kernel ``ResearchQuestionVersion`` reference; no protocol-local question identity exists.
    """

    schema_name: Literal["aletheia.protocol_graph_scope"] = "aletheia.protocol_graph_scope"
    schema_version: Literal[1] = PROTOCOL_SCHEMA_VERSION
    scope_binding: ResearchScopeBinding
    scope_node_id: str = Field(pattern=SCOPE_NODE_ID_PATTERN)
    branch_id: str = Field(pattern=BRANCH_ID_PATTERN)
    question_ref: KernelObjectRef
    graph_snapshot_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _scope_is_exact(self) -> "ProtocolScope":
        expected_node = (
            self.scope_binding.campaign_id
            or self.scope_binding.program_id
            or self.scope_binding.quest_id
        )
        if self.scope_node_id != expected_node:
            raise ValueError("scope_node_id must be the most-specific PR-2 scope node")
        if (
            self.question_ref.object_kind is not KernelObjectKind.QUESTION
            or self.question_ref.quest_id != self.scope_binding.quest_id
        ):
            raise ValueError("graph scope requires an exact kernel question in the same Quest")
        return self

    @property
    def graph_scope_sha256(self) -> str:
        return canonical_sha256(self)


# A descriptive compatibility name for callers that distinguish graph scope from other protocol
# scopes.  Both names denote the same closed contract, not two authority types.
GraphScopeBinding = ProtocolScope


__all__ = [
    "BRANCH_ID_PATTERN",
    "GraphScopeBinding",
    "JsonSchemaRef",
    "LOCAL_ID_PATTERN",
    "PRINCIPAL_ID_PATTERN",
    "PROTOCOL_SCHEMA_VERSION",
    "ProtocolScope",
    "ProtocolModel",
    "SCOPE_NODE_ID_PATTERN",
    "SEMVER_PATTERN",
    "SHA256_PATTERN",
    "canonical_json_bytes",
    "canonical_models",
    "canonical_sha256s",
    "canonical_sha256",
    "canonical_strings",
]
