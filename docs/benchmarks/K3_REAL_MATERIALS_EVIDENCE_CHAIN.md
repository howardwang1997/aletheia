# K3 real-materials evidence chain

## Scope

This runbook executes the F9-S10 Matbench band-gap model diagnostic. It proves a real-data,
precommitted alternatives → experiment → validation → Bayesian-update path. It does not establish a
physical mechanism, a prospective result, or external replication.

The current protocol is
`configs/materials/k3_band_gap_range_compression_v2.yaml`. It compares:

- H0: neither partition materially compresses prediction range;
- H1: unseen chemical systems add compression beyond a represented-system control; and
- H2: compression is generic model shrinkage rather than unseen-system-specific.

The outcome statistic is

```text
compression = 1 - SD(predicted band gap) / SD(measured band gap)
delta       = unseen-system compression - within-system-control compression
```

The confidence interval resamples chemical systems as clusters. Partition membership is derived
from frozen hashes and never from target values.

## Trust sequence

```text
frozen protocol (no data access)
  -> observation-blind EIG ranking
  -> immutable preregistration
  -> real Matbench measurement + measurement-key signature
  -> separate-key full physical recomputation
  -> signed validation receipt
  -> likelihood-sensitivity Bayesian update
  -> qualified-complete or insufficient-contraction decision
```

The module is `aletheia.domains.materials.k3_evidence`; the operator CLI is
`scripts/real_k3_materials_e2e.py`.

## Runbook

Create two raw key files of at least 32 bytes outside source control, then run:

```bash
conda run -n aletheia python scripts/real_k3_materials_e2e.py inspect \
  --protocol configs/materials/k3_band_gap_range_compression_v2.yaml

conda run -n aletheia python scripts/real_k3_materials_e2e.py preregister \
  --protocol configs/materials/k3_band_gap_range_compression_v2.yaml \
  --preregistration-id <stable-id> \
  --output <evidence-root>/preregistration.json

conda run -n aletheia python scripts/real_k3_materials_e2e.py measure \
  --preregistration <evidence-root>/preregistration.json \
  --measurement-key <measurement-key-file> \
  --output <evidence-root>/observation.json

conda run -n aletheia python scripts/real_k3_materials_e2e.py validate \
  --preregistration <evidence-root>/preregistration.json \
  --observation <evidence-root>/observation.json \
  --measurement-key <measurement-key-file> \
  --validation-key <validation-key-file> \
  --output <evidence-root>/validation.json

conda run -n aletheia python scripts/real_k3_materials_e2e.py update \
  --preregistration <evidence-root>/preregistration.json \
  --observation <evidence-root>/observation.json \
  --validation <evidence-root>/validation.json \
  --measurement-key <measurement-key-file> \
  --validation-key <validation-key-file> \
  --output <evidence-root>/evidence_bundle.json

conda run -n aletheia python scripts/real_k3_materials_e2e.py verify \
  --bundle <evidence-root>/evidence_bundle.json \
  --measurement-key <measurement-key-file> \
  --validation-key <validation-key-file> \
  --recompute
```

Every output is create-only. A repeated command refuses to overwrite frozen evidence.

## Current result

The authoritative v2 local bundle is
`workspaces/evaluator/materials-k3-band-gap-v2/evidence_bundle.json`, SHA-256
`7163113d8d93058156fde1762271dceea0d1872f2e0a0a5f22d9629e8a41b270`.

| Quantity | Frozen 20260817 result |
|---|---:|
| unseen compression | 0.2409 |
| represented-system control compression | 0.1948 |
| difference | 0.0461 |
| cluster-bootstrap 95% interval | [-0.0140, 0.1145] |
| bootstrap probability difference > 0 | 0.930 |
| unseen/control MAE | 0.4337 / 0.3642 eV |
| outcome | generic model shrinkage |

The nominal posterior is H0/H1/H2 = 0.161/0.143/0.696. H2 remains the winner in every frozen
likelihood scenario, but worst-case effective-count contraction is only 0.0134, below the 0.10 gate.
The resulting disposition is `valid_update_without_robust_contraction`.

## Interpreting v1 and v2

The retained v1 20260816 result had delta 0.0681 with interval [0.00535, 0.1309], producing an
unseen-specific outcome and robust posterior contraction. A code audit then found that its revision
directive could retire H0 from nominal likelihoods despite skeptical posterior 0.134. The exact v1
implementation and immutable evidence remain under its local evidence root, but its terminal
revision is superseded.

The v2 run used a new partition and conservative all-scenarios retirement. Its opposite result is
not a failure of the evidence chain; it is evidence that the specific effect is partition-sensitive.
Do not average, select, or relabel these runs post hoc. Register a multi-partition replication before
using further seeds.

## Scientific boundary

This chain satisfies the real-data execution limb at engineering strength. It does not satisfy the
full F9 scientific exit because:

- v2 misses the robust substantial-contraction gate;
- the benchmark is public and retrospective;
- measurement and validation keys have local single-operator custody; and
- no independent laboratory or external dataset replicated the result.
