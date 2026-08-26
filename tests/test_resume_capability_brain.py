"""agents/brain.py's half of the resume-capability feature (see
memory.py's record_in_progress_branch/resume_branch_for and
implementation.py's resume=True checkout/always-push, both covered by
their own test files):

- check_backlog attaches a resume_branch field to each known-bug/
  feature-queue entry (brain._attach_resume_branches), computed
  concurrently rather than baked into Memory.known_bugs()/feature_queue()
  themselves (those are read on every dashboard poll).
- implement_feature records an In-Progress-Branch marker on whichever
  tracking issue this call names (resolves_id or sub_feature_of),
  regardless of whether the call itself succeeds -- an interrupted/
  partially-implemented call still pushed real work (see
  implementation.py's run()), which is exactly what needs to stay
  resumable.
- implement_feature's resume_branch arg is only honored on this run's
  first call (session.feature_branch is None), and threads through to
  implementation.run(resume=True).

No real LLM call: implementation.run and requirements.run are both
monkeypatched throughout (see the autouse _stub_requirements fixture --
implement_feature now calls Requirements Agent before Implementation Agent,
see test_requirements_agent.py for that behavior's own dedicated coverage;
here it's stubbed to a no-op "no spec" result so it doesn't interfere with
these tests' own concerns).
"""

import asyncio
import subprocess
from pathlib import Path

import pytest

from agentra import registry
from agentra.agents import brain
from agentra.agents.base import AgentResult
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory


@pytest.fixture(autouse=True)
def _stub_requirements(monkeypatch):
    async def fake_run(*a, **k):
        return AgentResult(ok=False, text="stubbed -- no spec", json_data=None, cost_usd=0.0, turns=0)

    monkeypatch.setattr(brain.requirements, "run", fake_run)


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


def _fake_impl_result(**overrides) -> AgentResult:
    defaults = dict(ok=True, text="done", json_data={"feature": "A feature", "status": "implemented"}, cost_usd=0.01, turns=2)
    defaults.update(overrides)
    return AgentResult(**defaults)


# -- check_backlog attaches resume_branch --------------------------------------------


def test_check_backlog_attaches_resume_branch_to_bugs_and_queue(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)

    monkeypatch.setattr(session.mem, "shipped_features", lambda: [])
    monkeypatch.setattr(session.mem, "in_progress_features", lambda: [])
    monkeypatch.setattr(
        session.mem,
        "known_bugs",
        lambda: [{"external_id": "13", "diagnosis": "sort order bug", "needs_human": False, "blocking_agentra": False}],
    )
    monkeypatch.setattr(session.mem, "feature_queue", lambda: [{"external_id": "20", "description": "a feature"}])
    monkeypatch.setattr(
        session.mem,
        "resume_branch_for",
        lambda external_id: "dev/abc123-fix-sort-order" if external_id == "13" else None,
    )

    result = asyncio.run(_tool(session, "check_backlog").handler({}))

    text = result["content"][0]["text"]
    assert "dev/abc123-fix-sort-order" in text
    assert '"resume_branch": null' in text  # the feature-queue entry has none


# -- implement_feature records the marker regardless of ok/fail ----------------------


def test_implement_feature_records_in_progress_branch_when_resolving_a_known_bug(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    async def fake_run(*a, **k):
        return _fake_impl_result()

    monkeypatch.setattr(brain.implementation, "run", fake_run)
    monkeypatch.setattr(session.mem, "record_code_complete", lambda *a, **k: {"issue_number": 13, "board_issue_number": 13})
    monkeypatch.setattr(session.mem, "append_documentation", lambda *a, **k: None)
    monkeypatch.setattr(session.mem, "clear_known_bug", lambda *a, **k: None)
    marker_calls = []
    monkeypatch.setattr(session.mem, "record_in_progress_branch", lambda issue_number, branch, run_id=None, session_id=None: marker_calls.append((issue_number, branch, run_id)))

    result = asyncio.run(
        _tool(session, "implement_feature").handler(
            {"feature_brief": "fix it", "resolves_origin": "known_bug", "resolves_id": "13", "sub_feature_of": "", "more_parts_expected": False}
        )
    )

    assert result.get("is_error") is not True
    assert marker_calls == [(13, session.feature_branch, session.run_id)]


def test_implement_feature_records_in_progress_branch_on_the_parent_when_continuing_a_multi_part_feature(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    async def fake_run(*a, **k):
        return _fake_impl_result()

    monkeypatch.setattr(brain.implementation, "run", fake_run)
    monkeypatch.setattr(session.mem, "record_code_complete", lambda *a, **k: {"issue_number": 21, "board_issue_number": 20})
    monkeypatch.setattr(session.mem, "append_documentation", lambda *a, **k: None)
    marker_calls = []
    monkeypatch.setattr(session.mem, "record_in_progress_branch", lambda issue_number, branch, run_id=None, session_id=None: marker_calls.append((issue_number, branch, run_id)))

    asyncio.run(
        _tool(session, "implement_feature").handler(
            {"feature_brief": "part 2", "resolves_origin": "", "resolves_id": "", "sub_feature_of": "20", "more_parts_expected": True}
        )
    )

    assert marker_calls == [(20, session.feature_branch, session.run_id)]


def test_implement_feature_records_in_progress_branch_even_when_implementation_fails(tmp_path, monkeypatch):
    # An interrupted/blocked call still pushed whatever it managed to commit
    # (see implementation.py's run()) -- this is exactly the case resume
    # exists for, so the marker must still be recorded.
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    async def fake_run(*a, **k):
        return _fake_impl_result(ok=False, text="blocked")

    monkeypatch.setattr(brain.implementation, "run", fake_run)
    monkeypatch.setattr(session.mem, "record_failure", lambda *a, **k: None)
    marker_calls = []
    monkeypatch.setattr(session.mem, "record_in_progress_branch", lambda issue_number, branch, run_id=None, session_id=None: marker_calls.append((issue_number, branch, run_id)))

    result = asyncio.run(
        _tool(session, "implement_feature").handler(
            {"feature_brief": "fix it", "resolves_origin": "known_bug", "resolves_id": "13", "sub_feature_of": "", "more_parts_expected": False}
        )
    )

    assert result.get("is_error") is True
    assert marker_calls == [(13, session.feature_branch, session.run_id)]


def test_implement_feature_links_the_commit_on_the_tracking_issue(tmp_path, monkeypatch):
    # A tracking issue's work can span more than one commit (multi-part
    # features, a resumed call, a self-heal fix-up) -- record_shipped's own
    # Shipped-Commit stamp only ever captures the last one, so every commit
    # gets linked as its own comment (see github_issues.record_commit).
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    subprocess.run(["git", "init", "-b", "main", str(session.repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(session.repo), "config", "user.email", "test@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(session.repo), "config", "user.name", "Test"], check=True, capture_output=True)
    (session.repo / "file.txt").write_text("content\n")
    subprocess.run(["git", "-C", str(session.repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(session.repo), "commit", "-m", "a commit"], check=True, capture_output=True)
    real_sha = subprocess.run(
        ["git", "-C", str(session.repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    async def fake_run(*a, **k):
        return _fake_impl_result()

    monkeypatch.setattr(brain.implementation, "run", fake_run)
    monkeypatch.setattr(session.mem, "record_code_complete", lambda *a, **k: {"issue_number": 13, "board_issue_number": 13})
    monkeypatch.setattr(session.mem, "append_documentation", lambda *a, **k: None)
    monkeypatch.setattr(session.mem, "clear_known_bug", lambda *a, **k: None)
    monkeypatch.setattr(session.mem, "record_in_progress_branch", lambda *a, **k: None)
    commit_calls = []
    monkeypatch.setattr(session.mem, "record_commit", lambda issue_number, commit_sha: commit_calls.append((issue_number, commit_sha)))

    asyncio.run(
        _tool(session, "implement_feature").handler(
            {"feature_brief": "fix it", "resolves_origin": "known_bug", "resolves_id": "13", "sub_feature_of": "", "more_parts_expected": False}
        )
    )

    assert commit_calls == [(13, real_sha)]


def test_implement_feature_links_the_commit_even_when_implementation_fails(tmp_path, monkeypatch):
    # implementation.py's _commit_if_dirty safety net runs even on a
    # failed/blocked turn -- a commit made this way must still get linked.
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    subprocess.run(["git", "init", "-b", "main", str(session.repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(session.repo), "config", "user.email", "test@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(session.repo), "config", "user.name", "Test"], check=True, capture_output=True)
    (session.repo / "file.txt").write_text("content\n")
    subprocess.run(["git", "-C", str(session.repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(session.repo), "commit", "-m", "partial work"], check=True, capture_output=True)
    real_sha = subprocess.run(
        ["git", "-C", str(session.repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    async def fake_run(*a, **k):
        return _fake_impl_result(ok=False, text="blocked")

    monkeypatch.setattr(brain.implementation, "run", fake_run)
    monkeypatch.setattr(session.mem, "record_failure", lambda *a, **k: None)
    monkeypatch.setattr(session.mem, "record_in_progress_branch", lambda *a, **k: None)
    commit_calls = []
    monkeypatch.setattr(session.mem, "record_commit", lambda issue_number, commit_sha: commit_calls.append((issue_number, commit_sha)))

    asyncio.run(
        _tool(session, "implement_feature").handler(
            {"feature_brief": "fix it", "resolves_origin": "known_bug", "resolves_id": "13", "sub_feature_of": "", "more_parts_expected": False}
        )
    )

    assert commit_calls == [(13, real_sha)]


# -- implement_feature routes HUMAN_INPUT_REQUIRED through record_known_bug ----------


def test_implement_feature_routes_human_input_required_through_record_known_bug(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)

    async def fake_run(*a, **k):
        return _fake_impl_result(
            json_data={
                "feature": "add_sso",
                "status": "HUMAN_INPUT_REQUIRED",
                "reason": "Two SSO providers are equally plausible with no clear default.",
                "question": "Which SSO provider should we integrate?",
                "options": ["Okta", "Auth0"],
            }
        )

    monkeypatch.setattr(brain.implementation, "run", fake_run)
    known_bug_calls = []
    monkeypatch.setattr(
        session.mem,
        "record_known_bug",
        lambda run_id, severity, diagnosis, proposed_fix, **k: known_bug_calls.append((severity, diagnosis, k)),
    )

    result = asyncio.run(
        _tool(session, "implement_feature").handler(
            {"feature_brief": "add SSO", "resolves_origin": "", "resolves_id": "", "sub_feature_of": "", "more_parts_expected": False}
        )
    )

    assert result.get("is_error") is True
    assert len(known_bug_calls) == 1
    severity, diagnosis, kwargs = known_bug_calls[0]
    assert kwargs["needs_human"] is True
    assert kwargs.get("blocking_agentra", False) is False
    assert "Two SSO providers are equally plausible" in diagnosis
    assert "Which SSO provider should we integrate?" in diagnosis
    assert "Okta" in diagnosis and "Auth0" in diagnosis


def test_implement_feature_human_input_required_still_records_in_progress_branch_and_commit(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    subprocess.run(["git", "init", "-b", "main", str(session.repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(session.repo), "config", "user.email", "test@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(session.repo), "config", "user.name", "Test"], check=True, capture_output=True)
    (session.repo / "file.txt").write_text("content\n")
    subprocess.run(["git", "-C", str(session.repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(session.repo), "commit", "-m", "partial work"], check=True, capture_output=True)
    real_sha = subprocess.run(
        ["git", "-C", str(session.repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    async def fake_run(*a, **k):
        return _fake_impl_result(json_data={"feature": "x", "status": "HUMAN_INPUT_REQUIRED", "reason": "needs a call"})

    monkeypatch.setattr(brain.implementation, "run", fake_run)
    monkeypatch.setattr(session.mem, "record_known_bug", lambda *a, **k: None)
    marker_calls = []
    monkeypatch.setattr(session.mem, "record_in_progress_branch", lambda issue_number, branch, run_id=None, session_id=None: marker_calls.append((issue_number, branch, run_id)))
    commit_calls = []
    monkeypatch.setattr(session.mem, "record_commit", lambda issue_number, commit_sha: commit_calls.append((issue_number, commit_sha)))

    asyncio.run(
        _tool(session, "implement_feature").handler(
            {"feature_brief": "fix it", "resolves_origin": "known_bug", "resolves_id": "13", "sub_feature_of": "", "more_parts_expected": False}
        )
    )

    assert marker_calls == [(13, session.feature_branch, session.run_id)]
    assert commit_calls == [(13, real_sha)]


def test_implement_feature_human_input_required_does_not_touch_the_failure_counter(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)

    async def fake_run(*a, **k):
        return _fake_impl_result(json_data={"feature": "x", "status": "HUMAN_INPUT_REQUIRED", "reason": "needs a call"})

    monkeypatch.setattr(brain.implementation, "run", fake_run)
    monkeypatch.setattr(session.mem, "record_known_bug", lambda *a, **k: None)
    monkeypatch.setattr(
        session.mem, "record_failure", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called"))
    )

    asyncio.run(
        _tool(session, "implement_feature").handler(
            {"feature_brief": "my own idea", "resolves_origin": "", "resolves_id": "", "sub_feature_of": "", "more_parts_expected": False}
        )
    )

    assert session.tool_failure_counts.get("implement_feature", 0) == 0


def test_implement_feature_does_not_record_a_marker_for_a_self_initiated_idea(tmp_path, monkeypatch):
    # No resolves_id/sub_feature_of -- no backlog entry exists to attach a marker to.
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    async def fake_run(*a, **k):
        return _fake_impl_result()

    monkeypatch.setattr(brain.implementation, "run", fake_run)
    monkeypatch.setattr(session.mem, "record_code_complete", lambda *a, **k: {"issue_number": 30, "board_issue_number": 30})
    monkeypatch.setattr(session.mem, "append_documentation", lambda *a, **k: None)
    monkeypatch.setattr(
        session.mem, "record_in_progress_branch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called"))
    )

    asyncio.run(
        _tool(session, "implement_feature").handler(
            {"feature_brief": "my own idea", "resolves_origin": "", "resolves_id": "", "sub_feature_of": "", "more_parts_expected": False}
        )
    )


# -- resume_branch arg: only honored on the first call this run, threads to resume= --


def test_implement_feature_resume_branch_becomes_the_session_feature_branch(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path)
    calls = []

    async def fake_run(repo, objective, brief, cb_summary, env, feature_branch, resume=False, spec="", session_id=None):
        calls.append((feature_branch, resume))
        return _fake_impl_result()

    monkeypatch.setattr(brain.implementation, "run", fake_run)
    monkeypatch.setattr(session.mem, "record_code_complete", lambda *a, **k: {"issue_number": 13, "board_issue_number": 13})
    monkeypatch.setattr(session.mem, "append_documentation", lambda *a, **k: None)
    monkeypatch.setattr(session.mem, "clear_known_bug", lambda *a, **k: None)
    monkeypatch.setattr(session.mem, "record_in_progress_branch", lambda *a, **k: None)

    asyncio.run(
        _tool(session, "implement_feature").handler(
            {
                "feature_brief": "resume it", "resolves_origin": "known_bug", "resolves_id": "13",
                "sub_feature_of": "", "more_parts_expected": False, "resume_branch": "dev/abc123-fix-sort-order",
            }
        )
    )

    assert session.feature_branch == "dev/abc123-fix-sort-order"
    assert calls == [("dev/abc123-fix-sort-order", True)]


def test_implement_feature_resume_branch_ignored_on_a_later_call_this_run(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _session(tmp_path, feature_branch="dev/already-set")
    calls = []

    async def fake_run(repo, objective, brief, cb_summary, env, feature_branch, resume=False, spec="", session_id=None):
        calls.append((feature_branch, resume))
        return _fake_impl_result()

    monkeypatch.setattr(brain.implementation, "run", fake_run)
    monkeypatch.setattr(session.mem, "record_code_complete", lambda *a, **k: {"issue_number": 20, "board_issue_number": 20})
    monkeypatch.setattr(session.mem, "append_documentation", lambda *a, **k: None)
    monkeypatch.setattr(session.mem, "record_in_progress_branch", lambda *a, **k: None)

    asyncio.run(
        _tool(session, "implement_feature").handler(
            {
                "feature_brief": "part 2", "resolves_origin": "", "resolves_id": "",
                "sub_feature_of": "20", "more_parts_expected": False, "resume_branch": "dev/some-other-branch",
            }
        )
    )

    # session.feature_branch was already set -- resume_branch must not override it.
    assert session.feature_branch == "dev/already-set"
    assert calls == [("dev/already-set", False)]
