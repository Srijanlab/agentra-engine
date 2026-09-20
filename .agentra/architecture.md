<!-- owner: agent:codebase -->
<!-- source-sha: 3b510085b9233eb46647a0adf4147b90d569a2dd -->
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
- `agentra/server/` — the FastAPI app. `__init__.py` wires middleware + all routers; `auth.py` (Firebase sign-in gate, CORS regex, public-path list); `routes/` (triggers, internal, apps, schedule, loops, systems, connectors, chat, standup, human_input, review); `state.py` (in-process `_active_runs`/`_app_locks`); `gh_cache/` (read-through TTL cache for the dashboard's GitHub-backed reads: in-process dict over a durable DynamoDB/local-JSON store, stale-on-error); `human_gate/` (`maybe_raise` / `sweep_recent_runs` — raises need_human + Slack for runs that end carrying `HUMAN_INPUT_REQUIRED`); `utils.py`.
- `agentra/registry/` — multi-app registry + durable work queue. `core.py` (apps, pause, llm-backend, `RepoSpec` resolution, DynamoDB init, Slack-thread map), `jobs.py` (`cycle|promote|prod_debug|human_resume` queue the loop drains), `runs.py`, `loops.py`, `inbox.py` (request submit/dispatch), `scheduler/` (read-only `compute_schedule_status` / `ScheduleStatus` — when an app is next due for a scheduled cycle), `_dynamo.py`, `_cache.py`. The module object is replaced by a proxy that delegates `_DELEGATED_NAMES` to `core`.
- `agentra/memory/` — per-repo product state. `Memory` = 5 mixins (issues, issue_lifecycle, features, settings, specs); GitHub Issues/Projects-backed. `core.py` holds label constants (incl. the `status:awaiting-testing` label and its legacy `status:shipped` alias) + failure-triage regexes (transient / unfixable-by-agentra / login-required) + spec-header helpers + `pipeline_stages()` (the dashboard's single ordered list of pipeline columns).
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
- A run in a terminal status (`completed|failed|blocked|waiting_for_human`) whose `error` or `summary` contains `HUMAN_INPUT_REQUIRED` must never end silently (agentra#55): `human_gate.maybe_raise(run_key)` runs after every successful `record_run` RPC and `human_gate.sweep_recent_runs()` backstops it on each `/trigger/cron` tick (result returned as `human_gates`). It is idempotent per `gate_run_key` (a recorded Slack thread proves the announce happened) and never raises.
- A successful memory-mutation RPC busts that app's dashboard cache: methods in `_MEMORY_MUTATION_METHODS` call `gh_cache.invalidate_app` for the app owning the coordination `repo_url`.
- A parent feature issue with open sub-issues is never marked code-complete: `record_code_complete` checks `github_issues.open_sub_issue_count` (GraphQL `subIssuesSummary`) and reports `blocked_by_open_sub_issues` instead (agentra#38).
- Human-answer discovery (`find_unanswered_human_input_comment`) anchors to the MOST RECENT `_HUMAN_INPUT_MARKER` comment only, so an already-consumed answer can't satisfy later escalations.
- `record_failure` dedups via `_find_similar_open_bug` for both auth and generic-unfixable failures, then escalates through `_escalate_blocking_failure` (thread-mapped Slack notify + human-input context + loop `waiting_for_human`) so replies route and the dashboard's "Needs your input" tab shows the block (agentra#42/#46/#47).

## Conventions
- Every registry/memory storage function branches `if core._ddb is not None: <dynamo> else: <local JSON>`; new state follows the same dual-path shape with a local fallback for tests/dev.
- `registry` sub-modules re-export through `registry/__init__.py`'s `__all__`; when adding one, update the `routes/internal.py` whitelist in the same change if the loop needs it.
- A new `Memory` method that changes GitHub Issues/Projects state and is exposed over RPC must also be added to `_MEMORY_MUTATION_METHODS` in `routes/internal.py`, or the dashboard serves a stale cached view.
- Dashboard GitHub-backed reads go through `gh_cache.cached(key, producer)`; a new per-app cache key must be added to `invalidate_app`.
- Routes are `APIRouter`s included at root prefix in `server/__init__.py`; auth exemptions go in `auth.py::_PUBLIC_PREFIXES`.
- Work-enqueuing endpoints: check `registry.is_paused()` first, dedup with `dedup_key=f"{kind}:{app}"`, record a run via `_new_run_key`, return `{triggered, run_key, job_id, queued}`.
- Schedule-due timing lives in one place: `registry.scheduler.compute_schedule_status(app, repo)` is shared by the `/trigger/scheduled` cron enqueue path and the read-only `GET /apps/{app}/schedule`; don't re-derive `due_in` from `environments.load` + `last_run_at` inline.
- Pipeline stage ordering/display lives in one place too: `memory.core.pipeline_stages()`, served read-only via `GET /pipeline/stages` for the dashboard — don't hardcode stage names/order elsewhere.
- Per-file 500-line limit + SRP + a subfolder per domain (`CLAUDE.md`): split into mixins/packages rather than growing a file. Single-sentence docstrings, no dead code, no comment paragraphs.
- A feature that needs a repo checkout or Claude is held with HTTP 503 and a "moving to agentra-loop" message, not partially implemented (`chat.py`, `standup.py`).
- `_json_safe` coerces dataclasses/`Path` for anything returned over RPC.
- Any orchestrator-authored comment posted on a GitHub issue (escalation text, markers, etc.) must have its prefix added to `_INTERNAL_COMMENT_PREFIXES` (`connectors/github_issue_lifecycle.py`) in the same change, or `find_unanswered_human_input_comment` can misread it as the human's answer to itself.

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
- Label membership against a live GitHub issue must go through `_label_names()` — `github_issues.get_issue` returns real REST label objects (`{name: ...}`), not flat strings; only `github_fake` uses flat strings, so a bare `label in issue["labels"]` silently always fails against prod (confirmed live: the escalation-throttle guard reposted the same "Auto-escalated" comment every cycle instead of once, issues #38/#15).
- `list_loops` sorts by `_recency()` (last_run_at, stamped only when `roll_up_loop` finishes a real run, else created_at) not `updated_at`; the DynamoDB `by-app-recency` GSI is keyed on `updated_at`, so `_query_loops_by_app` overfetches (`_RECENCY_OVERFETCH`=200 rows) off the index and re-sorts in Python — an administrative-only write like `set_loop_status` bumps `updated_at` without being real activity, and used to shove a days-old loop to the top of the list alongside one that just ran.
- `gh_cache.cached` is stale-on-error: if the producer raises and any prior value exists (however old) it is served instead, so a GitHub outage looks like frozen data, not a 5xx; only a never-cached key propagates the error. Mutations made outside the RPC/`apps.py` invalidation hooks leave the view stale until the TTL lapses.
- `human_gate` reads `summary` as well as `error`: the loop's orchestrator narrates `HUMAN_INPUT_REQUIRED` in free-text `summary` on runs that end `completed` with no `error`. A `need_human` label alone doesn't mean the ask was announced — only a recorded Slack thread does (confirmed live, #49/#50/#51).
- The `open_sub_issue_count` guard exists only in the real `github_issues` backend and `github_fake`; a code-complete call on a parent with open sub-issues returns success with `blocked_by_open_sub_issues > 0` and does NOT label the parent, so callers must check that field rather than assume the parent advanced.
