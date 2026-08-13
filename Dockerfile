# syntax=docker/dockerfile:1
# ─── Stage 0: build the React dashboard (agentra/web/) ───────────────────────
# Separate stage so only the built dist/ (static HTML/JS/CSS) makes it into
# the final image -- not node_modules or dev dependencies (vite, tailwind,
# typescript). The runtime stage below installs its own Node 20 anyway (for
# the Claude Code CLI), but that's a different concern from building this
# frontend, so it stays its own stage rather than reusing that install.
FROM node:20-slim AS web-builder

WORKDIR /web
COPY agentra/web/package.json agentra/web/package-lock.json ./
RUN npm ci
COPY agentra/web/ ./
RUN npm run build

# ─── Stage 1: build agentra package ──────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml .
COPY agentra/ agentra/

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

# Copy installed agentra (and its playwright dependency) from builder
COPY --from=builder /install /usr/local

# Chromium for Testing Agent's pre-prod screenshot capture (agents/
# screenshot.py) -- installed to a shared, world-readable path instead of
# the default ~/.cache under whichever user runs this install step (root,
# here), since the browser actually gets launched by agentuser at runtime.
# --with-deps pulls the OS-level libraries (fonts, libnss3, etc.) headless
# Chromium needs to launch at all, not just the browser binary itself.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers
RUN playwright install --with-deps chromium \
    && chmod -R a+rX /opt/playwright-browsers

# Create a non-root user for safer execution, and pre-create /workspace owned by it --
# so a *fresh* named/anonymous volume mounted at /workspace (the server/clone-on-start
# case: no pre-existing host checkout to bind-mount) inherits agentuser ownership from
# the image, instead of the root ownership Docker would otherwise copy in from a
# root-created WORKDIR. This means cloning, committing, and everything else this image
# does never needs --user root / chown / su anywhere, in local or server use.
#
# Same reasoning applies to /home/agentuser/.claude and /home/agentuser/.agentra below:
# `useradd --create-home` only creates /home/agentuser itself, never nested
# subdirectories -- so a fresh named volume mounted at one (agentra-claude-home,
# agentra-home in docker-compose.yml / run-agent.sh) got Docker's default root:root
# mount-point ownership instead. Confirmed live for .claude: the CLI's own
# session-env setup failed with `EACCES: permission denied, mkdir
# '/home/agentuser/.claude/session-env'` on every write/process-execution tool
# (Bash, Write) while read-only tools kept working -- consistent with agentuser
# being unable to write under a root-owned .claude. /home/agentuser/.agentra (the
# multi-app registry + inbox, agentra/registry.py) needs the same treatment: without
# a persistent, agentuser-writable volume there, every --rm container run would
# start with a blank registry, silently losing all registered apps and any inbox
# state a scheduled `agentra dispatch` hadn't yet processed.
RUN useradd --create-home --shell /bin/bash agentuser \
    && mkdir -p /workspace /home/agentuser/.claude /home/agentuser/.agentra \
    && chown agentuser:agentuser /workspace /home/agentuser/.claude /home/agentuser/.agentra

# Built React dashboard (server.py's AGENTRA_WEB_DIST default assumes
# agentra/web/dist next to the source tree, which doesn't exist in this
# image -- only the installed package does -- so this env var points it at
# where the built assets actually landed instead.)
COPY --from=web-builder --chown=agentuser:agentuser /web/dist /home/agentuser/web-dist
ENV AGENTRA_WEB_DIST=/home/agentuser/web-dist

# Claude CLI stores its config here; a named volume is mounted at runtime
# so OAuth tokens / session state persist across container restarts.
ENV CLAUDE_CONFIG_DIR=/home/agentuser/.claude

WORKDIR /workspace

# Switch to non-root for all agent operations -- everything from here on, including
# the entrypoint script below, runs as agentuser, never root.
USER agentuser

COPY --chown=agentuser:agentuser docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY --chown=agentuser:agentuser git-askpass.sh /usr/local/bin/git-askpass.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh /usr/local/bin/git-askpass.sh

# ─── Auth note ─────────────────────────────────────────────────────────────────
# Pass one of:
#   CLAUDE_CODE_OAUTH_TOKEN=<your_oauth_access_token>   ← preferred for containers
#   ANTHROPIC_API_KEY=<your_api_key>                    ← alternative
#
# The CLI reads CLAUDE_CODE_OAUTH_TOKEN before checking the credentials file,
# so no file mounts are needed when using this env var.
#
# ─── Server / clone-on-start mode ──────────────────────────────────────────────
# Local/dev usage (bind-mount a host checkout at /workspace) needs nothing extra --
# the entrypoint sees /workspace already has content and runs agentra directly, same
# as always. For a server with no pre-existing checkout, mount an empty volume at
# /workspace and additionally set:
#   GIT_CLONE_URL=https://x-access-token@github.com/OWNER/REPO.git
#   GIT_CLONE_BRANCH=main                 (defaults to main)
#   GITHUB_TOKEN=<token>  +  GIT_ASKPASS=/usr/local/bin/git-askpass.sh
#     (a one-line script that echoes $GITHUB_TOKEN -- keeps the token out of
#     .git/config entirely; git invokes it for the password prompt only, since
#     the username is already in the URL as x-access-token@)
# The entrypoint then clones before handing off to agentra.
# ──────────────────────────────────────────────────────────────────────────────

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["--help"]
