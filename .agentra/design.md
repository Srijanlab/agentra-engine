<!-- owner: agent:codebase -->
<!-- source-sha: ae1786ea48fd970ceb9553558c925896b0003d9e -->
- Engine = state authority, loop = execution — a hard split. Trigger endpoints only `registry.enqueue_job({cycle|promote|prod_debug|human_resume})`; agentra-loop drains the queue and reports back. The engine carries no `claude-agent-sdk` and no docker/deploy code.
- One RPC contract. `POST /internal/rpc` gated by `AGENTRA_INTERNAL_TOKEN` (+ optional Vercel-header IP allowlist), restricted to the `_REGISTRY_METHODS` / `_MEMORY_METHODS` frozensets, is the entire state surface the loop may touch. Credential-holding side doors are separate token-gated endpoints (`/internal/git-token`, `/internal/slack/message`, `/internal/runs/{id}/log`).
- Dual-path persistence. Every registry/memory write is `if core._ddb: <DynamoDB> else: <local JSON under AGENTRA_HOME>`. `cloud_mode()` gates all checkout-dependent behavior.
- Durable queue with self-healing: CAS claim on a `by-status` GSI, per-job heartbeat staleness requeue (poison-fail after max attempts), native DynamoDB TTL on terminal jobs.
- Windowed-read hardening: app-scoped run/loop lookups page the `by-app-recency` GSI with server-side filters until enough matches are found, and stale-run reconciliation adds every queued/running run per app to the global recent window, so old orphans aren't missed.
- Operator audit trail: `server/audit.py` resolves an actor (user email / verify-token / loop / anonymous) and mutating operator routes log it into the durable signals feed via `audit_log` / `_server_log(..., actor=)`; the signals store is a bounded ring appended atomically with `list_append` and conditionally trimmed.
- App config application is factored into `routes/app_config.py::_apply_app_config`, shared by register and update, keeping `apps.py` under the file-size cap.
- Unified 401 shape (issue #83): Firebase, verify-token and `/trigger/queue` gates all return `{detail, error: "authentication_required", hint}` via `verify_token.unauthenticated_body()`; the queue gate raises `QueueAuthError` handled by an app-level handler rather than `HTTPException`.
- Module-proxy pattern for `registry`/`memory` so sub-modules can mutate shared `core` state through delegated names.
- Memory modelled as GitHub Issues/Projects composed from 5 mixins; failure triage via regex classes (transient / unfixable / login-required).
- SRP + 500-line file cap + domain subfolders enforced by CLAUDE.md; features needing a checkout return 503 rather than half-working.
