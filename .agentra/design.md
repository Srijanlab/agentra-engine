<!-- owner: agent:codebase -->
<!-- source-sha: 16d23bed47384db6d5c265be18bec707c233aeb0 -->
• **SRP & Domain‑oriented folder layout** – every feature lives in a dedicated subfolder.
• **File size constraint (≤500 lines)** – prevents cross‑cutting logic; encourages splitting.
• **One‑sentence docstrings** – brief, self‑documenting; no large comments.
• **No dead code** – unused blocks are removed.
• **Memory mixin architecture** (`Memory` = 5 mixins) keeps state logic separate from transport.
• **API router design** – routers under `agentra/server/routes/` wired in `__init__`, auth exemptions listed in `auth.py::_PUBLIC_PREFIXES`.
• **Job enqueuing via `/trigger/*`** – bootstrap via `registry.enqueue_job`, loop consumes via `/internal/rpc`.
• **Infrastructure invariants** – loop never touches DynamoDB/GitHub directly; engine holds secrets; state always persisted via DynamoDB or local JSON.
• **Testing conventions** – `pytest + pytest‑xdist` with `moto[dynamodb]` mocks for DB; test fixtures strip prod env vars.
