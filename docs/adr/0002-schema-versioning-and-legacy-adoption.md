# ADR-0002: versioned schemas and fail-closed legacy adoption

- Status: accepted
- Date: 2026-08-13
- Scope: PF-1

## Decision

Alembic is the sole owner of durable database schema changes. Application startup checks that the
database revision equals the code's single Alembic head and never runs `create_all` or ad-hoc DDL.
Empty, stale, future, branched, and unversioned databases fail closed with an operator action.

The historical `create_all()` helper remains temporarily for explicit tests and local fixtures. It
is not called from application startup. A legacy database may be stamped at the baseline only after
Alembic autogeneration reports zero differences against the frozen ORM baseline; adoption never
repairs or alters application tables. Operators must take a verified backup before production
adoption or upgrade.

The baseline downgrade is intentionally unavailable because deleting the scientific ledger is not
a safe rollback. Restore a verified backup instead. Later, genuinely reversible migrations should
implement their own downgrade; irreversible migrations must document that property.

## Consequences

- Existing installations require one explicit baseline-adoption operation.
- ORM edits without migrations cause CI/autogenerate checks to fail.
- Deployments apply migrations before starting the API.
- Schema drift can no longer be silently concealed by startup DDL.
