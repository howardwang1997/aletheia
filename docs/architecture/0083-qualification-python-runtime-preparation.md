# ADR 0083: Materialize and close the qualification Python runtime before deployment

- Status: Accepted
- Date: 2026-08-29

## Decision

Prepare the qualification Python runtime as a distinct, explicit and non-authoritative stage.
Never install an ordinary Conda prefix directly as the reviewed runtime. The preparer must
materialize symlinks, break hardlinks, normalize immutable root custody, copy a request-pinned
loader/glibc closure, patch Python to that in-tree loader, and reject any native mapping outside
the resulting tree during a real no-site dependency probe.

The five deployed services set the C locale and continue to prohibit `LD_LIBRARY_PATH` and
`LD_PRELOAD`. Third-party packages are appended by the guarded bootstrap only after stdlib
initialization, without executing `site`, `.pth`, or `sitecustomize` hooks.

## Consequences

- The exact prepared tree is larger than the minimal package payload because every alias becomes
  an independent entry; this is intentional evidence simplicity.
- Host glibc bytes become reviewed deployment inputs rather than invisible ambient dependencies.
- A new Conda solve, host library update, source mutation, or relocation changes the exhaustive
  tree hash and requires a new reviewed deployment request.
- A preparation receipt proves no deployment or scientific claim. PR-8f through PR-8h and the
  ARL-1 independent audit remain mandatory.

