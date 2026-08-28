# Heavier hard-sandbox image for AI-authored SOTA model code that needs deep
# learning (CPU torch) on top of the gradient-boosting stack. Same isolation as
# the light image (the host featurizes; this container runs ONLY train_evaluate
# on staged X/y, offline). Use it by pointing the backend at this image:
#   ALETHEIA_SANDBOX_DOCKER_IMAGE=aletheia-sandbox-sota:latest
#
# Build (large; CPU-only torch):
#   docker build -t aletheia-sandbox-sota:latest -f docker/sandbox-sota.Dockerfile .
FROM python:3.11-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff

RUN python -m pip install --no-cache-dir --upgrade \
    "pip==26.2.1" \
    "setuptools==84.0.0"

# CPU-only torch wheels (no CUDA) to keep the image as small as deep learning allows.
RUN pip install --no-cache-dir \
    "numpy==2.4.6" "pandas==2.3.3" "scikit-learn==1.8.0" \
    "scipy==1.17.1" "joblib==1.5.3" "matplotlib==3.10.9" \
    "xgboost==3.2.0" "lightgbm==4.6.0" "cloudpickle==3.1.2" \
    "pydantic==2.13.4" "pydantic-settings==2.14.2" "PyYAML==6.0.3" \
    "skorch==1.2.0" --extra-index-url https://download.pytorch.org/whl/cpu "torch==2.13.0"

COPY aletheia /opt/aletheia/aletheia

ENV MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp \
    PYTHONPATH=/opt/aletheia \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /work
