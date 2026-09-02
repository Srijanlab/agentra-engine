#!/usr/bin/env bash
# Set agentra-engine's Vercel env vars (all non-secret; the engine pulls its
# secrets from GCP Secret Manager). Run after: vercel login && vercel link
#   (scope: roshan-sharma-s-sentinel, project: agentra-engine)
set -euo pipefail

WIF_JSON='{"type":"external_account","audience":"//iam.googleapis.com/projects/801839294441/locations/global/workloadIdentityPools/github/providers/vercel","subject_token_type":"urn:ietf:params:oauth:token-type:jwt","token_url":"https://sts.googleapis.com/v1/token","credential_source":{"file":"/tmp/agentra_vercel_oidc_token"},"service_account_impersonation_url":"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/agentra-engine-run@agentra-prod.iam.gserviceaccount.com:generateAccessToken"}'
EMAILS="${AGENTRA_ALLOWED_EMAILS:-rossharma1@gmail.com}"

set_var() {  # name value
  for env in production preview development; do
    vercel env rm "$1" "$env" --yes >/dev/null 2>&1 || true
    printf '%s' "$2" | vercel env add "$1" "$env" >/dev/null
    echo "  $1 -> $env"
  done
}

set_var AGENTRA_FIRESTORE_PROJECT    agentra-prod
set_var GCP_WORKLOAD_IDENTITY_CONFIG "$WIF_JSON"
set_var FIREBASE_PROJECT_ID          agentra-prod
set_var AGENTRA_ALLOWED_EMAILS       "$EMAILS"

echo
echo "Done. Still do by hand: Settings -> Security -> enable OIDC / Secure Backend Access."
echo "Then: vercel deploy --prod   (or push to main)"
