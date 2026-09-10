<!-- owner: agent:codebase -->
<!-- source-sha: 4cf85dec21a9ceb0871136696a0aa84f3c19a29c -->
- Engine = state authority, loop = execution — a hard split. Trigger endpoints only `registry.enqueue_job({cycle|promote|prod_debug|human_resume})`; agentra-loop drains the queue and reports back. The engine carries no `claude-agent-sdk` and no docker/deploy code.
- One RPC contract. `POST /internal/rpc` gated by `AGENTRA_INTERNAL_TOKEN` (+ optional Vercel-header IP allowlist), restricted to the `_REGISTRY_METHODS` / `_MEMORY_METHODS` frozensets, is the entire state surface the loop may touch. Credential-holding side doors are separate token-gated endpoints (`/internal/git-token`, `/internal/slack/message`, `/internal/runs/{id}/log`).
- Dual-path persistence. Every registry/memory write is `if core._ddb: <DynamoDB> else: <local JSON under AGENTRA_HOME>`. DynamoDB (static prefixed `AGENTRA_AWS_*` IAM keys) backs prod; local JSON serves the CLI, tests, and the loop's own process. `cloud_mode()` gates all checkout-dependent behavior.
- Registry/memory as swapped module proxies delegating `_DELEGATED_NAMES` to `core`, so tests can set `_ddb` through the package name.
- Durable queue with CAS claim on the `by-status` GSI, stale-`claimed` re-queue after 1h, DynamoDB TTL on terminal jobs.
- Memory modeled as GitHub Issues/Projects state via 5 focused mixins; failure triage is regex classification (transient / unfixable / login-required).
- Per-file 500-line + SRP + domain-subfolder discipline: features split into mixins/packages (server/routes, registry sub-modules, memory mixins).
- Health endpoints are failure-proof and expose deployed commit SHA for the loop's pre-prod verification.
