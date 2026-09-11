<!-- owner: agent:codebase -->
<!-- source-sha: 9e2f0780b02c44bee77e92ece232a164e102975e -->
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
- `agentra/server/` — the FastAPI app. `__init__.py` wires middleware + all routers; `auth.py` (Firebase sign-in gate, CORS regex, public-path list); `routes/` (triggers, internal, apps, schedule, loops, systems, connectors, chat, standup, human_input, review); `state.py` (in-process `_active_runs`/`_app_locks`); `gh_cache.py`; `utils.py`.
- `agentra/registry/` — multi-app registry + durable work queue. `core.py` (apps, pause, llm-backend, `RepoSpec` resolution, DynamoDB init, Slack-thread map), `jobs.py` (`cycle|promote|prod_debug|human_resume` queue the loop drains), `runs.py`, `loops.py` (loop binding, roll-up, recency-ordered `list_loops`), `inbox.py` (request submit/dispatch), `scheduler/` (read-only `compute_schedule_status` / `ScheduleStatus` — when an app is next due for a scheduled cycle), `_dynamo.py`, `_cache.py`. The module object is replaced by a proxy that delegates `_DELEGATED_NAMES` to `core`.
- `agentra/memory/` — per-repo product state. `Memory` = 5 mixins (issues, issue_lifecycle, features, settings, specs); GitHub Issues/Projects-backed. `core.py` holds label constants (incl. the `status:awaiting-testing` label and its legacy `status:shipped` alias) + failure-triage regexes (transient / unfixable-by-agentra / login-required) + spec-header helpers + `pipeline_stages()` (the dashboard's single ordered list of pipeline columns) + `_label_names()` (normalizes a GitHub-issue payload's `labels` into a flat name set — see Gotchas).
- `agentra/connectors/` — GitHub App (`github_app.py` mints a per-`owner/repo` installation token), issues/pulls/projects/variables/issue-lifecycle mutations (incl. `migrate_awaiting_testing_label`, the one-time label-rename migration), `github_fake.py`, Slack (`slack.py` + `slack_allowlist.py`).
- `agentra/a2a/` — read-only A2A agent-discovery cards + `/.well-known/agent-card.json` (`routes.py`); the other files model tasks/messages/parts but aren't wired to executable dispatch here.
- `agentra/agents/` — `catalog.py` only: static per-agent capability/tool metadata for `/agents/metadata` and the a2a cards. No executable agents.
- `agentra/proxy/` — standalone FastAPI app translating the Anthropic Messages API to NVIDIA NIM chat-completions (the `nim` LLM backend).
- `agentra/` root — `cli.py` (env / objective / apps / submit / dispatch / migrate-labels / serve), `environments.py` (per-app pipeline config stored in GitHub Actions Variables), `git_ops.py`, `artifacts.py`, `change_risk.py`, `ranking.py`, `chat_store.py`, `urls.py`, `langfuse_api.py`, `observability.py`, `dev_seed.py`.
- `api/index.py` — Vercel entrypoint; serves a diagnostic fallback app if `agentra.server` import fails.
- `tests/` — pytest suite; `conftest.py` strips prod-pointing env vars at import.

## Invariants
- The engine never executes a cycle / promotion / prod-debug / resume. Trigger endpoints call `registry.enqueue_job(...)`; agentra-loop claims and runs it. No `claude-agent-sdk` dependency.
- The loop reaches engine state only through `POST /internal/rpc` (bearer `AGENTRA_INTERNAL_TOKEN`), restricted to the `_REGISTRY_METHODS` / `_MEMORY_METHODS` frozenset whitelists; it never touches DynamoDB or GitHub directly. The GitHub App key and `SLACK_BOT_TOKEN` stay in the engine — the loop calls `/internal/git-token` and `/internal/slack/message`.
- Exactly one repo per app has `role="coordination"` (issues, `.agentra/memory`, objective); `Memory` is always the coordination repo. A legacy single-repo app has one `RepoSpec` that is both coordination and code.
- `cloud_mode()` (== DynamoDB configured) means no writable checkout: local-file `Memory` methods and `persist_agentra_dir` become no-ops / raise `OSError`, and repo resolution returns the stored path unresolved.
- `AGENTRA_AWS_*` is deliberately prefixed, not bare `AWS_*` — Vercel's Lambda runtime reserves the bare names for its own execution-role creds.
- Job claim is a CAS `pending -> claimed` on the `by-status` GSI; a job stuck in `claimed` past `STALE_PROCESSING_SECONDS` (1h) is re-queued so a crashed loop can't strand work. Terminal jobs carry `expires_at` for DynamoDB native TTL.
- `/health` and `/healthz` are identical and must never fail on a backend blip (catch-all -> `{status: degraded}`). Both also return `commit` (deployed `VERCEL_GIT_COMMIT_SHA`, fallback `AGENTRA_BUILD_SHA`, else `""`) so the loop's `verify_pre_prod` can confirm a pre-prod deploy has caught up.
- The pre-prod-merged-awaiting-verification status label was renamed `status:shipped` -> `status:awaiting-testing` (GitHub issue #38); every write path emits the new name and strips the old, but every read path (`_at_awaiting_testing_stage`, `issue_status`, `_items_at_stage`) still matches both, so a repo never carrying the new label doesn't silently lose items mid-pipeline.
- A loop left `active`/`waiting_for_human`/`escalated` whose tracked issue is later closed by a human is otherwise orphaned forever: `_reconcile_closed_issue_loops` (run for every app on each `/trigger/cron` tick) marks it `released` with pipeline `terminal=True` so no later scheduled cycle re-binds to it (agentra#20/#25).
- A loop's `last_run_at` is stamped only by `roll_up_loop` (a run actually finishing); `updated_at` is bumped by every write, including administrative-only ones (`set_loop_status`, `set_loop_pipeline`) from a retire/reconcile pass. `list_loops`/`_recency()` order by `last_run_at`/`created_at`, never `updated_at`, so a bulk administrative touch can't shove stale loops to the top of the dashboard's Loops list.

## Conventions
- Every registry/memory storage function branches `if core._ddb is not None: <dynamo> else: <local JSON>`; new state follows the same dual-path shape with a local fallback for tests/dev.
- `registry` sub-modules re-export through `registry/__init__.py`'s `__all__`; when adding one, update the `routes/internal.py` whitelist in the same change if the loop needs it.
- Routes are `APIRouter`s included at root prefix in `server/__init__.py`; auth exemptions go in `auth.py::_PUBLIC_PREFIXES`.
- Work-enqueuing endpoints: check `registry.is_paused()` first, dedup with `dedup_key=f"{kind}:{app}"`, record a run via `_new_run_key`, return `{triggered, run_key, job_id, queued}`.
- Schedule-due timing lives in one place: `registry.scheduler.compute_schedule_status(app, repo)` is shared by the `/trigger/scheduled` cron enqueue path and the read-only `GET /apps/{app}/schedule`; don't re-derive `due_in` from `environments.load` + `last_run_at` inline.
- Pipeline stage ordering/display lives in one place too: `memory.core.pipeline_stages()`, served read-only via `GET /pipeline/stages` for the dashboard — don't hardcode stage names/order elsewhere.
- GitHub label membership checks always go through `memory.core._label_names(issue)`, never a bare `LABEL in issue["labels"]` — the real GitHub REST payload's labels are `{"name": ..., ...}` objects, not plain strings (`github_fake`'s flat strings are the exception, not the rule).
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
- Open issues still carrying the legacy `status:shipped` label (pre-#38) only get renamed onto `status:awaiting-testing` when `migrate_awaiting_testing_label` actually runs for that repo — via `agentra migrate-labels --repo <path>` or automatically on `POST /apps` registration. Until then they rely on every read path's back-compat matching, not an actual label change.
- `github_issues.get_issue`'s `labels` are real GitHub REST objects, not the flat strings `github_fake` returns — a bare `LABEL in issue["labels"]` silently always misses against real GitHub. `human_input_pending` shipped with exactly this bug: it always returned `False`, so the escalation throttle guard re-posted the same "Auto-escalated" comment on every cycle instead of once (confirmed live on agentra#38/#15) until it was fixed to use `_label_names()`.
- The `by-app-recency` DynamoDB GSI is keyed on `updated_at`, not the real-activity `last_run_at` — `_query_loops_by_app` overfetches `_RECENCY_OVERFETCH` (200) items off the index and re-sorts by `_recency()` in Python before truncating to `limit`; a naive `Limit=limit` query straight off the GSI can return the wrong top-N whenever an administrative pass has recently touched `updated_at` on loops that didn't actually run.
