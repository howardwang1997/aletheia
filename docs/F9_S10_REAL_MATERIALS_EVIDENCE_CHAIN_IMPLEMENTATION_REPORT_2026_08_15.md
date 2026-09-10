# F9-S10 real-materials protocol contract

The materials K3 implementation supports a retrospective development diagnostic of prediction
range compression on `matbench_expt_gap`. Its current protocol is
`configs/materials/k3_band_gap_range_compression_v2.yaml`.

The protocol compares no material compression, unseen-system-specific compression and generic
model shrinkage. It selects a controlled experiment before observation access and freezes the
model, partitions, cluster bootstrap, outcome rules, priors and likelihood-sensitivity scenarios.
Measurement and full recomputation use distinct local keys. Only validated observations enter
the Bayesian update; retirement requires the threshold in every registered sensitivity scenario.

The implementation is `aletheia/domains/materials/k3_evidence.py`; the CLI is
`scripts/real_k3_materials_e2e.py`. Outputs are create-only and content addressed. The
[operator guide](benchmarks/K3_REAL_MATERIALS_EVIDENCE_CHAIN.md) gives the execution sequence.

This is a development protocol on a public retrospective benchmark. Local key separation does
not establish independent custody. Scientific promotion requires a verified current-protocol
bundle, its full registered matrix, the contraction gate and claim-appropriate independent
confirmation. No scientific-exit claim is made here.
