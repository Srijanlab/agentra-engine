I have a comprehensive picture now. Here's the codebase understanding summary.

## Summary

**Agentra** is a Python-based autonomous product-engineering agent system: it's not the target app being improved, but the *system* that drives improvements to some other repo (the "target repo") by spawning Claude Agent SDK sessions for each specialized role.

**Core architecture**: a FastAPI backend (`agentra/server.py`, deployed as an always-on Cloud Run service) plus a Vite/React/TypeScript dashboard (`agentra/web/`) for observing runs, chat, and app management. Actual work happens via short-lived Claude Agent SDK subprocess calls (`agentra/agents/base.py::run_agent`), not standing services — "specialized agents run on demand." Two orchestration modes exist side by side: `orchestrator.py::run_cycle` (a hardcoded fixed pipeline: codebase→discovery→implementation→testing→deploy→feedback) and the default, `agents/brain.py::run_autonomous_cycle`, where an LLM "brain" picks from 9 tool-wrapped specialized agents and decides sequencing itself, bounded by deterministic circuit breakers (max consecutive tool failures, max cycle cost, stagnation detection) written in plain Python rather than left to prompting.

**Backend/data layer**: hybrid persistence. Agentra's own operational state (multi-app registry, durable inbox) lives in Firestore when `AGENTRA_FIRESTORE_PROJECT` is set, falling back to local JSON under `AGENTRA_HOME` for local dev (no GCP creds needed). Per-target-repo project knowledge (known bugs, feature queue, shipped features, objective) lives entirely in GitHub Issues/Actions Variables — deliberately no local JSON mirror, so it travels with the project via git/GitHub rather than agentra's own store. A small "architecture" memory (`.agentra/memory/architecture/*.md` steering files: codebase.md, design.md, testing-notes.md, documentation.md) is git-committed to the target repo itself.

**Deployment**: Terraform-based infra for GCP (Cloud Run, Firestore, Pub/Sub, Artifact Registry) and Cloudflare (tunnel/DNS/access), a multi-stage Dockerfile, docker-compose, and a sandboxed container model (non-root, read-only FS, capabilities dropped) documented in CONTAINER.md.

**Features** (as a product): objective-driven autonomous improvement cycles; production-debugging agent with opt-in auto-remediation; pre-prod-only deploys with human-gated promotion to prod; live dashboard with per-agent chat/voice, run logs, and a "standup" summary feature; GitHub-backed backlog/project-board sync; safety hooks blocking destructive/prod/secrets operations.

**Test/build tooling**: Python side — `pytest tests` (30+ test files covering safety hooks, GitHub sync, deployment, brain stagnation/blocking-bug logic, resume capability, etc.), plus `python -m py_compile` over all `agentra/*.py`, installed via `pip install -e .[dev]`. Web side — `npm ci`, `npm test` (vitest), `npm run build` (tsc -b && vite build). CI defined in `ci/github-actions-ci.yml` running both jobs on push/PR.

```json
{
  "framework": "Python/FastAPI backend orchestrating Claude Agent SDK sessions; React + TypeScript + Vite dashboard frontend",
  "backend": "Firestore (agentra's own multi-app registry/inbox, prod) with local-JSON fallback under AGENTRA_HOME for dev; per-target-repo product state lives in GitHub Issues/Actions Variables, not a database",
  "architecture": "Single FastAPI service (deployed on Cloud Run) that triggers short-lived Claude Agent SDK subprocesses on demand — not a queue of standing microservices. Two orchestration strategies: a hardcoded fixed-pipeline mode (orchestrator.py) and a default LLM-driven 'brain' mode (agents/brain.py) that dynamically selects among 9 tool-wrapped specialized agents, with deterministic Python-level safety rails (circuit breakers, cost caps, prod-promotion gating).",
  "features": [
    "Autonomous objective-driven improvement cycles (discover→implement→test→deploy→feedback)",
    "Production debugging agent with opt-in auto-remediation, always proven in pre-prod before promotion",
    "Human-gated production promotion (never automatic except explicit auto_remediate_prod opt-in)",
    "Live web dashboard: run logs, per-agent chat with voice, agent roster, run lifecycle view",
    "Daily/standup summaries",
    "GitHub-backed backlog (known bugs, feature queue, shipped features) with GitHub Projects board sync",
    "Multi-app registry with a durable, crash-safe request inbox",
    "Layered safety system: Docker sandboxing + regex-based PreToolUse hook blocking destructive/prod/secrets operations"
  ],
  "test_commands": [
    "pytest tests",
    "find agentra -name '*.py' -print0 | xargs -0 -n1 python -m py_compile",
    "npm test (in agentra/web, runs vitest)"
  ],
  "build_commands": [
    "pip install -e .[dev]",
    "npm ci && npm run build (in agentra/web, tsc -b && vite build)",
    "docker build -t agentra:local .",
    "docker compose build"
  ],
  "notes": "This repo IS the autonomous agent system described in vision.md (not an app being improved by it). It operates on other repos passed via --repo. Terraform configs exist for both GCP and Cloudflare deployment targets.",
  "design_notes": "- Deterministic control flow in plain Python for anything that must not silently fail or drift (circuit breakers, cost caps, prod-promotion gating, retry-on-contradictory-CLI-result logic) — prose/system-prompt instructions are explicitly distrusted for safety-critical invariants ('never deploy before local tests pass' is a real boolean check, not a prompt rule). - Safety is layered: Docker container isolation is primary; a regex-based PreToolUse hook (not the SDK's can_use_tool, which is silently bypassed under bypassPermissions mode) is defense-in-depth secondary. - Persistent state is deliberately split by ownership: agentra's own cross-app bookkeeping goes to Firestore/local-JSON (AGENTRA_HOME), while a target repo's product knowledge (bugs, features, shipped work) lives entirely in that repo's own GitHub Issues/Variables with no local mirror — 'project knowledge belongs with the project.' - Only 4/5 originally-planned local memory categories (architecture/decisions/features/metrics/failures) survived after auditing which ones actually had readers; unused write-only ledgers were deleted rather than kept 'just in case.' - Two parallel orchestration strategies coexist on purpose: a fixed hardcoded pipeline and an LLM-driven dynamic planner, so there's always a deterministic fallback path. - Testing agent runs in two independent passes (local test suite, then live pre-prod verification) rather than one, treating 'code passes tests' and 'the actual deployed thing works' as genuinely different claims. - Production is reachable from exactly one code path (auto-remediate hotfix promotion) and is structurally excluded from the LLM brain's own tool menu, not just discouraged by instruction."
}
```