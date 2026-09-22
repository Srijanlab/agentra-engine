<!-- owner: agent:codebase -->
<!-- source-sha: b3b818ce3cc00f2d894c9534521b1af05b45920d -->
- Strict Single‑Responsibility Principle across modules.
- Sub‑folder organization for domain concerns.
- 500‑line file cap to enforce SRP.
- Dual‑path storage: DynamoDB if configured, otherwise local JSON.
- RPC whitelisting for internal engine governance.
- Memoized GitHub caching with TTL and invalidation hooks.
- Human‑gate flow on `HUMAN_INPUT_REQUIRED` via Slack thread tracking.
- Test isolation via environment overrides in pytest `conftest.py`.
- Vercel serverless deployment with ASGI entry (`api/index.py`).
