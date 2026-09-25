<!-- owner: agent:codebase -->
<!-- source-sha: ae1786ea48fd970ceb9553558c925896b0003d9e -->
- Engine = state authority, loop = execution — a hard split. Trigger endpoints only `registry.enqueue_job({cycle|promote|prod_debug|human_resume})`; agentra-loop drains the queue and reports back. The engine carries no `claude-agent-sdk` and no docker/deploy code.
- One RPC contract. `POST /internal/rpc` gated by `AGENTRA_INTERNAL_TOKEN` (+ optional Vercel-header IP allowlist), restricted to the `_REGISTRY_METHODS` / `_MEMORY_METHODS` frozensets, is the entire state surface the loop may touch. Credential-holding side doors are separate token-gated endpoints (`/internal/git-token`, `/internal/slack/message`, `/internal/runs/{id}/log`).
- Dual-path persistence. Every registry/memory write is `if core._ddb: <DynamoDB> else: <local JSON under AGENTRA_HOME>`. `cloud_mode()` gates all checkout-dependent behavior.
- Durable queue with self-healing: CAS claim on a `by-status` GSI, per-job heartbeat staleness requeue (poison-fail + human gate after max attempts), native DynamoDB TTL on terminal jobs.
- Full-window reconciliation: stale-run reconcile unions the newest global runs with every `queued|running` run per registered app, and status/in-flight lookups (`list_app_runs(limit=None)`, `list_loops_by_status`, `list_jobs(limit=None)`) paginate to completion instead of trusting a first page.
- Module-proxy pattern for `registry`/`memory` so sub-modules can mutate shared `core` state through delegated names.
- Memory modelled as GitHub Issues/Projects composed from 5 mixins; failure triage via regex classes (transient / unfixable / login-required).
- Operator audit trail: `server/audit.py` (`actor_for`/`audit_log`) tags signals-feed events with the caller (Firebase email, `verify-token`, `loop`, or `anonymous`); `_server_log`/`record_signal` accept an optional `actor` and `list_signals` always returns the key.
- App config persistence is factored into `server/routes/app_config.py` (`_apply_app_config`), shared by register and update, so `apps.py` stays under the 500-line cap.
- Unified 401 shape (issue #83): the Firebase, verify-token and `/trigger/queue` gates all return `{detail, error: "authentication_required", hint}` via `verify_token.unauthenticated_body()`; the queue gate raises `QueueAuthError` handled by an app-level exception handler rather than `HTTPException`.
- Durable signals feed: `GET /signals` is backed by `registry/signals.py` (single DynamoDB `system` item via atomic `list_append` + conditional trim, or local `signals.json`), a bounded ring of the newest 200 events.
- SRP + 500-line file cap + domain subfolders enforced by CLAUDE.md; features needing a checkout return 503 rather than half-working.
