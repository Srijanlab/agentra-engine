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

The engine's **secret** values (GitHub PAT, Slack tokens, alarm password) are
**not** set in Vercel -- the runtime SA (`agentra-engine-run`, which already has
`secretmanager.secretAccessor`) pulls them from Secret Manager at startup via the
same WIF credentials. Only these non-secret vars go in Vercel:

| var | value |
|---|---|
| `AGENTRA_FIRESTORE_PROJECT` | `agentra-prod` |
| `GCP_WORKLOAD_IDENTITY_CONFIG` | the JSON below (not secret) |
| `FIREBASE_PROJECT_ID` | `agentra-prod` (for the Google-OAuth check) |
| `AGENTRA_ALLOWED_EMAILS` | your email(s), comma-separated |

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
