#!/usr/bin/env bash
# Set agentra-engine's Vercel env vars. Run after: vercel login && vercel link
#   (scope: roshan-sharma-s-sentinel, project: agentra-engine)
#
# GitHub access = the agentra-orchestrator GitHub App (per-repo installation
# tokens). Regenerate its key: github.com/settings/apps/agentra-orchestrator ->
# Private keys -> Generate. GITHUB_TOKEN (a PAT) is only a fallback.
#
# Provide values via env or the prompts:
#   AGENTRA_DYNAMODB_TABLE_PREFIX   - the AgentraData stack's table prefix
#   AGENTRA_AWS_ACCESS_KEY_ID       - the DynamoDB IAM user's key id
#   AGENTRA_AWS_SECRET_ACCESS_KEY   - the DynamoDB IAM user's secret
#   GITHUB_APP_ID                   - the App ID (a number, from the App page)
#   GITHUB_APP_PRIVATE_KEY_FILE     - path to the downloaded .pem
#   SLACK_SIGNING_SECRET            - api.slack.com/apps -> Basic Information
#   SLACK_BOT_TOKEN                 - api.slack.com/apps -> OAuth & Permissions (xoxb-)
#   ALARM_WEBHOOK_PASSWORD          - optional; unset leaves /trigger/alarm open
#   GITHUB_TOKEN                    - optional PAT fallback (repo scope)
set -euo pipefail

set_var() {  # name value
  [ -z "${2:-}" ] && { echo "  skip $1 (empty)"; return; }
  for env in production preview development; do
    vercel env rm "$1" "$env" --yes >/dev/null 2>&1 || true
    printf '%s' "$2" | vercel env add "$1" "$env" >/dev/null
  done
  echo "  set $1"
}
ask() { local v="${!1:-}"; [ -z "$v" ] && read -rsp "  $2: " v && echo; printf '%s' "$v"; }

echo "state (DynamoDB):"
set_var AGENTRA_DYNAMODB_TABLE_PREFIX "$(ask AGENTRA_DYNAMODB_TABLE_PREFIX 'DynamoDB table prefix')"
set_var AGENTRA_AWS_REGION            "${AGENTRA_AWS_REGION:-us-west-2}"
set_var AGENTRA_AWS_ACCESS_KEY_ID     "$(ask AGENTRA_AWS_ACCESS_KEY_ID 'DynamoDB IAM key id')"
set_var AGENTRA_AWS_SECRET_ACCESS_KEY "$(ask AGENTRA_AWS_SECRET_ACCESS_KEY 'DynamoDB IAM secret')"

echo "sign-in gate:"
set_var FIREBASE_PROJECT_ID     "$(ask FIREBASE_PROJECT_ID 'Firebase project id')"
set_var AGENTRA_ALLOWED_EMAILS  "${AGENTRA_ALLOWED_EMAILS:-rossharma1@gmail.com}"
set_var AGENTRA_INTERNAL_TOKEN  "$(ask AGENTRA_INTERNAL_TOKEN 'Internal RPC token (openssl rand -hex 32)')"

echo "github app:"
set_var GITHUB_APP_ID          "$(ask GITHUB_APP_ID 'GitHub App ID')"
PEM="${GITHUB_APP_PRIVATE_KEY:-}"
[ -z "$PEM" ] && [ -n "${GITHUB_APP_PRIVATE_KEY_FILE:-}" ] && PEM="$(cat "$GITHUB_APP_PRIVATE_KEY_FILE")"
[ -z "$PEM" ] && read -rp "  path to App .pem: " f && PEM="$(cat "$f")"
set_var GITHUB_APP_PRIVATE_KEY "$PEM"

echo "slack + misc:"
set_var SLACK_SIGNING_SECRET          "$(ask SLACK_SIGNING_SECRET 'Slack signing secret')"
set_var SLACK_BOT_TOKEN               "$(ask SLACK_BOT_TOKEN 'Slack bot token (xoxb-)')"
set_var ALARM_WEBHOOK_PASSWORD        "${ALARM_WEBHOOK_PASSWORD:-}"   # optional
set_var GITHUB_TOKEN                  "${GITHUB_TOKEN:-}"   # optional fallback

echo
echo "Done. Redeploy: vercel deploy --prod   (or push to main)"
