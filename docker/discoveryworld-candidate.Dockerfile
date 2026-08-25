# Neutral policy runtime for DiscoveryWorld evaluation.
#
# This image deliberately contains neither DiscoveryWorld nor Aletheia/evaluator source.  The
# scorer resolves the built tag to an immutable image ID and verifies both absences before freezing
# a suite.  Candidate policies use only Python's standard library and the one-way file bridge.
FROM python:3.11-slim-bookworm@sha256:2e32f7d302adc1c37428355c1e646897c0c53f4fd60b6a551245fb90ee129f91

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
