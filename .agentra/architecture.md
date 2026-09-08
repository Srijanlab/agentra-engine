<!-- owner: agent:codebase -->
<!-- source-sha: 60c3f7ee2ece0d0571a6170e7d3fb9ea98cd0b57 -->
# engine — Architecture

## Purpose
agentra-engine is the API + state-authority service of the agentra autonomous product-engineering platform. It owns all cross-app state (app registry, runs, loops, the job queue, per-repo memory) and every credential (GitHub App key, Slack bot token, DynamoDB/GCP auth), exposing them to the dashboard (agentra-ui) over an authenticated REST API and to the executor (agentra-loop) over a token-gated `/internal` RPC. It runs no LLM and no build/deploy work itself.

## Stack
- Python >=3.11 (Docker image python:3.12-slim); FastAPI + Uvicorn + Pydantic.
- Prod host: Vercel serverless — `api/index.py` is the ASGI entry, `vercel.json` rewrites every path to it, `requirements.txt` is the install (no uvicorn, no dev extras). `Dockerfile` is a fallback for plain container hosts.
- Persistence: DynamoDB via boto3 (static `AGENTRA_AWS_*` IAM keys) when `AGENTRA_DYNAMODB_TABLE_PREFIX` is set; otherwise local JSON under `AGENTRA_HOME` (default `~/.agentra`).
- Auth: Firebase/Google ID-token verification (`google-auth`) for the dashboard gate; `pyjwt[crypto]` for minting GitHub App installation tokens.
- Observability: Langfuse (`langfuse>=4`), no-op without credentials.
- Tests: pytest + pytest-xdist (`-n auto` baked into `pyproject.toml`), `moto[dynamodb]`; `google-cloud-texttospeech` (dev extra) for `/tts`.
- `agentra` console-script CLI (`agentra.cli:main`).

## Module map
- `agentra/server/` — the FastAPI app. `__init__.py` wires middleware + all routers; `auth.py` (Firebase sign-in gate, CORS regex, public-path list); `routes/` (triggers, internal, apps, loops, systems, connectors, chat, standup, human_input, review); `state.py` (in-process `_active_runs`/`_app_locks`); `gh_cache.py`; `utils.py`.
- `agentra/registry/` — multi-app registry + durable work queue. `core.py` (apps, pause, llm-backend, `RepoSpec` resolution, DynamoDB init, Slack-thread map), `jobs.py` (`cycle|promote|prod_debug|human_resume` queue the loop drains), `runs.py`, `loops.py`, `inbox.py` (request submit/dispatch), `_dynamo.py`, `_cache.py`. The module object is replaced by a proxy that delegates `_DELEGATED_NAMES` to `core`.
- `agentra/memory/` — per-repo product state. `Memory` = 5 mixins (issues, issue_lifecycle, features, settings, specs); GitHub Issues/Projects-backed. `core.py` holds label constants + failure-triage regexes (transient / unfixable-by-agentra / login-required) + spec-header helpers.
- `agentra/connectors/` — GitHub App (`github_app.py` mints a per-`owner/repo` installation token), issues/pulls/projects/variables/issue-lifecycle mutations, `github_fake.py`, Slack (`slack.py` + `slack_allowlist.py`).
- `agentra/a2a/` — read-only A2A agent-discovery cards + `/.well-known/agent-card.json` (`routes.py`); the other files model tasks/messages/parts but aren't wired to executable dispatch here.
- `agentra/agents/` — `catalog.py` only: static per-agent capability/tool metadata for `/agents/metadata` and the a2a cards. No executable agents.
- `agentra/proxy/` — standalone FastAPI app translating the Anthropic Messages API to NVIDIA NIM chat-completions (the `nim` LLM backend).
- `agentra/` root — `cli.py` (env / objective / apps / submit / dispatch / serve), `environments.py` (per-app pipeline config stored in GitHub Actions Variables), `git_ops.py`, `artifacts.py`, `change_risk.py`, `ranking.py`, `chat_store.py`, `urls.py`, `langfuse_api.py`, `observability.py`, `dev_seed.py`.
- `api/index.py` — Vercel entrypoint; serves a diagnostic fallback app if `agentra.server` import fails.
- `tests/` — pytest suite; `conftest.py` strips prod-pointing env vars at import.

## Invariants
- The engine never executes a cycle / promotion / prod-debug / resume. Trigger endpoints call `registry.enqueue_job(...)`; agentra-loop claims and runs it. No `claude-agent-sdk` dependency.
- The loop reaches engine state only through `POST /internal/rpc` (bearer `AGENTRA_INTERNAL_TOKEN`), restricted to the `_REGISTRY_METHODS` / `_MEMORY_METHODS` frozenset whitelists; it never touches DynamoDB or GitHub directly. The GitHub App key and `SLACK_BOT_TOKEN` stay in the engine — the loop calls `/internal/git-token` and `/internal/slack/message`.
- Exactly one repo per app has `role="coordination"` (issues, `.agentra/memory`, objective); `Memory` is always the coordination repo. A legacy single-repo app has one `RepoSpec` that is both coordination and code.
- `cloud_mode()` (== DynamoDB configured) means no writable checkout: local-file `Memory` methods and `persist_agentra_dir` become no-ops / raise `OSError`, and repo resolution returns the stored path unresolved.
- `AGENTRA_AWS_*` is deliberately prefixed, not bare `AWS_*` — Vercel's Lambda runtime reserves the bare names for its own execution-role creds.
- Job claim is a CAS `pending -> claimed` on the `by-status` GSI; a job stuck in `claimed` past `STALE_PROCESSING_SECONDS` (1h) is re-queued so a crashed loop can't strand work. Terminal jobs carry `expires_at` for DynamoDB native TTL.
- `/health` and `/healthz` are identical and must never fail on a backend blip (catch-all -> `{status: degraded}`).

## Conventions
- Every registry/memory storage function branches `if core._ddb is not None: <dynamo> else: <local JSON>`; new state follows the same dual-path shape with a local fallback for tests/dev.
- `registry` sub-modules re-export through `registry/__init__.py`'s `__all__`; when adding one, update the `routes/internal.py` whitelist in the same change if the loop needs it.
- Routes are `APIRouter`s included at root prefix in `server/__init__.py`; auth exemptions go in `auth.py::_PUBLIC_PREFIXES`.
- Work-enqueuing endpoints: check `registry.is_paused()` first, dedup with `dedup_key=f"{kind}:{app}"`, record a run via `_new_run_key`, return `{triggered, run_key, job_id, queued}`.
- Per-file 500-line limit + SRP + a subfolder per domain (`CLAUDE.md`): split into mixins/packages rather than growing a file. Single-sentence docstrings, no dead code, no comment paragraphs.
- A feature that needs a repo checkout or Claude is held with HTTP 503 and a "moving to agentra-loop" message, not partially implemented (`chat.py`, `standup.py`).
- `_json_safe` coerces dataclasses/`Path` for anything returned over RPC.

## Gotchas
- `tests/conftest.py` pops `AGENTRA_ENGINE_URL`, `AGENTRA_DYNAMODB_TABLE_PREFIX`, `AGENTRA_AWS_*`, and GCP vars before anything imports `agentra` — running pytest with those set has previously written test fixtures straight into prod. Tests exercising those paths must monkeypatch and mock the transport themselves.
- `registry` and `memory` are import-time module swaps/proxies. `import agentra.registry as r; r._ddb = x` writes through to `core`, but only for names in `_DELEGATED_NAMES`.
- `/internal/rpc` ignores `x-forwarded-for` on purpose (client-spoofable); only Vercel-set `x-real-ip` / `x-vercel-forwarded-for` are trusted for the IP allowlist.
- `_memory_for` over RPC returns a real `Memory` only if the coordination-repo checkout happens to exist on the engine host; otherwise a `_UrlMemory` on a throwaway tmpdir whose local-file methods write nowhere useful (and aren't whitelisted anyway).
- No inbound Slack/GitHub webhook: human answers to `need_human` issues are discovered by polling issue comments on the `/trigger/cron` tick (`_reconcile_human_input_for_app`).
- Off cloud mode, `get_app_repo` runs on nearly every request and may `git pull` (throttled to once per 30s per repo) — a slow remote slows the whole API.
- DynamoDB init never raises: on bad creds `_ddb` is `None` and the engine silently falls back to local JSON (empty in prod) rather than erroring. Diagnose via `/debug/dynamodb`.
- `agents/catalog.py` still describes the full agent pipeline (orchestrator, implementation, deployment, ...) that actually runs in agentra-loop — it is display metadata only.
- Vercel function `maxDuration` is 30s; any endpoint doing real work must enqueue a job, not block.
