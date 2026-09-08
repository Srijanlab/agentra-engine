# Split from srijanlab-agentra

This repo was carved out of `Srijanlab/srijanlab-agentra` on 2026-09-02, the second
step of the three-service split:

| Repo | Role | Host |
|---|---|---|
| `agentra-ui` | dashboard (React/Vite) | Firebase Hosting |
| `agentra-engine` | the API — DynamoDB + GitHub + Slack, sole credential holder | Vercel (serverless) |
| `agentra-loop` | the autonomous orchestrator — runs cycles, `docker build`, git | AWS ECS-on-EC2 (us-west-2) |

`agentra-engine` and `agentra-loop` share the `agentra.*` state/connector core
(vendored into both, not published) but the split is real:

- **agentra-engine** = the API + state authority. `server/` (the full dashboard
  API), `registry/` + `memory/` (DynamoDB, local-JSON fallback), `connectors/`,
  `proxy/`, the `/internal/*` RPC handlers, and the `a2a/` metadata endpoints.
  It runs **no cycle / promote / prod-debug** -- a trigger records a run and
  `registry.enqueue_job(...)`; the loop claims it. The engine runs **no Claude**
  (no `claude-agent-sdk`); `agents/` here is just `catalog.py` (static metadata).
  Chat + standup *generation* are held (503) pending a loop endpoint; Slack runs
  on the loop via Socket Mode. The loop's job-drain loop calls `GET /trigger/cron`
  every ~5 min to enqueue due cycles + reconcile.
- **agentra-loop** = execution. The `agents/` pipeline + `agents/brain/` (the one
  execution path — no `orchestrator.py`), `agents/job_runner.py`, `docker`/`git`,
  Slack Socket Mode. It mounts no dashboard API. It reaches engine state over RPC
  (`AGENTRA_ENGINE_URL` -> `POST /internal/rpc`) and drains the job queue
  (`registry.claim_next_job()`) on its 30 s poll.

`srijanlab-agentra` is the frozen incumbent (its GCP VM is decommissioned).

## Two separate GitHub concerns — don't conflate them

- **Deploy access** — each repo's own CI (Vercel Git integration for the engine,
  GitHub Actions + `ecs update-service` for the loop, Firebase for the UI).
  Nothing to do with the App.
- **Issue / contents access** — the `agentra-orchestrator` GitHub App, used across
  *every* app agentra manages to read backlogs, open PRs, comment on issues.
  `connectors/github_app.py` mints an installation token **per `owner/repo`**.

For the App: the org install (`id 153365557`) is `repository_selection: selected`,
so `agentra-ui` / `agentra-engine` / `agentra-loop` each have to be added to it
(the user is handling this). The App has no `workflows` permission — the CI/deploy
YAML in these repos is human-maintained.

The full plan: "Three Repos, One Engine".


The `.agentra/` spec standard (what each repo stores, who owns it, when it changes): [docs/agentra-spec.md](docs/agentra-spec.md).
