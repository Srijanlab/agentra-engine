<!-- owner: agent:codebase -->
<!-- source-sha: ae1786ea48fd970ceb9553558c925896b0003d9e -->
- Engine = state authority, loop = execution — a hard split. Trigger endpoints only `registry.enqueue_job({cycle|promote|prod_debug|human_resume})`; agentra-loop drains the queue and reports back. The engine carries no `claude-agent-sdk` and no docker/deploy code.
- One RPC contract: `POST /internal/rpc` gated by `AGENTRA_INTERNAL_TOKEN`, restricted to the `_REGISTRY_METHODS` / `_MEMORY_METHODS` frozensets; credential-holding side doors (`/internal/git-token`, `/internal/slack/message`) are separate token-gated endpoints.
- Dual-path persistence: every registry/memory write is `if core._ddb: <DynamoDB> else: <local JSON under AGENTRA_HOME>`; `cloud_mode()` gates checkout-dependent behavior.
- Durable queue with self-healing: CAS claim on a `by-status` GSI, per-job heartbeat staleness requeue (poison-fail after max attempts), native DynamoDB TTL on terminal jobs.
- Stale-run/loop reconciliation reads are deliberately unbounded (global newest-200 window unioned with every queued/running run per app; `list_loops_by_status` never truncated) so old orphans can't hide behind newer activity.
- Module-proxy pattern for `registry`/`memory` so sub-modules can mutate shared `core` state through delegated names.
- Memory modelled as GitHub Issues/Projects composed from 5 mixins; failure triage via regex classes (transient / unfixable / login-required).
- Unified 401 shape (issue #83): Firebase, verify-token and `/trigger/queue` gates all return `{detail, error: "authentication_required", hint}` via `verify_token.unauthenticated_body()`; the queue gate raises `QueueAuthError` handled by an app-level exception handler rather than `HTTPException`.
- Durable signals feed: `GET /signals` is backed by `registry/signals.py` (single DynamoDB `system` item or local `signals.json`, atomic `list_append` + conditional trim to the newest 200). `_server_log` persists to it best-effort and never raises.
- Operator audit trail: `server/audit.py` (`actor_for` / `audit_log`) resolves the caller (Firebase email, `verify-token`, `loop`, or `anonymous`) and threads it into signals as an optional `actor`, so the feed records who paused, registered, promoted, or answered.
- App-config application is split out of the apps route into `routes/app_config.py::_apply_app_config` (objective + `EnvironmentConfig` fields saved to the coordination repo, committed/pushed off cloud mode), shared by register, register-from-repos, and update.
- Silent-run gate (`server/human_gate/`, #55): fast path after each `record_run` RPC plus a `/trigger/cron` sweep backstop, idempotent per run.
- Multi-part feature guard (#38): a parent issue is only marked code-complete when `open_sub_issue_count` is 0.
- Failure escalation (#42/#46/#47): auth and unfixable failures share `_escalate_blocking_failure`, deduped against similar open bugs.
- SRP + 500-line file cap + domain subfolders enforced by CLAUDE.md; features needing a checkout return 503 rather than half-working.
