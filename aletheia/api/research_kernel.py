"""HTTP composition root for the authoritative research kernel.

The authenticated HTTP principal is a transport permission only.  It cannot author, replace, or
otherwise reinterpret the principal inside an ``AuthorizedResearchCommand``; scientific mutation
authority is established exclusively by the deployment-pinned trust root and certified policy.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field, model_validator

from aletheia.api.deps import require_access
from aletheia.config import get_settings
from aletheia.research_kernel.commands import AuthorizedResearchCommand
from aletheia.research_kernel.policy import (
    ResearchAuthorizationError,
    ResearchAuthorizationPolicyV1,
    ResearchAuthorizationTrustRootV1,
    verify_research_authorization_policy,
)
from aletheia.research_kernel.reducer import ResearchStateGraph
from aletheia.research_store.cas import FilesystemResearchArchive, ResearchArchiveError
from aletheia.research_store.store import (
    ResearchCommandReceipt,
    ResearchIdempotencyConflict,
    ResearchKernelStore,
    ResearchQuestNotFound,
    ResearchReplayAudit,
    ResearchStoreError,
    ResearchStoreInvariantError,
    ResearchVersionConflict,
)

router = APIRouter(prefix="/research-kernel", tags=["authoritative-research-kernel"])

_QUEST_ID_PATTERN = r"^qst_[0-9a-f]{32}$"
_PROGRAM_ID_PATTERN = r"^prg_[0-9a-f]{32}$"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_CUSTODY_FILE_BYTES = 16 * 1024 * 1024
_T = TypeVar("_T")


class _GenesisPolicyRegistry(BaseModel):
    """Deployment-pinned registry of the exact certified policy for each Quest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["aletheia.research_genesis_policy_registry"] = (
        "aletheia.research_genesis_policy_registry"
    )
    schema_version: Literal[1] = 1
    policies: tuple[ResearchAuthorizationPolicyV1, ...] = Field(max_length=10_000)

    @model_validator(mode="after")
    def _policies_are_unique_and_canonical(self) -> "_GenesisPolicyRegistry":
        quest_ids = tuple(policy.quest_id for policy in self.policies)
        if quest_ids != tuple(sorted(set(quest_ids))):
            raise ValueError("genesis policies must have unique, canonically ordered Quest ids")
        return self

    def policy_for(self, quest_id: str) -> ResearchAuthorizationPolicyV1:
        for policy in self.policies:
            if policy.quest_id == quest_id:
                return policy
        raise ValueError("no deployment-pinned genesis policy exists for this Quest")


class _KernelCustodyUnavailable(RuntimeError):
    """Deployment authority or CAS custody is absent, unsafe, or not exactly pinned."""


def _existing_absolute_path(configured: Path | None, *, label: str) -> Path:
    if configured is None:
        raise _KernelCustodyUnavailable(f"{label} is not configured")
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        raise _KernelCustodyUnavailable(f"{label} must be an absolute deployment pin")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise _KernelCustodyUnavailable(f"{label} does not exist") from exc
    if candidate != resolved or candidate.is_symlink():
        raise _KernelCustodyUnavailable(f"{label} cannot traverse symbolic links")
    return resolved


def _read_pinned_file(
    configured_path: Path | None,
    expected_sha256: str | None,
    *,
    label: str,
) -> bytes:
    path = _existing_absolute_path(configured_path, label=label)
    if not path.is_file():
        raise _KernelCustodyUnavailable(f"{label} must be a regular file")
    if expected_sha256 is None or _SHA256.fullmatch(expected_sha256) is None:
        raise _KernelCustodyUnavailable(f"{label} SHA-256 is not configured")
    try:
        size = path.stat().st_size
        if size < 1 or size > _MAX_CUSTODY_FILE_BYTES:
            raise _KernelCustodyUnavailable(f"{label} size is outside the custody bound")
        payload = path.read_bytes()
    except OSError as exc:
        raise _KernelCustodyUnavailable(f"{label} cannot be read") from exc
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise _KernelCustodyUnavailable(f"{label} content does not match its deployment pin")
    return payload


def _compose_research_kernel_store(quest_id: str) -> ResearchKernelStore:
    settings = get_settings()
    try:
        trust_root = ResearchAuthorizationTrustRootV1.model_validate_json(
            _read_pinned_file(
                settings.research_kernel_trust_root_path,
                settings.research_kernel_trust_root_file_sha256,
                label="research-kernel trust root",
            )
        )
        registry = _GenesisPolicyRegistry.model_validate_json(
            _read_pinned_file(
                settings.research_kernel_genesis_policy_registry_path,
                settings.research_kernel_genesis_policy_registry_file_sha256,
                label="research-kernel genesis-policy registry",
            )
        )
        policy = registry.policy_for(quest_id)
        for registered_policy in registry.policies:
            verify_research_authorization_policy(
                policy=registered_policy,
                trust_root=trust_root,
            )
        cas_root = _existing_absolute_path(
            settings.research_kernel_cas_root,
            label="research-kernel CAS root",
        )
        if not cas_root.is_dir():
            raise _KernelCustodyUnavailable("research-kernel CAS root must be a directory")
        archive = FilesystemResearchArchive(cas_root)
        return ResearchKernelStore(
            trust_root=trust_root,
            archive=archive,
            genesis_policy=policy,
        )
    except _KernelCustodyUnavailable:
        raise
    except (OSError, ValueError, ResearchAuthorizationError, ResearchArchiveError) as exc:
        raise _KernelCustodyUnavailable("research-kernel custody validation failed") from exc


def get_research_kernel_store(quest_id: str) -> ResearchKernelStore:
    """Build a store only from exact deployment custody; never synthesize authority."""

    try:
        return _compose_research_kernel_store(quest_id)
    except _KernelCustodyUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="authoritative research-kernel custody is unavailable",
        ) from exc


async def _invoke(call: Callable[[], _T]) -> _T:
    try:
        return await asyncio.to_thread(call)
    except ResearchQuestNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ResearchAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ResearchIdempotencyConflict, ResearchVersionConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ResearchStoreInvariantError as exc:
        raise HTTPException(status_code=500, detail="research-kernel audit failed") from exc
    except ResearchStoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ResearchArchiveError as exc:
        raise HTTPException(
            status_code=503, detail="research-kernel archive is unavailable"
        ) from exc


def _assert_command_scope(
    *,
    program_id: str,
    quest_id: str,
    command: AuthorizedResearchCommand,
) -> None:
    if command.quest_id != quest_id or command.scope_binding.quest_id != quest_id:
        raise HTTPException(status_code=409, detail="command belongs to another Quest")
    if command.scope_binding.program_id != program_id:
        raise HTTPException(status_code=409, detail="command belongs to another Program")


def _assert_audit_scope(*, program_id: str, quest_id: str, audit: ResearchReplayAudit) -> None:
    if audit.quest_id != quest_id or audit.scope_binding.quest_id != quest_id:
        raise HTTPException(status_code=409, detail="Quest stream scope does not match the path")
    if audit.scope_binding.program_id != program_id:
        raise HTTPException(status_code=409, detail="Quest stream belongs to another Program")


@router.post(
    "/programs/{program_id}/quests/{quest_id}/commands",
    response_model=ResearchCommandReceipt,
)
async def commit_authorized_command(
    program_id: Annotated[str, ApiPath(pattern=_PROGRAM_ID_PATTERN)],
    quest_id: Annotated[str, ApiPath(pattern=_QUEST_ID_PATTERN)],
    command: AuthorizedResearchCommand,
    _user: dict = Depends(require_access),
    store: ResearchKernelStore = Depends(get_research_kernel_store),
) -> ResearchCommandReceipt:
    """Commit the signed command unchanged; the HTTP user never becomes its principal."""

    _assert_command_scope(program_id=program_id, quest_id=quest_id, command=command)
    return await _invoke(lambda: store.commit(command))


@router.get(
    "/programs/{program_id}/quests/{quest_id}/audit",
    response_model=ResearchReplayAudit,
)
async def audit_quest_stream(
    program_id: Annotated[str, ApiPath(pattern=_PROGRAM_ID_PATTERN)],
    quest_id: Annotated[str, ApiPath(pattern=_QUEST_ID_PATTERN)],
    _user: dict = Depends(require_access),
    store: ResearchKernelStore = Depends(get_research_kernel_store),
) -> ResearchReplayAudit:
    audit = await _invoke(lambda: store.audit(quest_id))
    _assert_audit_scope(program_id=program_id, quest_id=quest_id, audit=audit)
    return audit


@router.get(
    "/programs/{program_id}/quests/{quest_id}/replay",
    response_model=ResearchStateGraph,
)
async def replay_quest_stream(
    program_id: Annotated[str, ApiPath(pattern=_PROGRAM_ID_PATTERN)],
    quest_id: Annotated[str, ApiPath(pattern=_QUEST_ID_PATTERN)],
    _user: dict = Depends(require_access),
    store: ResearchKernelStore = Depends(get_research_kernel_store),
) -> ResearchStateGraph:
    # ``audit`` performs the canonical replay and also returns the immutable routing binding, so
    # one locked read proves both state custody and the path-to-Program relationship.
    audit = await _invoke(lambda: store.audit(quest_id))
    _assert_audit_scope(program_id=program_id, quest_id=quest_id, audit=audit)
    return audit.state


__all__ = ["get_research_kernel_store", "router"]
