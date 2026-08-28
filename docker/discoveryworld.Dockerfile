# Trusted hidden-world runtime for the pinned DiscoveryWorld public-validation mini-suite.
#
# The official source is downloaded at build time, verified byte-for-byte, and retained under
# /opt solely for evaluator runtime verification.  It is never mounted into the candidate plane.
# DiscoveryWorld code is Apache-2.0.  Its PixyMoon art assets have a separate project-use,
# attribution, modification, and no-resale license; this repository does not vendor those assets.
FROM python:3.11-slim-bookworm@sha256:2e32f7d302adc1c37428355c1e646897c0c53f4fd60b6a551245fb90ee129f91

RUN python -m pip install --no-cache-dir --upgrade \
    "pip==26.2.1" \
    "setuptools==84.0.0"

ARG DISCOVERYWORLD_COMMIT=fd591323920be0d3786ef350955de1945aa571e5
ARG DISCOVERYWORLD_ARCHIVE_SHA256=0ef5f45566807083754aa140e5653b9e8260434fc71d977591598b6625e619b1

ENV DISCOVERYWORLD_SOURCE_COMMIT=${DISCOVERYWORLD_COMMIT} \
    DISCOVERYWORLD_SOURCE_ARCHIVE_SHA256=${DISCOVERYWORLD_ARCHIVE_SHA256} \
    SDL_VIDEODRIVER=dummy \
    MPLBACKEND=Agg \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN curl --fail --location --silent --show-error \
        "https://github.com/allenai/discoveryworld/archive/${DISCOVERYWORLD_COMMIT}.tar.gz" \
        --output /tmp/discoveryworld.tar.gz \
    && echo "${DISCOVERYWORLD_ARCHIVE_SHA256}  /tmp/discoveryworld.tar.gz" | sha256sum --check - \
    && mkdir -p /opt/discoveryworld-source \
    && tar --extract --gzip --file /tmp/discoveryworld.tar.gz \
        --strip-components=1 --directory /opt/discoveryworld-source \
    && rm /tmp/discoveryworld.tar.gz

# Only dependencies imported by the environment package are installed.  Agent-only OpenAI and
# tokenization clients from upstream requirements are intentionally absent from this offline image.
RUN pip install --no-cache-dir \
        "numpy==2.4.6" \
        "matplotlib==3.10.9" \
        "pygame==2.6.1" \
        "pathfinding==1.0.18" \
        "termcolor==3.2.0" \
    && pip install --no-cache-dir --no-deps /opt/discoveryworld-source
