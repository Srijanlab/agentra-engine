# Running agentos in a Container

The agents in this system have unrestricted `Bash`, `Write`, and `Edit` access to
the target repository via the Claude CLI. The `safety.py` regex filter is a
**second line of defence** — it catches an agent following bad instructions, but
is not a real sandbox. Running inside a Docker container provides true OS-level
isolation: the agent process can only reach what is explicitly mounted and
granted.

---

## What the container isolates

| Isolated ✅ | Not isolated ⚠️ |
|---|---|
| Host filesystem (only `/workspace` is visible) | Code changes in the mounted repo (intentional — you want them) |
| Host environment variables / secrets | `git commit` / `git push` from inside the container |
| Host SSH keys, `~/.aws`, `~/.gcloud`, etc. | Outbound network (Claude API, git, Vercel, Firebase) |
| Other running processes on the host | |

---

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or Docker
  Engine on Linux)
- Your OAuth access token from `claude login`, or an `ANTHROPIC_API_KEY`

---

## Quick start

```bash
# 1. Export your OAuth token (generated via `claude login` or the Anthropic Console)
export CLAUDE_CODE_OAUTH_TOKEN=<your_oauth_access_token>

# 2. Point at the target repo
export REPO_PATH=/Users/you/projects/my-app

# 3. Set your git identity (used for commits made by the agent)
export GIT_AUTHOR_NAME="agentos-bot"
export GIT_AUTHOR_EMAIL="agentos@localhost"

# 4. Run a single improvement cycle
./run-agent.sh run \
  --objective "improve engagement and retention" \
  --skip-deploy

# 5. Or run multiple autonomous cycles
./run-agent.sh loop \
  --objective "improve engagement and retention" \
  --cycles 5
```

The script auto-builds the image on first run. Subsequent runs reuse the cached
image.

---

## Auth: OAuth token vs API key

The Claude CLI checks credentials in this order:

1. **`CLAUDE_CODE_OAUTH_TOKEN`** env var — used directly as the bearer token.
   No file mounts needed. Preferred for containers.
2. **`ANTHROPIC_API_KEY`** env var — traditional API key auth.
3. **`~/.claude/.credentials.json`** — the on-disk token file written by
   `claude login`. Not used in the container by default (that path isn't mounted).

If you generated an OAuth token via `claude login`, copy the `accessToken` field
and export it as `CLAUDE_CODE_OAUTH_TOKEN`.

> [!WARNING]
> OAuth access tokens expire. If the agent starts failing with auth errors,
> re-run `claude login` and export the new `accessToken`.

---

## Passing deployment credentials (optional)

The Deployment Agent (`agents/deployment.py`) uses Vercel CLI and Firebase CLI.
These tools authenticate via env vars when no interactive session is available:

```bash
export VERCEL_TOKEN=<your_vercel_token>     # from vercel.com/account/tokens
export FIREBASE_TOKEN=<your_ci_token>       # from `firebase login:ci`

./run-agent.sh run --objective "..." --repo /path/to/app
```

Both are forwarded into the container automatically by `run-agent.sh`.

---

## Manual docker run (without run-agent.sh)

```bash
docker build -t agentos:local .

docker run --rm -it \
  --volume /path/to/repo:/workspace \
  --volume agentos-claude-home:/home/agentuser/.claude \
  --tmpfs /tmp:size=256m,mode=1777 \
  --read-only \
  --cap-drop ALL \
  --env CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
  --env GIT_AUTHOR_NAME="agentos-bot" \
  --env GIT_AUTHOR_EMAIL="agentos@localhost" \
  --env GIT_COMMITTER_NAME="agentos-bot" \
  --env GIT_COMMITTER_EMAIL="agentos@localhost" \
  agentos:local run \
    --objective "improve engagement" \
    --skip-deploy
```

---

## Security hardening applied

The container runs with:

- **Non-root user** (`agentuser`, UID 1000) — no host UID privileges
- **Read-only root filesystem** — only `/workspace` and `/home/agentuser/.claude` are writable
- **`--cap-drop ALL`** — all Linux capabilities removed
- **`tmpfs /tmp`** — ephemeral scratch space (256 MB), wiped on exit
- **No inbound ports** — the container is a pure outbound worker

---

## Rebuilding the image

```bash
# After changing pyproject.toml, agentos/ source, or the Dockerfile:
docker build --no-cache -t agentos:local .
```

Or with compose:

```bash
docker compose build --no-cache
```
