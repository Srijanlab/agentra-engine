"""Shared machinery for invoking a single agent turn via the Claude Agent SDK."""

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from agentos.agents.safety import make_hooks

_JSON_BLOCK = re.compile(r"```json\s*(.*?)```", re.DOTALL)

# claude_agent_sdk._internal.query.py's read-task builds this exact string when the CLI's
# final `result` message for a turn arrives with is_error=True but subtype="success" --
# "success" is the CLI's own normal-completion subtype, so this combination is the CLI
# self-contradicting, not a real reported failure (confirmed by reading query.py directly;
# same behavior on the latest CLI 2.1.202 and SDK 0.2.111, so not something a version bump
# fixes). Safe to retry *only* for agents whose work up to the point of failure is read-only
# or otherwise idempotent -- see run_agent's retry_on_contradictory_result docstring.
_CONTRADICTORY_RESULT_SUFFIX = "returned an error result: success"


async def single_prompt_stream(prompt: str) -> AsyncIterator[dict[str, Any]]:
    """can_use_tool requires streaming input; wrap a plain string prompt as one."""
    yield {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": prompt},
        "parent_tool_use_id": None,
    }


@dataclass
class AgentResult:
    ok: bool
    text: str
    json_data: dict[str, Any] | None
    cost_usd: float
    turns: int


def extract_json_block(text: str) -> dict[str, Any] | None:
    match = _JSON_BLOCK.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


async def run_agent(
    *,
    prompt: str,
    system_prompt: str,
    cwd: Path,
    allowed_tools: list[str],
    permission_mode: str = "bypassPermissions",
    max_turns: int | None = None,
    allow_prod: bool = False,
    retry_on_contradictory_result: bool = True,
) -> AgentResult:
    """Run one agent to completion and return its final result message.

    allow_prod must only be set True for the single, explicitly-approved
    prod-promotion call in the auto-remediate hotfix path — never as a
    general default. See agents/safety.py.

    retry_on_contradictory_result governs whether a fresh, from-scratch retry
    happens when the CLI hits the self-contradictory is_error=True/subtype="success"
    case (see _CONTRADICTORY_RESULT_SUFFIX above). A retry re-runs the *entire*
    conversation from the same prompt in a brand-new subprocess -- safe for
    read-only or otherwise idempotent agents (codebase, discovery, testing,
    feedback, prod_debug), but implementation.py must pass False: by the time
    this error surfaces the agent's actual work (file edits, a git commit) has
    typically already happened, so a blind retry risks a second, conflicting
    attempt at the same commit rather than recovering anything.
    """
    options = ClaudeAgentOptions(
        cwd=str(cwd),
        system_prompt=system_prompt,
        allowed_tools=allowed_tools,
        permission_mode=permission_mode,
        hooks=make_hooks(allow_prod=allow_prod),
        max_turns=max_turns,
    )

    attempts = 2 if retry_on_contradictory_result else 1
    for attempt in range(attempts):
        result_msg: ResultMessage | None = None
        try:
            async for message in query(prompt=single_prompt_stream(prompt), options=options):
                if isinstance(message, ResultMessage):
                    result_msg = message
            break
        except Exception as exc:
            is_contradictory = str(exc).endswith(_CONTRADICTORY_RESULT_SUFFIX)
            if is_contradictory and attempt < attempts - 1:
                continue
            # Either a different, real error, or retries are exhausted/disabled -- same
            # graceful-failure path as before: report it, don't crash the whole cycle.
            return AgentResult(ok=False, text=f"agent turn raised: {exc}", json_data=None, cost_usd=0.0, turns=0)

    if result_msg is None:
        return AgentResult(ok=False, text="", json_data=None, cost_usd=0.0, turns=0)

    text = result_msg.result or ""
    return AgentResult(
        ok=not result_msg.is_error,
        text=text,
        json_data=extract_json_block(text),
        cost_usd=result_msg.total_cost_usd or 0.0,
        turns=result_msg.num_turns,
    )
