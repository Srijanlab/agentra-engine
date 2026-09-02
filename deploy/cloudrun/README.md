# agentra-engine on Cloud Run

Two services in `agentra-prod`, one region:

| Branch merge | Service | Role | Verified by |
|---|---|---|---|
| `beta` | `agentra-engine-preprod` | pre-prod | the Testing Agent (agentra-loop), post-merge |
| `main` | `agentra-engine` | production | Cloud Scheduler `/healthz` ping |

`.github/workflows/deploy.yml` does the build + deploy on each push. It needs
Workload Identity Federation (no service-account keys in the repo) plus a runtime
service account with Firestore + Secret Manager access.

## One-time setup

```bash
PROJECT=agentra-prod
REGION=us-central1
PROJECT_NUM=$(gcloud projects describe $PROJECT --format='value(projectNumber)')
REPO=Srijanlab/agentra-engine

gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  iamcredentials.googleapis.com --project $PROJECT

# --- Artifact Registry (reuse the existing 'agentra' repo) ---
gcloud artifacts repositories describe agentra --location=$REGION --project=$PROJECT \
  || gcloud artifacts repositories create agentra --repository-format=docker \
       --location=$REGION --project=$PROJECT

# --- Runtime service account (what the Cloud Run service runs as) ---
gcloud iam service-accounts create agentra-engine-run --project $PROJECT \
  --display-name "agentra-engine runtime"
RUN_SA=agentra-engine-run@$PROJECT.iam.gserviceaccount.com
gcloud projects add-iam-policy-binding $PROJECT \
  --member "serviceAccount:$RUN_SA" --role roles/datastore.user
for S in agentra-github-app-id agentra-github-app-private-key agentra-github-token \
         agentra-slack-signing-secret agentra-slack-bot-token agentra-alarm-webhook-password; do
  gcloud secrets add-iam-policy-binding $S --project $PROJECT \
    --member "serviceAccount:$RUN_SA" --role roles/secretmanager.secretAccessor
done

# --- Deploy service account (what GitHub Actions authenticates as) ---
gcloud iam service-accounts create agentra-engine-deploy --project $PROJECT \
  --display-name "agentra-engine CI deploy"
DEPLOY_SA=agentra-engine-deploy@$PROJECT.iam.gserviceaccount.com
for ROLE in roles/run.admin roles/cloudbuild.builds.editor \
            roles/artifactregistry.writer roles/iam.serviceAccountUser \
            roles/storage.admin; do
  gcloud projects add-iam-policy-binding $PROJECT \
    --member "serviceAccount:$DEPLOY_SA" --role $ROLE
done

# --- Workload Identity Federation for GitHub Actions ---
gcloud iam workload-identity-pools create github --project $PROJECT --location global \
  --display-name "GitHub Actions"
gcloud iam workload-identity-pools providers create-oidc github \
  --project $PROJECT --location global --workload-identity-pool github \
  --display-name "GitHub" \
  --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition "assertion.repository=='$REPO'" \
  --issuer-uri "https://token.actions.githubusercontent.com"
WIF=projects/$PROJECT_NUM/locations/global/workloadIdentityPools/github/providers/github
gcloud iam service-accounts add-iam-policy-binding $DEPLOY_SA --project $PROJECT \
  --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/$PROJECT_NUM/locations/global/workloadIdentityPools/github/attribute.repository/$REPO"

# --- GitHub repo variables (Settings -> Secrets and variables -> Actions -> Variables) ---
gh variable set GCP_PROJECT  --repo $REPO --body "$PROJECT"
gh variable set GCP_REGION   --repo $REPO --body "$REGION"
gh variable set WIF_PROVIDER --repo $REPO --body "$WIF"
gh variable set DEPLOY_SA    --repo $REPO --body "$DEPLOY_SA"
gh variable set RUNTIME_SA   --repo $REPO --body "$RUN_SA"
```

## First deploy

```bash
# pre-prod (from beta)
gh workflow run deploy.yml --repo $REPO --ref beta -f target=preprod
# prod (from main), once pre-prod checks out
gh workflow run deploy.yml --repo $REPO --ref main -f target=prod
```

## Keep-warm + scheduled tick

```bash
PREPROD_URL=$(gcloud run services describe agentra-engine-preprod --region $REGION --project $PROJECT --format 'value(status.url)')
PROD_URL=$(gcloud run services describe agentra-engine --region $REGION --project $PROJECT --format 'value(status.url)')

# every 10 min: hold a warm prod instance
gcloud scheduler jobs create http agentra-engine-warm --project $PROJECT --location $REGION \
  --schedule "*/10 * * * *" --uri "$PROD_URL/healthz" \
  --oidc-service-account-email $RUN_SA

# scheduled autonomous-cycle tick (replaces the VM's agentra-trigger-loop)
gcloud scheduler jobs create http agentra-scheduled-cycle --project $PROJECT --location $REGION \
  --schedule "0 */2 * * *" --http-method POST --uri "$PROD_URL/trigger/scheduled" \
  --headers "Content-Type=application/json" --message-body "{}" \
  --oidc-service-account-email $RUN_SA
```

## Wiring the agentic pipeline

The Deployment Agent (in agentra-loop) treats this repo as `AGENTRA_CI_CD_ON_PUSH=true`:
it merges a feature branch to `beta`, the workflow above deploys
`agentra-engine-preprod`, and the agent watches the run with `gh run watch`. The
Testing Agent then verifies against `$PREPROD_URL`. Promote = merge `beta` -> `main`,
same watch. Set on this repo:

```bash
gh variable set AGENTRA_PRE_PROD_BRANCH --repo $REPO --body beta
gh variable set AGENTRA_PROD_BRANCH     --repo $REPO --body main
gh variable set AGENTRA_CI_CD_ON_PUSH   --repo $REPO --body true
gh variable set AGENTRA_PREPROD_URL     --repo $REPO --body "$PREPROD_URL"
```
