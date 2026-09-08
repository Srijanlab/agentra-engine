"""Chat + standup *generation* are held on the engine (503) -- they run Claude
with a repo checkout, which the loop has and the engine (cloud mode) doesn't.
History reads stay on the engine.
"""

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from agentra import registry, server
from agentra.connectors import github_fake
from agentra.memory import Memory


def _register(tmp_path: Path, name: str = "myapp") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", f"https://github.com/acme/{name}.git"], check=True, capture_output=True)
    Memory(repo).set_objective("Ship things.")
    registry.register_app(name, str(repo), repo_url=f"https://github.com/acme/{name}.git", branch="main")
    return repo


def _isolate(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for attr, sub in [("AGENTRA_HOME", ""), ("APPS_PATH", "apps.json"), ("INBOX_ROOT", "inbox"),
                      ("PAUSE_PATH", "paused.json"), ("_RUNS_PATH", "runs.json")]:
        monkeypatch.setattr(registry, attr, home / sub if sub else home)
    monkeypatch.setattr(registry, "_ddb", None)
    server._active_runs.clear()
    github_fake.install(monkeypatch=monkeypatch)


def _c() -> TestClient:
    return TestClient(server.app)


def test_chat_turn_is_held(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _register(tmp_path)
    for path in ("/apps/myapp/agents/codebase/chat", "/apps/myapp/agents/codebase/chat/stream"):
        r = _c().post(path, json={"message": "hi"})
        assert r.status_code == 503
        assert "loop" in r.json()["detail"]


def test_chat_history_read_still_works(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _register(tmp_path)
    r = _c().get("/apps/myapp/agents/codebase/chat")
    assert r.status_code == 200 and r.json() == {"messages": []}


def test_standup_generation_is_held_but_latest_reads(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _register(tmp_path)
    assert _c().post("/apps/myapp/standup").status_code == 503
    assert _c().post("/standup/daily").status_code == 503
    latest = _c().get("/apps/myapp/standup/latest")
    assert latest.status_code == 200 and latest.json() == {"app": "myapp", "standup": None}


def test_slack_events_route_is_gone(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert _c().post("/slack/events", json={}).status_code == 404
