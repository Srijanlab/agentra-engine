<!-- owner: agent:codebase -->
<!-- source-sha: ae1786ea48fd970ceb9553558c925896b0003d9e -->
- Engine = state authority, loop = execution. Trigger endpoints only `registry.enqueue_job(...)`; agentra-loop drains the queue and reports back over the token-gated `POST /internal/rpc` (whitelisted `_REGISTRY_METHODS` / `_MEMORY_METHODS`). Credential-holding side doors are separate endpoints (`/internal/git-token`, `/internal/slack/message`).
- Dual-path persistence: every registry/memory write is `if core._ddb: <DynamoDB> else: <local JSON under AGENTRA_HOME>`; `cloud_mode()` gates checkout-dependent behavior.
- Durable queue with self-healing: CAS claim on a `by-status` GSI, per-job heartbeat staleness requeue (poison-fail after max attempts), native DynamoDB TTL on terminal jobs.
- Per-app queries instead of global recent-N windows: schedule status, loop run history and stale-run reconciliation read each app's own `by-app-recency` partition (`list_app_runs` with source/status/loop_id filters), so a busy sibling app can't hide an app's runs.
- Operator audit trail: `server/audit.py` resolves an actor (Firebase email, `verify-token`, `loop` for `/internal/*`, else `anonymous`) and `audit_log` writes it onto signals; lower-level helpers accept an `actor` argument and forward it to `_server_log`. Signals are a bounded (200) durable ring, appended with an atomic DynamoDB `list_append` plus a conditional trim.
- App config application is split out of the apps routes (`routes/app_config.py::_apply_app_config`): set objective, merge `EnvironmentConfig` fields, save, and commit/push `.agentra/` only off cloud mode.
- Unified 401 shape: the Firebase, verify-token and `/trigger/queue` gates all return `{detail, error: "authentication_required", hint}` via `verify_token.unauthenticated_body()`; the queue gate raises `QueueAuthError` handled by an app-level handler.
- Hardened auth edges: Firebase gate requires `email_verified` (403 `email_not_verified`); `?access_token=` only for `GET /runs/{id}/logs`; Pub/Sub OIDC needs both audience and service-account email.
- Silent-run gate (`server/human_gate/`, #55) with a fast path after `record_run` plus a cron sweep; human answers enqueue `human_resume` before clearing `need_human`.
- Module-proxy pattern for `registry`/`memory`; Memory is GitHub Issues/Projects composed from 5 mixins with regex-classed failure triage.
- Durable signals feed: `GET /signals` reads `registry/signals.py`, not a log file.
- SRP + 500-line file cap + domain subfolders enforced by CLAUDE.md; features needing a checkout return 503 rather than half-working.
