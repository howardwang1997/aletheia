# PR-7f deterministic continuation service

- Status: source composition complete; target-host commissioning pending
- Date: 2026-08-26

## What is now runnable

The PR-7e server can load
`aletheia.research_controller_continuation_runtime.build_continuation_assessment_rpc_service` for a
single `derive_continuation` endpoint. The service replays the durable Kernel,
compilation/validation/admission chain, applies the closed exact-outcome-bin fit rule, writes
content-addressed assessment artifacts, and returns the existing durable continuation receipt.

The deployment config must be canonical JSON without duplicate keys. It pins the exact worker and
controller, service pin, powerless continuation authority, policy, database URL hash/schema
revision, read-only Kernel CAS, assessor source bytes, and artifact-root inode/ACL. The assessor has
no model callback and no signing, execution, admission, or Kernel mutation authority.

Prediction authors opt into mechanical assessment with:

```python
predicted_outcome_sha256 = exact_outcome_bin_prediction_sha256(
    observable_spec_sha256=observable.observable_sha256,
    measurement_protocol_sha256=method.method_contract_sha256,
    outcome_space_sha256=analysis_plan.outcome_space_sha256,
    outcome_bin_id="outcome.negative",
)
```

The bin must also be present in the signed admission policy. Opaque identities, ambiguous
same-context predictions, and missing predictions do not count as contrary evidence; they request
observable redesign.

## Local verification

Focused tests cover recognized same/different bins, opaque and ambiguous predictions, canonical
artifact publication, same-path byte tamper, root inode/mode rebinding, restart rehash without
provider reinvocation, duplicate/rebound config rejection, exact operation partitioning, and the
guarded PR-7e runtime loader. The complete controller suite also exercises the existing signed
vertical cut.

## Remaining release gates

This is checked-in source composition, not host evidence. A provisioned Linux service account must
still demonstrate exact socket, PostgreSQL, CAS, artifact-root, supervisor, and alert custody in the
PR-4/PR-5 campaign. PR-7g, PR-7h, PR-7i and PR-7j subsequently supply the conservative
action-proposal, frozen protocol-compilation, exact-template execution-authorization, and atomic
registration factories; the other six PR-7e service factories, including raw-run loading,
validation, admission and Kernel signing, remain uncommissioned.

See [ADR 0063](architecture/0063-deterministic-continuation-rpc-service.md), the
[PR-7e server guide](PR7E_EXTERNAL_RPC_SERVICE_RUNTIME.md), and the
[PR-7d worker guide](PR7D_COMPLETE_CONTROLLER_WORKER.md).
