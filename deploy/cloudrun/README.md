# agentra-engine on Cloud Run

**This covers the platform deploy pipeline only** — building the engine image and
shipping it to Cloud Run. It is unrelated to the `agentra-orchestrator` GitHub App
and issue access (that's broader, and covered in `SPLIT.md`).

Two services in `agentra-prod`, one region:

| Branch merge | Service | Role | Verified by |
|---|---|---|---|
| `beta` | `agentra-engine-preprod` | pre-prod | the Testing Agent (agentra-loop), post-merge |
| `main` | `agentra-engine` | production | Cloud Scheduler `/healthz` ping |

`.github/workflows/deploy.yml` builds + deploys on each push, authenticating with
Workload Identity Federation (no service-account keys in the repo). It no-ops
cleanly until the setup below has run.

## One-time setup

Run the script — don't paste it line by line (the `gcloud --format='value(...)'`
args trip up zsh):

```bash
bash deploy/cloudrun/setup.sh
```

It's idempotent. Creates: the runtime SA (`datastore.user` + secret access), the
deploy SA (`run.admin`, Cloud Build, Artifact Registry, act-as the runtime SA),
the org-scoped WIF pool with a binding for this repo only, and the five GitHub
repo variables. Override with `GCP_PROJECT` / `GCP_REGION` / `GITHUB_REPO` env
vars.

## First deploy

```bash
gh workflow run deploy.yml --repo Srijanlab/agentra-engine --ref beta -f target=preprod
# then, once pre-prod checks out:
gh workflow run deploy.yml --repo Srijanlab/agentra-engine --ref main -f target=prod
```

## Keep-warm + scheduled tick

```bash
PROJECT=agentra-prod; REGION=us-central1
RUN_SA=agentra-engine-run@$PROJECT.iam.gserviceaccount.com
PROD_URL=$(gcloud run services describe agentra-engine --region "$REGION" --project "$PROJECT" --format 'value(status.url)')

# every 10 min: hold a warm prod instance
gcloud scheduler jobs create http agentra-engine-warm --project "$PROJECT" --location "$REGION" \
  --schedule "*/10 * * * *" --uri "$PROD_URL/healthz" \
  --oidc-service-account-email "$RUN_SA"

# scheduled autonomous-cycle tick (replaces the VM's agentra-trigger-loop)
gcloud scheduler jobs create http agentra-scheduled-cycle --project "$PROJECT" --location "$REGION" \
  --schedule "0 */2 * * *" --http-method POST --uri "$PROD_URL/trigger/scheduled" \
  --headers "Content-Type=application/json" --message-body "{}" \
  --oidc-service-account-email "$RUN_SA"
```

## Wiring the agentic pipeline

The Deployment Agent (in agentra-loop) treats this repo as `AGENTRA_CI_CD_ON_PUSH=true`:
it merges a feature branch to `beta`, the workflow deploys `agentra-engine-preprod`,
and the agent watches the run with `gh run watch`. The Testing Agent then verifies
against `AGENTRA_PREPROD_URL`. Promote = merge `beta` -> `main`, same watch.

```bash
REPO=Srijanlab/agentra-engine
PREPROD_URL=$(gcloud run services describe agentra-engine-preprod --region us-central1 --project agentra-prod --format 'value(status.url)')
gh variable set AGENTRA_PRE_PROD_BRANCH --repo "$REPO" --body beta
gh variable set AGENTRA_PROD_BRANCH     --repo "$REPO" --body main
gh variable set AGENTRA_CI_CD_ON_PUSH   --repo "$REPO" --body true
gh variable set AGENTRA_PREPROD_URL     --repo "$REPO" --body "$PREPROD_URL"
```
