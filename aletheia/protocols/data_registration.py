"""Content-addressed external-dataset registration for bounded campaigns.

ARL-2 roadmap step 1 requires external data to enter a question loop only through an explicit
lineage and target audit.  This module carries that registration as a pure value contract:
a :class:`RegisteredDatasetV1` card is a frozen, hash-closed artifact archived in the research
CAS and the campaign bundle.  There is deliberately no database table and no runtime consumer;
compilers and replay verifiers consume pinned card bytes.

The audit discipline is mechanical: every verdict recorded on a card must be recomputable from
the registered content bytes plus card fields alone.  :func:`verify_dataset_card` re-derives the
full verdict set and fails closed on any mismatch, so a card cannot assert an audit its bytes do
not support.  Policy commitments that are not data-derivable (target aggregation policy, split
mandate) ride as explicit policy statements bound by ``inputs_sha256``; the recomputable part of
each check is the numbers, which live only in the hashed detail.

This module stays pure: bytes and text are passed in, nothing is read from the filesystem.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import re
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from aletheia.protocols.base import (
    LOCAL_ID_PATTERN,
    PROTOCOL_SCHEMA_VERSION,
    SEMVER_PATTERN,
    SHA256_PATTERN,
    ProtocolModel,
    canonical_models,
    canonical_sha256,
    canonical_strings,
)

_ELEMENT_PATTERN = re.compile(r"[A-Z][a-z]?")

# Every recorded verdict asserts conformance by construction; a nonconforming audit raises
# instead of producing a card.  The constant follows the repo's Literal-typed receipt style.
_AUDIT_CONFORMS = "conforms"


class DatasetLicenseStatus(str, Enum):
    """Verification status of the recorded license terms; never an assertion of rights."""

    VERIFIED = "verified"
    TO_VERIFY = "to_verify"


class DatasetSourceLineageV1(ProtocolModel):
    """Where the registered bytes came from, exactly as retrieved."""

    schema_name: Literal["aletheia.dataset_source_lineage"] = "aletheia.dataset_source_lineage"
    schema_version: Literal[1] = PROTOCOL_SCHEMA_VERSION
    name: str = Field(min_length=1, max_length=255)
    url: str = Field(min_length=1, max_length=2_000)
    retrieval_note: str = Field(min_length=1, max_length=4_000)
    retrieved_at: datetime
    raw_filename: str = Field(min_length=1, max_length=255)
    raw_sha256: str = Field(pattern=SHA256_PATTERN)
    raw_bytes: int = Field(ge=1)
    citation: str = Field(min_length=1, max_length=1_000)
    license_terms: str = Field(min_length=1, max_length=1_000)
    license_status: DatasetLicenseStatus

    @model_validator(mode="after")
    def _license_evidence_is_explicit(self) -> "DatasetSourceLineageV1":
        if not self.license_terms.strip() or not self.citation.strip():
            raise ValueError("dataset lineage must record license terms and a citation")
        return self

    @property
    def lineage_sha256(self) -> str:
        return canonical_sha256(self)


class DatasetColumnManifestV1(ProtocolModel):
    """Exact column partition of the registered table."""

    schema_name: Literal["aletheia.dataset_column_manifest"] = "aletheia.dataset_column_manifest"
    schema_version: Literal[1] = PROTOCOL_SCHEMA_VERSION
    columns: tuple[str, ...] = Field(min_length=2)
    composition_column: str = Field(min_length=1, max_length=255)
    target_column: str = Field(min_length=1, max_length=255)
    feature_columns: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _columns_partition_exactly(self) -> "DatasetColumnManifestV1":
        canonical_strings(self.columns, "dataset columns")
        canonical_strings(self.feature_columns, "dataset feature columns")
        remainder = tuple(sorted(set(self.columns) - {self.composition_column, self.target_column}))
        if remainder != self.feature_columns:
            raise ValueError("feature columns must be exactly columns minus composition and target")
        if self.composition_column == self.target_column:
            raise ValueError("composition and target columns must differ")
        return self

    @property
    def manifest_sha256(self) -> str:
        return canonical_sha256(self)


class DatasetStratumRuleV1(ProtocolModel):
    """A membership rule the audit applies to composition strings.

    ``required_elements`` must all be present; at least ``multi_choice_minimum`` of
    ``multi_choice_elements`` must also be present.  ``multi_choice_minimum == 0`` disables the
    multi-choice clause (pure required-element stratum).
    """

    schema_name: Literal["aletheia.dataset_stratum_rule"] = "aletheia.dataset_stratum_rule"
    schema_version: Literal[1] = PROTOCOL_SCHEMA_VERSION
    stratum_id: str = Field(pattern=LOCAL_ID_PATTERN)
    required_elements: tuple[str, ...] = Field(min_length=1)
    multi_choice_elements: tuple[str, ...] = Field(default=())
    multi_choice_minimum: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _rule_is_canonical(self) -> "DatasetStratumRuleV1":
        canonical_strings(self.required_elements, "stratum required elements")
        canonical_strings(self.multi_choice_elements, "stratum multi-choice elements")
        if self.multi_choice_minimum > len(self.multi_choice_elements):
            raise ValueError("stratum multi-choice minimum exceeds its element set")
        if self.multi_choice_minimum > 0 and not self.multi_choice_elements:
            raise ValueError("stratum multi-choice minimum requires multi-choice elements")
        overlap = set(self.required_elements) & set(self.multi_choice_elements)
        if overlap:
            raise ValueError(f"stratum rule lists {sorted(overlap)} in both clauses")
        return self

    def matches_elements(self, elements: frozenset[str]) -> bool:
        if not set(self.required_elements) <= set(elements):
            return False
        if self.multi_choice_minimum > 0:
            hits = len(set(self.multi_choice_elements) & set(elements))
            if hits < self.multi_choice_minimum:
                return False
        return True

    @property
    def rule_sha256(self) -> str:
        return canonical_sha256(self)


class DatasetAuditVerdictV1(ProtocolModel):
    """One mechanically recomputable audit check, as recorded on the card."""

    schema_name: Literal["aletheia.dataset_audit_verdict"] = "aletheia.dataset_audit_verdict"
    schema_version: Literal[1] = PROTOCOL_SCHEMA_VERSION
    check_id: str = Field(pattern=LOCAL_ID_PATTERN)
    statement: str = Field(min_length=1, max_length=2_000)
    inputs_sha256: str = Field(pattern=SHA256_PATTERN)
    detail_sha256: str = Field(pattern=SHA256_PATTERN)
    outcome: Literal["conforms"] = _AUDIT_CONFORMS

    @property
    def verdict_sha256(self) -> str:
        return canonical_sha256(self)


class DatasetPreprocessingPolicyV1(ProtocolModel):
    """Declared preprocessing between the retrieved bytes and the registered content."""

    schema_name: Literal["aletheia.dataset_preprocessing_policy"] = (
        "aletheia.dataset_preprocessing_policy"
    )
    schema_version: Literal[1] = PROTOCOL_SCHEMA_VERSION
    policy_version: str = Field(pattern=SEMVER_PATTERN)
    statements: tuple[str, ...] = Field(min_length=1)
    derived_artifact_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _statements_are_canonical(self) -> "DatasetPreprocessingPolicyV1":
        canonical_strings(self.statements, "preprocessing statements")
        return self

    @property
    def policy_sha256(self) -> str:
        return canonical_sha256(self)


class DatasetRoundSplitPolicyV1(ProtocolModel):
    """Deterministic formula-based round partition over formula groups.

    For a group id ``g`` on dataset ``d`` under this policy::

        round(g) = 2 if int(sha256(d + "\\n" + salt + "\\n" + g), 16) % 100 < holdout_percent else 1

    The rule is a replayable hash fact, not a promise: both round sets re-derive from registered
    hashes alone, so compile-time and replay-time disjointness proofs share one definition and a
    changed dataset invalidates the partition by construction.
    """

    schema_name: Literal["aletheia.dataset_round_split_policy"] = (
        "aletheia.dataset_round_split_policy"
    )
    schema_version: Literal[1] = PROTOCOL_SCHEMA_VERSION
    holdout_percent: int = Field(ge=1, le=99)
    salt: str = Field(min_length=8, max_length=255)
    round_ids: tuple[int, ...] = (1, 2)

    @model_validator(mode="after")
    def _rounds_are_canonical(self) -> "DatasetRoundSplitPolicyV1":
        if self.round_ids != (1, 2):
            raise ValueError("round split policy partitions groups into rounds 1 and 2 only")
        if self.salt != self.salt.strip():
            raise ValueError("round split salt must be canonical")
        return self

    @property
    def policy_sha256(self) -> str:
        return canonical_sha256(self)


class RegisteredDatasetV1(ProtocolModel):
    """A registered external dataset: lineage, content identity, and audited target semantics."""

    schema_name: Literal["aletheia.registered_dataset"] = "aletheia.registered_dataset"
    schema_version: Literal[1] = PROTOCOL_SCHEMA_VERSION
    dataset_id: str = Field(pattern=LOCAL_ID_PATTERN)
    version: int = Field(ge=1)
    revision_parent_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    lineage: DatasetSourceLineageV1
    content_sha256: str = Field(pattern=SHA256_PATTERN)
    row_count: int = Field(ge=1)
    column_manifest: DatasetColumnManifestV1
    preprocessing: DatasetPreprocessingPolicyV1
    audit_verdicts: tuple[DatasetAuditVerdictV1, ...] = Field(min_length=1)
    stratum_rules: tuple[DatasetStratumRuleV1, ...] = Field(default=())
    split_policy: DatasetRoundSplitPolicyV1
    limitations: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _card_is_consistent(self) -> "RegisteredDatasetV1":
        canonical_models(
            self.audit_verdicts, key=lambda item: item.check_id, label="dataset audit verdicts"
        )
        canonical_models(
            self.stratum_rules, key=lambda item: item.stratum_id, label="dataset stratum rules"
        )
        canonical_strings(self.limitations, "dataset limitations")
        if self.version == 1:
            if self.revision_parent_sha256 is not None:
                raise ValueError("first dataset version has no revision parent")
        elif self.revision_parent_sha256 is None:
            raise ValueError("dataset versions above 1 must cite their revision parent")
        derived = self.preprocessing.derived_artifact_sha256
        if derived is None and self.content_sha256 != self.lineage.raw_sha256:
            raise ValueError("unprocessed registration content must be the retrieved raw bytes")
        if derived is not None and self.content_sha256 == self.lineage.raw_sha256:
            raise ValueError("derived content must differ from the retrieved raw bytes")
        return self

    @property
    def dataset_card_sha256(self) -> str:
        return canonical_sha256(self)


def dataset_group_round(
    dataset_sha256: str, policy: DatasetRoundSplitPolicyV1, group_id: str
) -> int:
    """Apply the card's partition formula to one group id."""

    if re.fullmatch(SHA256_PATTERN, dataset_sha256) is None:
        raise ValueError("dataset round split requires a lowercase sha256 dataset identity")
    if not group_id or group_id != group_id.strip():
        raise ValueError("dataset round split requires a canonical group id")
    digest = hashlib.sha256(
        "\n".join((dataset_sha256, policy.salt, group_id)).encode("utf-8")
    ).hexdigest()
    return 2 if int(digest, 16) % 100 < policy.holdout_percent else 1


def partition_group_rounds(
    dataset_sha256: str,
    policy: DatasetRoundSplitPolicyV1,
    group_ids: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Derive the (round 1, round 2) group sets; canonical order, disjoint by construction."""

    if len(group_ids) != len(set(group_ids)):
        raise ValueError("group ids must be unique")
    round_one: list[str] = []
    round_two: list[str] = []
    for group_id in group_ids:
        if dataset_group_round(dataset_sha256, policy, group_id) == 2:
            round_two.append(group_id)
        else:
            round_one.append(group_id)
    return tuple(sorted(round_one)), tuple(sorted(round_two))


def verify_group_partition(
    dataset_sha256: str,
    policy: DatasetRoundSplitPolicyV1,
    group_ids: tuple[str, ...],
    declared_round_one: tuple[str, ...],
    declared_round_two: tuple[str, ...],
) -> None:
    """Fail closed unless the declared round sets equal the formula's derivation."""

    derived_one, derived_two = partition_group_rounds(dataset_sha256, policy, group_ids)
    if sorted(declared_round_one) != list(derived_one) or sorted(declared_round_two) != list(
        derived_two
    ):
        raise ValueError("declared round groups differ from the split-policy derivation")
    overlap = set(declared_round_one) & set(declared_round_two)
    if overlap:
        raise ValueError(f"round group sets overlap on {sorted(overlap)}")


def enumerate_formula_groups(
    csv_text: str, column_manifest: DatasetColumnManifestV1
) -> tuple[str, ...]:
    """Enumerate the canonical formula-group ids of registered content.

    The group set is exactly the audit's ``formula_target`` key set: a row contributes
    its stripped composition string only when its target parses to a finite float, and
    the result is unique and sorted.  Round partitions and spent-group ledgers derive
    from this enumeration plus the card's split formula, so the campaign driver and the
    replay verifier share one group-set definition with :func:`recompute_dataset_audit`.
    """

    reader = csv.DictReader(io.StringIO(csv_text))
    header = reader.fieldnames
    if (
        header is None
        or len(header) != len(set(header))
        or set(header) != set(column_manifest.columns)
    ):
        raise ValueError("csv header differs from the registered column manifest")
    composition = column_manifest.composition_column
    target = column_manifest.target_column
    groups: set[str] = set()
    for record in reader:
        raw_target = (record.get(target) or "").strip()
        try:
            value = float(raw_target)
        except ValueError:
            continue
        if not math.isfinite(value):
            continue
        groups.add((record.get(composition) or "").strip())
    if not groups:
        raise ValueError("registered content contains no parsable target rows")
    return tuple(sorted(groups))


def _detail_digest(detail: dict[str, object]) -> str:
    from aletheia.research_kernel.schemas import canonical_json_bytes

    return hashlib.sha256(canonical_json_bytes(detail)).hexdigest()


def _inputs_digest(check_id: str, inputs: dict[str, object]) -> str:
    from aletheia.research_kernel.schemas import canonical_json_bytes

    payload = {"check_id": check_id, **inputs}
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _element_set(composition: str) -> frozenset[str]:
    return frozenset(_ELEMENT_PATTERN.findall(composition))


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


_CHECK_STATEMENTS = {
    "row_and_column_count": (
        "registered content exposes exactly the card's row count and column manifest"
    ),
    "target_finiteness": (
        "every target value parses as a finite positive float; near-zero-but-nonzero values are "
        "low-target measurements, not censored observations"
    ),
    "target_range": (
        "target minimum, maximum, median, and mean recomputed from the registered bytes"
    ),
    "duplication_policy": (
        "formula duplication quantified from the registered bytes; the registered target "
        "aggregation policy is the formula median, so train/test partitions must be grouped by "
        "formula"
    ),
    "within_formula_spread": (
        "within-formula target spread and median-predictor error recomputed from the registered "
        "bytes; the median-predictor error is the measurement noise floor bounding resolvable "
        "effects"
    ),
    "stratum_membership": (
        "each declared stratum rule's row count, row share, and on/off-stratum target medians "
        "recomputed from the registered bytes"
    ),
}


def recompute_dataset_audit(
    *,
    content_sha256: str,
    column_manifest: DatasetColumnManifestV1,
    stratum_rules: tuple[DatasetStratumRuleV1, ...],
    csv_text: str,
) -> tuple[DatasetAuditVerdictV1, ...]:
    """Re-derive every audit verdict from content bytes and card fields alone.

    Returns the verdict tuple in canonical ``check_id`` order; raises on any internal
    inconsistency (unparsable rows, empty tables).  The caller compares this against the
    recorded verdicts via :func:`verify_dataset_card` or installs it verbatim at authoring
    time.
    """

    if re.fullmatch(SHA256_PATTERN, content_sha256) is None:
        raise ValueError("content identity must be a lowercase sha256")
    reader = csv.DictReader(io.StringIO(csv_text))
    header = reader.fieldnames
    if (
        header is None
        or len(header) != len(set(header))
        or set(header) != set(column_manifest.columns)
    ):
        raise ValueError("csv header differs from the registered column manifest")
    composition = column_manifest.composition_column
    target = column_manifest.target_column
    rows = 0
    target_values: list[float] = []
    formula_target: dict[str, list[float]] = {}
    stratum_rows: dict[str, int] = {rule.stratum_id: 0 for rule in stratum_rules}
    stratum_targets: dict[str, list[float]] = {rule.stratum_id: [] for rule in stratum_rules}
    off_stratum_targets: list[float] = []
    unparsed = 0
    for record in reader:
        rows += 1
        raw_target = (record.get(target) or "").strip()
        try:
            value = float(raw_target)
        except ValueError:
            unparsed += 1
            continue
        if not math.isfinite(value):
            unparsed += 1
            continue
        target_values.append(value)
        formula = (record.get(composition) or "").strip()
        formula_target.setdefault(formula, []).append(value)
        elements = _element_set(formula)
        matched = False
        for rule in stratum_rules:
            if rule.matches_elements(elements):
                stratum_rows[rule.stratum_id] += 1
                stratum_targets[rule.stratum_id].append(value)
                matched = True
        if not matched:
            off_stratum_targets.append(value)
    if unparsed:
        raise ValueError(f"{unparsed} target values failed finite-float parsing")
    if not target_values:
        raise ValueError("registered content contains no parsable target values")

    verdicts: list[DatasetAuditVerdictV1] = []

    def add(check_id: str, inputs: dict[str, object], detail: dict[str, object]) -> None:
        verdicts.append(
            DatasetAuditVerdictV1(
                check_id=check_id,
                statement=_CHECK_STATEMENTS[check_id],
                inputs_sha256=_inputs_digest(
                    check_id, {"content_sha256": content_sha256, **inputs}
                ),
                detail_sha256=_detail_digest(detail),
            )
        )

    add(
        "row_and_column_count",
        {"row_count": rows, "columns": list(column_manifest.columns)},
        {"row_count": rows, "column_count": len(column_manifest.columns)},
    )
    add(
        "target_finiteness",
        {"parsed_target_rows": len(target_values)},
        {
            "parsed_target_rows": len(target_values),
            "nonpositive_targets": sum(1 for value in target_values if value <= 0.0),
            "exact_zero_targets": sum(1 for value in target_values if value == 0.0),
        },
    )
    add(
        "target_range",
        {"target_column": target},
        {
            "minimum": min(target_values),
            "maximum": max(target_values),
            "median": _median(target_values),
            "mean": sum(target_values) / len(target_values),
        },
    )
    duplicated = {name: values for name, values in formula_target.items() if len(values) > 1}
    duplicated_rows = sum(len(values) for values in duplicated.values())
    add(
        "duplication_policy",
        {"composition_column": composition},
        {
            "rows": rows,
            "unique_formulas": len(formula_target),
            "formulas_with_multiple_rows": len(duplicated),
            "rows_in_duplicated_formulas": duplicated_rows,
            "max_rows_one_formula": max((len(v) for v in formula_target.values()), default=0),
            "target_aggregation": "formula_median",
            "split_mandate": "formula_grouped",
        },
    )
    spreads: list[float] = []
    group_mae: list[float] = []
    for values in duplicated.values():
        group_median = _median(values)
        spreads.extend(abs(value - group_median) for value in values)
        group_mae.append(sum(abs(value - group_median) for value in values) / len(values))
    spread_p90_index = max(0, math.ceil(0.9 * len(spreads)) - 1) if spreads else 0
    add(
        "within_formula_spread",
        {"aggregation": "formula_median", "duplicated_formulas": len(duplicated)},
        {
            "duplicated_formulas": len(duplicated),
            "deviation_median": _median(spreads) if spreads else 0.0,
            "deviation_mean": (sum(spreads) / len(spreads)) if spreads else 0.0,
            "deviation_p90": (sorted(spreads)[spread_p90_index]) if spreads else 0.0,
            "deviation_max": (max(spreads) if spreads else 0.0),
            "median_predictor_mae_median": _median(group_mae) if group_mae else 0.0,
            "median_predictor_mae_mean": (sum(group_mae) / len(group_mae)) if group_mae else 0.0,
        },
    )
    for rule in stratum_rules:
        on_targets = stratum_targets[rule.stratum_id]
        detail = {
            "row_count": stratum_rows[rule.stratum_id],
            "row_share": stratum_rows[rule.stratum_id] / rows,
        }
        if on_targets and off_stratum_targets:
            detail["on_stratum_target_median"] = _median(on_targets)
            detail["off_stratum_target_median"] = _median(off_stratum_targets)
        verdicts.append(
            DatasetAuditVerdictV1(
                check_id=f"stratum:{rule.stratum_id}",
                statement=_CHECK_STATEMENTS["stratum_membership"],
                inputs_sha256=_inputs_digest(
                    f"stratum:{rule.stratum_id}",
                    {
                        "content_sha256": content_sha256,
                        "rule_sha256": rule.rule_sha256,
                    },
                ),
                detail_sha256=_detail_digest(detail),
            )
        )
    return tuple(sorted(verdicts, key=lambda item: item.check_id))


def verify_dataset_card(card: RegisteredDatasetV1, csv_bytes: bytes) -> None:
    """Fail closed unless the card's identity and every recorded verdict re-derive."""

    digest = hashlib.sha256(csv_bytes).hexdigest()
    if digest != card.content_sha256:
        raise ValueError("registered content bytes do not hash to the card's content identity")
    if len(csv_bytes) != card.lineage.raw_bytes:
        raise ValueError("registered content byte count differs from the recorded lineage")
    csv_text = csv_bytes.decode("utf-8")
    rows = sum(1 for _ in csv.reader(io.StringIO(csv_text))) - 1
    if rows != card.row_count:
        raise ValueError("registered row count does not re-derive from the content bytes")
    recomputed = recompute_dataset_audit(
        content_sha256=card.content_sha256,
        column_manifest=card.column_manifest,
        stratum_rules=card.stratum_rules,
        csv_text=csv_text,
    )
    if recomputed != card.audit_verdicts:
        recorded = {item.check_id: item.detail_sha256 for item in card.audit_verdicts}
        fresh = {item.check_id: item.detail_sha256 for item in recomputed}
        differing = sorted(
            check_id
            for check_id in set(recorded) | set(fresh)
            if recorded.get(check_id) != fresh.get(check_id)
        )
        raise ValueError(f"dataset audit verdicts do not re-derive: {differing}")


__all__ = [
    "DatasetAuditVerdictV1",
    "DatasetColumnManifestV1",
    "DatasetLicenseStatus",
    "DatasetPreprocessingPolicyV1",
    "DatasetRoundSplitPolicyV1",
    "DatasetSourceLineageV1",
    "DatasetStratumRuleV1",
    "RegisteredDatasetV1",
    "dataset_group_round",
    "partition_group_rounds",
    "recompute_dataset_audit",
    "verify_dataset_card",
    "verify_group_partition",
]
