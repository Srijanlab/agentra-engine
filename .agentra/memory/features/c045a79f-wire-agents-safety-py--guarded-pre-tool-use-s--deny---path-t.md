Committed cleanly on the correct branch. Summary of what was implemented:

- **`agentra/agents/base.py`**: added `current_run_logger()`, a public accessor for the existing `_RUN_LOGGER` ContextVar, so other modules can read the ambient logger set by `run_log_scope` without new plumbing.
- **`agentra/memory.py`**: added `format_safety_denial_line()` (module-level, builds a `[safety]`-tagged line with tool name, matched pattern, and truncated detail) and `Memory.record_safety_denial()` (writes that line via the existing `self.log()` channel) for any caller holding a `Memory` instance directly.
- **`agentra/agents/safety.py`**: `_deny()` now calls `_record_denial()`, which lazily imports `current_run_logger` from `base.py` (lazy to avoid a circular import, since `base.py` imports `make_hooks` from `safety.py` at module scope) and, if a `run_log_scope` is active, writes the formatted audit line through it. All three deny call sites (forbidden bash, prod-only bash, forbidden edit path) now pass `tool_name`/`pattern`/`detail`.
- **`tests/test_safety_hook.py`**: added regression tests covering audit logging for Bash/prod/Edit denials, confirming allowed calls log nothing, detail truncation, no-crash when no `run_log_scope` is active, and an end-to-end test that writes through a real `Memory` instance and asserts the line lands in the on-disk run log file (mirroring `orchestrator.py`'s real wiring).

All tests pass (61/61 in the safety test file, 111/112 overall — the one failure, `test_alarm_trigger_respects_per_app_alarm_enabled`, is pre-existing/unrelated, confirmed via `git stash`).

```json
{
  "feature": "Wire agents/safety.py::guarded_pre_tool_use's _deny() path to write a durable audit record on every blocked tool call, via base.py's run_log_scope ContextVar",
  "status": "implemented",
  "files_changed": [
    "agentra/agents/base.py",
    "agentra/agents/safety.py",
    "agentra/memory.py",
    "tests/test_safety_hook.py"
  ],
  "self_test_result": "pass",
  "notes": "Added base.current_run_logger() as a public accessor for the existing _RUN_LOGGER ContextVar; safety.py's _deny() now writes a '[safety]' audit line (tool, matched pattern, truncated command/path) through the ambient run logger via a lazy import (avoids a circular import with base.py, which imports safety.py at module scope). Added Memory.record_safety_denial/format_safety_denial_line so the line format is defined once and reusable by anyone holding a Memory instance. Regression tests cover Bash/prod/Edit denials, no-log-on-allow, truncation, safe no-op with no active run_log_scope, and an end-to-end write to a real on-disk Memory log file. One pre-existing, unrelated test failure (test_alarm_trigger_respects_per_app_alarm_enabled, an auth/401 issue) confirmed present before this change via git stash."
}
```