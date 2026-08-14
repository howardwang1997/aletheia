# Research-plane image for F7 formal evaluation attempts.
#
# This image intentionally does not COPY the Aletheia repository.  In particular,
# evaluator runner/scorer code, hidden assets, project credentials and host state are
# absent.  A concrete system-under-test may extend this image with its *research-side*
# runtime, but scorer implementations must remain in the separate evaluator process.
FROM python:3.11-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff

RUN pip install --no-cache-dir \
    "numpy==2.4.6" "pandas==2.3.3" "scikit-learn==1.8.0" \
    "scipy==1.17.1" "joblib==1.5.3" "matplotlib==3.10.9" \
    "pydantic==2.13.4" "PyYAML==6.0.3"

ENV MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /workspace
