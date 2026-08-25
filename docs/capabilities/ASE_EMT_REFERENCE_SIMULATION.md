# ASE/EMT reference simulation capability

## What this capability does

`materials.simulation.ase_emt_eos_reference@1.0.0` runs one frozen periodic ASE/EMT
equation-of-state job in a hardened, digest-pinned container. It retains raw bytes and checkpoint
lineage, recomputes eleven quality/reference checks, and can compare two distinct physical attempts
for exact deterministic agreement.

Its only valid positive disposition is `validated_classical_reference`, with claim ceiling
`classical_potential_reference_calibration`. It is provisional and exploratory. It is not DFT,
experimental validation, an independent implementation replication, a transferability result, or
mechanism evidence.

## Frozen identities

| Object | Identity |
|---|---|
| capability manifest | `ff5507f8fd891b6fea3354a2e963da74f3c5bd0c8af1d49ac1340eea97931546` |
| registry v4 snapshot | `80ea6dfa5c250dbdb76a4b3b38ceb7460580d17d7cdb47695da93ff38930ad77` |
| protocol v2 | `d4e224336fd3a062839eb8ceaba01309aa3b5285a550237a5f3f0721158b22d5` |
| exact image / platform | `sha256:54190c4fdf338fa4cf342f11f573593d47a623fabfe9c34f0828b8cac29b4b24` / `linux/arm64` |
| worker | `d4e5f88056f7770595ea29a9759f953d143b08b4e13f9d095142c871ff4c9b8a` |
| host executor | `9f21dcf30151208d1feb11147adb253ef6914feb4f9bfba923a346e0467cc463` |
| parser/validator module | `3f77e6ec7edbffb5778fd6d56fb8ebd4c5af477eb7674254373a18bbbed3c1f2` |
| exact job bytes / job object | `e1b7f957186fee7340e7d1e108f7a84a69a155b1f9b44752a7590ad211c27bfa` / `4885a3fbe4943c2cd39ea2695909c63b239a4b3a472ef925f3fed526eabcabce` |
| expected result payload | `f8d94b2850f51ec72037521fb87d72546e966968fc2de9845c8dfe2c6c7057f7` |

The image must already exist locally under that exact ID. Rebuilding the Dockerfile is development
work and does not recreate the frozen evidence unless the new image has the same content ID.

## Inspect the capability and image

```bash
conda run -n aletheia python scripts/capability_registry.py validate-manifest \
  --manifest configs/capabilities/materials_ase_emt_eos_reference_provisional_v1.yaml

conda run -n aletheia python scripts/capability_registry.py inspect \
  --registry workspaces/evaluator/capabilities/materials_registry_v4.json

docker image inspect \
  sha256:54190c4fdf338fa4cf342f11f573593d47a623fabfe9c34f0828b8cac29b4b24 \
  --format '{{.Id}} {{.Os}}/{{.Architecture}}'
```

Default registry discovery rejects this capability because it is provisional. A planner or
operator must deliberately set `allow_provisional=True`, and the resulting evidence remains
exploratory.

## Execute a new immutable attempt

Choose new create-only output names; the CLI refuses to overwrite a frozen artifact.

```bash
conda run -n aletheia python scripts/ase_emt_simulation_e2e.py execute \
  --protocol configs/materials/f10_ase_emt_cu_fcc_eos_reference_v2.yaml \
  --job tests/fixtures/materials_simulation/cu_fcc_eos_job.json \
  --run-id ase-emt-cu-gold-manual-01 \
  --archive workspaces/evaluator/materials-simulation-emt-manual/archive \
  --output workspaces/evaluator/materials-simulation-emt-manual/raw-run-01.json

conda run -n aletheia python scripts/ase_emt_simulation_e2e.py finalize \
  --protocol configs/materials/f10_ase_emt_cu_fcc_eos_reference_v2.yaml \
  --job tests/fixtures/materials_simulation/cu_fcc_eos_job.json \
  --raw-run workspaces/evaluator/materials-simulation-emt-manual/raw-run-01.json \
  --archive workspaces/evaluator/materials-simulation-emt-manual/archive \
  --output workspaces/evaluator/materials-simulation-emt-manual/bundle-01.json
```

`execute` binds the scratch directory under the archive's workspace-backed parent so Colima can
mount it. It uses an exact named container, inspects terminal state, records cleanup output, and then
removes that exact container. Timeouts force-stop it. Worker outputs are copied into the
content-addressed archive before scratch cleanup.

## Verify retained evidence

The formal v2 evidence can be replayed without rerunning the calculator:

```bash
conda run -n aletheia python scripts/ase_emt_simulation_e2e.py verify \
  --bundle workspaces/evaluator/materials-simulation-emt-v2/bundle-01.json \
  --job tests/fixtures/materials_simulation/cu_fcc_eos_job.json \
  --archive workspaces/evaluator/materials-simulation-emt-v2/archive

conda run -n aletheia python scripts/ase_emt_simulation_e2e.py verify \
  --bundle workspaces/evaluator/materials-simulation-emt-v2/bundle-02.json \
  --job tests/fixtures/materials_simulation/cu_fcc_eos_job.json \
  --archive workspaces/evaluator/materials-simulation-emt-v2/archive
```

To compare two newly finalized validated attempts:

```bash
conda run -n aletheia python scripts/ase_emt_simulation_e2e.py compare \
  --source-bundle workspaces/evaluator/materials-simulation-emt-manual/bundle-01.json \
  --replay-bundle workspaces/evaluator/materials-simulation-emt-manual/bundle-02.json \
  --output workspaces/evaluator/materials-simulation-emt-manual/reproduction.json
```

The formal comparison receipt is
`263424ae4f8148516b175832c53c4b6ab1dfc87e4a3881f0a9afd38409497a59`. It records exact payload
agreement and also records that same-image/same-implementation repetition is not independent
replication.

## Validation semantics

The validator recomputes these sorted checks:

- `bulk_modulus_in_policy`;
- `calculator_and_scan_exact`;
- `evaluation_count_complete`;
- `execution_succeeded`;
- `fit_inside_scan`;
- `fit_residual_in_policy`;
- `gold_reference_exact`;
- `parser_succeeded`;
- `runtime_versions_exact`;
- `sample_minimum_interior`; and
- `volumes_strictly_increasing`.

All eleven must pass for `validated_classical_reference`. Failure maps to
`rejected_execution`, `rejected_parse`, `rejected_quality`, or `rejected_gold_mismatch`. Timeout,
output quota, container infrastructure, unsupported elements, corrupt checkpoint/result, or a bad
fit never becomes a material-property negative result.

## Current reference result

The two formal v2 runs produced the same exact payload:

| Quantity | Result |
|---|---:|
| equilibrium volume per atom | 11.565374360310688 Å³ |
| derived fcc conventional lattice constant | 3.589824595554312 Å |
| ASE documentation reference | 3.589825 Å |
| bulk modulus | 0.8392121970531606 eV/Å³ (about 134.46 GPa) |
| fitted minimum energy | -0.007036378469896576 eV |
| fit RMSE | 7.715379800753145e-08 eV |
| maximum absolute fit residual | 1.2363129364700853e-07 eV |

## Promotion gates

This capability must remain provisional until all of the following exist:

- a validator that is separately implemented or independently audited and does not share the
  executor/parser development principal;
- complete reference, adversarial, positive/negative-control, safety, domain-review, and promotion
  receipts required by `CapabilityRegistrationEvidence`;
- a registry promotion that preserves the append-only manifest chain;
- an OCI registry digest plus SBOM/signature/provenance and a portability policy; and
- if the intended claim is DFT, a new executor with exact pseudopotential, basis/cutoff, k-point,
  SCF/ionic convergence, code/runtime, and multiple-reference-system contracts.

The architecture decision and full evidence interpretation are in
[`ADR 0030`](../adr/0030-f10-digest-pinned-ase-emt-reference-simulation.md) and the
[`F10-S5 implementation report`](../F10_S5_REPRODUCIBLE_SIMULATION_CAPABILITY_IMPLEMENTATION_REPORT_2026_08_15.md).
