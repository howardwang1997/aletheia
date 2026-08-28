# ADR 0082: qualify ARL-1 from cumulative replayable evidence

- Status: Accepted
- Date: 2026-08-28

## Context

The repository contains protocol compilation, qualification-only execution, independent
validation/admission and deterministic Kernel replay. Individual demonstrations do not establish
that the deployed system reliably executes a given protocol, and an Alembic revision stamp does
not establish that the live database actually has the expected authority structure.

Calling either condition “ARL-1” would turn source availability or a stamped database into a
system-capability claim.

## Decision

1. Runtime database entry points require both the exact Alembic head and zero ORM structural drift.
2. ARL is cumulative: ARL-1 evidence contains every ordered ARL-0 integrity gate.
3. A qualifying protocol campaign must recompile canonically, name one exact WorkOrder node with at
   least two preregistered reexecutions, retain evidence for every declared replicate, use a
   validator frozen before execution, admit and incorporate the validated observation, and derive
   one deterministic report.
4. The exact destructive PR-8h target receipt is embedded and natively replayed; source tests do not
   substitute for it, and target qualification must complete before protocol execution starts.
5. An independent evidence-verifier principal freshly rehashes and replays sources. A separate
   qualification principal signs only the exact retained verification receipts and full bundle;
   no verification receipt may predate its completed evidence.
6. The signed receipt is scope-specific, time-bounded and carries a non-overridable engineering
   claim ceiling. It grants no scientific authority and cannot claim autonomous design, scientific
   validity or independent replication.
7. Source replay/signing, qualification signing and later audit are distinct one-shot Linux
   operations. They consume byte-pinned canonical manifests; the first two load only their own
   0400 key, while the auditor loads no private key and uses a third application principal.
8. A running preregistered execution is represented by one signed, typed terminal-material-pending
   source result. The campaign retries only that result, at the signed bounded interval and no later
   than the scientific authorization's admission deadline; integrity failures remain terminal.
9. Qualification and audit time come from the deployment-pinned PostgreSQL clock after fresh
   replay. Operator manifests authorize only a bounded 24-hour invocation window and receipt
   duration; they cannot supply the claimed qualification or observation timestamp.

## Consequences

The repository now contains the concrete PostgreSQL/CAS/Kernel/F9-v2 source verifier and guarded
campaign/prepare/issue/verify entrypoints, so no recording adapter is required for the production
path. It can express and verify what would constitute ARL-1 without awarding itself the level. A
real receipt remains impossible until the Linux target campaign and production scientific protocol
campaigns are retained and independently replayed. This cost is intentional: qualification is a
statement about a deployed system, not about how many source tests pass.
