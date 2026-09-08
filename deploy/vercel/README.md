# agentra-engine on Vercel

FastAPI as one serverless function (`api/index.py` + `vercel.json` rewrite).
Connect `Srijanlab/agentra-engine` in the Vercel dashboard:
- `main`  -> Production
- `beta`  -> a Preview deployment (alias it to a stable URL for the Testing Agent)

## State: DynamoDB

Every registry collection (apps, runs, loops, requests, gh-cache, slack-threads,
memory, `system`) lives in DynamoDB, one table per collection, name-prefixed by
`AGENTRA_DYNAMODB_TABLE_PREFIX`. The tables are provisioned by the loop's CDK
(`deploy/aws` in `agentra-loop`, the `AgentraData` stack). Auth is a static IAM
user's keys -- `AGENTRA_AWS_*`, prefixed because Vercel's runtime reserves the
bare `AWS_*` names for its own execution role. With `AGENTRA_DYNAMODB_TABLE_PREFIX`
unset (local dev, CI) the registry falls back to JSON files under `~/.agentra`.

## Sign-in gate

`FIREBASE_PROJECT_ID` turns on the Google-identity check in `server/auth.py`
(`id_token.verify_firebase_token`, from `google-auth`). Unset -> the API stays
open (local dev). `AGENTRA_ALLOWED_EMAILS` is the allowlist.

## Vercel env vars

Set every var in **both** environments -- Production (`main` -> prod) and Preview
(`beta` -> pre-prod) -- with `bash deploy/vercel/set-env.sh` or the dashboard.
Full list with placeholders: [`.env.example`](.env.example).

| var | value |
|---|---|
| `AGENTRA_DYNAMODB_TABLE_PREFIX` | the `AgentraData` stack's table prefix (e.g. `agentra-`) |
| `AGENTRA_AWS_REGION` | `us-west-2` |
| `AGENTRA_AWS_ACCESS_KEY_ID` / `AGENTRA_AWS_SECRET_ACCESS_KEY` | the DynamoDB IAM user's keys |
| `FIREBASE_PROJECT_ID` | Firebase project id (for the Google sign-in check) |
| `AGENTRA_ALLOWED_EMAILS` | your email(s), comma-separated |
| `AGENTRA_INTERNAL_TOKEN` | shared bearer for `/internal/*` (same value in the loop's secret) |
| `GITHUB_APP_ID` | `agentra-orchestrator` App ID (`4545406`) |
| `GITHUB_APP_PRIVATE_KEY` | the App's `.pem` contents (multi-line) |
| `GITHUB_TOKEN` | optional PAT fallback (repo scope) |
| `SLACK_SIGNING_SECRET`, `SLACK_BOT_TOKEN` | Slack app credentials |
| `ALARM_WEBHOOK_PASSWORD` | optional; unset leaves `/trigger/alarm` open |

GitHub access is the `agentra-orchestrator` GitHub App (per-repo installation
tokens minted by `agentra/connectors/github_app.py`); the PAT is only a fallback.

`/debug/dynamodb` reports which of these resolved (no secret values).
