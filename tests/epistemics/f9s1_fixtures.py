from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import aletheia.epistemics as e


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, seed: str) -> str:
    return f"{prefix}_{_digest(seed)[:32]}"


def _revalidate(model_type, model, **updates):
    payload = model.model_dump(mode="python")
    payload.update(updates)
    return model_type.model_validate(payload)


def build_world_model(
    run_id: str,
    *,
    identity_seed: str = "f9s1",
    content_variant: str = "initial",
) -> e.WorldModelSnapshot:
    base = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    principal = _digest(f"{identity_seed}:principal")
    question = e.ResearchQuestion(
        run_id=run_id,
        question_id=_stable_id("rq", f"{identity_seed}:question"),
        version=1,
        kind=e.ResearchQuestionKind.MECHANISM,
        statement=f"What mechanism produces the measured response? [{content_variant}]",
        scope_sha256=_digest(f"{identity_seed}:scope"),
        author_principal_sha256=principal,
        frozen_at=base,
    )

    specifications = (
        (
            "null",
            e.HypothesisRole.NULL,
            "The response is indistinguishable from measurement noise.",
            None,
        ),
        (
            "primary",
            e.HypothesisRole.PRIMARY,
            "The intervention changes the response through mechanism A.",
            "Intervention activates mediator A, which changes the measured endpoint.",
        ),
        (
            "alternative",
            e.HypothesisRole.ALTERNATIVE,
            "The apparent response is produced by batch-dependent mechanism B.",
            "Batch composition changes mediator B and mimics the intervention response.",
        ),
    )
    hypotheses = tuple(
        sorted(
            (
                e.HypothesisVersion(
                    run_id=run_id,
                    question_id=question.question_id,
                    question_version_sha256=question.question_sha256,
                    hypothesis_id=_stable_id("hyp", f"{identity_seed}:{label}"),
                    version=1,
                    role=role,
                    lifecycle=e.HypothesisLifecycle.ACTIVE,
                    statement=f"{statement} [{content_variant}]",
                    mechanism=mechanism,
                    rationale_sha256=_digest(f"{identity_seed}:{label}:rationale:{content_variant}"),
                    author_principal_sha256=principal,
                    frozen_at=base + timedelta(minutes=1),
                )
                for label, role, statement, mechanism in specifications
            ),
            key=lambda item: item.hypothesis_id,
        )
    )
    probability_by_role = {
        e.HypothesisRole.NULL: 0.2,
        e.HypothesisRole.PRIMARY: 0.5,
        e.HypothesisRole.ALTERNATIVE: 0.3,
    }

    assumptions = tuple(
        sorted(
            (
                e.Assumption(
                    run_id=run_id,
                    assumption_id=_stable_id(
                        "asm", f"{identity_seed}:{hypothesis.hypothesis_id}:assumption"
                    ),
                    version=1,
                    hypothesis_id=hypothesis.hypothesis_id,
                    hypothesis_version_sha256=hypothesis.hypothesis_sha256,
                    kind=e.AssumptionKind.MEASUREMENT,
                    statement=f"The endpoint measures the proposed process. [{content_variant}]",
                    risk_if_violated="The observed pattern cannot identify this mechanism.",
                    author_principal_sha256=principal,
                    frozen_at=base + timedelta(minutes=2),
                )
                for hypothesis in hypotheses
            ),
            key=lambda item: item.assumption_id,
        )
    )
    hypothesis_ids = {item.hypothesis_id for item in hypotheses}
    predictions = tuple(
        sorted(
            (
                e.Prediction(
                    run_id=run_id,
                    prediction_id=_stable_id(
                        "pred", f"{identity_seed}:{hypothesis.hypothesis_id}:prediction"
                    ),
                    version=1,
                    hypothesis_id=hypothesis.hypothesis_id,
                    hypothesis_version_sha256=hypothesis.hypothesis_sha256,
                    observable_id="endpoint.response",
                    outcome_space=("effect_absent", "effect_present"),
                    expected_outcome=(
                        "effect_absent"
                        if hypothesis.role is e.HypothesisRole.NULL
                        else "effect_present"
                    ),
                    direction=(
                        e.PredictionDirection.NO_CHANGE
                        if hypothesis.role is e.HypothesisRole.NULL
                        else e.PredictionDirection.INCREASE
                    ),
                    discriminates_from_hypothesis_ids=tuple(
                        sorted(hypothesis_ids - {hypothesis.hypothesis_id})
                    ),
                    measurement_protocol_sha256=_digest(
                        f"{identity_seed}:measurement:{content_variant}"
                    ),
                    author_principal_sha256=principal,
                    frozen_at=base + timedelta(minutes=3),
                )
                for hypothesis in hypotheses
            ),
            key=lambda item: item.prediction_id,
        )
    )
    belief = e.BeliefState(
        run_id=run_id,
        belief_lineage_id=_stable_id("blf", f"{identity_seed}:belief"),
        version=1,
        question_id=question.question_id,
        question_version_sha256=question.question_sha256,
        hypotheses=tuple(
            e.HypothesisBelief(
                hypothesis_id=hypothesis.hypothesis_id,
                hypothesis_version_sha256=hypothesis.hypothesis_sha256,
                probability=probability_by_role[hypothesis.role],
            )
            for hypothesis in hypotheses
        ),
        update_kind=e.BeliefUpdateKind.PRIOR,
        author_principal_sha256=principal,
        frozen_at=base + timedelta(minutes=4),
    )
    return e.WorldModelSnapshot(
        question=question,
        hypotheses=hypotheses,
        assumptions=assumptions,
        predictions=predictions,
        belief_state=belief,
        frozen_at=base + timedelta(minutes=5),
    )


def revise_primary_hypothesis(
    previous: e.WorldModelSnapshot,
    *,
    version: int = 2,
    parent_override: str | None = None,
) -> e.WorldModelSnapshot:
    moment = previous.frozen_at + timedelta(hours=1)
    primary_before = next(
        item for item in previous.hypotheses if item.role is e.HypothesisRole.PRIMARY
    )
    primary_after = _revalidate(
        e.HypothesisVersion,
        primary_before,
        version=version,
        parent_hypothesis_sha256=(
            primary_before.hypothesis_sha256 if parent_override is None else parent_override
        ),
        statement=f"{primary_before.statement} Narrowed to the preregistered operating range.",
        lifecycle=e.HypothesisLifecycle.NARROWED,
        frozen_at=moment,
    )
    hypotheses = tuple(
        sorted(
            (
                primary_after if item.hypothesis_id == primary_before.hypothesis_id else item
                for item in previous.hypotheses
            ),
            key=lambda item: item.hypothesis_id,
        )
    )

    assumptions = []
    for item in previous.assumptions:
        if item.hypothesis_id == primary_before.hypothesis_id:
            item = _revalidate(
                e.Assumption,
                item,
                version=version,
                parent_assumption_sha256=item.assumption_sha256,
                hypothesis_version_sha256=primary_after.hypothesis_sha256,
                statement=f"{item.statement} The operating range is now explicit.",
                frozen_at=moment + timedelta(minutes=1),
            )
        assumptions.append(item)

    predictions = []
    for item in previous.predictions:
        if item.hypothesis_id == primary_before.hypothesis_id:
            item = _revalidate(
                e.Prediction,
                item,
                version=version,
                parent_prediction_sha256=item.prediction_sha256,
                hypothesis_version_sha256=primary_after.hypothesis_sha256,
                frozen_at=moment + timedelta(minutes=2),
            )
        predictions.append(item)

    old_probability = {
        item.hypothesis_id: item.probability for item in previous.belief_state.hypotheses
    }
    belief = _revalidate(
        e.BeliefState,
        previous.belief_state,
        version=version,
        parent_belief_state_sha256=previous.belief_state.belief_state_sha256,
        hypotheses=tuple(
            e.HypothesisBelief(
                hypothesis_id=hypothesis.hypothesis_id,
                hypothesis_version_sha256=hypothesis.hypothesis_sha256,
                probability=old_probability[hypothesis.hypothesis_id],
            )
            for hypothesis in hypotheses
        ),
        update_kind=e.BeliefUpdateKind.HYPOTHESIS_REVISION,
        frozen_at=moment + timedelta(minutes=3),
    )
    return e.WorldModelSnapshot(
        question=previous.question,
        hypotheses=hypotheses,
        assumptions=tuple(sorted(assumptions, key=lambda item: item.assumption_id)),
        predictions=tuple(sorted(predictions, key=lambda item: item.prediction_id)),
        belief_state=belief,
        frozen_at=moment + timedelta(minutes=4),
    )
