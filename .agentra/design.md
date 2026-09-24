<!-- owner: agent:codebase -->
<!-- source-sha: ae1786ea48fd970ceb9553558c925896b0003d9e -->
- Engine = state authority, loop = execution — a hard split. Trigger endpoints only `registry.enqueue_job({cycle|promote|prod_debug|human_resume})`; agentra-loop drains the queue and reports back. The engine carries no `claude-agent-sdk` and no docker/deploy code.
- One RPC contract. `POST /internal/rpc` gated by `AGENTRA_INTERNAL_TOKEN`, restricted to `_REGISTRY_METHODS` / `_MEMORY_METHODS` frozensets, is the entire state surface the loop may touch; credential-holding side doors are separate token-gated endpoints.
- Dual-path persistence. Every registry/memory write is `if core._ddb: <DynamoDB> else: <local JSON under AGENTRA_HOME>`; `cloud_mode()` gates all checkout-dependent behavior.
- Durable queue with self-healing: CAS claim on a `by-status` GSI, per-job heartbeat staleness requeue (poison-fail after max attempts), native DynamoDB TTL on terminal jobs.
- Reconcilers are window-independent: stale-run reconciliation unions the newest global window with every queued/running run per app, loop lookups use untruncated `list_loops_by_status`, and schedule timing reads per-app run queries rather than a global window.
- Operator audit trail: `server/audit.py` resolves an actor (user email / `verify-token` / `loop` / `anonymous`) and mutating operator routes tag their signals with it; `registry.signals` stores an optional `actor` per event via an atomic `list_append` + conditional trim.
- App config application (objective + `EnvironmentConfig` + optional `.agentra/` push) is factored into `routes/app_config._apply_app_config`, shared by register (single- and multi-repo) and update.
- Module-proxy pattern for `registry`/`memory` so sub-modules can mutate shared `core` state through delegated names.
- Unified 401 shape: Firebase, verify-token and `/trigger/queue` gates all return `{detail, error: "authentication_required", hint}` via `verify_token.unauthenticated_body()`; the queue gate raises `QueueAuthError` handled at app level.
- SRP + 500-line file cap + domain subfolders enforced by CLAUDE.md; features needing a checkout return 503 rather than half-working.
