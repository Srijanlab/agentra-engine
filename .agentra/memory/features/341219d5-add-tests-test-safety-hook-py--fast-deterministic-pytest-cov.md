Implementation complete. Summary:

**Bug found & fixed** (`agentra/agents/safety.py`): the `DELETE FROM` entry in `FORBIDDEN_BASH_PATTERNS` was `r"\bDELETE\s+FROM\s+\w+\s*;?\s*$"` — anchored to end-of-string, so a chained command like `psql -c "DELETE FROM users" && echo done` slipped through undetected. Removed the trailing `$`/`;?` anchor logic, replacing it with `r"\bDELETE\s+FROM\s+\w+\b"`, which matches regardless of trailing shell content. Verified with a raw regex check that the old pattern truly missed the repro case and the new one catches it.

**Sibling audit**: checked every other entry in `FORBIDDEN_BASH_PATTERNS` and `PROD_ONLY_BASH_PATTERNS` for the same end-of-string-anchoring assumption. None of the others were anchored that way (`rm -rf /` uses `(?:\s|$)` intentionally to allow either a following token or true EOF, which is correct as-is). Added a doc comment above both lists plus regression tests (`test_sibling_forbidden_patterns_are_not_end_anchored`, `test_prod_only_patterns_are_not_end_anchored`) pinning that none of them regress into the same bug later.

**New test file** `tests/test_safety_hook.py` (54 tests, all passing): calls `make_pre_tool_use_hook`'s returned async hook directly with synthetic `tool_name`/`tool_input` dicts — no API calls, no SDK dispatch, ~2.5s total. Covers every entry in all three pattern lists (with a length-parity guard against list drift), the exact chained-command bypass regression, `allow_prod` gating semantics for prod-only patterns, benign-command/path false-positive checks, and the "unconditional forbidden list wins over allow_prod=True" precedence case. Left `tests/test_safety_integration.py` completely untouched, per the requirement.

Also added `pytest` as an optional `dev` dependency in `pyproject.toml` since this is the first pytest-based test in the repo (existing tests use plain `unittest`/hand-rolled `asyncio.run` mains).

Ran `compileall` and the existing `test_brain_stagnation.py` unittest suite too — both still green, nothing else disturbed. Committed on the current branch (`dev/341219d5-...`); did not push or open a PR, per my instructions.

```json
{
  "feature": "Add tests/test_safety_hook.py (fast deterministic pytest coverage for make_pre_tool_use_hook) and fix the DELETE FROM end-of-string-anchoring bypass",
  "status": "implemented",
  "files_changed": [
    "tests/test_safety_hook.py",
    "agentra/agents/safety.py",
    "pyproject.toml"
  ],
  "self_test_result": "pass",
  "notes": "54/54 new tests pass in ~2.5s. Confirmed the fix is real by checking the old vs new pattern against the exact bypass command from the bug report. Audited PROD_ONLY_BASH_PATTERNS and the rest of FORBIDDEN_BASH_PATTERNS for the same anchoring bug -- none found, but pinned with regression tests anyway. Added pytest as an optional 'dev' dependency (repo had no pytest usage before). Left tests/test_safety_integration.py untouched. Committed locally on the existing dev branch; did not push/open PR per operating constraints."
}
```