# Reference CPU-only image for the PR-4b local qualification runtime.
#
# The image is intentionally not tagged or trusted by source name.  Deployment must pin the
# resulting manifest/config digests and independently attest the launch-gate bytes before it can
# appear in DeploymentPinnedOCIPolicy.
FROM python:3.11-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff

RUN python -m pip install --no-cache-dir --upgrade \
    "pip==26.2.1" \
    "setuptools==84.0.0"

RUN pip install --no-cache-dir "cryptography==50.0.0"

COPY aletheia/execution/qualification_launch_gate.py \
    /opt/aletheia/bin/qualification-launch-gate
COPY docker/qualification-smoke-workload.py \
    /opt/aletheia/bin/qualification-smoke-workload.py

RUN chmod 0555 \
    /opt/aletheia/bin/qualification-launch-gate \
    /opt/aletheia/bin/qualification-smoke-workload.py

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /work
