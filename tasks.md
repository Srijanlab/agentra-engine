# Agentra Task Queue

Tasks in the `## Queue` section are picked up automatically when the
**"Claude — Run Tasks (from file)"** GitHub Action is triggered.
Move completed tasks to `## Done` with the commit SHA.

---

## How to add tasks

1. Add a new task block under `## Queue` following the template below
2. Go to **Actions → Claude — Run Tasks (from file) → Run workflow**
3. Claude will work through all queued tasks, commit, and push to main
4. Completed tasks will appear in `## Done`

### Task template

```
### TASK-XXX: Short title
**Scope:** frontend | backend | both
**Priority:** high | medium | low

Description of what needs to be built or fixed.

**Acceptance criteria:**
- [ ] Criterion 1
- [ ] Criterion 2
```

---

## Queue

---

## Done

<!-- Completed tasks go here with commit SHA -->

### TASK-009: Generic Claude Code sub-agent spawning
**Commit:** ac873bc

Added `agents/generic.py` (`TaskSpec` + `spawn()`, reusing `base.py::run_agent()`
directly, logged through the same `Memory.log()` ledger every other agent
uses) and wired a ninth `spawn_custom_agent` tool into `agents/brain.py` so
the orchestrator can reach for it on task types that don't fit the other
eight. No `allow_prod` field on `TaskSpec` — production stays reachable
only through the existing explicit paths. Existing 8 specialized agents
untouched. Verified via `py_compile`/import checks; a live-integration
test (`tests/test_generic_spawn_integration.py`, same
not-run-automatically convention as `test_safety_integration.py`) is
added but not yet executed — no `CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY`
available in this session without reading the credential files flagged
for TASK-013.

### TASK-010: Orchestrator selects which agent(s) to spawn based on requirement
**Commit:** 0da9178

`agents/brain.py::run_autonomous_cycle()` already satisfied every
acceptance criterion here (arbitrary objective in, tool-based agent
selection, decisions logged via `session.note()`, production excluded) —
it just sat behind an opt-in `--autonomous` flag. Flipped the default:
`agentra run`/`agentra loop` now use it by default; the old hardcoded
sequence (`orchestrator.py::run_cycle`) is reached via the new
`--fixed-pipeline` flag instead.

### TASK-011: Event integrations to invoke the orchestrator (schedule, error/alarm, queue)
**Commit:** 23b3130, b7eb717, c4ab151

Added `agentra/server.py` (FastAPI, run via `agentra serve`) with
`POST /trigger/scheduled`, `/trigger/alarm`, `/trigger/queue`, each logging
its source and dispatched task to `~/.agentra/server.log`. Scheduled and
queue paths authenticate via Cloud Run's IAM invoker check (OIDC tokens from
dedicated `agentra-scheduler-invoker`/`agentra-pubsub-invoker` service
accounts, `deploy/gcp/terraform/scheduler.tf`/`pubsub.tf`); the alarm path
gets its own HTTP Basic Auth check (`_verify_alarm_webhook_auth`) since GCP
Monitoring's webhook channel has no OIDC support. Verified live against the
deployed service: all three endpoints respond correctly via
`gcloud run services proxy`, and a real `gcloud pubsub topics publish` to
`agentra-work-queue` was confirmed via Cloud Logging to reach
`/trigger/queue` and return 200 (not just direct curl tests). The alarm path
isn't wired to a real Monitoring alerting policy yet — no deployed app has
metrics to alert on — documented in `docs/deployment.md`.

### TASK-012: Deploy the multi-agent system safely on GCP (always-on orchestrator, on-demand agents)
**Commit:** c4ab151

Deployed to a new dedicated GCP project, `agentra-prod`. Orchestrator runs
as a Cloud Run Service (`agentra-orchestrator`, `min_instance_count=1`) via
the existing `Dockerfile`'s `agentra serve` entrypoint. Specialized agents
remain on-demand subprocesses spawned by the Claude Agent SDK inside that
service, not standing services themselves. Secrets sourced from Secret
Manager (see TASK-013). Full Terraform under `deploy/gcp/terraform/`,
runbook in `docs/deployment.md`. Deployed **idle by design** — zero apps
registered, per the user's decision to defer app registration to a future
admin UI rather than hardcode one now; `docs/deployment.md` documents this
plus the known follow-up gaps it implies (registry on ephemeral disk, no
clone-on-register mechanism yet for a registered app's repo checkout).
Live-verified: `/health` returns 200 with the expected body via
`gcloud run services proxy`.

### TASK-013: Carry over the existing Claude SDK + OAuth auth into the deployment
**Commit:** c4ab151

`CLAUDE_CODE_OAUTH_TOKEN`/`GITHUB_TOKEN`/`ALARM_WEBHOOK_PASSWORD` all sourced
from Secret Manager (`deploy/gcp/terraform/secrets.tf`), wired into the
Cloud Run service env in `cloudrun.tf`. Rotation documented per-secret in
`docs/deployment.md`. Loose `.claude_oauth_token`/`.github_pat` files
removed from the repo root (were already gitignored, never committed).
Auth verified two ways: (1) indirectly, the Cloud Run service starts
successfully with the secret populated — a failed Secret Manager read would
fail container start; (2) directly, a real `spawn()` call run locally with
the exact migrated token value executed a live Bash tool call and returned
correctly, confirming the credential itself is valid, not just present.
Not verified: an actual agent turn running *inside* the deployed container,
since (per TASK-012) no app is registered there yet and there's no
clone-on-register mechanism to get one in — an honest, documented gap, not
faked.

### TASK-014: Git pull/push support for on-demand agents
**Commit:** 7dfd77d

Added `agentra/agents/git_ops.py` (`pull_latest`/`push_branch`, raising
`GitOpError` with git's real stderr on failure — never silently swallowed)
and wired `pull_before`/`push_after` fields into `agents/generic.py`'s
`TaskSpec`/`spawn()`, folding a push failure into `AgentResult.ok=False`
rather than discarding it. Verified against real local bare-remote git
repos: pull on an existing branch, a successful push, a deliberately
conflicting/rejected push correctly raising `GitOpError`, and `pull_latest`
correctly recovering by resetting to a diverged remote tip — all passed.
Not verified against the deployed GCP environment itself, for the same
reason as TASK-013's last point (no app registered there to run a real
task against yet).

### TASK-018: Dedicated persistent context ("team") per registered project
**Commit:** 8f626c0, 2b955b1

The multi-app registry previously lived on Cloud Run's ephemeral local
disk. Mounted a GCS bucket at `/data` via a Cloud Run v2 GCS FUSE volume
(`deploy/gcp/terraform/storage.tf`, requires the gen2 execution
environment) and pointed `AGENTRA_HOME` at it — `apps.json` and the pause
marker now survive a restart. Repo checkouts deliberately do **not** live
on that mount: live testing against the real deployed service caught
gcsfuse rejecting `git clone`'s `chmod` calls outright. Checkouts stay on
the container's local disk (`AGENTRA_REPOS_ROOT=/home/agentuser/repos`,
not durable) and self-heal instead — `registry.register_app()` now stores
`repo_url`/`branch`, and `registry.get_app_repo()` (the one function every
existing call site already goes through) transparently re-clones from
`repo_url` if the checkout is missing, so a project's own git remote is
the real durable copy of its history, same as it always was. Verified
against the live deployed service, not just locally: registered a real
repo (the original design 500'd here), confirmed the registry entry
landed as a real GCS object, forced a new revision (fresh container,
empty local disk), and confirmed both the registration and the
auto-reclone survived and worked correctly.

### TASK-016: Register any GitHub repo from the dashboard and start an autonomous cycle
**Commit:** 10b565f

Added `git_ops.clone_repo` (same `GIT_ASKPASS`/`GITHUB_TOKEN` credential
`docker-entrypoint.sh`'s own clone-on-start uses) and three endpoints on
`agentra/server.py`: `GET /apps` (registry + objective/shipped/bug counts),
`POST /apps` (clone a repo by URL under `registry.REPOS_ROOT` and register
it), `POST /apps/{name}/run` (same dispatch path as `/trigger/scheduled`,
without waiting for a cron tick). Verified live: registered a real repo
cloned from an actual git remote end-to-end and confirmed `/apps/{name}/run`
actually dispatched a live autonomous cycle against it.

### TASK-017: Shut down the autonomous system from the UI
**Commit:** 387d813

`registry.py` gets a durable pause marker (`paused.json` under
`AGENTRA_HOME`, so it persists on TASK-018's volume same as the app
registry) plus `pause()`/`resume()`/`is_paused()`. `server.py` adds
`GET`/`POST /system/status,pause,resume` and checks `is_paused()` at the
top of every trigger path — scheduled, alarm, queue, and the on-demand
`/apps/{name}/run` (inherits the guard by delegating to the scheduled
path) — each returning a clean no-op instead of starting new agent work.
Verified live: paused, confirmed all four paths correctly no-op with
"system is paused", resumed, confirmed dispatch works again.

### TASK-015: Observability dashboard (signals, agent activity, system state)
**Commit:** f172751

`GET /` serves a self-contained HTML/CSS/JS page (`agentra/dashboard.py`,
no build step, no CDN dependency) backed entirely by JSON APIs — added
`GET /runs` (list, newest first) and `GET /signals` (parsed tail of
`server.log`) alongside the existing `GET /apps` and `GET /system/status`.
Polls every 5s; shows system status with pause/resume, a register-a-repo
form, registered apps with a "run now" button, recent runs, and recent
signals. Verified live: dashboard JS extracted and checked with `node
--check` (syntax-valid), and the page + its supporting APIs confirmed
working against a real running `agentra serve` process.

### TASK-019: Daily standup between the orchestrator and its agents
**Commit:** b3f7231

`agentra/standup.py` generates one project's standup as a single no-tools
LLM call over data extracted deterministically from that project's own
`Memory`: "yesterday" from timestamped `.agentra/logs/*.log` lines in the
last 24h (`Memory.recent_log_lines` — the only append-only, per-event
record in `.agentra/`; `shipped`/`known_bugs`/`feature_queue` are undated
snapshots), "today" from the actual open backlog/objective. Explicitly
instructed not to invent activity or plans beyond what it's given; an
empty project short-circuits to a plain "no activity / no backlog" report
with no LLM call at all. `server.py` adds `POST /standup/daily` (all
apps, behind the new paused `agentra-daily-standup` Scheduler job),
`POST /apps/{name}/standup` (one app, on demand), `GET
/apps/{name}/standup/latest` — all gated by the TASK-017 kill switch.
Persists to each app's own `.agentra/standups/<date>.md`; dashboard shows
a standup panel with a "Generate now" button per app. Verified live:
seeded a test app's Memory with a real known bug, feature request, and log
entry from an actual prior autonomous cycle, ran the standup for real, and
confirmed the generated report accurately reflected exactly that data with
nothing fabricated — then verified the same round-trip through the real
HTTP endpoints.
