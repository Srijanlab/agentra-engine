<!-- owner: agent:codebase -->
<!-- source-sha: ae1786ea48fd970ceb9553558c925896b0003d9e -->
- Engine = state authority, loop = execution — a hard split. Trigger endpoints only `registry.enqueue_job({cycle|promote|prod_debug|human_resume})`; agentra-loop drains the queue and reports back. The engine carries no `claude-agent-sdk` and no docker/deploy code.
- One RPC contract. `POST /internal/rpc` gated by `AGENTRA_INTERNAL_TOKEN` (+ optional Vercel-header IP allowlist), restricted to the `_REGISTRY_METHODS` / `_MEMORY_METHODS` frozensets, is the entire state surface the loop may touch. Credential-holding side doors are separate token-gated endpoints (`/internal/git-token`, `/internal/slack/message`, `/internal/runs/{id}/log`).
- Dual-path persistence. Every registry/memory write is `if core._ddb: <DynamoDB> else: <local JSON under AGENTRA_HOME>`; `cloud_mode()` gates all checkout-dependent behavior.
- Durable queue with self-healing: CAS claim on a `by-status` GSI, per-job heartbeat staleness requeue (poison-fail after max attempts), native DynamoDB TTL on terminal jobs.
- Windowless reconciliation: stale-run reaping unions the global recent window with every `queued|running` run per app (`list_app_runs` with `statuses`/`loop_id` filters and `limit=None` pagination), and `list_loops_by_status` follows the whole GSI partition, so old stuck work isn't missed when the recent-N window fills with newer runs.
- Operator audit trail: `server/audit.py` (`actor_for`/`audit_log`) tags signals with the caller (Firebase email, `verify-token`, `loop`, or `anonymous`); `_server_log`/`record_signal` accept an optional `actor` that `GET /signals` surfaces.
- App config persistence is factored out of the apps route into `routes/app_config.py::_apply_app_config` (objective + `EnvironmentConfig` save, then commit/push `.agentra/` off cloud mode, returning a warning string instead of raising).
- Module-proxy pattern for `registry`/`memory` so sub-modules can mutate shared `core` state through delegated names.
- Memory modelled as GitHub Issues/Projects composed from 5 mixins; failure triage via regex classes (transient / unfixable / login-required).
- SRP + 500-line file cap + domain subfolders enforced by CLAUDE.md; features needing a checkout return 503 rather than half-working.
- Silent-run gate (`server/human_gate/`, #55): a two-path, idempotent mechanism — a fast path right after each `record_run` RPC plus a `/trigger/cron` sweep backstop.
- Multi-part feature guard (#38): the parent issue is only marked code-complete when `open_sub_issue_count` is 0.
- Failure escalation (#42/#46/#47): auth and unfixable failures share `_escalate_blocking_failure`, deduped against similar open bugs.
- Unified 401 shape (issue #83): Firebase gate, verify-token gate and `/trigger/queue` gate all return `{detail, error: "authentication_required", hint}` via `verify_token.unauthenticated_body()`; the queue gate raises `QueueAuthError` handled by an app-level exception handler rather than `HTTPException`.
- Durable signals feed: `GET /signals` is backed by `registry/signals.py` (single DynamoDB `system` item `key="signals"` appended atomically via `list_append` + conditional trim, or local `signals.json`), a bounded ring of the newest 200 events.
