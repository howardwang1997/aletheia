"""Registered-card row binding for the cuprate diagnostic (replaces TC_CSV).

The capability never reads a hardcoded path.  Its input is the card's CSV
bytes (staged by the input materializer from CAS custody) plus the bound
batch of formula-group ids; this module verifies the bytes against the
registered content identity and restricts the rows to the bound batch
before any analysis touches them.

Group identity follows the card's own convention: the stripped raw
composition string (``data_registration`` keys ``formula_target`` on the
same value).  The round a group belongs to is derived by the caller from
the card's split formula keyed on ``card.content_sha256`` — row
partitioning is invariant to card metadata revisions over the same bytes.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass


@dataclass(frozen=True)
class BoundBatchRows:
    """The registered rows belonging to one protocol's bound batch."""

    formulas: tuple[str, ...]
    targets: tuple[float, ...]
    dropped_off_batch: int

    def __post_init__(self) -> None:
        if not self.formulas:
            raise ValueError("bound batch contains no registered rows")
        if len(self.formulas) != len(self.targets):
            raise ValueError("bound-batch formulas and targets are misaligned")


def restrict_to_bound_batch(
    *,
    csv_bytes: bytes,
    expected_content_sha256: str,
    composition_column: str,
    target_column: str,
    bound_batch_group_ids: tuple[str, ...],
) -> BoundBatchRows:
    """Verify the card bytes and keep only the bound batch's rows.

    Fails closed when the bytes' sha256 differs from the registered content
    identity, when the header lacks the composition or target column, or
    when a target value is not a finite float (the card's audit already
    guarantees these over the full file; re-deriving here keeps the
    capability's input contract self-contained).
    """

    import hashlib
    import math

    digest = hashlib.sha256(csv_bytes).hexdigest()
    if digest != expected_content_sha256:
        raise ValueError("materialized rows differ from the registered dataset content")
    if not bound_batch_group_ids:
        raise ValueError("bound batch declares no formula groups")
    batch = set(bound_batch_group_ids)

    reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8")))
    if composition_column not in (reader.fieldnames or []):
        raise ValueError("materialized rows lack the registered composition column")
    if target_column not in (reader.fieldnames or []):
        raise ValueError("materialized rows lack the registered target column")

    formulas: list[str] = []
    targets: list[float] = []
    dropped = 0
    for record in reader:
        formula = (record.get(composition_column) or "").strip()
        raw_target = (record.get(target_column) or "").strip()
        if formula not in batch:
            dropped += 1
            continue
        try:
            value = float(raw_target)
        except ValueError as exc:
            raise ValueError(
                f"registered target for {formula} is not a float: {raw_target!r}"
            ) from exc
        if not math.isfinite(value):
            raise ValueError(f"registered target for {formula} is not finite")
        formulas.append(formula)
        targets.append(value)
    return BoundBatchRows(
        formulas=tuple(formulas), targets=tuple(targets), dropped_off_batch=dropped
    )
