"""Authenticated controller endpoints for the scientific program graph.

No lifecycle or graph state is held in FastAPI.  Every mutation delegates to the transactional
store and every read is a fresh deterministic ledger reconstruction.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Path, Query
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
    MemoryCompactionArtifact,
    MemoryMutationReceipt,
    MemorySummaryDraft,
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
    ResearchMemoryConflict,
    ResearchMemoryContextOverflow,
    ResearchMemoryFactSpec,
    ResearchMemoryInvariantError,
    ResearchMemoryNotFound,
    ResearchMemorySnapshot,
    ResearchMemoryStale,
    ResearchMemoryStore,
    ResearchProgramSpec,
    ScientificFamilySpec,
    TaskContextReceipt,
    TaskContextRequest,
)

router = APIRouter(prefix="/research-graph", tags=["research-program-graph"])
_STORE = ProgramGraphStore()
_MEMORY_STORE = ResearchMemoryStore()
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


class RegisterMemoryFactRequest(CommandMetadata):
    fact: ResearchMemoryFactSpec


class CompactMemoryRequest(CommandMetadata):
    scope_node_id: str = Field(pattern=r"^(qst|prg|cmp)_[0-9a-f]{32}$")
    task_key: str = Field(pattern=r"^(\*|[A-Za-z0-9][A-Za-z0-9._:/-]{0,127})$")
    draft: MemorySummaryDraft
    parent_compaction_id: str | None = Field(default=None, pattern=r"^mcp_[0-9a-f]{32}$")


class BuildMemoryContextRequest(CommandMetadata):
    request: TaskContextRequest


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
    except ResearchMemoryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProgramGraphInvariantError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ResearchMemoryInvariantError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ProgramGraphTransitionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ProgramGraphConflict, ProgramGraphCycleError, ScientificIdempotencyConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ResearchMemoryConflict, ResearchMemoryStale) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ResearchMemoryContextOverflow as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
    return await _invoke(lambda: _STORE.add_dependency(request.dependency, _context(request, user)))


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
    return await _invoke(lambda: _STORE.bind_experiment(request.binding, _context(request, user)))


@router.post("/bindings/questions", response_model=GraphMutationReceipt)
async def bind_question(
    request: BindQuestionRequest,
    user: dict[str, Any] = Depends(require_access),
) -> GraphMutationReceipt:
    return await _invoke(lambda: _STORE.bind_question(request.binding, _context(request, user)))


@router.post("/allocations/data", response_model=GraphMutationReceipt)
async def allocate_data(
    request: AllocateDataRequest,
    user: dict[str, Any] = Depends(require_access),
) -> GraphMutationReceipt:
    return await _invoke(lambda: _STORE.allocate_data(request.allocation, _context(request, user)))


@router.post("/allocations/budgets", response_model=GraphMutationReceipt)
async def allocate_budget(
    request: AllocateBudgetRequest,
    user: dict[str, Any] = Depends(require_access),
) -> GraphMutationReceipt:
    return await _invoke(
        lambda: _STORE.allocate_budget(request.allocation, _context(request, user))
    )


@router.post("/memory/facts", response_model=MemoryMutationReceipt)
async def register_memory_fact(
    request: RegisterMemoryFactRequest,
    user: dict[str, Any] = Depends(require_access),
) -> MemoryMutationReceipt:
    return await _invoke(lambda: _MEMORY_STORE.register_fact(request.fact, _context(request, user)))


@router.post("/memory/compactions", response_model=MemoryMutationReceipt)
async def compact_memory(
    request: CompactMemoryRequest,
    user: dict[str, Any] = Depends(require_access),
) -> MemoryMutationReceipt:
    return await _invoke(
        lambda: _MEMORY_STORE.compact(
            scope_node_id=request.scope_node_id,
            task_key=request.task_key,
            draft=request.draft,
            parent_compaction_id=request.parent_compaction_id,
            context=_context(request, user),
        )
    )


@router.get("/memory/{scope_node_id}", response_model=ResearchMemorySnapshot)
async def rebuild_memory(
    scope_node_id: Annotated[str, Path(pattern=r"^(qst|prg|cmp)_[0-9a-f]{32}$")],
    task_key: Annotated[
        str,
        Query(pattern=r"^(\*|[A-Za-z0-9][A-Za-z0-9._:/-]{0,127})$"),
    ],
    _user: dict[str, Any] = Depends(require_access),
) -> ResearchMemorySnapshot:
    return await _invoke(lambda: _MEMORY_STORE.rebuild_memory(scope_node_id, task_key))


@router.get(
    "/memory/compactions/{compaction_id}/artifact",
    response_model=MemoryCompactionArtifact,
)
async def recover_memory_compaction(
    compaction_id: Annotated[str, Path(pattern=r"^mcp_[0-9a-f]{32}$")],
    _user: dict[str, Any] = Depends(require_access),
) -> MemoryCompactionArtifact:
    return await _invoke(lambda: _MEMORY_STORE.recover_compaction(compaction_id))


@router.post("/memory/contexts", response_model=TaskContextReceipt)
async def build_memory_context(
    request: BuildMemoryContextRequest,
    user: dict[str, Any] = Depends(require_access),
) -> TaskContextReceipt:
    return await _invoke(
        lambda: _MEMORY_STORE.build_task_context(
            request.request,
            _context(request, user),
        )
    )


@router.get("/memory/contexts/{context_receipt_id}", response_model=TaskContextReceipt)
async def load_memory_context(
    context_receipt_id: Annotated[str, Path(pattern=r"^mctx_[0-9a-f]{32}$")],
    _user: dict[str, Any] = Depends(require_access),
) -> TaskContextReceipt:
    return await _invoke(lambda: _MEMORY_STORE.load_task_context(context_receipt_id))


__all__ = ["router"]
