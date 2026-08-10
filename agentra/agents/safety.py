"""Safety gate shared by every agent that gets Bash/Write/Edit access.

Implements the "Forbidden Actions" list from vision.md section 8: no
production deploys, no destructive data operations, no secrets/billing edits,
no irreversible operations. This is a blunt regex net, not a sandbox — it
exists to catch an agent following bad instructions, not a malicious one.

LAYERED SAFETY MODEL
--------------------
Primary boundary  — Docker container (see CONTAINER.md): the Claude CLI
    subprocess can only reach the bind-mounted target repo and explicitly
    provided env vars; host filesystem, SSH keys, and other secrets are
    invisible to the container entirely.

Secondary boundary — this module: even inside a container, the regex
    patterns below block the most dangerous bash commands and file edits
    (destructive ops, secrets, production deploys) as a defence-in-depth
    measure.

Production access is normally blocked outright. The *only* exception is the
Production Debugging Agent's opt-in auto-remediate path (orchestrator.py),
which passes allow_prod=True for the single promote-to-prod call it makes —
and only for apps that set auto_remediate_prod: true in their environment
config. Every other agent, and every other call, keeps prod blocked.

IMPLEMENTATION NOTE — can_use_tool vs. a PreToolUse hook
---------------------------------------------------------
This used to be a `can_use_tool` callback. That was silently never invoked:
every agent call uses permission_mode="bypassPermissions" (base.py), and the
SDK auto-approves every tool call under that mode *before* can_use_tool is
ever consulted — confirmed via claude_agent_sdk's own
CanUseToolShadowedWarning, which fires exactly in this configuration and
says so explicitly ("can_use_tool will not be invoked... use a PreToolUse
hook instead"). The whole regex gate below was dead code in every real run
this entire session; a callback-level unit test that calls the function
directly can't catch this, since it never exercises the SDK's actual
tool-dispatch path -- only a live query() integration test can (see
tests/test_safety_integration.py). Rebuilt on `hooks={"PreToolUse": [...]}`,
which the SDK does invoke under bypassPermissions.
"""

import re

from claude_agent_sdk import HookMatcher

# These are matched with re.search against the full Bash `command` string, which
# can be an arbitrary shell one-liner (e.g. `cmd1 && cmd2`, `cmd1; cmd2`). Do NOT
# anchor any of these to end-of-string (`$`) unless the trailing content is
# deliberately part of what's being matched (e.g. `(?:\s|$)` below, which allows
# either a following token or true end-of-string) -- a bare trailing `$` lets a
# destructive command slip through the moment it's chained with something
# harmless after it, e.g. `psql -c "DELETE FROM users" && echo done`. See
# tests/test_safety_hook.py for the regression coverage.
FORBIDDEN_BASH_PATTERNS = [
    r"rm\s+-rf\s+/(?:\s|$)",
    r"git\s+push\s+[^\n]*--force",
    r"git\s+reset\s+--hard\s+[^\n]*(main|master|production)",
    r"\bDROP\s+TABLE\b",
    r"\bDELETE\s+FROM\s+\w+\b",
    r"\.env(?:\.\w+)?\b",
    r"stripe\b",
    r"billing",
]

# Only enforced when allow_prod=False (the default for every agent call).
# Same no-end-anchor rule as FORBIDDEN_BASH_PATTERNS above applies here.
PROD_ONLY_BASH_PATTERNS = [
    r"--prod\b",
    r"vercel\s+[^\n]*--prod",
    r"firebase\s+deploy[^\n]*production",
    r"firebase\s+use\s+(?!(pre-prod|pre_prod|beta|staging)\b)\S+",  # only the known non-prod aliases
]

FORBIDDEN_EDIT_PATH_PATTERNS = [
    r"\.env(?:\.\w+)?$",
    r"secrets?/",
    r"credentials",
]


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def make_pre_tool_use_hook(allow_prod: bool = False):
    """Build a PreToolUse hook callback. allow_prod must only be True for the
    single, explicitly-approved prod-promotion call — never as a default."""

    async def guarded_pre_tool_use(input_data, tool_use_id, context) -> dict:
        tool_name = input_data.get("tool_name")
        tool_input = input_data.get("tool_input") or {}

        if tool_name == "Bash":
            command = tool_input.get("command", "")
            for pattern in FORBIDDEN_BASH_PATTERNS:
                if re.search(pattern, command, re.IGNORECASE):
                    return _deny(
                        f"Blocked by safety policy: command matches forbidden "
                        f"pattern for autonomous execution ({pattern!r}). "
                        "Destructive data ops and secrets/billing access are "
                        "never allowed autonomously."
                    )
            if not allow_prod:
                for pattern in PROD_ONLY_BASH_PATTERNS:
                    if re.search(pattern, command, re.IGNORECASE):
                        return _deny(
                            f"Blocked by safety policy: command touches production "
                            f"({pattern!r}). Production changes require the explicit, "
                            "opted-in promote/auto-remediate path, not this agent."
                        )
        if tool_name in ("Write", "Edit"):
            path = str(tool_input.get("file_path", ""))
            for pattern in FORBIDDEN_EDIT_PATH_PATTERNS:
                if re.search(pattern, path, re.IGNORECASE):
                    return _deny(f"Blocked by safety policy: refusing to edit {path!r} autonomously.")
        return {}

    return guarded_pre_tool_use


def make_hooks(allow_prod: bool = False) -> dict[str, list[HookMatcher]]:
    """The hooks= dict to pass into ClaudeAgentOptions. matcher=None applies to
    every tool call, not just a named subset."""
    return {"PreToolUse": [HookMatcher(matcher=None, hooks=[make_pre_tool_use_hook(allow_prod=allow_prod)])]}
