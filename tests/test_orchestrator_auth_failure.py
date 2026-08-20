"""orchestrator.py::run_cycle -- the deterministic fixed-pipeline
orchestration mode (not the default, but still a real path every
agent-subprocess call funnels through the same agents/base.py::run_agent as
agents/brain.py's default mode -- see that module's own docstring).

GitHub issue #42: previously the pipeline's very first agent-subprocess call
(understand_codebase) was the one step whose failure was never reported
through Memory.record_failure at all -- a Claude Code auth/login failure
there would silently abort the cycle with no bug filed and no Slack
escalation, then repeat identically forever on the next run_cycle call.
Every other step (discovery, implementation, testing, deployment, pre-prod
verification) already called mem.record_failure, so they already got the
Slack escalation for free the moment Memory.record_failure itself learned
to send it (see test_failure_triage.py) -- this file only needs to cover
the one gap that was actually fixed here.

No real LLM call: codebase.run_cached is monkeypatched.
"""

import asyncio
from pathlib import Path

from agentra import orchestrator
from agentra.agents.base import AgentResult
from agentra.connectors import slack
from agentra.memory import Memory


def test_run_cycle_reports_a_codebase_auth_failure_via_record_failure_and_slack(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    async def fake_run_cached(*a, **k):
        return AgentResult(
            ok=False,
            text=(
                "Claude Code authentication failure -- the CLI reported it is not usable on this "
                "runner (Claude Code returned an error result: Not logged in · Please run /login "
                "(exit code: 1))."
            ),
            json_data=None, cost_usd=0.0, turns=0, auth_failure=True,
        )

    monkeypatch.setattr(orchestrator.codebase, "run_cached", fake_run_cached)
    bug_calls = []
    monkeypatch.setattr(Memory, "record_known_bug", lambda self, *a, **k: bug_calls.append(k) or 1)
    monkeypatch.setattr(Memory, "issue_html_url", lambda self, n: f"https://github.com/acme/app/issues/{n}")
    slack_calls = []
    monkeypatch.setattr(slack, "notify_human_input_required", lambda **k: slack_calls.append(k) or True)

    report = asyncio.run(orchestrator.run_cycle(repo, "Ship useful features."))

    assert report.codebase_ok is False
    assert "aborting cycle" in report.summary

    # Filed as needs_human + blocking_agentra -- the next run_cycle call
    # would refuse (or at minimum re-report distinctly) rather than the
    # failure vanishing silently, matching every other step's behavior.
    assert len(bug_calls) == 1
    assert bug_calls[0]["needs_human"] is True
    assert bug_calls[0]["blocking_agentra"] is True

    # And, since Memory.record_failure now sends it for any login failure
    # regardless of which orchestration mode/step detected it, the same
    # Slack human-in-the-loop escalation fires here too.
    assert len(slack_calls) == 1
    assert slack_calls[0]["question"] == "Claude Code session is not authenticated on this runner -- run /login and re-trigger."


def test_run_cycle_ordinary_codebase_failure_does_not_notify_slack(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    async def fake_run_cached(*a, **k):
        return AgentResult(ok=False, text="agent turn raised: some transient tool error", json_data=None, cost_usd=0.0, turns=0)

    monkeypatch.setattr(orchestrator.codebase, "run_cached", fake_run_cached)
    monkeypatch.setattr(Memory, "record_known_bug", lambda self, *a, **k: 1)
    monkeypatch.setattr(
        slack, "notify_human_input_required", lambda **k: (_ for _ in ()).throw(AssertionError("must not notify"))
    )

    report = asyncio.run(orchestrator.run_cycle(repo, "Ship useful features."))

    assert report.codebase_ok is False
