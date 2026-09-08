"""understand_codebase (agents/brain/tools.py) -- forces a fresh per-repo spec
sync for every code repo of a registered app. No real LLM call: codebase.sync_spec
is monkeypatched.
"""

import asyncio
from pathlib import Path

from agentra.agents import brain
from agentra.agents.base import AgentResult
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory
from agentra.registry.core import RepoSpec


def _tool(session, name):
    return next(t for t in brain._tools_for(session) if t.name == name)


def _session(tmp_path: Path, **overrides) -> brain.OrchestratorSession:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    defaults = dict(repo=repo, objective="test objective", env=EnvironmentConfig(), mem=Memory(repo), run_id="testrun1")
    defaults.update(overrides)
    return brain.OrchestratorSession(**defaults)


def test_unregistered_app_syncs_session_repo(tmp_path, monkeypatch):
    session = _session(tmp_path)
    calls = []

    async def fake_sync_spec(repo, mem, *, owner_repo_name, force_full=False):
        calls.append((repo, owner_repo_name, force_full))
        return AgentResult(ok=True, text="legacy summary", json_data=None, cost_usd=0.01, turns=1)

    monkeypatch.setattr(brain.codebase, "sync_spec", fake_sync_spec)
    result = asyncio.run(_tool(session, "understand_codebase").handler({}))

    assert result.get("is_error") is not True
    assert calls == [(session.repo, session.repo.name, True)]
    assert session.cb_summary == "legacy summary"


def test_multi_repo_app_syncs_every_code_repo(tmp_path, monkeypatch):
    engine_repo, ui_repo = tmp_path / "engine", tmp_path / "ui"
    engine_repo.mkdir()
    ui_repo.mkdir()
    session = _session(tmp_path, code_repos={
        "engine": RepoSpec(name="engine", path=engine_repo, repo_url=None, branch="main", role="code"),
        "ui": RepoSpec(name="ui", path=ui_repo, repo_url=None, branch="main", role="code"),
    })
    calls = []

    async def fake_sync_spec(repo, mem, *, owner_repo_name, force_full=False):
        calls.append((owner_repo_name, force_full))
        return AgentResult(ok=True, text=f"summary for {owner_repo_name}", json_data=None, cost_usd=0.01, turns=1)

    monkeypatch.setattr(brain.codebase, "sync_spec", fake_sync_spec)
    result = asyncio.run(_tool(session, "understand_codebase").handler({}))

    assert result.get("is_error") is not True
    assert set(calls) == {("engine", True), ("ui", True)}
    assert session.cb_summaries == {"engine": "summary for engine", "ui": "summary for ui"}
    assert session.stale_spec_repos == {"engine", "ui"}
    assert session.cb_summary is None  # ambiguous with two repos


def test_single_registered_repo_mirrors_into_cb_summary(tmp_path, monkeypatch):
    solo_repo = tmp_path / "solo"
    solo_repo.mkdir()
    session = _session(tmp_path, code_repos={
        "myapp": RepoSpec(name="myapp", path=solo_repo, repo_url=None, branch="main", role="coordination"),
    })

    async def fake_sync_spec(repo, mem, *, owner_repo_name, force_full=False):
        return AgentResult(ok=True, text="solo summary", json_data=None, cost_usd=0.0, turns=1)

    monkeypatch.setattr(brain.codebase, "sync_spec", fake_sync_spec)
    asyncio.run(_tool(session, "understand_codebase").handler({}))

    assert session.cb_summaries == {"myapp": "solo summary"}
    assert session.cb_summary == "solo summary"


def test_one_repo_failing_does_not_fail_the_whole_call(tmp_path, monkeypatch):
    engine_repo, ui_repo = tmp_path / "engine", tmp_path / "ui"
    engine_repo.mkdir()
    ui_repo.mkdir()
    session = _session(tmp_path, code_repos={
        "engine": RepoSpec(name="engine", path=engine_repo, repo_url=None, branch="main", role="code"),
        "ui": RepoSpec(name="ui", path=ui_repo, repo_url=None, branch="main", role="code"),
    })

    async def fake_sync_spec(repo, mem, *, owner_repo_name, force_full=False):
        if owner_repo_name == "engine":
            return AgentResult(ok=False, text="scan failed", json_data=None, cost_usd=0.0, turns=1)
        return AgentResult(ok=True, text="ui summary", json_data=None, cost_usd=0.0, turns=1)

    monkeypatch.setattr(brain.codebase, "sync_spec", fake_sync_spec)
    result = asyncio.run(_tool(session, "understand_codebase").handler({}))

    assert result.get("is_error") is not True
    assert session.cb_summaries == {"ui": "ui summary"}
    assert session.stale_spec_repos == {"ui"}
