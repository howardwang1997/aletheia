"""Safe operational helpers around the Alembic schema baseline."""

from __future__ import annotations

from dataclasses import dataclass

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.engine import Connection

import aletheia.memory.ledger  # noqa: F401  (register every ORM table)
from aletheia.db import Base, SchemaCompatibilityError, alembic_config, engine

LEGACY_BASELINE_REVISION = "20260813_0001"
POST_BASELINE_TABLES = frozenset({"run_manifests"})


@dataclass(frozen=True)
class BaselineAdoptionReceipt:
    revision: str
    table_count: int
    schema_diff_count: int


def schema_diffs(
    connection: Connection, *, exclude_tables: frozenset[str] = frozenset()
) -> list[object]:
    """Return Alembic's structural diff between the connected schema and ORM metadata."""
    context = MigrationContext.configure(
        connection,
        opts={
            "compare_type": True,
            "compare_server_default": False,
            "include_object": (
                lambda _object, name, type_, _reflected, _compare_to: not (
                    type_ == "table" and name in exclude_tables
                )
            ),
        },
    )
    return list(compare_metadata(context, Base.metadata))


def adopt_existing_baseline() -> BaselineAdoptionReceipt:
    """Stamp a legacy create_all database only after exact structural comparison.

    This never creates, alters, or repairs application tables. An empty database must use
    ``alembic upgrade head`` instead; a partial/changed database requires a reviewed migration.
    """
    with engine().connect() as connection:
        tables = set(inspect(connection).get_table_names())
        if "alembic_version" in tables:
            raise SchemaCompatibilityError("database is already managed by Alembic")
        application_tables = tables - {"alembic_version"}
        if not application_tables:
            raise SchemaCompatibilityError(
                "database is empty; use `conda run -n aletheia alembic upgrade head`"
            )
        diffs = schema_diffs(connection, exclude_tables=POST_BASELINE_TABLES)
        if diffs:
            preview = "; ".join(repr(diff) for diff in diffs[:5])
            raise SchemaCompatibilityError(
                "legacy schema does not exactly match the audited baseline; refusing to stamp "
                f"({len(diffs)} differences: {preview})"
            )

    cfg = alembic_config()
    command.stamp(cfg, LEGACY_BASELINE_REVISION)
    return BaselineAdoptionReceipt(
        revision=LEGACY_BASELINE_REVISION,
        table_count=len(application_tables),
        schema_diff_count=0,
    )
