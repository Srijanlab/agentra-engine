#!/usr/bin/env bash
# Set agentra-engine's Vercel env vars. Run after: vercel login && vercel link
#   (scope: roshan-sharma-s-sentinel, project: agentra-engine)
#
# GitHub access = the agentra-orchestrator GitHub App (per-repo installation
# tokens). Regenerate its key: github.com/settings/apps/agentra-orchestrator ->
# Private keys -> Generate. GITHUB_TOKEN (a PAT) is only a fallback.
#
# Provide secret values via env or the prompts (GCP Secret Manager is
# billing-gated, so they can't be pulled from there):
#   GITHUB_APP_ID                   - the App ID (a number, from the App page)
#   GITHUB_APP_PRIVATE_KEY_FILE     - path to the downloaded .pem
#   SLACK_SIGNING_SECRET            - api.slack.com/apps -> Basic Information
#   SLACK_BOT_TOKEN                 - api.slack.com/apps -> OAuth & Permissions (xoxb-)
#   ALARM_WEBHOOK_PASSWORD          - optional; unset leaves /trigger/alarm open
#   GITHUB_TOKEN                    - optional PAT fallback (repo scope)
set -euo pipefail

WIF_JSON='{"type":"external_account","audience":"//iam.googleapis.com/projects/801839294441/locations/global/workloadIdentityPools/github/providers/vercel","subject_token_type":"urn:ietf:params:oauth:token-type:jwt","token_url":"https://sts.googleapis.com/v1/token","credential_source":{"file":"/tmp/agentra_vercel_oidc_token"},"service_account_impersonation_url":"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/agentra-engine-run@agentra-prod.iam.gserviceaccount.com:generateAccessToken"}'

set_var() {  # name value
  [ -z "${2:-}" ] && { echo "  skip $1 (empty)"; return; }
  for env in production preview development; do
    vercel env rm "$1" "$env" --yes >/dev/null 2>&1 || true
    printf '%s' "$2" | vercel env add "$1" "$env" >/dev/null
  done
  echo "  set $1"
}
ask() { local v="${!1:-}"; [ -z "$v" ] && read -rsp "  $2: " v && echo; printf '%s' "$v"; }

echo "non-secret:"
set_var AGENTRA_FIRESTORE_PROJECT    agentra-prod
set_var GCP_WORKLOAD_IDENTITY_CONFIG "$WIF_JSON"
set_var FIREBASE_PROJECT_ID          agentra-prod
set_var AGENTRA_ALLOWED_EMAILS       "${AGENTRA_ALLOWED_EMAILS:-rossharma1@gmail.com}"

echo "github app:"
set_var GITHUB_APP_ID          "$(ask GITHUB_APP_ID 'GitHub App ID')"
PEM="${GITHUB_APP_PRIVATE_KEY:-}"
[ -z "$PEM" ] && [ -n "${GITHUB_APP_PRIVATE_KEY_FILE:-}" ] && PEM="$(cat "$GITHUB_APP_PRIVATE_KEY_FILE")"
[ -z "$PEM" ] && read -rp "  path to App .pem: " f && PEM="$(cat "$f")"
set_var GITHUB_APP_PRIVATE_KEY "$PEM"

echo "slack + misc:"
set_var SLACK_SIGNING_SECRET           "$(ask SLACK_SIGNING_SECRET 'Slack signing secret')"
set_var SLACK_BOT_TOKEN               "$(ask SLACK_BOT_TOKEN 'Slack bot token (xoxb-)')"
set_var ALARM_WEBHOOK_PASSWORD        "${ALARM_WEBHOOK_PASSWORD:-}"   # optional
set_var GITHUB_TOKEN                   "${GITHUB_TOKEN:-}"   # optional fallback

echo
echo "Done. Redeploy: vercel deploy --prod   (or push to main)"
