#!/bin/sh
# Entrypoint for the agentos image. Two modes, chosen automatically:
#
#   Local/dev (default): /workspace is a bind-mount of an already-checked-out repo.
#   The clone step below is a no-op (directory isn't empty) and this just execs
#   agentos with whatever args were passed, exactly like the old plain
#   `ENTRYPOINT ["agentos"]` did.
#
#   Server (no pre-existing checkout): /workspace is an empty volume/disk and
#   GIT_CLONE_URL is set. Clones first, then execs agentos. Runs entirely as
#   agentuser (see Dockerfile) -- no root, no chown, no su needed either way.
set -e

if [ -n "${GIT_CLONE_URL:-}" ] && [ -z "$(ls -A /workspace 2>/dev/null)" ]; then
  echo "[entrypoint] /workspace is empty and GIT_CLONE_URL is set -- cloning ${GIT_CLONE_BRANCH:-main}..."
  git clone --branch "${GIT_CLONE_BRANCH:-main}" --single-branch "$GIT_CLONE_URL" /workspace
fi

exec agentos "$@"
