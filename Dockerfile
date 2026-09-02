# syntax=docker/dockerfile:1
# agentra-engine — the API. Slim, Cloud-Run-first: no browser, no Node/CLIs, no
# docker client. Just Python + git (registry/apps clone & pull repos over the
# GitHub App). The dashboard is Srijanlab/agentra-ui, hosted separately; the LLM
# + build workloads are Srijanlab/agentra-loop.

FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml .
COPY agentra/ agentra/
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

RUN useradd --create-home --shell /bin/bash agentuser \
    && mkdir -p /workspace /home/agentuser/.agentra \
    && chown agentuser:agentuser /workspace /home/agentuser/.agentra
WORKDIR /workspace
USER agentuser

# Cloud Run sets $PORT; `agentra serve` already reads it (default 8080).
EXPOSE 8080

# No entrypoint script — Cloud Run has no repo to clone on start, the engine
# just serves. Firestore + GitHub App creds come from the runtime service
# account and mounted secrets, not a git checkout.
CMD ["agentra", "serve"]
