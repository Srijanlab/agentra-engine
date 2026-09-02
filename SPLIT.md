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

The full plan: "Three Repos, One Engine".
