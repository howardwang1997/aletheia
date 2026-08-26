# Architecture decision 0065: Frozen protocol-compilation RPC service

- Status: accepted for the PR-7h source composition
- Date: 2026-08-26
- Scope: `COMPILE_PROTOCOL`

## Decision

The first concrete protocol provider is a deployment-frozen exact-action catalog, not a model or a
generic author callback. Each canonical entry binds one authorized action object hash and action
kind to the hash and complete bytes of one `ProtocolCompilationRequest`. Entries are unique and
canonically ordered. The protocol author must be permitted by the existing compilation policy.

At runtime the provider selects only the exact action hash. It returns the frozen request under the
fresh context hash and a current aware preparation time. Missing actions produce one typed blocker;
kind mismatch, graph/catalog/compiler drift, or request mutation fails closed. The existing pure
compiler remains the only producer of work-order or blocker output.

The durable compilation service gains a separate preparation-verification port. A new request is
verified before compilation. On an exact retry, the service rebuilds a synthetic preparation from
the stored request and requires the currently deployed verifier to resolve the same template. This
keeps an old or concurrently won row from silently escaping a changed provider closure.

`aletheia.research_controller_protocol_compilation_runtime` is the guarded-loader factory for
exactly one `compile_protocol` operation. Its duplicate-free canonical config pins database URL
hash and Alembic revision, read-only Kernel trust root/CAS custody, authority and compilation
policy, complete template-provider policy/catalog, exact provider source path and SHA-256,
controller/worker identity, service pin, and preparation time. It loads no signing key, execution
port, mutable template registry, or model callback.

## Consequences

- The source-complete baseline can compile only already reviewed, exact-state templates. It is not
  general protocol generation, novelty assessment, causal design, or adaptive experiment planning.
- Updating or adding a template requires a new canonical config byte hash and deployment pin; an
  in-process provider cannot mutate the catalog.
- Blocked canonical compiler results remain valid planning evidence. An absent template is an
  operational provider blocker and must not be fabricated into a compiler result.
- This closes the safe baseline provider/factory gap, not the service account, socket/PostgreSQL
  ACLs, transport-key custody, supervisor/alerting, or live Linux multi-process campaign. PR-7i and
  PR-7j subsequently close the exact-template execution-authorization signer and atomic
  registration service; PR-7k subsequently closes verified raw-run loading and PR-7l database
  observation attestation. Four other PR-7e service factories remain.
