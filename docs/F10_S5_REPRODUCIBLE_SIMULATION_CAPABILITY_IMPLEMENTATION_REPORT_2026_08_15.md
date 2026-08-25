# F10-S5 reproducible simulation capability implementation report

Date: 2026-08-15
Status: Core simulation engineering and classical reference calibration complete; capability remains provisional

## Outcome

Aletheia can now execute a periodic atomistic equation-of-state job in a digest-pinned, no-network,
resource-bounded container; checkpoint every energy evaluation; retain raw input/output/failure and
container lifecycle evidence; independently reopen and validate the retained bytes; exactly replay
the evidence; and compare two distinct physical attempts without calling them independent software
replications.

The formal Cu fcc reference passed all eleven frozen checks twice with exact result-payload
agreement. This completes the reproducible-simulation engineering slice of F10-S5. The capability
is deliberately `provisional / exploratory`: ASE EMT is a classical empirical potential, the
parser and validator share one agent-authored module, the final image is local arm64 only, and no
independent reviewer or DFT executor exists. Those facts prevent registered, first-principles,
experimental, transferability, causal, or mechanism claims.

## Stack decision

ADR 0030 selects ASE as the workflow boundary and its pure-Python EMT calculator as the first
reference engine. ASE's documentation positions EMT for demonstrations/tests, and its equation-of-
state example supplies a compact Cu fcc gold value. GPAW and Quantum ESPRESSO were reviewed as DFT
successors, but either requires a separate pseudopotential/basis/k-point/convergence contract and a
heavier reproducible runtime.

The chosen sequence is intentional:

1. prove job identity, sandboxing, checkpoint, raw retention, failure taxonomy, parse/validate, and
   replay with a cheap deterministic reference;
2. independently audit and promote the execution boundary; then
3. add a new append-only DFT capability rather than silently widening the EMT claim.

## Implemented boundary

### Container worker

`docker/simulation/emt_worker.py` accepts one strict JSON schema with at most 256 periodic atomic
sites, a frozen ASE EMT policy, and an odd 5–31 point stabilized-jellium EOS scan. It rejects unknown
top-level fields, malformed dimensions, nonfinite geometry, degenerate cells, non-3D periodicity,
unsupported calculators/scans, and values outside the safe scan policy.

For each volume it computes energy, forces, and stress, checks finiteness, and atomically replaces a
checkpoint containing the complete prefix of observations. It then fits the EOS, records residuals,
bracketing/interior-minimum signals, and writes a self-authenticating result. Any exception creates a
bounded typed `failure.json`; it does not create a result.

The Dockerfile binds the official Python base by digest and installs ASE 3.29.0. The executed image
is frozen by final image ID, not by mutable tag:

| Runtime object | Frozen value |
|---|---|
| base image | `python:3.11-slim-bookworm@sha256:2e32f7d302adc1c37428355c1e646897c0c53f4fd60b6a551245fb90ee129f91` |
| final image | `sha256:54190c4fdf338fa4cf342f11f573593d47a623fabfe9c34f0828b8cac29b4b24` |
| platform | `linux/arm64` |
| ASE / NumPy / SciPy | 3.29.0 / 2.4.6 / 1.17.1 |
| worker SHA-256 | `d4e5f88056f7770595ea29a9759f953d143b08b4e13f9d095142c871ff4c9b8a` |

### Hardened host executor

`scripts/ase_emt_simulation_e2e.py` provides create-only `execute`, `finalize`, `verify`, and
`compare` stages. Execution requires the exact local image ID and platform and freezes:

- `--init`, network `none`, read-only root, all capabilities dropped, and
  `no-new-privileges`;
- non-root uid/gid `65532:65532`;
- 32 processes, 256 MiB, one CPU, and a ten-second timeout;
- read-only exact job mount plus one workspace-backed output mount;
- an allowlist of checkpoint, result, and failure worker files;
- regular-file/no-symlink, four-file, and 8 MiB output gates; and
- exact named-container state inspection and cleanup evidence.

Raw artifacts are content-addressed before scratch removal. `SimulationRawRun` distinguishes
`succeeded`, `failed`, `timed_out`, `output_quota_exceeded`, and `infrastructure_failure`, retaining
exit/failure/checkpoint status rather than converting operational failure into a property value.

### Parser, validator, and lineage

`aletheia/domains/materials/simulation.py` defines frozen schemas for structure, job, container,
quality, reference, protocol, raw run, worker result, parse result, validation, bundle, and
reproduction receipt.

The parser reopens retained bytes and rechecks receipt byte length/SHA-256, exact protocol/job/image/
worker/executor binding, security receipt, checkpoint prefix, result self-hash, job lineage,
calculator/scan, and runtime. The validator separately derives eleven checks and a claim ceiling.
The bundle validator closes protocol → job → raw → parse → validation lineage, and the replay path
must recreate the exact parse and validation objects from archived bytes.

Parser and validator are distinct functions and roles, but both are in the same agent-authored
source module with the same implementation hash. That satisfies deterministic role separation for
this provisional exercise, not independent validator custody. The manifest marks every role
`agent_authored: true`, carries no registration evidence, and caps evidence at exploratory.

## Append-only protocol correction

The first formal v1 execution was not erased. It failed before the worker because the macOS system
temporary directory was not shared into the Colima VM, so Docker rejected the bind source with exit
125. Finalization retained it as `rejected_execution`:

| v1 object | Identity |
|---|---|
| protocol | `e6245206b59dbfd64b8d5f203311f02d780f2d844e3e39d2cbb8e62122ee14f5` |
| raw run | `ea8bd99f7c4f6696ce46644d2c18afa60b046ec0849fab121f18831f1ed4c7d6` |
| failed bundle | `e7e075f4b9fa6f6717ee661254bc01fb49b8abafa652a2dbb5994b4969bdf785` |
| disposition | `rejected_execution` |

Protocol v2 supersedes the exact v1 hash and explains the correction. It changes only the host
scratch parent to the workspace-backed archive parent; the image, worker, job, scan, gold, quality,
resource, and scientific claim contracts remain unchanged. Exact replay of the failed bundle also
passes, showing that infrastructure failure is a stable evidence state.

## Gold and adversarial tests

The simulation test module covers:

- official Cu result parsing, eleven-check validation, and closed bundle lineage;
- result-payload and checkpoint tampering;
- execution failure versus physical result separation;
- numerical-quality/gold mismatch disposition;
- changed job, worker, executor, parser, or validator binding;
- disposition forgery;
- digest-pinned image and worker-source binding;
- frozen v1 → v2 supersession and source/config drift;
- an actual unsupported-Xe worker failure with no result/checkpoint;
- two distinct validated runs with an honestly bounded reproduction receipt; and
- provisional manifest evidence plus default-deny/explicit-opt-in registry discovery.

At the S5 implementation freeze, the simulation module reports 12 passing tests. The combined
focused and full non-Docker regression results are recorded in the final verification section below.

## Formal v2 evidence

The frozen v2 protocol uses one primitive-cell Cu atom with initial conventional lattice parameter
3.6 Å, five volumes over ±4%, ASE EMT with `asap_cutoff=false`, and the stabilized-jellium EOS model.
The gold check derives the conventional fcc lattice constant as `(4V_atom)^(1/3)` and compares it
with 3.589825 Å at an absolute tolerance of 1e-6 Å. Exact image-local result payload identity is an
additional required check.

| Frozen/evidence object | Identity |
|---|---|
| v2 protocol | `d4e224336fd3a062839eb8ceaba01309aa3b5285a550237a5f3f0721158b22d5` |
| Dockerfile bytes | `f68371753ea14a8d8db45f9c9e3709171769958fca340d73cfcdf0f9d2fe3a23` |
| host executor | `9f21dcf30151208d1feb11147adb253ef6914feb4f9bfba923a346e0467cc463` |
| parser/validator module | `3f77e6ec7edbffb5778fd6d56fb8ebd4c5af477eb7674254373a18bbbed3c1f2` |
| exact job bytes / object | `e1b7f957186fee7340e7d1e108f7a84a69a155b1f9b44752a7590ad211c27bfa` / `4885a3fbe4943c2cd39ea2695909c63b239a4b3a472ef925f3fed526eabcabce` |
| run 01 / bundle 01 | `99e85364d797d7edef2779edd218fcb615b2920e129c13356d9e523e2cf740d3` / `d96e06001c1121166cf03910d453aaa9bcd8ce29d2e3fbe89271298d718af031` |
| run 01 parse / validation | `8de6b55c9bd0d82bd5512123a878ab822d287d50b2a1955986ac1b91b439a2e8` / `3659aa8156cb66abdc7a64760ca23dea160225815c3b78fd4669226d47f33d73` |
| run 02 / bundle 02 | `028d80ca9cea19fada1825eafb776eb66163ccf0a398327b6a1ed48742edbb53` / `31ea0b1056f4ee82368491b2974a05c98d16d395558f7e093e5816c7a8d28b31` |
| exact result payload, both runs | `f8d94b2850f51ec72037521fb87d72546e966968fc2de9845c8dfe2c6c7057f7` |
| reproduction receipt | `263424ae4f8148516b175832c53c4b6ab1dfc87e4a3881f0a9afd38409497a59` |

Both raw archives were independently reopened, every raw artifact rehashed, the checkpoint and
result reparsed, all quality/gold checks recomputed, and each frozen bundle exactly reproduced.

## Reference result

| Quantity | Result |
|---|---:|
| equilibrium volume per atom | 11.565374360310688 Å³ |
| conventional fcc lattice constant | 3.589824595554312 Å |
| reference lattice constant | 3.589825 Å |
| absolute lattice difference | about 4.04e-7 Å |
| bulk modulus | 0.8392121970531606 eV/Å³ (about 134.46 GPa) |
| minimum energy | -0.007036378469896576 eV |
| fit RMSE | 7.715379800753145e-08 eV |
| maximum absolute fit residual | 1.2363129364700853e-07 eV |
| disposition | `validated_classical_reference` |
| claim ceiling | `classical_potential_reference_calibration` |

Every required check passed in each run. Exact equality is used only between attempts in the same
frozen image and implementation; the external documentation comparison uses its declared 1e-6 Å
tolerance.

## Capability registry

The new manifest
`materials.simulation.ase_emt_eos_reference@1.0.0` has hash
`ff5507f8fd891b6fea3354a2e963da74f3c5bd0c8af1d49ac1340eea97931546`. It binds the strict job,
bundle, and preregistration schemas; four role identities; controls; assumptions; failure taxonomy;
reference-only sample rule; resource/nondeterminism/reproduction policy; and ASE software/licence
evidence.

Append-only registry `materials-capabilities-v4` retains all three band-gap capability versions and
adds the simulation capability. Its snapshot hash is
`80ea6dfa5c250dbdb76a4b3b38ceb7460580d17d7cdb47695da93ff38930ad77`. Default discovery rejects
the new capability as provisional; explicit provisional opt-in returns the exact manifest.

## Final verification

Post-documentation acceptance completed with:

- Ruff checks passing for the simulation module, worker, host CLI, public exports, and tests;
- `tests/domains/materials/test_simulation.py`: 12 passed;
- combined `tests/domains/materials tests/capabilities`: 64 passed with 2,611 upstream spglib
  deprecation warnings;
- exact raw-artifact replay of both formal v2 bundles, including checkpoint/result reparsing and
  quality/gold recomputation; and
- authoritative host non-Docker regression: 1,201 passed, 1 skipped, 29 deselected, 2,611 warnings
  in 731.80 seconds.

The host run was required for the suite's configured local PostgreSQL integration tests. Docker
tests were deliberately deselected from the general regression; the two real digest-pinned Docker
executions and their exact bundle replays are the separately retained S5 runtime evidence.

## Scientific interpretation and limits

The formal result supports one bounded statement: the frozen local arm64 ASE 3.29.0/EMT image,
worker, Cu input, five-point scan, parser, and validator reproduce the declared ASE EOS reference
within tolerance, and two distinct attempts produce exactly the same self-authenticating payload.

It does not establish DFT correctness, real Cu accuracy, EMT transferability, calculator
convergence for a research target, an intervention effect, a material mechanism, experimental
validation, or independent implementation replication. The gold reference and validator were not
under external custody. The registry's logical principals do not substitute for an independent
reviewer.

## Remaining work

- implement or independently audit a validator under separate custody and collect the complete
  promotion/safety/domain-review evidence required for registered status;
- publish the image to an OCI registry with digest, SBOM, signature/provenance, and platform policy;
- hash-lock build dependencies rather than relying only on the executed final image identity;
- add multiple EMT reference systems and adversarial numerical/convergence fixtures before claiming
  broader adapter calibration;
- build a new DFT successor with exact pseudopotential, basis/cutoff, k-point, SCF/ionic convergence,
  raw-output, and reference-system contracts; and
- use that independently reviewed successor in F10-S6 only after a mechanism-discriminating
  intervention and fresh/independent confirmation protocol are frozen.
