<!-- owner: agent:codebase -->
<!-- source-sha: ae1786ea48fd970ceb9553558c925896b0003d9e -->
- Engine = state authority, loop = execution — a hard split. Trigger endpoints only `registry.enqueue_job({cycle|promote|prod_debug|human_resume})`; agentra-loop drains the queue and reports back. No `claude-agent-sdk`, no docker/deploy code.
- One RPC contract: `POST /internal/rpc` (bearer `AGENTRA_INTERNAL_TOKEN`) restricted to `_REGISTRY_METHODS` / `_MEMORY_METHODS` frozensets; credential-holding side doors (`/internal/git-token`, `/internal/slack/message`) are separate token-gated endpoints.
- Dual-path persistence: every registry/memory write is `if core._ddb: <DynamoDB> else: <local JSON under AGENTRA_HOME>`; `cloud_mode()` gates checkout-dependent behavior.
- Durable queue with self-healing: CAS claim on a `by-status` GSI, per-job heartbeat staleness requeue (poison-fail after max attempts), native DynamoDB TTL on terminal jobs.
- Window-independent reconciliation: stale-run reconcile, loop lookups and schedule status read unbounded (`limit=None`) or explicitly widened sets (all queued/running runs per app on top of the newest-200 global window) so busy apps can't hide in-flight work.
- Operator audit trail: `server/audit.py` (`actor_for` / `audit_log`) resolves the caller (Firebase email, `verify-token`, `loop` for /internal, else `anonymous`) and threads it as `actor` through `_server_log` into the durable signals feed (`registry/signals.py`, atomic `list_append` + conditional trim, capped at 200).
- App config apply step factored into `server/routes/app_config.py::_apply_app_config` (objective + `EnvironmentConfig` write, then commit/push `.agentra/` unless cloud mode), shared by register and update routes.
- Unified 401 shape (issue #83) via `verify_token.unauthenticated_body()` across the Firebase, verify-token and `/trigger/queue` gates; the queue gate raises `QueueAuthError` handled by an app-level handler rather than `HTTPException`.
- Module-proxy pattern for `registry`/`memory`; Memory modelled as GitHub Issues/Projects composed from 5 mixins; failure triage via regex classes.
- SRP + 500-line file cap + domain subfolders enforced by CLAUDE.md; features needing a checkout return 503 rather than half-working.
