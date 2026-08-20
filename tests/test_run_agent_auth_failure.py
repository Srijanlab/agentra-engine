"""agents/base.py::run_agent — Claude Code CLI auth/login failure handling
(GitHub issue #42: a prior autonomous cycle crashed opaquely on "Claude Code
returned an error result: Not logged in · Please run /login (exit code: 1)").

Covers:
  1. An auth/login failure is detected distinctly (AgentResult.auth_failure)
     from an ordinary subprocess failure.
  2. The returned text is a clear, actionable diagnostic, not the bare
     "agent turn raised: ..." used for everything else.
  3. Auth failures get no retry at all -- neither the resume-fallback retry
     (even though exit_code==1 + resume set would otherwise qualify) nor the
     contradictory-result retry -- query() is called exactly once.

Fast unit tests (query() monkeypatched, no real LLM call) -- same style as
tests/test_run_agent_tool_isolation.py.
"""

import asyncio

from claude_agent_sdk import ProcessError

from agentra.agents import base

_LOGIN_ERROR_TEXT = "Claude Code returned an error result: Not logged in · Please run /login (exit code: 1)"


def _fake_query_raising(exc, call_count):
    async def _fake_query(prompt, options):
        call_count["n"] += 1
        raise exc
        yield  # pragma: no cover -- makes this an async generator, never reached

    return _fake_query


def test_run_agent_flags_a_login_failure_distinctly(tmp_path, monkeypatch):
    call_count = {"n": 0}
    monkeypatch.setattr(
        base, "query", _fake_query_raising(ProcessError(_LOGIN_ERROR_TEXT, exit_code=1), call_count)
    )

    result = asyncio.run(
        base.run_agent(
            prompt="do the thing",
            system_prompt="you are an agent",
            cwd=tmp_path,
            allowed_tools=["Read"],
            agent_label="Test Agent",
        )
    )

    assert result.ok is False
    assert result.auth_failure is True
    assert "claude /login" in result.text
    assert call_count["n"] == 1


def test_run_agent_does_not_retry_a_login_failure_as_a_stale_resume(tmp_path, monkeypatch):
    """A login failure also happens to look exactly like the "stale resume
    session" signal (ProcessError, exit_code==1, resume set) -- must not be
    misclassified as that and given a pointless fresh-session retry, since
    the fresh session will hit the exact same missing credentials."""
    call_count = {"n": 0}
    monkeypatch.setattr(
        base, "query", _fake_query_raising(ProcessError(_LOGIN_ERROR_TEXT, exit_code=1), call_count)
    )

    result = asyncio.run(
        base.run_agent(
            prompt="do the thing",
            system_prompt="you are an agent",
            cwd=tmp_path,
            allowed_tools=["Read"],
            agent_label="Test Agent",
            resume="some-prior-session-id",
        )
    )

    assert result.auth_failure is True
    assert call_count["n"] == 1, "a login failure must not trigger the stale-resume fresh-session retry"


def test_run_agent_does_not_apply_contradictory_result_retry_to_a_login_failure(tmp_path, monkeypatch):
    """retry_on_contradictory_result must not accidentally cover auth
    failures either -- they don't share the contradictory-result suffix, but
    guard against future regressions where the checks get reordered."""
    call_count = {"n": 0}
    monkeypatch.setattr(
        base, "query", _fake_query_raising(ProcessError(_LOGIN_ERROR_TEXT, exit_code=1), call_count)
    )

    result = asyncio.run(
        base.run_agent(
            prompt="do the thing",
            system_prompt="you are an agent",
            cwd=tmp_path,
            allowed_tools=["Read"],
            agent_label="Test Agent",
            retry_on_contradictory_result=True,
        )
    )

    assert result.auth_failure is True
    assert call_count["n"] == 1


def test_run_agent_ordinary_failure_is_not_flagged_as_an_auth_failure(tmp_path, monkeypatch):
    call_count = {"n": 0}
    monkeypatch.setattr(
        base, "query", _fake_query_raising(ProcessError("some other CLI failure", exit_code=2), call_count)
    )

    result = asyncio.run(
        base.run_agent(
            prompt="do the thing",
            system_prompt="you are an agent",
            cwd=tmp_path,
            allowed_tools=["Read"],
            agent_label="Test Agent",
        )
    )

    assert result.ok is False
    assert result.auth_failure is False
    assert "agent turn raised" in result.text
