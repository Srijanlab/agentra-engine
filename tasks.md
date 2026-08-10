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

### TASK-015: Observability dashboard (signals, agent activity, system state)
**Scope:** both
**Priority:** high

`agentra/server.py` already logs every trigger (schedule/alarm/queue) to
`~/.agentra/server.log` and tracks in-flight/recent runs in `_active_runs`,
but none of it is visible except by reading raw logs. Add a dashboard,
served from the same Cloud Run service, that visualizes what the system is
doing: which signals have come in, which agent/cycle is running for which
app, and overall system status.

**Acceptance criteria:**
- [ ] A dashboard page (served from the existing FastAPI app, no separate deploy) shows registered apps, recent trigger signals with source/timestamp, and active + recent runs with status
- [ ] Backed by JSON APIs (not data baked into the HTML) so the same data is scriptable
- [ ] Refreshes live (polling is fine) without a manual reload
- [ ] Reuses existing tracking (`_active_runs`, `server.log`, each app's `Memory`) rather than a second parallel logging system

---

### TASK-016: Register any GitHub repo from the dashboard and start an autonomous cycle
**Scope:** both
**Priority:** high

Today `agentra apps add` requires a repo already checked out locally --
there's no way to point agentra at a GitHub repo it hasn't seen before from
the deployed service (a gap already flagged in `docs/deployment.md`). Add
that: given a repo URL, clone it (reusing the existing `GITHUB_TOKEN` /
`GIT_ASKPASS` credential), register it, and let a cycle be started for it
on demand from the dashboard, not just cron/queue.

**Acceptance criteria:**
- [ ] Dashboard form to add a GitHub repo by URL + app name + objective
- [ ] Backend clones the repo server-side using existing git credentials and registers it via `registry.py`
- [ ] Dashboard/API can start an autonomous cycle for a registered app on demand
- [ ] Verified live: register a real repo through the running service and confirm a cycle actually starts and logs against it

---

### TASK-017: Shut down the autonomous system from the UI
**Scope:** both
**Priority:** high

There's no kill switch today -- once a trigger fires, it runs. Add a global
pause that every trigger path (scheduled/alarm/queue/on-demand) respects,
controllable from the dashboard.

**Acceptance criteria:**
- [ ] Dashboard has a visible pause/resume control for the whole system
- [ ] While paused, all trigger paths no-op and log why, instead of starting new agent work
- [ ] Pause state is durable (survives an instance restart), not just in-memory
- [ ] Verified live: pause via the dashboard, confirm a trigger during pause correctly no-ops, resume, confirm it dispatches again

---

### TASK-018: Dedicated persistent context ("team") per registered project
**Scope:** backend
**Priority:** high

Each app's `.agentra/` memory already exists per-repo, but the repo
checkout and the multi-app registry itself (`~/.agentra/apps.json`) live on
Cloud Run's ephemeral local disk -- gone on every restart (documented in
`docs/deployment.md`). TASK-016 and TASK-017 both need this to actually
persist. Move both onto durable storage so each project's context survives
restarts and stays isolated from every other project's.

**Acceptance criteria:**
- [ ] Registered apps and their repo checkouts survive a Cloud Run instance restart/redeploy
- [ ] Each project's `.agentra/` memory persists and is isolated from other projects'
- [ ] Documented in `docs/deployment.md`
- [ ] Verified live: register an app, force a new revision, confirm the app + checkout + memory are still there

---

### TASK-019: Daily standup between the orchestrator and its agents
**Scope:** backend
**Priority:** medium

For each registered project, produce a daily standup: what happened
yesterday (grounded in that project's actual `Memory` -- `shipped.json`,
`known_bugs.json`, run logs) and what's planned for today (grounded in its
actual backlog/objective). Not a chat transcript between fictional
personas -- a real summary an LLM call generates from that project's real
data, explicitly instructed not to invent activity that didn't happen.

**Acceptance criteria:**
- [ ] A standup routine runs per registered app, producing a "yesterday" summary and a "today" plan grounded only in that project's real Memory data
- [ ] Runs automatically on a daily schedule and is viewable from the dashboard
- [ ] Standup output is persisted (visible after the fact, not just streamed once)
- [ ] Verified live against at least one real registered project's actual history

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
