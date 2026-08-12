Now I have a comprehensive understanding of this repository. This is **Agentra** itself — the autonomous product engineering agent system (not a target app it operates on).

```json
{
  "framework": "Python backend (FastAPI + Claude Agent SDK) with a React 18 + TypeScript + Vite + Tailwind v4 dashboard frontend",
  "backend": "FastAPI HTTP server (agentra/server.py) orchestrating Claude Agent SDK subagents; state persistence via local JSON/YAML files under .agentra/ (per-target-repo, git-committed audit trail) and Google Cloud Firestore for agentra's own multi-app registry/inbox (falls back to local JSON when AGENTRA_FIRESTORE_PROJECT unset); GCS-backed volume for durable registry data in deployment; deploys on GCP Cloud Run",
  "architecture": "Multi-agent orchestrator system, not a typical web app. A Python 'brain' orchestrator (agentra/agents/brain.py, agentra/orchestrator.py) spawns specialized on-demand subprocess agents (codebase understanding, product discovery, research/planning, implementation, testing, deployment, feedback, prod-debug, git-ops, generic/custom) via the Claude Agent SDK. Runs as an always-on Cloud Run service (agentra serve) that reacts to HTTP triggers (schedule, alarm/webhook, pub/sub queue, on-demand dashboard 'run now') rather than a human CLI only, dispatching short-lived agent subprocesses per request. A React dashboard SPA (agentra/web) is served as static files by the same FastAPI app for observability/control. Infra defined via Terraform for both GCP (Cloud Run, Firestore, Pub/Sub, Cloud Scheduler, Secret Manager, Artifact Registry) and Cloudflare (tunnel/access/dns). Also containerized (Docker) with a hardened non-root, read-only, cap-dropped sandbox for agent execution against arbitrary target repos.",
  "features": [
    "Codebase Understanding agent (scans a target repo's stack/architecture)",
    "Product Discovery agent (proposes features from objectives/analytics/backlog without explicit human instruction)",
    "Implementation agent (writes code, git branches, safety-filtered Bash/Edit/Write)",
    "Testing agent (local + live pre-prod verification)",
    "Deployment agent (pre-prod deploy, human-gated production promotion, audit trail)",
    "Production Debugging agent (diagnose issues, optional auto-remediation with pre-prod verification before promoting)",
    "Analytics/Feedback agent (measures feature impact against objective)",
    "Generic/custom subagent spawning for tasks not covered by the 8 specialized agents",
    "Multi-app registry: register any GitHub repo from the dashboard, clone, and kick off autonomous cycles",
    "System-wide pause/resume kill switch enforced across all trigger paths",
    "Observability dashboard (React) showing system status, registered apps, run history, live signals, agent chat modal, standups",
    "Daily standup generation per project summarizing agent activity/backlog via LLM call over deterministic Memory data",
    "Durable per-repo Memory ledger (architecture/decisions/features/metrics/failures, shipped features, known bugs, feature queue)",
    "GitHub App connector for repo access/auth"
  ],
  "test_commands": [
    "pytest tests  (Python unit/integration tests, from repo root, requires `pip install -e .[dev]`)",
    "python -m py_compile on all agentra/**/*.py (compile check used in CI)",
    "npm run build in agentra/web (TypeScript build via tsc -b, doubles as web typecheck; no separate JS unit test suite found)"
  ],
  "build_commands": [
    "pip install -e .[dev]  (Python package, setuptools build backend)",
    "cd agentra/web && npm ci && npm run build  (Vite + tsc build producing dist/ served by FastAPI)",
    "docker build -t agentra:local .  (containerized runtime, see Dockerfile/CONTAINER.md)",
    "docker compose build  (docker-compose.yml)",
    "./run-agent.sh run --objective '...' [--skip-deploy]  (invoke a single autonomous cycle against a target repo)",
    "terraform (deploy/gcp/terraform, deploy/cloudflare/terraform) for infra provisioning"
  ],
  "notes": "This repo IS the autonomous product engineering agent system described in vision.md (the very system this Codebase Understanding Agent role is part of) — it is meta: agentra points itself at OTHER target repos (via git clone under a registry) and runs improvement cycles against them, rather than being a typical product app itself. tasks.md (TASK-009 through TASK-019, all marked Done) shows an active development history: generic subagent spawning, autonomous orchestrator agent-selection, HTTP trigger endpoints (schedule/alarm/queue), GCP deployment, secrets via Secret Manager, git pull/push support, per-project persistent 'team' context via GCS, register-repo-from-dashboard flow, pause/resume kill switch, and the observability dashboard/standup feature. Safety is enforced via agentra/agents/safety.py (a regex-based Bash/Edit/Write filter, explicitly documented as a second line of defense, not a real sandbox — true isolation comes from running inside the hardened Docker container per CONTAINER.md). Production deployment is strictly human-gated (agentra promote) except one explicit opt-in auto-remediation path for production hotfixes, which still requires pre-prod verification before promotion."
}
```