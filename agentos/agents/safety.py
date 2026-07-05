"""Safety gate shared by every agent that gets Bash/Write/Edit access.

Implements the "Forbidden Actions" list from vision.md section 8: no production
deploys, no destructive data operations, no secrets/billing edits, no
irreversible operations. This is a blunt regex net, not a sandbox — it exists
to catch an agent following bad instructions, not a malicious one.
"""

import re

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

FORBIDDEN_BASH_PATTERNS = [
    r"rm\s+-rf\s+/(?:\s|$)",
    r"git\s+push\s+[^\n]*--force",
    r"git\s+reset\s+--hard\s+[^\n]*(main|master|production)",
    r"--prod\b",
    r"vercel\s+[^\n]*--prod",
    r"firebase\s+deploy[^\n]*production",
    r"\bDROP\s+TABLE\b",
    r"\bDELETE\s+FROM\s+\w+\s*;?\s*$",
    r"\.env(?:\.\w+)?\b",
    r"stripe\b",
    r"billing",
]

FORBIDDEN_EDIT_PATH_PATTERNS = [
    r"\.env(?:\.\w+)?$",
    r"secrets?/",
    r"credentials",
]


async def guarded_can_use_tool(tool_name, tool_input, context: ToolPermissionContext):
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        for pattern in FORBIDDEN_BASH_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return PermissionResultDeny(
                    message=(
                        f"Blocked by safety policy: command matches forbidden "
                        f"pattern for autonomous execution ({pattern!r}). "
                        "Production deploys, destructive data ops, and secrets "
                        "access require human approval."
                    )
                )
    if tool_name in ("Write", "Edit"):
        path = str(tool_input.get("file_path", ""))
        for pattern in FORBIDDEN_EDIT_PATH_PATTERNS:
            if re.search(pattern, path, re.IGNORECASE):
                return PermissionResultDeny(
                    message=f"Blocked by safety policy: refusing to edit {path!r} autonomously."
                )
    return PermissionResultAllow()
