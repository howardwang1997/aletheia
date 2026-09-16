"""Contract tests for content-addressed dataset registration (ARL-2 keystone B, PR B1)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from aletheia.protocols.data_registration import (
    DatasetColumnManifestV1,
    DatasetLicenseStatus,
    DatasetPreprocessingPolicyV1,
    DatasetRoundSplitPolicyV1,
    DatasetSourceLineageV1,
    DatasetStratumRuleV1,
    RegisteredDatasetV1,
    dataset_group_round,
    enumerate_formula_groups,
    partition_group_rounds,
    recompute_dataset_audit,
    verify_dataset_card,
    verify_group_partition,
)

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

CUPRATE_RULE = DatasetStratumRuleV1(
    stratum_id="multi_ae_cuprate",
    required_elements=("Cu", "O"),
    multi_choice_elements=("Ba", "Ca", "Mg", "Sr"),
    multi_choice_minimum=2,
)


def _manifest() -> DatasetColumnManifestV1:
    return DatasetColumnManifestV1(
        columns=("critical_temp", "feature_a", "material"),
        composition_column="material",
        target_column="critical_temp",
        feature_columns=("feature_a",),
    )


def _lineage(csv_bytes: bytes) -> DatasetSourceLineageV1:
    return DatasetSourceLineageV1(
        name="synthetic registration fixture",
        url="https://example.invalid/synthetic.csv",
        retrieval_note="authored in-test",
        retrieved_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
        raw_filename="synthetic.csv",
        raw_sha256=hashlib.sha256(csv_bytes).hexdigest(),
        raw_bytes=len(csv_bytes),
        citation="synthetic fixture; no external source",
        license_terms="CC0 (synthetic)",
        license_status=DatasetLicenseStatus.VERIFIED,
    )


def _card(csv_text: str = CSV_TEXT) -> RegisteredDatasetV1:
    csv_bytes = csv_text.encode("utf-8")
    lineage = _lineage(csv_bytes)
    manifest = _manifest()
    verdicts = recompute_dataset_audit(
        content_sha256=lineage.raw_sha256,
        column_manifest=manifest,
        stratum_rules=(CUPRATE_RULE,),
        csv_text=csv_text,
    )
    return RegisteredDatasetV1(
        dataset_id="synthetic-registration-fixture",
        version=1,
        lineage=lineage,
        content_sha256=lineage.raw_sha256,
        row_count=7,
        column_manifest=manifest,
        preprocessing=DatasetPreprocessingPolicyV1(
            policy_version="1.0.0",
            statements=("no row-level preprocessing; registered content is the retrieved file",),
        ),
        audit_verdicts=verdicts,
        stratum_rules=(CUPRATE_RULE,),
        split_policy=DatasetRoundSplitPolicyV1(holdout_percent=20, salt="fixture-salt-0001"),
        limitations=("synthetic fixture for contract tests",),
    )


def test_card_round_trip_verifies() -> None:
    card = _card()
    verify_dataset_card(card, CSV_TEXT.encode("utf-8"))
    check_ids = tuple(item.check_id for item in card.audit_verdicts)
    assert check_ids == tuple(sorted(check_ids))
    assert "stratum:multi_ae_cuprate" in check_ids


def test_stratum_audit_tracks_formula_family_membership() -> None:
    # Ba2Sr1Cu2O6 (2 rows) and Sr2Ca1Cu2O6 match the rule; removing one family row must
    # change the hashed stratum detail, proving the count is data-derived.
    full = recompute_dataset_audit(
        content_sha256="d" * 64,
        column_manifest=_manifest(),
        stratum_rules=(CUPRATE_RULE,),
        csv_text=CSV_TEXT,
    )
    trimmed = recompute_dataset_audit(
        content_sha256="d" * 64,
        column_manifest=_manifest(),
        stratum_rules=(CUPRATE_RULE,),
        csv_text=CSV_TEXT.replace("Sr2Ca1Cu2O6,85.0,2\n", ""),
    )

    def detail(verdicts: tuple, check_id: str) -> str:
        return next(item.detail_sha256 for item in verdicts if item.check_id == check_id)

    assert detail(full, "stratum:multi_ae_cuprate") != detail(trimmed, "stratum:multi_ae_cuprate")
    assert detail(full, "target_range") != detail(trimmed, "target_range")
    assert detail(full, "duplication_policy") != detail(trimmed, "duplication_policy")
    # Sr2Ca1Cu2O6 is a single-row formula: its removal cannot move the within-formula spread.
    assert detail(full, "within_formula_spread") == detail(trimmed, "within_formula_spread")


def test_verify_fails_on_swapped_bytes() -> None:
    card = _card()
    swapped = CSV_TEXT.replace("SiO2,0.5,5", "SiO2,0.6,5").encode("utf-8")
    with pytest.raises(ValueError, match="content identity"):
        verify_dataset_card(card, swapped)


def test_verify_fails_on_tampered_verdict() -> None:
    card = _card()
    tampered = []
    for verdict in card.audit_verdicts:
        if verdict.check_id == "target_range":
            tampered.append(
                verdict.model_copy(
                    update={"detail_sha256": "0" * 32 + "f" * 32},
                )
            )
        else:
            tampered.append(verdict)
    tampered_card = card.model_copy(
        update={
            "audit_verdicts": tuple(tampered),
        }
    )
    with pytest.raises(ValueError, match="target_range"):
        verify_dataset_card(tampered_card, CSV_TEXT.encode("utf-8"))


def test_recompute_fails_on_header_mismatch() -> None:
    with pytest.raises(ValueError, match="column manifest"):
        recompute_dataset_audit(
            content_sha256="0" * 64,
            column_manifest=_manifest(),
            stratum_rules=(CUPRATE_RULE,),
            csv_text=CSV_TEXT.replace("feature_a", "feature_b"),
        )


def test_split_policy_golden_vectors() -> None:
    policy = DatasetRoundSplitPolicyV1(holdout_percent=20, salt="fixture-salt-0001")
    dataset_sha = "a" * 64
    for group_id in ("Ba2Sr1Cu2O6", "Fe2O3", "SiO2", "LaCuO3", "Sr2Ca1Cu2O6"):
        digest = hashlib.sha256(
            "\n".join((dataset_sha, policy.salt, group_id)).encode("utf-8")
        ).hexdigest()
        expected = 2 if int(digest, 16) % 100 < 20 else 1
        assert dataset_group_round(dataset_sha, policy, group_id) == expected


def test_partition_is_disjoint_and_complete() -> None:
    policy = DatasetRoundSplitPolicyV1(holdout_percent=50, salt="fixture-salt-0002")
    dataset_sha = "b" * 64
    groups = tuple(f"formula-{index}" for index in range(200))
    round_one, round_two = partition_group_rounds(dataset_sha, policy, groups)
    assert not (set(round_one) & set(round_two))
    assert set(round_one) | set(round_two) == set(groups)
    assert round_one == tuple(sorted(round_one))
    assert round_two == tuple(sorted(round_two))
    with pytest.raises(ValueError, match="differ"):
        verify_group_partition(dataset_sha, policy, groups, round_two, round_one)
    verify_group_partition(dataset_sha, policy, groups, round_one, round_two)


def _revalidate(card: RegisteredDatasetV1, **updates: object) -> RegisteredDatasetV1:
    return RegisteredDatasetV1.model_validate({**card.model_dump(), **updates})


def test_version_chain_requires_parent_above_one() -> None:
    card = _card()
    with pytest.raises(ValidationError, match="revision parent"):
        _revalidate(card, version=2)
    child = _revalidate(card, version=2, revision_parent_sha256=card.dataset_card_sha256)
    assert child.revision_parent_sha256 == card.dataset_card_sha256


def test_unprocessed_content_must_match_raw_bytes() -> None:
    card = _card()
    with pytest.raises(ValidationError, match="raw bytes"):
        _revalidate(card, content_sha256="c" * 64)


def test_lineage_requires_license_evidence() -> None:
    csv_bytes = CSV_TEXT.encode("utf-8")
    lineage = _lineage(csv_bytes)
    with pytest.raises(ValidationError, match="license"):
        DatasetSourceLineageV1.model_validate({**lineage.model_dump(), "license_terms": "  "})


def test_column_manifest_partitions_exactly() -> None:
    with pytest.raises(ValidationError, match="feature columns"):
        DatasetColumnManifestV1(
            columns=("critical_temp", "feature_a", "material"),
            composition_column="material",
            target_column="critical_temp",
            feature_columns=("feature_a", "material"),
        )


def test_stratum_rule_clauses_must_not_overlap() -> None:
    with pytest.raises(ValidationError, match="both clauses"):
        DatasetStratumRuleV1(
            stratum_id="broken",
            required_elements=("Ba", "Cu"),
            multi_choice_elements=("Ba", "Sr"),
            multi_choice_minimum=1,
        )


def test_enumerate_formula_groups_returns_the_audit_grouping() -> None:
    groups = enumerate_formula_groups(CSV_TEXT, _manifest())
    assert groups == ("Ba2Sr1Cu2O6", "Fe2O3", "LaCuO3", "SiO2", "Sr2Ca1Cu2O6")


def test_enumerate_formula_groups_skips_unparsable_target_rows() -> None:
    csv_text = CSV_TEXT + "HgBa2Ca2Cu3O8.1,not-a-number,9\n"
    groups = enumerate_formula_groups(csv_text, _manifest())
    assert "HgBa2Ca2Cu3O8.1" not in groups
    assert groups == ("Ba2Sr1Cu2O6", "Fe2O3", "LaCuO3", "SiO2", "Sr2Ca1Cu2O6")


def test_enumerate_formula_groups_requires_the_registered_header() -> None:
    with pytest.raises(ValueError, match="csv header differs"):
        enumerate_formula_groups("wrong,header,x\n1,2,3\n", _manifest())


def test_enumerate_formula_groups_fails_closed_without_parsable_rows() -> None:
    csv_text = "material,critical_temp,feature_a\nBa2Sr1Cu2O6,NaN,1\n"
    with pytest.raises(ValueError, match="no parsable target rows"):
        enumerate_formula_groups(csv_text, _manifest())


def test_enumeration_partitions_fully_into_card_rounds() -> None:
    card = _card()
    groups = enumerate_formula_groups(CSV_TEXT, card.column_manifest)
    round_one, round_two = partition_group_rounds(card.content_sha256, card.split_policy, groups)
    assert sorted(round_one + round_two) == list(groups)
    assert not (set(round_one) & set(round_two))
    verify_group_partition(card.content_sha256, card.split_policy, groups, round_one, round_two)
