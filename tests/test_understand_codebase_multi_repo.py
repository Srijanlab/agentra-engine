"""understand_codebase (agents/brain/tools.py) -- scans every code repo for a
multi-repo app instead of always session.repo, storing each repo's summary in
cb_summaries. No real LLM call: codebase.run_cached is monkeypatched.
"""

import asyncio
from pathlib import Path

from agentra.agents import brain
from agentra.agents.base import AgentResult
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory
from agentra.registry.core import RepoSpec


def _tool(session, name):
    tools = brain._tools_for(session)
    return next(t for t in tools if t.name == name)


def _session(tmp_path: Path, **overrides) -> brain.OrchestratorSession:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    defaults = dict(
        repo=repo,
        objective="test objective",
        env=EnvironmentConfig(),
        mem=Memory(repo),
        run_id="testrun1",
    )
    defaults.update(overrides)
    return brain.OrchestratorSession(**defaults)


def test_legacy_app_scans_session_repo_directly(tmp_path, monkeypatch):
    session = _session(tmp_path)
    calls = []

    async def fake_run_cached(repo, mem, cache_key="codebase"):
        calls.append((repo, cache_key))
        return AgentResult(ok=True, text="legacy summary", json_data=None, cost_usd=0.01, turns=1)

    monkeypatch.setattr(brain.codebase, "run_cached", fake_run_cached)

    result = asyncio.run(_tool(session, "understand_codebase").handler({}))

    assert result.get("is_error") is not True
    assert calls == [(session.repo, "codebase")]
    assert session.cb_summary == "legacy summary"


def test_multi_repo_app_scans_every_code_repo_with_namespaced_cache_keys(tmp_path, monkeypatch):
    engine_repo = tmp_path / "engine"
    ui_repo = tmp_path / "ui"
    engine_repo.mkdir()
    ui_repo.mkdir()
    session = _session(
        tmp_path,
        code_repos={
            "engine": RepoSpec(name="engine", path=engine_repo, repo_url=None, branch="main", role="code"),
            "ui": RepoSpec(name="ui", path=ui_repo, repo_url=None, branch="main", role="code"),
        },
    )
    calls = []

    async def fake_run_cached(repo, mem, cache_key="codebase"):
        calls.append((repo, cache_key))
        return AgentResult(ok=True, text=f"summary for {repo.name}", json_data=None, cost_usd=0.01, turns=1)

    monkeypatch.setattr(brain.codebase, "run_cached", fake_run_cached)

    result = asyncio.run(_tool(session, "understand_codebase").handler({}))

    assert result.get("is_error") is not True
    assert set(calls) == {(engine_repo, "codebase_engine"), (ui_repo, "codebase_ui")}
    assert session.cb_summaries == {"engine": "summary for engine", "ui": "summary for ui"}
    # Ambiguous with two repos -- cb_summary (the "active" mirror) is not
    # auto-picked; set_active_repo (called by implement_feature) sets it.
    assert session.cb_summary is None


def test_single_real_registered_repo_mirrors_into_cb_summary(tmp_path, monkeypatch):
    # A real registered single-repo app still has exactly one code_repos entry
    # (get_code_repos' legacy shim) -- no ambiguity, so cb_summary is set too,
    # exactly like a legacy/unregistered app.
    solo_repo = tmp_path / "solo"
    solo_repo.mkdir()
    session = _session(
        tmp_path,
        code_repos={"myapp": RepoSpec(name="myapp", path=solo_repo, repo_url=None, branch="main", role="coordination")},
    )

    async def fake_run_cached(repo, mem, cache_key="codebase"):
        return AgentResult(ok=True, text="solo summary", json_data=None, cost_usd=0.0, turns=1)

    monkeypatch.setattr(brain.codebase, "run_cached", fake_run_cached)

    asyncio.run(_tool(session, "understand_codebase").handler({}))

    assert session.cb_summaries == {"myapp": "solo summary"}
    assert session.cb_summary == "solo summary"


def test_one_repo_failing_does_not_fail_the_whole_call_if_another_succeeds(tmp_path, monkeypatch):
    engine_repo = tmp_path / "engine"
    ui_repo = tmp_path / "ui"
    engine_repo.mkdir()
    ui_repo.mkdir()
    session = _session(
        tmp_path,
        code_repos={
            "engine": RepoSpec(name="engine", path=engine_repo, repo_url=None, branch="main", role="code"),
            "ui": RepoSpec(name="ui", path=ui_repo, repo_url=None, branch="main", role="code"),
        },
    )

    async def fake_run_cached(repo, mem, cache_key="codebase"):
        if repo == engine_repo:
            return AgentResult(ok=False, text="scan failed", json_data=None, cost_usd=0.0, turns=1)
        return AgentResult(ok=True, text="ui summary", json_data=None, cost_usd=0.0, turns=1)

    monkeypatch.setattr(brain.codebase, "run_cached", fake_run_cached)

    result = asyncio.run(_tool(session, "understand_codebase").handler({}))

    assert result.get("is_error") is not True  # at least one repo succeeded
    assert session.cb_summaries == {"ui": "ui summary"}
