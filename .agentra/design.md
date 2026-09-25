<!-- owner: agent:codebase -->
<!-- source-sha: ae1786ea48fd970ceb9553558c925896b0003d9e -->
- Operator-action audit trail: `server/audit.py` resolves the caller once (`actor_for`: Firebase email > `verify-token` > `loop` for `/internal/*` > `anonymous`) and `audit_log` writes it into the durable signals feed; `record_signal`/`_server_log` take an optional `actor`, and `list_signals` always emits an `actor` key (null for non-request events). Shared trigger/dispatch helpers thread `actor` explicitly rather than reading request state.
- App configuration write path is factored into `server/routes/app_config.py::_apply_app_config` (objective + `EnvironmentConfig` save + optional commit/push of `.agentra/`), reused by single-repo register, multi-repo register and update, keeping `apps.py` under the file-size cap.
- Run/loop queries are scoped per app instead of relying on a bounded global window: stale-run reconciliation unions the newest global window with every in-flight run per registered app; `last_run_at`, `get_loop` and the continuous scheduler read the app's own runs; `list_loops_by_status` is never truncated.
- Signals store is append-atomic: DynamoDB `list_append` then a conditional size-guarded trim (bounded retries) replaces the earlier unguarded read-modify-write.
- Queue staleness is judged per job by its own heartbeat, with poison-fail after max attempts.
