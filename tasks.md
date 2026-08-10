# AgentOS Task Queue

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

### TASK-011: Event integrations to invoke the orchestrator (schedule, error/alarm, queue)
**Scope:** backend
**Priority:** high

No cron/queue/pub-sub integration exists today. `agentos/registry.py::dispatch_once()`
drains a local `~/.agentos/inbox/` dir but nothing schedules it, and the
GitHub Action referenced at the top of this file has no `.github/workflows`
committed in this repo. Add three trigger paths into the orchestrator:
(a) scheduled, for certain recurring work types, (b) reactive, on
errors/alarms, (c) reactive, when new work lands in a queue.

**Acceptance criteria:**
- [ ] Scheduled invocation: orchestrator triggerable on a cron/interval for a given work type (e.g. Cloud Scheduler → orchestrator endpoint)
- [ ] Error/alarm invocation: orchestrator triggerable from an error/alarm signal (e.g. GCP Error Reporting / Cloud Monitoring alert), with enough context to run `prod_debug`/`safety`-style agents
- [ ] Queue invocation: orchestrator triggerable when new work is enqueued (e.g. Pub/Sub, or by extending `registry.py::dispatch_once()`)
- [ ] Each trigger path logs its source (schedule/alarm/queue) and the task it produced

---

### TASK-012: Deploy the multi-agent system safely on GCP (always-on orchestrator, on-demand agents)
**Scope:** backend
**Priority:** high

No GCP deployment config exists in this repo (no Cloud Run/GKE/terraform/gcloud
scripts tracked), despite the `Dockerfile`/`CONTAINER.md` hardening (non-root
`agentuser`, read-only rootfs, cap-drop ALL, tmpfs `/tmp`) being
deployment-ready. Deploy so the orchestrator runs continuously as an
always-on service, while specialized agents (TASK-009) are invoked on demand
rather than running as standing services themselves.

**Acceptance criteria:**
- [ ] Orchestrator deployed as an always-on GCP service (e.g. Cloud Run with min-instances ≥ 1, or a GKE Deployment) using the existing `Dockerfile`
- [ ] Specialized agents run on demand (e.g. Cloud Run Jobs, or short-lived tasks spawned by the orchestrator), not as standing services
- [ ] Deployment applies the same hardening already documented in `CONTAINER.md`
- [ ] Secrets (OAuth token / API key, GitHub token) are sourced from GCP Secret Manager, not baked into the image or committed to the repo
- [ ] Deployment steps/config documented (or IaC added) and reviewed before first production rollout

---

### TASK-013: Carry over the existing Claude SDK + OAuth auth into the deployment
**Scope:** backend
**Priority:** medium

Sub-agents already authenticate via `CLAUDE_CODE_OAUTH_TOKEN` /
`ANTHROPIC_API_KEY` env vars read by the Claude CLI (see `run-agent.sh`,
`CONTAINER.md`, `Dockerfile`). A `.claude_oauth_token` file currently sits
untracked at the repo root but isn't read by any code. Carry the same SDK +
OAuth token approach into the GCP deployment (TASK-012) via a proper secret
store instead of a loose file in the repo.

**Acceptance criteria:**
- [ ] `CLAUDE_CODE_OAUTH_TOKEN` sourced from GCP Secret Manager in the deployed environment, matching local `run-agent.sh` behavior
- [ ] Loose `.claude_oauth_token` file at repo root removed from the working tree and confirmed covered by `.gitignore` (never committed)
- [ ] Token rotation path documented for the deployed environment
- [ ] Deployed orchestrator/agents authenticate successfully end-to-end using the secret-sourced token

---

### TASK-014: Git pull/push support for on-demand agents
**Scope:** backend
**Priority:** medium

Git operations already exist in `agentos/agents/deployment.py`
(`_sync_branch_to_remote`, `_merge_and_push`, raw `subprocess` git calls) and
`git-askpass.sh` supplies `$GITHUB_TOKEN` for clone-on-start. Confirm/extend
this so any spawned agent — not just the deployment agent — can pull latest
and push its own changes as part of its task, which becomes necessary once
agents run on demand in GCP (TASK-012) instead of a single long-lived local
checkout.

**Acceptance criteria:**
- [ ] Orchestrator/agents can `git pull` latest `main` before starting a task, in the deployed environment
- [ ] Agents can `git push` their committed changes back to the remote using the token from `git-askpass.sh`/`GIT_ASKPASS` (or an equivalent secret-sourced credential)
- [ ] Push failures (conflicts, rejected pushes) are surfaced/logged rather than silently swallowed
- [ ] Verified working against the deployed GCP environment (TASK-012), not just local `run-agent.sh` usage

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
`agentos run`/`agentos loop` now use it by default; the old hardcoded
sequence (`orchestrator.py::run_cycle`) is reached via the new
`--fixed-pipeline` flag instead.
