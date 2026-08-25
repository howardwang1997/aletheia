# PF-1 + PF-2 + F7-S1 implementation report

Date: 2026-08-13

> Continuation: F7-S2 is now also engineering-complete. See
> `F7S2_INDEPENDENT_RUNNER_IMPLEMENTATION_REPORT_2026_08_13.md`.

## Outcome

The first implementation slice of the Frontier Scientist plan is engineering-complete at its
contract/foundation boundary:

- PF-1: Alembic owns the database schema, startup fails closed on revision mismatch, and the live
  legacy database was adopted and upgraded without changing existing evidence rows.
- PF-2: Run Manifest v1 freezes exact code, patch, Python/Conda SBOM, dependency lock, sandbox,
  models, prompts, tools, domain capabilities, datasets, split ledgers, evaluator, safety, budget,
  approvals, and lineage before the first scientific action.
- F7-S1: pure evaluation schemas, invalid-versus-scientific-false semantics, content-addressed
  submission/scorer receipts, an independent evaluator boundary policy, adversarial tests, and the
  accepted threat-model ADR now exist.

This report captures the earlier foundation slice. The independent runner was subsequently
completed in F7-S2; three benchmark adapters, private suite, repeated statistics,
baseline/ablation matrix, and report generator remain subsequent work.

## PF-1 evidence

### Live migration

- Legacy schema comparison before adoption: 0 differences.
- Legacy tables at adoption: 24.
- Baseline stamped: `20260813_0001`.
- Current head after upgrade: `20260813_0002`.
- Alembic autogeneration check after upgrade: no new operations.
- Current ORM/schema comparison: 0 differences.

The migration added `alembic_version` and `run_manifests`; application startup no longer executes
`create_all` or ad-hoc `ALTER TABLE`. The historical `create_all()` Python name remains only as a
compatibility wrapper around Alembic.

### Backup and restore drill

The pre-migration custom-format PostgreSQL backup is stored under the gitignored artifact directory:

```text
artifacts/backups/aletheia_pf1_pre_migration.dump
bytes: 215597219
sha256: caedb460cc1f547fa8e84414f7c1d528eee83bc305a0661d9fd27c8452e653e5
```

The backup was restored to an isolated database, strictly adopted at the legacy baseline, upgraded
to head, and checked against the pre-migration evidence fingerprints. All eight matched exactly:

| Table | Rows | Aggregate evidence hash |
|---|---:|---:|
| runs | 2,193,752 | -5298600135516788731 |
| experiments | 7,781 | 4526981641224014689 |
| claims | 17,088 | 7946035225853913670 |
| claim_evidence | 22,177 | -5478644618996391687 |
| artifacts | 10,683 | -2072442016573872503 |
| hypothesis_attempts | 56 | 1457548425535779996 |
| external_validation_ledgers | 33 | -3837454717808859461 |
| campaign_split_ledgers | 36 | -5941177687999185341 |

An independent empty database was also upgraded from zero to head. It produced 25 application
tables plus `alembic_version`, with zero ORM/schema differences. Both temporary verification
databases and container-side backup copies were removed after verification; the checked backup in
`artifacts/backups/` was retained.

## Verification

- Focused new tests: 28 passed.
- Direct database/API/campaign regression group: 33 passed.
- Full non-Docker suite before the final fixture adjustment: 505 passed, 1 skipped, 1 expected
  manifest-order conflict. The conflict was fixed by explicitly marking test runs as development
  manifests; the affected test and all focused tests then passed.
- Final full non-Docker suite: 509 passed, 1 skipped, 6 explicitly deselected Docker tests.
- Isolated PF-2 integration: a real run + dataset froze the file and ledger manifest to the same
  SHA-256, an identical second freeze was idempotent, and the generated Python/Conda SBOM had its
  own content identity.
- Ruff: passed.
- `alembic check`: no new upgrade operations.
- `git diff --check`: passed.

## Next slice

Proceed with F7-S2 and F7-S3:

1. implement the independent runner/workspace lifecycle and attempt ledger;
2. enforce resource limits and evaluator-owned retry classification;
3. generate scorer receipts and repeated-run statistics;
4. implement the first three minimal public adapters with known-good and deliberately wrong
   submissions.
