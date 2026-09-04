# Frozen MoleculeNet ESOL asset

`delaney-processed.csv` is the exact public CSV used by the molecules-domain ESOL
demonstrations.

- Source: `https://deepchemdata.s3-us-west-1.amazonaws.com/datasets/delaney-processed.csv`
- Retrieved: 2026-09-03 UTC
- Bytes: 96,699
- Records excluding the header: 1,128
- SHA-256: `8c06a76f0c6487d29ab0f903e6a7a7139f189ab3c1178f159c8be8964602f189`

The runtime verifies the digest and record count before use. This copy removes network and
upstream-mutation ambiguity; it does not change the benchmark's attribution or licensing.
The source CSV contains upstream-significant trailing spaces, so `.gitattributes` disables text
conversion and whitespace cleanup for this exact asset.
