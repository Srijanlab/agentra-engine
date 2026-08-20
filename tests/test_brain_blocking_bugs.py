"""Tests agents/brain.py's pre-flight check: run_autonomous_cycle must
refuse to start an autonomous cycle at all while a NON-auth bug is labeled
both "bug" and "blocking_agentra" (Memory.blocking_bugs()) -- see the
label-pair docstring in memory.py and the comment right above the check in
run_autonomous_cycle. This is deterministic Python gating the SDK's
query() call, not a system-prompt instruction, so it's tested the same
way -- no real LLM call should ever happen when a non-auth blocking bug is
open.

Costs nothing to run (query() is monkeypatched to fail loudly if called at
all) -- part of the normal pytest suite, unlike tests/test_safety_integration.py.

GitHub issue #42 (resumed): a Claude Code auth/login-failure blocking bug
is handled differently -- see the "self-healing" tests below -- since
whether it's still true is one cheap, verifiable Claude Code CLI call away,
unlike a bug like "403 Write access not granted" above.
"""

import asyncio

from claude_agent_sdk import ResultMessage

from agentra import registry
from agentra.agents import brain
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory


async def _fail_if_called(*args, **kwargs):
    raise AssertionError("query() must not be called while a blocking_agentra bug is open")


def test_run_autonomous_cycle_stops_immediately_on_a_blocking_bug(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "record_agent_step", lambda *a, **k: None)
    monkeypatch.setattr("agentra.agents.brain.deployment.persist_audit_trail", lambda *a, **k: None)
    monkeypatch.setattr(brain, "query", _fail_if_called)
    monkeypatch.setattr(
        Memory,
        "blocking_bugs",
        lambda self: [
            {
                "run_id": "7", "severity": "medium", "diagnosis": "403 Write access to repository not granted",
                "proposed_fix": "", "source": "github", "external_id": "7",
                "html_url": "https://github.com/acme/app/issues/7", "needs_human": True, "blocking_agentra": True,
            }
        ],
    )

    repo = tmp_path / "repo"
    repo.mkdir()

    report = asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))

    assert report.cost_usd == 0.0
    assert "#7" in report.final_message
    assert "blocking_agentra" in report.final_message
    assert any("blocked by open blocking_agentra bug" in a for a in report.actions)


def test_run_autonomous_cycle_does_not_short_circuit_without_a_blocking_bug(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "record_agent_step", lambda *a, **k: None)
    monkeypatch.setattr("agentra.agents.brain.deployment.persist_audit_trail", lambda *a, **k: None)
    monkeypatch.setattr(Memory, "blocking_bugs", lambda self: [])
    called = {"query": False}

    async def _fake_query(*args, **kwargs):
        called["query"] = True
        return
        yield  # pragma: no cover -- makes this an async generator, never reached

    monkeypatch.setattr(brain, "query", _fake_query)

    repo = tmp_path / "repo"
    repo.mkdir()

    asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))

    assert called["query"] is True


def _auth_blocking_bug(number: str = "9") -> dict:
    return {
        "run_id": number, "severity": "high",
        "diagnosis": "understand_codebase failed during an autonomous cycle",
        "proposed_fix": "Claude Code returned an error result: Not logged in · Please run /login (exit code: 1)",
        "source": "autonomous-failure", "external_id": number,
        "html_url": f"https://github.com/acme/app/issues/{number}", "needs_human": True, "blocking_agentra": True,
    }


def _result_message(cost: float = 0.01) -> ResultMessage:
    return ResultMessage(
        subtype="success", duration_ms=10, duration_api_ms=9, is_error=False, num_turns=1,
        session_id="s1", total_cost_usd=cost, result="done", terminal_reason="completed",
    )


def test_run_autonomous_cycle_attempts_anyway_with_only_an_auth_blocking_bug_open(tmp_path, monkeypatch):
    """GitHub issue #42 (resumed): unlike an ordinary blocking_agentra bug,
    an open auth-classified one must NOT hard-stop query() from ever being
    called -- it's cheap and unambiguous to just verify whether a human
    already re-authenticated since it was filed."""
    monkeypatch.setattr(registry, "record_agent_step", lambda *a, **k: None)
    monkeypatch.setattr("agentra.agents.brain.deployment.persist_audit_trail", lambda *a, **k: None)
    monkeypatch.setattr(Memory, "blocking_bugs", lambda self: [_auth_blocking_bug()])
    monkeypatch.setattr(Memory, "clear_resolved_auth_bugs", lambda self, run_id: [])
    called = {"query": False}

    async def _fake_query(*args, **kwargs):
        called["query"] = True
        yield _result_message()

    monkeypatch.setattr(brain, "query", _fake_query)

    repo = tmp_path / "repo"
    repo.mkdir()

    report = asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))

    assert called["query"] is True
    assert any("attempting this cycle anyway" in a for a in report.actions)


def test_run_autonomous_cycle_self_clears_an_auth_blocking_bug_on_a_successful_attempt(tmp_path, monkeypatch):
    """The self-heal payoff: this run got a real result back with no auth
    failure of its own, so the stale auth blocking bug gets auto-cleared --
    the core fix for issue #42 "keeps resurfacing on the backlog"."""
    monkeypatch.setattr(registry, "record_agent_step", lambda *a, **k: None)
    monkeypatch.setattr("agentra.agents.brain.deployment.persist_audit_trail", lambda *a, **k: None)
    monkeypatch.setattr(Memory, "blocking_bugs", lambda self: [_auth_blocking_bug()])
    cleared_calls = []
    monkeypatch.setattr(Memory, "clear_resolved_auth_bugs", lambda self, run_id: cleared_calls.append(run_id) or ["9"])

    async def _fake_query(*args, **kwargs):
        yield _result_message()

    monkeypatch.setattr(brain, "query", _fake_query)

    repo = tmp_path / "repo"
    repo.mkdir()

    report = asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))

    assert len(cleared_calls) == 1
    assert any("auto-cleared previously-blocking Claude Code auth-failure bug" in a for a in report.actions)
    assert "#9" in "".join(report.actions)


def test_run_autonomous_cycle_does_not_self_clear_when_the_same_auth_failure_recurs(tmp_path, monkeypatch):
    """If a human hasn't actually fixed credentials yet, a self-heal attempt
    just re-hits the identical failure -- must NOT clear the bug (it's not
    actually resolved), and must not call clear_resolved_auth_bugs at all
    since this run's own result proves nothing was fixed."""
    from claude_agent_sdk import ProcessError

    monkeypatch.setattr(registry, "record_agent_step", lambda *a, **k: None)
    monkeypatch.setattr("agentra.agents.brain.deployment.persist_audit_trail", lambda *a, **k: None)
    monkeypatch.setattr(Memory, "blocking_bugs", lambda self: [_auth_blocking_bug()])
    monkeypatch.setattr(Memory, "record_known_bug", lambda self, *a, **k: 9)
    monkeypatch.setattr(Memory, "_find_similar_open_bug", lambda self, diagnosis: "9")
    monkeypatch.setattr(
        Memory, "clear_resolved_auth_bugs",
        lambda self, run_id: (_ for _ in ()).throw(AssertionError("must not attempt to clear -- this run also failed")),
    )

    async def _fake_query(*args, **kwargs):
        raise ProcessError(
            "Claude Code returned an error result: Not logged in · Please run /login (exit code: 1)", exit_code=1
        )
        yield  # pragma: no cover

    monkeypatch.setattr(brain, "query", _fake_query)

    repo = tmp_path / "repo"
    repo.mkdir()

    report = asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))

    assert "not authenticated" in report.final_message


def test_run_autonomous_cycle_still_hard_stops_when_a_non_auth_blocking_bug_is_also_open(tmp_path, monkeypatch):
    """A mix of an auth-classified bug and an ordinary (non-self-healable)
    blocking bug must still hard-stop before attempting anything -- the
    non-auth bug takes precedence, unchanged from the original behavior."""
    monkeypatch.setattr(registry, "record_agent_step", lambda *a, **k: None)
    monkeypatch.setattr("agentra.agents.brain.deployment.persist_audit_trail", lambda *a, **k: None)
    monkeypatch.setattr(brain, "query", _fail_if_called)
    monkeypatch.setattr(
        Memory,
        "blocking_bugs",
        lambda self: [
            _auth_blocking_bug("9"),
            {
                "run_id": "7", "severity": "medium", "diagnosis": "403 Write access to repository not granted",
                "proposed_fix": "", "source": "github", "external_id": "7",
                "html_url": "https://github.com/acme/app/issues/7", "needs_human": True, "blocking_agentra": True,
            },
        ],
    )

    repo = tmp_path / "repo"
    repo.mkdir()

    report = asyncio.run(brain.run_autonomous_cycle(repo, "Ship useful features.", EnvironmentConfig()))

    assert report.cost_usd == 0.0
    assert "#7" in report.final_message
    assert "#9" not in report.final_message
