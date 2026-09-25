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

## Pre-prod domain

The `beta` branch deploys to a Vercel **Preview** deployment, which Vercel gives
an auto-generated, personal-looking URL of the form
`agentra-engine-git-beta-<vercel-account>.vercel.app` (e.g.
`agentra-engine-git-beta-roshan-sharma-s-sentinel.vercel.app`). That URL, tied
to whichever account owns the Vercel project, *is* the intended pre-prod
backend address today — it is not a stray personal deploy, and the loop's
Testing Agent and any pre-prod dashboard are expected to point at it. A stable,
branded custom domain for pre-prod (aliased the way `main` -> Production
already has one) is a tracked follow-up to do before promoting the engine out
of pre-prod; until then, the Preview URL changing if the underlying Vercel
project or account changes is expected, not a bug (issue #83).

## Pre-prod verification

Pre-prod (beta, a Vercel Preview) must be isolated from production and the live loop:

- Its own `AGENTRA_DYNAMODB_TABLE_PREFIX` — never the prod/live-loop prefix — so verification traffic cannot touch live registry state.
- Its own distinct `AGENTRA_INTERNAL_TOKEN`, `AGENTRA_TICK_TOKEN` and `AGENTRA_VERIFY_TOKEN` values, never equal to production's.

`/internal/*` (including `POST /internal/rpc`) is guarded solely by the deployment's `AGENTRA_INTERNAL_TOKEN` bearer (401 without or wrong, 503 if unset). The engine does **not** read `AGENTRA_PREPROD_INTERNAL_TOKEN`; the Testing Agent (agentra-loop) owns where it stores the pre-prod token.

`GET /trigger/cron` accepts `Authorization: Bearer <AGENTRA_TICK_TOKEN>` or `CRON_SECRET`. `AGENTRA_INTERNAL_TOKEN` is accepted only as a backward-compat fallback while `AGENTRA_TICK_TOKEN` is unset. It fails closed (401) on a DynamoDB-configured deployment with no tick credential.

`AGENTRA_VERIFY_TOKEN` (unset = disabled) enables a read-only user-facing path for black-box checks: send `X-Agentra-Verify-Token: <token>` (constant-time compared) with no Firebase token. It is honored only for `GET /apps`, `GET /apps/{name}/schedule`, `GET /runs/{run_key}`, `GET /loops/{loop_id}/context` and `GET /openapi.json`; any other route (including `GET /loops`, `GET /loops/{loop_id}` and `/internal/loops/{loop_id}/context`) or method, and a wrong or empty value, returns 401. It never authorizes `/trigger/cron` or `/internal/*`. It must NOT be set on Production: when `VERCEL_ENV` or `AGENTRA_ENVIRONMENT` is `production`, any request carrying the header gets 403.

Leading/trailing whitespace or newlines on both the configured `AGENTRA_VERIFY_TOKEN` and the supplied header are trimmed before comparison; a blank/whitespace-only configured token counts as unset (fails closed). `GET /health` and `GET /healthz` expose a public boolean `verify_token_enabled` (true only when the token is non-blank and the deployment is not production) so a verifier can confirm the feature is on without seeing the secret.

Provisioning contract for pre-prod verification: set `AGENTRA_VERIFY_TOKEN` in the beta/Preview Vercel environment only (never Production). The engine does **not** read `AGENTRA_PREPROD_INTERNAL_TOKEN` or `AGENTRA_PREPROD_ENGINE_URL` (those are agentra-loop Testing Agent settings). The loop's verifier must send the same value in `X-Agentra-Verify-Token` against the pre-prod engine URL, and can check `GET /health` `verify_token_enabled` first. Provisioning the secret on the beta deployment and wiring it into the loop's verifier are infra/human steps this repo cannot perform.

A 401 on `/loops/{id}/context` or `/internal/loops/{id}/context` because no credential was provisioned for the verifier is classified as **unverified** (missing credential), not a product failure; check `GET /health` `verify_token_status` (`enabled|not_configured|disabled_in_production`) up front. A verifier can discover a `loop_id` from a run record (`GET /runs/{run_key}`) when it carries `loop_id`; `GET /loops` is deliberately not opened to the verify token.

The OpenAPI schema (`/openapi.json`) and API docs pages (`/docs`, `/redoc`) are intentionally auth-gated: unauthenticated callers get 401 `authentication_required`. On pre-prod/non-production deployments only, black-box verifiers can read `/openapi.json` (and only that; `/docs` and `/redoc` stay 401) with `GET` plus the `X-Agentra-Verify-Token` header; production refuses the token with 403, and non-GET methods are never authorized.

Every 401 from the Firebase gate or the verify-token gate (missing credentials, an invalid/expired Firebase token, or a wrong/ineligible verify token) returns the same JSON shape: `{"detail": ..., "error": "authentication_required", "hint": ...}`, where `hint` names both accepted auth flows (a Firebase ID token via `Authorization: Bearer <token>`, or `X-Agentra-Verify-Token` on eligible read-only GET routes) without leaking any secret value, allowlisted email, or other config (issue #83).

`/v1/messages` is served by the separate NIM
proxy (`agentra/proxy/main.py`), not the engine app. LLM rotation state can be read with the read-only `GET /debug/llm-rotation`,
which returns only `{"backends": [...], "current_index": <int>}` (no health,
cooldown or secrets; other methods return 405). It and `GET /debug/dynamodb`
sit behind the Firebase sign-in gate (401 without a valid ID token whenever
`FIREBASE_PROJECT_ID` is set; `FIREBASE_PROJECT_ID` and `AGENTRA_ALLOWED_EMAILS` are required
whenever DynamoDB is configured, otherwise gated routes return 503 `auth_misconfigured`
and `/health` reports `auth.mode: "misconfigured"`); the loop reads rotation state through
`/internal/rpc` `get_llm_rotation`. State-changing calls (`set_llm_rotation`,
`select_llm_provider`) still require the deployment's own
`AGENTRA_INTERNAL_TOKEN`; rotation behaviour is covered by
`tests/test_llm_rotation_e2e.py`.
`GET /` on an API-only deploy (no built dashboard) returns
`{"status": "ok", "service": "agentra-engine", "commit": ...}`.

## Triggers

`POST /trigger/scheduled`, `/trigger/alarm` (HTTP Basic, `ALARM_WEBHOOK_PASSWORD` -- required in production;
unset, it returns 401 in cloud mode and is open only for local dev),
`/trigger/queue`, and `POST /apps/{name}/run`. Each checks the durable pause
marker (`registry.PAUSE_PATH` / the `system` table) first and no-ops while paused.

`POST /trigger/queue` is fail-closed: every request returns 401 unless it
carries `Authorization: Bearer <AGENTRA_INTERNAL_TOKEN>` or a Google-signed
Pub/Sub push OIDC token. The OIDC path is only tried when
`AGENTRA_PUBSUB_AUDIENCE` is set (the token's audience).
`AGENTRA_PUBSUB_SERVICE_ACCOUNT_EMAIL` is **required** whenever the audience is
set: the token's verified `email` claim must equal it. Without it, every OIDC
request is rejected with 401 and a startup warning is logged. With neither `AGENTRA_INTERNAL_TOKEN` nor
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
