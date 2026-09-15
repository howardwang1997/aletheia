"""Regressions for the registered-card row binding."""

import hashlib

import pytest

from aletheia.execution.cuprate.card_rows import restrict_to_bound_batch

CSV = (
    b"material,critical_temp\n"
    b"  YBa2Cu3O7 ,93.0\n"
    b"SiO2,0.0005\n"
    b"Bi2Sr2CaCu2O8,91.5\n"
    b"Fe2O3,0.0009\n"
    b"La2SrCu2O6,0.0011\n"
)


def bind(csv_bytes=CSV, batch=("YBa2Cu3O7", "Bi2Sr2CaCu2O8", "La2SrCu2O6")):
    return restrict_to_bound_batch(
        csv_bytes=csv_bytes,
        expected_content_sha256=hashlib.sha256(csv_bytes).hexdigest(),
        composition_column="material",
        target_column="critical_temp",
        bound_batch_group_ids=batch,
    )


def test_bound_batch_rows_are_kept_and_off_batch_rows_dropped():
    rows = bind()
    assert rows.formulas == ("YBa2Cu3O7", "Bi2Sr2CaCu2O8", "La2SrCu2O6")
    assert rows.targets == (93.0, 91.5, 0.0011)
    assert rows.dropped_off_batch == 2


def test_content_identity_is_verified_before_any_row_is_read():
    with pytest.raises(ValueError, match="registered dataset content"):
        restrict_to_bound_batch(
            csv_bytes=CSV,
            expected_content_sha256="0" * 64,
            composition_column="material",
            target_column="critical_temp",
            bound_batch_group_ids=("YBa2Cu3O7",),
        )


def test_group_identity_is_the_stripped_raw_formula():
    # The first row carries surrounding whitespace; the batch id matches the strip.
    rows = bind(batch=("YBa2Cu3O7",))
    assert rows.formulas == ("YBa2Cu3O7",)
    assert rows.targets == (93.0,)


def test_batch_without_matching_rows_fails_closed():
    with pytest.raises(ValueError, match="no registered rows"):
        bind(batch=("Nb3Sn",))


def test_empty_batch_declaration_fails_closed():
    with pytest.raises(ValueError, match="no formula groups"):
        bind(batch=())


def test_missing_columns_fail_closed():
    with pytest.raises(ValueError, match="composition column"):
        bind(batch=("SiO2",), csv_bytes=b"formula,critical_temp\nSiO2,1.0\n")
    with pytest.raises(ValueError, match="target column"):
        bind(batch=("SiO2",), csv_bytes=b"material,temp\nSiO2,1.0\n")


@pytest.mark.parametrize(
    "bad",
    [
        b"material,critical_temp\nYBa2Cu3O7,abc\n",
        b"material,critical_temp\nYBa2Cu3O7,nan\n",
        b"material,critical_temp\nYBa2Cu3O7,inf\n",
    ],
)
def test_non_finite_targets_fail_closed(bad):
    with pytest.raises(ValueError):
        bind(batch=("YBa2Cu3O7",), csv_bytes=bad)
