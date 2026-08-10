# Deploying agentra to GCP

The always-on orchestrator (`agentra/server.py`, run via `agentra serve`) is
deployed as a Cloud Run service in its own dedicated project, **agentra-prod**,
separate from any app it might manage (e.g. ContentAutomationPlatform's own
`cap-prod-503116`). Infrastructure is defined in `deploy/gcp/terraform/`.

Specialized agents are not deployed separately — they run as short-lived
subprocesses the Claude Agent SDK spawns inside the orchestrator service
itself (`agents/base.py::run_agent`), the same as every other entry point in
this codebase. "On demand, not a standing service" was already true of that
architecture before this deployment existed.

## Current state: deployed idle

This deployment intentionally has **zero apps registered**
(`agentra apps list` is empty). It's live and reachable, but there is
nothing for it to act on. Real app registration is planned via an admin UI
(not part of this repo yet) — until that exists, register one manually with
`agentra apps add <name> --repo <path>` against the running instance, or via
`gcloud run services proxy` / a one-off exec.

Known limitation this implies: the multi-app registry
(`~/.agentra/apps.json`, `agentra/registry.py`) lives on the Cloud Run
instance's ephemeral local disk — it does not survive an instance restart.
Fine while idle; needs a durable backing store (Firestore, or a
Filestore/GCS FUSE mount) before it's relied on for a real registered app.
Likewise, a registered app's own repo checkout would need to live on that
same ephemeral disk (cloned on first use, mirroring `docker-entrypoint.sh`'s
existing "server / clone-on-start" mode) — not yet wired up for the
multi-app case, only for the original single-repo container mode.

## One-time setup

```bash
# 1. Create and configure the project (already done for agentra-prod; steps
#    kept here for reference / a future re-deploy to a different project).
gcloud projects create agentra-prod --name="Agentra"
gcloud billing projects link agentra-prod --billing-account=<ACCOUNT_ID>
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com cloudscheduler.googleapis.com \
  pubsub.googleapis.com cloudbuild.googleapis.com monitoring.googleapis.com \
  logging.googleapis.com iam.googleapis.com cloudresourcemanager.googleapis.com \
  --project=agentra-prod

# 2. terraform init (from deploy/gcp/terraform/)
terraform init

# 3. Bootstrap: APIs + Artifact Registry repo must exist before the image
#    can be pushed, and the image must exist before the Cloud Run service
#    resource can be applied. Two-phase apply:
terraform plan -target=google_project_service.apis \
  -target=google_artifact_registry_repository.agentra \
  -var="claude_code_oauth_token=$(cat .claude_oauth_token)" \
  -var="github_token=$(cat .github_pat)" \
  -out=/tmp/tfplan_bootstrap
terraform apply /tmp/tfplan_bootstrap

# 4. Build and push the image
gcloud builds submit --config=deploy/gcp/cloudbuild.yaml \
  --substitutions=_IMAGE="us-central1-docker.pkg.dev/agentra-prod/agentra/agentra:staging" \
  --project=agentra-prod .

# 5. Apply everything else (Cloud Run service, secrets, Scheduler, Pub/Sub, IAM)
terraform plan \
  -var="claude_code_oauth_token=$(cat .claude_oauth_token)" \
  -var="github_token=$(cat .github_pat)" \
  -out=/tmp/tfplan_full
terraform apply /tmp/tfplan_full
```

After step 5, `terraform output orchestrator_url` is the live service URL.

## Redeploying after a code change

Cloud Run Services (unlike Worker Pools) reliably pick up a new revision
from `gcloud run services update --image=...` even when reusing the mutable
`:staging` tag — confirmed for this deployment. Rebuild and redeploy:

```bash
gcloud builds submit --config=deploy/gcp/cloudbuild.yaml \
  --substitutions=_IMAGE="us-central1-docker.pkg.dev/agentra-prod/agentra/agentra:staging" \
  --project=agentra-prod .
gcloud run services update agentra-orchestrator --region=us-central1 \
  --project=agentra-prod \
  --image="us-central1-docker.pkg.dev/agentra-prod/agentra/agentra:staging"
```

## Secrets and rotation

All three secrets live in Secret Manager (`deploy/gcp/terraform/secrets.tf`),
never as plain env vars or committed to the repo:

| Secret | Used for | Rotation |
|---|---|---|
| `agentra-claude-code-oauth-token` | `CLAUDE_CODE_OAUTH_TOKEN` — the Claude CLI's auth, read before its on-disk credentials file | **Expires.** Re-run `claude login` locally, copy the new `accessToken`, then: `gcloud secrets versions add agentra-claude-code-oauth-token --project=agentra-prod --data-file=-` (paste the token, Ctrl-D). No redeploy needed — Cloud Run reads `version: latest` on each new revision, but for an *existing* running revision to pick it up, restart it: `gcloud run services update agentra-orchestrator --region=us-central1 --project=agentra-prod --no-traffic && gcloud run services update-traffic agentra-orchestrator --region=us-central1 --project=agentra-prod --to-latest` (or just redeploy, per above). |
| `agentra-github-token` | `GITHUB_TOKEN` — `git-askpass.sh`'s password for git pull/push (TASK-014) | GitHub PATs expire per their configured lifetime. Generate a new one (repo scope) at github.com/settings/tokens, then `gcloud secrets versions add agentra-github-token --project=agentra-prod --data-file=-`. |
| `agentra-alarm-webhook-password` | HTTP Basic Auth password for `/trigger/alarm` (see below) | Terraform-generated (`random_password`); rotate by tainting and re-applying: `terraform apply -replace=random_password.alarm_webhook_password`, then update the value in whatever Monitoring notification channel uses it. |

Both `.claude_oauth_token` and `.github_pat` are gitignored, untracked loose
files at the repo root — kept locally only as the source used to populate
the Terraform variables above at apply time (`-var="...=$(cat ...)"`), never
committed.

## The three trigger paths

`agentra/server.py` exposes three POST endpoints, one per vision.md trigger
type. Each logs its source and outcome to `~/.agentra/server.log` on the
instance.

### Scheduled — `POST /trigger/scheduled`

Cloud Scheduler → Cloud Run, authenticated via an OIDC token from the
`agentra-scheduler-invoker` service account (`roles/run.invoker` on the
service, nothing broader). The job (`agentra-daily-cycle`) exists but is
**paused** — no app is registered yet, so a live cron would just produce
"app not registered" no-ops in the logs every day. Once a real app exists:

```bash
gcloud scheduler jobs update http agentra-daily-cycle --region=us-central1 \
  --project=agentra-prod \
  --message-body='{"app":"<real-app-name>"}'
gcloud scheduler jobs resume agentra-daily-cycle --region=us-central1 --project=agentra-prod
```

### Error/alarm — `POST /trigger/alarm`

Meant to sit behind a GCP Monitoring alerting policy's **Webhook**
notification channel — dispatches to `orchestrator.run_prod_debug_cycle`.
Not fully wired to a live alerting policy yet: there's no deployed *app*
with its own metrics to alert on. Two things need deciding once one exists:

1. GCP Monitoring's webhook delivery has no OIDC support (unlike Scheduler
   and Pub/Sub push), so it can't authenticate via Cloud Run's IAM invoker
   check the way the other two trigger paths do. `server.py` has its own
   HTTP Basic Auth check for this one path
   (`_verify_alarm_webhook_auth`, gated on `ALARM_WEBHOOK_PASSWORD` —
   see the secrets table above) as a result.
2. For Monitoring's webhook to reach this endpoint at all, the Cloud Run
   service needs an `allUsers` **invoker** grant (Cloud Run's IAM invoker
   check happens before any request body is inspected — it can't be scoped
   per-path). That grant is deliberately *not* made in `cloudrun.tf` today,
   since there's no real alerting policy that needs it yet and the other two
   trigger paths currently rely on the service staying private. Add it
   explicitly when wiring the first real alerting policy:
   `gcloud run services add-iam-policy-binding agentra-orchestrator --region=us-central1 --project=agentra-prod --member=allUsers --role=roles/run.invoker`
   — the Basic Auth check remains the real access control for that path
   once this grant is made.

Can be exercised directly today (bypasses steps 1-2 above, since a direct
authenticated call doesn't need the Cloud Run service to be public):

```bash
curl -X POST "$(terraform output -raw orchestrator_url)/trigger/alarm" \
  -H "Content-Type: application/json" \
  -u "monitoring:$(gcloud secrets versions access latest --secret=agentra-alarm-webhook-password --project=agentra-prod)" \
  -d '{"app":"<real-app-name>","symptom":"500 errors spiking"}'
```

### Queue — `POST /trigger/queue`

A Pub/Sub push subscription (`agentra-work-queue-push`, topic
`agentra-work-queue`) → Cloud Run, authenticated the same OIDC-via-IAM-invoker
way as the scheduled path (`agentra-pubsub-invoker` service account). Publish
a message shaped like `registry.submit_request()`'s params to enqueue work:

```bash
gcloud pubsub topics publish agentra-work-queue --project=agentra-prod \
  --message='{"app":"<real-app-name>","type":"bug","description":"...","severity":"high"}'
```

This is live and fully working today — verified end-to-end locally (see the
commit history) — it just has nothing to route to until an app is
registered.
