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

Revision `20260814_0003` adds the F8-S1 immutable corpus store. Its knowledge rows and membership
edges reject SQL `UPDATE` and `DELETE`; corrections, later observations, and new corpus membership
must be inserted as new content-addressed versions. The migration stores hashes, locators, typed
metadata, article-level access grants, and provider receipt identities—not licensed source text.

Revision `20260815_0004` adds F9-S1 immutable research-question, hypothesis-version, assumption,
prediction, competing-belief, and world-model snapshot tables. Stable lineage IDs are separate
from content SHA-256 version identities; database triggers reject mutation. The existing K2
`belief_states` table and historical K2 events are not rewritten. A read-only
`k2_belief_state_compat` projection exposes their Beta mean and labels the representation explicitly
so callers cannot confuse it with an F9 multi-hypothesis posterior.

Revision `20260815_0005` adds immutable F9-S8 world-model transition records. Each record binds one
committed update receipt to its source, posterior, and optional revision-closed next snapshot.
Application code writes those objects and the corresponding typed scheduler event in one
transaction; transition rows reject SQL `UPDATE` and `DELETE`.
