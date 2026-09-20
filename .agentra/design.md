<!-- owner: agent:codebase -->
<!-- source-sha: 3b510085b9233eb46647a0adf4147b90d569a2dd -->
- Engine = state authority, loop = execution — a hard split. Trigger endpoints only `registry.enqueue_job({cycle|promote|prod_debug|human_resume})`; agentra-loop drains the queue and reports back. The engine carries no `claude-agent-sdk` and no docker/deploy code.
- One RPC contract. `POST /internal/rpc` gated by `AGENTRA_INTERNAL_TOKEN` (+ optional Vercel-header IP allowlist), restricted to the `_REGISTRY_METHODS` / `_MEMORY_METHODS` frozensets, is the entire state surface the loop may touch. Credential-holding side doors are separate token-gated endpoints (`/internal/git-token`, `/internal/slack/message`, `/internal/runs/{id}/log`).
- Dual-path persistence. Every registry/memory write is `if core._ddb: <DynamoDB> else: <local JSON under AGENTRA_HOME>`. DynamoDB (static prefixed `AGENTRA_AWS_*` IAM keys) backs prod; local JSON serves the CLI, tests, and the loop's own process. `cloud_mode()` gates all checkout-dependent behavior.
- Durable queue with self-healing: CAS claim on a `by-status` GSI, stale-claim requeue after 1h, native DynamoDB TTL on terminal jobs.
- Module-proxy pattern for `registry`/`memory` so sub-modules can mutate shared `core` state through delegated names.
- Memory modelled as GitHub Issues/Projects composed from 5 mixins; failure triage via regex classes (transient / unfixable / login-required).
- SRP + 500-line file cap + domain subfolders enforced by CLAUDE.md; features needing a checkout return 503 rather than half-working.
- Backward-compatible label rename (#38): write-forward, read-both migration of `status:shipped` -> `status:awaiting-testing`, plus an idempotent one-time `migrate_awaiting_testing_label`.
- Single source of truth for pipeline UI shape: `memory.core.pipeline_stages()`, served at `GET /pipeline/stages`.
- Loop lifecycle self-healing: `_reconcile_closed_issue_loops` releases loops whose tracked issue was closed out-of-band (agentra#20/#25).
- Internal-comment allowlist: every orchestrator-authored comment prefix is registered in `_INTERNAL_COMMENT_PREFIXES`; human-answer detection anchors to the most recent marker so an old consumed answer can't satisfy a later escalation (agentra#38/#54).
- Label comparisons against a live GitHub issue always go through `_label_names()` (REST returns `{name: ...}` objects; `github_fake` uses flat strings).
- Loop recency is a derived real-activity-only sort key (`_recency()`), distinct from `updated_at`; the `by-app-recency` GSI is only an overfetch source, re-sorted in Python.
- Read-through dashboard cache (`server/gh_cache/`): in-process dict over a durable store (`gh-cache` DynamoDB table with native TTL in cloud mode, local JSON otherwise), stale-on-error so a GitHub blip never 5xxs the dashboard, optional etag via `FetchResult`; RPC memory mutations invalidate per-app keys (`_MEMORY_MUTATION_METHODS` -> `invalidate_app`).
- Silent-run human gate (`server/human_gate/`, #55): `maybe_raise` on every `record_run` RPC (fast path) plus a `/trigger/cron` sweep (backstop); idempotent per run via `gate_run_key`, best-effort and never raising, so a Slack/GitHub blip is retried instead of failing the write.
- Multi-part feature guard (#38): the parent is only marked code-complete when `open_sub_issue_count` is 0; otherwise `record_code_complete` returns `blocked_by_open_sub_issues`.
- Unified blocking-failure escalation (#42/#46/#47): auth and generic-unfixable failures share `_escalate_blocking_failure` (thread-mapped Slack, human-input context, loop `waiting_for_human`) and are deduped against similar open bugs.
