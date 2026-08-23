# ADR 0045: PR-0 legacy freeze and scientific-authority boundary

- Status: Accepted
- Date: 2026-08-23
- Scope: Migration prerequisite for the end-to-end autonomous research architecture

## Context

The current product can execute useful research workflows, but its scientific state is distributed
across `ExperimentDriver`, a fixed stage machine, mutable ledger rows, unkeyed events, workspace
files, domain plugins, and external side effects.  Adding a second controller directly on top of
those writers would create two incompatible authorities: both could change what the system believes
and neither could be replayed as the unique history of a Quest.

The replacement architecture therefore needs a migration boundary before it needs a new planner or
another experiment.  Historical behavior is valuable compatibility evidence, but it cannot silently
become an observation in the new evidence graph.  The completed 72-hour endurance run is especially
important: its authoritative terminal disposition is `blocked`, and a later interpretation must not
rewrite it to `passed`.

## Decision

1. `aletheia.scheduler.durable` is the only production module allowed to import
   `aletheia.scheduler.driver`; repository scripts must use its explicit compatibility seam, and the
   dynamic worker rejects raw-driver registration.  Direct test imports remain test-only compatibility
   probes.  `ExperimentDriver`, `DomainPlugin`, and `ComputeBackend` are code-marked compatibility
   surfaces. Normalized complete-class AST fingerprints cover all three surfaces; complete driver-module
   and durable-gateway fingerprints cover module helpers and selection. Together with the legacy source
   graph freeze, any legacy-driver body, plugin/backend extension, stage surface, registry/factory
   selection, or production implementation-selection change requires explicit migration review.
2. `research_kernel`, `protocols`, `execution`, `planning`, and `observations` are protected package
   roots.  A standard-library AST check follows their transitive internal import graph and rejects
   legacy driver, stage-machine, mutable ledger/service, event-bus, compute-factory, data-registry,
   domain-control, and conventional AST-visible runtime loading paths, including common
   `importlib`, `importlib.util` file-loader, `runpy`, `pkgutil`, `__builtins__`, and `sys.modules`
   forms.  The reviewed runtime loaders are centralized in one content-pinned guard that rejects raw
   driver module names/source paths and resolved or re-exported driver objects.  Kernel and protocol
   code have additional bans on ORM, operational persistence, and `sys`.  This is trusted-source
   dependency lint, not a Python capability sandbox or a proof against deliberately obfuscated code.
   `research_kernel/__init__.py` is present in PR-0 so the check cannot pass vacuously.
3. Every current SQLAlchemy table (80/80) and the reviewed file/event/external mutation surfaces are
   classified by the 68 entries in `architecture/legacy_write_owners.v1.json`.  Scientific state has
   one future owner, no dual-write policy, a declared cutover PR, and a post-cutover legacy
   mode.  The migration test freezes every current `memory.service` import and the distinct lexical
   scopes containing statically resolvable mutator uses; direct ORM, SQL, file, or external-system
   mutations remain an explicitly reviewed inventory baseline rather than a claim that arbitrary Python
   side effects can be discovered. A normalized-AST backstop freezes the complete 281-file legacy
   production source graph, so added/removed files and semantic source changes require explicit review
   even when a specialized sink classifier does not recognize their syntax. The 27-file migration and
   Alembic source graph is frozen separately.
   Executable Alembic revisions participate in the single-driver import boundary, but their immutable
   schema/backfill authority is reviewed separately and intentionally excluded from the runtime
   scientific write-owner graph.
4. Legacy inputs cross into the replacement system only through explicit, sanitized,
   content-addressed snapshots.  The release CLI binds a clean exporter Git commit/tree to one
   normalized, non-symlink, regular entrypoint tracked at `HEAD`, including that blob's SHA-256.  This
   is a verifiable code declaration; whether that entrypoint actually produced the reviewed export is
   explicitly only `operator_attested`, not a cryptographic provenance claim.  The manifest separately
   records a runtime-byte identity for the CLI and core freezer source files hashed immediately before
   the freeze.  A declared `(source_system, source_scope, source_version)` can bind to only one
   snapshot.  An import receipt is read-only, idempotency-keyed, and fixed to
   `engineering_regression_only`; it disables scientific admission, training use, live refresh, and
   mutation propagation.  PR-0 applies this mechanism to two reviewed sanitized legacy projections and
   tracks the resulting CAS objects, source-version binding, manifest, and import receipt as the v1
   migration instance; the machinery is not accepted only on synthetic temporary-directory tests.
   Release creation is CLI-only because the model layer cannot establish that an external Git object
   exists.  An explicit freezer identity is re-read from its declared source root at the core freeze
   boundary.  `LegacyImportReceipt` is an unsigned snapshot-verification/scope-intent record; PR-0 does
   not claim that it mutates a target scope or transactionally enforces its derived import key.
5. Legacy events that originally have neither `event_key` nor `event_sha256` remain explicitly
   `unkeyed_unhashed`.  The migration may hash a sanitized projection as its new import identity, but
   must not present that hash as an identity that existed on the original event.
6. Golden compatibility evidence has two layers: tracked offline test contracts and sanitized
   projections from completed and rejected legacy runs.  A separately pinned source manifest retains
   the complete event-type sequence and every admitted artifact's relative path, role, size, and byte
   hash, so summaries can be recomputed without the mutable database or workspaces.  Model blobs are
   hashed as opaque bytes and never deserialized.  Prompts, payloads, logs, transcripts, generated
   source, credentials, absolute paths, hidden-evaluator content, and environment-sensitive exact ML
   values are excluded.
7. The real endurance-v1 manifest and blocked report are authoritative frozen objects.  The 73-entry
   checkpoint identity/reference file is an authoritative-source projection: it reconstructs the chain
   asserted by the report but deliberately omits raw observation/evidence payloads, so it is not
   mislabeled as a complete checkpoint object.  Operational interpretation is a separate derived object
   bound to the source hashes with `supersedes=false` and `mutates_source=false`.

## Authority after this decision

PR-0 does not yet make the new kernel authoritative.  It freezes the path by which that authority can
be introduced:

- existing `/runs` remain `legacy_protocol_executor` workflows;
- historical projections remain compatibility-only inputs;
- the event bus remains telemetry/projection, not a scientific commit receipt;
- no new scientific stage or write surface may be added to `ExperimentDriver`;
- PR-1 can add pure kernel contracts without importing mutable legacy state.

## Consequences

The migration is intentionally fail-closed at the new-authority boundary.  Changing a frozen source
version, adding an unreviewed `memory.service` importer, introducing an indirect forbidden dependency,
or modifying pinned endurance evidence causes an offline test failure.  A legitimate behavioral refresh
requires a new explicit fixture/snapshot version and review.  The owner inventory is also the reviewed
baseline for legacy direct ORM/file/external writers; arbitrary new legacy mutations still require code
review until their cutover removes those modules.

Content-addressed snapshot failure can leave an unreferenced CAS object, but cannot rebind or mutate an
accepted version.  Garbage collection is outside PR-0 and must treat version bindings as roots.

The filesystem implementation assumes trusted local, single-writer roots.  Its static symlink checks do
not defend against an adversary concurrently swapping intermediate directories, and its narrow
credential denylist does not replace independent sanitization/custody review.  Large raw datasets remain
external pointers because the v1 freezer materializes each explicitly reviewed object in memory.
The exporter entrypoint binding identifies declared code but cannot prove that those bytes executed or
produced the source export; that link remains a named operator attestation.  Likewise, the freezer
runtime identity hashes source files present immediately before the freeze and is not remote attestation
of interpreter state or loaded bytecode.
Historical PostgreSQL/workspace origin labels on run and endurance projections are also operator
attestations: their frozen bytes and internal hashes are independently checkable, their original capture
environment is not cryptographically reconstructible from this repository. A content-bound
`origin-assurance.v1.json` sidecar makes that limitation machine-readable without mutating the frozen
historical objects.

The AST dependency boundary assumes reviewed Python source using conventional import and runtime-loader
forms.  It is not designed to contain hostile code and does not prove absence of object injection,
pre-populated module-cache access, serialized callable recovery, native-extension loading, or arbitrary
metaprogramming.  Untrusted execution still requires process/container isolation, a narrow capability
surface, and runtime policy enforcement; code review remains required for migration-policy changes.

These fixtures prove engineering compatibility and historical custody only.  They do not prove a new
scientific claim, benchmark generality, autonomous research quality, or successful completion of the
72-hour scientific gate.

## Rejected alternatives

- A direct-import grep: it misses helper modules, lazy imports, and dynamic imports.
- Hashing the entire legacy driver without a write-owner inventory: it detects drift but cannot identify
  who owns each mutation or how that authority cuts over.  PR-0 therefore uses both mechanisms.
- Dual-writing old and new scientific state: it creates two authorities and ambiguous recovery.
- Reading mutable legacy rows as a live graph view: later edits would change historical inputs without
  a new receipt.
- Re-running or editing the endurance gate to obtain a green result: this destroys the meaning of the
  original precommitted gate.
- Freezing only successful materials runs: it hides rejection behavior and overfits the migration to a
  single tabular domain.
