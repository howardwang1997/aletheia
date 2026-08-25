# Architecture decision 0055: Qualification-gated evaluation runtime and atomic SEA registration

- Status: accepted for the PR-6 production-boundary source slice
- Date: 2026-08-26
- Scope: legacy-evaluation container entry, PR-4 launch binding, and `REGISTER_EXECUTION`

## Decision

The PR-6 compatibility leaf runs in a fixed-path, no-argument handler behind the existing PR-4
qualification launch gate. A deployment constructs one exact `PinnedLaunchSpec` from the fully
validated `ExecutionIntent`, invocation receipt, harness, capability manifest, and executable
digest. The inner handler is intentionally narrower than the outer adapter: PR-4 has already bound
the WorkOrder, resource, retry, network, input, and output policy, so the container can only reopen
the fixed invocation/table, fresh-rehash the reviewed source closure, and call `featurize` plus
`train_evaluate`.

The checked-in Dockerfile is a build candidate, not host evidence. It pins the base image digest
and a complete application-dependency version constraint set observed on the Linux/arm64
candidate, then installs source, config, launch-gate, and handler paths in immutable image layers.
Deployment must still freeze the built OCI manifest/config and executable digests and run the exact
Linux/root/systemd/loop/ext4/rootful-Docker qualification campaign.

The controller's `REGISTER_EXECUTION` step calls an external SEA signer bound by its adapter
manifest. The worker holds no SEA or runtime-control private key. It verifies the signed SEA using
the action, qualification, and bridge public authorities, then appends the SEA and calls PR-4
`admit_and_reserve_in_session` inside one PostgreSQL transaction. A failure at either boundary
rolls back both. The receipt omits the raw lease token and remains
`qualification_only=true` / `scientific_admission_allowed=false`.

## Consequences

- A crash cannot leave a newly reserved attempt without its earlier durable SEA registration.
- A verification-only allocator may admit the exact work but cannot issue launch, termination, or
  artifact runtime controls; those remain in a separately deployed signer role.
- Engineering success and raw artifacts still cannot become an observation without independent
  validation, DB-time admission, and atomic Kernel incorporation.
- The source/test slice does not qualify an image or host and does not commission external issuer,
  validator, admission, monitoring, or process-supervision services.
