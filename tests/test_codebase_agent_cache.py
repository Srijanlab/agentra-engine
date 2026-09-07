"""agents/codebase.py `sync_spec` — the Codebase Agent's repo scan is a real
multi-turn LLM call, so it must run only when a repo's HEAD moved since
`.agentra/state.json`'s `indexed_sha` (then a bounded *delta*), and serve the
committed spec at cost 0 otherwise.

Real local git repo on disk (`git init` + commits) — the point is real git
behavior, not a mock.
"""

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

from agentra.agents import codebase
from agentra.agents.base import AgentResult
from agentra.memory import Memory


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial commit")
    return path


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _fake_result(arch: str, mode: str = "full") -> AgentResult:
    body = json.dumps({
        "mode": mode, "architecture_md": arch, "design_md": "uses X pattern",
        "test_commands": ["pytest"], "build_commands": [],
    })
    return AgentResult(ok=True, text=f"```json\n{body}\n```", json_data=json.loads(body), cost_usd=0.05, turns=3)


def test_first_call_does_a_full_scan_and_writes_the_spec(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    mock_run = AsyncMock(return_value=_fake_result("# repo — Architecture\n## Purpose\nx"))
    monkeypatch.setattr(codebase, "run", mock_run)

    result = asyncio.run(codebase.sync_spec(repo, mem, owner_repo_name="repo"))

    assert mock_run.await_count == 1
    assert mock_run.await_args.kwargs["mode"] == "full"
    assert "## Purpose" in result.text
    assert "## Purpose" in (mem.read_spec("architecture") or "")
    assert mem.read_spec("design") is not None
    assert mem.indexed_sha() == _head(repo)


def test_unchanged_head_serves_the_cache_at_zero_cost(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    mock_run = AsyncMock(return_value=_fake_result("# repo — Architecture\n## Purpose\nx"))
    monkeypatch.setattr(codebase, "run", mock_run)

    first = asyncio.run(codebase.sync_spec(repo, mem, owner_repo_name="repo"))
    second = asyncio.run(codebase.sync_spec(repo, mem, owner_repo_name="repo"))

    assert mock_run.await_count == 1
    assert second.cost_usd == 0.0 and second.turns == 0 and second.ok
    assert second.text == first.text


def test_a_new_commit_triggers_a_delta_update(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    mock_run = AsyncMock(side_effect=[
        _fake_result("# repo — Architecture\n## Purpose\nv1"),
        _fake_result("# repo — Architecture\n## Purpose\nv2", mode="delta"),
    ])
    monkeypatch.setattr(codebase, "run", mock_run)

    asyncio.run(codebase.sync_spec(repo, mem, owner_repo_name="repo"))
    (repo / "feature.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add feature")

    second = asyncio.run(codebase.sync_spec(repo, mem, owner_repo_name="repo"))

    assert mock_run.await_count == 2
    assert mock_run.await_args.kwargs["mode"] == "delta"
    assert mock_run.await_args.kwargs.get("diff")  # the diff was passed
    assert "v2" in (mem.read_spec("architecture") or "")
    assert mem.indexed_sha() == _head(repo)


def test_force_full_bypasses_the_sha_gate(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    mock_run = AsyncMock(side_effect=[
        _fake_result("# repo — Architecture\n## Purpose\nv1"),
        _fake_result("# repo — Architecture\n## Purpose\nv1-rebuilt"),
    ])
    monkeypatch.setattr(codebase, "run", mock_run)

    asyncio.run(codebase.sync_spec(repo, mem, owner_repo_name="repo"))
    second = asyncio.run(codebase.sync_spec(repo, mem, owner_repo_name="repo", force_full=True))

    assert mock_run.await_count == 2
    assert mock_run.await_args.kwargs["mode"] == "full"
    assert "v1-rebuilt" in (mem.read_spec("architecture") or "")


def test_a_failed_scan_writes_nothing(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    failed = AgentResult(ok=False, text="agent error", json_data=None, cost_usd=0.01, turns=1)
    monkeypatch.setattr(codebase, "run", AsyncMock(return_value=failed))

    result = asyncio.run(codebase.sync_spec(repo, mem, owner_repo_name="repo"))

    assert result.ok is False
    assert mem.read_spec("architecture") is None
    assert mem.indexed_sha() is None


def test_sync_spec_never_touches_codegraph(tmp_path, monkeypatch):
    """codegraph is a runtime navigation tool, not a spec input — sync_spec must
    not call it (it used to append the graph digest to the summary)."""
    repo = _init_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(codebase, "run", AsyncMock(return_value=_fake_result("# x\n## Purpose\ny")))
    import agentra.agents.codegraph as codegraph_mod
    monkeypatch.setattr(codegraph_mod, "load_or_build", lambda *a, **k: (_ for _ in ()).throw(AssertionError("codegraph called")))

    result = asyncio.run(codebase.sync_spec(repo, mem, owner_repo_name="repo"))
    assert result.ok
