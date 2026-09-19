<!-- owner: agent:codebase -->
<!-- source-sha: ca79d75e1feb876387f2edfa7dcd0039e1a14832 -->
- Engine is the sole state/credential authority; the executor (agentra-loop) is stateless and reaches state only via a whitelisted, bearer-token `/internal/rpc` and credential-vending endpoints (`/internal/git-token`, `/internal/slack/message`).
- Trigger endpoints never execute work: they check pause, dedup, record a run, and enqueue a job on a durable DynamoDB-backed queue (CAS claim, stale-claim requeue, TTL on terminal jobs).
- Storage is dual-path everywhere (DynamoDB vs local JSON) keyed on `core._ddb`; `registry` and `memory` are proxy/mixin facades over `core`.
- Per-repo product state lives in GitHub Issues/Projects via `Memory`; the coordination repo is the single home of issues and `.agentra/memory`.
- Status-label renames are made backward-compatible by emitting the new label on write, stripping the old, and matching both on read.
- Dashboard views are read-only projections: `pipeline_stages()` is the single source of stage ordering; the backlog board buckets are not_started / in_progress / code_complete (the awaiting_testing bucket was removed from `/apps/{name}/backlog-board`), and `/apps/{name}/ready-to-review` serves status:tested items with test reports.
- Work that needs Claude or a repo checkout is held with 503 rather than partially implemented.
