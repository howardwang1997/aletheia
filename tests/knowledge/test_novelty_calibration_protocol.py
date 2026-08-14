from __future__ import annotations

import pytest
from pydantic import ValidationError

import aletheia.knowledge as k
from .f8s3_fixtures import sha
from .f8s5_fixtures import RECEIPT_KEY, build_f8s5_fixture
from .test_schema_spike import _time


@pytest.fixture(scope="module")
def fixture():
    return build_f8s5_fixture()


def _revalidate(model_type, model, **updates):
    raw = model.model_dump(mode="python")
    raw.update(updates)
    return model_type.model_validate(raw)


def test_suite_freezes_two_time_separated_splits_and_hidden_labels(fixture) -> None:
    suite = fixture["suite"]
    validation = tuple(
        case for case in suite.cases if case.split is k.NoveltyCalibrationSplit.VALIDATION
    )
    holdout = tuple(
        case for case in suite.cases if case.split is k.NoveltyCalibrationSplit.TEMPORAL_HOLDOUT
    )

    assert len(validation) == len(holdout) == 40
    assert max(case.temporal_cutoff for case in validation) < min(
        case.temporal_cutoff for case in holdout
    )
    assert "labels" not in suite.model_dump(mode="json")
    assert len(suite.labels_commitment_sha256) == 64
    assert suite.policy.holdout_labels_evaluator_only is True


def test_temporal_holdout_cannot_overlap_validation_cutoff(fixture) -> None:
    suite = fixture["suite"]
    cases = list(suite.cases)
    first_holdout = next(
        index
        for index, case in enumerate(cases)
        if case.split is k.NoveltyCalibrationSplit.TEMPORAL_HOLDOUT
    )
    cases[first_holdout] = _revalidate(
        k.NoveltyCalibrationCase,
        cases[first_holdout],
        temporal_cutoff=_time("2024-01-01T00:00:00Z"),
    )

    with pytest.raises(ValidationError, match="holdout cutoffs must follow"):
        _revalidate(k.NoveltyCalibrationSuite, suite, cases=tuple(cases))


def test_each_case_requires_base_first_and_three_frozen_perturbations(fixture) -> None:
    case = fixture["cases"][0]
    assert tuple(variant.kind for variant in case.variants) == (
        k.NoveltyPerturbationKind.BASE,
        k.NoveltyPerturbationKind.CLAIM_PARAPHRASE,
        k.NoveltyPerturbationKind.QUERY_SYNONYM,
    )

    with pytest.raises(ValidationError, match="base variant first"):
        _revalidate(
            k.NoveltyCalibrationCase,
            case,
            variants=(case.variants[1], case.variants[0], case.variants[2]),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("minimum_validation_cases", 39),
        ("minimum_temporal_holdout_cases", 39),
        ("minimum_known_non_novel_cases_per_split", 29),
        ("minimum_strong_novel_cases_per_split", 9),
        ("minimum_semantics_preserving_variants", 2),
        ("minimum_known_answer_recall_lower_bound", 0.79),
        ("minimum_seed_reference_recovery_lower_bound", 0.79),
        ("maximum_false_strong_novelty_upper_bound", 0.11),
        ("maximum_missed_strong_novelty_upper_bound", 0.26),
    ],
)
def test_policy_rejects_weaker_scientific_floors(fixture, field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _revalidate(k.NoveltyCalibrationPolicy, fixture["policy"], **{field: value})


def test_evaluator_is_exactly_versioned_and_has_no_tools(fixture) -> None:
    evaluator = fixture["evaluator_manifest"]
    assert evaluator.classification_policy_sha256 == k.NOVELTY_CLASSIFICATION_POLICY_SHA256
    assert evaluator.tool_names == ()

    with pytest.raises(ValidationError, match="another novelty classification policy"):
        _revalidate(
            k.CalibrationEvaluatorManifest,
            evaluator,
            classification_policy_sha256=sha("another-classifier"),
        )
    with pytest.raises(ValidationError, match="cannot receive tool authority"):
        _revalidate(
            k.CalibrationEvaluatorManifest,
            evaluator,
            tool_names=("web_search",),
        )


def test_candidate_authors_cannot_adjudicate_their_own_labels(fixture) -> None:
    labels = list(fixture["labels"])
    case = fixture["cases"][0]
    author = case.candidate_author_principal_sha256s[0]
    labels[0] = _revalidate(
        k.NoveltyCalibrationLabel,
        labels[0],
        expert_adjudicator_principal_sha256s=tuple(
            sorted((author, sha("independent-adjudicator")))
        ),
    )

    with pytest.raises(ValueError, match="authors cannot adjudicate"):
        k.build_novelty_calibration_suite(
            suite_id="author-conflict-suite",
            policy=fixture["policy"],
            system_manifest_sha256=fixture["system_manifest_sha256"],
            cases=fixture["cases"],
            labels=tuple(labels),
            holdout_custody_manifest_sha256=sha("author-conflict-custody"),
            sealed_at=fixture["suite"].sealed_at,
        )


def test_labels_must_be_frozen_before_suite_sealing(fixture) -> None:
    labels = list(fixture["labels"])
    labels[0] = _revalidate(
        k.NoveltyCalibrationLabel,
        labels[0],
        labeled_at=_time("2025-07-02T00:00:00Z"),
    )

    with pytest.raises(ValueError, match="not frozen before suite sealing"):
        k.build_novelty_calibration_suite(
            suite_id="late-label-suite",
            policy=fixture["policy"],
            system_manifest_sha256=fixture["system_manifest_sha256"],
            cases=fixture["cases"],
            labels=tuple(labels),
            holdout_custody_manifest_sha256=sha("late-label-custody"),
            sealed_at=fixture["suite"].sealed_at,
        )


@pytest.mark.parametrize(
    "relation,components,classification",
    [
        (k.PriorArtRelationType.EQUIVALENT, (), k.NoveltyClassification.KNOWN_EQUIVALENT),
        (
            k.PriorArtRelationType.SUBSUMES,
            (k.DifferenceComponent.CONDITION,),
            k.NoveltyClassification.KNOWN_SPECIAL_CASE,
        ),
        (
            k.PriorArtRelationType.SPECIAL_CASE,
            (k.DifferenceComponent.CONDITION,),
            k.NoveltyClassification.KNOWN_SPECIAL_CASE,
        ),
        (
            k.PriorArtRelationType.CONTRADICTION,
            (k.DifferenceComponent.RELATION,),
            k.NoveltyClassification.CONTRADICTORY_TO_PRIOR,
        ),
        (
            k.PriorArtRelationType.COMBINATION,
            (k.DifferenceComponent.METHOD,),
            k.NoveltyClassification.NOVEL_COMBINATION,
        ),
        (
            k.PriorArtRelationType.EXTENSION,
            (k.DifferenceComponent.METHOD,),
            k.NoveltyClassification.NOVEL_METHOD,
        ),
        (
            k.PriorArtRelationType.EXTENSION,
            (k.DifferenceComponent.OBJECT,),
            k.NoveltyClassification.NOVEL_PHENOMENON,
        ),
        (
            k.PriorArtRelationType.EXTENSION,
            (k.DifferenceComponent.CONDITION,),
            k.NoveltyClassification.INCREMENTAL_EXTENSION,
        ),
    ],
)
def test_classification_is_mechanically_derived_from_reviewed_relation_views(
    fixture,
    relation: k.PriorArtRelationType,
    components: tuple[k.DifferenceComponent, ...],
    classification: k.NoveltyClassification,
) -> None:
    receipt = fixture["receipts"][0]
    view = k.CalibrationRelationView(
        rank=1,
        prior_claim_sha256=sha(f"prior:{relation.value}"),
        relation=relation,
        difference_components=components,
        relation_sha256=sha(f"relation:{relation.value}"),
    )
    payload = receipt.payload.model_dump(mode="python")
    payload.update(
        relations=(view,),
        predicted_classification=classification,
    )

    assert (
        k.CalibrationTrialPayload.model_validate(payload).predicted_classification is classification
    )


def test_receipt_signing_rejects_short_keys(fixture) -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        k.SignedCalibrationTrialReceipt.sign(
            payload=fixture["receipts"][0].payload,
            key_id=fixture["evaluator_manifest"].receipt_key_id,
            key=b"too-short",
        )
    fixture["receipts"][0].verify(
        key=RECEIPT_KEY,
        expected_key_id=fixture["evaluator_manifest"].receipt_key_id,
    )
