"""agents/brain/__init__.py::run_autonomous_cycle — the orchestrator's own
top-level query() call (not a sub-agent tool call routed through agents/
base.py::run_agent) hitting a Claude Code CLI auth/login failure.

GitHub issue #42: a prior autonomous cycle crashed opaquely with "Claude
Code returned an error result: Not logged in · Please run /login (exit
code: 1)". This must be (1) detected distinctly from an ordinary crash, (2)
surfaced as a clear, actionable diagnostic instead of a bare "cycle raised:
...", and (3) filed as a needs_human/blocking_agentra bug so the *next*
cycle's blocking_bugs() pre-flight check refuses to start at all rather than
repeating the same failure.

Fast unit tests (query() monkeypatched, no real LLM call) -- same style as
tests/test_brain_blocking_bugs.py.
"""

import asyncio

from claude_agent_sdk import ProcessError

from agentra import registry
from agentra.agents import brain
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

    monkeypatch.setattr(Memory, "record_known_bug", fake_record_known_bug)

    repo = tmp_path / "repo"
    repo.mkdir()

    report = asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))

    # Clear, actionable diagnostic -- not the generic "autonomous cycle raised: ..." text.
    assert "authentication failure" in report.final_message
    assert "claude /login" in report.final_message
    assert any("Claude Code authentication failure" in a for a in report.actions)
    assert not any("autonomous cycle crashed:" in a for a in report.actions)

    # Filed as needs_human + blocking_agentra so the next cycle's
    # blocking_bugs() pre-flight check stops before repeating the failure.
    assert recorded_bug["needs_human"] is True
    assert recorded_bug["blocking_agentra"] is True


def test_autonomous_cycle_ordinary_crash_keeps_the_generic_diagnostic(tmp_path, monkeypatch):
    _common_monkeypatches(monkeypatch)

    async def _fake_query(*args, **kwargs):
        raise RuntimeError("some unrelated CLI failure")
        yield  # pragma: no cover

    monkeypatch.setattr(brain, "query", _fake_query)
    monkeypatch.setattr(Memory, "record_known_bug", lambda self, *a, **k: None)

    repo = tmp_path / "repo"
    repo.mkdir()

    report = asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))

    assert "autonomous cycle raised: some unrelated CLI failure" in report.final_message
    assert any("autonomous cycle crashed:" in a for a in report.actions)
