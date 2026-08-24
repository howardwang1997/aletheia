"""SQLite-only safety harness for observation persistence contract tests."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine

# Register the real referenced tables in Base.metadata so SQLAlchemy can resolve every FK while
# emitting only the isolated PR-5 tables below.  The SQLite database itself uses deliberately tiny
# parent stubs; production FK/trigger behavior belongs to opt-in PostgreSQL migration tests.
import aletheia.execution.persistence  # noqa: F401
import aletheia.jobs.persistence  # noqa: F401
import aletheia.research_store.persistence  # noqa: F401
from aletheia.observations.persistence import OBSERVATION_PERSISTENCE_TABLES


_PARENT_STUBS = (
    "CREATE TABLE research_quest_streams (quest_id varchar(36) PRIMARY KEY)",
    "CREATE TABLE durable_tasks (task_id varchar(96) PRIMARY KEY)",
    "CREATE TABLE research_kernel_objects (object_sha256 varchar(64) PRIMARY KEY)",
    """
    CREATE TABLE execution_attempts (
      attempt_id varchar(36) PRIMARY KEY,
      execution_id varchar(36) NOT NULL,
      UNIQUE (attempt_id, execution_id)
    )
    """,
    """
    CREATE TABLE research_kernel_events (
      quest_id varchar(36) NOT NULL,
      sequence bigint NOT NULL,
      event_sha256 varchar(64) PRIMARY KEY,
      event_type varchar(64) NOT NULL,
      UNIQUE (quest_id, sequence, event_sha256, event_type)
    )
    """,
    """
    CREATE TABLE research_kernel_outbox (
      outbox_id varchar(96) PRIMARY KEY,
      quest_id varchar(36) NOT NULL,
      sequence bigint NOT NULL,
      event_sha256 varchar(64) NOT NULL,
      UNIQUE (outbox_id, quest_id, sequence, event_sha256)
    )
    """,
    """
    CREATE TABLE execution_qualification_terminal_outbox (
      outbox_id varchar(96) PRIMARY KEY,
      execution_id varchar(36) NOT NULL,
      attempt_id varchar(36) NOT NULL,
      terminal_authority_sha256 varchar(64) NOT NULL,
      UNIQUE (outbox_id, execution_id, attempt_id, terminal_authority_sha256)
    )
    """,
    """
    CREATE TABLE execution_qualification_admissions (
      admission_sha256 varchar(64) PRIMARY KEY
    )
    """,
)


def sqlite_observation_engine() -> Engine:
    """Create an isolated schema with FK checks and test-only append-only triggers."""

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        for statement in _PARENT_STUBS:
            connection.exec_driver_sql(statement)
        for table in OBSERVATION_PERSISTENCE_TABLES:
            table.create(connection)
        for table in OBSERVATION_PERSISTENCE_TABLES:
            connection.exec_driver_sql(
                f"""
                CREATE TRIGGER test_{table.name}_reject_update
                BEFORE UPDATE ON {table.name}
                BEGIN SELECT RAISE(ABORT, '{table.name} is append-only'); END
                """
            )
            connection.exec_driver_sql(
                f"""
                CREATE TRIGGER test_{table.name}_reject_delete
                BEFORE DELETE ON {table.name}
                BEGIN SELECT RAISE(ABORT, '{table.name} is append-only'); END
                """
            )
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
    return engine
