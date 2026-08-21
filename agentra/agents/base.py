"""Shared machinery for invoking a single agent turn via the Claude Agent SDK."""

import json
import re
from collections.abc import AsyncIterator, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

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
    if is_login_required_failure(raw):
        return "Claude Code isn't authenticated on this server right now -- that needs a human to fix (re-run /login or refresh credentials), not something asking again will resolve."
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


def current_run_logger() -> RunLogger | None:
    """The ambient logger set by the innermost active run_log_scope, if any.

    Lets other modules that don't otherwise hold a Memory instance or run_id
    (e.g. agents/safety.py's PreToolUse hook, which only ever sees
    tool_name/tool_input/context) write into the same durable per-run audit
    log that run_agent's own message logging uses, without threading new
    Memory/run_id plumbing through every call site. Returns None outside any
    run_log_scope (e.g. a bare unit test), in which case callers should just
    skip logging rather than raise.
    """
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
    # True only for the Claude Code CLI's own "this runner has no valid
    # session at all" failure (see memory.core.is_login_required_failure) --
    # distinct from every other reason ok can be False (a failing test, a
    # rejected diff, a transient API hiccup, ...). Callers that need to react
    # specifically (skip a retry, route to a distinct diagnostic) can check
    # this instead of re-deriving it from `text` themselves.
    auth_failure: bool = False


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
    agent_label: str | None = None,
    resume: str | None = None,
    mcp_servers: dict[str, Any] | None = None,
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

    mcp_servers: e.g. {"graphify": {"command": "graphify-mcp", "args": [...]}}
    (see agents/codegraph.py's mcp_config) -- lets a caller grant scoped,
    read-only tool access (mcp__<server>__<tool>, added to allowed_tools by
    the caller) without opening full Bash. None/omitted for every agent that
    doesn't need it, same as before this parameter existed.
    """
    # tools= (built-in tool *availability*) is distinct from allowed_tools=
    # (auto-approval) -- confirmed live (run 26bf7dee, tests/test_brain_tool_
    # isolation.py): leaving tools=None (the SDK default) grants the full
    # built-in Claude Code toolset regardless of allowed_tools, since every
    # call here uses permission_mode="bypassPermissions", which auto-approves
    # everything and never consults allowed_tools to gate availability either.
    # A "read-only" agent (allowed_tools=["Read","Glob","Grep"]) was therefore
    # never actually prevented from calling Bash/Write/WebSearch/... -- only
    # asked not to, in its system prompt. Every caller's allowed_tools is
    # already meant to be that agent's *complete* tool set (see agents/
    # catalog.py's per-agent metadata, hand-kept in sync with each module's
    # own allowed_tools), so mirroring it into tools= here closes that gap for
    # every agent at once. mcp__-prefixed entries are excluded: those aren't
    # built-in tool names, and MCP tool availability is controlled separately
    # by mcp_servers, not by tools=.
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
            # "Claude Code returned an error result: Not logged in · Please
            # run /login (exit code: 1)". Checked first, ahead of both retry
            # paths below, and deliberately never retried by either of them:
            # this means the CLI subprocess itself has no valid session on
            # this runner at all, so every other tool/prompt-shaped signal
            # below (a stale `resume`, the contradictory-result quirk) is
            # irrelevant -- a retry reruns the exact same subprocess with the
            # exact same missing credentials and fails identically, just
            # slower and more expensively. The only real fix (a human running
            # `claude /login` or otherwise provisioning credentials on this
            # runner) is outside agentra's control -- see this module's
            # docstring note and memory.core.is_login_required_failure, which
            # also routes this into record_failure's needs_human/
            # blocking_agentra GitHub-issue path instead of a plain retry.
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
            # disk and the VM was redeployed since (session_store isn't wired up
            # here). The CLI reports this as a plain ProcessError; the SDK
            # doesn't surface the real stderr text ("No conversation found with
            # session ID: ...") on the exception object, only a placeholder, so
            # exit_code==1 + a resume actually being set is the closest signal
            # available without reaching into SDK internals. Bounded to exactly
            # one extra cold-start attempt so a genuinely different exit-1
            # failure still surfaces for real afterward, just one attempt later.
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
    just send another message, same as any other chat failure."""
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
