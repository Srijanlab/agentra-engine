## Summary

This repository is **agentra** — an autonomous product-engineering agent system (not a typical product app). It's a meta-system: a fleet of specialized Claude-Agent-SDK-driven agents that operate *on* other repositories (or, when run against itself, on its own code) to autonomously discover, build, test, and deploy features per `vision.md`'s spec.

**Language/framework**: Python 3.11 backend (FastAPI + `claude-agent-sdk`) with a React 18 + TypeScript + Vite + Tailwind dashboard frontend (`agentra/web/`), packaged as a CLI (`agentra` entry point) and a Docker container for sandboxed execution.

**Architecture**: A monolithic Python service (`agentra/server.py`, deployed as a single always-on Cloud Run service) that fans out to short-lived Claude Agent SDK subprocess "agents" on demand — not microservices, but an orchestrator + specialized-agent pattern. Two orchestration modes exist: a fixed pipeline (`orchestrator.py::run_cycle`, hardcoded understand→discover→implement→test→deploy sequence) and the default LLM-driven brain (`agents/brain.py::run_autonomous_cycle`) where an Orchestrator Agent picks its own sequence from 9 exposed MCP-style tools, with deterministic circuit breakers (max consecutive tool failures, max cycle cost, stagnation detection) enforced in plain Python around it.

**Backend/data**: Firestore when `AGENTRA_FIRESTORE_PROJECT` is set, falling back to local JSON files under `AGENTRA_HOME` for local dev (agentra's own operational state — apps registry, inbox, runs). Per-target-repo audit trail/memory (`.agentra/memory/architecture/*.md` "steering files") is committed to the target repo's own git history; feature/bug lifecycle state lives entirely in GitHub Issues/Projects/Actions Variables, with no local JSON mirror by design.

**Features** (this is infrastructure, not an end-user product, so "features" are its own capabilities): multi-agent pipeline (codebase understanding, product discovery, research/implementation, local + live pre-prod testing, pre-prod/prod deployment via Vercel/Firebase CLIs, analytics feedback loop, production debugging/auto-remediation), a React ops dashboard (apps, runs, live agent chat, standups, loops views), GitHub App integration for issues/projects/variables, Slack-style daily standups, HTTP trigger endpoints (schedule/alert/queue) for unattended operation, and a layered safety system (Docker sandbox + regex `PreToolUse` hook gate blocking destructive ops, secrets, and unapproved prod access).

**Test/build tooling**:
- Python: `pytest tests` (see `pyproject.toml` `[dev]` extra), plus `py_compile` sanity check
- Web: `npm test` (Vitest + Testing Library), `npm run build` (`tsc -b && vite build`)
- CI: `ci/github-actions-ci.yml` runs both jobs on push/PR to main/beta

```json
{
  "framework": "Python 3.11 (FastAPI, claude-agent-sdk) backend; React 18 + TypeScript + Vite + Tailwind frontend",
  "backend": "Firestore (when AGENTRA_FIRESTORE_PROJECT set) with local-JSON-file fallback for agentra's own operational state; per-target-repo memory lives as committed files/GitHub Issues/Projects/Actions Variables in the repo being improved",
  "architecture": "Single-process monolith (FastAPI service on Cloud Run) that orchestrates short-lived Claude Agent SDK subprocess agents on demand; supports a fixed hardcoded pipeline and a default LLM-driven orchestrator ('brain') that picks its own tool sequence under deterministic Python-enforced circuit breakers",
  "features": [
    "Codebase Understanding Agent (read-only repo scanning)",
    "Product Discovery Agent (feature opportunity generation)",
    "Implementation Agent (branch-per-feature implement/test/fix loop)",
    "Testing Agent (local + live pre-prod verification)",
    "Deployment Agent (pre-prod via Vercel/Firebase, human-gated prod promotion)",
    "Production Debugging Agent (diagnose + optional auto-remediate hotfix path)",
    "Analytics Feedback Agent",
    "React ops dashboard (apps, runs, live agent chat/voice, standups, loops)",
    "GitHub App integration (issues, projects, variables as system of record)",
    "HTTP trigger endpoints for schedule/alert/queue-driven unattended runs",
    "Daily standup reporting"
  ],
  "test_commands": ["pytest tests", "npm test (in agentra/web/, Vitest)"],
  "build_commands": ["pip install -e .[dev]", "npm install && npm run build (in agentra/web/)", "docker build -t agentra:local .", "python -m py_compile (CI sanity pass)"],
  "notes": "This repo IS the agent system described in vision.md, not a product being improved by it -- when agentra runs against itself it is dogfooding its own Codebase Understanding Agent prompt. tests/ is heavily scenario-named after real observed failure modes (e.g. test_brain_stagnation.py, test_claude_stream_logging.py, test_safety_integration.py), suggesting a codebase whose design was refined by live dogfooding runs rather than upfront spec alone.",
  "design_notes": "- Deterministic Python, not LLM prose, for anything that must not silently fail: git checkout/commit/push in implementation.py and git_ops.py, safety enforcement in safety.py, and circuit breakers/stagnation detection in brain.py -- multiple docstrings cite specific observed dogfooding failures (e.g. an LLM told 'checkout branch X, commit at the end' via prose simply didn't) as the reason logic moved out of prompts into code.\n- Safety is explicitly layered and documented as non-sandbox defense-in-depth: Docker container isolation is the real boundary; the regex-based PreToolUse hook in agents/safety.py is a second line of defense the module docstring says was previously silently dead code (a can_use_tool callback never invoked under bypassPermissions) until rebuilt on the SDK's actual hook mechanism -- a concrete case of a bug being found and documented in-place rather than just fixed silently.\n- Hybrid/graceful-degradation persistence pattern repeated twice: registry.py falls back from Firestore to local JSON when AGENTRA_FIRESTORE_PROJECT is unset, and git auth (git_ops.py) tries a GitHub App installation token first, falling back to static GIT_ASKPASS/GITHUB_TOKEN credentials, never hard-failing when the fancier path is unavailable.\n- Clear separation of durable-state ownership: agentra's own bookkeeping (which apps, pending inbox requests) lives in agentra's infra (Firestore/local JSON under AGENTRA_HOME); a target project's own knowledge (shipped features, known bugs, objective) is deliberately pushed into that project's own git history and GitHub Issues/Variables so it travels with the project and survives agentra redeploys.\n- Production is treated as a hard boundary enforced in multiple independent places at once (regex safety patterns, orchestrator.py never exposing promote_prod as an autonomous brain tool, allow_prod defaulting False everywhere) rather than trusted to any single check."
}
```