<!-- owner: agent:codebase -->
<!-- source-sha: ae1786ea48fd970ceb9553558c925896b0003d9e -->
Additions since the last design.md build (merge into the existing list; the full current file was not visible to this run):
- Operator audit trail: `server/audit.py` (`actor_for`/`audit_log`) resolves the caller (Firebase email, `verify-token`, `loop` for /internal, else `anonymous`) and threads it through `_server_log` -> `registry.record_signal(..., actor=...)`, so the signals feed attributes pause/resume, llm config, app register/update/remove, run/promote and human-answer actions.
- Signals store hardened: DynamoDB append is an atomic `list_append` `update_item` plus a conditional-`REMOVE` trim (bounded retries), replacing a get/put read-modify-write; `_server_log` never raises on persist failure.
- Windowless run/loop queries: `list_app_runs` and `list_loops_by_status` page through DynamoDB `LastEvaluatedKey` (filters apply post-limit), and `reconcile_stale_runs` unions the newest global window with every queued/running run per registered app, so stale detection is not starved by busy neighbours.
- App config persistence extracted to `server/routes/app_config.py::_apply_app_config` (objective + `EnvironmentConfig` save, then commit/push `.agentra/` off cloud mode, returning a warning string rather than raising) to keep `apps.py` under the 500-line cap.
- Continuous scheduling derives due time from the newest activity across the last 50 cycle runs (`scheduled` and `on-demand` sources) and treats any pending/claimed cycle job or queued/running run as in flight.
