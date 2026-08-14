# Aletheia database migrations

The database schema is versioned with Alembic. Application startup never creates or alters tables.

For a fresh database:

```bash
conda run -n aletheia alembic upgrade head
```

For a pre-Alembic database created by the old `create_all()` path:

```bash
conda run -n aletheia python scripts/adopt_schema_baseline.py
```

The adoption command is deliberately strict: it stamps revision `20260813_0001` only when Alembic
reports no differences from that legacy baseline. It does not repair a partial or unexpected
schema. Then run `alembic upgrade head` to add post-baseline tables. Back up the database before
adoption or upgrade; `scripts/backup_database.py` records a content hash receipt when the local
`pg_dump` client is available.

Tests may still use `aletheia.db.create_all()` as an explicit test fixture. Runtime code must call
`require_schema_current()` and fail closed when the database is empty, behind, or ahead.
