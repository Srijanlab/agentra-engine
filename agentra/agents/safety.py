"""Safety gate shared by every agent that gets Bash/Write/Edit access."""

import re

from claude_agent_sdk import HookMatcher

from agentra.memory import format_safety_denial_line

# These are matched with re.search against the full Bash `command` string, which
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


def _record_denial(tool_name: str, pattern: str, detail: str) -> None:
    """Write a durable audit-trail line for a blocked tool call via the ambient run logger that base.py's run_log_scope sets (the same ContextVar run_agent's own message logging reads from) -- so a denial leaves a trace in the run's log instead of just the deny decision going back to the SDK with zero record anywhere else."""
    from agentra.agents.base import current_run_logger

    logger = current_run_logger()
    if logger is None:
        return  # no active run_log_scope (e.g. a bare unit test) -- nothing to write to
    logger(format_safety_denial_line(tool_name, pattern, detail))


def _deny(reason: str, *, tool_name: str, pattern: str, detail: str) -> dict:
    _record_denial(tool_name, pattern, detail)
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
                        "never allowed autonomously.",
                        tool_name=tool_name,
                        pattern=pattern,
                        detail=command,
                    )
            if not allow_prod:
                for pattern in PROD_ONLY_BASH_PATTERNS:
                    if re.search(pattern, command, re.IGNORECASE):
                        return _deny(
                            f"Blocked by safety policy: command touches production "
                            f"({pattern!r}). Production changes require the explicit, "
                            "opted-in promote/auto-remediate path, not this agent.",
                            tool_name=tool_name,
                            pattern=pattern,
                            detail=command,
                        )
        if tool_name in ("Write", "Edit"):
            path = str(tool_input.get("file_path", ""))
            for pattern in FORBIDDEN_EDIT_PATH_PATTERNS:
                if re.search(pattern, path, re.IGNORECASE):
                    return _deny(
                        f"Blocked by safety policy: refusing to edit {path!r} autonomously.",
                        tool_name=tool_name,
                        pattern=pattern,
                        detail=path,
                    )
        return {}

    return guarded_pre_tool_use


def make_hooks(allow_prod: bool = False) -> dict[str, list[HookMatcher]]:
    """The hooks= dict to pass into ClaudeAgentOptions. matcher=None applies to
    every tool call, not just a named subset."""
    return {"PreToolUse": [HookMatcher(matcher=None, hooks=[make_pre_tool_use_hook(allow_prod=allow_prod)])]}
