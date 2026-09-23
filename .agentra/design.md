<!-- owner: agent:codebase -->
<!-- source-sha: 097ec93b289ecdf4f67fe41d0ffe04d89724ad23 -->
- Engine = state authority, loop = execution — a hard split. Trigger endpoints only `registry.enqueue_job({cycle|promote|prod_debug|human_resume})`; agentra-loop drains the queue and reports back. The engine carries no `claude-agent-sdk` and no docker/deploy code.
- One RPC contract. `POST /internal/rpc` gated by `AGENTRA_INTERNAL_TOKEN` (+ optional Vercel-header IP allowlist), restricted to the `_REGISTRY_METHODS` / `_MEMORY_METHODS` frozensets, is the entire state surface the loop may touch. Credential-holding side doors are separate token-gated endpoints (`/internal/git-token`, `/internal/slack/message`, `/internal/runs/{id}/log`).
- Dual-path persistence. Every registry/memory write is `if core._ddb: <DynamoDB> else: <local JSON under AGENTRA_HOME>`. DynamoDB (static prefixed `AGENTRA_AWS_*` IAM keys) backs prod; local JSON serves the CLI, tests, and the loop's own process. `cloud_mode()` gates all checkout-dependent behavior.
- Two independent, composable auth gates on the same middleware chain: `verify_token.check_verify_token` runs first (scoped read-only bypass for pre-prod black-box checks), falling through to the Firebase sign-in gate. Both now share one 401 body shape (`verify_token.unauthenticated_body()`) with a `hint` naming both accepted flows, so callers get a consistent, secret-free error regardless of which gate rejected them.
- Durable queue with self-healing: CAS claim on a `by-status` GSI, stale-claim requeue keyed off the MAX heartbeat per app (not per job), native DynamoDB TTL on terminal jobs.
- Module-proxy pattern for `registry`/`memory` so sub-modules can mutate shared `core` state through delegated names.
- Memory modelled as GitHub Issues/Projects composed from 5 mixins; failure triage via regex classes (transient / unfixable / login-required).
- SRP + 500-line file cap + domain subfolders enforced by CLAUDE.md; features needing a checkout return 503 rather than half-working.
- Backward-compatible label rename (GitHub issue #38): `status:shipped` -> `status:awaiting-testing` is a write-forward, read-both migration.
- Single source of truth for pipeline UI shape: `memory.core.pipeline_stages()` defines the dashboard's ordered columns once, exposed read-only at `GET /pipeline/stages`.
- Loop lifecycle self-healing: `_reconcile_closed_issue_loops` releases any loop left active/waiting/escalated whose tracked GitHub issue was closed out-of-band (agentra#20/#25).
- Internal-comment allowlist plus most-recent-marker anchoring so the orchestrator's own comments or an already-consumed answer are never read as a fresh human answer (agentra#38, #54).
- Read-through dashboard cache (`server/gh_cache/`), stale-on-error, invalidated per-app by `_MEMORY_MUTATION_METHODS` RPCs.
- Silent-run gate (`server/human_gate/`, #55): idempotent, two-path (post-`record_run` fast path + `/trigger/cron` sweep backstop) so a run needing a human always gets a Slack thread.
- Multi-part feature guard (#38): parent issue marked code-complete only when `open_sub_issue_count` is 0.
- Failure escalation (#42/#46/#47): auth and unfixable failures share `_escalate_blocking_failure`, deduped against similar open bugs.
