"""implement_feature's target_repo resolution (agents/brain/tools.py), the core of
multi-repo Phase 2 step 3: for a multi-repo app, implement_feature must resolve which
code repo a feature belongs to -- via an explicit target_repo arg, a resolves_id
item's repo:<name> label (memory/core.py's _target_repo_label), or (with only one
code repo) an automatic default -- before doing any git/agent work, since
feature_branch_name/implementation.run/requirements.run all need the right repo's
path and EnvironmentConfig by then.

No real LLM call: implementation.run/requirements.run are monkeypatched.
"""

import asyncio
from pathlib import Path

import pytest

from agentra import registry
from agentra.agents import brain
from agentra.agents.base import AgentResult
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory
from agentra.registry.core import RepoSpec


@pytest.fixture(autouse=True)
def _stub_requirements(monkeypatch):
    async def fake_run(*a, **k):
        return AgentResult(ok=False, text="stubbed -- no spec", json_data=None, cost_usd=0.0, turns=0)

    monkeypatch.setattr(brain.requirements, "run", fake_run)


def _patch_registry(monkeypatch):
    monkeypatch.setattr(registry, "record_run", lambda *a, **k: None)
    monkeypatch.setattr(registry, "bind_loop", lambda *a, **k: "loop1")


def _fake_impl_result(**overrides) -> AgentResult:
    defaults = dict(ok=True, text="done", json_data={"feature": "A feature", "status": "implemented"}, cost_usd=0.01, turns=2)
    defaults.update(overrides)
    return AgentResult(**defaults)


def _tool(session, name):
    tools = brain._tools_for(session)
    return next(t for t in tools if t.name == name)


def _multi_repo_session(tmp_path: Path, **overrides) -> brain.OrchestratorSession:
    repo = tmp_path / "repo"
    engine_repo = tmp_path / "engine"
    ui_repo = tmp_path / "ui"
    repo.mkdir(exist_ok=True)
    engine_repo.mkdir(exist_ok=True)
    ui_repo.mkdir(exist_ok=True)
    defaults = dict(
        repo=repo,
        objective="test objective",
        env=EnvironmentConfig(),
        mem=Memory(repo),
        run_id="testrun1",
        code_repos={
            "engine": RepoSpec(name="engine", path=engine_repo, repo_url=None, branch="main", role="code"),
            "ui": RepoSpec(name="ui", path=ui_repo, repo_url=None, branch="main", role="code"),
        },
        cb_summaries={"engine": "engine summary", "ui": "ui summary"},
    )
    defaults.update(overrides)
    return brain.OrchestratorSession(**defaults)


def _stub_backlog(session, monkeypatch, bugs=None, queue=None):
    monkeypatch.setattr(session.mem, "known_bugs", lambda: bugs or [])
    monkeypatch.setattr(session.mem, "feature_queue", lambda: queue or [])
    monkeypatch.setattr(session.mem, "run_ids_for", lambda *a, **k: set())
    monkeypatch.setattr(session.mem, "record_in_progress_branch", lambda *a, **k: None)


def test_single_code_repo_needs_no_target_repo_arg(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    solo_repo = tmp_path / "solo"
    solo_repo.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    session = brain.OrchestratorSession(
        repo=repo, objective="obj", env=EnvironmentConfig(), mem=Memory(repo), run_id="r1",
        code_repos={"solo": RepoSpec(name="solo", path=solo_repo, repo_url=None, branch="main", role="coordination")},
        cb_summaries={"solo": "a summary"},
    )
    _stub_backlog(session, monkeypatch)
    captured = {}

    async def fake_impl_run(repo, *a, **k):
        captured["repo"] = repo
        return _fake_impl_result()

    monkeypatch.setattr(brain.implementation, "run", fake_impl_run)

    result = asyncio.run(_tool(session, "implement_feature").handler(
        {"feature_brief": "add a thing", "resolves_origin": "new"}
    ))

    assert result.get("is_error") is not True
    assert session.active_repo == "solo"
    assert captured["repo"] == solo_repo


def test_multi_repo_requires_target_repo_when_ambiguous(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _multi_repo_session(tmp_path)
    _stub_backlog(session, monkeypatch)

    result = asyncio.run(_tool(session, "implement_feature").handler(
        {"feature_brief": "add a thing", "resolves_origin": "new"}
    ))

    assert result.get("is_error") is True
    assert "target_repo" in result["content"][0]["text"]
    assert session.active_repo is None


def test_explicit_target_repo_arg_is_honored(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _multi_repo_session(tmp_path)
    _stub_backlog(session, monkeypatch)
    captured = {}

    async def fake_impl_run(repo, *a, **k):
        captured["repo"] = repo
        return _fake_impl_result()

    monkeypatch.setattr(brain.implementation, "run", fake_impl_run)

    result = asyncio.run(_tool(session, "implement_feature").handler(
        {"feature_brief": "add a thing", "resolves_origin": "new", "target_repo": "ui"}
    ))

    assert result.get("is_error") is not True
    assert session.active_repo == "ui"
    assert captured["repo"] == session.code_repos["ui"].path
    assert session.cb_summary == "ui summary"


def test_invalid_target_repo_is_refused(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _multi_repo_session(tmp_path)
    _stub_backlog(session, monkeypatch)

    result = asyncio.run(_tool(session, "implement_feature").handler(
        {"feature_brief": "add a thing", "resolves_origin": "new", "target_repo": "nope"}
    ))

    assert result.get("is_error") is True
    assert "nope" in result["content"][0]["text"]
    assert session.active_repo is None


def test_target_repo_defaults_to_the_resolves_id_items_repo_label(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _multi_repo_session(tmp_path)
    _stub_backlog(session, monkeypatch, bugs=[
        {"external_id": "9", "diagnosis": "a bug", "needs_human": False, "target_repo": "engine"},
    ])
    captured = {}

    async def fake_impl_run(repo, *a, **k):
        captured["repo"] = repo
        return _fake_impl_result()

    monkeypatch.setattr(brain.implementation, "run", fake_impl_run)

    result = asyncio.run(_tool(session, "implement_feature").handler(
        {"feature_brief": "fix the bug", "resolves_origin": "known_bug", "resolves_id": "9"}
    ))

    assert result.get("is_error") is not True
    assert session.active_repo == "engine"
    assert captured["repo"] == session.code_repos["engine"].path


def test_a_second_call_naming_a_different_repo_is_refused(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    session = _multi_repo_session(tmp_path)
    _stub_backlog(session, monkeypatch)

    async def fake_impl_run(repo, *a, **k):
        return _fake_impl_result()

    monkeypatch.setattr(brain.implementation, "run", fake_impl_run)

    first = asyncio.run(_tool(session, "implement_feature").handler(
        {"feature_brief": "first part", "resolves_origin": "new", "target_repo": "engine", "more_parts_expected": True}
    ))
    assert first.get("is_error") is not True

    second = asyncio.run(_tool(session, "implement_feature").handler(
        {"feature_brief": "second part", "resolves_origin": "new", "target_repo": "ui"}
    ))

    assert second.get("is_error") is True
    assert "engine" in second["content"][0]["text"]
    assert session.active_repo == "engine"  # unchanged
