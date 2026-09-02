# Split from srijanlab-agentra

This repo was carved out of `Srijanlab/srijanlab-agentra` on 2026-09-02, the second
step of the three-service split:

| Repo | Role | Host |
|---|---|---|
| `agentra-ui` | dashboard (React/Vite) | Firebase Hosting |
| `agentra-engine` | the API — Firestore + GitHub + Slack, sole credential holder | Cloud Run |
| `agentra-loop` | the autonomous orchestrator — runs cycles, `docker build`, git | TBD |

**At this stage `agentra-engine` and `agentra-loop` are identical** — both are
`srijanlab-agentra` minus `agentra/web/`. They diverge in later stages:

- **agentra-engine** keeps `server/` (pure-API routes), `registry/`, `memory/`,
  `connectors/`, and drops `agents/brain/` + the specialised agents + the LLM/repo
  routes (`chat`, `standup`, `human_input`, `triggers`' cycle-spawn).
- **agentra-loop** keeps `agents/`, `proxy/` (NIM), the LLM/repo routes, and replaces
  direct `registry`/`Memory`/Firestore access with an HTTP client to the engine.

`srijanlab-agentra` stays as the running incumbent (the GCP VM keeps serving from it)
until the loop has a new home and the VM is decommissioned.

## Two separate GitHub concerns — don't conflate them

- **Deploy access** — WIF → GCP → Cloud Run. Only for shipping this platform's own
  images. Per-repo, set up in `deploy/cloudrun/`. Nothing to do with the App.
- **Issue / contents access** — the `agentra-orchestrator` GitHub App, used across
  *every* app agentra manages to read backlogs, open PRs, comment on issues.
  `connectors/github_app.py` mints an installation token **per `owner/repo`**.

For the App: the org install (`id 153365557`) is `repository_selection: selected`,
so `agentra-ui` / `agentra-engine` / `agentra-loop` each have to be added to it
(the user is handling this). The App has no `workflows` permission — the CI/deploy
YAML in these repos is human-maintained.

The full plan: "Three Repos, One Engine".
