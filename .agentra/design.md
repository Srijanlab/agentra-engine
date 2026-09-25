<!-- owner: agent:codebase -->
<!-- source-sha: ae1786ea48fd970ceb9553558c925896b0003d9e -->
- Engine = state authority, loop = execution: trigger endpoints only enqueue jobs (`cycle|promote|prod_debug|human_resume`); no claude-agent-sdk or deploy code here.
- One RPC contract: `POST /internal/rpc` gated by `AGENTRA_INTERNAL_TOKEN`, limited to `_REGISTRY_METHODS`/`_MEMORY_METHODS` whitelists; credential-holding side doors are separate token-gated endpoints.
- Dual-path persistence: every registry/memory write is DynamoDB when `core._ddb` is set, else local JSON under `AGENTRA_HOME`; `cloud_mode()` gates checkout-dependent behavior.
- Durable queue with self-healing: CAS claim on the `by-status` GSI, per-job heartbeat staleness requeue (poison-fail after max attempts), DynamoDB TTL on terminal jobs.
- Window-safe run/loop reads: `list_app_runs` paginates with `LastEvaluatedKey` past the server-side filter; stale-run reconciliation covers the global recent window plus every queued/running run per app, so old orphans are not missed.
- Scheduling: one `compute_schedule_status` serves both the cron enqueue path and `GET /apps/{app}/schedule` (fixed-cadence or continuous mode).
- Operator audit trail: `server/audit.py` resolves the caller (`user_email` / `verify-token` / `loop` / `anonymous`) and `record_signal` stores an optional `actor` on each capped signal event; operator routes (pause/resume, llm-backend/pool, app register/update/remove, run/promote, human-input) log through it.
- App config application split out of `apps.py` into `routes/app_config.py::_apply_app_config` (objective + `EnvironmentConfig` + optional commit/push of `.agentra/`), shared by register, register-from-URL, and update.
- Signals feed: `registry/signals.py` appends atomically with `list_append` then conditionally trims to the newest 200; `_server_log` swallows persist errors so logging never raises.
- Unified 401 shape (#83) across the Firebase, verify-token and `/trigger/queue` gates via `verify_token.unauthenticated_body()`; the queue gate raises `QueueAuthError` handled at app level.
- Module-proxy pattern for `registry`/`memory`; memory modelled as GitHub Issues/Projects composed from 5 mixins; SRP + 500-line cap + domain subfolders per CLAUDE.md.
