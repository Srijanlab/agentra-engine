I now have a comprehensive picture of the repository. Here's the summary.

## Codebase Understanding: `agentra`

**What it is:** This repo *is* the "Autonomous Product Engineering Agent System" described in its own `vision.md` — a meta-system built with the Claude Agent SDK that, given a business objective (not a feature spec), understands a *target* codebase, discovers what to build, implements it, tests it, deploys to pre-prod, and (only with explicit human/opt-in approval) promotes to production. It runs itself against other repos, mounted/cloned as the "target app."

**Stack:** Python 3.11 backend (`agentra/`) using `claude-agent-sdk`, `fastapi`+`uvicorn` (HTTP trigger/dashboard API), `pyjwt` (GitHub App auth), `google-cloud-firestore` (durable state), `playwright` (browser QA), packaged via `setuptools`/`pyproject.toml` with a `agentra` CLI entry point. Frontend is a React 18 + TypeScript + Vite + Tailwind v4 dashboard (`agentra/web/`) served as static files by FastAPI, using Vitest/Testing-Library for tests.

**Architecture:** A single always-on FastAPI service (deployable to Cloud Run) that spawns short-lived Claude Agent SDK subagent "turns" on demand — not microservices, more like an orchestrator process invoking narrowly-scoped in-process agent modules (`agents/codebase.py`, `discovery.py`, `implementation.py`, `testing.py`, `deployment.py`, `feedback.py`, `prod_debug.py`), each a thin wrapper around a Claude Agent SDK `query()` call with its own system prompt and tool grants. Two competing orchestration entry points coexist: `orchestrator.py::run_cycle()` (fixed hardcoded pipeline order) and `agents/brain.py::run_autonomous_cycle()` (an LLM "brain" that dynamically decides which of 9 MCP-exposed tools to call and in what order) — the latter is now the default, the former kept for a deterministic fallback path.

**Backend/data layer:** Deliberately hybrid and app-agnostic. Agentra's own operational state (registry of managed apps, inbox/queue) lives in Firestore when `AGENTRA_FIRESTORE_PROJECT` is set, else local JSON files. Per-target-app knowledge (backlog, known bugs, shipped features, objective) is stored as GitHub Issues/Projects/Actions Variables directly via GitHub App/REST connectors (`connectors/github_*.py`) with **no local-file fallback** if GitHub is unreachable. A small set of "steering files" under `.agentra/memory/architecture/` (codebase.md, design.md, testing-notes.md, documentation.md) are plain git-committed markdown, overwritten per-run and read as shared context by downstream agents.

**User-facing features (of the agentra dashboard/system itself):**
- Register/manage target apps, view runs, live-streaming run logs, standups
- Trigger cycles (fixed-pipeline, autonomous "brain", or on-demand prod-debug) via CLI, HTTP triggers (Cloud Scheduler / alert webhook / Pub/Sub), or dashboard button
- Human-gated "Promote to prod" action
- Agent chat modal, voice ("useAgentVoice"), activity/loops/standups panels in the React dashboard

**Test/build tooling:**
- Python: `pytest tests` (26 test files covering safety hooks, git ops, memory, registry sync, GitHub fakes/issues/projects, deployment, brain stagnation/blocking-bugs, chat store, etc.), plus `python -m py_compile` over all `.py` files; installed via `pip install -e .[dev]`.
- Web: `npm ci`, `npm test` (Vitest), `npm run build` (`tsc -b && vite build`) in `agentra/web/`.
- CI: `ci/github-actions-ci.yml` runs both jobs on push to `main`/`beta` and on PRs.
- Container: Dockerfile + `docker-compose.yml` + `run-agent.sh` for sandboxed execution; Terraform under `deploy/gcp` and `deploy/cloudflare` for infra.

**Design decisions/patterns actually observed in code:**
- **Deterministic Python for anything that must not silently fail, prose/LLM-driven for judgment calls.** Git checkout/commit/push (`git_ops.py`, `implementation.py`) and safety enforcement (breaker thresholds in `brain.py`, deploy gating on `tests_passed`) are hard Python logic — the docstrings cite *observed* dogfooding failures (e.g. an LLM told "commit at the end" via prompt simply didn't) as the reason, not hypothetical caution.
- **Layered safety, explicitly not a sandbox substitute:** Docker container isolation is primary; a regex `PreToolUse` hook (`agents/safety.py`) is defense-in-depth, blocking destructive bash, secrets/`.env` edits, and production-touching commands. The module docstring documents a real bug they hit and fixed: `can_use_tool` callbacks are silently never invoked under `bypassPermissions` mode, so it was rebuilt on SDK hooks.
- **Production is a hard architectural boundary**, not a policy note — `promote_prod()` is reachable only via human-triggered `agentra promote`/dashboard button, or a single explicitly opt-in (`auto_remediate_prod: true`) auto-remediation path in `orchestrator.run_prod_debug_cycle`, and is never one of the autonomous "brain" agent's 9 tools.
- **Circuit breakers on autonomous LLM decision-making**: consecutive-per-tool-failure breaker and a "stagnation" breaker (same tool+args repeated with no state change over a sliding window) stop a cycle deterministically rather than trusting the model to know when to quit, plus a hard cost cap ($3/cycle).
- **GitHub Issues/Projects as the durable backlog/state machine** instead of a bespoke DB: features are Issues (with linked Project boards), bugs are Issues only, "shipped" = closed issue with run_id/commit_sha trail — avoiding a duplicate ledger.
- **Two independent testing passes are a deliberate distinction**, not redundancy: `run_local_tests` validates code, `verify_pre_prod` independently re-validates the *live* deployed instance, because local-pass ≠ deploy-actually-works (missing env vars, build-only breakage).
- Memory subsystem was pruned from vision.md's original 5-category design down to what actually had readers — an explicit "delete unused abstraction" decision recorded in `memory.py`'s docstring.

```json
{
  "framework": "Claude Agent SDK (Python) backend + FastAPI + React/TypeScript/Vite frontend",
  "backend": "Hybrid: GCP Firestore (or local JSON fallback) for agentra's own operational state; GitHub Issues/Projects/Actions Variables (no local fallback) as the durable backlog/state for each target app",
  "architecture": "Single long-running FastAPI orchestrator service that spawns short-lived Claude Agent SDK subagent turns on demand; not microservices — a hub of narrowly-scoped agent modules coordinated either by a hardcoded pipeline (orchestrator.py) or an LLM-driven tool-calling 'brain' (agents/brain.py)",
  "features": [
    "Multi-app registry and dashboard for managing target repos",
    "Autonomous improvement cycle: understand codebase -> discover feature -> implement -> test -> deploy to pre-prod -> assess feedback",
    "On-demand production debugging agent with optional auto-remediation (opt-in only)",
    "Human-gated promote-to-production action",
    "Live-streaming run logs, activity/loops/standups panels, agent chat modal, voice output",
    "HTTP trigger endpoints for scheduler/alerting/pubsub-driven cycles",
    "GitHub-backed backlog: known bugs, feature request queue, shipped features as Issues/Projects"
  ],
  "test_commands": [
    "pytest tests",
    "python -m py_compile <every agentra/**/*.py>",
    "npm test (vitest run, in agentra/web/)"
  ],
  "build_commands": [
    "pip install -e .[dev]",
    "npm ci && npm run build (tsc -b && vite build, in agentra/web/)",
    "docker build -t agentra:local . / docker compose build"
  ],
  "notes": "This repo is itself the 'agentra' autonomous engineering system from vision.md, meant to run against other target repositories (mounted or cloned) rather than being a product codebase in its own right. Extensive, unusually candid docstrings throughout document real observed failure modes (dogfooding bugs) that drove specific design choices — treat those docstrings as authoritative over vision.md where they diverge, since vision.md is the original aspirational spec and the code has since evolved past/around parts of it (e.g. memory.py's category pruning, brain.py superseding orchestrator.py as the default).",
  "design_notes": "- Deterministic Python (not LLM prompts) for anything that must not silently fail: git checkout/commit/push, deploy-gating on test results, safety enforcement, breaker thresholds — each justified in docstrings by a specific observed agent failure, not hypothetical risk. - Safety is explicitly layered and non-sandboxing: Docker isolation is primary, a regex PreToolUse hook is defense-in-depth (and the code notes a real bug where can_use_tool callbacks were silently dead under bypassPermissions mode, since fixed via SDK hooks). - Production is architecturally unreachable from autonomous flows: only a human `agentra promote` call or a single explicit per-app opt-in (auto_remediate_prod) auto-remediation path can touch prod; it is never one of the 'brain' agent's 9 exposed tools. - Circuit breakers (consecutive-failure count, stagnation/no-progress detection over a sliding window, hard cost cap) stop autonomous cycles deterministically rather than trusting the LLM's own judgment to know when to quit. - GitHub Issues/Projects serve as the durable, app-specific backlog and shipped-feature ledger instead of a bespoke database, avoiding duplicate state. - Two independent test passes (local code tests vs. live pre-prod verification) are a deliberate design choice, not redundancy, since a locally-passing build can still fail once actually deployed."
}
```