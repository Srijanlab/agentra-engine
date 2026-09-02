#!/usr/bin/env bash
# One-time GCP + GitHub setup for deploying agentra-engine to Cloud Run.
# Idempotent — safe to re-run. Run it as a file, don't copy-paste line by line:
#     bash deploy/cloudrun/setup.sh
#
# This is ONLY the platform deploy pipeline (build image -> Cloud Run). It has
# nothing to do with the agentra-orchestrator GitHub App / issue access — that
# is a separate, broader concern (see SPLIT.md).
set -euo pipefail

PROJECT="${GCP_PROJECT:-agentra-prod}"
REGION="${GCP_REGION:-us-central1}"
REPO="${GITHUB_REPO:-Srijanlab/agentra-engine}"
PROJECT_NUM="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"

RUN_SA="agentra-engine-run@${PROJECT}.iam.gserviceaccount.com"
DEPLOY_SA="agentra-engine-deploy@${PROJECT}.iam.gserviceaccount.com"
WIF="projects/${PROJECT_NUM}/locations/global/workloadIdentityPools/github/providers/github"

say() { printf '\n=== %s ===\n' "$1"; }
sa_exists() { gcloud iam service-accounts describe "$1" --project "$PROJECT" >/dev/null 2>&1; }
wait_for_sa() {  # new SAs take a few seconds to be usable in IAM bindings
  for _ in $(seq 1 30); do sa_exists "$1" && return 0; sleep 2; done
  echo "timed out waiting for $1" >&2; return 1
}

say "APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  iamcredentials.googleapis.com sts.googleapis.com --project "$PROJECT"

say "Artifact Registry (reuse the existing 'agentra' repo)"
gcloud artifacts repositories describe agentra --location "$REGION" --project "$PROJECT" >/dev/null 2>&1 \
  || gcloud artifacts repositories create agentra --repository-format docker \
       --location "$REGION" --project "$PROJECT"

say "Runtime service account: $RUN_SA"
sa_exists "$RUN_SA" || { gcloud iam service-accounts create agentra-engine-run \
  --project "$PROJECT" --display-name "agentra-engine runtime"; wait_for_sa "$RUN_SA"; }
gcloud projects add-iam-policy-binding "$PROJECT" --condition=None \
  --member "serviceAccount:${RUN_SA}" --role roles/datastore.user >/dev/null
for S in agentra-github-app-id agentra-github-app-private-key agentra-github-token \
         agentra-slack-signing-secret agentra-slack-bot-token agentra-alarm-webhook-password; do
  gcloud secrets add-iam-policy-binding "$S" --project "$PROJECT" \
    --member "serviceAccount:${RUN_SA}" --role roles/secretmanager.secretAccessor >/dev/null
done

say "Deploy service account: $DEPLOY_SA"
sa_exists "$DEPLOY_SA" || { gcloud iam service-accounts create agentra-engine-deploy \
  --project "$PROJECT" --display-name "agentra-engine CI deploy"; wait_for_sa "$DEPLOY_SA"; }
for ROLE in roles/run.admin roles/cloudbuild.builds.editor roles/artifactregistry.writer \
            roles/iam.serviceAccountUser roles/storage.admin roles/logging.viewer; do
  gcloud projects add-iam-policy-binding "$PROJECT" --condition=None \
    --member "serviceAccount:${DEPLOY_SA}" --role "$ROLE" >/dev/null
done
# the deploy SA must be allowed to act as the runtime SA on `gcloud run deploy`
gcloud iam service-accounts add-iam-policy-binding "$RUN_SA" --project "$PROJECT" \
  --member "serviceAccount:${DEPLOY_SA}" --role roles/iam.serviceAccountUser >/dev/null

say "Workload Identity Federation (org-scoped pool, per-repo binding)"
gcloud iam workload-identity-pools describe github --project "$PROJECT" --location global >/dev/null 2>&1 \
  || gcloud iam workload-identity-pools create github --project "$PROJECT" --location global \
       --display-name "GitHub Actions"
gcloud iam workload-identity-pools providers describe github --project "$PROJECT" --location global \
  --workload-identity-pool github >/dev/null 2>&1 \
  || gcloud iam workload-identity-pools providers create-oidc github \
       --project "$PROJECT" --location global --workload-identity-pool github --display-name GitHub \
       --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
       --attribute-condition "assertion.repository_owner=='Srijanlab'" \
       --issuer-uri "https://token.actions.githubusercontent.com"
gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA" --project "$PROJECT" \
  --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/${PROJECT_NUM}/locations/global/workloadIdentityPools/github/attribute.repository/${REPO}" >/dev/null

say "GitHub repo variables on $REPO"
gh variable set GCP_PROJECT  --repo "$REPO" --body "$PROJECT"
gh variable set GCP_REGION   --repo "$REPO" --body "$REGION"
gh variable set WIF_PROVIDER --repo "$REPO" --body "$WIF"
gh variable set DEPLOY_SA    --repo "$REPO" --body "$DEPLOY_SA"
gh variable set RUNTIME_SA   --repo "$REPO" --body "$RUN_SA"

# --set-secrets for `gcloud run deploy`, as a variable so it's easy to extend.
# Only secrets with a live version — agentra-github-app-{id,private-key} have
# all versions DESTROYED, so the engine runs on the PAT (GITHUB_TOKEN) until
# the App key is regenerated (then add GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY).
SECRETS="GITHUB_TOKEN=agentra-github-token:latest"
for S in agentra-slack-signing-secret:SLACK_SIGNING_SECRET \
         agentra-slack-bot-token:SLACK_BOT_TOKEN \
         agentra-alarm-webhook-password:AGENTRA_ALARM_WEBHOOK_PASSWORD; do
  SECRETS="${SECRETS},${S##*:}=${S%%:*}:latest"
done
gh variable set ENGINE_SECRETS --repo "$REPO" --body "$SECRETS"

say "Done"
cat <<EOF
Next:
  gh workflow run deploy.yml --repo $REPO --ref beta -f target=preprod
  # then, once pre-prod checks out:
  gh workflow run deploy.yml --repo $REPO --ref main -f target=prod

Then run the schedulers + agentic-pipeline variables — see the rest of README.md.
EOF
