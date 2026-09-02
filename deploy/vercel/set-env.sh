#!/usr/bin/env bash
# Set agentra-engine's Vercel env vars. Run after: vercel login && vercel link
#   (scope: roshan-sharma-s-sentinel, project: agentra-engine)
#
# Non-secret vars are baked in. Secret values: export them first, or the script
# prompts. GCP Secret Manager is billing-gated (project billing is closed), so
# these can't be pulled from there.
#   GITHUB_TOKEN                    - a GitHub PAT with repo scope (e.g. `gh auth token`)
#   SLACK_SIGNING_SECRET            - api.slack.com/apps -> Basic Information
#   SLACK_BOT_TOKEN                 - api.slack.com/apps -> OAuth & Permissions (xoxb-...)
#   AGENTRA_ALARM_WEBHOOK_PASSWORD  - your choice; match it in the alarm sender
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

ask() {  # varname prompt
  local v="${!1:-}"
  [ -z "$v" ] && read -rsp "  $2: " v && echo
  printf '%s' "$v"
}

echo "non-secret:"
set_var AGENTRA_FIRESTORE_PROJECT    agentra-prod
set_var GCP_WORKLOAD_IDENTITY_CONFIG "$WIF_JSON"
set_var FIREBASE_PROJECT_ID          agentra-prod
set_var AGENTRA_ALLOWED_EMAILS       "${AGENTRA_ALLOWED_EMAILS:-rossharma1@gmail.com}"

echo "secrets:"
set_var GITHUB_TOKEN                   "$(ask GITHUB_TOKEN 'GitHub PAT (repo scope)')"
set_var SLACK_SIGNING_SECRET           "$(ask SLACK_SIGNING_SECRET 'Slack signing secret')"
set_var SLACK_BOT_TOKEN               "$(ask SLACK_BOT_TOKEN 'Slack bot token (xoxb-)')"
set_var AGENTRA_ALARM_WEBHOOK_PASSWORD "$(ask AGENTRA_ALARM_WEBHOOK_PASSWORD 'Alarm webhook password')"

echo
echo "Done. Redeploy: vercel deploy --prod   (or push to main)"
