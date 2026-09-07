"""The brain preseed's SHA staleness gate: a code repo whose committed
`.agentra/state.json` indexed_sha matches HEAD costs zero LLM; one that moved
triggers codebase.sync_spec and joins session.stale_spec_repos.
"""

import asyncio
import subprocess
from pathlib import Path

from agentra import registry
from agentra.agents import brain
from agentra.agents.base import AgentResult
from agentra.environments import EnvironmentConfig
from agentra.memory import Memory
from agentra.memory.core import spec_header
from agentra.registry.core import RepoSpec


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for a in (["init", "-b", "main"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True)
    (path / "f.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True)
    return path


def _head(path: Path) -> str:
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def _run_preseed(coord: Path, code: dict, monkeypatch, sync_calls: list):
    async def fake_sync_spec(repo, mem, *, owner_repo_name, force_full=False):
        sync_calls.append(owner_repo_name)
        mem.write_spec("architecture", spec_header("agent:codebase", _head(repo)) + "# x\n## Purpose\ny")
        mem.write_state({"indexed_sha": _head(repo)})
        return AgentResult(ok=True, text="fresh summary", json_data=None, cost_usd=0.1, turns=2)

    monkeypatch.setattr(brain.codebase, "sync_spec", fake_sync_spec)
    monkeypatch.setattr(brain.deployment, "persist_repo_specs", lambda *a, **k: None)
    monkeypatch.setattr(brain, "query", _noop_query)
    monkeypatch.setattr(registry, "get_code_repos", lambda name: code)

    asyncio.run(brain.run_autonomous_cycle(coord, "obj", EnvironmentConfig(), app_name="agentra"))


async def _noop_query(prompt, options):
    async for _ in prompt:
        pass
    return
    yield  # pragma: no cover


def test_unchanged_sha_skips_sync(tmp_path, monkeypatch):
    coord = _git_repo(tmp_path / "coord")
    ui = _git_repo(tmp_path / "ui")
    mem = Memory(ui)
    mem.write_spec("architecture", spec_header("agent:codebase", _head(ui)) + "# ui\n## Purpose\nz")
    mem.write_state({"indexed_sha": _head(ui)})

    calls: list = []
    _run_preseed(coord, {"ui": RepoSpec("ui", ui, None, "main", "code")}, monkeypatch, calls)

    assert calls == []  # spec was current -> no scan


def test_moved_sha_triggers_sync_and_marks_stale(tmp_path, monkeypatch):
    coord = _git_repo(tmp_path / "coord")
    ui = _git_repo(tmp_path / "ui")
    mem = Memory(ui)
    mem.write_spec("architecture", spec_header("agent:codebase", "stale-sha") + "# ui\n## Purpose\nz")
    mem.write_state({"indexed_sha": "stale-sha"})

    calls: list = []
    _run_preseed(coord, {"ui": RepoSpec("ui", ui, None, "main", "code")}, monkeypatch, calls)

    assert calls == ["ui"]  # indexed_sha != HEAD -> a fresh sync ran
