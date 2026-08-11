"""Deterministic tests for Claude SDK message -> run log formatting.

These tests do not call the SDK or network. They verify that the
dashboard-facing formatter emits readable lines for assistant content
blocks, system/hook events, stream events, and result messages, and that
the run-scoped logger callback is honored.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_agent_sdk import (
    AssistantMessage,
    HookEventMessage,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    StreamEvent,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from agentra.agents.base import format_claude_message, log_claude_message, run_log_scope


class ClaudeStreamLoggingTest(unittest.TestCase):
    def test_assistant_message_is_expanded_into_readable_lines(self):
        message = AssistantMessage(
            content=[
                TextBlock(text="hello\nworld"),
                ThinkingBlock(thinking="reasoning text", signature="sig"),
                ToolUseBlock(id="tool-1", name="Read", input={"path": "foo.py"}),
                ToolResultBlock(tool_use_id="tool-1", content=[{"type": "text", "text": "done"}], is_error=False),
                ServerToolUseBlock(id="srv-1", name="web_search", input={"query": "docs"}),
                ServerToolResultBlock(tool_use_id="srv-1", content={"type": "web_search_result", "count": 1}),
            ],
            model="claude-test",
            stop_reason="tool_use",
        )

        lines = format_claude_message(message)

        self.assertIn("assistant model=claude-test stop_reason=tool_use", lines[0])
        self.assertTrue(any("assistant text: hello world" in line for line in lines))
        self.assertTrue(any("assistant thinking: reasoning text" in line for line in lines))
        self.assertTrue(any("assistant tool_use: Read id=tool-1" in line for line in lines))
        self.assertTrue(any("assistant tool_result: id=tool-1" in line for line in lines))
        self.assertTrue(any("assistant server_tool_use: web_search id=srv-1" in line for line in lines))
        self.assertTrue(any("assistant server_tool_result: id=srv-1" in line for line in lines))

    def test_system_and_stream_messages_are_formatted(self):
        started = TaskStartedMessage(
            subtype="task_started",
            data={"type": "system", "subtype": "task_started"},
            task_id="task-1",
            description="scan repo",
            uuid="u1",
            session_id="s1",
        )
        updated = TaskUpdatedMessage(
            subtype="task_updated",
            data={"type": "system", "subtype": "task_updated"},
            task_id="task-1",
            patch={"status": "running"},
            status="running",
            uuid="u2",
            session_id="s1",
        )
        hook = HookEventMessage(
            subtype="hook_started",
            data={"type": "system", "subtype": "hook_started", "hook_event": "PreToolUse", "tool_name": "Read"},
            hook_event_name="PreToolUse",
            session_id="s1",
            uuid="u3",
        )
        notification = TaskNotificationMessage(
            subtype="task_notification",
            data={"type": "system", "subtype": "task_notification"},
            task_id="task-1",
            status="completed",
            output_file="out.txt",
            summary="finished cleanly",
            uuid="u4",
            session_id="s1",
        )
        stream = StreamEvent(
            uuid="u5",
            session_id="s1",
            event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "partial"}},
        )
        rate = RateLimitEvent(
            rate_limit_info=RateLimitInfo(status="allowed_warning", rate_limit_type="five_hour", utilization=0.9, raw={}),
            uuid="u6",
            session_id="s1",
        )

        self.assertIn("system task_started: scan repo", format_claude_message(started)[0])
        self.assertIn("system task_updated[running]: task_id=task-1", format_claude_message(updated)[0])
        self.assertIn("system hook[PreToolUse:hook_started]", format_claude_message(hook)[0])
        self.assertIn("system task_notification[completed]: finished cleanly", format_claude_message(notification)[0])
        self.assertIn("stream_event[content_block_delta]", format_claude_message(stream)[0])
        self.assertIn("rate_limit: status=allowed_warning", format_claude_message(rate)[0])

    def test_result_message_and_run_logger_scope_emit_lines(self):
        result = ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=9,
            is_error=False,
            num_turns=3,
            session_id="s1",
            total_cost_usd=0.1234,
            result="done",
            terminal_reason="completed",
        )

        lines: list[str] = []
        with run_log_scope(lines.append):
            log_claude_message(result)

        self.assertEqual(len(lines), 1)
        self.assertIn("result: ok=True turns=3 cost=$0.1234", lines[0])
        self.assertIn("terminal_reason=completed", lines[0])


if __name__ == "__main__":
    unittest.main()
