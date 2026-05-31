# Heavier hard-sandbox image for AI-authored SOTA model code that needs deep
# learning (CPU torch) on top of the gradient-boosting stack. Same isolation as
# the light image (the host featurizes; this container runs ONLY train_evaluate
# on staged X/y, offline). Use it by pointing the backend at this image:
#   ALETHEIA_SANDBOX_DOCKER_IMAGE=aletheia-sandbox-sota:latest
#
# Build (large; CPU-only torch):
#   docker build -t aletheia-sandbox-sota:latest -f docker/sandbox-sota.Dockerfile docker/
FROM python:3.11-slim

# CPU-only torch wheels (no CUDA) to keep the image as small as deep learning allows.
RUN pip install --no-cache-dir \
    "numpy" "pandas" "scikit-learn" "scipy" "joblib" "matplotlib" \
    "xgboost" "lightgbm" "skorch" \
    --extra-index-url https://download.pytorch.org/whl/cpu "torch"

ENV MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /work
