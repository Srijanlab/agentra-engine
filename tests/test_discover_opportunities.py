"""discover_opportunities (agents/brain/tools.py) -- had zero dedicated
coverage before this file. Covers the normal ranked-opportunities path and
the structured HUMAN_INPUT_REQUIRED routing through
Memory.record_known_bug(needs_human=True), distinct from the pre-existing
"no opportunities found" path (which still counts as a tool failure).

No real LLM call: discovery.run is monkeypatched.
"""

import asyncio
from pathlib import Path

from agentra import registry
from agentra.agents import brain
from agentra.agents.base import AgentResult
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory


def _session(tmp_path: Path, **overrides) -> brain.OrchestratorSession:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    defaults = dict(
        repo=repo,
        objective="test objective",
        env=EnvironmentConfig(),
        mem=Memory(repo),
        run_id="testrun1",
        cb_summary="a codebase summary",
    )
    defaults.update(overrides)
    return brain.OrchestratorSession(**defaults)


def _tool(session, name):
    tools = brain._tools_for(session)
    return next(t for t in tools if t.name == name)


def _patch_registry(monkeypatch):
    monkeypatch.setattr(registry, "record_agent_step", lambda *a, **k: None)
    # session.mark_waiting_for_human (human-in-the-loop escalation, GitHub
    # issue #34) writes the run's status through to the registry immediately
    # -- keep tests hermetic (no real writes under AGENTRA_HOME) the same way
    # record_agent_step is patched above.
    monkeypatch.setattr(registry, "record_run", lambda *a, **k: None)


def _stub_backlog(session, monkeypatch, in_progress=None, bugs=None, queue=None):
    monkeypatch.setattr(session.mem, "shipped_features", lambda: [])
    monkeypatch.setattr(session.mem, "in_progress_features", lambda: in_progress or [])
    monkeypatch.setattr(session.mem, "known_bugs", lambda: bugs or [])
    monkeypatch.setattr(session.mem, "feature_queue", lambda: queue or [])


def test_discover_opportunities_ranks_and_returns_opportunities_on_the_normal_path(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    _stub_backlog(session, monkeypatch)
    monkeypatch.setattr(session.mem, "record_feature_request", lambda *a, **k: {"number": 5, "html_url": "https://x/5"})

    async def fake_discovery_run(*a, **k):
        return AgentResult(
            ok=True, text="ok",
            json_data={"opportunities": [{"feature": "add_search", "description": "...", "impact": "high", "effort": "low", "reason": "...", "origin": "autonomous", "id": None}]},
            cost_usd=0.02, turns=3,
        )

    monkeypatch.setattr(brain.discovery, "run", fake_discovery_run)

    result = asyncio.run(_tool(session, "discover_opportunities").handler({}))

    assert result.get("is_error") is not True
    assert "add_search" in result["content"][0]["text"]
    assert session.tool_failure_counts.get("discover_opportunities", 0) == 0


def test_discover_opportunities_routes_human_input_required_through_record_known_bug(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    _stub_backlog(session, monkeypatch)

    async def fake_discovery_run(*a, **k):
        return AgentResult(
            ok=True, text="blocked",
            json_data={
                "status": "HUMAN_INPUT_REQUIRED",
                "reason": "Objective names two mutually exclusive directions.",
                "question": "Should we prioritize retention or acquisition?",
                "options": ["retention", "acquisition"],
            },
            cost_usd=0.01, turns=2,
        )

    monkeypatch.setattr(brain.discovery, "run", fake_discovery_run)
    known_bug_calls = []
    monkeypatch.setattr(
        session.mem,
        "record_known_bug",
        lambda run_id, severity, diagnosis, proposed_fix, **k: known_bug_calls.append((severity, diagnosis, k)),
    )

    result = asyncio.run(_tool(session, "discover_opportunities").handler({}))

    assert result.get("is_error") is True
    assert len(known_bug_calls) == 1
    severity, diagnosis, kwargs = known_bug_calls[0]
    assert kwargs["needs_human"] is True
    assert kwargs.get("blocking_agentra", False) is False
    assert "two mutually exclusive directions" in diagnosis
    assert "Should we prioritize retention or acquisition?" in diagnosis


def test_discover_opportunities_human_input_required_does_not_count_as_a_tool_failure(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    _stub_backlog(session, monkeypatch)

    async def fake_discovery_run(*a, **k):
        return AgentResult(ok=True, text="blocked", json_data={"status": "HUMAN_INPUT_REQUIRED", "reason": "needs a call"}, cost_usd=0.0, turns=1)

    monkeypatch.setattr(brain.discovery, "run", fake_discovery_run)
    monkeypatch.setattr(session.mem, "record_known_bug", lambda *a, **k: None)
    monkeypatch.setattr(
        session.mem, "record_failure", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called"))
    )

    asyncio.run(_tool(session, "discover_opportunities").handler({}))

    assert session.tool_failure_counts.get("discover_opportunities", 0) == 0


# -- backlog-empty auto-filing (issue #23) --------------------------------------------


def _fake_discovery_run_with_opportunities(*opportunities):
    async def fake_run(*a, **k):
        return AgentResult(ok=True, text="ok", json_data={"opportunities": list(opportunities)}, cost_usd=0.02, turns=3)

    return fake_run


_TOP_OPPORTUNITY = {
    "feature": "add_saved_searches",
    "description": "Let users save a search query and re-run it with one click.",
    "impact": "high",
    "effort": "low",
    "reason": "Several support tickets ask for this; low effort, high retention impact.",
    "origin": "autonomous",
    "id": None,
}
_SECOND_OPPORTUNITY = {
    "feature": "add_dark_mode",
    "description": "Add a dark color theme toggle.",
    "impact": "medium",
    "effort": "medium",
    "reason": "Frequently requested but lower urgency than saved searches.",
    "origin": "autonomous",
    "id": None,
}


def test_discover_opportunities_files_the_top_opportunity_when_backlog_is_empty(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    _stub_backlog(session, monkeypatch)  # in_progress=[], bugs=[], queue=[] -- genuinely empty
    monkeypatch.setattr(brain.discovery, "run", _fake_discovery_run_with_opportunities(_SECOND_OPPORTUNITY, _TOP_OPPORTUNITY))

    calls = []
    monkeypatch.setattr(
        session.mem, "record_feature_request",
        lambda description, **k: calls.append((description, k)) or {"number": 42, "html_url": "https://github.com/acme/app/issues/42"},
    )

    result = asyncio.run(_tool(session, "discover_opportunities").handler({}))

    assert result.get("is_error") is not True
    assert len(calls) == 1  # exactly one issue filed, regardless of how many opportunities were ranked
    description, kwargs = calls[0]
    # The ranked top pick (impact/effort score) is add_saved_searches, not
    # whichever happened to be listed first in the raw discovery output.
    assert kwargs["title"] == "add_saved_searches"
    assert kwargs["source"] == "github"
    assert kwargs["extra_labels"] == ["discovery"]
    assert "Let users save a search query and re-run it with one click." in description
    assert "Several support tickets ask for this" in description  # the "why", not just the what
    assert "#42" in result["content"][0]["text"]


def test_discover_opportunities_filed_issue_never_uses_the_bug_path(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    _stub_backlog(session, monkeypatch)
    monkeypatch.setattr(brain.discovery, "run", _fake_discovery_run_with_opportunities(_TOP_OPPORTUNITY))
    monkeypatch.setattr(session.mem, "record_feature_request", lambda *a, **k: {"number": 1, "html_url": "https://x/1"})
    monkeypatch.setattr(
        session.mem, "record_known_bug",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("a discovered opportunity must never be filed via record_known_bug")),
    )

    asyncio.run(_tool(session, "discover_opportunities").handler({}))


def test_discover_opportunities_does_not_file_when_the_feature_queue_is_non_empty(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    _stub_backlog(session, monkeypatch, queue=[{"external_id": "9", "feature": "existing queued feature"}])
    monkeypatch.setattr(brain.discovery, "run", _fake_discovery_run_with_opportunities(_TOP_OPPORTUNITY))
    monkeypatch.setattr(
        session.mem, "record_feature_request",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not file -- feature queue is non-empty")),
    )

    result = asyncio.run(_tool(session, "discover_opportunities").handler({}))

    assert result.get("is_error") is not True


def test_discover_opportunities_does_not_file_when_there_is_an_actionable_known_bug(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    _stub_backlog(session, monkeypatch, bugs=[{"external_id": "3", "diagnosis": "checkout crashes", "needs_human": False}])
    monkeypatch.setattr(brain.discovery, "run", _fake_discovery_run_with_opportunities(_TOP_OPPORTUNITY))
    monkeypatch.setattr(
        session.mem, "record_feature_request",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not file -- there is an actionable known bug")),
    )

    asyncio.run(_tool(session, "discover_opportunities").handler({}))


def test_discover_opportunities_files_despite_a_needs_human_bug_since_it_is_not_actionable(tmp_path, monkeypatch):
    # A bug already escalated to a human (needs_human=True) is excluded from
    # _actionable_bugs -- it must not itself count as "backlog not empty"
    # (same filtering check_backlog already applies).
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    _stub_backlog(session, monkeypatch, bugs=[{"external_id": "3", "diagnosis": "needs a human call", "needs_human": True}])
    monkeypatch.setattr(brain.discovery, "run", _fake_discovery_run_with_opportunities(_TOP_OPPORTUNITY))
    calls = []
    monkeypatch.setattr(session.mem, "record_feature_request", lambda *a, **k: calls.append(1) or {"number": 1, "html_url": "https://x/1"})

    asyncio.run(_tool(session, "discover_opportunities").handler({}))

    assert calls == [1]


def test_discover_opportunities_does_not_file_when_there_is_an_in_progress_feature(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    _stub_backlog(session, monkeypatch, in_progress=[{"external_id": "7", "description": "multi-part feature underway"}])
    monkeypatch.setattr(brain.discovery, "run", _fake_discovery_run_with_opportunities(_TOP_OPPORTUNITY))
    monkeypatch.setattr(
        session.mem, "record_feature_request",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not file -- an in-progress feature exists")),
    )

    asyncio.run(_tool(session, "discover_opportunities").handler({}))


def test_discover_opportunities_filing_failure_does_not_fail_the_tool_call(tmp_path, monkeypatch):
    # record_feature_request already handles "no repo remote"/GitHub errors
    # internally and returns None -- the tool call's own success must not
    # depend on the filing actually landing.
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    _stub_backlog(session, monkeypatch)
    monkeypatch.setattr(brain.discovery, "run", _fake_discovery_run_with_opportunities(_TOP_OPPORTUNITY))
    monkeypatch.setattr(session.mem, "record_feature_request", lambda *a, **k: None)

    result = asyncio.run(_tool(session, "discover_opportunities").handler({}))

    assert result.get("is_error") is not True


def test_discover_opportunities_does_not_file_when_active_feature_branches_exist(tmp_path, monkeypatch):
    """Don't re-file opportunities when there are active dev/ branches (work-in-progress)."""
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    _stub_backlog(session, monkeypatch)
    monkeypatch.setattr(brain.discovery, "run", _fake_discovery_run_with_opportunities(_TOP_OPPORTUNITY))
    
    # Simulate active feature branches by mocking git output
    def mock_git_branches(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=["git", "branch", "-a"],
            returncode=0,
            stdout="  dev/a365dcfd-human-in-the-loop-escalation\n  main\n  origin/main\n",
            stderr="",
        )
    
    import subprocess
    monkeypatch.setattr(subprocess, "run", mock_git_branches)
    monkeypatch.setattr(
        session.mem, "record_feature_request",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not file -- active feature branches exist")),
    )

    asyncio.run(_tool(session, "discover_opportunities").handler({}))
