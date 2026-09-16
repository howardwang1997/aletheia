"""Contract tests for the ARL-2 campaign bundle replay and grading adapters (PR B8)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from aletheia.execution.cuprate.exact_content import combined_outcome_bin_id
from aletheia.protocols.data_registration import (
    DatasetColumnManifestV1,
    DatasetLicenseStatus,
    DatasetPreprocessingPolicyV1,
    DatasetRoundSplitPolicyV1,
    DatasetSourceLineageV1,
    DatasetStratumRuleV1,
    RegisteredDatasetV1,
    enumerate_formula_groups,
    partition_group_rounds,
    recompute_dataset_audit,
)
from aletheia.protocols.schemas import AnalysisPlan
from aletheia.research_controller.campaign_replay import (
    DATASET_CARD_KIND,
    DATASET_CONTENT_KIND,
    KERNEL_EVENT_SCORE_TABLE,
    KERNEL_STREAM_KIND,
    ROUND_SPLIT_POLICY_KIND_PREFIX,
    SCORED_EVENT_VOCABULARY,
    SELECTION_RECEIPT_KIND_PREFIX,
    STAGED_ROUND_ROWS_KIND_PREFIX,
    CampaignBundleEntryV1,
    CampaignBundleManifestV1,
    RoundGradeRecordV1,
    StagedRowV1,
    StagedRoundRowsV1,
    build_round_split_state,
    campaign_spent_groups,
    four_way_disposition,
    kernel_events_to_score_k2_tables,
    kernel_stream_bytes,
    observation_to_classify_demo,
    verify_campaign_bundle,
)
from aletheia.research_controller.experiment_selection import (
    SELECTION_RULE_SHA256,
    CandidateSelectionRecord,
    ExperimentSelectionReceipt,
    SelectionPolicy,
    SplitState,
)
from aletheia.research_controller.external_rpc import (
    CuprateDiagnosticResult,
    CuprateDopingStratificationOutcome,
    CuprateMatchedControlOutcome,
)
from aletheia.research_controller.protocol_compilation_step import (
    RoundSplitBindingPolicyV1,
    RoundSplitTemplateBindingV1,
)
from aletheia.research_kernel.schemas import (
    ActionAuthorizedPayload,
    ActionProposedPayload,
    CharterActivatedPayload,
    EventType,
    KernelObjectKind,
    KernelObjectRef,
    ObservationIncorporatedPayload,
    RefineCommittedPayload,
    RefineDirective,
    ResearchEvent,
    StopCommittedPayload,
    StopDirective,
    StopReason,
    TransitionDecision,
    canonical_json_bytes,
    canonical_sha256,
)
from aletheia.scheduler.k2_acceptance import score_k2
from aletheia.scheduler.outcome import (
    REASON_DID_NOT_GENERALIZE,
    REASON_GENERALIZED,
    classify_outcome,
)

QUEST_ID = "qst_" + "1" * 32
BRANCH = "rbr_" + "2" * 32
CHILD = "rbr_" + "3" * 32
SLOT_ONE = "sos_" + "4" * 32
SLOT_TWO = "sos_" + "5" * 32
ACTION_ONE_ID = "act_round_one"
ACTION_TWO_ID = "act_round_two"
CHARTER_ID = "chr_fixture_2026_09"
PRINCIPAL = "principal:fixture-kernel"
QUESTION_KEY = "cuprate_plane_doping_h1"

T0 = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)
STEP = timedelta(minutes=5)
AS_OF = T0 + 8 * STEP

CSV_TEXT = (
    "material,critical_temp,feature_a\n"
    "Ba2Sr1Cu2O6,90.0,1\n"
    "Ba2Sr1Cu2O6,92.0,1\n"
    "Sr2Ca1Cu2O6,85.0,2\n"
    "LaCuO3,10.0,3\n"
    "Fe2O3,1.5,4\n"
    "Fe2O3,1.7,4\n"
    "SiO2,0.5,5\n"
)

ROWS_BY_GROUP = {
    "Ba2Sr1Cu2O6": (90.0, 92.0),
    "Sr2Ca1Cu2O6": (85.0,),
    "LaCuO3": (10.0,),
    "Fe2O3": (1.5, 1.7),
    "SiO2": (0.5,),
}

CUPRATE_RULE = DatasetStratumRuleV1(
    stratum_id="multi_ae_cuprate",
    required_elements=("Cu", "O"),
    multi_choice_elements=("Ba", "Ca", "Mg", "Sr"),
    multi_choice_minimum=2,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _set_sha(group_ids) -> str:
    return canonical_sha256(list(group_ids))


def _manifest() -> DatasetColumnManifestV1:
    return DatasetColumnManifestV1(
        columns=("critical_temp", "feature_a", "material"),
        composition_column="material",
        target_column="critical_temp",
        feature_columns=("feature_a",),
    )


def _fixture_split_policy(dataset_sha: str, groups) -> DatasetRoundSplitPolicyV1:
    for index in range(40):
        policy = DatasetRoundSplitPolicyV1(holdout_percent=50, salt=f"arl2-replay-{index:04d}")
        round_one, round_two = partition_group_rounds(dataset_sha, policy, groups)
        if len(round_one) >= 2 and len(round_two) >= 1:
            return policy
    raise AssertionError("no fixture salt splits the five formula groups")


def _card(csv_bytes: bytes) -> RegisteredDatasetV1:
    csv_text = csv_bytes.decode("utf-8")
    content_sha256 = hashlib.sha256(csv_bytes).hexdigest()
    groups = enumerate_formula_groups(csv_text, _manifest())
    lineage = DatasetSourceLineageV1(
        name="synthetic replay fixture",
        url="https://example.invalid/synthetic.csv",
        retrieval_note="authored in-test",
        retrieved_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
        raw_filename="synthetic.csv",
        raw_sha256=content_sha256,
        raw_bytes=len(csv_bytes),
        citation="synthetic fixture; no external source",
        license_terms="CC0 (synthetic)",
        license_status=DatasetLicenseStatus.VERIFIED,
    )
    return RegisteredDatasetV1(
        dataset_id="synthetic-replay-fixture",
        version=1,
        lineage=lineage,
        content_sha256=content_sha256,
        row_count=7,
        column_manifest=_manifest(),
        preprocessing=DatasetPreprocessingPolicyV1(
            policy_version="1.0.0",
            statements=("no row-level preprocessing; registered content is the retrieved file",),
        ),
        audit_verdicts=recompute_dataset_audit(
            content_sha256=content_sha256,
            column_manifest=_manifest(),
            stratum_rules=(CUPRATE_RULE,),
            csv_text=csv_text,
        ),
        stratum_rules=(CUPRATE_RULE,),
        split_policy=_fixture_split_policy(content_sha256, groups),
        limitations=("synthetic fixture for contract tests",),
    )


def _ref(kind: KernelObjectKind, object_id: str, sha_label: str) -> KernelObjectRef:
    return KernelObjectRef(
        object_kind=kind,
        object_id=object_id,
        object_sha256=_sha(sha_label),
        quest_id=QUEST_ID,
    )


CHARTER_REF = _ref(KernelObjectKind.CHARTER, CHARTER_ID, "charter")
ACTION_ONE_REF = _ref(KernelObjectKind.ACTION, ACTION_ONE_ID, "action-one")
ACTION_TWO_REF = _ref(KernelObjectKind.ACTION, ACTION_TWO_ID, "action-two")


def _event(sequence: int, event_type: EventType, payload, committed_at: datetime) -> ResearchEvent:
    return ResearchEvent(
        quest_id=QUEST_ID,
        sequence=sequence,
        parent_event_sha256=None if sequence == 1 else _sha(f"parent-{sequence - 1}"),
        event_type=event_type,
        payload=payload,
        command_sha256=_sha(f"command-{sequence}"),
        principal_id=PRINCIPAL,
        authorization_receipt_sha256=_sha(f"authorization-{sequence}"),
        committed_at=committed_at,
    )


def _observation(
    branch_id: str, action_id: str, slot: str, label: str
) -> ObservationIncorporatedPayload:
    return ObservationIncorporatedPayload(
        branch_id=branch_id,
        action_id=action_id,
        scientific_slot_id=slot,
        committed_admission_sha256=_sha(f"admission-{label}"),
        scientific_observation_sha256=_sha(f"observation-{label}"),
        outcome="negative",
        source_world_model_sha256=_sha("world-model"),
    )


def _decision(
    transition_id: str, directive, selected: KernelObjectRef, at: datetime
) -> TransitionDecision:
    return TransitionDecision(
        transition_id=transition_id,
        quest_id=QUEST_ID,
        charter_ref=CHARTER_REF,
        source_graph_sha256=_sha("graph"),
        selected_action_ref=selected,
        directive=directive,
        budget_receipt_sha256=_sha("budget"),
        risk_receipt_sha256=_sha("risk"),
        policy_receipt_sha256=_sha("policy"),
        reason_codes=("fixture_transition",),
        rationale="fixture transition inside the replay bundle stream",
        decided_by_principal_id=PRINCIPAL,
        decided_at=at,
    )


def _stream() -> tuple[ResearchEvent, ...]:
    return (
        _event(
            1,
            EventType.CHARTER_ACTIVATED,
            CharterActivatedPayload(charter_ref=CHARTER_REF, root_branch_id=BRANCH),
            T0,
        ),
        _event(
            2,
            EventType.ACTION_PROPOSED,
            ActionProposedPayload(action_ref=ACTION_ONE_REF, branch_id=BRANCH),
            T0 + STEP,
        ),
        _event(
            3,
            EventType.ACTION_AUTHORIZED,
            ActionAuthorizedPayload(action_id=ACTION_ONE_ID, branch_id=BRANCH),
            T0 + 2 * STEP,
        ),
        _event(
            4,
            EventType.OBSERVATION_INCORPORATED,
            _observation(BRANCH, ACTION_ONE_ID, SLOT_ONE, "one"),
            T0 + 3 * STEP,
        ),
        _event(
            5,
            EventType.REFINE_COMMITTED,
            RefineCommittedPayload(
                decision=_decision(
                    "trn_refine_round_two",
                    RefineDirective(source_branch_id=BRANCH, child_branch_id=CHILD),
                    ACTION_ONE_REF,
                    T0 + 4 * STEP,
                )
            ),
            T0 + 4 * STEP,
        ),
        _event(
            6,
            EventType.ACTION_PROPOSED,
            ActionProposedPayload(action_ref=ACTION_TWO_REF, branch_id=CHILD),
            T0 + 5 * STEP,
        ),
        _event(
            7,
            EventType.ACTION_AUTHORIZED,
            ActionAuthorizedPayload(action_id=ACTION_TWO_ID, branch_id=CHILD),
            T0 + 6 * STEP,
        ),
        _event(
            8,
            EventType.OBSERVATION_INCORPORATED,
            _observation(CHILD, ACTION_TWO_ID, SLOT_TWO, "two"),
            T0 + 7 * STEP,
        ),
        _event(
            9,
            EventType.STOP_COMMITTED,
            StopCommittedPayload(
                decision=_decision(
                    "trn_stop_campaign",
                    StopDirective(
                        branch_id=CHILD,
                        stop_reason=StopReason.RELIABLE_NEGATIVE_RESULT,
                        reopen_conditions=("new_doping_family_registered",),
                    ),
                    ACTION_TWO_REF,
                    AS_OF,
                )
            ),
            AS_OF,
        ),
    )


def _rows_for_batch(batch) -> tuple[StagedRowV1, ...]:
    return tuple(
        StagedRowV1(composition=group, target=target)
        for group in batch
        for target in ROWS_BY_GROUP[group]
    )


def _receipt(round_index: int, unspent) -> ExperimentSelectionReceipt:
    protocol_sha256 = _sha(f"protocol-round-{round_index}")
    return ExperimentSelectionReceipt(
        selection_policy=SelectionPolicy(
            eig_floor=0.0, selection_rule_sha256=SELECTION_RULE_SHA256
        ),
        split_state=SplitState(round_index=round_index, unspent_group_ids=tuple(unspent)),
        world_model_sha256=_sha("world-model"),
        capability_catalog_sha256=_sha("capability-catalog"),
        candidate_records=(
            CandidateSelectionRecord(protocol_sha256=protocol_sha256, eig=0.5, eligible=True),
        ),
        chosen_protocol_sha256=protocol_sha256,
        chosen_eig=0.5,
        authored_by_principal_id=PRINCIPAL,
        authored_at=T0,
    )


def _fixture() -> SimpleNamespace:
    """The canonical two-round bundle parts: card, stream, batches, bindings, receipts."""

    csv_bytes = CSV_TEXT.encode("utf-8")
    card = _card(csv_bytes)
    events = _stream()
    groups = enumerate_formula_groups(CSV_TEXT, card.column_manifest)
    round_one, round_two = partition_group_rounds(card.content_sha256, card.split_policy, groups)
    assert round_one and round_two and not (set(round_one) & set(round_two))
    batch_one = round_one[:1]
    batch_two = round_two[:1]
    staged_rounds = (
        StagedRoundRowsV1(
            round_index=1,
            action_sha256=_sha("action-one"),
            request_sha256=_sha("request-one"),
            staged_artifact_sha256=_sha("staged-artifact-one"),
            rows=_rows_for_batch(batch_one),
        ),
        StagedRoundRowsV1(
            round_index=2,
            action_sha256=_sha("action-two"),
            request_sha256=_sha("request-two"),
            staged_artifact_sha256=_sha("staged-artifact-two"),
            rows=_rows_for_batch(batch_two),
        ),
    )
    bindings = (
        RoundSplitBindingPolicyV1(
            dataset_content_sha256=card.content_sha256,
            split_policy_sha256=card.split_policy.policy_sha256,
            round_index=1,
            sealed_group_ids_sha256=_set_sha(round_one),
            template_bindings=(
                RoundSplitTemplateBindingV1(
                    action_sha256=_sha("action-one"),
                    bound_batch_group_ids_sha256=_set_sha(batch_one),
                    spent_group_ids_sha256=_set_sha(()),
                    unspent_group_ids_sha256=_set_sha(round_one),
                ),
            ),
        ),
        RoundSplitBindingPolicyV1(
            dataset_content_sha256=card.content_sha256,
            split_policy_sha256=card.split_policy.policy_sha256,
            round_index=2,
            sealed_group_ids_sha256=_set_sha(round_two),
            template_bindings=(
                RoundSplitTemplateBindingV1(
                    action_sha256=_sha("action-two"),
                    bound_batch_group_ids_sha256=_set_sha(batch_two),
                    spent_group_ids_sha256=_set_sha(batch_one),
                    unspent_group_ids_sha256=_set_sha(round_two),
                ),
            ),
        ),
    )
    receipts = (
        _receipt(1, round_one),
        _receipt(2, round_two),
    )
    return SimpleNamespace(
        csv_bytes=csv_bytes,
        card=card,
        events=events,
        round_one=round_one,
        round_two=round_two,
        batch_one=batch_one,
        batch_two=batch_two,
        staged_rounds=staged_rounds,
        bindings=bindings,
        receipts=receipts,
    )


def _bundle(
    fx: SimpleNamespace,
    *,
    staged_rounds=None,
    bindings=None,
    receipts=None,
) -> tuple[CampaignBundleManifestV1, dict[str, bytes]]:
    staged_rounds = fx.staged_rounds if staged_rounds is None else staged_rounds
    bindings = fx.bindings if bindings is None else bindings
    receipts = fx.receipts if receipts is None else receipts
    objects: dict[str, bytes] = {}
    entries: list[CampaignBundleEntryV1] = []

    def add(kind: str, payload: bytes, canonical: bool) -> None:
        sha = hashlib.sha256(payload).hexdigest()
        objects[sha] = payload
        entries.append(
            CampaignBundleEntryV1(
                object_kind=kind,
                object_sha256=sha,
                byte_length=len(payload),
                canonical_json=canonical,
            )
        )

    add(DATASET_CARD_KIND, canonical_json_bytes(fx.card), True)
    add(DATASET_CONTENT_KIND, fx.csv_bytes, False)
    add(KERNEL_STREAM_KIND, kernel_stream_bytes(fx.events), True)
    for binding in bindings:
        kind = f"{ROUND_SPLIT_POLICY_KIND_PREFIX}round-{binding.round_index:03d}"
        add(kind, canonical_json_bytes(binding), True)
    for staged in staged_rounds:
        kind = f"{STAGED_ROUND_ROWS_KIND_PREFIX}round-{staged.round_index:03d}"
        add(kind, canonical_json_bytes(staged), True)
    for receipt in receipts:
        kind = f"{SELECTION_RECEIPT_KIND_PREFIX}round-{receipt.split_state.round_index:03d}"
        add(kind, canonical_json_bytes(receipt), True)
    manifest = CampaignBundleManifestV1(
        quest_id=QUEST_ID,
        as_of=AS_OF,
        entries=tuple(sorted(entries, key=lambda item: item.object_kind)),
    )
    return manifest, objects


def _verify(fx, manifest, objects, **overrides):
    kwargs = dict(
        card=fx.card,
        csv_bytes=fx.csv_bytes,
        events=fx.events,
        round_split_bindings=fx.bindings,
        staged_rounds=fx.staged_rounds,
        manifest=manifest,
        objects=objects,
    )
    kwargs.update(overrides)
    return verify_campaign_bundle(**kwargs)


def test_bundle_round_trip_rederives_the_campaign() -> None:
    fx = _fixture()
    manifest, objects = _bundle(fx)
    report = _verify(fx, manifest, objects)

    assert report.manifest_sha256 == manifest.manifest_sha256
    assert report.dataset_content_sha256 == fx.card.content_sha256
    # rounds_observed is the root-branch count (count_rounds_observed): the
    # refine child's holdout observation is not a root-branch round
    assert report.rounds_observed == 1
    assert report.admitted_observation_events == 2
    assert report.terminal_stop_committed is True
    assert report.campaign_spent_group_ids == tuple(sorted(set(fx.batch_one) | set(fx.batch_two)))

    ledger_one, ledger_two = report.ledgers
    assert ledger_one.round_index == 1
    assert ledger_one.spent_group_ids == ()
    assert ledger_one.unspent_group_ids == fx.round_one
    assert ledger_one.bound_group_ids == tuple(fx.batch_one)
    assert ledger_one.admitted is True
    # round two pins the RAW ledger (round one's admitted batch) while its own
    # round-scoped projection stays the untouched second partition
    assert ledger_two.spent_group_ids == tuple(fx.batch_one)
    assert ledger_two.unspent_group_ids == fx.round_two
    assert ledger_two.admitted is True


def test_replay_rejects_a_card_that_is_not_the_pinned_one() -> None:
    fx = _fixture()
    manifest, objects = _bundle(fx)
    other_card = fx.card.model_copy(update={"limitations": ("tampered limitations",)})

    with pytest.raises(ValueError, match="not the bundle's pinned card"):
        _verify(fx, manifest, objects, card=other_card)


def test_replay_rejects_fabricated_staged_rows() -> None:
    fx = _fixture()
    fabricated = (
        fx.staged_rounds[0].model_copy(
            update={
                "rows": tuple(
                    sorted(
                        (*fx.staged_rounds[0].rows, StagedRowV1(composition="LaCuO3", target=99.0)),
                        key=lambda item: (item.composition, item.target),
                    )
                )
            }
        ),
        fx.staged_rounds[1],
    )
    manifest, objects = _bundle(fx, staged_rounds=fabricated)

    with pytest.raises(ValueError, match="pairs the registered card does not carry"):
        _verify(fx, manifest, objects, staged_rounds=fabricated)


def test_replay_rejects_a_batch_outside_its_own_round_partition() -> None:
    fx = _fixture()
    foreign_batch = fx.round_two[:1]
    staged = (
        StagedRoundRowsV1(
            round_index=1,
            action_sha256=_sha("action-one"),
            request_sha256=_sha("request-one"),
            staged_artifact_sha256=_sha("staged-artifact-one"),
            rows=_rows_for_batch(foreign_batch),
        ),
        fx.staged_rounds[1],
    )
    bindings = (
        fx.bindings[0].model_copy(
            update={
                "template_bindings": (
                    RoundSplitTemplateBindingV1(
                        action_sha256=_sha("action-one"),
                        bound_batch_group_ids_sha256=_set_sha(foreign_batch),
                        spent_group_ids_sha256=_set_sha(()),
                        unspent_group_ids_sha256=_set_sha(fx.round_one),
                    ),
                )
            }
        ),
        fx.bindings[1],
    )
    manifest, objects = _bundle(fx, staged_rounds=staged, bindings=bindings)

    with pytest.raises(ValueError):
        _verify(fx, manifest, objects, staged_rounds=staged, round_split_bindings=bindings)


def test_replay_rejects_a_spent_ledger_that_skips_round_one_admission() -> None:
    fx = _fixture()
    bindings = (
        fx.bindings[0],
        fx.bindings[1].model_copy(
            update={
                "template_bindings": (
                    fx.bindings[1]
                    .template_bindings[0]
                    .model_copy(update={"spent_group_ids_sha256": _set_sha(())}),
                )
            }
        ),
    )
    manifest, objects = _bundle(fx, bindings=bindings)

    with pytest.raises(ValueError, match="spent set differs from the re-derived ledger"):
        _verify(fx, manifest, objects, round_split_bindings=bindings)


def test_replay_pins_the_stream_to_the_final_stop_commitment() -> None:
    fx = _fixture()
    manifest, objects = _bundle(fx)
    late = manifest.model_copy(update={"as_of": AS_OF + STEP})

    with pytest.raises(ValueError, match="as_of pin is not the final stop commitment time"):
        _verify(fx, late, objects)


def test_replay_rejects_a_selection_receipt_that_disagrees() -> None:
    fx = _fixture()
    wrong = (_receipt(1, fx.round_one[1:]), fx.receipts[1])
    manifest, objects = _bundle(fx, receipts=wrong)

    with pytest.raises(ValueError, match="selection receipt disagrees"):
        _verify(fx, manifest, objects)


def test_round_split_state_subtracts_only_the_rounds_own_partition() -> None:
    fx = _fixture()
    first = build_round_split_state(
        card=fx.card, csv_bytes=fx.csv_bytes, round_index=1, spent_group_ids=()
    )
    assert first.unspent_group_ids == fx.round_one

    # round one's admitted batch belongs to the first partition; spending it
    # cannot shrink the second round's unspent set
    second = build_round_split_state(
        card=fx.card,
        csv_bytes=fx.csv_bytes,
        round_index=2,
        spent_group_ids=tuple(fx.batch_one),
    )
    assert second.unspent_group_ids == fx.round_two

    with pytest.raises(ValueError, match="canonically ordered"):
        build_round_split_state(
            card=fx.card,
            csv_bytes=fx.csv_bytes,
            round_index=1,
            spent_group_ids=(fx.round_one[1], fx.round_one[1]),
        )


def test_campaign_spent_groups_counts_only_admitted_batches() -> None:
    fx = _fixture()
    assert campaign_spent_groups(events=fx.events, staged_rounds=fx.staged_rounds) == tuple(
        sorted(set(fx.batch_one) | set(fx.batch_two))
    )
    without_round_two_admission = tuple(event for event in fx.events if event.sequence != 8)
    assert campaign_spent_groups(
        events=without_round_two_admission, staged_rounds=fx.staged_rounds
    ) == tuple(sorted(fx.batch_one))


def test_score_table_covers_every_kernel_event_type() -> None:
    table = dict(KERNEL_EVENT_SCORE_TABLE)
    assert set(table) == {item for item in EventType}
    assert {name for name in table.values() if name is not None} <= SCORED_EVENT_VOCABULARY


SUPPORTED_IF = {"op": ">", "threshold": 10.0}

_D1_EXCLUDES = "family_excess_ci_excludes_zero"
_D1_INCLUDES = "family_excess_ci_includes_zero"
_D2_CONCENTRATES = "error_concentrates_at_extremes"
_D2_NONE = "no_stratification_structure"


def _plan() -> AnalysisPlan:
    return AnalysisPlan(
        primary_endpoint_sha256s=(_sha("primary-endpoint"),),
        estimator_or_likelihood_sha256=_sha("estimator"),
        sample_size_or_precision_rule_sha256=_sha("sample-size"),
        missingness_rule_sha256=_sha("missingness"),
        exclusion_rule_sha256=_sha("exclusion"),
        multiplicity_rule_sha256=_sha("multiplicity"),
        stopping_rule_sha256=_sha("stopping"),
        futility_rule_sha256=_sha("futility"),
        positive_decision_rule_sha256=canonical_sha256(SUPPORTED_IF),
        negative_decision_rule_sha256=_sha("negative-rule"),
        inconclusive_decision_rule_sha256=_sha("inconclusive-rule"),
        frozen_before_observation=True,
        preregistration_seal_sha256=_sha("seal"),
    )


def _cuprate_result(d1_bin: str, d2_bin: str) -> CuprateDiagnosticResult:
    return CuprateDiagnosticResult(
        dataset_content_sha256=_sha("cuprate-content"),
        doping_optimum=0.15,
        analyzed_rows=48,
        dropped_off_batch_rows=2,
        d1_matched_control=CuprateMatchedControlOutcome(
            mae_cuprate=9.0,
            mae_matched=6.0,
            mae_non_all=6.5,
            excess_over_matched=3.0,
            ci=(0.4, 5.6) if d1_bin == _D1_EXCLUDES else (-1.2, 4.2),
            survives=True,
            n_cuprate=12,
            outcome_bin=d1_bin,
        ),
        d2_doping_stratification=CuprateDopingStratificationOutcome(
            family_holdout_rows=16,
            deviation_threshold=0.05,
            effect=1.8,
            ctrl_p95=0.9,
            concentrates=d2_bin == _D2_CONCENTRATES,
            outcome_bin=d2_bin,
        ),
    )


#: (d1 bin, d2 bin, four-way disposition, classify_outcome reason)
FOUR_WAY = (
    (_D1_EXCLUDES, _D2_CONCENTRATES, "h1_direction_supported", REASON_GENERALIZED),
    (_D1_EXCLUDES, _D2_NONE, "family_excess_without_doping_structure", REASON_DID_NOT_GENERALIZE),
    (_D1_INCLUDES, _D2_CONCENTRATES, "structure_without_family_excess", REASON_DID_NOT_GENERALIZE),
    (_D1_INCLUDES, _D2_NONE, "h2_null", REASON_DID_NOT_GENERALIZE),
)


def _demo(d1_bin: str, d2_bin: str) -> dict:
    return observation_to_classify_demo(
        result=_cuprate_result(d1_bin, d2_bin),
        plan=_plan(),
        supported_if=SUPPORTED_IF,
        n_confirm=1,
    )


@pytest.mark.parametrize(("d1_bin", "d2_bin", "disposition", "reason"), FOUR_WAY)
def test_demo_adapter_maps_the_envelope_mechanically(d1_bin, d2_bin, disposition, reason) -> None:
    result = _cuprate_result(d1_bin, d2_bin)
    demo = _demo(d1_bin, d2_bin)

    assert demo["holds"] is (d1_bin == _D1_EXCLUDES and d2_bin == _D2_CONCENTRATES)
    assert demo["detail"] == combined_outcome_bin_id(result)
    assert demo["test_statistic"] == 3.0
    assert demo["test_triggers"] is (d2_bin == _D2_CONCENTRATES)
    assert demo["control_silent"] is True
    assert demo["preregistration"]["supported_if"] == SUPPORTED_IF
    assert classify_outcome(demo)["reason"] == reason
    assert four_way_disposition(result) == disposition


def test_demo_adapter_requires_the_plans_pinned_rule() -> None:
    with pytest.raises(ValueError, match="pinned positive decision rule"):
        observation_to_classify_demo(
            result=_cuprate_result(_D1_EXCLUDES, _D2_CONCENTRATES),
            plan=_plan(),
            supported_if={"op": ">", "threshold": 1.0},
            n_confirm=1,
        )


def _grade_records(demo: dict) -> tuple[RoundGradeRecordV1, ...]:
    outcome = "positive" if demo["holds"] else "negative"
    return (
        RoundGradeRecordV1(
            round_index=1,
            action_id=ACTION_ONE_ID,
            question_key=QUESTION_KEY,
            outcome=outcome,
            demonstration=demo,
            predicted_p_holds=0.5,
            prior_alpha=1.0,
            prior_beta=1.0,
        ),
        RoundGradeRecordV1(
            round_index=2,
            action_id=ACTION_TWO_ID,
            question_key=QUESTION_KEY,
            outcome=outcome,
            demonstration=demo,
            predicted_p_holds=0.5,
            prior_alpha=1.0,
            prior_beta=1.0,
        ),
    )


@pytest.mark.parametrize(("d1_bin", "d2_bin", "disposition", "reason"), FOUR_WAY)
def test_adapted_two_round_stream_scores_full_in_every_disposition(
    d1_bin, d2_bin, disposition, reason
) -> None:
    fx = _fixture()
    demo = _demo(d1_bin, d2_bin)
    rows, credences = kernel_events_to_score_k2_tables(
        events=fx.events, grade_records=_grade_records(demo)
    )
    k2 = score_k2(rows, credences)

    # K2 grades the loop's completeness, not the hypothesis: every honest
    # disposition, negative ones included, reaches FULL
    assert k2.verdict == "full"
    graded_reasons = [row["payload"]["reason"] for row in rows if row["type"] == "campaign_reason"]
    assert graded_reasons == [reason, reason]
    updates = [row for row in rows if row["type"] == "belief_update"]
    assert len(updates) == 2
    assert updates[-1]["payload"]["n_updates"] == 2
    assert credences == [
        {
            "question_key": QUESTION_KEY,
            "alpha": 3.0 if demo["holds"] else 1.0,
            "beta": 1.0 if demo["holds"] else 3.0,
            "n_updates": 2,
        }
    ]


def test_adapter_requires_every_admitted_observation_to_carry_a_grade() -> None:
    fx = _fixture()
    demo = _demo(_D1_INCLUDES, _D2_NONE)
    records = _grade_records(demo)[:1]

    with pytest.raises(ValueError, match="without grade records"):
        kernel_events_to_score_k2_tables(events=fx.events, grade_records=records)
