<!-- owner: agent:codebase -->
<!-- source-sha: ae1786ea48fd970ceb9553558c925896b0003d9e -->
- Engine = state authority, loop = execution — a hard split. Trigger endpoints only `registry.enqueue_job({cycle|promote|prod_debug|human_resume})`; agentra-loop drains the queue and reports back. The engine carries no `claude-agent-sdk` and no docker/deploy code.
- One RPC contract. `POST /internal/rpc` gated by `AGENTRA_INTERNAL_TOKEN` (+ optional Vercel-header IP allowlist), restricted to the `_REGISTRY_METHODS` / `_MEMORY_METHODS` frozensets, is the entire state surface the loop may touch. Credential-holding side doors are separate token-gated endpoints (`/internal/git-token`, `/internal/slack/message`, `/internal/runs/{id}/log`).
- Dual-path persistence. Every registry/memory write is `if core._ddb: <DynamoDB> else: <local JSON under AGENTRA_HOME>`. `cloud_mode()` gates all checkout-dependent behavior.
- Durable queue with self-healing: CAS claim on a `by-status` GSI, per-job heartbeat staleness requeue (poison-fail after max attempts), native DynamoDB TTL on terminal jobs.
- Stale-run/loop reconciliation queries per-app partitions with server-side filters (following `LastEvaluatedKey`) rather than trusting a fixed recency window, so old orphaned `queued|running` runs and stuck loops are still found.
- Operator audit trail: `server/audit.py` (`actor_for`/`audit_log`) resolves the caller (Firebase email, `verify-token`, `loop`, else `anonymous`) and threads it as `actor` into `_server_log` -> `registry.record_signal`, so the durable signals feed records who performed each mutation. `GET /signals` always returns an `actor` key.
- App-config application (`_apply_app_config`: objective + `EnvironmentConfig` write, optional `.agentra/` commit/push off cloud mode) is extracted into `server/routes/app_config.py`, shared by app register and update routes.
- Module-proxy pattern for `registry`/`memory` so sub-modules can mutate shared `core` state through delegated names.
- Memory modelled as GitHub Issues/Projects composed from 5 mixins; failure triage via regex classes (transient / unfixable / login-required).
- SRP + 500-line file cap + domain subfolders enforced by CLAUDE.md; features needing a checkout return 503 rather than half-working.
- Silent-run gate (`server/human_gate/`, #55): idempotent two-path mechanism (fast path after `record_run` RPC + `/trigger/cron` sweep) so a run asking for a human always gets the `need_human` label, loop human-input state, and a Slack thread.
- Multi-part feature guard (#38): parent issue only marked code-complete when `open_sub_issue_count` is 0.
- Failure escalation (#42/#46/#47): auth and unfixable failures share `_escalate_blocking_failure`, deduped against similar open bugs.
- Unified 401 shape (issue #83): Firebase, verify-token and `/trigger/queue` gates all return `{detail, error: "authentication_required", hint}` via `verify_token.unauthenticated_body()`; the queue gate swaps in its own static hint and raises `QueueAuthError` handled by an app-level handler. The Firebase gate additionally requires `email_verified`.
- Durable signals feed: `GET /signals` is backed by `registry/signals.py` (single DynamoDB `system` item `key="signals"` with atomic `list_append` + conditional trim, or local `signals.json`), a bounded ring of the newest 200 events; `_server_log` never raises on persist failure.
