<!-- owner: agent:codebase -->
<!-- source-sha: ae1786ea48fd970ceb9553558c925896b0003d9e -->
- Engine = state authority, loop = execution: trigger endpoints only `registry.enqueue_job(...)`; agentra-loop drains the queue and reports back through the single token-gated `POST /internal/rpc` whitelist (`_REGISTRY_METHODS` / `_MEMORY_METHODS`), with credential-holding side doors (`/internal/git-token`, `/internal/slack/message`) kept in the engine.
- Dual-path persistence: every registry/memory write is `if core._ddb: <DynamoDB> else: <local JSON under AGENTRA_HOME>`; `cloud_mode()` gates all checkout-dependent behavior.
- Durable queue with self-healing: CAS claim on a `by-status` GSI, per-job heartbeat staleness requeue (poison-fail after max attempts), native DynamoDB TTL on terminal jobs. Stale-run reconcile covers the newest global window plus every queued/running run per app, and stale-loop reconcile walks every loop stuck at running/queued via unbounded queries rather than a truncated list.
- Module-proxy pattern for `registry`/`memory` so sub-modules can mutate shared `core` state through delegated names.
- Single shared schedule computation (`compute_schedule_status`) for the cron enqueue path and the read-only schedule endpoint; fixed-cadence or continuous mode.
- Unified 401 shape (issue #83) via `verify_token.unauthenticated_body()` across the Firebase, verify-token and `/trigger/queue` gates; the queue gate raises `QueueAuthError` handled by an app-level exception handler.
- Durable signals feed: `GET /signals` is backed by `registry/signals.py` (atomic DynamoDB `list_append` + bounded trim, or local `signals.json`), newest 200 events. `server.utils._server_log` persists to it without ever raising.
- Operator attribution: `server/audit.py` (`actor_for` / `audit_log`) tags signal events with the caller (Firebase email, `verify-token`, `loop`, or `anonymous`); trigger, human-input, systems and app register/update routes pass the actor through.
- App config application is factored out of the route module into `server/routes/app_config.py::_apply_app_config` (objective + `EnvironmentConfig` write, commit `.agentra/` only off cloud mode), shared by register and update.
- Human-gate and escalation paths are idempotent and never raise: `human_gate.maybe_raise` after each `record_run` plus a cron sweep; blocking failures escalate through `_escalate_blocking_failure`, deduped against similar open bugs.
- SRP + 500-line file cap + domain subfolders enforced by CLAUDE.md; features needing a checkout return 503 rather than half-working.
