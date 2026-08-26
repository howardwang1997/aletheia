# Architecture decision 0060: Durable continuation assessment and custody

- Status: accepted for the continuation-assessment service source slice
- Date: 2026-08-26
- Scope: `DERIVE_CONTINUATION`

## Decision

The continuation step no longer trusts a caller-supplied observation-projection hash. A shared pure
function reconstructs the projection from the exact signed validation receipt and the audited
`observation_incorporated` payload. Its observed-outcome identity hashes the committed F9-v2
campaign projection, validation batch, outcome bin, scientific observation, and mapped outcome
under a frozen identity-policy hash. Restart recovery performs the same reconstruction.

Before calling an assessor, the service locks and fully audits the Quest, reloads the applied action
from Kernel CAS, recomputes the accepted compiler request/result, verifies the committed validation
and admission rows, and proves that the incorporation event is the current tail and exact
non-droppable action evidence. It exposes only a closed context containing the world model,
observation projection, source hashes, excluded prior authority principals, and a deployment policy.

The policy pins the assessment implementation, allowed assessor principals, allowed fit-rule
hashes, and observed-outcome identity scheme. The provider returns only canonical per-prediction
fit assessments. It cannot access execution, sign a Kernel command, admit an observation, or invoke
legacy continuation/optimization control flow. Its principal must differ from proposal, Kernel
authorization, execution, qualification, validation, admission, and runtime-terminal authorities.

The service persists assessor principal, implementation, policy, assessment-source hash, and time
as `ContinuationAssessmentProvenance`, derives the disposition with `derive_continuation_v2`, then
locks and re-audits the entire source before appending one `research_continuation_receipts` row.
Exact retries and restarted workers rederive and verify the durable winner without invoking the
provider. Missing active-hypothesis fits become the typed `redesign_observable` disposition; an
unavailable assessor is an operational blocker rather than a fabricated scientific result.

## Consequences

- The durable receipt can no longer be rebound to an arbitrary observed-outcome or observation
  projection hash while still passing controller recovery.
- Concurrent variants converge under the Quest lock and the registry's per-slot/action uniqueness;
  the row remains an operational projection, not a second scientific ledger.
- The step returns receipt/provenance/assessment-artifact hashes and sets neither Kernel-command nor
  observation-admission authority flags. A later proposal remains unsigned and separately reviewed.
- This slice does not commission the production assessor RPC, fresh byte custody for referenced
  assessment artifacts, database ACL, controller/worker manifest policy composition, full worker
  factory, target-host deployment, or multi-process kill/restart campaign.
