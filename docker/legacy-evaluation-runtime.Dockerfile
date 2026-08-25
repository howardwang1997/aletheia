# Candidate PR-6 qualification-only image. Deployment must resolve and freeze the resulting OCI
# manifest/config digests; this source file is not itself target-host qualification evidence.
FROM python:3.11-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff

COPY configs/capabilities/legacy-evaluation-runtime-constraints-v1.txt \
    /opt/aletheia/config/legacy-evaluation-runtime-constraints-v1.txt

RUN pip install --no-cache-dir \
    --constraint /opt/aletheia/config/legacy-evaluation-runtime-constraints-v1.txt \
    "cloudpickle==3.1.2" \
    "cryptography==48.0.0" \
    "joblib==1.5.3" \
    "matminer==0.10.1" \
    "numpy==2.4.6" \
    "pandas==2.3.3" \
    "pydantic==2.13.4" \
    "pymatgen==2026.5.4" \
    "scikit-learn==1.8.0"

COPY aletheia /opt/aletheia/src/aletheia
COPY configs/capabilities/legacy-evaluation-materials-v1.json \
    /opt/aletheia/config/legacy-evaluation-materials-v1.json
COPY aletheia/execution/qualification_launch_gate.py \
    /opt/aletheia/bin/qualification-launch-gate
COPY aletheia/legacy_evaluation/handler.py \
    /opt/aletheia/bin/legacy-evaluation-workload

RUN chmod 0555 \
      /opt/aletheia/bin/qualification-launch-gate \
      /opt/aletheia/bin/legacy-evaluation-workload \
    && chmod 0444 \
      /opt/aletheia/config/legacy-evaluation-materials-v1.json \
      /opt/aletheia/config/legacy-evaluation-runtime-constraints-v1.txt

ENV LC_ALL=C.UTF-8 \
    MPLCONFIGDIR=/opt/aletheia/scratch/matplotlib \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONPATH=/opt/aletheia/src \
    PYTHONUNBUFFERED=1

WORKDIR /opt/aletheia/output
