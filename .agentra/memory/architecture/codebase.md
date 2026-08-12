I now have a solid picture of this repository. Here's the summary.

## Codebase Understanding: `agentra`

This is **not** a typical product web app — it's an autonomous AI product-engineering agent platform (per `vision.md`) whose job is to point *at* other repos and improve them itself. The repo contains:

1. **Python orchestration core** (`agentra/`) — a multi-agent system built on `claude-agent-sdk`, with specialized agents (codebase understanding, product discovery, implementation, testing, deployment, prod-debug, feedback) coordinated by an orchestrator (`orchestrator.py`) or an autonomous "brain" (`agents/brain.py`) that decides which agent to invoke next.
2. **FastAPI service** (`server.py`) — always-on Cloud Run service exposing trigger endpoints (schedule, alert, Pub/Sub queue) that kick off background agent cycles, plus REST endpoints backing a dashboard.
3. **React/TypeScript dashboard** (`agentra/web/`) — Vite + Tailwind v4 SPA for monitoring apps, runs, agents, standups, logs.
4. **GCP deployment infra** (`deploy/gcp/terraform`) — Cloud Run, Firestore, Pub/Sub, Cloud Scheduler, Artifact Registry, Secret Manager, plus a Cloudflare Tunnel/Access setup for exposing the dashboard.
5. **Data/state layer**: Firestore when `AGENTRA_FIRESTORE_PROJECT` is set (production), falling back to local JSON files under `~/.agentra` for local dev/no-GCP-creds workflows. A separate `Memory` module keeps per-target-repo, git-committed, human-readable audit logs (`.agentra/memory/*`) distinct from agentra's own operational bookkeeping (registry/inbox).
6. **Safety layer** (`agents/safety.py`) — regex-based guardrail on agent shell/file actions, reinforced by Docker sandboxing (non-root, read-only rootfs, dropped capabilities) documented in `CONTAINER.md`.
7. **GitHub App connector** (`connectors/github_app.py`) for cloning/PR-ing into target repos.

Tests live under `tests/` (pytest, covering agent features, brain stagnation, deployment, git ops, registry sync, safety hooks, server triggers, Claude stream logging). The dashboard (`agentra/web/`) has its own Vitest + React Testing Library suite (`npm test`, co-located `*.test.tsx` files) covering the run detail drawer (including malformed/partial run and app-detail data), the agent roster panel, and the standups view. CI config exists (`ci/github-actions-ci.yml`) but is intentionally *not* yet activated as `.github/workflows/ci.yml` because the GitHub App lacks the Workflows permission scope (explained in `ci/README.md`).

```json
{
  "framework": "Python (FastAPI, asyncio) core orchestrator using claude-agent-sdk; React 18 + TypeScript + Vite + Tailwind v4 dashboard",
  "backend": "Google Cloud Firestore (production, gated by AGENTRA_FIRESTORE_PROJECT) with local-JSON-file fallback under ~/.agentra for dev; per-target-repo git-committed markdown/JSON memory store (.agentra/memory)",
  "architecture": "Single-process FastAPI service (Cloud Run) that dispatches short-lived async agent-cycle tasks; multi-agent orchestration pattern (orchestrator/brain dispatches specialized agents: codebase, discovery, implementation, testing, deployment, prod_debug, feedback, git_ops, safety); HTTP trigger endpoints for schedule/alert/queue inputs (Cloud Scheduler, GCP Monitoring, Pub/Sub); Dockerized with hardened sandboxing for operating on external target repos; Terraform-defined GCP infra plus optional Cloudflare Tunnel/Access edge",
  "features": [
    "Multi-app registry with durable inbox for cross-app requests",
    "Autonomous improvement cycle: understand codebase -> discover/rank feature -> implement -> test -> deploy to pre-prod -> measure",
    "Fixed-pipeline CLI run mode (`agentra run --fixed-pipeline`) and autonomous brain-driven mode",
    "Production debugging cycle with opt-in auto-remediation and promotion to prod",
    "React dashboard: apps panel, agent roster/chat, run detail drawer, activity/log streaming, standups view, loops view",
    "Daily/periodic standup report generation (standup.py)",
    "GitHub App integration for cloning and PR creation into target repos",
    "Safety regex hook + Docker-level isolation guarding agent shell/file actions",
    "Persistent per-repo memory system (architecture/decisions/features/metrics/failures logs)"
  ],
  "test_commands": ["pytest tests", "find agentra -name '*.py' -print0 | xargs -0 -n1 python -m py_compile", "cd agentra/web && npm ci && npm test"],
  "build_commands": ["pip install -e .[dev]", "cd agentra/web && npm ci && npm run build", "docker build -t agentra:local .", "docker compose build"],
  "notes": "This repo is the agent platform itself, not a consumer-facing product; it is meant to be pointed at a separate 'target repo' via --repo/REPO_PATH to perform the actual product engineering. Two parallel package manifests exist: pyproject.toml (root, Python core) and agentra/web/package.json (dashboard). CI workflow file is intentionally kept outside .github/workflows/ pending a GitHub App permission fix. Deployment credentials (Vercel/Firebase CLI tokens) are used by the Deployment Agent when operating on target apps, separate from agentra's own GCP/Cloudflare infra."
}
```