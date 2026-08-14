"""Immutable PostgreSQL persistence for F9 world-model snapshots.

The legacy K2 ``belief_states`` row remains mutable and untouched.  New F9 objects are append-only,
content addressed, and version linked.  A database view exposes K2 credences for read compatibility
without pretending that a binary Beta credence is already a competing-hypothesis posterior.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, insert as postgresql_insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from aletheia.db import Base, session_scope
from aletheia.epistemics.schemas import (
    Assumption,
    BeliefState,
    HypothesisVersion,
    LegacyK2BeliefView,
    Prediction,
    ResearchQuestion,
    WorldModelSnapshot,
)


class EpistemicPersistenceError(RuntimeError):
    """Persisted world-model state is absent, conflicting, or no longer self-validating."""


class ImmutableEpistemicConflict(EpistemicPersistenceError):
    """A stable lineage/version identity is already bound to different content."""


class EpistemicLineageError(EpistemicPersistenceError):
    """A revision skips, changes, or fails to name its exact immutable parent."""


class EpistemicObjectNotFound(EpistemicPersistenceError):
    """A requested world-model object does not exist."""


class EpistemicResearchQuestionRecord(Base):
    __tablename__ = "epistemic_research_questions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_epistemic_question_version_positive"),
        UniqueConstraint("question_id", "version", name="uq_epistemic_question_version"),
    )

    question_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    question_id: Mapped[str] = mapped_column(String(35), index=True)
    version: Mapped[int] = mapped_column(Integer)
    parent_question_sha256: Mapped[str | None] = mapped_column(
        ForeignKey("epistemic_research_questions.question_sha256"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)


class EpistemicHypothesisVersionRecord(Base):
    __tablename__ = "epistemic_hypothesis_versions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_epistemic_hypothesis_version_positive"),
        UniqueConstraint("hypothesis_id", "version", name="uq_epistemic_hypothesis_version"),
        Index(
            "ix_epistemic_hypothesis_question_role",
            "question_id",
            "role",
            "lifecycle",
        ),
    )

    hypothesis_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    question_id: Mapped[str] = mapped_column(String(35), index=True)
    question_sha256: Mapped[str] = mapped_column(
        ForeignKey("epistemic_research_questions.question_sha256"), index=True
    )
    hypothesis_id: Mapped[str] = mapped_column(String(36), index=True)
    version: Mapped[int] = mapped_column(Integer)
    parent_hypothesis_sha256: Mapped[str | None] = mapped_column(
        ForeignKey("epistemic_hypothesis_versions.hypothesis_sha256"), index=True
    )
    role: Mapped[str] = mapped_column(String(24))
    lifecycle: Mapped[str] = mapped_column(String(24))
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)


class EpistemicAssumptionRecord(Base):
    __tablename__ = "epistemic_assumptions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_epistemic_assumption_version_positive"),
        UniqueConstraint("assumption_id", "version", name="uq_epistemic_assumption_version"),
    )

    assumption_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    assumption_id: Mapped[str] = mapped_column(String(36), index=True)
    version: Mapped[int] = mapped_column(Integer)
    parent_assumption_sha256: Mapped[str | None] = mapped_column(
        ForeignKey("epistemic_assumptions.assumption_sha256"), index=True
    )
    hypothesis_id: Mapped[str] = mapped_column(String(36), index=True)
    hypothesis_sha256: Mapped[str] = mapped_column(
        ForeignKey("epistemic_hypothesis_versions.hypothesis_sha256"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    disposition: Mapped[str] = mapped_column(String(24))
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)


class EpistemicPredictionRecord(Base):
    __tablename__ = "epistemic_predictions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_epistemic_prediction_version_positive"),
        UniqueConstraint("prediction_id", "version", name="uq_epistemic_prediction_version"),
    )

    prediction_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    prediction_id: Mapped[str] = mapped_column(String(37), index=True)
    version: Mapped[int] = mapped_column(Integer)
    parent_prediction_sha256: Mapped[str | None] = mapped_column(
        ForeignKey("epistemic_predictions.prediction_sha256"), index=True
    )
    hypothesis_id: Mapped[str] = mapped_column(String(36), index=True)
    hypothesis_sha256: Mapped[str] = mapped_column(
        ForeignKey("epistemic_hypothesis_versions.hypothesis_sha256"), index=True
    )
    observable_id: Mapped[str] = mapped_column(String(512), index=True)
    direction: Mapped[str] = mapped_column(String(24))
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)


class EpistemicBeliefStateRecord(Base):
    __tablename__ = "epistemic_belief_states"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_epistemic_belief_version_positive"),
        UniqueConstraint(
            "belief_lineage_id", "version", name="uq_epistemic_belief_lineage_version"
        ),
    )

    belief_state_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    belief_lineage_id: Mapped[str] = mapped_column(String(36), index=True)
    version: Mapped[int] = mapped_column(Integer)
    parent_belief_state_sha256: Mapped[str | None] = mapped_column(
        ForeignKey("epistemic_belief_states.belief_state_sha256"), index=True
    )
    question_id: Mapped[str] = mapped_column(String(35), index=True)
    question_sha256: Mapped[str] = mapped_column(
        ForeignKey("epistemic_research_questions.question_sha256"), index=True
    )
    update_kind: Mapped[str] = mapped_column(String(32))
    source_observation_receipt_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    likelihood_model_sha256: Mapped[str | None] = mapped_column(String(64))
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)


class EpistemicBeliefStateMemberRecord(Base):
    __tablename__ = "epistemic_belief_state_members"
    __table_args__ = (
        CheckConstraint(
            "probability >= 0.0 AND probability <= 1.0",
            name="ck_epistemic_belief_member_probability",
        ),
        UniqueConstraint(
            "belief_state_sha256", "ordinal", name="uq_epistemic_belief_member_order"
        ),
        UniqueConstraint(
            "belief_state_sha256",
            "hypothesis_id",
            name="uq_epistemic_belief_member_lineage",
        ),
    )

    belief_state_sha256: Mapped[str] = mapped_column(
        ForeignKey("epistemic_belief_states.belief_state_sha256"), primary_key=True
    )
    hypothesis_sha256: Mapped[str] = mapped_column(
        ForeignKey("epistemic_hypothesis_versions.hypothesis_sha256"), primary_key=True
    )
    hypothesis_id: Mapped[str] = mapped_column(String(36), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    probability: Mapped[float] = mapped_column(Float)


class EpistemicWorldModelSnapshotRecord(Base):
    __tablename__ = "epistemic_world_model_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "question_sha256",
            "belief_state_sha256",
            name="uq_epistemic_world_model_question_belief",
        ),
    )

    snapshot_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    question_id: Mapped[str] = mapped_column(String(35), index=True)
    question_sha256: Mapped[str] = mapped_column(
        ForeignKey("epistemic_research_questions.question_sha256"), index=True
    )
    belief_state_sha256: Mapped[str] = mapped_column(
        ForeignKey("epistemic_belief_states.belief_state_sha256"), index=True
    )
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)


@dataclass(frozen=True)
class WorldModelStoreReceipt:
    snapshot_sha256: str
    created: bool


ModelT = TypeVar("ModelT", bound=BaseModel)
RecordT = TypeVar("RecordT")


def _payload(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _parse_stored(
    model_type: type[ModelT], payload: dict[str, Any], *, label: str
) -> ModelT:
    try:
        return model_type.model_validate(payload)
    except ValidationError as exc:
        raise EpistemicPersistenceError(f"persisted {label} no longer validates") from exc


def _insert_or_verify(
    session: Session,
    *,
    record_type: type[RecordT],
    content_key_name: str,
    content_key: str,
    identity_predicates: tuple[Any, ...],
    values: dict[str, Any],
    model_type: type[ModelT],
    model: ModelT,
    label: str,
) -> bool:
    inserted_key = session.scalar(
        postgresql_insert(record_type)
        .values(**values)
        .on_conflict_do_nothing()
        .returning(getattr(record_type, content_key_name))
    )
    session.flush()
    row = session.get(record_type, content_key)
    if row is None:
        existing = session.scalar(select(record_type).where(*identity_predicates))
        if existing is not None:
            raise ImmutableEpistemicConflict(
                f"{label} stable lineage/version is already bound to different content"
            )
        raise EpistemicPersistenceError(f"could not persist {label}")
    stored = _parse_stored(model_type, row.payload_json, label=label)
    if stored != model or getattr(row, content_key_name) != content_key:
        raise ImmutableEpistemicConflict(f"persisted {label} content identity conflicts")
    return inserted_key is not None


def _require_parent(
    session: Session,
    *,
    record_type: type[RecordT],
    parent_key: str | None,
    model_type: type[ModelT],
    current: ModelT,
    version: int,
    lineage_field: str,
    label: str,
) -> None:
    if version == 1:
        return
    parent = session.get(record_type, parent_key)
    if parent is None:
        raise EpistemicLineageError(f"{label} parent does not exist")
    previous = _parse_stored(model_type, parent.payload_json, label=f"{label} parent")
    if getattr(previous, lineage_field) != getattr(current, lineage_field):
        raise EpistemicLineageError(f"{label} parent belongs to a different stable lineage")
    if previous.version != version - 1:
        raise EpistemicLineageError(f"{label} versions must advance by exactly one")
    if previous.run_id != current.run_id:
        raise EpistemicLineageError(f"{label} parent belongs to a different run")
    if previous.frozen_at > current.frozen_at:
        raise EpistemicLineageError(f"{label} revision predates its parent")


def _store_question(session: Session, question: ResearchQuestion) -> bool:
    _require_parent(
        session,
        record_type=EpistemicResearchQuestionRecord,
        parent_key=question.parent_question_sha256,
        model_type=ResearchQuestion,
        current=question,
        version=question.version,
        lineage_field="question_id",
        label="research question",
    )
    return _insert_or_verify(
        session,
        record_type=EpistemicResearchQuestionRecord,
        content_key_name="question_sha256",
        content_key=question.question_sha256,
        identity_predicates=(
            EpistemicResearchQuestionRecord.question_id == question.question_id,
            EpistemicResearchQuestionRecord.version == question.version,
        ),
        values={
            "question_sha256": question.question_sha256,
            "run_id": question.run_id,
            "question_id": question.question_id,
            "version": question.version,
            "parent_question_sha256": question.parent_question_sha256,
            "kind": question.kind.value,
            "frozen_at": question.frozen_at,
            "payload_json": _payload(question),
        },
        model_type=ResearchQuestion,
        model=question,
        label="research question",
    )


def _require_question_binding(
    session: Session,
    *,
    question_sha256: str,
    question_id: str,
    run_id: str,
    label: str,
) -> ResearchQuestion:
    row = session.get(EpistemicResearchQuestionRecord, question_sha256)
    if row is None:
        raise EpistemicLineageError(f"{label} question version does not exist")
    question = _parse_stored(ResearchQuestion, row.payload_json, label="research question")
    if question.question_id != question_id or question.run_id != run_id:
        raise EpistemicLineageError(f"{label} changed its research-question lineage")
    return question


def _store_hypothesis(session: Session, hypothesis: HypothesisVersion) -> bool:
    _require_question_binding(
        session,
        question_sha256=hypothesis.question_version_sha256,
        question_id=hypothesis.question_id,
        run_id=hypothesis.run_id,
        label="hypothesis",
    )
    _require_parent(
        session,
        record_type=EpistemicHypothesisVersionRecord,
        parent_key=hypothesis.parent_hypothesis_sha256,
        model_type=HypothesisVersion,
        current=hypothesis,
        version=hypothesis.version,
        lineage_field="hypothesis_id",
        label="hypothesis",
    )
    if hypothesis.version > 1:
        parent_row = session.get(
            EpistemicHypothesisVersionRecord, hypothesis.parent_hypothesis_sha256
        )
        parent = _parse_stored(
            HypothesisVersion, parent_row.payload_json, label="hypothesis parent"
        )
        if parent.question_id != hypothesis.question_id:
            raise EpistemicLineageError("hypothesis revision changed its question lineage")
        if parent.role is not hypothesis.role:
            raise EpistemicLineageError("hypothesis revision changed its null/primary/alternative role")
    return _insert_or_verify(
        session,
        record_type=EpistemicHypothesisVersionRecord,
        content_key_name="hypothesis_sha256",
        content_key=hypothesis.hypothesis_sha256,
        identity_predicates=(
            EpistemicHypothesisVersionRecord.hypothesis_id == hypothesis.hypothesis_id,
            EpistemicHypothesisVersionRecord.version == hypothesis.version,
        ),
        values={
            "hypothesis_sha256": hypothesis.hypothesis_sha256,
            "run_id": hypothesis.run_id,
            "question_id": hypothesis.question_id,
            "question_sha256": hypothesis.question_version_sha256,
            "hypothesis_id": hypothesis.hypothesis_id,
            "version": hypothesis.version,
            "parent_hypothesis_sha256": hypothesis.parent_hypothesis_sha256,
            "role": hypothesis.role.value,
            "lifecycle": hypothesis.lifecycle.value,
            "frozen_at": hypothesis.frozen_at,
            "payload_json": _payload(hypothesis),
        },
        model_type=HypothesisVersion,
        model=hypothesis,
        label="hypothesis",
    )


def _require_hypothesis_binding(
    session: Session,
    *,
    hypothesis_sha256: str,
    hypothesis_id: str,
    run_id: str,
    label: str,
) -> HypothesisVersion:
    row = session.get(EpistemicHypothesisVersionRecord, hypothesis_sha256)
    if row is None:
        raise EpistemicLineageError(f"{label} hypothesis version does not exist")
    hypothesis = _parse_stored(HypothesisVersion, row.payload_json, label="hypothesis")
    if hypothesis.hypothesis_id != hypothesis_id or hypothesis.run_id != run_id:
        raise EpistemicLineageError(f"{label} changed its hypothesis lineage")
    return hypothesis


def _store_assumption(session: Session, assumption: Assumption) -> bool:
    _require_hypothesis_binding(
        session,
        hypothesis_sha256=assumption.hypothesis_version_sha256,
        hypothesis_id=assumption.hypothesis_id,
        run_id=assumption.run_id,
        label="assumption",
    )
    _require_parent(
        session,
        record_type=EpistemicAssumptionRecord,
        parent_key=assumption.parent_assumption_sha256,
        model_type=Assumption,
        current=assumption,
        version=assumption.version,
        lineage_field="assumption_id",
        label="assumption",
    )
    if assumption.version > 1:
        parent_row = session.get(EpistemicAssumptionRecord, assumption.parent_assumption_sha256)
        parent = _parse_stored(Assumption, parent_row.payload_json, label="assumption parent")
        if parent.hypothesis_id != assumption.hypothesis_id:
            raise EpistemicLineageError("assumption revision changed its hypothesis lineage")
    return _insert_or_verify(
        session,
        record_type=EpistemicAssumptionRecord,
        content_key_name="assumption_sha256",
        content_key=assumption.assumption_sha256,
        identity_predicates=(
            EpistemicAssumptionRecord.assumption_id == assumption.assumption_id,
            EpistemicAssumptionRecord.version == assumption.version,
        ),
        values={
            "assumption_sha256": assumption.assumption_sha256,
            "run_id": assumption.run_id,
            "assumption_id": assumption.assumption_id,
            "version": assumption.version,
            "parent_assumption_sha256": assumption.parent_assumption_sha256,
            "hypothesis_id": assumption.hypothesis_id,
            "hypothesis_sha256": assumption.hypothesis_version_sha256,
            "kind": assumption.kind.value,
            "disposition": assumption.disposition.value,
            "frozen_at": assumption.frozen_at,
            "payload_json": _payload(assumption),
        },
        model_type=Assumption,
        model=assumption,
        label="assumption",
    )


def _store_prediction(session: Session, prediction: Prediction) -> bool:
    _require_hypothesis_binding(
        session,
        hypothesis_sha256=prediction.hypothesis_version_sha256,
        hypothesis_id=prediction.hypothesis_id,
        run_id=prediction.run_id,
        label="prediction",
    )
    _require_parent(
        session,
        record_type=EpistemicPredictionRecord,
        parent_key=prediction.parent_prediction_sha256,
        model_type=Prediction,
        current=prediction,
        version=prediction.version,
        lineage_field="prediction_id",
        label="prediction",
    )
    if prediction.version > 1:
        parent_row = session.get(EpistemicPredictionRecord, prediction.parent_prediction_sha256)
        parent = _parse_stored(Prediction, parent_row.payload_json, label="prediction parent")
        if parent.hypothesis_id != prediction.hypothesis_id:
            raise EpistemicLineageError("prediction revision changed its hypothesis lineage")
    return _insert_or_verify(
        session,
        record_type=EpistemicPredictionRecord,
        content_key_name="prediction_sha256",
        content_key=prediction.prediction_sha256,
        identity_predicates=(
            EpistemicPredictionRecord.prediction_id == prediction.prediction_id,
            EpistemicPredictionRecord.version == prediction.version,
        ),
        values={
            "prediction_sha256": prediction.prediction_sha256,
            "run_id": prediction.run_id,
            "prediction_id": prediction.prediction_id,
            "version": prediction.version,
            "parent_prediction_sha256": prediction.parent_prediction_sha256,
            "hypothesis_id": prediction.hypothesis_id,
            "hypothesis_sha256": prediction.hypothesis_version_sha256,
            "observable_id": prediction.observable_id,
            "direction": prediction.direction.value,
            "frozen_at": prediction.frozen_at,
            "payload_json": _payload(prediction),
        },
        model_type=Prediction,
        model=prediction,
        label="prediction",
    )


def _store_belief_state(session: Session, belief: BeliefState) -> bool:
    _require_question_binding(
        session,
        question_sha256=belief.question_version_sha256,
        question_id=belief.question_id,
        run_id=belief.run_id,
        label="belief state",
    )
    _require_parent(
        session,
        record_type=EpistemicBeliefStateRecord,
        parent_key=belief.parent_belief_state_sha256,
        model_type=BeliefState,
        current=belief,
        version=belief.version,
        lineage_field="belief_lineage_id",
        label="belief state",
    )
    if belief.version > 1:
        parent_row = session.get(EpistemicBeliefStateRecord, belief.parent_belief_state_sha256)
        parent = _parse_stored(BeliefState, parent_row.payload_json, label="belief-state parent")
        if parent.question_id != belief.question_id:
            raise EpistemicLineageError("belief-state revision changed its question lineage")

    created = _insert_or_verify(
        session,
        record_type=EpistemicBeliefStateRecord,
        content_key_name="belief_state_sha256",
        content_key=belief.belief_state_sha256,
        identity_predicates=(
            EpistemicBeliefStateRecord.belief_lineage_id == belief.belief_lineage_id,
            EpistemicBeliefStateRecord.version == belief.version,
        ),
        values={
            "belief_state_sha256": belief.belief_state_sha256,
            "run_id": belief.run_id,
            "belief_lineage_id": belief.belief_lineage_id,
            "version": belief.version,
            "parent_belief_state_sha256": belief.parent_belief_state_sha256,
            "question_id": belief.question_id,
            "question_sha256": belief.question_version_sha256,
            "update_kind": belief.update_kind.value,
            "source_observation_receipt_sha256": belief.source_observation_receipt_sha256,
            "likelihood_model_sha256": belief.likelihood_model_sha256,
            "frozen_at": belief.frozen_at,
            "payload_json": _payload(belief),
        },
        model_type=BeliefState,
        model=belief,
        label="belief state",
    )

    for ordinal, member in enumerate(belief.hypotheses):
        _require_hypothesis_binding(
            session,
            hypothesis_sha256=member.hypothesis_version_sha256,
            hypothesis_id=member.hypothesis_id,
            run_id=belief.run_id,
            label="belief-state member",
        )
        result = session.execute(
            postgresql_insert(EpistemicBeliefStateMemberRecord)
            .values(
                belief_state_sha256=belief.belief_state_sha256,
                hypothesis_sha256=member.hypothesis_version_sha256,
                hypothesis_id=member.hypothesis_id,
                ordinal=ordinal,
                probability=member.probability,
            )
            .on_conflict_do_nothing()
        )
        session.flush()
        persisted = session.get(
            EpistemicBeliefStateMemberRecord,
            (belief.belief_state_sha256, member.hypothesis_version_sha256),
        )
        if (
            persisted is None
            or persisted.hypothesis_id != member.hypothesis_id
            or persisted.ordinal != ordinal
            or persisted.probability != member.probability
        ):
            raise ImmutableEpistemicConflict("belief-state membership conflicts")
        del result
    return created


def store_world_model_snapshot(snapshot: WorldModelSnapshot) -> WorldModelStoreReceipt:
    """Atomically store a closed immutable snapshot; identical retries are idempotent."""

    with session_scope() as session:
        _store_question(session, snapshot.question)
        for hypothesis in snapshot.hypotheses:
            _store_hypothesis(session, hypothesis)
        for assumption in snapshot.assumptions:
            _store_assumption(session, assumption)
        for prediction in snapshot.predictions:
            _store_prediction(session, prediction)
        _store_belief_state(session, snapshot.belief_state)
        created = _insert_or_verify(
            session,
            record_type=EpistemicWorldModelSnapshotRecord,
            content_key_name="snapshot_sha256",
            content_key=snapshot.snapshot_sha256,
            identity_predicates=(
                EpistemicWorldModelSnapshotRecord.question_sha256
                == snapshot.question.question_sha256,
                EpistemicWorldModelSnapshotRecord.belief_state_sha256
                == snapshot.belief_state.belief_state_sha256,
            ),
            values={
                "snapshot_sha256": snapshot.snapshot_sha256,
                "run_id": snapshot.question.run_id,
                "question_id": snapshot.question.question_id,
                "question_sha256": snapshot.question.question_sha256,
                "belief_state_sha256": snapshot.belief_state.belief_state_sha256,
                "frozen_at": snapshot.frozen_at,
                "payload_json": _payload(snapshot),
            },
            model_type=WorldModelSnapshot,
            model=snapshot,
            label="world-model snapshot",
        )
    return WorldModelStoreReceipt(snapshot_sha256=snapshot.snapshot_sha256, created=created)


def _require_exact_row(
    session: Session,
    *,
    record_type: type[RecordT],
    key: str,
    model_type: type[ModelT],
    expected: ModelT,
    hash_attribute: str,
    label: str,
) -> None:
    row = session.get(record_type, key)
    if row is None:
        raise EpistemicPersistenceError(f"world-model snapshot references missing {label}")
    stored = _parse_stored(model_type, row.payload_json, label=label)
    if stored != expected or getattr(stored, hash_attribute) != key:
        raise ImmutableEpistemicConflict(f"world-model snapshot {label} binding conflicts")


def get_world_model_snapshot(snapshot_sha256: str) -> WorldModelSnapshot:
    """Load and mechanically revalidate a snapshot and every referenced immutable row."""

    with session_scope() as session:
        row = session.get(EpistemicWorldModelSnapshotRecord, snapshot_sha256)
        if row is None:
            raise EpistemicObjectNotFound(f"world-model snapshot not found: {snapshot_sha256}")
        snapshot = _parse_stored(
            WorldModelSnapshot, row.payload_json, label="world-model snapshot"
        )
        if snapshot.snapshot_sha256 != snapshot_sha256:
            raise ImmutableEpistemicConflict("world-model snapshot SHA-256 no longer matches payload")
        _require_exact_row(
            session,
            record_type=EpistemicResearchQuestionRecord,
            key=snapshot.question.question_sha256,
            model_type=ResearchQuestion,
            expected=snapshot.question,
            hash_attribute="question_sha256",
            label="research question",
        )
        for hypothesis in snapshot.hypotheses:
            _require_exact_row(
                session,
                record_type=EpistemicHypothesisVersionRecord,
                key=hypothesis.hypothesis_sha256,
                model_type=HypothesisVersion,
                expected=hypothesis,
                hash_attribute="hypothesis_sha256",
                label="hypothesis",
            )
        for assumption in snapshot.assumptions:
            _require_exact_row(
                session,
                record_type=EpistemicAssumptionRecord,
                key=assumption.assumption_sha256,
                model_type=Assumption,
                expected=assumption,
                hash_attribute="assumption_sha256",
                label="assumption",
            )
        for prediction in snapshot.predictions:
            _require_exact_row(
                session,
                record_type=EpistemicPredictionRecord,
                key=prediction.prediction_sha256,
                model_type=Prediction,
                expected=prediction,
                hash_attribute="prediction_sha256",
                label="prediction",
            )
        _require_exact_row(
            session,
            record_type=EpistemicBeliefStateRecord,
            key=snapshot.belief_state.belief_state_sha256,
            model_type=BeliefState,
            expected=snapshot.belief_state,
            hash_attribute="belief_state_sha256",
            label="belief state",
        )
        members = session.scalars(
            select(EpistemicBeliefStateMemberRecord)
            .where(
                EpistemicBeliefStateMemberRecord.belief_state_sha256
                == snapshot.belief_state.belief_state_sha256
            )
            .order_by(EpistemicBeliefStateMemberRecord.ordinal)
        ).all()
        expected_members = snapshot.belief_state.hypotheses
        if len(members) != len(expected_members) or any(
            member.ordinal != ordinal
            or member.hypothesis_id != expected.hypothesis_id
            or member.hypothesis_sha256 != expected.hypothesis_version_sha256
            or member.probability != expected.probability
            for ordinal, (member, expected) in enumerate(zip(members, expected_members, strict=True))
        ):
            raise ImmutableEpistemicConflict("world-model belief membership no longer matches")
        return snapshot


def list_legacy_k2_belief_compat(run_id: str) -> list[LegacyK2BeliefView]:
    """Read K2 through the migration-owned compatibility view; never writes or backfills F9."""

    with session_scope() as session:
        rows = session.execute(
            text(
                """
                SELECT legacy_belief_state_id, belief_lineage_id, run_id, question_key,
                       alpha, beta, probability_holds, n_updates, updated_at, representation
                FROM k2_belief_state_compat
                WHERE run_id = :run_id
                ORDER BY updated_at, question_key
                """
            ),
            {"run_id": run_id},
        ).mappings()
        return [LegacyK2BeliefView.model_validate(dict(row)) for row in rows]


# Short operational aliases used by CLI/service callers.
persist_world_model_snapshot = store_world_model_snapshot
load_world_model_snapshot = get_world_model_snapshot
