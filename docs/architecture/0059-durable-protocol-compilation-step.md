# Architecture decision 0059: Durable authorized protocol-compilation step

- Status: accepted for the protocol-compilation service source slice
- Date: 2026-08-26
- Scope: `COMPILE_PROTOCOL`

## Decision

The controller's protocol-compilation step uses a powerless provider and an append-only registry.
Before disclosing a request context, the service locks and fully audits the exact Research Kernel
Quest, reloads the authorized action from CAS, and proves the adjacent `action_proposed` and
`action_authorized` events, current stream version, tail, snapshot, branch, and question binding.

A deployment pin fixes the permitted protocol-author principals, action-kind-to-protocol-category
mapping, action kinds that require a graph-scoped F9-v2 world model, capability and resource
catalog hashes, and compiler implementation hash. The provider may return only a complete
`ProtocolCompilationRequest` under that disclosed context. It cannot authorize execution, mutate
the Kernel, admit observations, or receive a signing key.

The service validates the provider request against the pin, executes the pure canonical compiler,
and verifies the result. It then opens a second transaction, locks and re-audits the same Kernel
context, rechecks an existing winner, and appends the exact request, result, compiler receipt, and
protocol revision lineage to `research_protocol_compilations`. A revision must resolve the exact
immediately preceding version and its stored request/result must recompile canonically.

Both accepted and blocked compiler results are durable. An exact retry or restarted worker reloads
and re-verifies the registered winner before contacting the provider. Concurrent variants converge
under the Quest lock and the registry's action/version uniqueness constraints. A blocked compiler
result is a scientific-planning input for the next redesign tick; it is not an execution failure.

## Consequences

- Stale recovery projections, rebound CAS actions, policy/catalog/compiler drift, invalid action
  categories, missing required world models, and broken revision chains fail closed before a
  controller receipt is emitted.
- A successful step returns only compilation/request/result/receipt hashes and sets neither Kernel
  command nor observation-admission authority flags.
- The registry is a recoverable compilation projection, not a second scientific ledger and not an
  execution authorization.
- ADR 0065 subsequently adds the exact-action frozen-template provider, a verifier applied to new
  and restarted rows, and the checked-in single-operation PR-7e factory. This is a safe preauthored
  baseline, not general or knowledge-grounded protocol generation.
- This slice does not commission the provider service account, RPC/receipt custody, database ACL,
  independent Kernel signer, target-host deployment, or process-kill campaign. Those deployment
  gates remain before the worker is production-ready.
