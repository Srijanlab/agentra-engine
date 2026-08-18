I have a clear and thorough picture now. Here's the summary.

## Summary

**agentra** is itself the "autonomous product engineering agent system" described in its own `vision.md` — not a product being improved, but the improving system. It's built to point at a *target repo* (a separate app), understand it, decide what to build, implement it, test it, and deploy it to pre-prod, with production changes gated behind an explicit human/opt-in path.

- **Language/runtime**: Python 3.11+ backend (FastAPI + `claude-agent-sdk`) driving Claude CLI subprocesses as its actual "agents"; a TypeScript/React (Vite, Tailwind v4, vitest) dashboard SPA served as static files by the FastAPI process.
- **Architecture**: A single always-on FastAPI/Cloud Run service (`agentra/server.py`) that accepts triggers (schedule, alert webhook, Pub/Sub queue) and spawns short-lived Claude CLI subprocesses per agent step (`agents/base.py::run_agent`) — not a fleet of standing microservices. Two orchestration modes: a hardcoded pipeline (`orchestrator.py::run_cycle`) and the default LLM-driven orchestrator (`agents/brain.py::run_autonomous_cycle`) that picks from 9 tools/specialized agents itself, with Python-enforced circuit breakers (cost cap, consecutive-failure cap, stagnation detection) rather than relying on prompted self-restraint.
- **Backend/data layer**: Deliberately thin. Almost all durable state (known bugs, feature queue, shipped features, objective, environments config) lives directly in **GitHub Issues/Actions Variables** on the target repo — no local JSON mirror, no database of record. Only `.agentra/memory/architecture/*.md` steering files are local, git-committed. Optional Firestore mirrors runs/agent-steps for dashboard durability across Cloud Run redeploys.
- **Features** (of agentra-the-system, since it has no end-user product features itself): autonomous discover→implement→test→deploy cycles; production-debugging agent with opt-in auto-remediate; a React ops dashboard (live agent logs, run history, promote-to-prod button, chat with individual agents, voice); GitHub App-based short-lived repo tokens; Cloudflare/GCP Terraform deploy configs.
- **Test/build tooling**: `pytest tests/` (Python, ~29 test files) + `python -m py_compile` for the backend; `npm test` (vitest) + `npm run build` (tsc + vite) for the dashboard. A ready-but-inactive GitHub Actions workflow lives at `ci/github-actions-ci.yml` (not `.github/workflows/`) because the GitHub App lacks Workflows permission.

```json
{
  "framework": "FastAPI (Python) backend + Claude Agent SDK subprocess orchestration; React/Vite/TypeScript dashboard frontend",
  "backend": "GitHub Issues/Actions Variables as system of record (no local DB); optional Firestore for run/dashboard durability; Cloud Run deployment target",
  "architecture": "Single always-on orchestrator service that dispatches short-lived Claude CLI agent subprocesses per step, triggered by HTTP (schedule/alert/queue); LLM-driven orchestrator (agents/brain.py) is default, hardcoded pipeline (orchestrator.py::run_cycle) is a fallback mode",
  "features": [
    "autonomous discover -> implement -> test -> deploy-to-pre-prod cycle",
    "LLM 'brain' orchestrator choosing among 9 specialized-agent tools with deterministic circuit breakers",
    "production debugging agent with human-gated or opt-in auto-remediate promotion",
    "React ops dashboard: live per-agent logs, run history, chat with agents, voice, promote-to-prod control",
    "GitHub App short-lived installation tokens replacing a static PAT",
    "safety hook (regex PreToolUse gate) layered under Docker container isolation",
    "GCP/Cloudflare Terraform deploy configs"
  ],
  "test_commands": [
    "pytest tests",
    "find agentra -name '*.py' -print0 | xargs -0 -n1 python -m py_compile",
    "npm test (in agentra/web/, runs vitest)"
  ],
  "build_commands": [
    "pip install -e .[dev]",
    "npm install && npm run build (in agentra/web/, tsc -b && vite build)",
    "docker build -t agentra:local .",
    "docker compose build --no-cache"
  ],
  "notes": "This repo IS the autonomous engineering system described in its own vision.md, not an app being improved by one. It operates on an external target repo passed via --repo / REPO_PATH. The ci/ workflow exists but is not wired into .github/workflows/ yet, pending a GitHub App permission grant.",
  "design_notes": "- Deliberate two-boundary safety model: Docker container isolation is primary; a regex PreToolUse hook (agents/safety.py) is defense-in-depth, rebuilt after discovering `can_use_tool` callbacks are silently never invoked under `permission_mode='bypassPermissions'`. - Production is reachable through exactly one code path (human `agentra promote` or an app's explicit `auto_remediate_prod: true` opt-in), enforced as real boolean checks in Python, never left to prompt instructions — the module docstrings repeatedly state 'control flow in Python, not prompts' as an explicit principle (circuit breakers, self-heal retry caps, stagnation detection in agents/brain.py). - State ownership is intentionally split: ephemeral/noisy data (run logs, test screenshots) stays local and gitignored; durable audit-trail data (shipped features, known bugs, feature queue, objective) lives entirely in GitHub Issues/Variables with no local fallback — an explicit availability tradeoff, not an oversight. - A 'feature' issue's open/closed state IS the shipped-vs-released state machine (closes only on production promotion), avoiding a duplicate JSON ledger. - Agents are cold, short-lived Claude CLI subprocesses per step (agents/base.py::run_agent), with resumable sessions only where conversational continuity is actually needed (dashboard chat), not in the autonomous cycle. - Retry-on-contradictory-CLI-result is opt-in per agent and explicitly disabled for implementation.py because a blind retry after a git commit already happened risks a duplicate/conflicting attempt."
}
```