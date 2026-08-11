#!/usr/bin/env bash
# dev.sh — Run the agentra dashboard fully locally, no cloud credentials.
#
# Uses a scratch AGENTRA_HOME (never the real ~/.agentra) and
# AGENTRA_DEV_MODE=1, which makes server.py fake an already-installed
# GitHub App instead of gating the whole dashboard behind a real one --
# dev_seed.py then fills that scratch home with a couple of realistic
# apps/runs/agent-steps/signals so every tab has something to show.
#
# Usage:
#   ./dev.sh [port]            # defaults to 8090
#   ./dev.sh --reseed [port]   # wipe and reseed fixture data first
#
# The dashboard build (agentra/web/dist/) must already exist --
# run `npm run build` in agentra/web/ first if you've changed the frontend.

set -euo pipefail

RESEED=0
if [[ "${1:-}" == "--reseed" ]]; then
  RESEED=1
  shift
fi
PORT="${1:-8090}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export AGENTRA_HOME="${AGENTRA_HOME:-$HOME/.agentra-dev}"
export AGENTRA_DEV_MODE=1
unset AGENTRA_FIRESTORE_PROJECT GITHUB_APP_ID GITHUB_APP_PRIVATE_KEY 2>/dev/null || true

cd "$SCRIPT_DIR"

if [[ "$RESEED" == "1" ]]; then
  rm -rf "$AGENTRA_HOME"
fi

echo "🌱  Seeding fixture data under $AGENTRA_HOME (no-op if already seeded)…"
python3 -c "from agentra.dev_seed import seed; seed()"

echo "🚀  Serving agentra dev dashboard on http://localhost:${PORT}"
python3 -m agentra.cli serve --port "$PORT"
