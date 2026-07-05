"""Shared machinery for invoking a single agent turn via the Claude Agent SDK."""

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from agentos.agents.safety import make_can_use_tool

_JSON_BLOCK = re.compile(r"```json\s*(.*?)```", re.DOTALL)


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
) -> AgentResult:
    """Run one agent to completion and return its final result message.

    allow_prod must only be set True for the single, explicitly-approved
    prod-promotion call in the auto-remediate hotfix path — never as a
    general default. See agents/safety.py.
    """
    options = ClaudeAgentOptions(
        cwd=str(cwd),
        system_prompt=system_prompt,
        allowed_tools=allowed_tools,
        permission_mode=permission_mode,
        can_use_tool=make_can_use_tool(allow_prod=allow_prod),
        max_turns=max_turns,
    )

    result_msg: ResultMessage | None = None
    async for message in query(prompt=single_prompt_stream(prompt), options=options):
        if isinstance(message, ResultMessage):
            result_msg = message

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
