# Light, hard-sandbox image for executing AI-authored model code.
#
# Deliberately minimal: only the sklearn training stack — NOT matminer/pymatgen
# or the agent runtime. The host featurizes the data (network) and stages X/y;
# this container runs ONLY `train_evaluate` on the staged arrays, offline.
# The aletheia source is mounted read-only at /repo (PYTHONPATH), not installed,
# so the image stays small and the agent runtime never enters the sandbox.
#
# Build:  docker build -t aletheia-sandbox:latest -f docker/sandbox.Dockerfile docker/
FROM python:3.11-slim

RUN pip install --no-cache-dir \
    "numpy" "pandas" "scikit-learn" "scipy" "joblib" "matplotlib" \
    "xgboost" "lightgbm"

ENV MPLBACKEND=Agg \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /work
