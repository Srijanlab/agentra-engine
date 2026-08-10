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

## Current state: deployed idle, dashboard live

This deployment intentionally has **zero apps registered** as shipped, but
registering one is now a normal, supported action — visit the orchestrator
URL in a browser (the dashboard, TASK-015, is served from `GET /`) and use
the "Register a repo" form, or `POST /apps` directly, to add one. Real admin
auth for that UI is still future work (not part of this repo yet); today
anyone who can reach the URL can register/run apps, same trust boundary as
every other trigger endpoint.

The multi-app registry (`~/.agentra/apps.json`, `agentra/registry.py`) lives
on a GCS FUSE volume mount (TASK-018, `deploy/gcp/terraform/storage.tf`/
`cloudrun.tf`) at `/data` inside the container, not the ephemeral local
disk — `AGENTRA_HOME=/data/home` survives an instance restart/redeploy.
Repo checkouts deliberately do **not** live on that mount:  gcsfuse doesn't
support `chmod`, which `git clone` needs, confirmed live (cloning failed
with `chmod on .git/config.lock: Operation not permitted` the one time
this was tried). Checkouts live on the container's own local disk instead
(`AGENTRA_REPOS_ROOT=/home/agentuser/repos`, agentuser's home — nothing at
the container root is writable by the non-root user this runs as) and are
**not** expected to survive a restart. `registry.get_app_repo()`
transparently re-clones from the app's stored `repo_url` if its checkout
is missing, so this is self-healing rather than durable: the actual
durable copy of a project's history is whatever it has pushed to its own
git remote. Locally (no GCS mount) both env vars default to paths under
`~/.agentra`.

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

## Dashboard and app registration (TASK-015/016)

`GET /` serves a self-contained dashboard: system status (with pause/resume,
see below), a form to register any GitHub repo by URL, the registered-apps
list with a "Run now" button, the standup panel (see below), recent runs,
and recent signals. Backed by JSON APIs that are just as usable directly:

- `POST /apps` — `{"name", "repo_url", "branch": "main", "objective": null}`.
  Clones server-side (same `GIT_ASKPASS`/`GITHUB_TOKEN` credential as
  TASK-014's pull/push) under `AGENTRA_REPOS_ROOT`, registers it.
- `GET /apps` — registry, plus each app's objective/shipped/known-bug counts.
- `POST /apps/{name}/run` — on-demand equivalent of `/trigger/scheduled`.
- `GET /runs`, `GET /signals` — feed the dashboard's activity tables.

No auth on any of this yet beyond Cloud Run's IAM invoker check on the
service as a whole — anyone who can reach the URL can register/run apps.
Fine for the current single-operator deployment; needs real auth before
this is opened up to a team.

## Kill switch (TASK-017)

`POST /system/pause` (optional `{"reason": "..."}`) / `POST /system/resume`
/ `GET /system/status`. The pause state is a durable marker file
(`registry.PAUSE_PATH`, under `AGENTRA_HOME` — on the same GCS mount as the
registry, TASK-018) checked at the top of every trigger path: scheduled,
alarm, queue, and the on-demand `/apps/{name}/run`. While paused, each
returns a clean no-op (`{"triggered": false, "reason": "system is paused"}`)
instead of starting new agent work. Survives a restart.

## Daily standup (TASK-019)

For each registered app, `POST /standup/daily` (behind the paused
`agentra-daily-standup` Scheduler job, `0 8 * * *` UTC — resume it the same
way as `agentra-daily-cycle` once an app exists, no per-app body needed
since it iterates the whole registry) generates a short "Yesterday" /
"Today" report and persists it to that app's own
`.agentra/standups/<date>.md`. Grounded only in that project's real data:
"yesterday" comes from timestamped `.agentra/logs/*.log` lines in the last
24h, "today" from the actual open `known_bugs`/`feature_queue`/objective —
the model is explicitly instructed not to invent activity or plans beyond
what it's given, and an empty project gets a plain "no activity / no
backlog" report with no LLM call at all. `POST /apps/{name}/standup` runs
one app's standup on demand; `GET /apps/{name}/standup/latest` (and the
dashboard's standup panel) reads the most recent one back.
