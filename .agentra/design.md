<!-- owner: agent:codebase -->
<!-- source-sha: 1be5e541d9b14492de83a9da2893754844a8eb6f -->
- Engine = state authority, loop = execution — a hard split. Trigger endpoints only `registry.enqueue_job({cycle|promote|prod_debug|human_resume})`; agentra-loop drains the queue and reports back. The engine carries no `claude-agent-sdk` and no docker/deploy code.
- One RPC contract. `POST /internal/rpc` gated by `AGENTRA_INTERNAL_TOKEN` (+ optional Vercel-header IP allowlist), restricted to the `_REGISTRY_METHODS` / `_MEMORY_METHODS` frozensets, is the entire state surface the loop may touch. Credential-holding side doors are separate token-gated endpoints (`/internal/git-token`, `/internal/slack/message`, `/internal/runs/{id}/log`).
- Dual-path persistence. Every registry/memory write is `if core._ddb: <DynamoDB> else: <local JSON under AGENTRA_HOME>`. DynamoDB (static prefixed `AGENTRA_AWS_*` IAM keys) backs prod; local JSON serves the CLI, tests, and the loop's own process. `cloud_mode()` gates all checkout-dependent behavior.
- Durable queue with self-healing: CAS `pending->claimed` on a `by-status` GSI, stale-claim requeue after 1h, `dedup_key` enforcing one open job per app per kind, native DynamoDB TTL (`expires_at`) on terminal jobs.
- State split by ownership. Engine bookkeeping -> DynamoDB / local JSON; a repo's product knowledge -> its GitHub Issues/Projects; deploy-pipeline config -> its GitHub Actions Variables (`environments.py`). No local mirror.
- Multi-repo apps via `RepoSpec` with exactly one `role="coordination"`; `Memory` always binds to the coordination repo.
- Composition for the 500-line rule: `Memory` is 5 mixins; `registry` is a package whose `__init__` swaps in a proxy module delegating selected names to `core`.
- Shared read-model: `registry.scheduler.compute_schedule_status` (frozen `ScheduleStatus`) computes next-due timing once, consumed by both the cron enqueue decision and `GET /apps/{app}/schedule`.
- Deploy-caught-up signal: `/health` echoes the deployed git SHA (`commit`) so the loop can gate `verify_pre_prod` on the pre-prod build reflecting the merge.
- Failure taxonomy is regex-driven in `memory/core.py`: transient/retryable vs. unfixable-by-agentra (auth/permission) vs. login-required.
- Graceful degradation everywhere: `/health` never fails; DynamoDB init returns `None` instead of raising; Langfuse calls are try/except no-ops; `api/index.py` serves a diagnostic fallback app on import failure.
- Held, not half-built: chat-turn and standup generation return 503 pending their move to the loop.
- Layered auth: dashboard = Firebase ID token + `AGENTRA_ALLOWED_EMAILS`; `/internal/*` = internal bearer token; `/trigger/alarm` = Basic-auth password; `/trigger/cron` = internal token or `CRON_SECRET`.
- Polling over webhooks: the loop hits `GET /trigger/cron` (~5 min) to reconcile stale runs, poll `need_human` comments, enqueue due scheduled cycles, and escalate timed-out human-input loops.
