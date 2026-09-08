"""The per-repo `.agentra/` spec files kept live by their responsible agent:
Codebase Agent owns `architecture.md` + `design.md`; Testing Agent owns
`testing.md`'s `## Last run` block + `state.json.last_local_test`.
`.agentra/memory/architecture/testing-notes.md` stays exclusively human-owned
and is never touched here.
"""

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

from agentra.agents import codebase, testing
from agentra.agents.base import AgentResult
from agentra.memory import Memory


def _init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for args in (["init", "-b", "main"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "initial"], check=True, capture_output=True)
    return path


def _codebase_result(**data) -> AgentResult:
    payload = {"mode": "full", "architecture_md": "# r — Architecture\n## Purpose\nx", **data}
    return AgentResult(ok=True, text=f"```json\n{json.dumps(payload)}\n```", json_data=payload, cost_usd=0.05, turns=3)


def test_codebase_agent_writes_design_md_alongside_architecture(tmp_path, monkeypatch):
    repo = _init_git_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(codebase, "run", AsyncMock(return_value=_codebase_result(
        design_md="Hybrid GitHub-authoritative config with local fallback.")))

    asyncio.run(codebase.sync_spec(repo, mem, owner_repo_name="repo"))

    assert "## Purpose" in mem.read_spec("architecture")
    assert "Hybrid GitHub-authoritative" in mem.read_spec("design")


def test_codebase_agent_skips_design_md_when_field_absent(tmp_path, monkeypatch):
    repo = _init_git_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(codebase, "run", AsyncMock(return_value=_codebase_result(design_md="")))

    asyncio.run(codebase.sync_spec(repo, mem, owner_repo_name="repo"))

    assert mem.read_spec("design") is None


def test_testing_agent_records_last_run_on_success(tmp_path, monkeypatch):
    repo = _init_git_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(testing, "run_agent", AsyncMock(return_value=AgentResult(
        ok=True, text="...", cost_usd=0.02, turns=5,
        json_data={"status": "pass", "lint_status": "pass", "typecheck_status": "not_configured", "notes": "No e2e."},
    )))

    asyncio.run(testing.run_local(repo, "codebase summary", mem))

    assert mem.read_state()["last_local_test"]["status"] == "pass"
    assert "No e2e." in mem.read_state()["last_local_test"]["summary"]
    testing_md = mem.read_spec("testing")
    assert "## Last run" in testing_md and "status: pass" in testing_md


def test_testing_agent_writes_nothing_without_a_memory_instance(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(testing, "run_agent", AsyncMock(return_value=AgentResult(
        ok=True, text="...", json_data={"status": "pass"}, cost_usd=0.02, turns=5)))

    result = asyncio.run(testing.run_local(repo, "codebase summary"))

    assert result.ok is True
    assert not (repo / ".agentra").exists()


def test_testing_agent_writes_nothing_on_a_failed_run(tmp_path, monkeypatch):
    repo = _init_git_repo(tmp_path / "repo")
    mem = Memory(repo)
    monkeypatch.setattr(testing, "run_agent", AsyncMock(return_value=AgentResult(
        ok=False, text="agent error", json_data=None, cost_usd=0.0, turns=1)))

    asyncio.run(testing.run_local(repo, "codebase summary", mem))

    assert "last_local_test" not in mem.read_state()
    assert mem.read_spec("testing") is None


def test_run_local_does_not_touch_human_testing_notes(tmp_path, monkeypatch):
    """The human-authored architecture/testing-notes.md (Register/Edit App modal)
    must survive a run_local pass -- machine output goes to .agentra/testing.md."""
    repo = _init_git_repo(tmp_path / "repo")
    mem = Memory(repo)
    mem.write("architecture", "testing-notes", "human notes: run the manual smoke checklist too")
    monkeypatch.setattr(testing, "run_agent", AsyncMock(return_value=AgentResult(
        ok=True, text="...", cost_usd=0.02, turns=5,
        json_data={"status": "pass", "lint_status": "pass", "typecheck_status": "pass", "notes": "All green."},
    )))

    asyncio.run(testing.run_local(repo, "codebase summary", mem))

    assert mem.read("architecture", "testing-notes") == "human notes: run the manual smoke checklist too"
    assert "All green." in mem.read_spec("testing")
