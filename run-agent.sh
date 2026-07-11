#!/usr/bin/env bash
# run-agent.sh — Convenience wrapper to run agentos inside a Docker container.
#
# Usage:
#   CLAUDE_CODE_OAUTH_TOKEN=<token> REPO_PATH=/path/to/repo ./run-agent.sh run \
#     --objective "improve engagement" --skip-deploy
#
#   CLAUDE_CODE_OAUTH_TOKEN=<token> REPO_PATH=/path/to/repo ./run-agent.sh loop \
#     --objective "improve engagement" --cycles 3
#
# All arguments after the script name are passed directly to `agentos`.

set -euo pipefail

# ── Preflight checks ──────────────────────────────────────────────────────────

if [[ -z "${REPO_PATH:-}" ]]; then
  echo "❌  REPO_PATH is not set. Point it at the target repository:"
  echo "    export REPO_PATH=/path/to/your/repo"
  exit 1
fi

if [[ ! -d "$REPO_PATH" ]]; then
  echo "❌  REPO_PATH does not exist: $REPO_PATH"
  exit 1
fi

if [[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" && -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "❌  No auth credentials provided. Set one of:"
  echo "    export CLAUDE_CODE_OAUTH_TOKEN=<your_oauth_access_token>"
  echo "    export ANTHROPIC_API_KEY=<your_api_key>"
  exit 1
fi

# ── Build image if not present ────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_NAME="agentos:local"

if ! docker image inspect "$IMAGE_NAME" &>/dev/null; then
  echo "🔨  Building $IMAGE_NAME (first run only)…"
  docker build -t "$IMAGE_NAME" "$SCRIPT_DIR"
fi

# ── Run ───────────────────────────────────────────────────────────────────────

echo "🤖  Running: agentos $*"
echo "    repo:  $REPO_PATH"
echo "    image: $IMAGE_NAME"
echo ""

docker run --rm -it \
  --volume "$REPO_PATH:/workspace" \
  --volume "agentos-claude-home:/home/agentuser/.claude" \
  --volume "agentos-home:/home/agentuser/.agentos" \
  --tmpfs /tmp:size=256m,mode=1777 \
  --read-only \
  --cap-drop ALL \
  --network bridge \
  --env "CLAUDE_CODE_OAUTH_TOKEN=${CLAUDE_CODE_OAUTH_TOKEN:-}" \
  --env "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}" \
  --env "GIT_AUTHOR_NAME=${GIT_AUTHOR_NAME:-agentos-bot}" \
  --env "GIT_AUTHOR_EMAIL=${GIT_AUTHOR_EMAIL:-rossharma1@gmail.com}" \
  --env "GIT_COMMITTER_NAME=${GIT_AUTHOR_NAME:-agentos-bot}" \
  --env "GIT_COMMITTER_EMAIL=${GIT_AUTHOR_EMAIL:-rossharma1@gmail.com}" \
  --env "VERCEL_TOKEN=${VERCEL_TOKEN:-}" \
  --env "FIREBASE_TOKEN=${FIREBASE_TOKEN:-}" \
  "$IMAGE_NAME" "$@"
