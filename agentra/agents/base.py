"""Shared machinery for invoking a single agent turn via the Claude Agent SDK."""

import json
import re
from collections.abc import AsyncIterator, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookEventMessage,
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

_JSON_BLOCK = re.compile(r"```json\s*(.*?)```", re.DOTALL)
_WHITESPACE = re.compile(r"\s+")

# claude_agent_sdk._internal.query.py's read-task builds this exact string when the CLI's
# final `result` message for a turn arrives with is_error=True but subtype="success" --
# "success" is the CLI's own normal-completion subtype, so this combination is the CLI
# self-contradicting, not a real reported failure (confirmed by reading query.py directly;
# same behavior on the latest CLI 2.1.202 and SDK 0.2.111, so not something a version bump
# fixes). Safe to retry *only* for agents whose work up to the point of failure is read-only
# or otherwise idempotent -- see run_agent's retry_on_contradictory_result docstring.
_CONTRADICTORY_RESULT_SUFFIX = "returned an error result: success"

_MAX_TURNS_PATTERN = re.compile(r"Reached maximum number of turns \(\d+\)")


def _friendly_error_text(raw: str) -> str:
    """Confirmed live: a grounded chat question ("is everything green?",
    Testing Agent, max_turns=5) that needed more tool calls than it had
    turns for raised "agent turn raised: Claude Code returned an error
    result: Reached maximum number of turns (N)" -- the CLI's internal
    exception text, verbatim, straight into a chat bubble. Callers that
    show `text` directly to a human (chat, standup channel -- NOT the
    autonomous-cycle callers, who log this and move on programmatically)
    should route it through here first."""
    if _MAX_TURNS_PATTERN.search(raw):
        return "I needed more steps than I had available to fully answer that -- try asking something narrower, or ask again for another attempt."
    if _CONTRADICTORY_RESULT_SUFFIX in raw:
        return "Claude Code returned a self-contradictory result (known CLI quirk, not a real failure) -- try asking again."
    return raw

RunLogger = Callable[[str], None]
_RUN_LOGGER: ContextVar[RunLogger | None] = ContextVar("agentra_run_logger", default=None)


@contextmanager
def run_log_scope(logger: RunLogger | None):
    token = _RUN_LOGGER.set(logger)
    try:
        yield
    finally:
        _RUN_LOGGER.reset(token)


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
    # pass back in as run_agent's `resume` argument to continue this exact
    # session on a later call instead of starting cold. None only if the
    # turn never produced a ResultMessage at all (the early-return failure
    # paths below).
    session_id: str | None = None


def extract_json_block(text: str) -> dict[str, Any] | None:
    match = _JSON_BLOCK.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def label_to_id(label: str) -> str:
    if "Codebase" in label:
        return "codebase"
    if "Discovery" in label:
        return "discovery"
    if "Implementation" in label:
        return "implementation"
    if "Testing" in label:
        return "testing"
    if "Deployment" in label:
        return "deployment"
    if "Feedback" in label:
        return "feedback"
    if "Production Debugging" in label:
        return "prod_debug"
    if "Custom" in label:
        return "custom"
    return "orchestrator"


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
) -> AgentResult:
    """Run one agent to completion and return its final result message.

    resume: a session_id from a previous call's AgentResult.session_id --
    continues that exact session (full prior context, same as `claude -c`)
    instead of a cold-start turn from just `prompt` alone. None (the
    default, and what every background-cycle caller still passes) starts a
    fresh session same as before -- resumption is opt-in for callers that
    actually want conversational continuity, e.g. server.py's per-agent
    chat.

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

    agent_label: the human-readable identity to prefix every logged line
    with (e.g. "Codebase Agent") -- matches agentRoster.ts's labels exactly
    so the dashboard's live-log view can tell which agent is currently
    speaking and show a "who's active right now" pill instead of an
    undifferentiated firehose. None (the CLI's own direct calls have no
    caller-supplied label) falls back to a generic "Agent" tag rather than
    silently dropping the prefix, so parsing on the frontend stays uniform.
    """
    options = ClaudeAgentOptions(
        cwd=str(cwd),
        system_prompt=system_prompt,
        allowed_tools=allowed_tools,
        permission_mode=permission_mode,
        hooks=make_hooks(allow_prod=allow_prod),
        max_turns=max_turns,
        include_partial_messages=True,
        include_hook_events=True,
        resume=resume,
    )

    attempts = 2 if retry_on_contradictory_result else 1
    raw_logger = _RUN_LOGGER.get()
    label = agent_label or "Agent"
    logger = (lambda line: raw_logger(f"[{label}] {line}")) if raw_logger else None
    for attempt in range(attempts):
        result_msg: ResultMessage | None = None
        try:
            if logger:
                logger(
                    "claude agent start | "
                    f"cwd={cwd} tools={allowed_tools} permission_mode={permission_mode!r} "
                    f"max_turns={max_turns} allow_prod={allow_prod}"
                )
            async for message in query(prompt=single_prompt_stream(prompt), options=options):
                log_claude_message(message, logger)
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
    res = AgentResult(
        ok=not result_msg.is_error,
        text=text,
        json_data=extract_json_block(text),
        cost_usd=result_msg.total_cost_usd or 0.0,
        turns=result_msg.num_turns,
        session_id=result_msg.session_id,
    )
    if res.ok and res.json_data and "work_update" in res.json_data:
        try:
            from agentra.memory import Memory
            mem = Memory(cwd)
            agent_id = label_to_id(agent_label or "Agent")
            mem.record_work_update(agent_id, res.json_data["work_update"])
        except Exception:
            pass
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
    """Streaming sibling of run_agent, for server.py's chat endpoint only --
    NOT a general replacement (every autonomous-cycle caller stays on
    run_agent). Yields {"type": "delta", "text": ...} as assistant text
    arrives (content_block_delta/text_delta stream events -- confirmed live
    against the real SDK output, see the type field literally matching
    Anthropic's own streaming API shape), then exactly one final
    {"type": "done", "ok", "text" (full), "json_data", "cost_usd", "turns",
    "session_id"} matching AgentResult's fields, so callers that want the
    complete picture don't have to reassemble it from deltas themselves.

    Deliberately no retry-on-contradictory-result: a blind full retry after
    already streaming partial output to a human would mean silently
    discarding what they saw and restarting mid-conversation, more
    confusing than just surfacing the error -- callers that hit this can
    just send another message, same as any other chat failure. Also no
    work_update auto-persistence here (server.py's caller already does its
    own post-processing on the final "done" event, same as it does for
    run_agent's return value)."""
    options = ClaudeAgentOptions(
        cwd=str(cwd),
        system_prompt=system_prompt,
        allowed_tools=allowed_tools,
        permission_mode="bypassPermissions",
        hooks=make_hooks(allow_prod=False),
        max_turns=max_turns,
        include_partial_messages=True,
        resume=resume,
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
    }
