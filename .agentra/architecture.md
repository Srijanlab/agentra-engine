<!-- owner: agent:codebase -->
<!-- source-sha: cae3ec8fb32537e13541040df2da15c9cbdbc663 -->
## Purpose

The credential-holding HTTP API of the Agentra autonomous product-engineering system (split from `srijanlab-agentra` into engine / loop / ui, see `SPLIT.md`). A FastAPI service consumed by the `agentra-ui` dashboard (browser, Firebase-auth) and the `agentra-loop` orchestrator (token-gated `/internal` RPC). It owns Firestore (multi-app registry, runs, loops, inbox), DynamoDB (durable run-log tails), GitHub App tokens, and Slack. The package still bundles the full orchestrator / LLM brain / specialized agents and can also run improvement cycles in-process.

## Stack

- Python >=3.11 (CI on 3.12), FastAPI + Starlette. Primary host: Vercel Fluid Compute ASGI via `api/index.py`; also a Docker image pushed to GHCR (`.github/workflows/image.yml`) for box/Fly/Oracle hosting. `uvicorn` for local `agentra serve`.
- `claude-agent-sdk` (agent subprocesses / brain), `pyjwt[crypto]` (GitHub App JWT), `httpx`, `pyyaml`.
- `google-cloud-firestore`, `boto3` (DynamoDB), `langfuse` (tracing/observability).
- Tests: `pytest` + `pytest-xdist` (`-n auto` is the pyproject default), `moto[dynamodb]`. CI (`.github/workflows/ci.yml`): `pip install -e .[dev]`, a `py_compile` sweep of `agentra/`, then `pytest tests`.
- Optional extras `[agents]`/`[dev]` pull `playwright` + `graphifyy` — NOT in engine runtime (`requirements.txt` / `Dockerfile` omit them and prune the bundled Claude CLI).
- Terraform under `deploy/` (GCP parked — billing closed; Cloudflare tunnel/DNS/access).

## Module map

- `agentra/server/` — FastAPI app. `server/__init__.py` builds `app`, wires middleware (Vercel OIDC refresh + lazy Firestore, Firebase-auth gate, CORS) and includes every router. `routes/`: apps, triggers (scheduled/alarm/queue/promote), human_input, chat, standup, loops, review, slack, connectors, systems, internal. `auth.py` = Firebase ID-token gate + `_PUBLIC_PREFIXES`. `state.py` = in-process run/lock dicts.
- `agentra/server/routes/internal.py` — token-gated (`AGENTRA_INTERNAL_TOKEN`) `/internal/rpc` dispatching to whitelisted `registry.*` / `Memory.*` methods, plus `/git-token`, `/slack/message`, `/runs/{id}/log`. The loop's only door to engine-held state.
- `agentra/registry/` — multi-app registry, runs, loops, durable inbox. Backends: Firestore (`AGENTRA_FIRESTORE_PROJECT`), DynamoDB (`AGENTRA_DYNAMODB_*`), or local JSON under `AGENTRA_HOME`. `__init__.py` is a `RegistryModule` proxy re-exporting `core`/`inbox`/`runs`/`loops`.
- `agentra/memory/` — repo-scoped product state (`Memory`), mixin-composed (issues, issue_lifecycle, features, settings, specs). Backed by GitHub Issues/Projects + Actions Variables and per-repo `.agentra/` spec files; no local backlog mirror. `core.py` holds failure-classification regex lists.
- `agentra/agents/` — specialized agents (codebase, discovery, requirements, architecture_review, implementation, testing, deployment, feedback, prod_debug, generic, human_answer_judge, screenshot, git_ops), `base.py` (`run_agent`/`stream_chat_turn` over claude-agent-sdk), `safety.py` (PreToolUse regex hook), `catalog.py` (dashboard metadata), `brain/` (LLM orchestrator: `run_autonomous_cycle`, `OrchestratorSession`, tool wrappers, circuit breakers, prompts).
- `agentra/orchestrator.py` — fixed-pipeline `run_cycle`, plus `run_promote` and `run_prod_debug_cycle`.
- `agentra/connectors/` — `github_app` (installation tokens), github_issues/pulls/projects/project_mutations/variables, `github_fake` (tests), `slack` (outbound only), `slack_allowlist`.
- `agentra/a2a/` — read-only A2A agent-card discovery endpoints.
- `agentra/proxy/main.py` — standalone Anthropic->NVIDIA NIM translation proxy (separate ASGI app, opt-in "nim" backend).
- `agentra/environments.py` — per-repo local/pre-prod/prod config, stored ONLY in GitHub Actions repo Variables (`AGENTRA_*`).
- `agentra/cli.py` — `agentra` console script: run / loop / promote / debug-prod / objective / apps / submit / dispatch / serve.
- `api/index.py` Vercel entrypoint; `deploy/`, `docs/`, `tests/`.

## Invariants

- Production is reachable from exactly two explicit paths: `orchestrator.run_promote` (human) and prod-debug auto-remediate when `EnvironmentConfig.auto_remediate_prod` is on. The brain's MCP tool menu structurally excludes any prod tool.
- Every agent subprocess runs `permission_mode="bypassPermissions"` with `make_hooks(allow_prod=False)`; `allow_prod=True` is passed only for the single promote call.
- Deploy gating is real Python booleans, not prompt text: no pre-prod deploy unless local tests passed; no promote unless pre-prod verified live; a push-failed feature branch is a deterministic hard stop (`check_push_failure`).
- `OrchestratorSession` circuit breakers: `MAX_CONSECUTIVE_TOOL_FAILURES=2`, stagnation = `STAGNATION_WINDOW=5` identical no-progress calls, open `blocking_agentra` bugs abort the pre-flight (auth-classified ones get one cheap zero-retry attempt then auto-clear).
- The loop reaches engine-held state (Firestore, GitHub, Slack, git creds) only via `/internal` RPC against the `_REGISTRY_METHODS` / `_MEMORY_METHODS` whitelists — never Firestore/GitHub directly.
- `EnvironmentConfig` lives ONLY in GitHub Actions repo Variables; no local or Firestore copy. Backlog lives ONLY in GitHub Issues/Projects; `Memory` has no local JSON mirror.
- The engine has no code checkout and a read-only FS: `Memory` local-file methods may raise `OSError` and callers must tolerate it; `_UrlMemory` serves GitHub-backed methods from a bare `repo_url`.
- Firestore can't be built at import on Vercel (per-request OIDC token) — `ensure_firestore()` lazy-inits in middleware; `/health` and `/healthz` must never fail on a Firestore blip.
- Every non-JSON `.agentra/` spec file starts with the two-line `<!-- owner: --><!-- source-sha: -->` provenance header.
- A code repo's `architecture.md` is exactly these 6 sections, ~200 lines, agent-maintained; coordination-repo `product.md` / `architecture.md` / `decisions/` are human-owned and never agent-written (`docs/agentra-spec.md`).

## Conventions

- New HTTP surface = a router in `agentra/server/routes/` included in `server/__init__.py`; any unauthenticated path must be added to `_PUBLIC_PREFIXES` in `server/auth.py`.
- Loop-facing state access = add the method name to the whitelist in `internal.py`, not a new bespoke endpoint.
- Files stay < 500 lines, single responsibility, domain subfolders (`CLAUDE.md`); oversized modules become packages with a re-exporting `__init__.py` (registry, memory, agents/brain).
- Docstrings: one summary sentence; no dead / commented-out code.
- Agent invocation goes through `agents/base.py`; logging via the ambient `run_log_scope` ContextVar.
- Anything safety-critical is a deterministic Python check, never a system-prompt instruction.
- New failure modes: classify (transient / unfixable-by-agentra / login-required) via the `memory/core.py` regex lists and route through `Memory.record_failure`.
- Observability: wrap with `@observe` / `propagate_attributes`; every Langfuse call must no-op without credentials (try/except).
- Persist `.agentra/` specs via `deployment.persist_repo_specs` / `persist_audit_trail` to the pre-prod branch, never a feature branch.
- Tests: `tests/test_*.py` using `connectors/github_fake.py` and `moto`; live-integration tests needing real Claude creds exist but are not auto-run (mirror `test_safety_integration.py`).

## Gotchas

- The engine<->loop divergence in `SPLIT.md` is NOT complete in the Python code: the full brain/agents/orchestrator are still present and `triggers.py` / `human_input.py` spawn `run_autonomous_cycle` in-process. Only the deploy artifacts are slim.
- `requirements.txt` (Vercel) != `pyproject.toml` deps: the Vercel install prunes `claude_agent_sdk/_bundled` (~199 MB) and drops uvicorn/playwright/graphifyy.
- `agentra/registry/__init__.py` reassigns its own `__class__` to `RegistryModule` so module-level names (`AGENTRA_HOME`, `_db`, ...) proxy to `registry.core`; setting `registry.X` in a test mutates `core`.
- `_sdk_env()`: the container base env must NOT carry `CLAUDE_CODE_OAUTH_TOKEN`, or the "claude" login backend can never win over the headless-token path.
- Claude CLI auth failures (`is_login_required_failure`) are a distinct class everywhere: zero retries, filed as a `blocking_agentra` + `need_human` bug, Slack-notified. `_find_similar_open_bug` dedup is content-aware only after issue #42's third pass (generic boilerplate titles used to merge unrelated failures).
- `/internal/rpc` `_client_ips` trusts only platform-set `x-real-ip` / `x-vercel-forwarded-for`; `x-forwarded-for` is deliberately ignored as spoofable.
- Run logs: `.agentra/logs/` is gitignored and REPOS_ROOT is ephemeral — the only durable copy is the DynamoDB `run-logs` table tail (last 500 lines). No DynamoDB => run logs vanish on redeploy.
- The `firebase use` safety regex only allows literal aliases `pre-prod|pre_prod|beta|staging`; a repo with a differently-named pre-prod alias gets blocked.
- Auth middleware stays fully open when `FIREBASE_PROJECT_ID` is unset (local / self-hosted).
- `ci/github-actions-ci.yml` and `.agentra/memory/architecture/deployment.md` (self-hosted-VM/GCP topology) are stale; the live pipeline is `.github/workflows/ci.yml` + `image.yml`, and GCP is parked (`deploy/cloudrun.parked/`).
