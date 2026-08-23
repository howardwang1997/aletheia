# PR-0 legacy freeze operator and developer guide

PR-0 is the migration gate before the new scientific kernel.  It makes historical behavior immutable
and keeps it below the scientific-admission boundary; it does not run new science and requires no GPU.

## What is enforced

- `tests/migration/test_pr0_dependency_boundary.py` follows transitive imports from the protected
  packages and freezes `ExperimentDriver` to the durable legacy worker, including repository scripts,
  Docker workers, Alembic revisions, and dynamic handler registration. It fails closed on conventional
  AST-visible `importlib`, `importlib.util` source-file loading, `runpy`, `pkgutil`, `__builtins__`,
  `sys.modules`, and runtime-code escapes. Host-side general-purpose dynamic selection and source-file
  loading covered by this migration boundary are centralized in a content-pinned guard that rejects
  raw driver requests and source paths as well as resolved/re-exported driver objects. Fixed-path
  authored-code loaders inside isolated sandbox runner programs remain governed by their sandbox and
  code-admission contracts.
- Normalized complete-class fingerprints freeze `ExperimentDriver`, `DomainPlugin`, and
  `ComputeBackend`; the complete driver module and durable gateway are frozen separately so module
  helpers cannot drift and production cannot silently select another driver implementation.
- `architecture/legacy_write_owners.v1.json` and its test enumerate existing mutation authority and
  the future single owner of each surface, covering all 80 current SQLAlchemy tables. Specialized
  service/writer/file/external/ORM graphs classify known sinks; a normalized-AST backstop separately
  freezes all 281 legacy production Python files and 27 migration/Alembic files so an unrecognized
  semantic source change still requires explicit review.
- `tests/fixtures/legacy/v1/golden_contract.v1.json` binds named offline compatibility tests.
- `tests/fixtures/legacy/v1/run_projections.v1.json` freezes sanitized event/artifact projections from
  materials completed, molecules completed/rejected, and RAG completed/rejected runs;
  `run_projection_sources.v1.json` contains the payload-free source manifest needed to recompute them.
- `tests/fixtures/legacy/v1/endurance/` preserves the real 72-hour authoritative manifest and blocked
  report, a source projection of checkpoint identities/references, and a non-authoritative derived
  interpretation.
- `tests/fixtures/legacy/v1/snapshot/` is the actual tracked v1 migration instance: two reviewed
  sanitized legacy projections, their CAS bytes, immutable source-version binding, redaction and
  exporter declarations, snapshot `lgs_134ee8f705cafb3f361719ec6429f6fe`, and engineering-only import
  receipt `lgi_61cb8625bec3ca0e77bc7de9ce272eb9`.

Run the migration-contract checks alone with:

```bash
conda run -n aletheia python -m pytest tests/migration -q
```

Run the complete PR-0 engineering gate, including every exact legacy golden test node named by the
frozen contract, with:

```bash
conda run -n aletheia python scripts/run_pr0_gate.py
```

The runner derives node IDs from the frozen contract instead of maintaining a second hand-written
list.  “Offline” here means no external model, retrieval service, or public network call; three legacy
nodes use the normal local PostgreSQL ledger.  Start the project's configured PostgreSQL service and
apply the expected schema before running the complete gate.  Migration tests are under the repository's
normal `testpaths`, so a full pytest run cannot omit them; the complete non-Docker regression also
executes the named golden nodes.

Run the complete non-Docker regression with:

```bash
conda run -n aletheia python -m pytest -m "not docker" -q
```

This boundary is trusted-source dependency lint, not a security or capability sandbox.  It recognizes
reviewed, conventional Python AST forms; it cannot establish the absence of deliberately obfuscated
metaprogramming, injected objects, pre-populated module caches, deserialized callables, or native
loaders.  Untrusted code must still run behind the project's process/container and capability controls.

## Creating a sanitized snapshot

Prepare a request whose objects are already reviewed and sanitized.  Paths are relative to one source
root and must be in `(logical_name, source_relative_path)` order:

```json
{
  "schema_name": "aletheia.legacy_freeze_request",
  "schema_version": 1,
  "source_system": "legacy-postgresql-export",
  "source_scope": "run/example",
  "source_version": "export/v2",
  "redaction_manifest_sha256": "<64 lowercase hex characters>",
  "exporter_identity_scheme": "git_tracked_entrypoint_v1",
  "exporter_git_commit": "<40 lowercase hex characters>",
  "exporter_git_tree": "<40 lowercase hex characters>",
  "exporter_entrypoint": "exporter.py",
  "exporter_entrypoint_sha256": "<SHA-256 of the entrypoint blob at HEAD>",
  "exporter_code_sha256": "<64 lowercase hex characters>",
  "exporter_execution_assurance": "operator_attested",
  "objects": [
    {
      "logical_name": "event-projection",
      "source_relative_path": "events.sanitized.json",
      "role": "event_log",
      "media_type": "application/json",
      "data_class": "internal_sanitized"
    }
  ]
}
```

Derive the exporter identity fields from the exact clean Git repository containing the reviewed
exporter.  The entrypoint must be a normalized repository-relative path, a regular file tracked at
`HEAD`, and neither it nor an intermediate component may be a symlink.  Write the diagnostic outside
that repository; creating it inside the repository would make the exporter dirty before identity
inspection:

```bash
conda run -n aletheia python scripts/freeze_legacy_snapshot.py exporter-identity \
  --exporter-root /clean/exporter/repository \
  --exporter-entrypoint exporters/export_legacy.py
```

Copy the printed commit, tree, entrypoint path, entrypoint SHA-256, and `exporter_code_sha256` into the
request.  The code hash is the canonical digest of the commit/tree plus the tracked entrypoint path and
its `HEAD` blob SHA-256, not a free-form operator label.  The separately supplied
`--exporter-entrypoint` must name the same path.  Then freeze and ask the CLI to atomically create the
manifest outside the exporter repository:

```bash
conda run -n aletheia python scripts/freeze_legacy_snapshot.py freeze request.json \
  --exporter-root /clean/exporter/repository \
  --exporter-entrypoint exporters/export_legacy.py \
  --source-root /reviewed/sanitized/export \
  --snapshot-store /restricted/legacy-cas \
  --output-manifest /restricted/legacy-cas/exports/manifest.json
```

The command refuses credential-like filenames, private-key markers, traversal, symlinks, a dirty
exporter, a missing/untracked/mismatched exporter entrypoint, an exporter identity mismatch, CAS
collisions, and reuse of one declared source version for different content.  Dirty exporter
repositories are refused even for dev fixtures.

There are two deliberately separate assurances in the resulting manifest:

- `exporter_execution_assurance=operator_attested` says a human/operator attests that the declared
  tracked entrypoint produced the reviewed source export.  The Git and entrypoint hashes make the
  declared code unambiguous, but they do **not** cryptographically prove that this code was executed or
  that its output is the supplied source root.
- `freezer_identity` is generated by the CLI, not copied from the request.  It hashes the on-disk bytes
  of `scripts/freeze_legacy_snapshot.py` and `aletheia/migration/legacy.py` immediately before freezing
  and records `runtime_source_bytes_hashed_before_freeze`.  This binds the freezer source bundle used by
  the command; it is not a claim about the exporter, the Python interpreter, or loaded bytecode.  The
  core freeze boundary re-reads every path behind an explicitly supplied freezer identity and rejects
  missing, changed, escaping, or symlinked sources rather than trusting self-consistent hash metadata.

Release snapshots must go through this CLI.  The request/model layer can validate the internal shape of
a declared external Git identity, but only the CLI checks that the commit, tree, regular tracked
entrypoint, clean worktree, and entrypoint bytes exist in the supplied exporter repository.  The tracked
v1 bundle retains the exporter entrypoint bytes and identity fields, not a Git bundle; its external
commit/tree are therefore historical operator-attested declarations, not Git objects independently
available from this repository.

`redaction_manifest_sha256` is an operator-reviewed external attestation in v1.  The built-in scanner
is deliberately only a narrow denylist for obvious credential filenames and private-key headers; it
does not prove that arbitrary API tokens, personal data, licensed content, or private evaluator data
were removed.  Custody review must happen before `freeze`.

Verify the manifest and every object before import:

```bash
conda run -n aletheia python scripts/freeze_legacy_snapshot.py verify manifest.json \
  --snapshot-store /restricted/legacy-cas
```

Issue an engineering-only receipt after review:

```bash
conda run -n aletheia python scripts/freeze_legacy_snapshot.py receipt manifest.json \
  --snapshot-store /restricted/legacy-cas \
  --target-scope quest/example \
  --imported-by migration/operator \
  --importer-code-sha256 <64-lowercase-hex>
```

The printed `LegacyImportReceipt` is an unsigned, content-addressed snapshot-verification and
target-scope-intent record.  `verification_status=accepted` means the issuing builder rehashed the CAS;
it does not prove issuer identity, write anything into the target scope, persist an import, or enforce
`import_key` uniqueness.  Those transactional authorities belong to PR-2's event store.  The receipt is
not an observation-admission command; a later kernel must require a new, independent validation path
before any imported bytes can affect a claim or belief state.

The tracked PR-0 instance follows this CLI flow. After the PR-0 freezer stabilized, its exporter was run
from a separate clean Git repository over the already reviewed `run_projections.v1.json` and
authoritative endurance report; the freeze, verification, and live-time receipt commands then produced
the tracked instance. `test_frozen_legacy_snapshot_v1.py` verifies the current freezer source hashes,
CAS, binding, manifest, and receipt. The receipt targets only `migration/pr0-compatibility` and grants no
scientific-admission or training authority.

The older run/source projections and endurance capture provide strong frozen-byte and internal-chain
consistency, but their claimed historical PostgreSQL/workspace origin is operator-attested rather than
cryptographically proven.  The tracked snapshot copies those reviewed projections; it does not upgrade
their source-origin assurance. `origin-assurance.v1.json` machine-binds this limitation to the exact
fixture bytes so consumers need not infer it only from prose.

## Golden projection policy

The tracked run projection stores terminal state, event type counts/order digest, artifact tree digest,
roles, sizes, and exclusions.  Its separately pinned source manifest stores the complete event-type
sequence plus each admitted artifact's normalized relative path, role, byte length, and SHA-256.  Tests
recompute every projection summary from that manifest offline.  Neither file stores event payloads or
artifact bytes.  Original event rows for these runs are unkeyed/unhashed; only the sanitized projection
and source manifest receive new content identities.

The v1 snapshot receipt transitively binds the projection summary and endurance report, not the larger
`run_projection_sources.v1.json` recomputation witness. That witness remains a separately pinned member
of this exact custody bundle and is jointly checked by the PR-0 gate. A future authoritative import in
PR-2 must issue a new outer receipt that binds both the imported projection and its recomputation witness;
the engineering-only v1 receipt is intentionally insufficient for scientific admission.

The artifact tree algorithm sorts normalized relative paths and hashes a canonical list of
`{relative_path, role, sha256, size_bytes}`.  It excludes `payload.json`, `job.log`, transcripts,
`__pycache__`, bytecode, and generated Python.  `model.joblib` is read only as bytes for hashing and is
never loaded.  Exact ML values and latency are not cross-environment golden assertions because the
legacy scientific dependency set is not fully pinned.

Never refresh this file from a live run in place.  Capture a new explicit version, review all excluded
classes and outcome coverage, then update the new fixture and its content hashes together.

## Filesystem and recovery threat boundary

PR-0 assumes trusted, local, single-writer source and snapshot-store directories.  Static symlink
components are rejected, but v1 does not use a directory-fd/openat protocol against a concurrent
attacker renaming intermediate directories.  Do not place either root in an adversarial shared
filesystem.  A later hardened artifact store must provide its own custody/fencing boundary.

CAS objects and manifests are written before the source-version binding.  A failed conflicting freeze
can therefore leave unreferenced immutable objects or a manifest, but cannot rebind the accepted
version.  Do not delete a binding to hide a conflict.  Quarantine the store, verify every accepted
binding, and only garbage-collect objects unreachable from verified bindings under a separately
reviewed maintenance procedure.  V1 reads each requested object into memory, so requests must contain
reviewed compact exports; bulk datasets remain external content-addressed custody pointers until the
streaming PR-4 artifact store exists.

## Endurance evidence policy

`tests/fixtures/legacy/v1/endurance/manifest.json` and `report.json` are the authoritative historical
objects.  The report disposition is `blocked`, with one blocker:
`structural_pivots:minimum_not_met:0/1`.  `checkpoint-identities.json` is explicitly classified as an
authoritative-source projection: it retains the declared identities, chain links, timestamps, counts,
and durable references, but raw observation/evidence payloads were excluded, so individual checkpoint
object hashes cannot be independently reconstructed from this bundle.  The derived interpretation can
explain operational durability but cannot remove the blocker, set `real_72h_passed`, supersede the
report, or modify source hashes.

## Current limits and next cut

There is no selected materials `results_rejected` projection in v1; the fixture records a materials
completed path plus rejection paths in two structurally different domains.  Adding one requires a new
reviewed fixture version, not an in-place live refresh.

Alembic `migrations/**/*.py` files are executable production inputs and therefore participate in the
single-driver import boundary. Their schema DDL and historical backfill DML are governed as immutable,
separately reviewed deployment revisions; they are intentionally excluded from the runtime legacy
scientific write-owner graph rather than mislabeled as controller-side scientific mutations.

The PR-0 gate and both full regression partitions passed on 2026-08-23: `148 passed` in the focused
migration suite, `154 passed` in the complete PR-0 gate, `1473 passed, 2 skipped, 29 deselected` in the
non-Docker partition, and `29 passed, 1471 deselected` in the real Docker partition.  PR-1 may now add
pure charter/problem/action/event/transition schemas and a deterministic reducer under
`research_kernel`.  PR-1 must remain free of database, model, scheduler, domain, and execution side
effects.
