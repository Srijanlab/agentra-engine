# Deploying agentra-engine

The engine is the API + state authority. It runs as **one Vercel serverless
function** (`api/index.py` + the `vercel.json` rewrite) and holds every
credential and every piece of registry state.

- `main` -> Vercel Production
- `beta` -> a Vercel Preview deployment (alias it to a stable URL for the loop's
  Testing Agent)

There is no CDK/Terraform for the engine — Vercel's Git integration builds and
deploys on push. Setup, the full env-var list, and the sign-in gate are in
[`deploy/vercel/README.md`](../deploy/vercel/README.md).

## State

All registry collections (apps, runs, loops, requests, gh-cache, slack-threads,
memory, `system`) live in **DynamoDB**, one table per collection, prefixed by
`AGENTRA_DYNAMODB_TABLE_PREFIX`. The tables are provisioned by the loop's CDK
(`AgentraData` stack in `agentra-loop/deploy/aws`). With that env var unset
(local dev, CI) the registry falls back to JSON files under `~/.agentra`.

## The loop boundary

`agentra-loop` runs the cycles and reaches engine state over HTTP:

- `POST /internal/rpc` — whitelisted `registry.*` / `Memory.*` calls
- `POST /internal/git-token` — per-repo GitHub App installation tokens
- `POST /internal/slack/message` — Slack replies via the engine's bot token

All `/internal/*` routes require the shared `AGENTRA_INTERNAL_TOKEN` bearer.

## Triggers

`POST /trigger/scheduled`, `/trigger/alarm` (HTTP Basic, `ALARM_WEBHOOK_PASSWORD`),
`/trigger/queue`, and `POST /apps/{name}/run`. Each checks the durable pause
marker (`registry.PAUSE_PATH` / the `system` table) first and no-ops while paused.

## Slack

`POST /slack/events` handles both the #68 human-input thread flow and the
ask/act assistant (`agentra/agents/slack_assistant.py`). Import
`docs/slack-app-manifest.json` to configure the Slack app. `SLACK_BOT_TOKEN`
posts messages; `SLACK_ALLOWED_USERS` optionally gates senders.

## Model backend

A global toggle (dashboard -> Account Settings, or `GET`/`POST
/system/llm-backend`) routes agent LLM traffic either straight to
`api.anthropic.com` or through the self-hosted NVIDIA NIM proxy
(`agentra/proxy/main.py`). Default `claude`; stored in the `system` table; takes
effect on the next agent turn.
