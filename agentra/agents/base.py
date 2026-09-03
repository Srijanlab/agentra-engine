"""Shared machinery for invoking a single agent turn via the Claude Agent SDK."""

import json
import os
import re
from collections.abc import AsyncIterator, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from agentra.observability import get_client, observe

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookEventMessage,
    ProcessError,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    ThinkingBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    ServerToolResultBlock,
    ServerToolUseBlock,
    query,
)

from agentra.agents.safety import make_hooks
from agentra.memory.core import is_login_required_failure

_JSON_BLOCK = re.compile(r"```json\s*(.*?)```", re.DOTALL)
_WHITESPACE = re.compile(r"\s+")

# claude_agent_sdk._internal.query.py's read-task builds this exact string when the CLI's
_CONTRADICTORY_RESULT_SUFFIX = "returned an error result: success"

_MAX_TURNS_PATTERN = re.compile(r"Reached maximum number of turns \(\d+\)")


def _friendly_error_text(raw: str) -> str:
    """Confirmed live: a grounded chat question ("is everything green?", Testing Agent, max_turns=5) that needed more tool calls than it had turns for raised "agent turn raised: Claude Code returned an error result: Reached maximum number of turns (N)" -- the CLI's internal exception text, verbatim, straight into a chat bubble."""
    if _MAX_TURNS_PATTERN.search(raw):
        return "I needed more steps than I had available to fully answer that -- try asking something narrower, or ask again for another attempt."
    if _CONTRADICTORY_RESULT_SUFFIX in raw:
        return "Claude Code returned a self-contradictory result (known CLI quirk, not a real failure) -- try asking again."
    if is_login_required_failure(raw):
        return "Claude Code isn't authenticated on this server right now -- that needs a human to fix (re-run /login or refresh credentials), not something asking again will resolve."
    return raw

def _sdk_env() -> dict[str, str]:
    """Extra env for the Claude Agent SDK subprocess, per the dashboard's Model
    Backend toggle:
      "claude"       -- the interactive `claude login` session (nothing to add)
      "claude_token" -- a headless CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`)
      "nim"          -- the self-hosted NVIDIA NIM proxy
    The container's base env must NOT carry CLAUDE_CODE_OAUTH_TOKEN, or the SDK
    subprocess inherits it and the "claude" login path can never win."""
    from agentra import registry

    backend = registry.get_llm_backend()
    if backend == "nim":
        base_url = os.environ.get("NIM_PROXY_URL", "")
        if not base_url:
            return {}
        return {
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_API_KEY": os.environ.get("NIM_PROXY_API_KEY", "nim-proxy"),
        }
    if backend == "claude_token":
        token = os.environ.get("AGENTRA_CLAUDE_TOKEN") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
        return {"CLAUDE_CODE_OAUTH_TOKEN": token} if token else {}
    return {}


RunLogger = Callable[[str], None]
_RUN_LOGGER: ContextVar[RunLogger | None] = ContextVar("agentra_run_logger", default=None)


@contextmanager
def run_log_scope(logger: RunLogger | None):
    token = _RUN_LOGGER.set(logger)
    try:
        yield
    finally:
        _RUN_LOGGER.reset(token)


def current_run_logger() -> RunLogger | None:
    """The ambient logger set by the innermost active run_log_scope, if any."""
    return _RUN_LOGGER.get()


# ── Message formatting: SDK message/block objects -> compact log lines ──────


def _compact_text(text: str, limit: int = 240) -> str:
    compact = _WHITESPACE.sub(" ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _compact_json(value: Any, limit: int = 240) -> str:
    try:
        text = json.dumps(value, sort_keys=True, default=str)
    except Exception:
        text = repr(value)
    return _compact_text(text, limit)


def _format_content_block(block: Any) -> list[str]:
    if isinstance(block, TextBlock):
        return [f"assistant text: {_compact_text(block.text)}"]
    if isinstance(block, ThinkingBlock):
        return [f"assistant thinking: {_compact_text(block.thinking)}"]
    if isinstance(block, ToolUseBlock):
        return [f"assistant tool_use: {block.name} id={block.id} input={_compact_json(block.input)}"]
    if isinstance(block, ToolResultBlock):
        return [
            "assistant tool_result: "
            f"id={block.tool_use_id} error={block.is_error} content={_compact_json(block.content)}"
        ]
    if isinstance(block, ServerToolUseBlock):
        return [f"assistant server_tool_use: {block.name} id={block.id} input={_compact_json(block.input)}"]
    if isinstance(block, ServerToolResultBlock):
        return [f"assistant server_tool_result: id={block.tool_use_id} content={_compact_json(block.content)}"]
    return [f"assistant block: {_compact_json(block)}"]


def _format_system_message(message: SystemMessage) -> list[str]:
    if isinstance(message, TaskStartedMessage):
        return [f"system task_started: {message.description}"]
    if isinstance(message, TaskProgressMessage):
        return [f"system task_progress: {message.description}"]
    if isinstance(message, TaskNotificationMessage):
        summary = _compact_text(message.summary or "")
        return [f"system task_notification[{message.status}]: {summary}"]
    if isinstance(message, TaskUpdatedMessage):
        return [f"system task_updated[{message.status}]: task_id={message.task_id}"]
    if isinstance(message, HookEventMessage):
        payload = message.data.copy()
        payload.pop("type", None)
        payload.pop("subtype", None)
        return [
            f"system hook[{message.hook_event_name or 'unknown'}:{message.subtype}]: {_compact_json(payload)}"
        ]
    return [f"system[{message.subtype}]: {_compact_json(message.data)}"]


def format_claude_message(message: Any) -> list[str]:
    if isinstance(message, AssistantMessage):
        header = f"assistant model={message.model}"
        if message.stop_reason:
            header += f" stop_reason={message.stop_reason}"
        if message.error:
            header += f" error={message.error}"
        lines = [header]
        for block in message.content:
            lines.extend(_format_content_block(block))
        return lines
    if isinstance(message, SystemMessage):
        return _format_system_message(message)
    if isinstance(message, StreamEvent):
        event_type = message.event.get("type") or message.event.get("event") or "stream_event"
        payload = message.event.copy()
        payload.pop("type", None)
        payload.pop("event", None)
        return [f"stream_event[{event_type}]: {_compact_json(payload)}"]
    if isinstance(message, RateLimitEvent):
        info = message.rate_limit_info
        return [
            "rate_limit: "
            f"status={info.status} type={info.rate_limit_type} utilization={info.utilization} "
            f"resets_at={info.resets_at}"
        ]
    if isinstance(message, ResultMessage):
        parts = [
            "result: "
            f"ok={not message.is_error} turns={message.num_turns} cost=${message.total_cost_usd or 0.0:.4f}"
        ]
        if message.stop_reason:
            parts[0] += f" stop_reason={message.stop_reason}"
        if message.terminal_reason:
            parts[0] += f" terminal_reason={message.terminal_reason}"
        if message.errors:
            parts.append(f"result errors: {_compact_json(message.errors)}")
        return parts
    return [f"message: {_compact_json(message)}"]


def log_claude_message(message: Any, logger: RunLogger | None = None) -> None:
    sink = logger or _RUN_LOGGER.get()
    if sink is None:
        return
    for line in format_claude_message(message):
        sink(line)


def _sum_model_usage(model_usage: dict[str, Any] | None) -> dict[str, int]:
    """Collapses ResultMessage.model_usage's per-model breakdown (usually one
    model, but a turn can span more than one) into the 4 token counters
    AgentResult exposes (GitHub issue #74)."""
    totals = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }
    for usage in (model_usage or {}).values():
        totals["input_tokens"] += usage.get("inputTokens") or 0
        totals["output_tokens"] += usage.get("outputTokens") or 0
        totals["cache_read_input_tokens"] += usage.get("cacheReadInputTokens") or 0
        totals["cache_creation_input_tokens"] += usage.get("cacheCreationInputTokens") or 0
    return totals


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
    # The CLI's own session id for this turn (ResultMessage.session_id) --
    session_id: str | None = None
    # True only for the Claude Code CLI's own "this runner has no valid
    auth_failure: bool = False
    # True only when implementation.run()'s post-commit git_ops.push_branch call
    # never succeeded even after retries -- the commit is NOT confirmed durable
    # on GitHub (see agentra/agents/implementation.py and GitHub issue #78).
    push_failed: bool = False
    # Token usage for this turn, summed across every model used (see
    # _sum_model_usage) -- 0 for any AgentResult built without a real
    # ResultMessage (e.g. an early-return error path before the SDK call
    # even happened). GitHub issue #74.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


def extract_json_block(text: str) -> dict[str, Any] | None:
    match = _JSON_BLOCK.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


@observe(name="agent", as_type="agent")
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
    agent_label: str | None = None,
    resume: str | None = None,
    mcp_servers: dict[str, Any] | None = None,
) -> AgentResult:
    """Run one agent to completion and return its final result message."""
    _lf = get_client()
    _lf.update_current_span(
        name=agent_label or "agent",
        input={"task": prompt, "cwd": str(cwd), "allowed_tools": allowed_tools,
               "permission_mode": permission_mode, "max_turns": max_turns, "resume": bool(resume)},
    )
    # tools= (built-in tool *availability*) is distinct from allowed_tools=
    built_in_tools = [t for t in allowed_tools if not t.startswith("mcp__")]
    options = ClaudeAgentOptions(
        cwd=str(cwd),
        system_prompt=system_prompt,
        tools=built_in_tools,
        allowed_tools=allowed_tools,
        permission_mode=permission_mode,
        hooks=make_hooks(allow_prod=allow_prod),
        max_turns=max_turns,
        include_partial_messages=True,
        include_hook_events=True,
        resume=resume,
        mcp_servers=mcp_servers or {},
        env=_sdk_env(),
    )

    max_contradictory_attempts = 2 if retry_on_contradictory_result else 1
    contradictory_attempts_used = 0
    resume_fallback_used = False
    raw_logger = _RUN_LOGGER.get()
    label = agent_label or "Agent"
    logger = (lambda line: raw_logger(f"[{label}] {line}")) if raw_logger else None
    while True:
        result_msg: ResultMessage | None = None
        try:
            if logger:
                logger(
                    "claude agent start | "
                    f"cwd={cwd} tools={allowed_tools} permission_mode={permission_mode!r} "
                    f"max_turns={max_turns} allow_prod={allow_prod} resume={options.resume!r}"
                )
            async for message in query(prompt=single_prompt_stream(prompt), options=options):
                log_claude_message(message, logger)
                if isinstance(message, ResultMessage):
                    result_msg = message
            break
        except Exception as exc:
            exc_text = str(exc)
            # GitHub issue #42: a prior autonomous cycle crashed opaquely on
            if is_login_required_failure(exc_text):
                if logger:
                    logger(
                        f"claude agent AUTH FAILURE, not retrying: {exc_text} -- this runner's "
                        "Claude Code CLI has no valid session; a human needs to run `claude /login` "
                        "(or otherwise refresh credentials) before further cycles can make progress."
                    )
                return AgentResult(
                    ok=False,
                    text=(
                        "Claude Code authentication failure -- the CLI reported it is not "
                        f"usable on this runner ({exc_text}). This needs a human to run "
                        "`claude /login` or otherwise refresh credentials here; retrying "
                        "automatically will not help."
                    ),
                    json_data=None,
                    cost_usd=0.0,
                    turns=0,
                    auth_failure=True,
                )
            # A `resume` session_id can go stale -- e.g. it lived only on local
            if (
                options.resume
                and not resume_fallback_used
                and isinstance(exc, ProcessError)
                and exc.exit_code == 1
            ):
                if logger:
                    logger(f"resume={options.resume!r} failed to load ({exc}); retrying as a fresh session")
                options = replace(options, resume=None)
                resume_fallback_used = True
                continue
            is_contradictory = exc_text.endswith(_CONTRADICTORY_RESULT_SUFFIX)
            contradictory_attempts_used += 1
            if is_contradictory and contradictory_attempts_used < max_contradictory_attempts:
                continue
            # Either a different, real error, or retries are exhausted/disabled -- same
            return AgentResult(ok=False, text=f"agent turn raised: {exc}", json_data=None, cost_usd=0.0, turns=0)

    if result_msg is None:
        return AgentResult(ok=False, text="", json_data=None, cost_usd=0.0, turns=0)

    text = result_msg.result or ""
    res = AgentResult(
        ok=not result_msg.is_error,
        text=text,
        json_data=extract_json_block(text),
        cost_usd=result_msg.total_cost_usd or 0.0,
        turns=result_msg.num_turns,
        session_id=result_msg.session_id,
        **_sum_model_usage(result_msg.model_usage),
    )
    return res


async def stream_chat_turn(
    *,
    prompt: str,
    system_prompt: str,
    cwd: Path,
    allowed_tools: list[str],
    max_turns: int | None = None,
    resume: str | None = None,
    agent_label: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Streaming sibling of run_agent, for server.py's chat endpoint only -- NOT a general replacement (every autonomous-cycle caller stays on run_agent)."""
    options = ClaudeAgentOptions(
        cwd=str(cwd),
        system_prompt=system_prompt,
        allowed_tools=allowed_tools,
        permission_mode="bypassPermissions",
        hooks=make_hooks(allow_prod=False),
        max_turns=max_turns,
        include_partial_messages=True,
        resume=resume,
        env=_sdk_env(),
    )

    raw_logger = _RUN_LOGGER.get()
    label = agent_label or "Agent"
    logger = (lambda line: raw_logger(f"[{label}] {line}")) if raw_logger else None

    result_msg: ResultMessage | None = None
    try:
        if logger:
            logger(f"claude chat stream start | cwd={cwd} tools={allowed_tools} max_turns={max_turns} resume={resume!r}")
        async for message in query(prompt=single_prompt_stream(prompt), options=options):
            log_claude_message(message, logger)
            if isinstance(message, StreamEvent):
                event = message.event
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        yield {"type": "delta", "text": delta["text"]}
            elif isinstance(message, ResultMessage):
                result_msg = message
    except Exception as exc:
        yield {
            "type": "done", "ok": False, "text": _friendly_error_text(f"agent turn raised: {exc}"),
            "json_data": None, "cost_usd": 0.0, "turns": 0, "session_id": None,
        }
        return

    if result_msg is None:
        yield {"type": "done", "ok": False, "text": "", "json_data": None, "cost_usd": 0.0, "turns": 0, "session_id": None}
        return

    text = result_msg.result or ""
    yield {
        "type": "done",
        "ok": not result_msg.is_error,
        "text": text,
        "json_data": extract_json_block(text),
        "cost_usd": result_msg.total_cost_usd or 0.0,
        "turns": result_msg.num_turns,
        "session_id": result_msg.session_id,
        **_sum_model_usage(result_msg.model_usage),
    }
