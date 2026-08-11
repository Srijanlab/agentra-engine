I now have a complete picture of the repository.

## Summary

This is **agentra** — the "Autonomous Product Engineering Agent System" whose own spec lives in `vision.md`: a meta-system that operates *on other target repos*, understanding their codebases, discovering features, implementing, testing, deploying to pre-prod/beta, and looping. This repo is the agent system itself, not a typical product app.

**Architecture**: Python backend orchestrating Claude Agent SDK subagents (`agentra/agents/*.py`: codebase, discovery, implementation, testing, deployment, feedback, prod_debug, safety, git_ops, generic) coordinated either by a hardcoded pipeline (`orchestrator.py::run_cycle`) or by an LLM-driven "brain" (`agents/brain.py::run_autonomous_cycle`) that picks which of 9 tool-wrapped specialized agents to call and when. Exposed via a CLI (`cli.py`) and a FastAPI service (`server.py`) with webhook/scheduler/pubsub trigger endpoints, backing a React/Vite/Tailwind dashboard (`agentra/web/`) served as static files. Multi-app registry + durable inbox (`registry.py`) backed by Firestore (falls back to local JSON). Ships as a hardened, non-root, read-only Docker container (`Dockerfile`, `run-agent.sh`) deployable to GCP Cloud Run (`deploy/gcp/terraform`) or via Cloudflare tunnel (`deploy/cloudflare/terraform`).

```json
{
  "framework": "Python (FastAPI backend + Claude Agent SDK) with a React/TypeScript/Vite/Tailwind dashboard",
  "backend": "Google Cloud Firestore (durable registry/inbox), falling back to local JSON files under AGENTRA_HOME when no GCP project configured; per-app audit trail stored as git-committed files under .agentra/ in each target repo",
  "architecture": "Single always-on FastAPI service (deployable to Cloud Run) that spawns short-lived Claude Agent SDK subagent tasks on demand — not microservices; monolithic Python package (agentra/) with a decoupled statically-built SPA frontend and Terraform-defined cloud infra",
  "features": [
    "Autonomous 'brain' orchestrator that decides which specialized agent to invoke and in what order (agents/brain.py)",
    "Fixed-pipeline orchestrator mode: codebase understanding -> feature discovery -> implementation -> testing -> pre-prod deploy -> feedback (orchestrator.py)",
    "Codebase Understanding Agent, Product Discovery Agent, Implementation Agent, Testing Agent, Deployment Agent, Analytics Feedback Agent, Production Debugging Agent, Safety filter agent, generic custom-agent spawner",
    "Multi-app registry with durable inbox for cross-app requests, GitHub App connector, HTTP trigger endpoints (schedule/alert/queue), daily standup reports",
    "React dashboard: connect GitHub, register apps, monitor agents/activity, view standups",
    "Safety hooks (agents/safety.py, tests/test_safety_hook.py) as a second line of defense over unrestricted Bash/Write/Edit access, with production deploys blocked unless explicitly promoted by a human or an opted-in auto-remediate path"
  ],
  "test_commands": [
    "pytest (root-level tests/, plus package installed via `pip install -e .`)",
    "python -m py_compile over agentra/*.py (used in CI as a compile sanity check, see ci/github-actions-ci.yml)"
  ],
  "build_commands": [
    "pip install -e . (Python package, pyproject.toml/setuptools)",
    "npm ci && npm run build (agentra/web — tsc -b && vite build)",
    "docker build -t agentra:local . (or docker compose build)"
  ],
  "notes": "Not a conventional product codebase — this repo IS the autonomous engineering agent tool described in vision.md, meant to be pointed at *other* target repositories via --repo. CI (ci/github-actions-ci.yml) only runs py_compile for Python and `npm run build` for the web app; no pytest step is wired into CI despite tests/ existing (4 pytest files covering brain stagnation, generic spawn integration, safety hook regex, and safety integration). tasks.md and CONTAINER.md/docs/deployment.md contain further operational detail; deploy/ holds Terraform for GCP Cloud Run+Firestore+Pub/Sub+Scheduler and an alternative Cloudflare Tunnel path."
}
```