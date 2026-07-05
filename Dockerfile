# syntax=docker/dockerfile:1
# ─── Stage 1: build agentos package ──────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml .
COPY agentos/ agentos/

RUN pip install --no-cache-dir --prefix=/install .

# ─── Stage 2: runtime image ───────────────────────────────────────────────────
FROM python:3.12-slim

# Install Node.js 20 LTS (needed for @anthropic-ai/claude-code CLI) and git
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        git \
        ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install the Claude Code CLI, and the Vercel/Firebase CLIs the Deployment
# Agent shells out to, globally
RUN npm install -g @anthropic-ai/claude-code vercel firebase-tools --no-update-notifier

# Copy installed agentos from builder
COPY --from=builder /install /usr/local

# Create a non-root user for safer execution
RUN useradd --create-home --shell /bin/bash agentuser

# Claude CLI stores its config here; a named volume is mounted at runtime
# so OAuth tokens / session state persist across container restarts.
ENV CLAUDE_CONFIG_DIR=/home/agentuser/.claude

# Target repo is always mounted at /workspace
WORKDIR /workspace

# Switch to non-root for all agent operations
USER agentuser

# ─── Auth note ─────────────────────────────────────────────────────────────────
# Pass one of:
#   CLAUDE_CODE_OAUTH_TOKEN=<your_oauth_access_token>   ← preferred for containers
#   ANTHROPIC_API_KEY=<your_api_key>                    ← alternative
#
# The CLI reads CLAUDE_CODE_OAUTH_TOKEN before checking the credentials file,
# so no file mounts are needed when using this env var.
# ──────────────────────────────────────────────────────────────────────────────

ENTRYPOINT ["agentos"]
CMD ["--help"]
