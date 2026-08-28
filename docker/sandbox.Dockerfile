# Light, hard-sandbox image for executing AI-authored model code.
#
# Deliberately minimal: only the sklearn training stack — NOT matminer/pymatgen
# or the agent runtime. The host featurizes the data (network) and stages X/y;
# this container runs ONLY `train_evaluate` on the staged arrays, offline.
# The small trusted evaluation subset of Aletheia is baked into the image. The
# repository (and therefore .env/credentials) is never mounted at runtime.
#
# Build from the repository root:
#   docker build -t aletheia-sandbox:latest -f docker/sandbox.Dockerfile .
FROM python:3.11-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff

RUN python -m pip install --no-cache-dir --upgrade \
    "pip==26.2.1" \
    "setuptools==84.0.0"

RUN pip install --no-cache-dir \
    "numpy==2.4.6" "pandas==2.3.3" "scikit-learn==1.8.0" \
    "scipy==1.17.1" "joblib==1.5.3" "matplotlib==3.10.9" \
    "xgboost==3.2.0" "lightgbm==4.6.0" "cloudpickle==3.1.2" \
    "pydantic==2.13.4" "pydantic-settings==2.14.2" "PyYAML==6.0.3"

COPY aletheia /opt/aletheia/aletheia

ENV MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp \
    PYTHONPATH=/opt/aletheia \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /work
