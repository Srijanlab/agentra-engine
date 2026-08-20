"""agents/brain/__init__.py::run_autonomous_cycle — the orchestrator's own
top-level query() call (not a sub-agent tool call routed through agents/
base.py::run_agent, see test_brain_tools_auth_failure.py for that layer)
hitting a Claude Code CLI auth/login failure.

GitHub issue #42: a prior autonomous cycle crashed opaquely with "Claude
Code returned an error result: Not logged in · Please run /login (exit
code: 1)". This must be (1) detected distinctly from an ordinary crash, (2)
surfaced as a clear, actionable diagnostic instead of a bare "cycle raised:
...", (3) escalated via the Slack human-in-the-loop channel (GitHub issue
#34's connectors/slack.py) with an actionable message, and (4) filed as a
needs_human/blocking_agentra bug so the *next* cycle's blocking_bugs()
pre-flight check refuses to start at all rather than repeating the same
failure/re-notifying every cycle.

Fast unit tests (query() monkeypatched, no real LLM call) -- same style as
tests/test_brain_blocking_bugs.py.
"""

import asyncio

from claude_agent_sdk import ProcessError

from agentra import registry
from agentra.agents import brain
from agentra.connectors import slack
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory

_LOGIN_ERROR_TEXT = "Claude Code returned an error result: Not logged in · Please run /login (exit code: 1)"


def _common_monkeypatches(monkeypatch):
    monkeypatch.setattr(registry, "record_agent_step", lambda *a, **k: None)
    monkeypatch.setattr("agentra.agents.brain.deployment.persist_audit_trail", lambda *a, **k: None)
    monkeypatch.setattr(Memory, "blocking_bugs", lambda self: [])


def test_autonomous_cycle_flags_a_login_failure_distinctly_and_files_a_blocking_bug(tmp_path, monkeypatch):
    _common_monkeypatches(monkeypatch)

    async def _fake_query(*args, **kwargs):
        raise ProcessError(_LOGIN_ERROR_TEXT, exit_code=1)
        yield  # pragma: no cover -- makes this an async generator, never reached

    monkeypatch.setattr(brain, "query", _fake_query)

    recorded_bug = {}

    def fake_record_known_bug(
        self, run_id, severity, diagnosis, proposed_fix, source="prod-monitoring", external_id=None,
        needs_human=False, blocking_agentra=False, title=None,
    ):
        recorded_bug.update(needs_human=needs_human, blocking_agentra=blocking_agentra, diagnosis=diagnosis)
        return 7

    monkeypatch.setattr(Memory, "record_known_bug", fake_record_known_bug)
    monkeypatch.setattr(Memory, "issue_html_url", lambda self, n: f"https://github.com/acme/app/issues/{n}")
    slack_calls = []
    monkeypatch.setattr(slack, "notify_human_input_required", lambda **k: slack_calls.append(k) or True)

    repo = tmp_path / "repo"
    repo.mkdir()

    report = asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))

    # Clear, actionable diagnostic -- not the generic "autonomous cycle raised: ..." text.
    assert "Claude Code session is not authenticated" in report.final_message
    assert "run /login and re-trigger" in report.final_message
    assert any("Claude Code authentication failure" in a for a in report.actions)
    assert not any("autonomous cycle crashed:" in a for a in report.actions)

    # Filed as needs_human + blocking_agentra so the next cycle's
    # blocking_bugs() pre-flight check stops before repeating the failure.
    assert recorded_bug["needs_human"] is True
    assert recorded_bug["blocking_agentra"] is True

    # Escalated via the existing Slack human-in-the-loop channel (issue #34)
    # with the actionable message this feature asks for verbatim.
    assert len(slack_calls) == 1
    assert slack_calls[0]["question"] == "Claude Code session is not authenticated on this runner -- run /login and re-trigger."
    assert slack_calls[0]["issue_url"] == "https://github.com/acme/app/issues/7"


def test_autonomous_cycle_ordinary_crash_keeps_the_generic_diagnostic(tmp_path, monkeypatch):
    _common_monkeypatches(monkeypatch)

    async def _fake_query(*args, **kwargs):
        raise RuntimeError("some unrelated CLI failure")
        yield  # pragma: no cover

    monkeypatch.setattr(brain, "query", _fake_query)
    monkeypatch.setattr(Memory, "record_known_bug", lambda self, *a, **k: None)
    monkeypatch.setattr(
        slack, "notify_human_input_required", lambda **k: (_ for _ in ()).throw(AssertionError("must not notify"))
    )

    repo = tmp_path / "repo"
    repo.mkdir()

    report = asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))

    assert "autonomous cycle raised: some unrelated CLI failure" in report.final_message
    assert any("autonomous cycle crashed:" in a for a in report.actions)


def test_autonomous_cycle_does_not_re_notify_slack_for_an_already_reported_login_failure(tmp_path, monkeypatch):
    """GitHub issue #42's requirement (4): once escalated and awaiting a
    human, a login failure must not perpetually resurface -- record_failure
    only notifies Slack for a genuinely new occurrence (see
    Memory.record_failure/_notify_claude_code_auth_failure and its
    _find_similar_open_bug dedup check)."""
    _common_monkeypatches(monkeypatch)

    async def _fake_query(*args, **kwargs):
        raise ProcessError(_LOGIN_ERROR_TEXT, exit_code=1)
        yield  # pragma: no cover

    monkeypatch.setattr(brain, "query", _fake_query)
    # A near-identical bug (same step_name -> same generic diagnosis text) is
    # already open, as it would be after the very first occurrence filed one
    # -- record_known_bug's own dedup comments on it instead of filing a new
    # issue; record_failure's own separate _find_similar_open_bug check
    # (used only to gate the Slack notification) sees the same thing.
    monkeypatch.setattr(Memory, "record_known_bug", lambda self, *a, **k: 7)
    monkeypatch.setattr(Memory, "_find_similar_open_bug", lambda self, diagnosis, detail="": "7")
    monkeypatch.setattr(
        slack, "notify_human_input_required", lambda **k: (_ for _ in ()).throw(AssertionError("must not re-notify"))
    )

    repo = tmp_path / "repo"
    repo.mkdir()

    # Must not raise (the Slack mock above throws if it's ever called).
    asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))
