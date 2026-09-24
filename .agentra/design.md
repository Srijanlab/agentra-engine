<!-- owner: agent:codebase -->
<!-- source-sha: ae1786ea48fd970ceb9553558c925896b0003d9e -->
- Engine = state authority, loop = execution — a hard split. Trigger endpoints only `registry.enqueue_job({cycle|promote|prod_debug|human_resume})`; agentra-loop drains the queue and reports back. The engine carries no `claude-agent-sdk` and no docker/deploy code.
- One RPC contract: `POST /internal/rpc` gated by `AGENTRA_INTERNAL_TOKEN`, restricted to the `_REGISTRY_METHODS` / `_MEMORY_METHODS` frozensets. Credential-holding side doors are separate token-gated endpoints (`/internal/git-token`, `/internal/slack/message`, `/internal/runs/{id}/log`).
- Dual-path persistence: every registry/memory write is `if core._ddb: <DynamoDB> else: <local JSON under AGENTRA_HOME>`; `cloud_mode()` gates checkout-dependent behavior.
- Durable queue with self-healing: CAS claim on a `by-status` GSI, per-job heartbeat staleness requeue (poison-fail after max attempts), native DynamoDB TTL on terminal jobs.
- Module-proxy pattern for `registry`/`memory` so sub-modules can mutate shared `core` state through delegated names.
- Memory modelled as GitHub Issues/Projects composed from 5 mixins; failure triage via regex classes (transient / unfixable / login-required).
- SRP + 500-line file cap + domain subfolders enforced by CLAUDE.md; features needing a checkout return 503 rather than half-working.
- Unified 401 shape (issue #83): the Firebase, verify-token and `/trigger/queue` gates all return `{detail, error: "authentication_required", hint}` via `verify_token.unauthenticated_body()`; the queue gate swaps in its own static hint and raises `QueueAuthError` handled by an app-level exception handler rather than `HTTPException`.
- Operator audit trail: `server/audit.py` (`actor_for` / `audit_log`) resolves the caller (Firebase email, verify-token actor, `loop` for `/internal`, else `anonymous`) and writes it onto each signal via `_server_log(..., actor=)` -> `registry.record_signal(..., actor=)`; the durable, capped (200) signals feed backing `GET /signals` uses an atomic DynamoDB `list_append` + conditional trim.
- Unbounded-window reads: run/loop reconciliation uses per-app filtered queries (`list_app_runs(statuses=...)`, `list_loops_by_status` following `LastEvaluatedKey`) instead of a fixed global recent-N window, so stuck `queued|running` runs and parked loops are never missed on a busy table.
- App config persistence is isolated in `server/routes/app_config.py::_apply_app_config` (objective + `EnvironmentConfig` written to the coordination repo, commit-pushed only outside cloud mode), shared by app register and update routes.
- Silent-run gate (`server/human_gate/`, #55): idempotent fast path after each `record_run` RPC plus a `/trigger/cron` sweep backstop so a run asking for a human always gets the `need_human` label, loop human-input state, and a Slack thread.
- Multi-part feature guard (#38): a parent issue is only marked code-complete when `open_sub_issue_count` is 0.
- Failure escalation (#42/#46/#47): auth and unfixable failures share `_escalate_blocking_failure`, deduped against similar open bugs.
