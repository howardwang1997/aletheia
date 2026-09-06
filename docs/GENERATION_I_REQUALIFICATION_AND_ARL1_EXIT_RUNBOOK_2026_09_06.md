# Generation-i re-qualification and ARL-1 exit runbook (2026-09-06)

- Status: operator runbook for Horizon-1 items 1–2 of
  [`LONG_TERM_ROADMAP_TO_ARL4_2026_09_06.md`](LONG_TERM_ROADMAP_TO_ARL4_2026_09_06.md); no code or
  authority change
- **2026-09-06 update: Phases A–C are complete.** The operator executed them on 2026-09-05 as
  generations `20260904i` (fail-closed at PR-8g commissioning: `pre-existing PostgreSQL role has
  variant authority` — the live incident behind #144) and `20260904j` (frozen from `3e65cca`,
  full chain on fresh isolated database, byte-identical exact-retry receipt
  `qtx_5a6fd1d4c725f990507c07cdf5b7d713`, `deployment_qualified=true`, completed
  `2026-09-05T05:37:20.406653Z`). See the PR-8h guide's generation-i/j record for the authoritative
  detail. **Phase D (ARL-1 exit steps 4–9) is the remaining work**; all Phase-B/D identity notes
  below now refer to generation j
- **2026-09-06 update 2: Phase D step 4 on j is blocked by a fail-closed commissioning-window
  expiry.** The scientific host foundation for j completed cleanly (receipt
  `0d20c084fe35b764afb1bd89fbf38bf617b750c62aca399447759176ee3d3a8a`: identities 2300–2309, shared
  group `aletheia_arl1_shared_j`, verified database ACL including the `UPDATE` row-lock grants
  generation e died on, `acl_verified=true`, all six role peer probes passed), but the scientific
  campaign composition stopped at `qualification custody authority is inactive at preparation
  time`: the commissioning foundation pins custody authorities valid only
  `2026-09-05T03:31:17Z → 2026-09-06T03:31:17Z` (24 h), and composition began `2026-09-06T03:42Z`
  — 11 minutes past expiry. There is no authority-renewal flow, and per
  `ARL1_PROTOCOL_EXECUTOR_QUALIFICATION.md` a deployment may not borrow another arc's
  commissioning; steps 4–9 therefore require a freshly commissioned generation (repeat steps 1–3
  on fresh identities, then execute Phase D inside the new 24 h window). Lesson: Phase D must
  follow Phase C immediately; the window is measured from commissioning, not from
  qualification.
- Scope: freeze current `main` as a new deployment generation, re-qualify it on the retained Linux
  target through PR-8f→8g→8b→8h, then execute ARL-1 exit steps 4–9
- Scientific authority: none. Every phase below keeps `qualification_only=true` and
  `scientific_admission_allowed=false`; the ARL-1 receipt claim ceiling stays
  `bounded_protocol_execution_engineering`

## Why a new generation is required

Generation `20260904h` qualified only the frozen `e0dc06c` deployment. Post-h merges (#142 launch
window/reconciliation, #143 timezone binding, #144 role-config convergence) deliberately altered
unit bytes, the ACL and the commissioning chain. Per the PR-8h guide, the hardened source "must
receive a new freeze and target qualification before it can claim the same result". This runbook
sequences that re-qualification and the ARL-1 exit on top of it.

PR-8j is closed and does not gate this sequence: the 2026-09-04 authorized retirement superseded
its remaining invocation (see the closure section of
[`PR8J_ATTEMPT_SCOPED_PRE_RUNTIME_CLEANUP.md`](PR8J_ATTEMPT_SCOPED_PRE_RUNTIME_CLEANUP.md)).

## Preconditions verified on 2026-09-06

- `main` (`3e65cca`) CI green on the last five merges; focused gates green locally: PR-8j list
  131 passed, ARL-1/qualification suites 241 passed, shared-custody regressions 57 passed;
  `pip check` clean. Skips are the destructive `ALETHEIA_DATABASE_URL`-gated tests that CI runs.
- Schema head is `20260903_0032`; a fresh generation database migrates directly to it. No new
  migration is required for any phase below.
- The prepared PR-8i runtime tree (manifest
  `9904a0cfa1cf7d49c0201f5a614ded78efadc0458157f38c6601600303ab1836`, 1,826 directories, 23,810
  files, 875,137,883 bytes) is unchanged by #143/#144 — those are source-level probe/renderer
  changes only. Re-evaluate it read-only against the new release's stricter probes (in-tree
  `share/zoneinfo`, `PYTHONTZPATH`, `TimeZone=UTC` session default) before embedding it in the new
  spec; do not re-prepare from scratch unless that re-evaluation fails.

## Phase A — freeze

1. Land this runbook and the PR-8j closure on `main`; require green PR CI and green main CI.
2. Freeze the resulting merge commit into the deterministic source archive per the established
   generation convention; record the archive SHA-256 and the release-freeze receipt SHA-256.
3. Install the archive read-only on the qualification target and verify it against the reviewed
   Python tree, as for every prior generation.

## Phase B — target preparation

1. **Retire the five generation-h units first.** They remain enabled and active by explicit
   receipt design, and the campaign host's closed-system check rejects any live
   `aletheia-qualification-*`/`aletheia-arl1-*` service outside the request's exact five units.
   Repeat the authorized-retirement pattern from 2026-09-04: bind all five units to the exact h
   manifest and release bytes, require their observed final activation state, then stop and
   disable exactly those five; remove any exited h container by full ID; unmount h's output mounts
   and workspace binds ordinarily; detach its loop devices only with no remaining mountpoint.
   Preserve all h journals, manifests, unit files, backing images and database; do not rewrite the
   h allocator database.
2. Confirm the twenty `20260831z`/`20260901a`/`20260901b`/`20260901e` units are still
   `disabled` (they were disable-only retired on 2026-09-04; the sibling check must see none
   enabled/static/indirect).
3. Create the fresh isolated generation database; migrate to `20260903_0032`; verify the exact
   schema head and `require_schema_exact()`.
4. Run PR-8f bootstrap, then PR-8g commissioning, then PR-8b installation under fresh non-reused
   identities. **PR-8g note:** this is the first live execution of the #144 monotonic role-config
   convergence path (see `tests/execution/test_qualification_authority_commissioning.py`); the
   commissioning receipt chain must show the converged role configs, and any post-h ACL or runtime
   change discovered here means repeating Phases A–B rather than borrowing h's qualification.
5. Re-evaluate the PR-8i tree read-only (preconditions above); embed its manifest in the
   generation-i spec. The rendered units will carry `TimeZone=UTC` and the in-tree
   `PYTHONTZPATH` — new bytes relative to h, which is exactly what this generation qualifies.

## Phase C — PR-8h target campaign

Freeze the campaign request and canonical plan; run the observer and apply sequence exactly as for
generation h. Required outcome: one complete receipt with `deployment_qualified=true`, all ten
destructive scenario evidences, and byte-identical exact-retry stdout. Record execution/attempt/
slot identities, journal SHA-256 values, and the receipt ID in the PR-8h guide, following the
generation-h record format. A fail-closed stop at any boundary is negative engineering evidence;
record it and repair at source — never adapt the target to pass.

## Phase D — ARL-1 exit steps 4–9

**Status 2026-09-06: blocked on j (see the top-of-file update 2) — requires a fresh generation
arc before these steps can run.**

Execute [`ARL1_PROTOCOL_EXECUTOR_QUALIFICATION.md`](ARL1_PROTOCOL_EXECUTOR_QUALIFICATION.md)
steps 4–9 unchanged, on the qualified generation-i deployment:

1. Execute each policy-required given protocol with every preregistered exact reexecution through
   the production controller, bounded terminal-material wait, validator, admission and Kernel
   incorporation path.
2. Retain the all-attempt and evidence-archive manifests plus deterministic reports.
3. Run each byte-pinned given-protocol campaign with
   `scripts/run-arl1-protocol-campaign.py --apply --acknowledge RUN_ARL1_PROTOCOL_CAMPAIGN`,
   retaining canonical stdout and its SHA-256.
4. Compose the phase manifests from Phase B/C commissioning outputs only. Apply the recorded
   composition lessons from generation h's r1–r3 revisions: canonical model bytes are not the
   stdout-with-LF artifact; root-private mode-`0400` files must be pinned with their real file
   type; parse only the exact one-line canonical model.
5. `prepare` under the source-verifier principal (`PREPARE_ARL1_EVIDENCE_BUNDLE`), `issue` under
   the disjoint qualification principal (`ISSUE_ARL1_QUALIFICATION`), restart from empty process
   memory, then `verify` under the third keyless auditor principal
   (`VERIFY_ARL1_QUALIFICATION`).
6. Tamper one byte in every retained source class and require `verify` to fail each time.

The first receipt that passes step 5 is the first issuable ARL-1 qualification; update the PR-8h,
ARL-1 and README status paragraphs at that point, and record the receipt's expiry and scope
limits verbatim.

## Non-goals

- No new cleanup authority form, no relaxation of live-custody verification (PR-8j closure).
- No scientific admission, no portfolio activation, no controller authority expansion. Those
  remain Horizon-2 work gated on this exit.
