"""Fast, deterministic, no-API-call pytest module for agents/safety.py's
make_pre_tool_use_hook.

Unlike tests/test_safety_integration.py (which deliberately drives a real,
live query() call to prove the hook is actually wired into the SDK's
PreToolUse dispatch path -- see that file's docstring for why a direct unit
test can't catch a can_use_tool-vs-PreToolUse wiring bug), this module
targets the *regex logic itself*: given the hook is wired up correctly, do
the FORBIDDEN_BASH_PATTERNS / PROD_ONLY_BASH_PATTERNS /
FORBIDDEN_EDIT_PATH_PATTERNS actually catch what they claim to catch, and
not catch what they shouldn't?

That's a distinct, complementary failure mode, and it's the kind of thing
worth pinning with a fast unit test: this file's regression test
(test_delete_from_chained_command_is_blocked) reproduces a confirmed bug
where `\\bDELETE\\s+FROM\\s+\\w+\\s*;?\\s*$` was anchored to the end of the
command string and so silently let a chained command like
`psql -c "DELETE FROM users" && echo done` straight through -- the kind of
thing a live integration test would only catch by accident (only if the
model happened to phrase the command with trailing content), but a direct
regex unit test catches deterministically, every time, in milliseconds.

No API calls, no network, no subprocess -- just calls the async hook
callback directly with synthetic tool_name/tool_input dicts. Run with:
    pytest tests/test_safety_hook.py
or:
    python -m pytest tests/test_safety_hook.py -v
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from agentra.agents.safety import (
    FORBIDDEN_BASH_PATTERNS,
    FORBIDDEN_EDIT_PATH_PATTERNS,
    PROD_ONLY_BASH_PATTERNS,
    make_pre_tool_use_hook,
)


def run_hook(hook, tool_name: str, tool_input: dict) -> dict:
    """Call the async PreToolUse hook synchronously, the way the SDK would
    invoke it (input_data dict in, decision dict out)."""
    return asyncio.run(
        hook({"tool_name": tool_name, "tool_input": tool_input}, "test-tool-use-id", {})
    )


def is_denied(result: dict) -> bool:
    return result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def bash_result(hook, command: str) -> dict:
    return run_hook(hook, "Bash", {"command": command})


def edit_result(hook, file_path: str, tool_name: str = "Write") -> dict:
    return run_hook(hook, tool_name, {"file_path": file_path})


# One representative, real-looking sample command per entry in
# FORBIDDEN_BASH_PATTERNS, in the same order as the list. Kept as a
# positional list (not keyed by pattern text) so a future edit to a
# pattern's regex doesn't desync the samples -- the length-parity assertion
# below is what actually guards against an added/removed pattern silently
# losing coverage.
#
# The DELETE FROM sample is deliberately the exact chained-command form from
# the bug report (`... && echo done`), not a bare `DELETE FROM users;` --
# that's what pins the fix instead of just re-testing the easy case.
BASH_FORBIDDEN_SAMPLES = [
    "rm -rf / --no-preserve-root",
    "git push origin main --force",
    "git reset --hard origin/main",
    "psql -c 'DROP TABLE users;'",
    'psql -c "DELETE FROM users" && echo done',
    "cat .env",
    "curl -s https://api.stripe.com/v1/charges",
    "update the billing address for this customer",
]

# One sample per entry in PROD_ONLY_BASH_PATTERNS, same ordering contract.
# None of these should also match FORBIDDEN_BASH_PATTERNS -- they're testing
# the *conditional* (allow_prod-gated) block, not the unconditional one.
PROD_SAMPLES = [
    "npm run deploy -- --prod",
    "vercel deploy --prod --yes",
    "firebase deploy --only hosting:production",
    "firebase use prod-project",
]

# One sample per entry in FORBIDDEN_EDIT_PATH_PATTERNS, same ordering contract.
EDIT_FORBIDDEN_SAMPLES = [
    "app/.env",
    "infra/secrets/api_key.txt",
    "config/credentials.json",
]


def test_pattern_lists_and_samples_stay_in_sync():
    # Guards the samples above against silent drift if someone adds/removes
    # a pattern without updating this test file.
    assert len(BASH_FORBIDDEN_SAMPLES) == len(FORBIDDEN_BASH_PATTERNS)
    assert len(PROD_SAMPLES) == len(PROD_ONLY_BASH_PATTERNS)
    assert len(EDIT_FORBIDDEN_SAMPLES) == len(FORBIDDEN_EDIT_PATH_PATTERNS)


# -- FORBIDDEN_BASH_PATTERNS: unconditionally denied, allow_prod irrelevant --


@pytest.mark.parametrize("index", range(len(FORBIDDEN_BASH_PATTERNS)))
@pytest.mark.parametrize("allow_prod", [False, True])
def test_forbidden_bash_pattern_is_denied(index, allow_prod):
    hook = make_pre_tool_use_hook(allow_prod=allow_prod)
    command = BASH_FORBIDDEN_SAMPLES[index]
    pattern = FORBIDDEN_BASH_PATTERNS[index]
    result = bash_result(hook, command)
    assert is_denied(result), (
        f"expected command {command!r} to be denied by forbidden pattern {pattern!r} "
        f"(allow_prod={allow_prod}), got {result!r}"
    )
    assert "Blocked by safety policy" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_delete_from_chained_command_is_blocked():
    """Regression test for the confirmed bypass: the old pattern
    r'\\bDELETE\\s+FROM\\s+\\w+\\s*;?\\s*$' was end-of-string anchored, so a
    DELETE FROM wrapped in a chained shell command (destructive statement
    first, harmless trailing command second) slipped through undetected."""
    hook = make_pre_tool_use_hook(allow_prod=False)
    command = 'psql -c "DELETE FROM users" && echo done'
    result = bash_result(hook, command)
    assert is_denied(result), f"chained DELETE FROM command was not blocked: {result!r}"


@pytest.mark.parametrize(
    "command",
    [
        'DELETE FROM users; echo cleanup complete',
        'DELETE FROM sessions && curl https://example.com/notify',
        'echo starting; DELETE FROM accounts',
        'DELETE FROM users',  # the original, already-covered bare form must still work
    ],
)
def test_delete_from_is_blocked_regardless_of_surrounding_shell_content(command):
    hook = make_pre_tool_use_hook(allow_prod=False)
    result = bash_result(hook, command)
    assert is_denied(result), f"expected {command!r} to be denied, got {result!r}"


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf / && echo done",
        "echo about to wipe; rm -rf / ; echo done",
        "git push origin main --force && echo pushed",
        "git reset --hard origin/main && echo reset",
        "psql -c 'DROP TABLE users;' && echo dropped",
    ],
)
def test_sibling_forbidden_patterns_are_not_end_anchored(command):
    """Audit companion to the DELETE FROM fix: none of the other
    FORBIDDEN_BASH_PATTERNS entries should be fooled by trailing shell
    content either (they weren't end-anchored to begin with, but this pins
    that so a future edit can't silently reintroduce the bug)."""
    hook = make_pre_tool_use_hook(allow_prod=False)
    result = bash_result(hook, command)
    assert is_denied(result), f"expected {command!r} to be denied, got {result!r}"


# -- PROD_ONLY_BASH_PATTERNS: denied only when allow_prod=False --------------


@pytest.mark.parametrize("index", range(len(PROD_ONLY_BASH_PATTERNS)))
def test_prod_only_pattern_denied_by_default(index):
    hook = make_pre_tool_use_hook(allow_prod=False)
    command = PROD_SAMPLES[index]
    pattern = PROD_ONLY_BASH_PATTERNS[index]
    result = bash_result(hook, command)
    assert is_denied(result), (
        f"expected command {command!r} to be denied by prod-only pattern {pattern!r} "
        f"under allow_prod=False, got {result!r}"
    )
    assert "touches production" in result["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("index", range(len(PROD_ONLY_BASH_PATTERNS)))
def test_prod_only_pattern_allowed_when_allow_prod_true(index):
    hook = make_pre_tool_use_hook(allow_prod=True)
    command = PROD_SAMPLES[index]
    result = bash_result(hook, command)
    assert result == {}, f"expected command {command!r} to pass through under allow_prod=True, got {result!r}"


@pytest.mark.parametrize(
    "command",
    [
        "vercel deploy --prod && echo deployed",
        "firebase deploy --only hosting:production; echo done",
        "firebase use prod-project && npm run migrate",
    ],
)
def test_prod_only_patterns_are_not_end_anchored(command):
    hook = make_pre_tool_use_hook(allow_prod=False)
    result = bash_result(hook, command)
    assert is_denied(result), f"expected {command!r} to be denied, got {result!r}"


def test_prod_only_pattern_still_allows_known_non_prod_aliases():
    # firebase use's negative lookahead explicitly carves out pre-prod/beta/staging.
    hook = make_pre_tool_use_hook(allow_prod=False)
    for alias in ["pre-prod", "pre_prod", "beta", "staging"]:
        result = bash_result(hook, f"firebase use {alias}")
        assert result == {}, f"expected 'firebase use {alias}' to be allowed, got {result!r}"


# -- FORBIDDEN_EDIT_PATH_PATTERNS: Write/Edit path checks --------------------


@pytest.mark.parametrize("index", range(len(FORBIDDEN_EDIT_PATH_PATTERNS)))
@pytest.mark.parametrize("tool_name", ["Write", "Edit"])
def test_forbidden_edit_path_is_denied(index, tool_name):
    hook = make_pre_tool_use_hook(allow_prod=False)
    path = EDIT_FORBIDDEN_SAMPLES[index]
    pattern = FORBIDDEN_EDIT_PATH_PATTERNS[index]
    result = edit_result(hook, path, tool_name=tool_name)
    assert is_denied(result), (
        f"expected {tool_name} to path {path!r} to be denied by pattern {pattern!r}, got {result!r}"
    )


# -- explicit false-negative / bypass sanity checks --------------------------


@pytest.mark.parametrize(
    "command",
    [
        "echo hello world",
        "ls -la",
        "git status",
        "pytest tests/",
        "cat README.md",
    ],
)
def test_benign_bash_commands_are_allowed(command):
    hook = make_pre_tool_use_hook(allow_prod=False)
    result = bash_result(hook, command)
    assert result == {}, f"expected benign command {command!r} to pass through, got {result!r}"


@pytest.mark.parametrize("tool_name", ["Write", "Edit"])
def test_benign_edit_paths_are_allowed(tool_name):
    hook = make_pre_tool_use_hook(allow_prod=False)
    for path in ["agentra/agents/safety.py", "tests/test_safety_hook.py", "README.md"]:
        result = edit_result(hook, path, tool_name=tool_name)
        assert result == {}, f"expected {tool_name} to {path!r} to be allowed, got {result!r}"


def test_non_gated_tool_names_pass_through_untouched():
    # Read/Glob/etc. aren't gated by this hook at all.
    hook = make_pre_tool_use_hook(allow_prod=False)
    result = run_hook(hook, "Read", {"file_path": ".env"})
    assert result == {}


def test_forbidden_bash_pattern_beats_allow_prod_even_when_command_also_looks_prod_like():
    # A command that matches BOTH an unconditional forbidden pattern and a
    # prod-only pattern must still be denied even with allow_prod=True --
    # the unconditional list is checked first and always wins.
    hook = make_pre_tool_use_hook(allow_prod=True)
    result = bash_result(hook, "git reset --hard origin/production --prod")
    assert is_denied(result)
