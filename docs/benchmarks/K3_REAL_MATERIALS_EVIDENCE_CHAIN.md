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

## Analysis scope

This protocol supports a retrospective development diagnostic on a public benchmark.
Any reported metrics must come from a verified bundle implementing the stated protocol and retain
that development label. Local recomputation and distinct local keys do not establish independent
confirmation, a physical mechanism or the F9 scientific exit.

A prospective replication requires a separately frozen full matrix, complete outcome retention,
the registered contraction rule and independent custody appropriate to the claim.
