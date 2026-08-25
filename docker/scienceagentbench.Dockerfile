# Research/scorer image for the reviewed CC-BY ScienceAgentBench mini-suite.
#
# The image contains only public scientific runtimes.  Benchmark datasets, evaluators and gold
# programs are never copied; the F7 harness supplies one task's data and evaluator through separate
# read-only mounts and never mounts gold programs.
FROM aletheia-evaluator-agent:latest

RUN pip install --no-cache-dir \
    "numpy==2.4.6" \
    "pandas==2.3.3" \
    "scikit-learn==1.8.0" \
    "scipy==1.17.1" \
    "matplotlib==3.10.9" \
    "rdkit==2026.3.4" \
    "geopandas==1.1.4" \
    "neurokit2==0.2.13"
