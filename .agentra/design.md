<!-- owner: agent:codebase -->
<!-- source-sha: 4cf85dec21a9ceb0871136696a0aa84f3c19a29c -->
- Engine = state authority, loop = execution — a hard split. Trigger endpoints only `registry.enqueue_job({cycle|promote|prod_debug|human_resume})`; agentra-loop drains the queue and reports back. The engine carries no `claude-agent-sdk` and no docker/deploy code.
- One RPC contract. `POST /internal/rpc` gated by `AGENTRA_INTERNAL_TOKEN` (+ optional Vercel-header IP allowlist), restricted to the `_REGISTRY_METHODS` / `_MEMORY_METHODS` frozensets, is the entire state surface the loop may touch. Credential-holding side doors are separate token-gated endpoints (`/internal/git-token`, `/internal/slack/message`, `/internal/runs/{id}/log`).
- Dual-path persistence. Every registry/memory write is `if core._ddb: <DynamoDB> else: <local JSON under AGENTRA_HOME>`. DynamoDB (static prefixed `AGENTRA_AWS_*` IAM keys) backs prod; local JSON serves the CLI, tests, and the loop's own process. `cloud_mode()` gates all checkout-dependent behavior.
- Durable queue on DynamoDB: CAS `pending -> claimed` on a `by-status` GSI, stale-claim re-queue after 1h, `expires_at` TTL on terminal jobs, `dedup_key` per `kind:app`.
- Registry/memory exposed as import-time module proxies delegating a fixed name set to `core`; `Memory` composed from feature mixins; routes are root-prefixed `APIRouter`s with an explicit public-path allowlist.
- Health endpoints are catch-all safe and echo the deployed commit so the loop can gate pre-prod promotion on deploy convergence.
