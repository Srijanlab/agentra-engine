"""GitHub issue #75 regression lock-in: #71/#73 duplicated #69/#72 because
implement_feature's resolves_id/resolves_origin arguments were silently
skippable even when check_backlog had just shown the exact item being
worked. Unlike #64's fix (a best-effort #<number>-in-prose fallback), this
closes the gap structurally: once check_backlog has shown any bug/feature-
queue item this cycle, implement_feature refuses to proceed unless the
caller either points at one (resolves_id) or explicitly declares the brief
isn't one of them (resolves_origin="new").
"""

import asyncio
import subprocess
from pathlib import Path

import pytest

from agentra import registry
from agentra.agents import brain
from agentra.agents.base import AgentResult
from agentra.connectors import github_fake
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory


@pytest.fixture(autouse=True)
def _stub_requirements(monkeypatch):
    async def fake_run(*a, **k):
        return AgentResult(ok=False, text="stubbed -- no spec", json_data=None, cost_usd=0.0, turns=0)

    monkeypatch.setattr(brain.requirements, "run", fake_run)


def _session(tmp_path: Path, mem: Memory) -> brain.OrchestratorSession:
    return brain.OrchestratorSession(
        repo=mem.repo,
        objective="test objective",
        env=EnvironmentConfig(),
        mem=mem,
        run_id="testrun1",
        cb_summary="a codebase summary",
    )


def _tool(session, name):
    tools = brain._tools_for(session)
    return next(t for t in tools if t.name == name)


def _patch_registry(monkeypatch):
    monkeypatch.setattr(registry, "record_agent_step", lambda *a, **k: None)
    monkeypatch.setattr(registry, "record_run", lambda *a, **k: None)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial commit")
    _git(repo, "remote", "add", "origin", "https://github.com/acme/repo.git")
    return repo


async def _fake_impl_run(*a, **k):
    return AgentResult(ok=True, text="done", json_data={"feature": "X", "status": "implemented"}, cost_usd=0.01, turns=1)


def test_implement_feature_blocked_when_backlog_shown_and_resolves_id_missing(tmp_path, monkeypatch):
    github_fake.install(monkeypatch=monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)
    repo_url = mem._repo_url()
    from agentra.connectors import github_issues

    github_issues.create_issue(repo_url, "Some bug", "body", labels=["bug", "agentra"])

    session = _session(tmp_path, mem)
    _patch_registry(monkeypatch)
    monkeypatch.setattr(brain.implementation, "run", _fake_impl_run)

    asyncio.run(_tool(session, "check_backlog").handler({}))
    result = asyncio.run(_tool(session, "implement_feature").handler({"feature_brief": "Unrelated new brief"}))

    assert result["is_error"] is True
    assert "resolves_id" in result["content"][0]["text"]


def test_implement_feature_allowed_with_explicit_new_origin(tmp_path, monkeypatch):
    github_fake.install(monkeypatch=monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)
    repo_url = mem._repo_url()
    from agentra.connectors import github_issues

    github_issues.create_issue(repo_url, "Some bug", "body", labels=["bug", "agentra"])

    session = _session(tmp_path, mem)
    _patch_registry(monkeypatch)
    monkeypatch.setattr(brain.implementation, "run", _fake_impl_run)
    monkeypatch.setattr(session.mem, "record_code_complete", lambda *a, **k: {"issue_number": None, "board_issue_number": None})
    monkeypatch.setattr(session.mem, "append_documentation", lambda *a, **k: None)

    asyncio.run(_tool(session, "check_backlog").handler({}))
    result = asyncio.run(_tool(session, "implement_feature").handler({
        "feature_brief": "Unrelated new brief", "resolves_origin": "new",
    }))

    assert result.get("is_error") is not True


def test_implement_feature_allowed_when_resolves_id_points_at_shown_bug(tmp_path, monkeypatch):
    github_fake.install(monkeypatch=monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)
    repo_url = mem._repo_url()
    from agentra.connectors import github_issues

    bug = github_issues.create_issue(repo_url, "Some bug", "body", labels=["bug", "agentra"])

    session = _session(tmp_path, mem)
    _patch_registry(monkeypatch)
    monkeypatch.setattr(brain.implementation, "run", _fake_impl_run)
    monkeypatch.setattr(session.mem, "record_code_complete", lambda *a, **k: {"issue_number": bug["number"], "board_issue_number": bug["number"]})
    monkeypatch.setattr(session.mem, "append_documentation", lambda *a, **k: None)
    monkeypatch.setattr(session.mem, "clear_known_bug", lambda *a, **k: None)

    asyncio.run(_tool(session, "check_backlog").handler({}))
    result = asyncio.run(_tool(session, "implement_feature").handler({
        "feature_brief": "Fix the bug", "resolves_id": str(bug["number"]), "resolves_origin": "known_bug",
    }))

    assert result.get("is_error") is not True


def test_implement_feature_not_gated_when_backlog_was_empty(tmp_path, monkeypatch):
    """No bugs/feature-queue items shown -- nothing to accidentally duplicate, so no gate."""
    github_fake.install(monkeypatch=monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)

    session = _session(tmp_path, mem)
    _patch_registry(monkeypatch)
    monkeypatch.setattr(brain.implementation, "run", _fake_impl_run)
    monkeypatch.setattr(session.mem, "record_code_complete", lambda *a, **k: {"issue_number": None, "board_issue_number": None})
    monkeypatch.setattr(session.mem, "append_documentation", lambda *a, **k: None)

    asyncio.run(_tool(session, "check_backlog").handler({}))
    result = asyncio.run(_tool(session, "implement_feature").handler({"feature_brief": "Brand new feature"}))

    assert result.get("is_error") is not True


def test_implement_feature_not_gated_when_check_backlog_never_called(tmp_path, monkeypatch):
    """discover_opportunities-only flow, no check_backlog call this turn -- nothing to enforce yet."""
    github_fake.install(monkeypatch=monkeypatch)
    repo = _make_repo(tmp_path)
    mem = Memory(repo)

    session = _session(tmp_path, mem)
    _patch_registry(monkeypatch)
    monkeypatch.setattr(brain.implementation, "run", _fake_impl_run)
    monkeypatch.setattr(session.mem, "record_code_complete", lambda *a, **k: {"issue_number": None, "board_issue_number": None})
    monkeypatch.setattr(session.mem, "append_documentation", lambda *a, **k: None)

    result = asyncio.run(_tool(session, "implement_feature").handler({"feature_brief": "Freshly discovered feature"}))

    assert result.get("is_error") is not True
