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

## Pre-prod verification of token-guarded endpoints

`/internal/*` (including `POST /internal/rpc`) is guarded solely by the
`AGENTRA_INTERNAL_TOKEN` bearer of the deployment being tested; it returns 401
without or with a wrong token (503 if unset). The engine does **not** read or
accept `AGENTRA_PREPROD_INTERNAL_TOKEN`. A pre-prod-scoped token is a distinct
`AGENTRA_INTERNAL_TOKEN` value set on the pre-prod (beta) Vercel deployment
only, and it must never equal the production value. The Testing Agent lives in
agentra-loop, so where it reads a token named `AGENTRA_PREPROD_INTERNAL_TOKEN`
is owned and configured there. `/v1/messages` is served by the separate NIM
proxy (`agentra/proxy/main.py`), not the engine app. LLM rotation state can be read with the read-only `GET /debug/llm-rotation`,
which returns only `{"backends": [...], "current_index": <int>}` (no health,
cooldown or secrets; other methods return 405). It and `GET /debug/dynamodb`
sit behind the Firebase sign-in gate (401 without a valid ID token whenever
`FIREBASE_PROJECT_ID` is set); the loop reads rotation state through
`/internal/rpc` `get_llm_rotation`. State-changing calls (`set_llm_rotation`,
`select_llm_provider`) still require the deployment's own
`AGENTRA_INTERNAL_TOKEN`; rotation behaviour is covered by
`tests/test_llm_rotation_e2e.py`.
`GET /` on an API-only deploy (no built dashboard) returns
`{"status": "ok", "service": "agentra-engine", "commit": ...}`.

## Triggers

`POST /trigger/scheduled`, `/trigger/alarm` (HTTP Basic, `ALARM_WEBHOOK_PASSWORD`),
`/trigger/queue`, and `POST /apps/{name}/run`. Each checks the durable pause
marker (`registry.PAUSE_PATH` / the `system` table) first and no-ops while paused.

`POST /trigger/queue` is fail-closed: every request returns 401 unless it
carries `Authorization: Bearer <AGENTRA_INTERNAL_TOKEN>` or a Google-signed
Pub/Sub push OIDC token. The OIDC path is only tried when
`AGENTRA_PUBSUB_AUDIENCE` is set (the token's audience); if
`AGENTRA_PUBSUB_SERVICE_ACCOUNT_EMAIL` is also set, the token's verified `email`
claim must equal it. With neither `AGENTRA_INTERNAL_TOKEN` nor
`AGENTRA_PUBSUB_AUDIENCE` configured, the endpoint rejects everything. Queue
senders (SQS forwarders, Pub/Sub push subscriptions) must send the bearer token
or configure the subscription's OIDC authentication accordingly.

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
