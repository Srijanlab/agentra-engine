# agentra-engine on Vercel

FastAPI as one serverless function (`api/index.py` + `vercel.json` rewrite).
Connect `Srijanlab/agentra-engine` in the Vercel dashboard:
- `main`  -> Production
- `beta`  -> a Preview deployment (alias it to a stable URL for the Testing Agent)

## Firestore auth — keyless (Vercel OIDC -> GCP Workload Identity Federation)

`iam.disableServiceAccountKeyCreation` is enforced on `agentra-prod`, so there's
no SA key. Instead Vercel's per-invocation OIDC token is federated to the
`agentra-engine-run` service account.

### 1. Vercel side (you)

Project Settings -> Security -> **Secure Backend Access / OIDC Federation** -> enable.
Issuer mode: **Team**. Note your **team slug** and the **project name**.

### 2. GCP side (run once, after you have the team slug)

```bash
PROJECT_NUM=801839294441
POOL=github            # the existing pool
TEAM=<your-vercel-team-slug>
RUN_SA=agentra-engine-run@agentra-prod.iam.gserviceaccount.com

gcloud iam workload-identity-pools providers create-oidc vercel \
  --project agentra-prod --location global --workload-identity-pool $POOL \
  --display-name Vercel \
  --issuer-uri "https://oidc.vercel.com/$TEAM" \
  --allowed-audiences "https://vercel.com/$TEAM" \
  --attribute-mapping "google.subject=assertion.sub,attribute.project=assertion.project,attribute.environment=assertion.environment"

# only agentra-engine's Vercel project may impersonate the runtime SA
gcloud iam service-accounts add-iam-policy-binding $RUN_SA --project agentra-prod \
  --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/$PROJECT_NUM/locations/global/workloadIdentityPools/$POOL/attribute.project/agentra-engine"
```

### 3. Vercel env vars

GCP Secret Manager is billing-gated, so **all** values live in Vercel. Set every
var in **both** environments -- Production (`main` -> prod) and Preview (`beta` ->
pre-prod) -- with `bash deploy/vercel/set-env.sh` or the dashboard. Full list with
placeholders: [`.env.example`](.env.example).

| var | value |
|---|---|
| `AGENTRA_FIRESTORE_PROJECT` | `agentra-prod` |
| `GCP_WORKLOAD_IDENTITY_CONFIG` | the JSON below (not secret) |
| `FIREBASE_PROJECT_ID` | `agentra-prod` (for the Google-OAuth check) |
| `AGENTRA_ALLOWED_EMAILS` | your email(s), comma-separated |
| `GITHUB_APP_ID` | `agentra-orchestrator` App ID (`4545406`) |
| `GITHUB_APP_PRIVATE_KEY` | the App's `.pem` contents (multi-line) |
| `GITHUB_TOKEN` | optional PAT fallback (repo scope) |
| `SLACK_SIGNING_SECRET`, `SLACK_BOT_TOKEN` | Slack app credentials |
| `AGENTRA_ALARM_WEBHOOK_PASSWORD` | your choice |

GitHub access is the `agentra-orchestrator` GitHub App (per-repo installation
tokens minted by `agentra/connectors/github_app.py`); the PAT is only a fallback.

`GCP_WORKLOAD_IDENTITY_CONFIG`:
```json
{
  "type": "external_account",
  "audience": "//iam.googleapis.com/projects/801839294441/locations/global/workloadIdentityPools/github/providers/vercel",
  "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
  "token_url": "https://sts.googleapis.com/v1/token",
  "credential_source": { "file": "/tmp/agentra_vercel_oidc_token" },
  "service_account_impersonation_url": "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/agentra-engine-run@agentra-prod.iam.gserviceaccount.com:generateAccessToken"
}
```

The engine writes `$VERCEL_OIDC_TOKEN` to that file per request (server middleware)
and `identity_pool.Credentials` exchanges it for a GCP token. No key anywhere.
