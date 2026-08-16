"""Authenticated controller endpoints for the scientific program graph.

No lifecycle or graph state is held in FastAPI.  Every mutation delegates to the transactional
store and every read is a fresh deterministic ledger reconstruction.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field

from aletheia.api.deps import require_access
from aletheia.jobs import ScientificIdempotencyConflict
from aletheia.programs import (
    BudgetAllocationSpec,
    CampaignExperimentBindingSpec,
    CampaignRunBindingSpec,
    CampaignSpec,
    DataRoleAllocationSpec,
    DependencySpec,
    GraphCommandContext,
    GraphMutationReceipt,
    GraphNodeState,
    NodeTransitionSpec,
    ProgramGraphConflict,
    ProgramGraphCycleError,
    ProgramGraphInvariantError,
    ProgramGraphNotFound,
    ProgramGraphStore,
    ProgramGraphTransitionError,
    ProgramQuestionBindingSpec,
    QuestGraphSnapshot,
    QuestSpec,
    ResearchProgramSpec,
    ScientificFamilySpec,
)

router = APIRouter(prefix="/research-graph", tags=["research-program-graph"])
_STORE = ProgramGraphStore()
_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"
_T = TypeVar("_T")


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CommandMetadata(_Request):
    idempotency_key: str = Field(pattern=_IDENTITY_PATTERN)
    source_event_key: str | None = Field(default=None, pattern=_IDENTITY_PATTERN)


class CreateQuestRequest(CommandMetadata):
    spec: QuestSpec


class CreateProgramRequest(CommandMetadata):
    spec: ResearchProgramSpec


class CreateScientificFamilyRequest(CommandMetadata):
    spec: ScientificFamilySpec


class CreateCampaignRequest(CommandMetadata):
    spec: CampaignSpec


class TransitionNodeRequest(CommandMetadata):
    expected_version: int = Field(ge=1)
    to_state: GraphNodeState
    reason: str = Field(min_length=1, max_length=4_000)


class AddDependencyRequest(CommandMetadata):
    dependency: DependencySpec


class BindRunRequest(CommandMetadata):
    binding: CampaignRunBindingSpec


class BindExperimentRequest(CommandMetadata):
    binding: CampaignExperimentBindingSpec


class BindQuestionRequest(CommandMetadata):
    binding: ProgramQuestionBindingSpec


class AllocateDataRequest(CommandMetadata):
    allocation: DataRoleAllocationSpec


class AllocateBudgetRequest(CommandMetadata):
    allocation: BudgetAllocationSpec


def _context(request: CommandMetadata, user: dict[str, Any]) -> GraphCommandContext:
    return GraphCommandContext(
        idempotency_key=request.idempotency_key,
        source_event_key=request.source_event_key,
        principal=f"api:{user['id']}",
    )


async def _invoke(call: Callable[[], _T]) -> _T:
    try:
        return await asyncio.to_thread(call)
    except ProgramGraphNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProgramGraphInvariantError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ProgramGraphTransitionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ProgramGraphConflict, ProgramGraphCycleError, ScientificIdempotencyConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/quests", response_model=tuple[QuestGraphSnapshot, ...])
async def list_quests(
    _user: dict[str, Any] = Depends(require_access),
) -> tuple[QuestGraphSnapshot, ...]:
    return await _invoke(_STORE.list_quests)


@router.get("/quests/{quest_id}", response_model=QuestGraphSnapshot)
async def get_quest(
    quest_id: Annotated[str, Path(pattern=r"^qst_[0-9a-f]{32}$")],
    _user: dict[str, Any] = Depends(require_access),
) -> QuestGraphSnapshot:
    return await _invoke(lambda: _STORE.get_quest(quest_id))


@router.post("/quests", response_model=GraphMutationReceipt)
async def create_quest(
    request: CreateQuestRequest,
    user: dict[str, Any] = Depends(require_access),
) -> GraphMutationReceipt:
    return await _invoke(lambda: _STORE.create_quest(request.spec, _context(request, user)))


@router.post("/programs", response_model=GraphMutationReceipt)
async def create_program(
    request: CreateProgramRequest,
    user: dict[str, Any] = Depends(require_access),
) -> GraphMutationReceipt:
    return await _invoke(lambda: _STORE.create_program(request.spec, _context(request, user)))


@router.post("/scientific-families", response_model=GraphMutationReceipt)
async def create_scientific_family(
    request: CreateScientificFamilyRequest,
    user: dict[str, Any] = Depends(require_access),
) -> GraphMutationReceipt:
    return await _invoke(
        lambda: _STORE.create_scientific_family(request.spec, _context(request, user))
    )


@router.post("/campaigns", response_model=GraphMutationReceipt)
async def create_campaign(
    request: CreateCampaignRequest,
    user: dict[str, Any] = Depends(require_access),
) -> GraphMutationReceipt:
    return await _invoke(lambda: _STORE.create_campaign(request.spec, _context(request, user)))


@router.post("/nodes/{node_id}/transitions", response_model=GraphMutationReceipt)
async def transition_node(
    node_id: Annotated[str, Path(pattern=r"^(qst|prg|cmp)_[0-9a-f]{32}$")],
    request: TransitionNodeRequest,
    user: dict[str, Any] = Depends(require_access),
) -> GraphMutationReceipt:
    spec = NodeTransitionSpec(
        node_id=node_id,
        expected_version=request.expected_version,
        to_state=request.to_state,
        reason=request.reason,
    )
    return await _invoke(lambda: _STORE.transition_node(spec, _context(request, user)))


@router.post("/dependencies", response_model=GraphMutationReceipt)
async def add_dependency(
    request: AddDependencyRequest,
    user: dict[str, Any] = Depends(require_access),
) -> GraphMutationReceipt:
    return await _invoke(
        lambda: _STORE.add_dependency(request.dependency, _context(request, user))
    )


@router.post("/bindings/runs", response_model=GraphMutationReceipt)
async def bind_run(
    request: BindRunRequest,
    user: dict[str, Any] = Depends(require_access),
) -> GraphMutationReceipt:
    return await _invoke(lambda: _STORE.bind_run(request.binding, _context(request, user)))


@router.post("/bindings/experiments", response_model=GraphMutationReceipt)
async def bind_experiment(
    request: BindExperimentRequest,
    user: dict[str, Any] = Depends(require_access),
) -> GraphMutationReceipt:
    return await _invoke(
        lambda: _STORE.bind_experiment(request.binding, _context(request, user))
    )


@router.post("/bindings/questions", response_model=GraphMutationReceipt)
async def bind_question(
    request: BindQuestionRequest,
    user: dict[str, Any] = Depends(require_access),
) -> GraphMutationReceipt:
    return await _invoke(
        lambda: _STORE.bind_question(request.binding, _context(request, user))
    )


@router.post("/allocations/data", response_model=GraphMutationReceipt)
async def allocate_data(
    request: AllocateDataRequest,
    user: dict[str, Any] = Depends(require_access),
) -> GraphMutationReceipt:
    return await _invoke(
        lambda: _STORE.allocate_data(request.allocation, _context(request, user))
    )


@router.post("/allocations/budgets", response_model=GraphMutationReceipt)
async def allocate_budget(
    request: AllocateBudgetRequest,
    user: dict[str, Any] = Depends(require_access),
) -> GraphMutationReceipt:
    return await _invoke(
        lambda: _STORE.allocate_budget(request.allocation, _context(request, user))
    )


__all__ = ["router"]
