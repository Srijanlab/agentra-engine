# Split from srijanlab-agentra

This repo was carved out of `Srijanlab/srijanlab-agentra` on 2026-09-02, the second
step of the three-service split:

| Repo | Role | Host |
|---|---|---|
| `agentra-ui` | dashboard (React/Vite) | Firebase Hosting |
| `agentra-engine` | the API — DynamoDB + GitHub + Slack, sole credential holder | Vercel (serverless) |
| `agentra-loop` | the autonomous orchestrator — runs cycles, `docker build`, git | AWS ECS-on-EC2 (us-west-2) |

`agentra-engine` and `agentra-loop` share most of the `agentra.*` package (it is
vendored into both, not published), and diverge where their jobs do:

- **agentra-engine** keeps `server/` (pure-API routes), `registry/`, `memory/`,
  `connectors/`, `proxy/`, and the `/internal/*` RPC handlers. State lives in
  DynamoDB (tables provisioned by the loop's `AgentraData` CDK stack); with no
  DynamoDB env configured it falls back to local JSON.
- **agentra-loop** runs `agents/`, the cycle pipeline, and `docker`/`git`, and
  replaces direct `registry`/`Memory` access with an HTTP client to the engine
  (`AGENTRA_ENGINE_URL` -> `POST /internal/rpc`).

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
