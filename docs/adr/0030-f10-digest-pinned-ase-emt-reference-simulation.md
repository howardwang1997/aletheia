# ADR 0030: Digest-pinned ASE/EMT reference simulation before DFT promotion

Date: 2026-08-15
Status: Accepted

## Context

F10 needs a reproducible simulation boundary, but the engineering claim and the scientific-method
claim are different. A container can be secure and replayable while its calculator is physically
too approximate for the eventual research question. Conversely, selecting a DFT package does not
by itself provide convergence policy, pseudopotential identity, raw-output retention, timeout,
failure semantics, or an independently recomputed reference calibration.

[ASE](https://ase-lib.org/index.html) provides a stable workflow and calculator interface. Its
[EMT documentation](https://ase-lib.org/ase/calculators/emt.html) describes the bundled
pure-Python effective-medium-theory calculator as suitable for demonstrations and tests, not as a
general production materials method. The ASE
[equation-of-state documentation](https://docs.ase-lib.org/ase/eos.html) contains a compact Cu fcc
example with a reference conventional lattice constant of 3.589825 Å. That makes EMT useful for
calibrating execution, checkpoint, parsing, validation, and replay without pretending that the
result is first-principles evidence.

The reviewed DFT candidates remain viable future executors. [GPAW's installation
guide](https://gpaw.readthedocs.io/install.html) requires ASE plus a compiled numerical stack, and
[Quantum ESPRESSO](https://www.quantum-espresso.org/documentation/) requires an external
plane-wave/pseudopotential workflow. Promoting either one responsibly also requires a frozen
pseudopotential/basis contract and convergence studies that are outside this reference-calibration
slice.

## Decision

1. Implement the first F10-S5 execution boundary with ASE 3.29.0 and the pure-Python EMT
   calculator. Freeze the evidence scope as `classical_potential_reference_calibration`; DFT,
   experimental, transferability, causal, and mechanism claims are schema-forbidden.
2. Bind execution to final OCI image ID
   `sha256:54190c4fdf338fa4cf342f11f573593d47a623fabfe9c34f0828b8cac29b4b24`,
   platform `linux/arm64`, worker source hash, host-executor hash, parser/validator source hash,
   runtime versions, exact job bytes, calculator, scan, quality policy, and reference result.
3. Run a named container with no network, a read-only root filesystem, all Linux capabilities
   dropped, `no-new-privileges`, uid/gid `65532:65532`, init, 32 processes, 256 MiB memory, one CPU,
   and a ten-second wall-clock timeout. Allow only one read-only job mount and one bounded output
   mount.
4. Scan five volumes over ±4% and atomically replace `checkpoint.json` after every completed energy
   evaluation. Retain input, checkpoint, result or failure, stdout/stderr, Docker state, and cleanup
   evidence in a content-addressed archive. Enforce an output-file allowlist, regular-file/no-symlink
   checks, a four-file worker-output limit, and an 8 MiB byte limit.
5. Reopen retained bytes after execution. The parser checks artifact byte count/hash, job and
   checkpoint lineage, result self-hash, runtime, calculator, scan, and ordered evaluations. The
   validator derives execution, parse, completeness, monotonic-volume, bracketing, interior sample
   minimum, residual, bulk-modulus, runtime, calculator, and exact gold checks.
6. Treat timeout, quota, infrastructure, unsupported-element, malformed-output, numerical-quality,
   and gold-mismatch outcomes as invalid or blocked execution states. None is a physical negative
   result.
7. Keep protocol history append-only. The v1 bind-mount infrastructure failure remains retained.
   v2 supersedes its exact protocol hash and changes only the host scratch parent from the unshared
   macOS system temporary root to the workspace-backed archive parent.
8. Require two distinct successful container attempts and exact result-payload agreement for this
   deterministic reference. Explicitly record that same-image, same-worker, same-parser, and
   same-validator repetition is not independent implementation replication.
9. Publish the capability as `provisional` with exploratory evidence only. Parser and validator are
   separate deterministic roles, but they currently share one agent-authored source module and
   lack independent promotion review. Therefore the registry rejects them by default unless the
   caller explicitly allows provisional capabilities.

## Rejected alternatives

- **Call EMT a DFT adapter.** EMT is a classical empirical potential. Relabeling it would make the
  engineering demonstration scientifically false.
- **Start directly with GPAW or Quantum ESPRESSO.** That would add compiled-runtime,
  pseudopotential, basis, k-point, and convergence choices before the failure-retention boundary was
  calibrated.
- **Use the host Conda environment as the evidence runtime.** It is useful for unit tests but does
  not supply the frozen network/resource/filesystem boundary used by a real run.
- **Trust a mutable image tag or Dockerfile alone.** The executed artifact is the final image ID;
  the tag and recipe are descriptive metadata, not sufficient identity.
- **Use `docker run --rm`.** The observed Colima runtime left a completed process with inconsistent
  container lifecycle state. Exact named-container inspection followed by explicit removal makes
  state and cleanup auditable.
- **Overwrite the failed v1 attempt.** Infrastructure failure is evidence about the execution
  boundary. Append-only supersession preserves the diagnosis and prevents retrospective cleanup of
  an unfavorable run.
- **Promote after exact same-image repetition.** Deterministic replay detects drift; it does not
  provide independent software, method, operator, or scientific replication.

## Consequences

- Aletheia now has a real, bounded simulation job lifecycle with raw evidence, checkpoint recovery,
  typed failures, independent recomputation of declared checks, and exact physical replay.
- The Cu result calibrates only the frozen ASE/EMT implementation. It cannot validate EMT for other
  elements or structures and cannot support a materials mechanism claim.
- The final image is presently local and arm64-specific. The Dockerfile pins its base image but pip
  wheel resolution is not hash-locked; rebuilding may produce another final image ID. Portable
  promotion requires an OCI registry digest, SBOM, signature/provenance, and multi-platform policy.
- Registered status still requires a separately implemented or independently audited validator,
  adversarial/promotion receipts, domain review, and safety review.
- A DFT successor must be a new append-only capability/protocol version with complete
  pseudopotential/basis/k-point/convergence identities and reference systems; it must not silently
  widen this capability's claim ceiling.
