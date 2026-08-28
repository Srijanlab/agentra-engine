"""GitHub #113 (/healthz alias) and #112 (mask stale machine-generated testing-notes on read)."""

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from agentra import registry, server
from agentra.connectors import github_fake
from agentra.memory import Memory
from agentra.server.routes.apps import _mask_stale_machine_testing_notes


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _isolate_registry(tmp_path, monkeypatch):
    home = tmp_path / "agentra_home"
    monkeypatch.setattr(registry, "_db", None)
    monkeypatch.setattr(registry, "AGENTRA_HOME", home)
    monkeypatch.setattr(registry, "APPS_PATH", home / "apps.json")
    monkeypatch.setattr(registry, "INBOX_ROOT", home / "inbox")
    monkeypatch.setattr(registry, "PAUSE_PATH", home / "paused.json")
    monkeypatch.setattr(registry, "_RUNS_PATH", home / "runs.json")
    monkeypatch.setattr(registry, "_AGENT_STEPS_PATH", home / "agent_steps.jsonl")
    server._active_runs.clear()
    server._app_locks.clear()
    github_fake.install(monkeypatch=monkeypatch)


def _register_tmp_app(tmp_path: Path, name: str = "myapp") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial commit")
    repo_url = f"https://github.com/acme/{name}.git"
    _git(repo, "remote", "add", "origin", repo_url)
    Memory(repo).set_objective("Ship useful dashboard improvements.")
    registry.register_app(name, str(repo), repo_url=repo_url, branch="main")
    return repo


# -- #113: /healthz alias -----------------------------------------------------


def test_healthz_is_a_pure_alias_of_health(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    _register_tmp_app(tmp_path)
    client = TestClient(server.app)

    health = client.get("/health")
    healthz = client.get("/healthz")

    assert health.status_code == healthz.status_code == 200
    assert health.headers["content-type"] == healthz.headers["content-type"] == "application/json"
    assert health.json() == healthz.json()
    body = healthz.json()
    assert body["status"] == "ok"
    assert isinstance(body["apps_registered"], int)
    assert set(body) == {"status", "apps_registered"}


def test_health_body_unchanged(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    client = TestClient(server.app)
    assert client.get("/health").json() == {"status": "ok", "apps_registered": 0}


# -- #112: sanitization helper ----------------------------------------------


def test_mask_machine_shape_with_notes_line():
    content = "Lint: not_configured\nTypecheck: pass\nNotes: Verified the actual feature works."
    assert _mask_stale_machine_testing_notes(content) is None


def test_mask_machine_shape_without_notes_line():
    assert _mask_stale_machine_testing_notes("Lint: pass\nTypecheck: fail") is None


def test_mask_machine_shape_with_surrounding_whitespace_and_blank_lines():
    assert _mask_stale_machine_testing_notes("\n  Lint: unknown\n\nTypecheck: not_configured\n") is None


def test_human_text_mentioning_lint_is_not_masked():
    content = "Run the manual smoke checklist. Check that lint and typecheck pass in CI first."
    assert _mask_stale_machine_testing_notes(content) == content


def test_human_text_with_lint_first_but_no_typecheck_second_is_not_masked():
    content = "Lint: we use ruff here\nRun the e2e suite against staging"
    assert _mask_stale_machine_testing_notes(content) == content


def test_machine_prefix_but_third_line_not_notes_is_not_masked():
    content = "Lint: pass\nTypecheck: pass\nAlso: remember to bump the version"
    assert _mask_stale_machine_testing_notes(content) == content


def test_empty_and_none_pass_through():
    assert _mask_stale_machine_testing_notes(None) is None
    assert _mask_stale_machine_testing_notes("") == ""
    assert _mask_stale_machine_testing_notes("   \n  ") == "   \n  "


def test_helper_is_idempotent_and_side_effect_free():
    human = "Smoke-test the checkout flow by hand before every release."
    assert _mask_stale_machine_testing_notes(_mask_stale_machine_testing_notes(human)) == human
    machine = "Lint: pass\nTypecheck: pass\nNotes: all green"
    assert _mask_stale_machine_testing_notes(machine) is None
    assert _mask_stale_machine_testing_notes(machine) is None


# -- #112: endpoint behavior ----------------------------------------------------


def test_get_app_masks_stale_machine_testing_notes(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    mem = Memory(repo)
    summary = "Lint: not_configured\nTypecheck: pass\nNotes: Verified the feature."
    mem.write("architecture", "testing-notes", summary)
    mem.write("architecture", "local-test-summary", summary)

    body = TestClient(server.app).get("/apps/myapp").json()

    assert body["testing_notes"] is None
    assert body["local_test_summary"] == summary  # real machine summary still surfaced under its own key
    assert mem.read("architecture", "testing-notes") == summary  # read did not mutate storage


def test_get_app_returns_genuine_human_testing_notes_verbatim_and_repeatedly(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    mem = Memory(repo)
    human_note = "Run the manual smoke checklist before release."
    mem.write("architecture", "testing-notes", human_note)

    client = TestClient(server.app)
    for _ in range(3):
        assert client.get("/apps/myapp").json()["testing_notes"] == human_note

    assert mem.read("architecture", "testing-notes") == human_note


def test_get_app_other_fields_unaffected_by_masking(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    repo = _register_tmp_app(tmp_path)
    mem = Memory(repo)
    mem.write("architecture", "testing-notes", "Lint: pass\nTypecheck: pass")
    mem.write("architecture", "documentation", "See the runbook in docs/.")

    body = TestClient(server.app).get("/apps/myapp").json()

    assert body["testing_notes"] is None
    assert body["documentation_notes"] == "See the runbook in docs/."
    assert body["objective"] == "Ship useful dashboard improvements."
    assert "pre_prod_branch" in body and "known_bugs" in body
    assert body["name"] == "myapp"


def test_get_app_404_for_unknown_app(tmp_path, monkeypatch):
    _isolate_registry(tmp_path, monkeypatch)
    assert TestClient(server.app).get("/apps/nope").status_code == 404
