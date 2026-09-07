"""GET /apps digest reports the same per-app environment config as GET /apps/{name},
even when the stored registration-time repo_path is stale (issue #6)."""

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from agentra import environments, registry, server
from agentra.connectors import github_fake


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_origin(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text(f"{path.name}\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")
    return path


def _isolate_registry(tmp_path, monkeypatch):
    home = tmp_path / "agentra_home"
    monkeypatch.setattr(registry, "_db", None)
    monkeypatch.setattr(registry, "_ddb", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "APPS_PATH", home / "apps.json")
    monkeypatch.setattr(registry, "INBOX_ROOT", home / "inbox")
    monkeypatch.setattr(registry, "PAUSE_PATH", home / "paused.json")
    monkeypatch.setattr(registry, "REPOS_ROOT", tmp_path / "repos")
    monkeypatch.setattr(registry, "_RUNS_PATH", home / "runs.json")
    monkeypatch.setattr(registry, "_LOOPS_PATH", home / "loops.json")
    monkeypatch.setattr(registry, "_AGENT_STEPS_PATH", home / "agent_steps.jsonl")
    server._active_runs.clear()
    server._app_locks.clear()
    github_fake.install(monkeypatch=monkeypatch)


def test_list_apps_digest_matches_detail_when_repo_path_is_stale(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    client = TestClient(server.app)

    origin = _init_origin(tmp_path / "cap-origin")
    resp = client.post("/apps", json={
        "name": "cap", "repo_url": str(origin), "branch": "main", "objective": "Ship CAP.",
    })
    assert resp.status_code == 200, resp.text

    # issue #6: the stored repo_path is the registration-time path from a host that
    # no longer exists; only the REPOS_ROOT checkout is real on this host.
    stale_path = str(tmp_path / "foreign-host" / "repos" / "ContentAutomationPlatform")
    assert not Path(stale_path).exists()
    apps = registry.core._local_apps()
    apps["cap"]["repo_path"] = stale_path
    registry.core._local_save_apps(apps)

    resolved = registry.REPOS_ROOT / "cap"
    non_default = environments.EnvironmentConfig(
        schedule_hours=72.0, alarm_enabled=False,
        pre_prod_branch="staging", prod_branch="release",
    )

    def fake_load(repo):
        if repo is not None and Path(repo) == resolved:
            return non_default
        return None

    monkeypatch.setattr(environments, "load", fake_load)

    digest = client.get("/apps")
    assert digest.status_code == 200
    entry = digest.json()["apps"]["cap"]
    assert "digest_error" not in entry

    detail = client.get("/apps/cap")
    assert detail.status_code == 200
    detail_body = detail.json()

    for field in ("schedule_hours", "alarm_enabled", "pre_prod_branch", "prod_branch", "objective"):
        assert entry[field] == detail_body[field], field
    assert entry["schedule_hours"] == 72.0
    assert entry["alarm_enabled"] is False
    assert entry["pre_prod_branch"] == "staging"
    assert entry["prod_branch"] == "release"


def test_list_apps_stays_200_when_one_app_digest_fails(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    client = TestClient(server.app)

    origin = _init_origin(tmp_path / "ok-origin")
    assert client.post("/apps", json={"name": "ok", "repo_url": str(origin), "branch": "main"}).status_code == 200

    def boom(repo):
        raise RuntimeError("environment config backend is down")

    monkeypatch.setattr(environments, "load", boom)

    resp = client.get("/apps")
    assert resp.status_code == 200
    entry = resp.json()["apps"]["ok"]
    assert entry["digest_error"] is True
    for field in ("schedule_hours", "alarm_enabled", "pre_prod_branch", "prod_branch"):
        assert field in entry
